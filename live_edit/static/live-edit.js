/**
 * live-edit.js — Client-side panel for natural-language-driven code editing.
 *
 * Usage: <script src="/live-edit/static/live-edit.js"></script>
 * Toggle panel: Ctrl+Shift+D (configurable) or click the toggle button.
 */

(function () {
  "use strict";

  const API_PREFIX = "/live-edit";
  const STORAGE_KEY = "le:panel-open";

  // ── State ──

  let panelEl = null;
  let bodyEl = null;
  let currentSessionId = null;
  let eventSource = null;
  let isStreaming = false;

  // ── DOM Construction ──

  function createPanel() {
    if (panelEl) return;

    // Toggle button
    const toggle = document.createElement("button");
    toggle.id = "le-toggle";
    toggle.textContent = "即时编辑";
    toggle.title = "打开即时编辑面板 (Ctrl+Shift+D)";
    toggle.addEventListener("click", togglePanel);
    document.body.appendChild(toggle);

    // Panel
    panelEl = document.createElement("div");
    panelEl.id = "le-panel";
    panelEl.className = "le-collapsed";
    panelEl.innerHTML = `
      <div class="le-header">
        <h3>即时编辑</h3>
        <div class="le-header-actions">
          <select class="le-mode-select" id="le-mode" title="切换模式">
            <option value="quick">快速修改</option>
            <option value="deep">深度开发</option>
            <option value="qa">代码问答</option>
          </select>
          <button class="le-btn" id="le-timeline-btn" title="历史记录">历史</button>
          <button class="le-btn" id="le-close-btn" title="关闭面板">✕</button>
        </div>
      </div>
      <div class="le-body" id="le-body">
        <div class="le-input-area">
          <textarea class="le-textarea" id="le-input"
            placeholder="描述你想要的效果，例如：把标题改成蓝色、添加一个搜索框..."
            rows="2"></textarea>
          <div class="le-submit-row">
            <button class="le-btn le-btn-primary" id="le-submit" disabled>发送</button>
          </div>
        </div>
        <div class="le-timeline" id="le-timeline">
          <div class="le-empty">
            <p>输入自然语言描述，AI 会帮你修改代码。</p>
            <p style="color:var(--le-text-muted);font-size:12px">
              快速修改：逐操作确认<br>
              深度开发：自主执行，最终审核<br>
              代码问答：只读分析
            </p>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(panelEl);

    bodyEl = document.getElementById("le-body");

    // Events
    document.getElementById("le-submit").addEventListener("click", startSession);
    document.getElementById("le-close-btn").addEventListener("click", () => collapsePanel());
    document.getElementById("le-timeline-btn").addEventListener("click", showTimeline);
    document.getElementById("le-mode").addEventListener("change", onModeChange);

    const input = document.getElementById("le-input");
    input.addEventListener("input", () => {
      document.getElementById("le-submit").disabled = !input.value.trim() || isStreaming;
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (input.value.trim() && !isStreaming) startSession();
      }
    });

    // Restore open state
    if (localStorage.getItem(STORAGE_KEY) === "1") {
      expandPanel();
    }
  }

  // ── Panel visibility ──

  function expandPanel() {
    panelEl.classList.remove("le-collapsed");
    localStorage.setItem(STORAGE_KEY, "1");
  }

  function collapsePanel() {
    panelEl.classList.add("le-collapsed");
    localStorage.setItem(STORAGE_KEY, "0");
  }

  function togglePanel() {
    if (panelEl.classList.contains("le-collapsed")) {
      expandPanel();
    } else {
      collapsePanel();
    }
  }

  // ── SSE Streaming ──

  async function startSession() {
    const input = document.getElementById("le-input");
    const request = input.value.trim();
    const mode = document.getElementById("le-mode").value;

    if (!request || isStreaming) return;

    isStreaming = true;
    document.getElementById("le-submit").disabled = true;

    clearTimeline();
    addEvent("thinking_started", { label: "处理中…" });

    try {
      const response = await fetch(API_PREFIX + "/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request, mode }),
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        addError(err.detail || "请求失败");
        isStreaming = false;
        document.getElementById("le-submit").disabled = false;
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const event = JSON.parse(line.slice(6));
              handleEvent(event);
            } catch (e) {
              // ignore parse errors
            }
          }
        }
      }
    } catch (e) {
      addError("连接中断: " + e.message);
    } finally {
      isStreaming = false;
      document.getElementById("le-submit").disabled = false;
      document.getElementById("le-input").value = "";
    }
  }

  async function continueSession() {
    if (!currentSessionId || isStreaming) return;

    const input = document.getElementById("le-input");
    const request = input.value.trim();
    const mode = document.getElementById("le-mode").value;

    if (!request) return;

    isStreaming = true;
    document.getElementById("le-submit").disabled = true;

    try {
      const response = await fetch(API_PREFIX + "/continue/" + currentSessionId, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request, mode }),
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        addError(err.detail || "继续会话失败");
        isStreaming = false;
        document.getElementById("le-submit").disabled = false;
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const event = JSON.parse(line.slice(6));
              handleEvent(event);
            } catch (e) {}
          }
        }
      }
    } catch (e) {
      addError("连接中断: " + e.message);
    } finally {
      isStreaming = false;
      document.getElementById("le-submit").disabled = false;
      document.getElementById("le-input").value = "";
    }
  }

  // ── Event handlers ──

  function handleEvent(event) {
    const type = event.type;
    removeEvent("thinking_started");

    switch (type) {
      case "session":
        currentSessionId = event.session_id;
        break;

      case "thinking":
        addEvent("thinking", { text: event.text });
        break;

      case "text":
        addEvent("text", { text: event.text });
        break;

      case "tool_plan":
        if (event.auto) {
          addEvent("tool_executing", { summary: event.summary, id: event.id });
        } else {
          addApprovalCard(event);
        }
        break;

      case "tool_result":
        removeEvent("tool_executing", event.id);
        if (event.ok) {
          addEvent("tool_done", { summary: event.path || event.cmd || "完成" });
        } else {
          addEvent("tool_error", { summary: event.error || "执行失败" });
        }
        break;

      case "diff":
        addDiffView(event);
        addFinalApproval(event);
        break;

      case "done":
        addEvent("done", { message: event.message || "完成" });
        currentSessionId = null;
        break;

      case "error":
        addError(event.error || "未知错误");
        break;
    }
  }

  // ── UI Rendering ──

  function clearTimeline() {
    const tl = document.getElementById("le-timeline");
    tl.innerHTML = "";
  }

  function addEvent(type, data) {
    const tl = document.getElementById("le-timeline");
    const el = document.createElement("div");
    el.className = "le-event le-event-" + type;
    if (data.id) el.dataset.eventId = data.id;

    switch (type) {
      case "thinking_started":
        el.innerHTML = '<span class="le-spinner"></span> ' + (data.label || "思考中…");
        break;
      case "thinking":
        el.innerHTML = '<div class="le-event-label">思考</div><div class="le-event-thinking">' + escapeHtml(data.text) + "</div>";
        break;
      case "text":
        el.innerHTML = '<div class="le-event-text">' + escapeHtml(data.text) + "</div>";
        break;
      case "tool_executing":
        el.innerHTML = '<span class="le-spinner"></span> ' + escapeHtml(data.summary || "执行中…");
        break;
      case "tool_done":
        el.innerHTML = '<span style="color:var(--le-success)">&#10003;</span> ' + escapeHtml(data.summary || "完成");
        break;
      case "tool_error":
        el.innerHTML = '<span style="color:var(--le-error)">&#10007;</span> ' + escapeHtml(data.summary || "失败");
        break;
      case "done":
        el.className = "le-done";
        el.innerHTML = data.message;
        break;
    }

    tl.appendChild(el);
    bodyEl.scrollTop = bodyEl.scrollHeight;
    return el;
  }

  function removeEvent(type, id) {
    const el = document.querySelector('.le-event[data-event-id="' + id + '"]');
    if (el) el.remove();
  }

  function addError(message) {
    const tl = document.getElementById("le-timeline");
    const el = document.createElement("div");
    el.className = "le-error-message";
    el.textContent = message;
    tl.appendChild(el);
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  function addApprovalCard(event) {
    const tl = document.getElementById("le-timeline");
    const card = document.createElement("div");
    card.className = "le-tool-card";
    card.dataset.toolId = event.id;
    card.innerHTML = `
      <div class="le-tool-summary">${escapeHtml(event.summary || event.tool)}</div>
      ${event.reason ? '<div class="le-tool-detail">原因: ' + escapeHtml(event.reason) + "</div>" : ""}
      <div class="le-tool-actions">
        <button class="le-btn le-btn-danger le-reject-btn">拒绝</button>
        <button class="le-btn le-btn-primary le-approve-btn">批准</button>
      </div>
    `;

    card.querySelector(".le-approve-btn").addEventListener("click", () => {
      approveTool(event.id, true);
      card.querySelector(".le-tool-actions").innerHTML =
        '<span style="color:var(--le-success)">已批准 &#10003;</span>';
    });

    card.querySelector(".le-reject-btn").addEventListener("click", () => {
      approveTool(event.id, false);
      card.querySelector(".le-tool-actions").innerHTML =
        '<span style="color:var(--le-error)">已拒绝 &#10007;</span>';
    });

    tl.appendChild(card);
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  function addDiffView(event) {
    const tl = document.getElementById("le-timeline");
    const block = document.createElement("div");
    block.className = "le-diff-block";
    block.innerHTML = `
      <div class="le-diff-header">变更摘要: ${escapeHtml(event.summary || "")}</div>
      <div class="le-diff-content">${renderDiff(event.diff || "")}</div>
    `;
    tl.appendChild(block);
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  function addFinalApproval(event) {
    const tl = document.getElementById("le-timeline");
    const card = document.createElement("div");
    card.className = "le-approval-card";
    card.innerHTML = `
      <div class="le-approval-summary">应用以上所有更改？</div>
      <div class="le-approval-actions">
        <button class="le-btn le-btn-danger le-final-reject">全部放弃</button>
        <button class="le-btn le-btn-primary le-final-approve">应用更改</button>
      </div>
    `;

    card.querySelector(".le-final-approve").addEventListener("click", () => {
      if (currentSessionId) {
        approveTool("__final__", true);
      }
      card.innerHTML = '<div class="le-done">更改已应用 &#10003;</div>';
    });

    card.querySelector(".le-final-reject").addEventListener("click", () => {
      if (currentSessionId) {
        approveTool("__final__", false);
      }
      card.innerHTML = '<div class="le-done" style="color:var(--le-error)">更改已放弃</div>';
    });

    tl.appendChild(card);
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  function renderDiff(diffText) {
    return diffText
      .split("\n")
      .map((line) => {
        if (line.startsWith("+")) return '<span class="le-diff-add">' + escapeHtml(line) + "</span>";
        if (line.startsWith("-")) return '<span class="le-diff-remove">' + escapeHtml(line) + "</span>";
        return escapeHtml(line);
      })
      .join("\n");
  }

  // ── Actions ──

  async function approveTool(toolId, approved) {
    if (!currentSessionId) return;
    try {
      await fetch(API_PREFIX + "/approve/" + currentSessionId + "/" + toolId, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved }),
      });
    } catch (e) {
      console.error("live-edit: approve error", e);
    }
  }

  function onModeChange() {
    const mode = document.getElementById("le-mode").value;
    const input = document.getElementById("le-input");
    if (mode === "qa") {
      input.placeholder = "描述你想了解的问题，例如：这个函数是做什么的？数据库表结构是怎样的？";
    } else if (mode === "deep") {
      input.placeholder = "描述你要实现的功能或修改，例如：重构用户认证模块，添加 JWT token 刷新逻辑";
    } else {
      input.placeholder = "描述你想要的效果，例如：把标题改成蓝色、添加一个搜索框...";
    }
  }

  async function showTimeline() {
    clearTimeline();
    addEvent("thinking_started", { label: "加载历史记录…" });

    try {
      const resp = await fetch(API_PREFIX + "/timeline?limit=20");
      const entries = await resp.json();
      removeEvent("thinking_started");

      if (!entries.length) {
        addEvent("text", { text: "暂无历史记录。" });
        return;
      }

      const tl = document.getElementById("le-timeline");
      for (const entry of entries) {
        const el = document.createElement("div");
        el.className = "le-event";
        const msg = entry.message || entry.session?.request || "";
        const date = entry.date ? entry.date.slice(0, 16) : "";
        el.innerHTML = `
          <div class="le-event-label">${date}</div>
          <div class="le-event-text">${escapeHtml(msg)}</div>
          ${entry.commit_hash ? '<div style="font-size:11px;color:var(--le-text-muted)">' + entry.commit_hash + "</div>" : ""}
        `;
        tl.appendChild(el);
      }
    } catch (e) {
      removeEvent("thinking_started");
      addError("加载历史失败: " + e.message);
    }
  }

  // ── Helpers ──

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // ── Keyboard shortcut ──

  document.addEventListener("keydown", (e) => {
    if (e.key === "D" && e.ctrlKey && e.shiftKey) {
      e.preventDefault();
      togglePanel();
    }
  });

  // ── Init ──

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", createPanel);
  } else {
    createPanel();
  }
})();
