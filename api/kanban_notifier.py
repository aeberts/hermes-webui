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


def _poll_once(kb, active_streams: dict[str, object]) -> None:
    if not active_streams:
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
                if channel is None:
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
                channel.put_nowait(("kanban_done", {
                    "kind": "kanban_done",
                    "text": f"[kanban] {task_title}: {events[-1].kind}",
                    "task_id": sub["task_id"],
                    "title": task.title if task else "",
                    "status": events[-1].kind,
                    "result": (task.result if task else "") or "",
                    "board": slug,
                }))
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
