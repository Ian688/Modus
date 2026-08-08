"""Wave5 E3: session tree — in-place branch, revert, tree, steer/followUp.

Covers the tests listed in docs/dev-wave5-evolution.md E3:
- ``test_branch_creates_leaf``: forking moves the leaf pointer; mainline
  history is untouched and fully reversible.
- ``test_revert_moves_leaf``: rewinding moves the leaf; history is preserved.
- ``test_tree_structure``: the returned tree keeps every branch's nodes.
- ``test_steer_injected_before_next_llm``: a steer turn lands after the
  current turn's tool results and before the next LLM call.
- ``test_followup_queued_after_run``: a follow-up turn waits for the whole
  run to settle and is then drained.
- ``test_branch_compaction_aligned``: compression after a branch/revert
  rebuilds the branch context with the turn-aligned tail rule.
"""

from __future__ import annotations

import asyncio

import pytest

from modus.config import ModusConfig
from modus.types import Message


# ── DB helpers ─────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from modus.desktop import db as desktop_db

    desktop_db.release_writer_lease()
    monkeypatch.setattr(desktop_db, "DB_DIR", tmp_path)
    monkeypatch.setattr(desktop_db, "DB_PATH", tmp_path / "desktop.db")
    monkeypatch.setattr(desktop_db, "_integrity_checked", False)
    monkeypatch.setattr(desktop_db, "_recovering", False)
    monkeypatch.setattr(desktop_db, "_checkpoint_calls", 0)
    desktop_db.init_db()
    return desktop_db


def _linear(db, session_id: str, count: int) -> list[int]:
    """Append ``count`` implicit messages; return their ids in order."""
    ids = []
    for index in range(count):
        db.add_message(session_id, "user", f"msg-{index}")
    with db._get_conn() as conn:
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()]
    return ids


# ── session tree ──────────────────────────────────────────────────────────


def test_branch_creates_leaf(db):
    """Forking moves the leaf pointer; mainline history stays intact."""
    sess = db.create_session("branch leaf")
    ids = _linear(db, sess["id"], 3)  # mainline m1 m2 m3

    branch = db.session_branch(sess["id"], ids[1])  # fork at m2
    assert branch is not None
    assert branch["message_id"] == ids[1]

    # Leaf pointer now names the branch point.
    assert db.current_session_leaf(sess["id"]) == ids[1]

    # Mainline history is untouched (nothing deleted/copied).
    assert [m["content"] for m in db.get_session_messages(sess["id"])] == [
        "msg-0", "msg-1",
    ]

    # New appends land on the branch.
    db.add_message(sess["id"], "user", "branch-one")
    with db._get_conn() as conn:
        branch_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY id ASC",
            (sess["id"],),
        ).fetchall()]
    assert len(branch_ids) == 4  # m1 m2 m3 + new
    assert [m["content"] for m in db.get_session_messages(sess["id"])] == [
        "msg-0", "msg-1", "branch-one",
    ]
    # The downstream mainline message (msg-2, id 3) is NOT in the active branch.
    assert "msg-2" not in [m["content"] for m in db.get_session_messages(sess["id"])]


def test_branch_is_reversible_to_mainline(db):
    """Re-branching back to the original mainline point restores the lineage."""
    sess = db.create_session("branch reversible")
    ids = _linear(db, sess["id"], 3)
    db.session_branch(sess["id"], ids[1])
    db.add_message(sess["id"], "user", "branch-one")
    branch_ids = [m["id"] for m in db.get_session_messages(sess["id"])]
    assert branch_ids == [ids[0], ids[1], ids[1] + 2]

    # Revert to the pre-branch point then branch from the original root.
    db.session_revert(sess["id"], ids[1])
    db.add_message(sess["id"], "user", "mainline-two")
    assert [m["content"] for m in db.get_session_messages(sess["id"])] == [
        "msg-0", "msg-1", "mainline-two",
    ]


