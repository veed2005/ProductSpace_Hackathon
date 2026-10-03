// Formline dashboard. Vanilla JS, no build step.
// Live updates: Server-Sent Events from /dashboard/events trigger a debounced refetch, and a slow
// poll covers events published from other processes (e.g. the in-process simulator).

const SOURCE_LABEL = {
  memory: "From memory", asked: "Newly asked", corrected: "Corrected",
  document: "From a letter", unknown: "Unknown", skipped: "Skipped",
};
const LANG = { en: "English", es: "Español" };
const POLL_MS = 4000;

const state = {
  people: [],
  phone: null,          // selected phone
  follow: true,         // jump to whoever is active
  messages: [],
  task: null,
  fieldSnapshot: {},    // field id -> value, to flash changes
  lastMessageId: 0,
  lastFocus: null,      // last field auto-scrolled to
  view: "form",         // "form" | "memory"
  viewPinned: false,    // viewer picked a tab; stop auto-switching
  profileId: null,      // profile shown in the memory view
  profile: null,
  docs: [],             // letters for the selected phone, newest first
  docId: null,          // letter shown in the letter view
  page: "live",         // "live" | "forms"
  forms: [],
  formId: null,         // form shown in the library
};

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Let long identifiers (applicant_name, employment.employer) wrap at _ and . instead of mid-word.
function escId(s) {
  return esc(s).replace(/([_.])/g, "$1<wbr>");
}

function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function clock(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
}

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

// ---------------------------------------------------------------- people

async function loadPeople() {
  state.people = await getJSON("/api/people");
  if (state.follow) {
    const live = state.people.find((p) => p.live) || state.people[0];
    if (live && live.phone !== state.phone) return select(live.phone, { keepFollow: true });
  } else if (!state.phone && state.people.length) {
    return select(state.people[0].phone, { keepFollow: true });
  }
  renderPeople();
}

function renderPeople() {
  const list = $("people-list");
  if (!state.people.length) {
    list.innerHTML = `<li class="empty">Nobody yet. Text the Formline number to begin.</li>`;
    return;
  }
  list.innerHTML = state.people.map((p) => `
    <li>
      <button class="person ${p.phone === state.phone ? "selected" : ""}" data-phone="${esc(p.phone)}">
        <span class="person-top">
          <span class="person-name">${esc(p.name)}</span>
          ${p.live ? `<span class="live-dot" title="Active in the last 5 minutes"></span>` : ""}
        </span>
        <span class="person-doing">${esc(p.doing)}</span>
        <span class="person-meta">
          <span class="tag">${esc(p.phone_masked)}</span>
          ${p.channel ? `<span class="tag">${p.channel === "voice" ? "Voice" : "SMS"}</span>` : ""}
          ${p.language ? `<span class="tag">${esc(LANG[p.language] || p.language)}</span>` : ""}
          ${p.profile_count > 1 ? `<span class="tag">${p.profile_count} people share this phone</span>` : ""}
          <span>${timeAgo(p.last_seen)}</span>
        </span>
      </button>
    </li>`).join("");
}

$("people-list").addEventListener("click", (e) => {
  const btn = e.target.closest(".person");
  if (!btn) return;
  state.follow = false;
  $("follow").checked = false;
  select(btn.dataset.phone);
});

$("follow").addEventListener("change", (e) => {
  state.follow = e.target.checked;
  if (state.follow) loadPeople();
});

async function select(phone, { keepFollow = false } = {}) {
  if (phone !== state.phone) {
    state.phone = phone;
    state.messages = [];
    state.task = null;
    state.fieldSnapshot = {};
    state.lastMessageId = 0;
    state.profileId = null;
    state.profile = null;
    state.docs = [];
    state.docId = null;
    state.docsSig = null;
  }
  if (!keepFollow) state.follow = $("follow").checked;
  renderPeople();
  await Promise.all([loadMessages(), loadTask(), loadProfile(), loadDocs()]);
}

// ---------------------------------------------------------------- transcript

