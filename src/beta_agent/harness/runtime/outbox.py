from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from ...messages import utc_now_iso
from ...session import SessionTree, agent_message_from_dict
from ...types import AgentEvent
from .failpoints import Failpoint, NoopFailpoint
from ...durable.types import OutboxRecord
from ...durable import DurableStorage


class SessionOutboxPublisher:
    def __init__(self, storage: DurableStorage, session: SessionTree,
                 session_file: str | Path | None, *, failpoint: Failpoint | None = None) -> None:
        self.storage = storage; self.session = session
        self.session_file = Path(session_file) if session_file is not None else None
        self.failpoint = failpoint or NoopFailpoint()

    async def publish_record(self, record: OutboxRecord,
                             *, emit: Callable[[AgentEvent], Awaitable[None]] | None = None) -> None:
        message = agent_message_from_dict(record.message)
        existing_count = len(self.session.entries)
        self.session.append_message(message)
        if self.session_file is not None: self.session.save_jsonl(self.session_file)
        suffix = "tool" if record.owner_kind == "tool" else ("assistant" if record.owner_kind in ("assistant", "recovery") else "input")
        await self.failpoint.hit(f"after_{suffix}_session_saved_before_outbox_ack")
        await self.storage.mark_outbox_published(record.id, utc_now_iso())
        if emit is not None and len(self.session.entries) != existing_count:
            await emit(AgentEvent(type="message_start", message=message))
            await emit(AgentEvent(type="message_end", message=message))

    async def publish_group(self, task_id: str, group_id: str,
                            *, emit: Callable[[AgentEvent], Awaitable[None]] | None = None) -> list[str]:
        published: list[str] = []
        for record in await self.storage.scan_pending_outbox(task_id=task_id, group_id=group_id):
            await self.publish_record(record, emit=emit); published.append(record.durable_message_id)
        return published

    async def publish_task(self, task_id: str,
                           *, emit: Callable[[AgentEvent], Awaitable[None]] | None = None) -> list[str]:
        published: list[str] = []
        for record in await self.storage.scan_pending_outbox(task_id=task_id):
            await self.publish_record(record, emit=emit); published.append(record.durable_message_id)
        return published
