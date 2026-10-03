// Formline content script: the bridge between the service worker and this page.
// Answers get_page_state and action requests, shows the "Formline is helping" pill, and tells the
// service worker when the page changes so the backend doesn't reason about a stale snapshot.
(() => {
  if (globalThis.__formlineContent) return;
  globalThis.__formlineContent = true;

  async function waitForBody() {
    for (let i = 0; i < 40 && !document.body; i++) await new Promise((r) => setTimeout(r, 50));
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || msg.kind !== "formline") return undefined;
    (async () => {
      try {
        if (msg.action === "get_page_state") {
          await waitForBody();
          sendResponse({ ok: true, data: FormlineObserver.snapshot() });
        } else if (msg.action === "overlay") {
          FormlineExecutor.showOverlay(msg.args && msg.args.text);
          sendResponse({ ok: true, data: {} });
        } else {
          sendResponse({ ok: true, data: await FormlineExecutor.run(msg.action, msg.args || {}) });
        }
      } catch (e) {
        sendResponse({ ok: false, error: e.code || "failed", detail: String((e && e.message) || e) });
      }
    })();
    return true; // async response
  });

  // Tell the service worker about meaningful page changes, at most every 1.5 s.
  let timer = null;
  let lastSent = 0;
  function notify() {
    timer = null;
    lastSent = Date.now();
    try {
      chrome.runtime.sendMessage({ kind: "formline_page_changed", url: location.href, title: document.title });
    } catch (e) { /* extension reloaded; this page's script is orphaned */ }
  }
  function schedule() {
    if (timer) return;
    timer = setTimeout(notify, Math.max(700, 1500 - (Date.now() - lastSent)));
  }
  new MutationObserver((muts) => {
    if (muts.some((m) => !(m.target instanceof Element && m.target.closest("[data-formline-ui]")))) schedule();
  }).observe(document.documentElement, {
    childList: true, subtree: true, attributes: true,
    attributeFilter: ["class", "hidden", "disabled", "aria-hidden", "aria-expanded", "aria-checked", "aria-selected", "open"],
  });
  addEventListener("hashchange", schedule);
  addEventListener("popstate", schedule);
})();