async function loadMessages() {
  if (!state.phone) return;
  const phone = state.phone;
  const msgs = await getJSON(`/api/phones/${encodeURIComponent(phone)}/messages`);
  if (phone !== state.phone) return;
  const prevLast = state.lastMessageId;
  state.messages = msgs;
  state.lastMessageId = msgs.length ? msgs[msgs.length - 1].id : 0;
  renderMessages(prevLast);
}

function renderMessages(prevLast) {
  const person = state.people.find((p) => p.phone === state.phone);
  $("convo-title").textContent = person ? person.name : "Conversation";
  $("convo-meta").textContent = person ? person.phone_masked : "";
  const box = $("messages");
  if (!state.messages.length) {
    box.innerHTML = `<p class="empty">No messages yet.</p>`;
    return;
  }
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  box.innerHTML = state.messages.map((m) => `
    <div class="msg ${m.direction} ${m.channel} ${prevLast && m.id > prevLast ? "fresh" : ""}">
      ${m.text ? `<div class="bubble">${esc(m.text)}</div>` : ""}
      ${m.media.map((f) => `<div class="attach">Photo: ${esc(f)}</div>`).join("")}
      <div class="msg-meta">
        <span class="channel ${m.channel}">${m.channel === "voice" ? "Voice" : "SMS"}</span>
        <span>${m.direction === "in" ? "Person" : "Formline"} · ${clock(m.at)}</span>
      </div>
    </div>`).join("");
  if (nearBottom || !prevLast || state.messages[state.messages.length - 1].id > prevLast) {
    box.scrollTop = box.scrollHeight;
  }
}

// ---------------------------------------------------------------- form

async function loadTask() {
  if (!state.phone) return;
  const phone = state.phone;
  const task = await getJSON(`/api/phones/${encodeURIComponent(phone)}/task`);
  if (phone !== state.phone) return;
  const changed = new Set();
  if (task && state.task && task.id === state.task.id) {
    for (const f of task.fields) {
      if (f.answered && state.fieldSnapshot[f.id] !== `${f.value}|${f.source}`) changed.add(f.id);
    }
  }
  state.task = task;
  autoView();
  state.fieldSnapshot = Object.fromEntries((task?.fields || []).map((f) => [f.id, `${f.value}|${f.source}`]));
  renderTask(changed);
}

function renderTask(changed) {
  const t = state.task;
  const box = $("task");
  if (!t) {
    box.innerHTML = `<p class="empty">No form yet for this person.</p>`;
    return;
  }
  const answered = t.progress.answered, total = t.progress.total;
  const c = t.counts;
  const pct = (n) => (total ? (100 * n) / total : 0);
  const fromMemory = c.memory || 0;

  const verify = t.verification && Object.keys(t.verification).length ? `
    <div class="verify ${t.verification.ok ? "ok" : "bad"}">
      <div>
        <strong>${t.verification.ok ? "Verified" : "Needs attention"}</strong>
        <div>${t.verification.ok
          ? `Every answer re-read from the filled PDF${t.verification.fields_checked ? ` (${t.verification.fields_checked} fields)` : ""}.`
          : "The filled PDF didn't match what we meant to write."}</div>
        ${verifyProblems(t.verification)}
      </div>
      ${t.has_pdf ? `<a class="btn" href="/api/tasks/${t.id}/pdf" target="_blank" rel="noopener">Download PDF</a>` : ""}
    </div>` : "";

  const groups = [];
  for (const f of t.fields) {
    const g = f.group || "Questions";
    let entry = groups.find((x) => x.name === g);
    if (!entry) groups.push((entry = { name: g, fields: [] }));
    entry.fields.push(f);
  }

  box.innerHTML = `
    <div class="task-head">
      <h2 class="task-name">${esc(t.form_name)}</h2>
      <span class="status ${esc(t.status)}">${statusLabel(t.status)}</span>
    </div>
    <div class="stats">
      <div class="stat"><span class="stat-big">${fromMemory}</span><span class="stat-label">answers from memory</span></div>
      <div class="stat"><span class="stat-num">${answered} / ${total}</span><span class="stat-label">answered</span></div>
      <div class="stat"><span class="stat-num">${t.turn_count || 0}</span><span class="stat-label">turns</span></div>
      ${t.duration_s != null ? `<div class="stat"><span class="stat-num">${fmtDuration(t.duration_s)}</span><span class="stat-label">to complete</span></div>` : ""}
    </div>
    <div class="bar" aria-hidden="true">
      ${["memory", "document", "asked", "corrected", "unknown", "skipped"].map((k) =>
        c[k] ? `<span class="${k}" style="width:${pct(c[k])}%"></span>` : "").join("")}
    </div>
    ${verify}
    ${groups.map((g) => `
      <div class="group-title">${esc(g.name)}</div>
      <div class="fields">${g.fields.map((f) => fieldRow(f, changed.has(f.id))).join("")}</div>`).join("")}
  `;
  // Keep the action on screen: scroll to the field being asked, or the one that just changed,
  // only when that changes (so a viewer scrolling around isn't yanked back every poll).
  const focus = box.querySelector(".field.current") || box.querySelector(".field.flash");
  const key = focus && `${t.id}:${focus.dataset.id}`;
  if (focus && key !== state.lastFocus) focus.scrollIntoView({ block: "nearest", behavior: "smooth" });
  state.lastFocus = key || state.lastFocus;
}

