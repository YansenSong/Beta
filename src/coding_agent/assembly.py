from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beta_agent.agent import Agent
from beta_agent.events import EventStream
from beta_agent.durable import SQLiteStorage
from beta_agent.durable.coordinator import DurableToolCoordinator
from beta_agent.durable.records import TaskOutcome, TaskRecord
from beta_agent.durable.recovery import RecoveryReport, recover_durable_runtime
from beta_agent.messages import utc_now_iso
import uuid
from beta_agent.compaction import Summarizer, compact_session
from beta_agent.extensions import ExtensionFactory, ExtensionHost, ExtensionRunner, RuntimeConfig, bind_extensions
from beta_agent.model import ModelAdapter
from beta_agent.session import SessionTree
from beta_agent.skills import Skill, SkillCatalog
from beta_agent.tools import Tool
from beta_agent.transcript import create_initial_system_message
from .prompt import build_coding_system_prompt
from .tools import create_coding_tools


@dataclass(slots=True)
class CodingCompactionOptions:
    keep_last_messages: int
    summarize: Summarizer
    estimate_tokens: Callable[[list[Any]], int] | None = None


@dataclass(slots=True)
class DurableRuntimeOptions:
    database_path: str | Path
    session_file: str | Path
    session_id: str | None = None


@dataclass(slots=True)
class CodingAgentOptions:
    cwd: str | Path
    model: ModelAdapter
    model_name: str | None = None
    system_prompt_prefix: str | None = None
    append_system_prompt: str | None = None
    skill_roots: Sequence[str | Path] = ()
    extensions: Sequence[ExtensionFactory] = ()
    extra_tools: Sequence[Tool[Any]] = ()
    child_model_factory: Callable[[], ModelAdapter] | None = None
    session_file: str | Path | None = None
    compaction: CodingCompactionOptions | None = None
    durable: DurableRuntimeOptions | None = None


@dataclass(slots=True)
class CodingAgentRuntime:
    """产品层 facade，确保普通 Coding Agent 调用都走 ExtensionHost。"""

    agent: Agent
    host: ExtensionHost
    runner: ExtensionRunner
    session: SessionTree
    tools: list[Tool[Any]]
    skills: SkillCatalog
    cwd: Path
    session_file: Path | None
    compaction: CodingCompactionOptions | None = None
    durable_storage: SQLiteStorage | None = None
    recovery_report: RecoveryReport | None = None
    durable_session_id: str | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def stream(self, prompt: str):
        if self.durable_storage is None:
            return self.host.stream(prompt)

        child = None
        async def drive(emit):
            nonlocal child
            task_id = uuid.uuid4().hex
            now = utc_now_iso()
            task = TaskRecord(task_id, self.durable_session_id or task_id,
                "agent_run", 1, "running", {"prompt": prompt},
                {"phase": "agent_loop", "session_leaf_id": self.session.leaf_id,
                 "started_message_count": len(self.agent.messages)}, None, False, False, now, now)
            await self.durable_storage.create_task(task)
            self.agent.config.tool_coordinator = DurableToolCoordinator(self.durable_storage, task_id=task_id, run_id=task_id)
            child = self.host.stream(prompt)
            try:
                async for event in child:
                    await emit(event)
                result = await child.result()
                await self.durable_storage.terminalize_task(task_id, TaskOutcome("completed", result={"message_count": len(result)}), utc_now_iso())
                return result
            except BaseException as exc:
                status = "aborted" if isinstance(exc, asyncio.CancelledError) else "failed"
                await self.durable_storage.terminalize_task(task_id, TaskOutcome(status, reason=str(exc)), utc_now_iso())
                raise
            finally:
                self.agent.config.tool_coordinator = None
        import asyncio
        return EventStream(drive, on_cancel=lambda _: child.cancel() if child is not None else None)

    async def run(self, prompt: str):
        return await self.stream(prompt).result()

    def continue_stream(self):
        return self.host.continue_stream()

    def abort(self) -> None:
        self.host.abort()

    async def wait_for_idle(self) -> None:
        await self.host.wait_for_idle()

    @property
    def is_running(self) -> bool:
        return self.host.is_running

    async def run_command(self, command: str) -> None:
        await self.host.run_command(command)

    async def save_session(self) -> None:
        # ExtensionHost 已经会 append 每一个 message_end event；
        # 尤其不要在这里再次 append agent.messages。
        if self.compaction is not None:
            entry = await compact_session(
                self.session,
                keep_last_messages=self.compaction.keep_last_messages,
                summarize=self.compaction.summarize,
                estimate_tokens=self.compaction.estimate_tokens,
            )
            if entry is not None:
                self.agent.replace_messages(self.session.reconstruct_messages())

        if self.session_file is not None:
            self.session.save_jsonl(self.session_file)

    async def aclose(self) -> None:
        if not self._closed:
            self.host.close()
            if self.durable_storage is not None:
                await self.durable_storage.close()
            self._closed = True

    def close(self) -> None:
        if self.durable_storage is not None:
            raise RuntimeError("Durable runtime requires await runtime.aclose()")
        if not self._closed:
            self.host.close()
            self._closed = True


