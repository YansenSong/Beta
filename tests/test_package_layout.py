from beta_agent import (
    AgentMessage,
    CancellationToken,
    EventStream,
    ModelAdapter,
    ProviderRequestOptions,
    ScriptedModelAdapter,
    collapse_transcript,
)
from beta_agent.agent import Agent


def test_root_exports_use_canonical_modules():
    from beta_agent.providers.model import ModelAdapter as CanonicalModelAdapter
    from beta_agent.providers.policy import ProviderRequestOptions as CanonicalProviderRequestOptions
    from beta_agent.runtime.cancellation import CancellationToken as CanonicalCancellationToken
    from beta_agent.runtime.events import EventStream as CanonicalEventStream
    from beta_agent.runtime.transcript import collapse_transcript as canonical_collapse_transcript

    assert CancellationToken is CanonicalCancellationToken
    assert EventStream is CanonicalEventStream
    assert ModelAdapter is CanonicalModelAdapter
    assert ProviderRequestOptions is CanonicalProviderRequestOptions
    assert collapse_transcript is canonical_collapse_transcript


def test_feature_modules_are_packages_with_stable_exports():
    from beta_agent.compaction import compact_session
    from beta_agent.compaction.session import compact_session as canonical_compact_session
    from beta_agent.session import SessionTree
    from beta_agent.session.tree import SessionTree as CanonicalSessionTree
    from beta_agent.skills import SkillCatalog
    from beta_agent.skills.catalog import SkillCatalog as CanonicalSkillCatalog
    from beta_agent.tools import ToolRuntime
    from beta_agent.tools.runtime import ToolRuntime as CanonicalToolRuntime

    assert compact_session is canonical_compact_session
    assert SessionTree is CanonicalSessionTree
    assert SkillCatalog is CanonicalSkillCatalog
    assert ToolRuntime is CanonicalToolRuntime


async def test_agent_facade_delegates_to_split_loop():
    model = ScriptedModelAdapter([AgentMessage.assistant("ok")])
    agent = Agent(model=model)

    result = await agent.run("hello")

    assert result[-1].role == "assistant"
    assert result[-1].text == "ok"
