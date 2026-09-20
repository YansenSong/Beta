from dataclasses import replace

import pytest

from beta_agent import AgentMessage, SessionTree, ToolCall
from beta_agent.durable import MemoryStorage, SQLiteStorage
from beta_agent.durable.records import OutboxRecord, TaskRecord, ToolOperationRecord
from beta_agent.durable.serialization import arguments_hash
from beta_agent.messages import utc_now_iso
from coding_agent.tools import create_coding_tools


def _task() -> TaskRecord:
    now = utc_now_iso()
    return TaskRecord("task", "session", "agent_run", 1, "running", {}, {"phase": "agent_loop"}, None, False, False, now, now)


def _operation() -> ToolOperationRecord:
    now = utc_now_iso()
    arguments = {"path": "a.txt"}
    return ToolOperationRecord("operation", "task", "task", "batch", 0, "call", "read_file", arguments, arguments_hash(arguments), "safe", "effect_pending", 1, None, None, None, now, now)


@pytest.mark.parametrize("factory", [MemoryStorage, lambda: SQLiteStorage(":memory:")])
async def test_storage_effect_and_outbox_transaction(factory):
    storage = factory()
    await storage.open()
    await storage.create_task(_task())
    operation = _operation()
    await storage.create_tool_batch_intents([operation])
    message = AgentMessage.tool_result(tool_call_id="call", name="read_file", content="ok", durable_message_id="message")
    from beta_agent import agent_message_to_dict
    settled = replace(operation, status="outcome_ready", result=agent_message_to_dict(message), is_error=False, terminate=False)
    outbox = OutboxRecord("outbox", operation.id, operation.batch_id, 0, "message", agent_message_to_dict(message), "pending", utc_now_iso(), None)
    await storage.settle_operation(settled, outbox)
    assert [item.id for item in await storage.scan_pending_outbox()] == ["outbox"]
    await storage.mark_outbox_published(operation.id, utc_now_iso())
    assert (await storage.get_operation(operation.id)).status == "completed"
    await storage.close()


def test_session_durable_message_is_idempotent_and_conflicts(tmp_path):
    session = SessionTree()
    message = AgentMessage.tool_result(tool_call_id="call", name="read", content="one", durable_message_id="stable")
    assert session.append_message(message) is session.append_message(message.copy())
    with pytest.raises(ValueError, match="Conflicting durable message"):
        session.append_message(message.copy(content="two"))
    path = tmp_path / "session.jsonl"
    session.save_jsonl(path)
    assert len(SessionTree.load_jsonl(path).entries) == 1


def test_coding_tool_replay_policies_and_atomic_write(tmp_path):
    tools = {tool.name: tool for tool in create_coding_tools(tmp_path)}
    assert {name: tool.replay_policy for name, tool in tools.items()} == {
        "read_file": "safe", "grep": "safe", "write_file": "safe", "edit": "unsafe", "bash": "unsafe"
    }