def test_revert_moves_leaf(db):
    """Rewinding moves the leaf; every downstream message stays on disk."""
    sess = db.create_session("revert")
    ids = _linear(db, sess["id"], 4)

    before = db.get_session_messages(sess["id"])
    assert len(before) == 4

    reverted = db.session_revert(sess["id"], ids[1])
    assert reverted is not None
    assert reverted["message_id"] == ids[1]
    assert db.current_session_leaf(sess["id"]) == ids[1]

    # History preserved: every row is still in the database and the tree.
    with db._get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id=?",
            (sess["id"],),
        ).fetchone()["n"]
    assert count == 4
    tree = db.session_tree(sess["id"])
    assert tree["message_count"] == 4
    assert len(tree["nodes"]) == 4

    # Active lineage is the rewind point + ancestors.
    assert [m["content"] for m in db.get_session_messages(sess["id"])] == [
        "msg-0", "msg-1",
    ]

    # New appends continue from the rewind point (msg-2 stays excluded).
    db.add_message(sess["id"], "user", "rewound-continue")
    assert [m["content"] for m in db.get_session_messages(sess["id"])] == [
        "msg-0", "msg-1", "rewound-continue",
    ]


def test_tree_structure(db):
    """The returned tree keeps every branch's nodes and the current leaf."""
    sess = db.create_session("tree")
    ids = _linear(db, sess["id"], 3)

    db.session_branch(sess["id"], ids[1])
    db.add_message(sess["id"], "user", "branch-one")
    branch_leaf = db.current_session_leaf(sess["id"])

    tree = db.session_tree(sess["id"])
    assert tree["current_leaf"] == branch_leaf
    assert tree["message_count"] == 4  # all branches kept
    assert len(tree["nodes"]) == 4
    # Mainline root.
    assert tree["roots"] == [ids[0]]
    # Branch pointer recorded: root stays at the fork point; the pointer's
    # leaf advances as messages append onto the branch.
    assert any(
        b["branch_root_id"] == ids[1]
        for b in tree["branches"]
    )
    current_branch = next(
        b for b in tree["branches"] if b["branch_root_id"] == ids[1]
    )
    assert current_branch["message_id"] == branch_leaf
    # Every node exposes its tree fields.
    for node in tree["nodes"].values():
        assert "parent_message_id" in node
        assert "branch_root_id" in node
        assert "children" in node
    # The branch node's parent is the mainline node it forked from.
    branch_node = tree["nodes"][branch_leaf]
    assert branch_node["parent_message_id"] == ids[1]
    assert branch_node["branch_root_id"] == ids[1]


def test_tree_fields_after_branch(db):
    """add_message with an explicit parent records the parent lineage."""
    sess = db.create_session("explicit parent")
    root = db.add_message(sess["id"], "user", "root")
    with db._get_conn() as conn:
        root_id = int(conn.execute("SELECT MAX(id) AS i FROM messages").fetchone()["i"])
    db.add_message(sess["id"], "user", "child", parent_id=root_id)
    with db._get_conn() as conn:
        child = conn.execute(
            "SELECT id, parent_message_id, branch_root_id FROM messages "
            "WHERE id=(SELECT MAX(id) FROM messages)",
        ).fetchone()
    assert child["parent_message_id"] == root_id
    assert child["branch_root_id"] is None


def test_migrate_v3_to_v4_preserves_linear_mainline(db):
    """A v3-era linear log reads as mainline with no branch pointer rows."""
    sess = db.create_session("linear legacy")
    _linear(db, sess["id"], 3)
    # No branch/revert ever happened: the leaf falls back to newest message.
    assert db.current_session_leaf(sess["id"]) == max(
        m["id"] for m in db.get_session_messages(sess["id"])
    )
    tree = db.session_tree(sess["id"])
    assert tree["current_leaf"] == max(m["id"] for m in tree["nodes"].values())
    assert len(tree["branches"]) == 0


# ── steer / followUp queues (react) ────────────────────────────────────────


