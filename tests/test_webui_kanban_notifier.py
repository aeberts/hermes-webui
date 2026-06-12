"""Unit tests for api/kanban_notifier.py.

CI does not install hermes-agent, so these tests inject a fake
hermes_cli.kanban_db module and drive _poll_once() directly, bypassing
the daemon thread and the live api.config globals.
"""
from __future__ import annotations

import queue
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


# ── Fake StreamChannel ────────────────────────────────────────────────────────

class FakeChannel:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()

    def put_nowait(self, item):
        self._q.put_nowait(item)

    def received(self):
        items = []
        while not self._q.empty():
            items.append(self._q.get_nowait())
        return items


# ── Fake kanban_db builder ────────────────────────────────────────────────────

def _make_fake_kb(
    boards=None,
    subs=None,
    events_by_task=None,
    task_by_id=None,
    db_path_raises=False,
    connect_raises=False,
):
    """Build a minimal fake kanban_db namespace."""
    boards = boards if boards is not None else [{"slug": "default"}]
    subs = subs if subs is not None else []
    events_by_task = events_by_task or {}
    task_by_id = task_by_id or {}

    removed_subs: list[dict] = []

    class FakeConn:
        def close(self):
            pass

    conn = FakeConn()

    kb = types.SimpleNamespace()
    kb.DEFAULT_BOARD = "default"

    def list_boards(include_archived=True):
        return boards

    def kanban_db_path(slug):
        if db_path_raises:
            raise RuntimeError("db path error")
        return f"/fake/{slug}.db"

    def connect(board=None):
        if connect_raises:
            raise RuntimeError("cannot open")
        return conn

    def list_notify_subs(c):
        return list(subs)

    def claim_unseen_events_for_sub(c, task_id, platform, chat_id, thread_id, kinds):
        evts = events_by_task.get(task_id, [])
        return (None, None, evts)

    def get_task(c, task_id):
        return task_by_id.get(task_id)

    def remove_notify_sub(c, task_id, platform, chat_id, thread_id):
        removed_subs.append({"task_id": task_id, "platform": platform, "chat_id": chat_id})

    kb.list_boards = list_boards
    kb.kanban_db_path = kanban_db_path
    kb.connect = connect
    kb.list_notify_subs = list_notify_subs
    kb.claim_unseen_events_for_sub = claim_unseen_events_for_sub
    kb.get_task = get_task
    kb.remove_notify_sub = remove_notify_sub
    kb._removed_subs = removed_subs

    return kb


def _fake_event(kind="completed"):
    return SimpleNamespace(kind=kind)


def _fake_task(title="My Task", status="done", result="ok"):
    return SimpleNamespace(title=title, status=status, result=result)


# ── Import helper ─────────────────────────────────────────────────────────────

def _import_notifier():
    """Import (or reload) the notifier module with no hermes_cli in sys.modules."""
    for key in list(sys.modules):
        if key == "api.kanban_notifier" or key.startswith("hermes_cli"):
            del sys.modules[key]
    import api.kanban_notifier as mod
    return mod


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_module_imports_cleanly_with_no_side_effects():
    mod = _import_notifier()
    assert mod.PLATFORM == "webui"
    assert mod.POLL_INTERVAL_SECONDS == 5.0
    # Starting the thread requires hermes_cli; just verify the functions exist
    assert callable(mod.start_kanban_notifier)
    assert callable(mod.stop_kanban_notifier)


def test_poll_once_skips_when_no_active_streams():
    mod = _import_notifier()
    kb = _make_fake_kb(subs=[
        {"platform": "webui", "chat_id": "s1", "task_id": "t1", "thread_id": ""},
    ])
    # No active streams → _poll_once should return without touching kb at all
    kb.list_boards = MagicMock(side_effect=AssertionError("should not be called"))
    mod._poll_once(kb, {})  # must not raise


def test_poll_once_delivers_to_matching_active_session():
    mod = _import_notifier()
    ch = FakeChannel()
    task = _fake_task(title="Deploy", status="done", result="shipped")
    kb = _make_fake_kb(
        subs=[{"platform": "webui", "chat_id": "sess-abc", "task_id": "t42", "thread_id": ""}],
        events_by_task={"t42": [_fake_event("completed")]},
        task_by_id={"t42": task},
    )

    mod._poll_once(kb, {"sess-abc": ch})

    delivered = ch.received()
    assert len(delivered) == 1
    evt_type, payload = delivered[0]
    assert evt_type == "kanban_done"
    assert payload["task_id"] == "t42"
    assert payload["title"] == "Deploy"
    assert payload["status"] == "completed"
    assert payload["result"] == "shipped"
    assert payload["board"] == "default"


