/* Agent Équipe — frontend
 * Communique avec le proxy team-app (server.py) qui isole chaque employé
 * sur son profil Hermes.
 */

"use strict";

// ── État global ─────────────────────────────────────────────────────────────
const S = {
  user: null,          // {username, display_name, profile}
  session: null,       // session de chat active (objet renvoyé par /api/session/new)
  sessions: [],        // liste des sessions de l'employé
  streaming: false,    // un flux SSE est en cours
  activeStreamId: null,
  eventSource: null,
};

// ── Raccourcis DOM ──────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const composerEl = $("composer");
const sendBtn = $("send-btn");
const cancelBtn = $("cancel-btn");
const emptyState = $("empty-state");
const sessionListEl = $("session-list");
const statusChip = $("status-chip");
const chatTitle = $("chat-title");

// ── Utilitaires ─────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (res.status === 401) {
    // Session expirée → retour à la connexion
    window.location.href = "/";
    throw new Error("unauthorized");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Erreur ${res.status}`);
  return data;
}

function setStatus(text, cls = "") {
  statusChip.textContent = text;
  statusChip.style.color = cls === "ok" ? "var(--ok)" : cls === "err" ? "var(--danger)" : "var(--muted)";
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Rendu markdown minimaliste (gras, italique, code, blocs de code)
function renderMd(text) {
  let html = escapeHtml(text);
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
    `<pre><code>${code}</code></pre>`);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(^|\s)\*([^*\n]+)\*/g, "$1<em>$2</em>");
  html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  html = html.replace(/^- (.+)$/gm, "• $1");
  html = html.replace(/\n/g, "<br>");
  return html;
}

function addMsg(role, content, extraClass = "") {
  if (emptyState) emptyState.style.display = "none";
  const div = document.createElement("div");
  div.className = `msg ${role} ${extraClass}`.trim();
  if (role === "user") {
    div.textContent = content;
  } else {
    div.innerHTML = renderMd(content);
  }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

// ── Sessions ─────────────────────────────────────────────────────────────────
async function refreshSessions() {
  try {
    const data = await api("/api/sessions");
    S.sessions = (data.sessions || [])
      .filter((s) => !s.archived)
      .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    renderSessionList();
  } catch (e) {
    console.warn("refreshSessions:", e);
  }
}

function renderSessionList() {
  sessionListEl.innerHTML = "";
  for (const s of S.sessions) {
    const item = document.createElement("div");
    item.className = "session-item" + (S.session && S.session.session_id === s.session_id ? " active" : "");
    const title = s.title || "Sans titre";
    item.title = title;
    item.textContent = title;

    const del = document.createElement("span");
    del.className = "del";
    del.textContent = "✕";
    del.title = "Supprimer";
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("Supprimer cette conversation ?")) return;
      try {
        await api("/api/session/delete", {
          method: "POST",
          body: JSON.stringify({ session_id: s.session_id }),
        });
        if (S.session && S.session.session_id === s.session_id) {
          S.session = null;
          messagesEl.innerHTML = "";
          if (emptyState) messagesEl.appendChild(emptyState), emptyState.style.display = "";
          chatTitle.textContent = "Nouvelle conversation";
        }
        await refreshSessions();
      } catch (err) {
        alert("Suppression impossible : " + err.message);
      }
    };
    item.appendChild(del);

    item.onclick = () => loadSession(s.session_id);
    sessionListEl.appendChild(item);
  }
}

async function newChat() {
  if (S.streaming) { alert("Attendez la fin de la réponse en cours."); return; }
  setStatus("Nouvelle conversation…");
  try {
    const data = await api("/api/session/new", {
      method: "POST",
      body: JSON.stringify({}),
    });
    S.session = data.session;
    messagesEl.innerHTML = "";
    if (emptyState) { messagesEl.appendChild(emptyState); emptyState.style.display = ""; }
    chatTitle.textContent = "Nouvelle conversation";
    composerEl.focus();
    setStatus("", "ok");
    await refreshSessions();
  } catch (e) {
    setStatus("Erreur : " + e.message, "err");
  }
}

async function loadSession(sid) {
  if (S.streaming) { alert("Attendez la fin de la réponse en cours."); return; }
  if (S.session && S.session.session_id === sid) return;
  setStatus("Chargement…");
  try {
    const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}`);
    S.session = data.session;
    messagesEl.innerHTML = "";
    const msgs = (data.session.messages || []).filter((m) => m.role === "user" || m.role === "assistant");
    if (!msgs.length && emptyState) { messagesEl.appendChild(emptyState); emptyState.style.display = ""; }
    for (const m of msgs) {
      const div = document.createElement("div");
      div.className = `msg ${m.role}`;
      div.innerHTML = m.role === "user" ? escapeHtml(m.content) : renderMd(m.content || "");
      messagesEl.appendChild(div);
    }
    chatTitle.textContent = data.session.title || "Conversation";
    messagesEl.scrollTop = messagesEl.scrollHeight;
    setStatus("", "ok");
    renderSessionList();
  } catch (e) {
    setStatus("Erreur : " + e.message, "err");
  }
}