function fieldRow(f, flash) {
  const cls = ["field"];
  if (!f.applies) cls.push("na");
  if (f.answered) cls.push(`src-${f.source}`); else cls.push("pending");
  if (f.current && !f.answered) cls.push("current");
  if (flash) cls.push("flash");
  const chip = f.answered
    ? `<span class="chip src-${esc(f.source)}">${esc(SOURCE_LABEL[f.source] || f.source)}</span>`
    : f.current ? `<span class="chip asking">Asking now</span>`
    : f.applies ? `<span class="chip src-pending">Not yet</span>` : `<span class="chip src-pending">Doesn't apply</span>`;
  return `
    <div class="${cls.join(" ")}" data-id="${esc(f.id)}">
      <span class="field-label">${esc(f.label)}${f.required ? "" : " <small>(optional)</small>"}</span>
      <span class="field-value">${f.answered ? esc(f.value || "—") : "…"}</span>
      ${chip}
    </div>`;
}

function verifyProblems(v) {
  const items = [
    ...(v.mismatches || []).map((m) => `${esc(m.pdf_field)}: expected “${esc(m.expected)}”, found “${esc(m.actual)}”`),
    ...(v.truncated || []).map((n) => `${esc(n)}: text doesn't fit the box`),
    ...(v.missing_required || []).map((n) => `${esc(n)}: required but empty`),
  ];
  return items.length ? `<ul>${items.map((i) => `<li>${i}</li>`).join("")}</ul>` : "";
}

function statusLabel(s) {
  return { active: "In progress", readback: "Reading back", completed: "Completed", needs_attention: "Needs attention", abandoned: "Stopped" }[s] || s;
}

function fmtDuration(s) {
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return m ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
}

// ---------------------------------------------------------------- memory (profile) view

function currentPerson() {
  return state.people.find((p) => p.phone === state.phone);
}

async function loadProfile() {
  const person = currentPerson();
  const ids = (person?.profiles || []).map((p) => p.id);
  if (!ids.includes(state.profileId)) state.profileId = person?.active_profile_id || ids[0] || null;
  renderProfileSwitch(person);
  if (!state.profileId) {
    state.profile = null;
  } else {
    const id = state.profileId;
    const profile = await getJSON(`/api/profiles/${id}`).catch(() => null);
    if (id !== state.profileId) return;
    state.profile = profile;
  }
  renderMemory();
}

function renderProfileSwitch(person) {
  const box = $("profile-switch");
  const profiles = person?.profiles || [];
  box.innerHTML = profiles.length > 1 ? profiles.map((p) =>
    `<button class="pill ${p.id === state.profileId ? "active" : ""}" data-profile="${p.id}">${esc(p.name)}</button>`).join("") : "";
}

$("profile-switch").addEventListener("click", (e) => {
  const pill = e.target.closest(".pill");
  if (!pill) return;
  state.profileId = Number(pill.dataset.profile);
  setView("memory", true);
  loadProfile();
});

