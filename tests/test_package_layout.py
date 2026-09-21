from beta_agent import AgentMessage, ScriptedModelAdapter
from beta_agent.agent import Agent


def test_legacy_runtime_imports_forward_to_canonical_modules():
    from beta_agent.cancellation import CancellationToken as LegacyCancellationToken
    from beta_agent.events import EventStream as LegacyEventStream
    from beta_agent.model import ModelAdapter as LegacyModelAdapter
    from beta_agent.provider_policy import ProviderRequestOptions as LegacyProviderRequestOptions
    from beta_agent.transcript import collapse_transcript as legacy_collapse_transcript

    from beta_agent.providers.model import ModelAdapter
    from beta_agent.providers.policy import ProviderRequestOptions
    from beta_agent.runtime.cancellation import CancellationToken
    from beta_agent.runtime.events import EventStream
    from beta_agent.runtime.transcript import collapse_transcript

    assert LegacyCancellationToken is CancellationToken
    assert LegacyEventStream is EventStream
    assert LegacyModelAdapter is ModelAdapter
    assert LegacyProviderRequestOptions is ProviderRequestOptions
    assert legacy_collapse_transcript is collapse_transcript


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
