// Formline service worker: keeps the backend connection, tracks which tab the person is looking at,
// and relays commands to the content script in that tab.
import { Connection } from "./connection.js";

const DEFAULT_SERVER = "http://localhost:8000";
const CONTENT_FILES = ["page-observer.js", "executor.js", "content-script.js"];

let serverUrl = DEFAULT_SERVER;
let lastTabKey = "";
let lastEligibleTabId = null;
const childTabs = new Map(); // opener tab id -> newest tab it opened (links with target=_blank)

const conn = new Connection({ onMessage, onStatus });

function err(code, detail) {
  const e = new Error(detail);
  e.code = code;
  return e;
}

// ---------------------------------------------------------------- config and status

async function loadConfig() {
  const cfg = await chrome.storage.local.get(["serverUrl", "installationId", "token"]);
  serverUrl = cfg.serverUrl || DEFAULT_SERVER;
  conn.helloTab = await currentTabInfo();
  conn.configure({ serverUrl, installationId: cfg.installationId, token: cfg.token });
}

function onStatus(status) {
  const badge = { connected: ["ON", "#16a34a"], connecting: ["…", "#ca8a04"], disconnected: ["OFF", "#dc2626"],
    unauthorized: ["!", "#dc2626"], unpaired: ["", "#64748b"] }[status] || ["", "#64748b"];
  chrome.action.setBadgeText({ text: badge[0] });
  chrome.action.setBadgeBackgroundColor({ color: badge[1] });
  chrome.storage.session?.set({ status }).catch(() => {});
  if (status === "connected") reportTab(true);
}

chrome.runtime.onInstalled.addListener(loadConfig);
chrome.runtime.onStartup.addListener(loadConfig);
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes.installationId || changes.token || changes.serverUrl)) loadConfig();
});
// Alarms wake the worker if Chrome suspended it, so the connection comes back on its own.
chrome.alarms.create("formline-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => {
  if (!conn.cfg) loadConfig();
  else conn.open();
});
loadConfig();

// ---------------------------------------------------------------- which tab is "the page"

function eligible(tab) {
  if (!tab || !tab.url || !/^https?:/.test(tab.url)) return false;
  try {
    const u = new URL(tab.url);
    const s = new URL(serverUrl);
    if (u.origin === s.origin && u.pathname.startsWith("/dashboard")) return false; // Formline's own dashboard
  } catch (e) { return false; }
  return true;
}

async function activeTab() {
  const [focused] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (eligible(focused)) return focused;
  if (lastEligibleTabId != null) {
    try {
      const t = await chrome.tabs.get(lastEligibleTabId);
      if (eligible(t)) return t;
    } catch (e) { /* closed */ }
  }
  const actives = await chrome.tabs.query({ active: true });
  return actives.find(eligible) || null;
}

async function currentTabInfo() {
  const t = await activeTab();
  if (!t) return null;
  lastEligibleTabId = t.id;
  return { tab_id: t.id, url: t.url, title: t.title || "" };
}

let reportTimer = null;
function reportTab(force = false) {
  clearTimeout(reportTimer);
  reportTimer = setTimeout(async () => {
    const tab = await currentTabInfo();
    const key = JSON.stringify(tab);
    if (force || key !== lastTabKey) {
      lastTabKey = key;
      conn.send({ type: "tab", tab });
    }
  }, 250);
}

chrome.tabs.onActivated.addListener(() => reportTab());
chrome.tabs.onUpdated.addListener((_id, info) => {
  if (info.status === "complete" || info.title || info.url) reportTab();
});
chrome.tabs.onRemoved.addListener(() => reportTab());
chrome.windows.onFocusChanged.addListener(() => reportTab());
chrome.tabs.onCreated.addListener((tab) => {
  if (tab.openerTabId != null) childTabs.set(tab.openerTabId, { id: tab.id, at: Date.now() });
});

// ---------------------------------------------------------------- messages from content scripts and popup

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return undefined;
  if (msg.kind === "formline_page_changed" && sender.tab) {
    conn.send({ type: "page_changed", tab_id: sender.tab.id, url: msg.url, title: msg.title });
    return undefined;
  }
  if (msg.kind === "formline_status") {
    currentTabInfo().then((tab) => sendResponse({ status: conn.status, tab, serverUrl }));
    return true;
  }
  if (msg.kind === "formline_reconnect") {
    loadConfig().then(() => sendResponse({ ok: true }));
    return true;
  }
  return undefined;
});

// ---------------------------------------------------------------- commands from the backend

function onMessage(msg) {
  if (msg.type === "command") handleCommand(msg);
}