def test_poll_once_skips_tui_platform_subscriptions():
    mod = _import_notifier()
    ch = FakeChannel()
    kb = _make_fake_kb(
        subs=[{"platform": "tui", "chat_id": "sess-abc", "task_id": "t1", "thread_id": ""}],
        events_by_task={"t1": [_fake_event("completed")]},
        task_by_id={"t1": _fake_task()},
    )

    mod._poll_once(kb, {"sess-abc": ch})

    assert ch.received() == []


def test_poll_once_skips_session_not_in_active_streams():
    mod = _import_notifier()
    ch = FakeChannel()
    kb = _make_fake_kb(
        subs=[{"platform": "webui", "chat_id": "other-session", "task_id": "t1", "thread_id": ""}],
        events_by_task={"t1": [_fake_event("completed")]},
        task_by_id={"t1": _fake_task()},
    )

    mod._poll_once(kb, {"sess-abc": ch})  # sess-abc ≠ other-session

    assert ch.received() == []


def test_poll_once_skips_when_no_events_claimed():
    mod = _import_notifier()
    ch = FakeChannel()
    kb = _make_fake_kb(
        subs=[{"platform": "webui", "chat_id": "sess-abc", "task_id": "t1", "thread_id": ""}],
        events_by_task={},  # no events for t1
        task_by_id={"t1": _fake_task()},
    )

    mod._poll_once(kb, {"sess-abc": ch})

    assert ch.received() == []


def test_poll_once_removes_sub_when_task_in_final_status():
    mod = _import_notifier()
    ch = FakeChannel()
    task = _fake_task(status="done")
    kb = _make_fake_kb(
        subs=[{"platform": "webui", "chat_id": "sess-abc", "task_id": "t1", "thread_id": ""}],
        events_by_task={"t1": [_fake_event("completed")]},
        task_by_id={"t1": task},
    )

    mod._poll_once(kb, {"sess-abc": ch})

    assert len(kb._removed_subs) == 1
    assert kb._removed_subs[0]["task_id"] == "t1"
    assert kb._removed_subs[0]["platform"] == "webui"
    assert kb._removed_subs[0]["chat_id"] == "sess-abc"


def test_poll_once_does_not_remove_sub_when_task_not_final():
    mod = _import_notifier()
    ch = FakeChannel()
    task = _fake_task(status="in_progress")
    kb = _make_fake_kb(
        subs=[{"platform": "webui", "chat_id": "sess-abc", "task_id": "t1", "thread_id": ""}],
        events_by_task={"t1": [_fake_event("completed")]},
        task_by_id={"t1": task},
    )

    mod._poll_once(kb, {"sess-abc": ch})

    assert kb._removed_subs == []
    assert len(ch.received()) == 1  # still delivered


def test_poll_once_deduplicates_boards_by_db_path():
    mod = _import_notifier()
    ch = FakeChannel()

    # Two boards that share the same DB path → only one should be processed
    boards = [{"slug": "alpha"}, {"slug": "beta"}]
    call_count = {"n": 0}

    def same_db_path(slug):
        return "/shared/kanban.db"

    subs = [{"platform": "webui", "chat_id": "sess-abc", "task_id": "t1", "thread_id": ""}]
    kb = _make_fake_kb(
        boards=boards,
        subs=subs,
        events_by_task={"t1": [_fake_event("completed")]},
        task_by_id={"t1": _fake_task(status="done")},
    )
    kb.kanban_db_path = same_db_path

    original_connect = kb.connect
    def counting_connect(board=None):
        call_count["n"] += 1
        return original_connect(board=board)
    kb.connect = counting_connect

    mod._poll_once(kb, {"sess-abc": ch})

    # Only one connect call despite two boards with the same path
    assert call_count["n"] == 1
    assert len(ch.received()) == 1


def test_poll_once_skips_board_when_connect_raises():
    mod = _import_notifier()
    ch = FakeChannel()
    kb = _make_fake_kb(
        subs=[{"platform": "webui", "chat_id": "sess-abc", "task_id": "t1", "thread_id": ""}],
        events_by_task={"t1": [_fake_event("completed")]},
        task_by_id={"t1": _fake_task()},
        connect_raises=True,
    )

    # Must not raise; board open failure is non-fatal
    mod._poll_once(kb, {"sess-abc": ch})
    assert ch.received() == []


def test_poll_once_uses_task_id_as_title_when_task_missing():
    mod = _import_notifier()
    ch = FakeChannel()
    kb = _make_fake_kb(
        subs=[{"platform": "webui", "chat_id": "sess-abc", "task_id": "t-missing", "thread_id": ""}],
        events_by_task={"t-missing": [_fake_event("crashed")]},
        task_by_id={},  # no task object
    )

    mod._poll_once(kb, {"sess-abc": ch})

    delivered = ch.received()
    assert len(delivered) == 1
    _, payload = delivered[0]
    assert payload["title"] == ""
    assert "t-missing" in payload["text"]


