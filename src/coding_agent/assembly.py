from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beta_agent.agent import Agent
from beta_agent.runtime.events import EventStream
from beta_agent.durable import SQLiteStorage, TaskOutcome
from beta_agent.harness.runtime import (
    DurableAgentHarness,
    DurableExecutionSnapshot,
    OperationAdmission,
    RecoveryReport,
    recover_durable_runtime,
)
from beta_agent.messages import utc_now_iso
import uuid
from beta_agent.compaction import Summarizer, compact_session
from beta_agent.extensions import ExtensionFactory, ExtensionHost, ExtensionRunner, RuntimeConfig, bind_extensions
from beta_agent.providers.model import ModelAdapter
from beta_agent.session import SessionTree
from beta_agent.skills import Skill, SkillCatalog
from beta_agent.tools import Tool
from beta_agent.runtime.transcript import create_initial_system_message
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
    durable_harness: DurableAgentHarness | None = None
    _closed: bool = field(default=False, init=False, repr=False)
    _active_durable_operation_id: str | None = field(default=None, init=False, repr=False)

    def stream(self, prompt: str):
        if self.durable_harness is None:
            return self.host.stream(prompt)
        async def drive(emit):
            admission=await self.durable_harness.accept(prompt)
            self._active_durable_operation_id=admission.operation_id
            try:
                before=len(self.session.reconstruct_messages())
                await self.durable_harness.drive(admission.operation_id,emit=emit)
                return self.session.reconstruct_messages()[before:]
            finally:self._active_durable_operation_id=None
        import asyncio
        operation={"id":None}
        async def cancel(_):
            if operation["id"] is not None: await self.durable_harness.request_abort(operation["id"])
        return EventStream(drive, on_cancel=lambda _: self.host.abort())

    async def run(self, prompt: str):
        return await self.stream(prompt).result()

    def continue_stream(self):
        if self.durable_harness is None:return self.host.continue_stream()
        async def drive(emit):
            admission=await self.durable_harness.accept_continue();self._active_durable_operation_id=admission.operation_id
            try:
                before=len(self.session.reconstruct_messages())
                await self.durable_harness.drive(admission.operation_id,emit=emit)
                return self.session.reconstruct_messages()[before:]
            finally:self._active_durable_operation_id=None
        return EventStream(drive,on_cancel=lambda _:self.host.abort())

    def abort(self) -> None:
        self.host.abort()
        if self.durable_harness is not None and self._active_durable_operation_id is not None:
            try:
                asyncio.get_running_loop().create_task(
                    self.durable_harness.request_abort(self._active_durable_operation_id)
                )
            except RuntimeError:
                pass

    async def request_abort(self, operation_id: str | None = None) -> None:
        if self.durable_harness is None:
            self.host.abort(); return
        await self.durable_harness.request_abort(operation_id)

    async def accept(self, prompt) -> OperationAdmission:
        if self.durable_harness is None: raise RuntimeError("Durable runtime is not enabled")
        return await self.durable_harness.accept(prompt)

    async def drive(self, operation_id: str, *, emit=None) -> TaskOutcome:
        if self.durable_harness is None: raise RuntimeError("Durable runtime is not enabled")
        return await self.durable_harness.drive(operation_id, emit=emit)

    async def inspect(self, operation_id: str) -> DurableExecutionSnapshot:
        if self.durable_harness is None: raise RuntimeError("Durable runtime is not enabled")
        return await self.durable_harness.inspect(operation_id)

    async def resume_pending(self, operation_id: str | None = None) -> TaskOutcome | None:
        if self.durable_harness is None: raise RuntimeError("Durable runtime is not enabled")
        if operation_id is None:
            task=await self.durable_storage.get_open_task(self.durable_harness.session_id)
            if task is None:return None
            operation_id=task.id
        return await self.durable_harness.drive(operation_id)

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
    host = bind_extensions(agent, runner, persist_messages=options.durable is None)
    storage = None
    recovery_report = None
    if options.durable is not None:
        database_path = _workspace_path(cwd, options.durable.database_path)
        storage = SQLiteStorage(database_path)
        await storage.open()
        recovery_report = await recover_durable_runtime(
            storage, session, session_file, tools, after_tool_call=agent.config.after_tool_call
        )
    durable_harness = None
    durable_session_id = options.durable.session_id if options.durable is not None else None
    if storage is not None:
        durable_session_id = durable_session_id or "default"
        durable_harness = DurableAgentHarness(storage=storage,agent=agent,host=host,session=session,
            session_file=session_file,session_id=durable_session_id)
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
        durable_session_id=durable_session_id,
        durable_harness=durable_harness,
    )