async function handleCommand(msg) {
  const { id, action, args = {}, tab_id } = msg;
  try {
    const data = await run(action, args, tab_id);
    conn.send({ type: "result", id, ok: true, data });
  } catch (e) {
    conn.send({ type: "result", id, ok: false, error: e.code || "failed", detail: String(e.message || e) });
  }
}

async function resolveTab(tabId) {
  if (tabId != null) {
    try {
      return await chrome.tabs.get(tabId);
    } catch (e) {
      throw err("no_tab", "That tab was closed.");
    }
  }
  const t = await activeTab();
  if (!t) throw err("no_tab", "No web page is open in the browser.");
  return t;
}

async function waitForLoad(tabId, timeoutMs = 10000) {
  const start = Date.now();
  await new Promise((r) => setTimeout(r, 150));
  while (Date.now() - start < timeoutMs) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (t.status === "complete") return t;
    } catch (e) {
      throw err("no_tab", "The tab closed.");
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  return chrome.tabs.get(tabId);
}

const NAVIGATED = /message port closed|back\/forward cache|Receiving end does not exist|context invalidated/i;

async function sendToTab(tabId, payload, { inject = true } = {}) {
  try {
    const res = await chrome.tabs.sendMessage(tabId, payload);
    if (res === undefined) throw new Error("Receiving end does not exist");
    return res;
  } catch (e) {
    const message = String(e.message || e);
    if (/Receiving end does not exist/i.test(message) && inject) {
      // Tab opened before the extension loaded (or a restricted reload): inject and retry once.
      try {
        await chrome.scripting.executeScript({ target: { tabId }, files: CONTENT_FILES });
      } catch (injectErr) {
        throw err("unsupported_page", "Formline can't read this kind of page (" + String(injectErr.message || injectErr) + ").");
      }
      return sendToTab(tabId, payload, { inject: false });
    }
    if (NAVIGATED.test(message)) return { navigated: true };
    throw err("failed", message);
  }
}

async function run(action, args, tabId) {
  let tab = await resolveTab(tabId);
  if (!eligible(tab)) throw err("unsupported_page", "Formline only works on regular web pages.");
  const urlBefore = tab.url;

  if (action === "get_page_state") {
    if (tab.status === "loading") tab = await waitForLoad(tab.id);
    const res = await sendToTab(tab.id, { kind: "formline", action });
    if (res.navigated) {
      tab = await waitForLoad(tab.id);
      return run(action, args, tab.id);
    }
    if (!res.ok) throw err(res.error, res.detail);
    return { ...res.data, tab_id: tab.id };
  }

  if (action === "overlay") {
    await sendToTab(tab.id, { kind: "formline", action, args }).catch(() => {});
    return {};
  }

  if (action === "go_back") {
    try {
      await chrome.tabs.goBack(tab.id);
    } catch (e) {
      throw err("invalid_action", "There's no previous page in this tab.");
    }
    const after = await waitForLoad(tab.id);
    return { success: true, action, url_before: urlBefore, url_after: after.url, page_changed: true };
  }

  if (action === "navigate") {
    // Same site only: the agent may not wander to other websites.
    let target;
    try {
      target = new URL(args.value, tab.url);
    } catch (e) {
      throw err("invalid_action", "That isn't a valid address.");
    }
    if (target.origin !== new URL(tab.url).origin) throw err("blocked", "Formline only navigates within the same website.");
    await chrome.tabs.update(tab.id, { url: target.toString() });
    const after = await waitForLoad(tab.id);
    return { success: true, action, url_before: urlBefore, url_after: after.url, page_changed: true };
  }

  const startedAt = Date.now();
  const res = await sendToTab(tab.id, { kind: "formline", action, args });
  let data;
  if (res.navigated) {
    data = { success: true, action, url_before: urlBefore, page_changed: true, navigating: true };
  } else if (!res.ok) {
    throw err(res.error, res.detail);
  } else {
    data = res.data;
  }
  let finalTab = tab;
  if (data.navigating) finalTab = await waitForLoad(tab.id);
  // A link that opened a new tab: follow it, so the person sees and we act on the same page.
  const child = childTabs.get(tab.id);
  if (child && child.at >= startedAt) {
    childTabs.delete(tab.id);
    try {
      await chrome.tabs.update(child.id, { active: true });
      finalTab = await waitForLoad(child.id);
      data.new_tab_id = child.id;
      data.page_changed = true;
    } catch (e) { /* closed already */ }
  }
  // The page's own location is freshest for in-page (hash/pushState) changes; after a full load or a
  // new tab, the tab's URL is.
  if (data.navigating || finalTab !== tab) data.url_after = finalTab.url || data.url_after;
  delete data.navigating;
  return data;
}