def _workspace_path(cwd: str | Path, path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve()


def _discover_skills(cwd: Path, roots: Sequence[str | Path]) -> SkillCatalog:
    selected_roots = list(roots)
    if not selected_roots and (cwd / "skills").is_dir():
        selected_roots = [cwd / "skills"]

    skills: list[Skill] = []
    seen: dict[str, Path] = {}
    for root in selected_roots:
        root_path = _workspace_path(cwd, root)
        discovered = SkillCatalog.discover(root_path)
        for skill in discovered.skills:
            if skill.name in seen:
                raise ValueError(
                    f"Duplicate skill name {skill.name!r} discovered at {seen[skill.name]} and {skill.location}"
                )
            seen[skill.name] = skill.location
            skills.append(skill)
    return SkillCatalog(skills)


def _check_unique_tools(tools: Sequence[Tool[Any]], *, source: str) -> None:
    seen: dict[str, str] = {}
    for tool in tools:
        if tool.name in seen:
            raise ValueError(f"Duplicate tool name {tool.name!r} from {seen[tool.name]} and {source}")
        seen[tool.name] = source


async def create_coding_agent(options: CodingAgentOptions) -> CodingAgentRuntime:
    """使用现有 Core 和 Extension Runtime 组装 Coding Agent。"""

    cwd = Path(options.cwd).expanduser().resolve()
    if not cwd.exists():
        raise ValueError(f"Coding Agent cwd does not exist: {options.cwd!s}")
    if not cwd.is_dir():
        raise ValueError(f"Coding Agent cwd is not a directory: {options.cwd!s}")

    product_tools = create_coding_tools(cwd)
    tools: list[Tool[Any]] = [*product_tools, *options.extra_tools]
    _check_unique_tools(product_tools, source="coding tools")
    _check_unique_tools(tools, source="extra tools")

    skills = _discover_skills(cwd, options.skill_roots)
    if options.durable is not None and options.session_file is not None:
        raise ValueError("Set the session file through durable options when durable mode is enabled")
    session_setting = options.durable.session_file if options.durable is not None else options.session_file
    session_file = None if session_setting is None else _workspace_path(cwd, session_setting)
    if session_file is not None and session_file.exists():
        session = SessionTree.load_jsonl(session_file)
        initial_messages = session.reconstruct_messages()
    else:
        session = SessionTree()
        initial_messages = []

    model_name = options.model_name
    if model_name is None:
        model_name = str(getattr(options.model, "model", "") or "")

    services: dict[str, Any] = {
        "cwd": cwd,
        "skills": skills,
        "tools": tools,
        "model": options.model,
    }
    if options.child_model_factory is not None:
        services["child_model_factory"] = options.child_model_factory

    config = RuntimeConfig(
        model=model_name,
        active_tools=[tool.name for tool in tools],
        services=services,
    )
    runner = ExtensionRunner(cwd=cwd, config=config, session=session)
    await runner.load(list(options.extensions))

    registered_tools = runner.get_registered_tools()
    _check_unique_tools(registered_tools, source="extensions")
    _check_unique_tools([*tools, *registered_tools], source="extensions and coding tools")

    system_prompt = build_coding_system_prompt(
        cwd=cwd,
        tools=tools,
        skills=skills,
        prefix=options.system_prompt_prefix,
        append=options.append_system_prompt,
    )
    has_transcript_state = any(
        message.role == "system"
        and not message.metadata.get("compaction_entry_id")
        and (message.text or message.tools_added or message.tools_removed)
        for message in initial_messages
    )
    if not has_transcript_state:
        baseline = create_initial_system_message(system_prompt, tools)
        if baseline is not None:
            if session.entries:
                # Mark migration snapshots so reconstruction can keep them ahead
                # of an older compaction summary while the Session stays append-only.
                baseline = baseline.copy(
                    metadata={**baseline.metadata, "legacy_migration_baseline": True}
                )
            # New sessions and legacy-session migration points are durable from the
            # outset, before the first request or any extension event can occur.
            session.append_message(baseline)
            initial_messages = session.reconstruct_messages()

    agent = Agent(model=options.model, system_prompt=system_prompt, tools=tools, messages=initial_messages)
    host = bind_extensions(agent, runner, persist_messages=True)
    storage = None
    recovery_report = None
    if options.durable is not None:
        database_path = _workspace_path(cwd, options.durable.database_path)
        storage = SQLiteStorage(database_path)
        await storage.open()
        recovery_report = await recover_durable_runtime(
            storage, session, session_file, tools, after_tool_call=agent.config.after_tool_call
        )
        async def persist_durable_message(event, cancellation=None):
            del cancellation
            if event.type == "message_end":
                session.save_jsonl(session_file)
        agent.subscribe(persist_durable_message)
    return CodingAgentRuntime(
        agent=agent,
        host=host,
        runner=runner,
        session=session,
        tools=tools,
        skills=skills,
        cwd=cwd,
        session_file=session_file,
        compaction=options.compaction,
        durable_storage=storage,
        recovery_report=recovery_report,
        durable_session_id=(options.durable.session_id if options.durable is not None else None),
    )
