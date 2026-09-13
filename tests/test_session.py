from beta_agent import Message, SessionTree, compact_session


async def test_session_branching_and_compaction_are_append_only():
    session = SessionTree()
    u1 = session.append_message(Message.user("u1"))
    a1 = session.append_message(Message.assistant("a1"))
    session.append_message(Message.user("u2"))
    session.append_message(Message.assistant("a2"))

    session.branch(a1.id)
    session.append_message(Message.user("u2-alt"))
    session.append_message(Message.assistant("a2-alt"))
    assert [m.content for m in session.reconstruct_messages()] == ["u1", "a1", "u2-alt", "a2-alt"]

    session.append_message(Message.user("u3"))
    session.append_message(Message.assistant("a3"))
    before = len(session.entries)
    entry = await compact_session(
        session,
        keep_last_messages=2,
        summarize=lambda messages: "summary: " + ",".join(m.content for m in messages),
    )
    assert entry is not None
    assert len(session.entries) == before + 1
    reconstructed = session.reconstruct_messages()
    assert reconstructed[0].role == "system"
    assert "summary:" in reconstructed[0].content
    assert reconstructed[-2].content == "u3"
    assert reconstructed[-1].content == "a3"
    assert u1.id in session.by_id
