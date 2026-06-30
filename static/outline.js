// ── Conversation Outline Panel (#2124) ───────────────────────────────────────
// Floating panel listing user messages as jump targets.
// _outlineSid guards against stale renders when the user switches sessions.

'use strict';

(function() {

let _outlineSid = null;       // session id the panel was last built for
let _panelOpen  = false;      // whether the panel is currently visible
let _outlineResizeObserver = null;
let _outlineWorkspaceObserver = null;

// Returns the current session id, or null if no session is loaded.
function _currentSid() {
  return (S && S.session && S.session.session_id) || null;
}

function _outlineAllowed() {
  const compact = window.matchMedia && window.matchMedia('(max-width:900px)').matches;
  // The outline is a chat-view affordance only — never show the toggle or panel
  // while another MAIN panel (settings, tasks, insights, …) is active. _currentPanel
  // is owned by panels.js; treat an undefined/absent value as the chat default.
  // 'todos' is a sidebar-only panel that leaves the chat transcript in <main>, so
  // the outline stays valid there too (and switching to it emits no <main> class
  // mutation for the observer, so allowing it keeps the toggle stable).
  const panel = (typeof _currentPanel === 'undefined') ? 'chat' : (_currentPanel || 'chat');
  const onChatView = panel === 'chat' || panel === 'todos';
  return window._showConversationOutline === true && !compact && onChatView;
}

function _syncOutlinePosition() {
  const root = document.documentElement;
  const panel = document.querySelector('.rightpanel');
  const open = root.dataset.workspacePanel === 'open';
  const width = open && panel ? Math.max(0, Math.round(panel.offsetWidth || 0)) : 0;
  root.style.setProperty('--outline-workspace-offset', width + 'px');
}

function applyConversationOutlinePreference() {
  const toggle = document.getElementById('outlineToggleBtn');
  const wrapper = document.getElementById('outlinePanelWrapper');
  const enabled = _outlineAllowed();
  document.documentElement.dataset.conversationOutline = enabled ? 'enabled' : 'disabled';
  _syncOutlinePosition();
  if (toggle) toggle.hidden = !enabled;
  if (!enabled) {
    _panelOpen = false;
    if (wrapper) wrapper.hidden = true;
  }
}

function _expandOutlineRenderWindow() {
  if (typeof _currentMessageRenderWindowSize !== 'function' ||
      typeof _messageRenderableMessageCount !== 'function' ||
      typeof _messageRenderWindowSize === 'undefined') return;
  _messageRenderWindowSize = Math.max(
    _currentMessageRenderWindowSize(),
    _messageRenderableMessageCount()
  );
}

function _ensureOutlineMessagesLoaded(sid) {
  if (!sid || S.busy || S.activeStreamId) return Promise.resolve(false);
  if (typeof _messagesTruncated === 'undefined' || !_messagesTruncated) {
    return Promise.resolve(false);
  }
  if (typeof _ensureAllMessagesLoaded !== 'function') return Promise.resolve(false);
  return _ensureAllMessagesLoaded().then(function() {
    if (!S.session || S.session.session_id !== sid) return false;
    _expandOutlineRenderWindow();
    return true;
  }).catch(function() {
    return false;
  });
}

// Extracts the first 60 visible characters from a message content value.
function _excerptText(content) {
  let text = '';
  if (Array.isArray(content)) {
    text = content
      .filter(p => p && p.type === 'text')
      .map(p => p.text || p.content || '')
      .join(' ');
  } else {
    text = String(content || '');
  }
  text = text.trim().replace(/\s+/g, ' ');
  return text.length > 60 ? text.slice(0, 60) + '…' : text;
}

// Scrolls to a user message row identified by its LOCAL rawIdx and flashes it.
// (Outline entries pass a local S.messages index — consistent with the
// `msg-user-<localIdx>` DOM ids stamped during render.)
function _jumpToMessage(rawIdx) {
  const sid = _currentSid();
  if (!sid) return;

  const rowId = 'msg-user-' + rawIdx;
  const row   = document.getElementById(rowId);
  if (row) {
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    _flashRow(row);
    return;
  }

  // Row is outside the render window — reload the full session and retry.
  if (typeof api !== 'function') return;
  if (S.busy || S.activeStreamId) return;
  api('/api/session?session_id=' + encodeURIComponent(sid) +
      '&messages=1&resolve_model=0&msg_limit=9999')
    .then(function(data) {
      if (!data || !data.session) return;
      if (!S.session || S.session.session_id !== sid) return;  // session switched
      S.messages = data.session.messages || [];                // populate S
      // Full history loaded — the response is no longer an offset tail, so
      // local indices == full-session indices. Keep _oldestIdx/_messagesTruncated
      // in sync so downstream id stamping + translation stay correct (#5106).
      if (typeof _oldestIdx !== 'undefined') _oldestIdx = data.session._messages_offset || 0;
      if (typeof _messagesTruncated !== 'undefined') _messagesTruncated = !!data.session._messages_truncated;
      _expandOutlineRenderWindow();
      if (typeof renderMessages === 'function') renderMessages({ preserveScroll: true });
      window.setTimeout(function() {
        if (!S.session || S.session.session_id !== sid) return;
        const localIdx = (typeof _oldestIdx !== 'undefined' && _oldestIdx > 0) ? (rawIdx - _oldestIdx) : rawIdx;
        const r = document.getElementById('msg-user-' + localIdx);
        if (r) { r.scrollIntoView({ block: 'center', behavior: 'smooth' }); _flashRow(r); }
      }, 120);
    })
    .catch(function() {});
}

// Scrolls to a message identified by its FULL-SESSION index (e.g. a content
// search `match_message_idx` over the complete sess.messages list) and flashes
// it. The transcript may be loaded as a TAIL WINDOW with DOM ids stamped from
// LOCAL indices offset by `_oldestIdx`, so a raw `getElementById('msg-user-' +
// fullIdx)` would resolve the WRONG row (or none) in a truncated session (#5106
// / #4159). This path force-loads the full history first (so _oldestIdx == 0 and
// local == full), then resolves; if already fully loaded it translates
// full -> local via _oldestIdx.
function _jumpToFullSessionMessage(fullIdx, targetSid) {
  const sid = targetSid || _currentSid();
  if (!sid || !Number.isInteger(fullIdx) || fullIdx < 0) return;

  function _resolveAndFlash() {
    // Guard against a session switch between click and resolve (#5106): only act
    // if the intended session is still the active one.
    if (!S.session || S.session.session_id !== sid) return false;
    const off = (typeof _oldestIdx !== 'undefined' && Number.isFinite(Number(_oldestIdx))) ? Number(_oldestIdx) : 0;
    const localIdx = fullIdx - off;
    if (localIdx < 0) return false;
    // User-message rows carry msg-user-<localIdx>; assistant rows render as
    // .assistant-segment[data-msg-idx=<localIdx>] with no msg-user id. A content
    // search can match EITHER role, so resolve both (#5106 Codex follow-up).
    let target = document.getElementById('msg-user-' + localIdx);
    if (!target) {
      const seg = document.querySelector('.assistant-segment[data-msg-idx="' + localIdx + '"]');
      if (seg) target = seg.closest('.assistant-turn') || seg;
    }
    if (target) { target.scrollIntoView({ block: 'center', behavior: 'smooth' }); _flashRow(target); return true; }
    return false;
  }

  const truncated = (typeof _messagesTruncated !== 'undefined' && _messagesTruncated) ||
                    (typeof _oldestIdx !== 'undefined' && Number(_oldestIdx) > 0);
  // If the session isn't truncated, local == full and the row is already present.
  if (!truncated) { _resolveAndFlash(); return; }

  if (typeof api !== 'function' || S.busy || S.activeStreamId) { _resolveAndFlash(); return; }
  api('/api/session?session_id=' + encodeURIComponent(sid) +
      '&messages=1&resolve_model=0&msg_limit=9999')
    .then(function(data) {
      if (!data || !data.session) return;
      if (!S.session || S.session.session_id !== sid) return;  // session switched
      S.messages = data.session.messages || [];
      if (typeof _oldestIdx !== 'undefined') _oldestIdx = data.session._messages_offset || 0;
      if (typeof _messagesTruncated !== 'undefined') _messagesTruncated = !!data.session._messages_truncated;
      _expandOutlineRenderWindow();
      if (typeof renderMessages === 'function') renderMessages({ preserveScroll: true });
      window.setTimeout(function() {
        _resolveAndFlash();
      }, 120);
    })
    .catch(function() {});
}

// Brief highlight flash on a message row after jumping.
function _flashRow(row) {
  if (!row) return;
  row.classList.remove('outline-jump-flash');
  void row.offsetWidth;   // reflow to restart animation
  row.classList.add('outline-jump-flash');
  window.setTimeout(function() { row.classList.remove('outline-jump-flash'); }, 1200);
}

// Builds the list of user messages from S.messages.
// Returns [{rawIdx, label, excerpt}, …] for every user message with content.
function _buildEntries() {
  const msgs = (S && S.messages) || [];
  const entries = [];
  let userN = 0;

  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    if (!m || m.role !== 'user') continue;
    const text = _excerptText(m.content);
    if (!text) continue;
    userN++;
    entries.push({ rawIdx: i, label: userN, excerpt: text });
  }
  return entries;
}

// Renders the panel body.  Called every time the panel opens or session changes.
function _renderPanel() {
  const panel = document.getElementById('outlinePanel');
  if (!panel) return;

  const sid = _currentSid();

  // Session-scoped staleness guard.
  if (!sid) {
    panel.innerHTML = '<p class="outline-empty">' + t('outline_empty') + '</p>';
    _outlineSid = null;
    return;
  }

  if (!S.messages) {
    panel.innerHTML = '<p class="outline-empty">' + t('outline_loading') + '</p>';
    _outlineSid = sid;
    return;
  }

  _outlineSid = sid;
  const entries = _buildEntries();

  if (!entries.length) {
    panel.innerHTML = '<p class="outline-empty">' + t('outline_empty') + '</p>';
    return;
  }

  const items = entries.map(function(e) {
    return '<button class="outline-entry" type="button" ' +
      'onclick="window._outlineJump(' + e.rawIdx + ')">' +
      '<span class="outline-entry-num">' + e.label + '</span>' +
      '<span class="outline-entry-text">' + _escHtml(e.excerpt) + '</span>' +
      '</button>';
  });

  panel.innerHTML = items.join('');
}

// Simple HTML-escape for entry text.
function _escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Opens or closes the outline panel.
function toggleOutlinePanel() {
  if (!_outlineAllowed()) {
    applyConversationOutlinePreference();
    return;
  }
  _panelOpen = !_panelOpen;
  const wrapper = document.getElementById('outlinePanelWrapper');
  if (!wrapper) return;

  if (_panelOpen) {
    _syncOutlinePosition();
    wrapper.hidden = false;
    const sid = _currentSid();
    const panel = document.getElementById('outlinePanel');
    if (panel) panel.innerHTML = '<p class="outline-empty">' + t('outline_loading') + '</p>';
    _ensureOutlineMessagesLoaded(sid).then(function() {
      if (!_panelOpen || _currentSid() !== sid) return;
      _renderPanel();
      // Keep rendered data fresh after every renderMessages() call.
      _outlineSid = _currentSid();
    });
  } else {
    wrapper.hidden = true;
  }
}

// Jump target exposed on window so inline onclick handlers can reach it.
window._outlineJump = _jumpToMessage;
// Also expose under its own name so OTHER scripts (e.g. sessions.js's
// search-result click handler, #4159) can reuse the jump-and-flash helper
// across the <script> boundary — _jumpToMessage is otherwise trapped inside
// this IIFE and unreachable from sessions.js.
window._jumpToMessage = _jumpToMessage;
// Full-session-index variant for the content-search jump (#5106): callers that
// have a `match_message_idx` over the COMPLETE sess.messages list must use this
// (not _jumpToMessage, which expects a local render-window index) so a truncated
// session resolves the correct row instead of the local-Nth.
window._jumpToFullSessionMessage = _jumpToFullSessionMessage;
window.applyConversationOutlinePreference = applyConversationOutlinePreference;

// Re-render after renderMessages() if the panel is open and the session
// changed or new messages arrived since the last render.
(function _hookRenderMessages() {
  if (typeof window._outlineRenderHooked !== 'undefined') return;

  const _orig = window.renderMessages;
  if (typeof _orig !== 'function') {
    // renderMessages may not be defined yet — retry after DOMContentLoaded.
    if (!window._outlineRenderHookPending) {
      window._outlineRenderHookPending = true;
      document.addEventListener('DOMContentLoaded', _hookRenderMessages, { once: true });
    }
    return;
  }
  window._outlineRenderHooked = true;
  window._outlineRenderHookPending = false;
  window.renderMessages = function() {
    const result = _orig.apply(this, arguments);
    if (_panelOpen) {
      const sid = _currentSid();
      if (sid && (sid !== _outlineSid || (S.messages || []).length > 0)) {
        _renderPanel();
      }
    }
    return result;
  };
})();

// Expose public API.
window.toggleOutlinePanel = toggleOutlinePanel;

document.addEventListener('DOMContentLoaded', function() {
  applyConversationOutlinePreference();
  const root = document.documentElement;
  const rightPanel = document.querySelector('.rightpanel');
  if (rightPanel && typeof ResizeObserver !== 'undefined' && !_outlineResizeObserver) {
    _outlineResizeObserver = new ResizeObserver(_syncOutlinePosition);
    _outlineResizeObserver.observe(rightPanel);
  }
  if (!_outlineWorkspaceObserver) {
    _outlineWorkspaceObserver = new MutationObserver(applyConversationOutlinePreference);
    _outlineWorkspaceObserver.observe(root, {
      attributes: true,
      attributeFilter: ['data-workspace-panel']
    });
    // Also re-evaluate when the active main panel changes. switchPanel() is a
    // global function declaration (called via inline onclick), so it can't be
    // reliably wrapped from this script; instead we watch the `showing-<panel>`
    // class it toggles on <main>. The outline is a chat-only affordance, so this
    // hides the toggle + closes the panel when leaving chat (settings, tasks,
    // insights, …) and restores the toggle on return to chat. _outlineAllowed()
    // reads _currentPanel for the actual gate; this observer just triggers it.
    const mainEl = document.querySelector('main.main');
    if (mainEl) {
      _outlineWorkspaceObserver.observe(mainEl, {
        attributes: true,
        attributeFilter: ['class']
      });
    }
  }
});
window.addEventListener('resize', applyConversationOutlinePreference);

})();
