"""Regression for #4159 (salvage of PR #4224): clicking a session-search result
that matched on message CONTENT must jump to that message in the transcript and
flash it.

Two halves:

1. Backend — the content scan in ``_handle_sessions_search`` already locates the
   exact message that contains the query (it iterates ``sess.messages`` and
   ``break``s on the first hit). Until now the loop index was discarded; these
   tests pin the new ``match_message_idx`` field on content-typed results,
   indexed against the same raw ``sess.messages`` array the renderer stamps onto
   each row as ``msg-user-<rawIdx>`` / ``data-msg-idx``. Title matches must NOT
   carry ``match_message_idx`` (there's no message-level hit to jump to).

2. Frontend scope wiring — the jump helper ``_jumpToMessage`` is defined INSIDE
   ``static/outline.js``'s IIFE (the file ends ``})();``), so a bare
   ``_jumpToMessage(...)`` call from ``static/sessions.js`` is a different
   ``<script>`` scope and resolves to nothing — a silent no-op (the bug that
   sank the original #4224). These structural tests pin the fix: outline.js must
   expose the helper on ``window`` and the sessions.js search-click path must
   reach it via ``window._jumpToMessage`` (the cross-<script> handle), exactly
   like the sibling ``window._outlineJump`` export. They fail on master, where
   neither the export under that name nor the call site exists.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


_REPO = Path(__file__).resolve().parent.parent
_OUTLINE_JS = _REPO / "static" / "outline.js"
_SESSIONS_JS = _REPO / "static" / "sessions.js"


# --------------------------------------------------------------------------- #
# Backend: match_message_idx is surfaced on content hits
# --------------------------------------------------------------------------- #
def _run_search(query, *, session_messages, sessions_meta=None):
    import api.routes as routes

    meta = sessions_meta or [
        {"session_id": "s1", "title": "Untitled", "profile": "default"}
    ]
    session = SimpleNamespace(session_id="s1", messages=session_messages)
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(meta)), patch(
        "api.routes.get_session", return_value=session
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def test_content_match_includes_message_index():
    """A content hit must carry match_message_idx pointing at the raw index
    inside sess.messages (so msg-user-<rawIdx> resolves on the client)."""
    msgs = [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "second message — no hit"},
        {"role": "user", "content": "NEEDLE in the third message"},
        {"role": "assistant", "content": "fourth message"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=10",
        session_messages=msgs,
    )
    assert captured["status"] == 200
    results = captured["payload"]["sessions"]
    assert len(results) == 1
    hit = results[0]
    assert hit["match_type"] == "content"
    assert hit["match_message_idx"] == 2, (
        "match_message_idx must be the raw enumerate index into sess.messages "
        "(0-based); the renderer stamps the same index onto msg-user-<rawIdx>"
    )


def test_content_match_returns_first_hit_index_not_last():
    """The scan break()s on the first hit (preserving existing behavior); the
    returned idx must reflect that first hit, not a later occurrence."""
    msgs = [
        {"role": "user", "content": "alpha NEEDLE first"},
        {"role": "user", "content": "beta NEEDLE second"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=10",
        session_messages=msgs,
    )
    assert captured["payload"]["sessions"][0]["match_message_idx"] == 0


def test_title_match_does_not_include_message_index():
    """Title matches short-circuit before the content scan, so they must not
    grow a match_message_idx field (nothing to jump to)."""
    meta = [{"session_id": "s1", "title": "needle in the title", "profile": "default"}]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1",
        session_messages=[{"role": "user", "content": "no hit here"}],
        sessions_meta=meta,
    )
    hit = captured["payload"]["sessions"][0]
    assert hit["match_type"] == "title"
    assert "match_message_idx" not in hit


def test_no_match_returns_empty_results():
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1",
        session_messages=[{"role": "user", "content": "no hit here"}],
    )
    assert captured["payload"]["count"] == 0


# --------------------------------------------------------------------------- #
# Frontend scope wiring: the jump helper must be reachable across <script>s
# --------------------------------------------------------------------------- #
def test_outline_js_exposes_jump_helper_on_window():
    """outline.js defines _jumpToMessage inside an IIFE; it must export it on
    window so other scripts can reach it across the <script> boundary. Without
    this export the sessions.js call below is a silent no-op (the #4224 bug)."""
    src = _OUTLINE_JS.read_text()
    assert "window._jumpToMessage = _jumpToMessage" in src, (
        "outline.js must expose _jumpToMessage on window (it is otherwise "
        "trapped inside the IIFE and unreachable from sessions.js)"
    )


def test_sessions_js_search_click_calls_window_jump_helper():
    """The search-result click path must invoke the FULL-SESSION jump helper via
    the cross-<script> window handle, NOT the bare in-scope name (which doesn't
    exist in sessions.js's scope and would silently do nothing).

    As of #5106 the search path uses ``window._jumpToFullSessionMessage`` (the
    full-session-index variant) rather than ``window._jumpToMessage`` (which
    expects a LOCAL render-window index) — match_message_idx is a full-session
    index, and treating it as local flashed the WRONG row in truncated sessions.
    """
    src = _SESSIONS_JS.read_text()
    # The dispatch is gated on a content match carrying an integer index.
    assert "s.match_type==='content'" in src
    assert "Number.isInteger(s.match_message_idx)" in src
    # And it must call the full-session helper through window (the reachable handle).
    assert "window._jumpToFullSessionMessage(" in src, (
        "sessions.js must call window._jumpToFullSessionMessage (the full-session "
        "index variant, reachable across scripts) for the content-search jump"
    )


def test_sessions_js_does_not_call_bare_jump_helper():
    """Guard against regressing to the original bug: a bare _jumpToMessage(
    call in sessions.js resolves to nothing because the definition lives in
    outline.js's IIFE. Every call site here must go through window."""
    src = _SESSIONS_JS.read_text()
    # Find any `_jumpToMessage(` call not immediately preceded by `window.`
    # or `.` (method access) — i.e. a bare cross-script call into the void.
    bare = re.findall(r"(?<![.\w])_jumpToMessage\s*\(", src)
    assert not bare, (
        "sessions.js contains a bare _jumpToMessage(...) call; it must be "
        "window._jumpToMessage(...) to cross the <script> boundary"
    )


def test_outline_exposes_full_session_jump_helper():
    """#5106: outline.js must expose _jumpToFullSessionMessage on window — the
    full-session-index variant the content-search path uses to avoid the
    local-vs-full coordinate bug in truncated sessions."""
    src = _OUTLINE_JS.read_text()
    assert "window._jumpToFullSessionMessage = _jumpToFullSessionMessage" in src
    assert "function _jumpToFullSessionMessage(" in src


def test_full_session_jump_translates_full_to_local_and_forceloads():
    """#5106: _jumpToFullSessionMessage must (a) translate a full-session index to
    a LOCAL DOM index via _oldestIdx, and (b) force-load full history when the
    session is truncated (so the offset tail doesn't leave the wrong row). Guards
    against re-introducing the raw getElementById('msg-user-' + fullIdx) bug."""
    src = _OUTLINE_JS.read_text()
    # body of the full-session helper
    start = src.index("function _jumpToFullSessionMessage(")
    body = src[start:start + 1800]
    # (a) full -> local translation via _oldestIdx
    assert "_oldestIdx" in body and "fullIdx - off" in body, (
        "_jumpToFullSessionMessage must translate full-session index to local via _oldestIdx"
    )
    # (b) truncated sessions force-load full history (msg_limit=9999) before resolving
    assert "_messagesTruncated" in body
    assert "msg_limit=9999" in body
    # and it must NOT resolve a raw full index directly as a DOM id
    assert "'msg-user-' + fullIdx" not in body, (
        "must resolve the LOCAL index, never the raw full-session index, as the DOM id"
    )


def test_full_session_jump_resolves_assistant_segments_and_guards_session():
    """#5106 Codex follow-up: a content hit on an ASSISTANT message must jump too
    (assistant rows have no msg-user id — they render as
    .assistant-segment[data-msg-idx]), and a session-switch race must not apply
    one session's match index to another (the helper takes a targetSid + re-checks
    the active session before scrolling)."""
    src = _OUTLINE_JS.read_text()
    start = src.index("function _jumpToFullSessionMessage(")
    body = src[start:start + 2400]
    # assistant-segment fallback
    assert '.assistant-segment[data-msg-idx="' in body
    # session-switch guard: helper accepts targetSid and re-checks active session
    assert "targetSid" in body
    assert "S.session.session_id !== sid" in body
    # caller passes the clicked session id
    sess = _SESSIONS_JS.read_text()
    assert "_jumpToFullSessionMessage(_jumpIdx, _jumpSid)" in sess
    assert "_jumpSid=s.session_id" in sess
