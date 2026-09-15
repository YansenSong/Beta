from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beta_agent.agent import Agent
from beta_agent.compaction import Summarizer, compact_session
from beta_agent.extensions import ExtensionFactory, ExtensionHost, ExtensionRunner, RuntimeConfig, bind_extensions
from beta_agent.model import ModelAdapter
from beta_agent.session import SessionTree
from beta_agent.skills import Skill, SkillCatalog
from beta_agent.tools import Tool
from .prompt import build_coding_system_prompt
from .tools import create_coding_tools


@dataclass(slots=True)
class CodingCompactionOptions:
    keep_last_messages: int
    summarize: Summarizer
    estimate_tokens: Callable[[list[Any]], int] | None = None


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


@dataclass(slots=True)
class CodingAgentRuntime:
    """确保常规 Coding Agent 调用统一经过 ExtensionHost 的 product facade。"""

    agent: Agent
    host: ExtensionHost
    runner: ExtensionRunner
    session: SessionTree
    tools: list[Tool[Any]]
    skills: SkillCatalog
    cwd: Path
    session_file: Path | None
    compaction: CodingCompactionOptions | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def stream(self, prompt: str):
        return self.host.stream(prompt)

    async def run(self, prompt: str):
        return await self.host.run(prompt)

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
        # ExtensionHost 已经会 append 每个 message_end event。
        # 特别注意，这里不要再次 append agent.messages。
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

    def close(self) -> None:
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
    """基于现有 Core 和 Extension Runtime 组装 Coding Agent。"""

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
    session_file = None if options.session_file is None else _workspace_path(cwd, options.session_file)
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
    agent = Agent(model=options.model, system_prompt=system_prompt, tools=tools, messages=initial_messages)
    host = bind_extensions(agent, runner, persist_messages=True)
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
    )
