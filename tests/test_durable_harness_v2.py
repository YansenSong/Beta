from __future__ import annotations

from dataclasses import replace

import pytest

from beta_agent import AgentMessage, Message, ScriptedModelAdapter, SessionTree
from beta_agent.agent import Agent
from beta_agent.harness.durable import MemoryStorage
from beta_agent.harness.durable import DurableStateConflict
from beta_agent.harness.durable.runtime import DurableAgentHarness
from beta_agent.harness.durable.runtime.checkpoint import (AssistantEffectPendingState, CheckpointState,
    OperationMeta, StartingState, ToolsState, operation_meta_from_json,
    operation_meta_to_json, operation_state_from_json, operation_state_to_json)
from beta_agent.extensions.bridge import bind_extensions
from beta_agent.extensions.runner import ExtensionRunner
from beta_agent.extensions.types import RuntimeConfig


@pytest.mark.parametrize("state", [
    StartingState(), CheckpointState(1,"need_assistant",None,("t",)),
    AssistantEffectPendingState(2,"m","r",None,"now"), ToolsState(3,"b","a",("o",)),
])
def test_operation_state_codec_round_trip(state):
    assert operation_state_from_json(operation_state_to_json(state)) == state
    meta=OperationMeta("o","s","agent_run","prompt","now",("m",),None)
    assert operation_meta_from_json(operation_meta_to_json(meta)) == meta


async def _harness(tmp_path, model):
    session=SessionTree(); agent=Agent(model=model)
    runner=ExtensionRunner(cwd=tmp_path,config=RuntimeConfig(),session=session)
    host=bind_extensions(agent,runner,persist_messages=False)
    storage=MemoryStorage();await storage.open()
    return DurableAgentHarness(storage=storage,agent=agent,host=host,session=session,
        session_file=tmp_path/"session.jsonl",session_id="session"),storage,session


async def test_accept_is_separate_and_one_open_run(tmp_path):
    harness,storage,_=await _harness(tmp_path,ScriptedModelAdapter([Message.assistant("ok")]))
    admission=await harness.accept("hello")
    snapshot=await harness.inspect(admission.operation_id)
    assert (snapshot.status,snapshot.state_kind)==("pending","starting")
    with pytest.raises(DurableStateConflict): await harness.accept("second")
    assert (await storage.scan_pending_outbox(task_id=admission.operation_id))[0].message["role"]=="user"


async def test_drive_materializes_input_and_assistant(tmp_path):
    harness,storage,session=await _harness(tmp_path,ScriptedModelAdapter([Message.assistant("ok")]))
    admission=await harness.accept("hello");outcome=await harness.drive(admission.operation_id)
    assert outcome.status=="completed"
    assert [(m.role,m.text) for m in session.reconstruct_messages()]==[("user","hello"),("assistant","ok")]
    task=await storage.get_task(admission.operation_id)
    assert task.status=="terminal" and task.checkpoint is None


async def test_orphan_provider_intent_is_not_replayed(tmp_path):
    model=ScriptedModelAdapter([Message.assistant("must not run")])
    harness,storage,session=await _harness(tmp_path,model)
    admission=await harness.accept("hello");task=await storage.get_task(admission.operation_id)
    pending=replace(task,status="running",checkpoint=operation_state_to_json(
        AssistantEffectPendingState(0,"response","request",None,"now")),revision=1)
    await storage.replace_task_cas(pending,expected_revision=0)
    outcome=await harness.drive(admission.operation_id)
    assert outcome.status=="failed" and outcome.reason=="provider_unknown_outcome"
    assert session.reconstruct_messages()[-1].metadata["effect_may_have_occurred"] is True
    assert len(model._responses)==1
