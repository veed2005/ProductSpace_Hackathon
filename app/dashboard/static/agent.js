// Agent page: paired browsers, the live browser task, its action feed, the call, and what the agent sees.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const st = { taskId: null, pinned: false, browsers: [], tasks: [], detail: null, seeOpen: false, seeFor: null };

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(url + " " + r.status);
    return r.json();
  }

  const STATUS = {
    active: ["Working…", "working"], waiting_input: ["Waiting for the caller", "waiting"],
    waiting_confirmation: ["Waiting for confirmation", "confirm"], completed: ["Done · verified on the page", "done"],
    stopped: ["Stopped by the caller", "stopped"], interrupted: ["Call ended", "stopped"], failed: ["Failed", "bad"],
  };

  function q(s) { return "“" + esc(s) + "”"; }

  function actionRow(a) {
    const L = a.element_label, V = a.value;
    const rows = {
      search: ["🔍", `Searched the site for ${q(V)}<div class="why">${esc(L)}</div>`],
      click: ["✓", `Clicked ${q(L)}`], type: ["✓", `Typed ${q(V)} into ${q(L)}`],
      select: ["✓", `Chose ${q(V)} in ${q(L)}`], check: ["✓", `Checked ${q(L)}`], uncheck: ["✓", `Unchecked ${q(L)}`],
      clear: ["✓", `Cleared ${q(L)}`], press_enter: ["✓", `Pressed Enter in ${q(L)}`],
      scroll: ["✓", `Scrolled ${esc(V || "down")}`], go_back: ["✓", "Went back a page"],
      navigate: ["✓", `Opened ${esc(V)}`], focus: ["✓", `Focused ${q(L)}`],
      ask: ["?", `Asked the caller: ${q(L)}`], confirm_request: ["⏸", `Asked to confirm: ${q(V)}`],
      answer: ["💬", `Answered: ${q(L)}` + (V ? `<div class="why">From the page: ${q(V)}</div>` : "")],
      confirmed: ["✔", `Caller said yes to ${q(L)}`], declined: ["✋", `Not done: ${esc(a.reason || "caller declined")}`],
      confirm_stale: ["↺", "Page changed after the yes, so nothing was pressed"],
      verified: ["🏁", `Success verified on the page: ${q(L)}`],
      unverified: ["✗", `Claimed done without proof on the page`], rejected: ["✗", `Refused an invalid step`],
      autofill_offer: ["?", `Asked to fill in from saved details: ${esc(L)}`],
      autofill_accepted: ["✔", `Caller said yes to filling in saved details: ${esc(L)}`],
      autofill_declined: ["✋", `Saved details not used: ${esc(L)}`],
      blocked: ["⚠", `Needs the person at the computer: ${q(L)}`], stopped: ["■", "Stopped by the caller"],
    };
    let [icon, text] = rows[a.kind] || ["•", esc(a.kind)];
    let cls = { "💬": "ask", "?": "ask", "⏸": "confirm", "✔": "ok", "🏁": "done", "✗": "bad", "⚠": "bad", "✋": "stopped", "■": "stopped", "↺": "bad" }[icon] || "ok";
    if (!a.ok && icon === "✓") { icon = "✗"; cls = "bad"; text += ` <span class="err">failed: ${esc(a.error)}</span>`; }
    else if (a.error) text += ` <span class="err">${esc(a.error)}</span>`;
    const ms = a.latency_ms != null ? `<span class="ms">${a.latency_ms} ms</span>` : "";
    const why = a.reason && !["ask", "declined", "answer"].includes(a.kind) ? `<div class="why">${esc(a.reason)}</div>` : "";
    return `<li class="act ${cls}"><span class="icon">${icon}</span><div class="what">${text}${why}</div>${ms}</li>`;
  }

  function elapsed(t) {
    const end = t.completed_at ? new Date(t.completed_at) : new Date();
    const s = Math.max(0, Math.round((end - new Date(t.started_at)) / 1000));
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
  }

  function renderBrowsers() {
    const list = st.browsers.map((b) => `
      <li class="browser ${b.connected ? "on" : "off"}">
        <div class="b-top"><span class="dot"></span><b>${esc(b.profile_name)}</b><span class="muted">${esc(b.phone_masked)}</span></div>
        <div class="b-state">${b.connected ? "CONNECTED" : "Not connected"} · ${esc(b.label || "Chrome")}</div>
        ${b.connected && b.tab && b.tab.title ? `<div class="b-tab" title="${esc(b.tab.url)}">${esc(b.tab.title)}</div>` : ""}
      </li>`).join("");
    $("agent-browsers").innerHTML = list || `<li class="empty">No paired browsers yet. Install the extension and pair it with a phone number.</li>`;
    $("agent-tasks").innerHTML = st.tasks.map((t) => `
      <li><button class="task-pick ${t.id === st.taskId ? "active" : ""}" data-task="${t.id}">
        <span class="t-goal">${esc(t.goal)}</span>
        <span class="t-meta">${esc((STATUS[t.status] || [t.status])[0])} · ${elapsed(t)}</span></button></li>`).join("") ||
      `<li class="empty">No browser tasks yet. Call Formline from a paired phone.</li>`;
  }

  function renderTask() {
    const d = st.detail;
    if (!d) {
      $("agent-task").innerHTML = `<p class="empty">When someone calls, their goal and every browser step appear here live.</p>`;
      $("agent-convo").innerHTML = "";
      return;
    }
    const t = d.task;
    const [label, cls] = STATUS[t.status] || [t.status, "working"];
    const pending = t.status === "waiting_confirmation" && t.pending && t.pending.say
      ? `<div class="pending"><div class="p-head">⏸ Waiting for the caller's OK before pressing ${q(t.pending.label)}</div><div class="p-say">${q(t.pending.say)}</div></div>` : "";
    const result = t.status === "completed" && t.result ? `<div class="result">🏁 ${esc(t.result)}</div>` : "";
    $("agent-task").innerHTML = `
      <div class="goal-head">
        <div class="goal">${q(t.goal)}</div>
        <span class="status ${cls}">${esc(label)}</span>
      </div>
      <div class="facts">
        <span><b>${esc(d.caller.name || "Caller")}</b> ${esc(d.caller.phone_masked)} · ${esc(t.channel)}</span>
        <span>${esc(t.site_name || "")}</span>
        <span>${t.steps} browser steps · ${t.model_calls} model calls · ${elapsed(t)}</span>
      </div>
      ${pending}${result}
      <ol class="feed">${d.actions.map(actionRow).join("") || `<li class="empty">Starting…</li>`}</ol>`;
    const feed = $("agent-task").querySelector(".feed");
    feed.scrollTop = feed.scrollHeight;
    $("agent-convo").innerHTML = d.messages.map((m) => `
      <div class="msg ${m.direction === "in" ? "in" : "out"}"><span class="who">${m.direction === "in" ? "Caller" : "Formline"} · ${esc(m.channel)}</span>${esc(m.text)}</div>`).join("") ||
      `<p class="empty">No conversation yet.</p>`;
    $("agent-convo").scrollTop = $("agent-convo").scrollHeight;
  }

  async function loadSee() {
    const b = st.browsers.find((x) => x.connected && (!st.detail || x.installation_id === st.detail.task.installation_id)) ||
      st.browsers.find((x) => x.connected);
    if (!b) { $("agent-see-body").textContent = "No browser connected."; return; }
    try {
      const p = await getJSON(`/api/agent/browsers/${encodeURIComponent(b.installation_id)}/page`);
      $("agent-see-meta").textContent = `${p.site} · ${p.controls} controls, ${p.items} items`;
      $("agent-see-body").textContent = p.text;
    } catch (e) {
      $("agent-see-body").textContent = "Can't read the page right now.";
    }
  }

  async function refresh() {
    if ($("page-agent").hidden) return;
    try {
      [st.browsers, st.tasks] = await Promise.all([getJSON("/api/agent/browsers"), getJSON("/api/agent/tasks")]);
      if (!st.pinned || !st.tasks.some((t) => t.id === st.taskId)) st.taskId = st.tasks[0] ? st.tasks[0].id : null;
      st.detail = st.taskId ? await getJSON(`/api/agent/tasks/${st.taskId}`) : null;
      renderBrowsers();
      renderTask();
      if (st.seeOpen) loadSee();
    } catch (e) {
      console.warn(e);
    }
  }

  let timer = null;
  function soon() {
    clearTimeout(timer);
    timer = setTimeout(refresh, 120);
  }

  document.addEventListener("click", (e) => {
    const pick = e.target.closest(".task-pick");
    if (pick) {
      st.taskId = +pick.dataset.task;
      st.pinned = st.taskId !== (st.tasks[0] && st.tasks[0].id);
      refresh();
    }
  });
  document.addEventListener("toggle", (e) => {
    if (e.target.id === "agent-see") {
      st.seeOpen = e.target.open;
      if (st.seeOpen) loadSee();
    }
  }, true);

  setInterval(() => { if (!$("page-agent").hidden) refresh(); }, 3000);
  window.FormlineAgent = { refresh: soon, show: refresh };
})();