def _tool_registry():
    from modus.tools.base import Tool, ToolResult, object_schema
    from modus.tools.registry import ToolRegistry

    async def echo(payload, _ctx):
        return ToolResult(str(payload.get("text") or ""))

    reg = ToolRegistry()
    reg.register(Tool(
        name="echo", description="echo",
        parameters=object_schema({"text": {"type": "string"}}, ["text"]),
        handler=echo,
    ))
    return reg


class _SteerableClient:
    """Fake LLM: tool-call on turn 1, then records the messages it saw.

    Lets a test assert that a steer turn was present BEFORE the second LLM
    call (i.e. after the first tool result was replayed).
    """

    model_name = "fake"
    provider_name = "test"
    max_context_window = 128_000

    def __init__(self) -> None:
        self.calls = 0
        self.seen_steer_turn2 = False
        self.seen_followup = False

    async def chat(self, messages, tools, *, system_prompt):
        self.calls += 1
        has_tool_result = any(m.role == "tool" for m in messages)
        contents = [
            str(m.content) for m in messages if isinstance(m.content, str)
        ]
        if not has_tool_result:
            yield {
                "type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": "e1",
                    "function": {"name": "echo", "arguments": '{"text":"x"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        # Second LLM call: the steer message (if queued) must already be in
        # the context, injected after the tool result was replayed.
        if any("STEER-MID" in c for c in contents):
            self.seen_steer_turn2 = True
        if any("FOLLOWUP" in c for c in contents):
            self.seen_followup = True
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


@pytest.mark.asyncio
async def test_steer_injected_before_next_llm():
    """A steer turn lands after the tool result and before the next LLM call."""
    from modus.agent.strategies.react import (
        ReActReasoner, enqueue_steer,
    )

    client = _SteerableClient()
    reasoner = ReActReasoner(
        llm_client=client, tool_registry=_tool_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        session_id="steer-sess",
    )
    enqueue_steer("steer-sess", Message(role="user", content="STEER-MID: pivot"))
    events = [
        event async for event in reasoner.run(
            [Message(role="user", content="build it")],
        )
    ]
    done = events[-1]
    assert done["type"] == "done"
    assert client.seen_steer_turn2, (
        "the steer message must be in the context of the second LLM call"
    )
    # The steer message is consumed (not re-injected across turns).
    assert reasoner._steer_consumed == 1


@pytest.mark.asyncio
async def test_steer_is_not_reinjected_every_turn():
    """A consumed steer turn never re-appears in later turns."""
    from modus.agent.strategies.react import (
        ReActReasoner, enqueue_steer,
    )

    class ThreeTurnClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        def __init__(self) -> None:
            self.calls = 0
            self.steer_counts = []

        async def chat(self, messages, tools, *, system_prompt):
            self.calls += 1
            has_tool_result = any(m.role == "tool" for m in messages)
            contents = [
                str(m.content) for m in messages if isinstance(m.content, str)
            ]
            self.steer_counts.append(sum("STEER-MID" in c for c in contents))
            if self.calls < 3:
                yield {
                    "type": "tool_call_delta", "tool_call": {
                        "index": 0, "id": f"e{self.calls}",
                        "function": {"name": "echo", "arguments": '{"text":"x"}'},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    client = ThreeTurnClient()
    reasoner = ReActReasoner(
        llm_client=client, tool_registry=_tool_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        session_id="steer-once",
    )
    enqueue_steer("steer-once", Message(role="user", content="STEER-MID: once"))
    events = [
        event async for event in reasoner.run(
            [Message(role="user", content="go")],
        )
    ]
    assert events[-1]["type"] == "done"
    # Steer is injected once (from the first loop iteration) and never
    # re-injected: every LLM call sees exactly one copy, and the queue stays
    # empty after the first drain.
    assert client.steer_counts == [1, 1, 1]
    assert reasoner._steer_consumed == 1
    from modus.agent.strategies.react import steer_queue_for

    assert len(steer_queue_for("steer-once")) == 0


@pytest.mark.asyncio
async def test_followup_queued_after_run():
    """A follow-up turn is consumed only after the run is done."""
    from modus.agent.strategies.react import (
        ReActReasoner, enqueue_followup, followup_queue_for,
    )

    client = _SteerableClient()
    reasoner = ReActReasoner(
        llm_client=client, tool_registry=_tool_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        session_id="followup-sess",
    )
    enqueue_followup(
        "followup-sess", Message(role="user", content="FOLLOWUP: after"),
    )
    events = [
        event async for event in reasoner.run(
            [Message(role="user", content="go")],
        )
    ]
    done = events[-1]
    assert done["type"] == "done"
    # The follow-up was NOT injected mid-run (the reasoner never consumes it).
    assert not client.seen_followup
    assert len(followup_queue_for("followup-sess")) == 1

    # Draining after the run consumes it.
    messages = list(done["messages"])
    consumed = reasoner.drain_followup(messages)
    assert [str(m.content) for m in consumed] == ["FOLLOWUP: after"]
    assert len(followup_queue_for("followup-sess")) == 0


def test_queues_are_session_scoped():
    """Steer/followUp queues never leak across sessions."""
    from modus.agent.strategies.react import (
        enqueue_followup, enqueue_steer, followup_queue_for, steer_queue_for,
    )

    enqueue_steer("q-a", Message(role="user", content="steer a"))
    enqueue_followup("q-a", Message(role="user", content="follow a"))
    enqueue_steer("q-b", Message(role="user", content="steer b"))

    assert [str(m.content) for m in steer_queue_for("q-a")] == ["steer a"]
    assert [str(m.content) for m in steer_queue_for("q-b")] == ["steer b"]
    assert [str(m.content) for m in followup_queue_for("q-a")] == ["follow a"]
    assert steer_queue_for("q-b") is not steer_queue_for("q-a")


# ── branch-aware compaction ────────────────────────────────────────────────


def test_branch_compaction_aligned(db):
    """Compaction after a branch rebuilds the branch context with the tail rule."""
    from modus.agent.compressor import SUMMARY_PREFIX, compress_messages

    sess = db.create_session("branch compaction")
    ids = _linear(db, sess["id"], 6)
    db.session_branch(sess["id"], ids[3])
    db.add_message(sess["id"], "user", "branch-extra")

    # Rebuild branch rows as model context via the compressor path.
    rows = db.get_session_messages(sess["id"])
    assert [r["content"] for r in rows] == [
        "msg-0", "msg-1", "msg-2", "msg-3", "branch-extra",
    ]
    rebuilt = [
        Message(role=r["role"], content=r["content"], tool_calls=r["tool_calls"])
        for r in rows
    ]
    compacted = compress_messages(rebuilt, summary="branch compacted", tail_count=2)
    # The summary is reference-only and the tail keeps the branch's final turn.
    assert any(
        m.role == "system" and str(m.content).startswith(SUMMARY_PREFIX)
        for m in compacted
    )
    assert compacted[-1].content == "branch-extra"
    # The downstream mainline messages (msg-4/msg-5) never appear in the
    # branch-compacted context.
    assert all(
        str(m.content) not in {"msg-4", "msg-5"} for m in compacted
    )


def test_active_branch_rows_excludes_siblings(db):
    """active_branch_rows walks only the current branch's parent chain."""
    from modus.agent.compressor import active_branch_rows

    sess = db.create_session("branch rows")
    ids = _linear(db, sess["id"], 4)
    db.session_branch(sess["id"], ids[1])
    db.add_message(sess["id"], "user", "branch-one")
    db.session_revert(sess["id"], ids[1])
    db.add_message(sess["id"], "user", "branch-two")

    tree = db.session_tree(sess["id"])
    rows = active_branch_rows(tree)
    contents = [str(r["content"]) for r in rows]
    assert contents == ["msg-0", "msg-1", "branch-two"]
    # Sibling branch content and downstream mainline are excluded.
    assert "branch-one" not in contents
    assert "msg-2" not in contents
    assert "msg-3" not in contents


# ── server WS wiring ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_server_branch_revert_tree_commands(db):
    from modus.desktop import server

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    socket = Socket()
    session = server.DaoSession(id="runtime", db_id=db.create_session("ws")["id"])
    ids = _linear(db, session.db_id, 3)

    # session_branch command.
    assert await server.command_router.dispatch(
        socket, session,
        {"type": "session_branch", "message_id": ids[1], "request_id": "b1"},
    ) is True
    branched = socket.sent[-1]
    assert branched["type"] == "session_branched"
    assert branched["branch"]["message_id"] == ids[1]

    # session_tree command reflects the fork.
    assert await server.command_router.dispatch(
        socket, session, {"type": "session_tree", "request_id": "t1"},
    ) is True
    tree = socket.sent[-1]
    assert tree["type"] == "session_tree"
    assert tree["tree"]["current_leaf"] == ids[1]
    assert tree["tree"]["message_count"] == 3

    # session_revert command.
    assert await server.command_router.dispatch(
        socket, session,
        {"type": "session_revert", "message_id": ids[0], "request_id": "r1"},
    ) is True
    reverted = socket.sent[-1]
    assert reverted["type"] == "session_reverted"
    assert reverted["branch"]["message_id"] == ids[0]


@pytest.mark.asyncio
async def test_server_branch_revert_refused_while_running(db):
    from modus.desktop import server

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    socket = Socket()
    session = server.DaoSession(id="runtime", db_id=db.create_session("busy")["id"])
    ids = _linear(db, session.db_id, 2)
    release = asyncio.Event()

    from modus.desktop.server import start_session_run

    assert start_session_run(session, release.wait()) is True
    try:
        assert await server.command_router.dispatch(
            socket, session,
            {"type": "session_branch", "message_id": ids[0], "request_id": "b1"},
        ) is True
        assert socket.sent[-1]["code"] == "session_busy"
    finally:
        release.set()
        await session.active_run_task
        from modus.desktop import session_state

        session_state._active_persisted_runs.pop(session.db_id, None)
        session.active_run_task = None
        session.active_run_session_id = None
        session.active_run_id = None
        session.active_controller = None


@pytest.mark.asyncio
async def test_server_steer_queued_during_live_run(db, monkeypatch):
    from modus.desktop import server
    from modus.agent.strategies.react import steer_queue_for

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    session = server.DaoSession(
        id="runtime", db_id=db.create_session("steer live")["id"],
    )
    release = asyncio.Event()

    async def fake_submission(ws, runner_session, content, skill_id, *, mode, emitter, controller):
        await release.wait()
        return emitter

    monkeypatch.setattr(server, "_run_preallocated_submission", fake_submission)
    socket = Socket()

    # Start a run.
    msg = {
        "type": "run_message", "content": "initial",
        "request_id": "run-1", "db_id": session.db_id,
        "session_id": session.db_id, "runtime_session_id": session.id,
    }
    assert await server._handle_explicit_run_message(socket, session, msg) is True
    run_task = session.active_run_task
    assert run_task is not None

    # A steer:true turn while the run is live is queued, not rejected.
    steer_msg = {
        "type": "run_message", "content": "STEER: pivot now",
        "request_id": "run-2", "db_id": session.db_id,
        "session_id": session.db_id, "runtime_session_id": session.id,
        "steer": True,
    }
    assert await server._handle_explicit_run_message(socket, session, steer_msg) is True
    queued = socket.sent[-1]
    assert queued["type"] == "run_queued"
    assert queued["mode"] == "steer"
    assert [str(m.content) for m in steer_queue_for(session.db_id)] == [
        "STEER: pivot now",
    ]

    # A non-steer turn while the run is live goes to the follow-up queue.
    follow_msg = {
        "type": "run_message", "content": "FOLLOW: and then",
        "request_id": "run-3", "db_id": session.db_id,
        "session_id": session.db_id, "runtime_session_id": session.id,
    }
    assert await server._handle_explicit_run_message(socket, session, follow_msg) is True
    assert socket.sent[-1]["mode"] == "followup"
    from modus.agent.strategies.react import followup_queue_for

    assert [str(m.content) for m in followup_queue_for(session.db_id)] == [
        "FOLLOW: and then",
    ]

    try:
        release.set()
        await run_task
    finally:
        from modus.desktop import session_state

        session_state._active_persisted_runs.pop(session.db_id, None)
        session.active_run_task = None
        session.active_run_session_id = None
        session.active_run_id = None
        session.active_controller = None


@pytest.mark.asyncio
async def test_server_followup_drained_after_settlement(db, monkeypatch):
    """Queued follow-up turns become fresh runs after the run settles."""
    from modus.desktop import server
    from modus.agent.strategies.react import (
        enqueue_followup, followup_queue_for,
    )

    started: list[str] = []

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    session = server.DaoSession(
        id="runtime", db_id=db.create_session("followup drain")["id"],
    )
    release = asyncio.Event()

    async def fake_submission(ws, runner_session, content, skill_id, *, mode, emitter, controller):
        started.append(content)
        return emitter

    monkeypatch.setattr(server, "_run_preallocated_submission", fake_submission)
    socket = Socket()

    # Queue a follow-up turn.
    enqueue_followup(
        session.db_id, Message(role="user", content="after you finish"),
    )

    # Drain after settlement: the queued turn starts a fresh run.
    await server._drain_followups(socket, session)
    assert any(packet.get("type") == "run_accepted" for packet in socket.sent)
    if session.active_run_task is not None:
        # Let the admitted run reach the runner so the fake records content.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        session.active_run_task.cancel()
        try:
            await session.active_run_task
        except (asyncio.CancelledError, Exception):
            pass
        from modus.desktop import session_state

        session_state._active_persisted_runs.pop(session.db_id, None)
        session.active_run_task = None
        session.active_run_session_id = None
        session.active_run_id = None
        session.active_controller = None
    assert "after you finish" in started
    assert len(followup_queue_for(session.db_id)) == 0


@pytest.mark.asyncio
async def test_run_queued_preserves_request_id(db, monkeypatch):
    """The run_queued ack echoes the submission identity for retry."""
    from modus.desktop import server

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    session = server.DaoSession(
        id="runtime", db_id=db.create_session("queued ack")["id"],
    )
    release = asyncio.Event()

    async def fake_submission(ws, runner_session, content, skill_id, *, mode, emitter, controller):
        await release.wait()
        return emitter

    monkeypatch.setattr(server, "_run_preallocated_submission", fake_submission)
    socket = Socket()

    msg = {
        "type": "run_message", "content": "initial",
        "request_id": "run-1", "db_id": session.db_id,
        "session_id": session.db_id, "runtime_session_id": session.id,
    }
    assert await server._handle_explicit_run_message(socket, session, msg) is True
    try:
        steer_msg = {
            "type": "run_message", "content": "steer",
            "request_id": "run-2", "db_id": session.db_id,
            "session_id": session.db_id, "runtime_session_id": session.id,
            "steer": True,
        }
        assert await server._handle_explicit_run_message(socket, session, steer_msg) is True
        queued = socket.sent[-1]
        assert queued["type"] == "run_queued"
        assert queued["request_id"] == "run-2"
        assert queued["requested_db_id"] == session.db_id
        assert queued["runtime_session_id"] == session.id
    finally:
        release.set()
        await session.active_run_task
        from modus.desktop import session_state

        session_state._active_persisted_runs.pop(session.db_id, None)
        session.active_run_task = None
        session.active_run_session_id = None
        session.active_run_id = None
        session.active_controller = None
