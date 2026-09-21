from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Literal, Sequence

from ...runtime.cancellation import CancellationToken
from ...messages import utc_now_iso
from ...session import agent_message_to_dict
from ...types import AgentEvent, AgentMessage
from .tools import DurableToolCoordinator
from .errors import DurableStaleOperation
from .failpoints import Failpoint, NoopFailpoint
from .outbox import SessionOutboxPublisher
from ...durable.types import OutboxRecord, TaskOutcome, TaskRecord
from .checkpoint import (AssistantEffectPendingState, CheckpointState, OperationMeta,
                    StartingState, operation_meta_to_json, operation_state_from_json,
                    operation_state_to_json)
from ...durable import DurableStorage


@dataclass(frozen=True, slots=True)
class OperationAdmission:
    operation_id: str
    session_id: str
    kind: Literal["agent_run"]
    accepted_at: str


@dataclass(frozen=True, slots=True)
class DurableExecutionSnapshot:
    operation_id: str
    status: str
    state_kind: str | None
    abort_requested: bool
    outcome: TaskOutcome | None


class DurableAgentHarness:
    def __init__(self, *, storage: DurableStorage, agent, host, session, session_file,
                 session_id: str, failpoint: Failpoint | None = None) -> None:
        self.storage=storage; self.agent=agent; self.host=host; self.session=session
        self.session_id=session_id; self.failpoint=failpoint or NoopFailpoint()
        self.publisher=SessionOutboxPublisher(storage,session,session_file,failpoint=self.failpoint)
        self._drives: dict[str, asyncio.Future[TaskOutcome]]={}
        self._tokens: dict[str,CancellationToken]={}

    async def accept(self, prompt: str | AgentMessage | Sequence[AgentMessage]) -> OperationAdmission:
        messages=self.agent._normalize_prompts(prompt)
        return await self._accept("prompt",messages)

    async def accept_continue(self) -> OperationAdmission:
        if not self.agent.messages: raise ValueError("Cannot continue: no messages in context")
        messages=[]
        if self.agent.messages[-1].role=="assistant":
            token=CancellationToken()
            messages=await self.agent._drain_steering(token)
            if not messages: messages=await self.agent._drain_follow_up(token)
            if not messages: raise ValueError("Cannot continue from an assistant message")
        return await self._accept("continue",messages)

    async def _accept(self,mode,messages)->OperationAdmission:
        operation_id=uuid.uuid4().hex; now=utc_now_iso(); boxes=[]; message_ids=[]
        for index,message in enumerate(messages):
            message_id=uuid.uuid4().hex; message_ids.append(message_id)
            durable=message.copy(metadata={**message.metadata,"durable_message_id":message_id,
                "durable_task_id":operation_id,"durable_turn_index":0,"durable_kind":"input"})
            boxes.append(OutboxRecord(uuid.uuid4().hex,operation_id,"input",message_id,operation_id,0,index,
                message_id,agent_message_to_dict(durable),"pending",now,None))
        meta=OperationMeta(operation_id,self.session_id,"agent_run",mode,now,tuple(message_ids),self.session.leaf_id)
        task=TaskRecord(operation_id,self.session_id,"agent_run",2,"pending",operation_meta_to_json(meta),
            operation_state_to_json(StartingState()),None,False,False,now,now,0)
        await self.storage.accept_task(task,boxes); await self.failpoint.hit("after_accept_committed")
        return OperationAdmission(operation_id,self.session_id,"agent_run",now)

    async def inspect(self,operation_id:str)->DurableExecutionSnapshot:
        task=await self.storage.get_task(operation_id)
        if task is None: raise KeyError(operation_id)
        kind=None if task.checkpoint is None else operation_state_from_json(task.checkpoint).kind
        return DurableExecutionSnapshot(task.id,task.status,kind,task.abort_requested,task.outcome)

    async def request_abort(self,operation_id:str|None=None)->None:
        if operation_id is None:
            task=await self.storage.get_open_task(self.session_id)
            if task is None:return
            operation_id=task.id
        await self.storage.request_abort(operation_id); await self.failpoint.hit("after_abort_committed")
        token=self._tokens.get(operation_id)
        if token is not None: token.cancel()
        # Also cancel the process-local Agent/provider/tool work.
        self.host.abort()

    async def drive(self,operation_id:str,*,emit:Callable[[AgentEvent],Awaitable[None]]|None=None)->TaskOutcome:
        existing=self._drives.get(operation_id)
        if existing is not None:return await asyncio.shield(existing)
        loop=asyncio.get_running_loop(); future=loop.create_future(); self._drives[operation_id]=future
        try:
            outcome=await self._drive_owned(operation_id,emit or _noop_emit); future.set_result(outcome); return outcome
        except BaseException as exc:
            if not future.done():future.set_exception(exc); future.exception()
            raise
        finally:self._drives.pop(operation_id,None);self._tokens.pop(operation_id,None)

    async def _drive_owned(self,operation_id,emit)->TaskOutcome:
        task=await self.storage.get_task(operation_id)
        if task is None:raise KeyError(operation_id)
        current=await self.storage.get_open_task(task.session_id)
        if current is not None and current.id!=operation_id:raise DurableStaleOperation(operation_id)
        if task.status=="terminal":return task.outcome
        if task.status=="pending":
            updated=replace(task,status="running",updated_at=utc_now_iso(),revision=task.revision+1)
            await self.storage.replace_task_cas(updated,expected_revision=task.revision);task=updated
        state=operation_state_from_json(task.checkpoint)
        if task.abort_requested:return await self._terminal(task,"aborted","abort_requested")
        if isinstance(state,AssistantEffectPendingState):return await self._unknown_provider(task,state)
        if isinstance(state,StartingState):
            await self.publisher.publish_group(task.id,task.id,emit=emit)
            self.agent.replace_messages(self.session.reconstruct_messages())
            checkpoint=replace(task,checkpoint=operation_state_to_json(CheckpointState(0,"need_assistant",None,())),updated_at=utc_now_iso(),revision=task.revision+1)
            await self.storage.replace_task_cas(checkpoint,expected_revision=task.revision);task=checkpoint
        request_id=uuid.uuid4().hex; response_id=uuid.uuid4().hex
        pending=replace(task,checkpoint=operation_state_to_json(AssistantEffectPendingState(0,response_id,request_id,getattr(self.agent.model,"model",None),utc_now_iso())),updated_at=utc_now_iso(),revision=task.revision+1)
        await self.storage.replace_task_cas(pending,expected_revision=task.revision);await self.failpoint.hit("after_assistant_intent_committed")
        token=CancellationToken();self._tokens[operation_id]=token
        self.agent.config.tool_coordinator=DurableToolCoordinator(self.storage,task_id=task.id,run_id=task.id)
        try:
            stream=self.host.stream([])
            async for event in stream:await emit(event)
            generated=await stream.result()
        finally:self.agent.config.tool_coordinator=None
        await self.failpoint.hit("after_provider_returned_before_assistant_settlement")
        boxes=[];now=utc_now_iso();assistant_index=0
        for message in generated:
            if message.role=="tool":continue
            message_id=response_id if assistant_index==0 else uuid.uuid4().hex
            durable=message.copy(metadata={**message.metadata,"durable_message_id":message_id,"durable_task_id":task.id,
                "durable_turn_index":assistant_index,"durable_kind":"assistant"})
            boxes.append(OutboxRecord(uuid.uuid4().hex,task.id,"assistant",message_id,request_id,assistant_index,assistant_index,
                message_id,agent_message_to_dict(durable),"pending",now,None));assistant_index+=1
        status="completed"
        if any(m.stop_reason=="error" for m in generated if m.role=="assistant"):status="failed"
        if any(m.stop_reason=="aborted" for m in generated if m.role=="assistant"):status="aborted"
        terminal=replace(pending,status="terminal",checkpoint=None,outcome=TaskOutcome(status,result={"message_count":len(generated)}),updated_at=now,revision=pending.revision+1)
        await self.storage.replace_task_with_outbox_cas(terminal,boxes,expected_revision=pending.revision)
        await self.failpoint.hit("after_assistant_settlement_committed")
        # Cross-group UUID ordering is meaningless.  Materialize the complete
        # live run in its source transcript order (assistant/tool/assistant),
        # while each tool batch still uses source_index internally.
        pending_boxes={box.durable_message_id:box for box in await self.storage.scan_pending_outbox(task_id=task.id)}
        assistant_ids=iter([box.durable_message_id for box in boxes])
        for message in generated:
            message_id=(message.metadata.get("durable_message_id") if message.role=="tool" else next(assistant_ids,None))
            box=pending_boxes.get(message_id)
            if box is not None:await self.publisher.publish_record(box)
        # Defensive completion for artifacts that were staged but not present
        # in the process-local result (for example a recovery-normalized tool).
        await self.publisher.publish_task(task.id)
        self.agent.replace_messages(self.session.reconstruct_messages());await self.failpoint.hit("after_terminal_committed")
        return terminal.outcome

    async def _unknown_provider(self,task,state)->TaskOutcome:
        aborted=task.abort_requested;message_id=state.response_message_id;now=utc_now_iso()
        text=("The provider request was interrupted before a durable final response was recorded. "
              "The external request outcome is unknown and may have completed remotely. "
              "This runtime will not replay the request automatically.")
        message=AgentMessage.assistant(text,stop_reason="aborted" if aborted else "error",
            durable_message_id=message_id,durable_task_id=task.id,durable_turn_index=state.turn_index,
            durable_kind="recovery",durable_recovery=True,effect_may_have_occurred=True)
        box=OutboxRecord(uuid.uuid4().hex,task.id,"recovery",message_id,state.request_id,state.turn_index,0,message_id,agent_message_to_dict(message),"pending",now,None)
        terminal=replace(task,status="terminal",checkpoint=None,outcome=TaskOutcome("aborted" if aborted else "failed",reason="provider_unknown_outcome"),updated_at=now,revision=task.revision+1)
        await self.storage.replace_task_with_outbox_cas(terminal,(box,),expected_revision=task.revision);await self.publisher.publish_record(box)
        return terminal.outcome

    async def _terminal(self,task,status,reason):
        outcome=TaskOutcome(status,reason=reason);await self.storage.terminalize_task(task.id,outcome,utc_now_iso());return outcome


async def _noop_emit(event:AgentEvent)->None:del event
