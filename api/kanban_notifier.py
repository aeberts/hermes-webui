"""Background kanban notification poller for webui sessions.

Polls kanban_notify_subs for platform='webui' entries matching active
webui sessions and delivers terminal-state events via the existing SSE
StreamChannel broadcaster (api.config.STREAMS).

Runs as a daemon thread started from server.py.
Does not modify the gateway notifier in gateway/run.py — that loop
handles registered adapter platforms (discord, telegram, etc.) only.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

PLATFORM = "webui"
TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out")
_FINAL_TASK_STATUSES = frozenset({"done", "archived"})
POLL_INTERVAL_SECONDS = 5.0

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def _active_session_streams() -> dict[str, object]:
    """Return {session_id: StreamChannel} for sessions with an active SSE connection."""
    try:
        from api.config import LOCK, SESSIONS, STREAMS
        with LOCK:
            snapshot = {
                sid: getattr(s, "active_stream_id", None)
                for sid, s in SESSIONS.items()
            }
        result = {}
        for sid, stream_id in snapshot.items():
            if not stream_id:
                continue
            ch = STREAMS.get(stream_id)
            if ch is not None:
                result[sid] = ch
        return result
    except Exception:
        return {}


def _known_session_ids() -> set[str]:
    """Return all live webui session ids, with or without an open stream.

    Delivery must not require an open in-turn stream (B05): kanban graphs
    finish minutes after the originating turn ended, when no stream exists.
    """
    try:
        from api.config import LOCK, SESSIONS
        with LOCK:
            return set(SESSIONS.keys())
    except Exception:
        return set()


def _build_wakeup_prompt(task_id: str, title: str, board: str, kind: str, result: str) -> str:
    """Kanban flavour of background_process.format_wakeup_prompt (B05).

    The prompt becomes the user message of a server-side wakeup turn, so it
    must instruct the agent to close the loop in conversation.
    """
    excerpt = (result or "").strip()
    if len(excerpt) > 400:
        excerpt = excerpt[:400] + " …[truncated]"
    lines = [
        f"[kanban] Task {task_id} ('{title or task_id}') on board '{board}' reached terminal state '{kind}'.",
    ]
    if excerpt:
        lines.append(f"Result excerpt: {excerpt}")
    lines.append(
        f"Read the card with kanban_show('{task_id}') (plus any parent/child cards you need), "
        "then report the outcome back to the user in this conversation. If the state is not "
        "'completed', explain what blocked it and propose the next step."
    )
    return "\n".join(lines)


def _dispatch_agent_wakeup(session_id: str, dedupe_id: str, prompt: str) -> None:
    """Server-side agent wakeup, mirroring the bg_task_complete drain (Option Z).

    Active turn → defer (delivered by the turn-teardown idle hook); idle →
    start a server-side turn directly. Works with no browser tab open.
    """
    try:
        from api import background_process as bp
        if bp._session_has_active_turn(session_id):
            bp.record_deferred_wakeup(session_id, dedupe_id, prompt)
            logger.debug(
                "kanban_notifier: wakeup deferred (turn active) for session %s", session_id
            )
        else:
            bp._start_server_side_wakeup_turn(session_id, prompt, process_id=dedupe_id)
            logger.debug(
                "kanban_notifier: server-side wakeup turn started for session %s", session_id
            )
    except Exception:
        logger.warning(
            "kanban_notifier: wakeup dispatch failed for session %s", session_id, exc_info=True
        )


def _emit_live_view(session_id: str, payload: dict) -> None:
    """Emit kanban_done to the in-turn stream AND the persistent session channel."""
    try:
        from api import background_process as bp
        bp._emit_to_session_streams(session_id, "kanban_done", dict(payload))
    except Exception:
        logger.debug(
            "kanban_notifier: live-view emit failed for session %s", session_id, exc_info=True
        )


def _poll_once(kb, active_streams: dict[str, object], known_sessions: set[str] | None = None) -> None:
    if known_sessions is None:
        known_sessions = _known_session_ids()
    if not active_streams and not known_sessions:
        return

    try:
        boards = kb.list_boards(include_archived=False)
    except Exception:
        boards = [{"slug": kb.DEFAULT_BOARD}]

    seen_db_paths: set[str] = set()
    for board_meta in boards:
        slug = board_meta.get("slug") or kb.DEFAULT_BOARD
        try:
            db_key = str(kb.kanban_db_path(slug))
        except Exception:
            db_key = slug
        if db_key in seen_db_paths:
            continue
        seen_db_paths.add(db_key)

        try:
            conn = kb.connect(board=slug)
        except Exception as exc:
            logger.debug("kanban_notifier: cannot open board %s: %s", slug, exc)
            continue

        try:
            for sub in kb.list_notify_subs(conn):
                if sub.get("platform") != PLATFORM:
                    continue
                chat_id = sub.get("chat_id", "")
                channel = active_streams.get(chat_id)
                is_known = chat_id in known_sessions
                if channel is None and not is_known:
                    continue
                _, _, events = kb.claim_unseen_events_for_sub(
                    conn,
                    task_id=sub["task_id"],
                    platform=PLATFORM,
                    chat_id=chat_id,
                    thread_id=sub.get("thread_id") or "",
                    kinds=TERMINAL_KINDS,
                )
                if not events:
                    continue
                task = kb.get_task(conn, sub["task_id"])
                task_title = (task.title or sub["task_id"]) if task else sub["task_id"]
                last = events[-1]
                event_id = f"kanban:{sub['task_id']}:{getattr(last, 'id', '') or last.kind}"
                payload = {
                    "kind": "kanban_done",
                    "text": f"[kanban] {task_title}: {last.kind}",
                    "task_id": sub["task_id"],
                    "title": task.title if task else "",
                    "status": last.kind,
                    "result": (task.result if task else "") or "",
                    "board": slug,
                    "session_id": chat_id,
                    "event_id": event_id,
                }
                if is_known:
                    # B05 primary path: live-view emit to in-turn stream +
                    # persistent session channel, then server-side agent
                    # wakeup so the session closes the loop in conversation.
                    _emit_live_view(chat_id, payload)
                    _dispatch_agent_wakeup(
                        chat_id,
                        event_id,
                        _build_wakeup_prompt(
                            sub["task_id"], payload["title"], slug, last.kind, payload["result"]
                        ),
                    )
                elif channel is not None:
                    channel.put_nowait(("kanban_done", payload))
                logger.debug(
                    "kanban_notifier: emitted task.done for %s → session %s",
                    sub["task_id"], chat_id,
                )
                task_status = task.status if task else ""
                if task_status in _FINAL_TASK_STATUSES:
                    kb.remove_notify_sub(
                        conn,
                        task_id=sub["task_id"],
                        platform=PLATFORM,
                        chat_id=chat_id,
                        thread_id=sub.get("thread_id") or "",
                    )
        finally:
            conn.close()


def _poll_loop() -> None:
    try:
        from hermes_cli import kanban_db as kb
    except ImportError:
        logger.warning("kanban_notifier: kanban_db not importable; disabled")
        return

    while not _stop_event.is_set():
        _stop_event.wait(POLL_INTERVAL_SECONDS)
        if _stop_event.is_set():
            break
        try:
            _poll_once(kb, _active_session_streams())
        except Exception as exc:
            logger.debug("kanban_notifier: tick error: %s", exc)


def start_kanban_notifier() -> None:
    """Start the global kanban notifier daemon thread (idempotent)."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_poll_loop, daemon=True, name="kanban-notifier")
        _thread.start()


def stop_kanban_notifier() -> None:
    """Stop the kanban notifier thread."""
    global _thread
    with _thread_lock:
        _stop_event.set()
        t = _thread
        _thread = None
    if t is not None:
        t.join(timeout=3)