// ── Envoi de message + streaming SSE ────────────────────────────────────────
async function sendMessage() {
  const text = composerEl.value.trim();
  if (!text || S.streaming) return;

  if (!S.session) await newChat();
  if (!S.session) return;

  composerEl.value = "";
  autoGrow();
  addMsg("user", text);

  S.streaming = true;
  sendBtn.hidden = true;
  cancelBtn.hidden = false;
  setStatus("Réflexion…");

  // Bulle assistant en construction
  let assistantDiv = null;
  let assistantText = "";
  let reasoningDiv = null;

  const ensureAssistant = () => {
    if (!assistantDiv) {
      assistantDiv = document.createElement("div");
      assistantDiv.className = "msg assistant typing-dots";
      messagesEl.appendChild(assistantDiv);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
    return assistantDiv;
  };

  try {
    // 1. Démarre le tour
    const start = await api("/api/chat/start", {
      method: "POST",
      body: JSON.stringify({
        session_id: S.session.session_id,
        message: text,
      }),
    });

    const streamId = start.stream_id;
    S.activeStreamId = streamId;

    // 409 = flux déjà actif sur cette session
    if (start.error && start.active_stream_id) {
      throw new Error("Cette conversation a déjà une réponse en cours.");
    }
    if (!streamId) throw new Error("Réponse inattendue du serveur");

    // 2. Écoute le flux SSE via le proxy
    await new Promise((resolve, reject) => {
      const src = new EventSource(`/proxy/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`);
      S.eventSource = src;

      const finalize = () => { src.close(); S.eventSource = null; resolve(); };

      src.onopen = () => setStatus("En cours…");

      src.addEventListener("token", (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d.text) {
            assistantText += d.text;
            const div = ensureAssistant();
            div.classList.remove("typing-dots");
            div.innerHTML = renderMd(assistantText);
            messagesEl.scrollTop = messagesEl.scrollHeight;
          }
        } catch (_) {}
      });

      src.addEventListener("reasoning", (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d.text && !reasoningDiv) {
            reasoningDiv = addMsg("assistant", "💭 Réflexion en cours…", "tool-note");
          }
        } catch (_) {}
      });

      src.addEventListener("tool", (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d.name && d.name !== "clarify") {
            const note = addMsg("assistant", `🔧 Outil : ${d.name}…`, "tool-note");
          }
        } catch (_) {}
      });

      src.addEventListener("stream_end", (e) => {
        if (reasoningDiv) reasoningDiv.remove();
        finalize();
      });

      src.addEventListener("error", (e) => {
        // EventSource 'error' arrive aussi à la fermeture normale du flux
        if (S.streaming) {
          finalize();
        } else {
          finalize();
        }
      });
    });

    setStatus("", "ok");
  } catch (e) {
    if (reasoningDiv) reasoningDiv.remove();
    addMsg("assistant", "⚠️ " + (e.message || "Erreur de communication"), "error");
    setStatus("Erreur", "err");
  } finally {
    S.streaming = false;
    S.activeStreamId = null;
    sendBtn.hidden = false;
    cancelBtn.hidden = true;
    await refreshSessions();
  }
}

async function cancelStream() {
  if (!S.activeStreamId) return;
  try {
    await api(`/api/chat/cancel?stream_id=${encodeURIComponent(S.activeStreamId)}`, { method: "POST" });
    setStatus("Annulé", "err");
  } catch (e) {
    console.warn("cancel:", e);
  }
}

// ── Divers ───────────────────────────────────────────────────────────────────
function autoGrow() {
  composerEl.style.height = "auto";
  composerEl.style.height = Math.min(composerEl.scrollHeight, 160) + "px";
}

composerEl.addEventListener("input", autoGrow);
composerEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
sendBtn.onclick = sendMessage;
cancelBtn.onclick = cancelStream;
$("new-chat-btn").onclick = newChat;
$("logout-btn").onclick = async () => {
  try { await api("/api/team/logout", { method: "POST" }); } catch (_) {}
  window.location.href = "/";
};

// Menu burger mobile
$("burger-btn").onclick = () => $("sidebar").classList.toggle("open");

// ── Initialisation ──────────────────────────────────────────────────────────
(async function init() {
  // Vérifie la session de connexion via le proxy
  try {
    const me = await api("/api/team/me");
    S.user = me;
    $("who").textContent = me.display_name ? `${me.display_name} (${me.username})` : me.username;
    const data = await api("/api/sessions");
    S.sessions = (data.sessions || []).filter((s) => !s.archived);
    renderSessionList();
    setStatus("", "ok");
  } catch (e) {
    return; // api() a déjà redirigé vers /
  }
  composerEl.focus();
})();