function renderMemory() {
  const box = $("memory");
  const p = state.profile;
  if (!p) {
    box.innerHTML = `<p class="empty">Nothing saved yet. Formline builds a profile as the person answers questions.</p>`;
    return;
  }
  const c = p.counts;
  box.innerHTML = `
    <div class="mem-head">
      <div>
        <h2 class="mem-name">${esc(p.name)}</h2>
        <div class="mem-sub">
          <span>${esc(p.phone_masked)}</span>
          <span>${esc(LANG[p.language] || p.language)}</span>
          <span>${p.pin_set ? "PIN set" : "No PIN yet"}</span>
        </div>
      </div>
    </div>
    <div class="stats">
      <div class="stat"><span class="stat-big">${c.facts}</span><span class="stat-label">details remembered</span></div>
      <div class="stat"><span class="stat-num">${c.stale}</span><span class="stat-label">to re-check</span></div>
      <div class="stat"><span class="stat-num">${c.forms_completed}</span><span class="stat-label">forms done</span></div>
      <div class="stat"><span class="stat-num">${c.documents}</span><span class="stat-label">letters explained</span></div>
    </div>
    <div class="facts">${p.facts.map(factRow).join("") || `<p class="empty">No details saved yet.</p>`}</div>
    ${p.reminders.length ? `
      <div class="group-title">Upcoming reminders</div>
      <ul class="side-list">${p.reminders.map((r) =>
        `<li><span>${esc(r.message)}</span><time>${new Date(r.due_at).toLocaleDateString([], { month: "short", day: "numeric" })}</time></li>`).join("")}</ul>` : ""}
    ${p.activity.length ? `
      <div class="group-title">What Formline has done</div>
      <ul class="side-list">${p.activity.map((a) =>
        `<li><span>${esc(a.description)}</span><time>${timeAgo(a.at)}</time></li>`).join("")}</ul>` : ""}
  `;
}

function factRow(f) {
  const freshness = f.fresh
    ? `<span class="badge fresh">Fresh</span>`
    : `<span class="badge stale">Re-check next time</span>`;
  const left = f.fresh && f.stale_in_days != null && f.stale_in_days < 400 ? ` · fresh for ${f.stale_in_days} more days` : "";
  return `
    <div class="fact ${f.fresh ? "" : "stale"}">
      <span class="fact-label">${esc(f.label)}</span>
      <span class="fact-value">${f.lines.map((l) => `<div>${esc(l)}</div>`).join("")}</span>
      <span class="fact-meta">
        ${freshness}
        <span>${esc(f.source_label)} · confirmed ${timeAgo(f.confirmed_at)}${left}</span>
      </span>
    </div>`;
}

// Pick the most relevant tab unless the viewer chose one: a form in progress, else whatever happened
// most recently in the last 30 minutes (a letter explained or a form finished), else memory.
function autoView() {
  if (state.viewPinned) return;
  const t = state.task, doc = state.docs[0];
  const RECENT = 30 * 60 * 1000;
  const ms = (iso) => (iso ? new Date(iso).getTime() : 0);
  if (t && ["active", "readback"].includes(t.status)) return setView("form");
  const taskAt = t ? ms(t.completed_at || t.started_at) : 0;
  const docAt = doc ? ms(doc.created_at) : 0;
  const newest = Math.max(taskAt, docAt);
  if (Date.now() - newest > RECENT || !newest) return setView("memory");
  setView(docAt >= taskAt ? "letter" : "form");
}

function setView(view, pinned = false) {
  state.view = view;
  if (pinned) state.viewPinned = true;
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.view === view;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", String(on));
  });
  $("task").hidden = view !== "form";
  $("memory").hidden = view !== "memory";
  $("letter").hidden = view !== "letter";
}

document.querySelector(".tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) setView(tab.dataset.view, true);
});

// ---------------------------------------------------------------- letter (document) view

async function loadDocs() {
  if (!state.phone) return;
  const phone = state.phone;
  const docs = await getJSON(`/api/phones/${encodeURIComponent(phone)}/documents`).catch(() => []);
  if (phone !== state.phone) return;
  const newest = docs[0]?.id;
  if (!docs.some((d) => d.id === state.docId) || (newest && newest !== state.docs[0]?.id)) state.docId = newest || null;
  const sig = JSON.stringify(docs) + state.docId;
  state.docs = docs;
  if (sig !== state.docsSig) renderLetter();  // don't reload photos on every poll
  state.docsSig = sig;
  autoView();
}

