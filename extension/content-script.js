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
        } else if (msg.action === "read_pdf") {
          sendResponse(await readPdf());
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

  // The PDF this tab shows. Chrome's PDF viewer is sealed off from extensions, so the file itself is read:
  // a copy is kept in this tab's memory the moment the PDF opens, while its link is still valid (document
  // portals and S3 hand out links that expire within minutes, often marked no-store so Chrome doesn't cache
  // them). The copy stays in this tab; it's sent to Formline only when the person asks about the document.
  // Fetched from the page itself, so a PDF behind a login comes with the person's own session.
  const MAX_PDF_BYTES = 10 * 1024 * 1024;  // base64 must fit in the server's 16 MiB websocket message
  let pdfCopy = null;  // Promise of {ok, data | error, detail}

  async function fetchPdf() {
    let res;
    try {
      res = await fetch(location.href, { credentials: "include", cache: "force-cache" });
    } catch (e) {
      return { ok: false, error: "failed", detail: "The PDF couldn't be downloaded (" + e.message + ")." };
    }
    if (!res.ok) {
      const expired = res.status === 401 || res.status === 403 || res.status === 410;
      return { ok: false, error: expired ? "expired" : "failed",
        detail: expired ? "The PDF's link has expired (HTTP " + res.status + ")." : "The PDF couldn't be downloaded (HTTP " + res.status + ")." };
    }
    const blob = await res.blob();
    if (blob.size > MAX_PDF_BYTES) return { ok: false, error: "too_large", detail: "The PDF is over 10 MB." };
    const dataUrl = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = () => reject(r.error);
      r.readAsDataURL(blob);
    });
    return { ok: true, data: { pdf_base64: String(dataUrl).split(",", 2)[1], size: blob.size, url: location.href } };
  }

  async function readPdf() {
    if (document.contentType !== "application/pdf") return { ok: false, error: "invalid_action", detail: "This tab isn't a PDF." };
    if (location.protocol === "file:") return { ok: false, error: "needs_worker", detail: "local file" };
    const kept = pdfCopy && (await pdfCopy);
    if (kept && kept.ok) return kept;
    return fetchPdf();  // no copy (or it failed): try again, in case the link still works
  }

  if (document.contentType === "application/pdf" && location.protocol !== "file:") {
    pdfCopy = fetchPdf();  // right away, while the link is fresh
  }

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