def test_poll_once_delivers_to_multiple_independent_sessions():
    mod = _import_notifier()
    ch1, ch2 = FakeChannel(), FakeChannel()
    kb = _make_fake_kb(
        subs=[
            {"platform": "webui", "chat_id": "sess-1", "task_id": "t1", "thread_id": ""},
            {"platform": "webui", "chat_id": "sess-2", "task_id": "t2", "thread_id": ""},
        ],
        events_by_task={
            "t1": [_fake_event("completed")],
            "t2": [_fake_event("blocked")],
        },
        task_by_id={
            "t1": _fake_task(title="Task One", status="done"),
            "t2": _fake_task(title="Task Two", status="in_progress"),
        },
    )

    mod._poll_once(kb, {"sess-1": ch1, "sess-2": ch2})

    r1 = ch1.received()
    r2 = ch2.received()
    assert len(r1) == 1
    assert r1[0][1]["title"] == "Task One"
    assert len(r2) == 1
    assert r2[0][1]["title"] == "Task Two"


def test_start_kanban_notifier_is_idempotent():
    mod = _import_notifier()

    # Inject a fake hermes_cli.kanban_db so the thread doesn't immediately exit
    fake_kb_mod = types.ModuleType("hermes_cli.kanban_db")
    fake_hermes = types.ModuleType("hermes_cli")
    fake_hermes.kanban_db = fake_kb_mod
    sys.modules["hermes_cli"] = fake_hermes
    sys.modules["hermes_cli.kanban_db"] = fake_kb_mod

    try:
        mod.start_kanban_notifier()
        t1 = mod._thread
        mod.start_kanban_notifier()  # second call must not spawn a new thread
        t2 = mod._thread
        assert t1 is t2
    finally:
        mod.stop_kanban_notifier()
        sys.modules.pop("hermes_cli", None)
        sys.modules.pop("hermes_cli.kanban_db", None)


def test_poll_loop_exits_cleanly_when_kanban_db_not_importable():
    """_poll_loop must exit without error when hermes_cli is absent.

    sys.modules[name] = None is the Python 3 mechanism to actively block an
    import regardless of what is on sys.path (the conftest adds hermes-agent
    to sys.path, making hermes_cli importable in normal test runs).
    """
    mod = _import_notifier()
    # Actively block the import (None entry causes ImportError in Python 3)
    saved = {k: sys.modules.get(k) for k in ("hermes_cli", "hermes_cli.kanban_db")}
    sys.modules["hermes_cli"] = None  # type: ignore[assignment]
    sys.modules["hermes_cli.kanban_db"] = None  # type: ignore[assignment]

    try:
        done = threading.Event()

        def run():
            mod._poll_loop()
            done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert done.wait(timeout=2.0), "_poll_loop did not exit within 2s on blocked import"
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# ── M1: server.py startup wiring (TC-15) ─────────────────────────────────────

def test_server_wires_start_kanban_notifier():
    """server.py must import and call start_kanban_notifier() at startup (static source check).

    Oracle direction: removing the startup block from server.py causes both
    assertions to fail.  Pattern mirrors B02's grep-assert for entry.py wiring.
    """
    server_path = Path(__file__).parent.parent / "server.py"
    assert server_path.exists(), f"server.py not found at {server_path}"
    content = server_path.read_text()
    assert "from api.kanban_notifier import start_kanban_notifier" in content, (
        "start_kanban_notifier not imported in server.py — startup wiring is missing"
    )
    assert "start_kanban_notifier()" in content, (
        "start_kanban_notifier() call not found in server.py — startup wiring is missing"
    )


# ── M2: _active_session_streams() live path (TC-16) ──────────────────────────

def _patch_api_config(fake_config):
    """Context manager: temporarily replace sys.modules['api.config']."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        saved = sys.modules.get("api.config")
        sys.modules["api.config"] = fake_config  # type: ignore[assignment]
        try:
            yield
        finally:
            if saved is None:
                sys.modules.pop("api.config", None)
            else:
                sys.modules["api.config"] = saved

    return _ctx()


def test_active_session_streams_returns_channel_for_active_session():
    """_active_session_streams() maps session_id → channel when active_stream_id is set."""
    mod = _import_notifier()
    ch = FakeChannel()

    fake_config = types.SimpleNamespace(
        LOCK=threading.Lock(),
        SESSIONS={"sess-1": SimpleNamespace(active_stream_id="stream-abc")},
        STREAMS={"stream-abc": ch},
    )

    with _patch_api_config(fake_config):
        result = mod._active_session_streams()

    assert result == {"sess-1": ch}


def test_active_session_streams_excludes_session_with_no_active_stream():
    """_active_session_streams() omits sessions whose active_stream_id is None or absent."""
    mod = _import_notifier()

    fake_config = types.SimpleNamespace(
        LOCK=threading.Lock(),
        SESSIONS={"sess-1": SimpleNamespace(active_stream_id=None)},
        STREAMS={},
    )

    with _patch_api_config(fake_config):
        result = mod._active_session_streams()

    assert result == {}