function fmtDate(iso) {
  if (!iso) return "No date";
  const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

function daysUntil(iso) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  const days = Math.round((d - Date.now()) / 86400000);
  if (days < 0) return `${-days} days ago`;
  if (days === 0) return "today";
  return days === 1 ? "tomorrow" : `in ${days} days`;
}

function renderLetter() {
  const box = $("letter");
  const doc = state.docs.find((d) => d.id === state.docId);
  if (!doc) {
    box.innerHTML = `<p class="empty">No letters yet. When someone texts a photo of a letter, it shows up here with its explanation.</p>`;
    return;
  }
  const e = doc.explanation || {};
  const conf = Math.round(100 * (doc.confidence || 0));
  const picker = state.docs.length > 1 ? `<div class="doc-picker">${state.docs.map((d, i) =>
    `<button class="pill ${d.id === doc.id ? "active" : ""}" data-doc="${d.id}">${esc(d.document_type || "Letter")} · ${timeAgo(d.created_at)}</button>`).join("")}</div>` : "";
  const list = (items) => `<ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
  box.innerHTML = `
    ${picker}
    <div class="letter">
      <div class="letter-photos">
        ${doc.media.length ? doc.media.map((src, i) => `<img src="${src}" alt="Photo ${i + 1} of the letter" loading="lazy">`).join("")
          : `<div class="nophoto">No photo</div>`}
      </div>
      <div>
        <div class="doc-type">${esc(doc.document_type || "Document")}</div>
        ${e.sender ? `<div class="doc-sender">From ${esc(e.sender)}</div>` : ""}
        ${e.high_stakes ? `<div class="serious">This is serious. Formline recommended calling legal aid.</div>` : ""}
        ${e.unreadable_parts?.length ? `<div class="unclear">Hard to read: ${esc(e.unreadable_parts.join("; "))}</div>` : ""}
        <p class="doc-summary">${esc(e.plain_summary || "")}</p>
        ${e.action_required ? `<div class="doc-block action"><h3>What to do</h3>${esc(e.action_required)}</div>` : ""}
        ${e.deadlines?.length ? `<div class="doc-block deadline"><h3>Deadlines</h3>${e.deadlines.map((d) =>
          `<div class="deadline-row"><span>${esc(d.description)}</span><strong>${fmtDate(d.date)}${d.date ? ` · ${daysUntil(d.date)}` : ""}</strong></div>`).join("")}</div>` : ""}
        ${e.amounts?.length ? `<div class="doc-block"><h3>Amounts</h3>${list(e.amounts)}</div>` : ""}
        ${e.reference_numbers?.length ? `<div class="doc-block"><h3>Reference numbers</h3>${list(e.reference_numbers)}</div>` : ""}
        ${doc.related_form ? `<div class="doc-block"><h3>Related form</h3>${esc(doc.related_form.name)}${doc.form_started
          ? ` · <strong>started</strong> (${esc(statusLabel(doc.form_started.status))})` : " · offered"}</div>` : ""}
        <div class="confidence">Read confidence <span class="meter"><span style="width:${conf}%"></span></span> ${conf}%</div>
      </div>
    </div>`;
}

$("letter").addEventListener("click", (e) => {
  const pill = e.target.closest(".pill");
  if (pill) {
    state.docId = Number(pill.dataset.doc);
    setView("letter", true);
    renderLetter();
    return;
  }
  const img = e.target.closest(".letter-photos img");
  if (img) {
    const lb = document.createElement("div");
    lb.className = "lightbox";
    lb.innerHTML = `<img src="${img.src}" alt="${esc(img.alt)}">`;
    lb.addEventListener("click", () => lb.remove());
    document.body.appendChild(lb);
  }
});

// ---------------------------------------------------------------- pages: live / forms

function setPage(page) {
  state.page = page;
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $("page-live").hidden = page !== "live";
  $("page-forms").hidden = page !== "forms";
  document.querySelector(".legend").hidden = page !== "live";
  document.querySelector(".follow").hidden = page !== "live";
  if (page === "forms") loadForms();
}

document.querySelector(".nav").addEventListener("click", (e) => {
  const b = e.target.closest(".nav-btn");
  if (b) setPage(b.dataset.page);
});

// ---------------------------------------------------------------- form library

async function loadForms(selectId) {
  state.forms = await getJSON("/api/forms");
  if (selectId) state.formId = selectId;
  if (!state.forms.some((f) => f.form_id === state.formId)) state.formId = state.forms[0]?.form_id || null;
  renderFormsList();
  if (state.formId) await loadFormDetail(state.formId);
  else $("form-detail").innerHTML = `<p class="empty">No forms yet. Add one to get started.</p>`;
}

function renderFormsList() {
  $("forms-list").innerHTML = state.forms.map((f) => `
    <li>
      <button class="form-card ${f.form_id === state.formId ? "selected" : ""}" data-form="${esc(f.form_id)}">
        <span class="form-card-name">${esc(f.name)}</span>
        <span class="form-card-meta">
          <span class="badge ${f.reviewed ? "reviewed" : "draft"}">${f.reviewed ? "Reviewed" : "Draft, needs review"}</span>
          <span>${f.field_count} questions</span>
          <span>${f.memory_coverage}% fillable from memory</span>
        </span>
        ${f.agency ? `<span class="form-card-meta">${esc(f.agency)}</span>` : ""}
      </button>
    </li>`).join("");
}

$("forms-list").addEventListener("click", (e) => {
  const card = e.target.closest(".form-card");
  if (!card) return;
  state.formId = card.dataset.form;
  renderFormsList();
  loadFormDetail(state.formId);
});

async function loadFormDetail(formId) {
  const f = await getJSON(`/api/forms/${encodeURIComponent(formId)}`);
  if (formId !== state.formId) return;
  const typeLabel = { yes_no: "yes / no", ssn_last4: "SSN last 4", money: "money" };
  $("form-detail").innerHTML = `
    <div class="detail-head">
      <div>
        <h2 class="task-name">${esc(f.name)}</h2>
        <div class="mem-sub">${f.agency ? `<span>${esc(f.agency)}</span>` : ""}<span>${f.field_count} questions</span>
          <span>${f.required_count} required</span><span>${f.memory_mapped} fillable from memory</span></div>
      </div>
      <div class="detail-actions">
        ${f.has_pdf ? `<a class="btn secondary" href="/api/forms/${encodeURIComponent(f.form_id)}/pdf" target="_blank" rel="noopener">Open PDF</a>` : ""}
        <button class="btn ${f.reviewed ? "secondary" : "ok"}" id="review-btn" data-reviewed="${f.reviewed}">
          ${f.reviewed ? "Mark as draft" : "Mark reviewed"}</button>
      </div>
    </div>
    ${f.description ? `<p class="hint">${esc(f.description)}</p>` : ""}
    ${f.aliases.length ? `<div class="aliases">${f.aliases.map((a) => `<span class="tag">${esc(a)}</span>`).join("")}</div>` : ""}
    ${f.problems.length
      ? `<div class="problems"><strong>${f.problems.length} thing${f.problems.length === 1 ? "" : "s"} to fix before using this form</strong><ul>${f.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>`
      : `<div class="allgood">Every question maps to a real PDF field.</div>`}
    <table class="q-table">
      <thead><tr><th>Question Formline asks</th><th>Type</th><th class="hide-sm">Saved to memory as</th><th class="hide-sm">PDF field</th></tr></thead>
      <tbody>${f.fields.map((q) => `
        <tr>
          <td><div class="q">${esc(q.question_hint)}</div>
            <div class="sub">${esc(q.label)}${q.required ? " · required" : ""}${q.sensitive ? " · sensitive" : ""}${q.group ? ` · ${esc(q.group)}` : ""}${q.condition ? ` · only if ${esc(q.condition.field)} = ${esc(q.condition.equals)}` : ""}</div></td>
          <td>${esc(typeLabel[q.type] || q.type)}${q.options?.length ? `<div class="sub">${esc(q.options.join(", "))}</div>` : ""}</td>
          <td class="hide-sm">${q.profile_key ? `<code class="mem-key">${escId(q.profile_key)}</code>` : `<span class="sub">not saved</span>`}</td>
          <td class="hide-sm">${q.pdf_field ? `<code>${escId(q.pdf_field)}</code>` : `<span class="sub">none</span>`}</td>
        </tr>`).join("")}</tbody>
    </table>`;
}

$("form-detail").addEventListener("click", async (e) => {
  const btn = e.target.closest("#review-btn");
  if (!btn) return;
  btn.disabled = true;
  await fetch(`/api/forms/${encodeURIComponent(state.formId)}/review`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reviewed: btn.dataset.reviewed !== "true" }),
  });
  loadForms();
});

// ---------------------------------------------------------------- upload a new form

const dialog = $("upload-dialog");
$("add-form").addEventListener("click", () => {
  $("upload-form").reset();
  setUploadStatus("");
  $("upload-submit").disabled = false;
  dialog.showModal();
});
$("upload-cancel").addEventListener("click", () => dialog.close());

function setUploadStatus(text, kind = "") {
  const el = $("upload-status");
  el.textContent = text;
  el.className = `upload-status ${kind}`;
}

$("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = new FormData(e.target);
  $("upload-submit").disabled = true;
  const started = Date.now();
  const tick = setInterval(() => setUploadStatus(
    `Reading the PDF and writing questions… ${Math.round((Date.now() - started) / 1000)}s`, "working"), 500);
  try {
    const r = await fetch("/api/forms/upload", { method: "POST", body: data });
    const body = await r.json().catch(() => ({}));
    clearInterval(tick);
    if (!r.ok) {
      setUploadStatus(body.detail || `Upload failed (${r.status}).`, "error");
      $("upload-submit").disabled = false;
      return;
    }
    setUploadStatus(`Done in ${Math.round((Date.now() - started) / 1000)}s: ${body.field_count} questions.`, "working");
    setTimeout(() => dialog.close(), 600);
    loadForms(body.form_id);
  } catch (err) {
    clearInterval(tick);
    setUploadStatus(`Upload failed: ${err.message}`, "error");
    $("upload-submit").disabled = false;
  }
});

// ---------------------------------------------------------------- live updates

let pending = { people: false, messages: false, task: false, profile: false, docs: false };
let timer = null;

function refresh(what) {
  Object.assign(pending, what);
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const p = pending;
    pending = { people: false, messages: false, task: false, profile: false, docs: false };
    try {
      if (p.people) await loadPeople();
      if (p.messages) await loadMessages();
      if (p.task) await loadTask();
      if (p.profile) await loadProfile();
      if (p.docs) await loadDocs();
    } catch (err) {
      console.warn(err);
    }
  }, 150);
}

function connect() {
  const es = new EventSource("/dashboard/events");
  const conn = $("conn");
  es.onopen = () => { conn.className = "conn live"; $("conn-text").textContent = "Live"; };
  es.onerror = () => { conn.className = "conn down"; $("conn-text").textContent = "Reconnecting…"; };
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    switch (ev.type) {
      case "message":
        if (state.follow && ev.phone && ev.phone !== state.phone) {
          state.phone = null;  // loadPeople will select the live phone
          refresh({ people: true });
        } else {
          refresh({ people: true, messages: ev.phone === state.phone, task: ev.phone === state.phone });
        }
        break;
      case "field_filled":
      case "task_updated":
      case "verification":
        refresh({ task: true, people: true });
        break;
      case "document_explained":
        refresh({ docs: true, people: true, profile: true });
        break;
      case "profile_updated":
      case "profile_deleted":
      case "activity":
        refresh({ people: true, profile: true });
        break;
      default:
        refresh({ people: true });
    }
  };
}

setInterval(() => {
  if (state.page === "live") refresh({ people: true, messages: true, task: true, profile: true, docs: true });
}, POLL_MS);
setInterval(renderPeople, 30000);  // keep "x min ago" current
connect();
loadPeople().catch(console.warn);
