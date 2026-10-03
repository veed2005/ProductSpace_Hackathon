// Formline executor: performs one constrained action on an element from the current snapshot.
//
// There is no "run this script" action. Every action names an element id from FormlineObserver, and
// is checked here before it happens: the id must belong to this document and still be on the page,
// the element must be visible and enabled, and the action must fit the element (you can't type into
// a checkbox or a password field). After acting, it waits for the page to settle and reports what
// changed, so the backend judges success from the page, not from the model's say-so.
(() => {
  if (globalThis.FormlineExecutor) return;
  const Obs = globalThis.FormlineObserver;

  class ActionError extends Error {
    constructor(code, detail) {
      super(detail);
      this.code = code;
    }
  }

  const TEXT_ROLES = new Set(["textbox", "searchbox", "combobox", "spinbutton"]);
  const TOGGLE_ROLES = new Set(["checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"]);

  let navigating = false;
  addEventListener("pagehide", () => { navigating = true; });
  addEventListener("beforeunload", () => { navigating = true; });
  if (globalThis.navigation) {
    navigation.addEventListener("navigate", (e) => {
      if (!e.destination.sameDocument) navigating = true;
    });
  }

  function frame() {
    return new Promise((r) => requestAnimationFrame(() => r()));
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  // Wait until the DOM has been quiet for 300 ms (at least 150 ms, at most 2.5 s), or a navigation starts.
  async function settle(counter) {
    const start = performance.now();
    let last = counter.count;
    let quietSince = performance.now();
    while (performance.now() - start < 2500) {
      await sleep(50);
      if (navigating) return;
      if (counter.count !== last) {
        last = counter.count;
        quietSince = performance.now();
      }
      if (performance.now() - start >= 150 && performance.now() - quietSince >= 300) return;
    }
  }

  function watchMutations() {
    const counter = { count: 0 };
    const mo = new MutationObserver((muts) => {
      for (const m of muts) {
        if (!(m.target instanceof Element && m.target.closest("[data-formline-ui]"))) counter.count++;
      }
    });
    mo.observe(document.documentElement, { childList: true, subtree: true, attributes: true, characterData: true });
    counter.stop = () => mo.disconnect();
    return counter;
  }

  function isVisible(el) {
    if (el.checkVisibility) return el.checkVisibility({ checkVisibilityCSS: true });
    const cs = getComputedStyle(el);
    return cs.display !== "none" && cs.visibility !== "hidden";
  }

  function isEnabled(el) {
    return !(el.disabled || el.getAttribute("aria-disabled") === "true" || el.closest("fieldset[disabled]"));
  }

  // Custom-styled checkboxes and radios often hide the real input and show its <label>.
  function clickTarget(el) {
    const r = el.getBoundingClientRect();
    if ((r.width < 2 || r.height < 2 || !isVisible(el)) && el.labels && el.labels.length) return el.labels[0];
    return el;
  }

  function highlight(el) {
    try {
      const r = el.getBoundingClientRect();
      const box = document.createElement("div");
      box.setAttribute("data-formline-ui", "");
      Object.assign(box.style, {
        position: "fixed", left: r.left - 4 + "px", top: r.top - 4 + "px", width: r.width + 8 + "px",
        height: r.height + 8 + "px", border: "3px solid #2563eb", borderRadius: "8px", zIndex: 2147483647,
        pointerEvents: "none", boxShadow: "0 0 0 6px rgba(37,99,235,.25)", transition: "opacity .4s",
      });
      document.documentElement.appendChild(box);
      setTimeout(() => { box.style.opacity = "0"; }, 700);
      setTimeout(() => box.remove(), 1200);
    } catch (e) { /* cosmetic only */ }
  }

  function interceptedBy(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return null;
    const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1);
    const y = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
    const top = document.elementFromPoint(x, y);
    if (!top || top === el || el.contains(top) || top.contains(el) || top.closest("[data-formline-ui]")) return null;
    if (el.labels && Array.from(el.labels).some((l) => l.contains(top))) return null;
    return top;
  }

  function realClick(el) {
    const opts = { bubbles: true, cancelable: true, view: window, button: 0 };
    el.dispatchEvent(new PointerEvent("pointerdown", { ...opts, pointerType: "mouse" }));
    el.dispatchEvent(new MouseEvent("mousedown", opts));
    el.focus?.({ preventScroll: true });
    el.dispatchEvent(new PointerEvent("pointerup", { ...opts, pointerType: "mouse" }));
    el.dispatchEvent(new MouseEvent("mouseup", opts));
    el.click();
  }

  function setValue(el, value) {
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return el.value;
    }
    // contenteditable
    el.focus();
    document.execCommand("selectAll", false);
    document.execCommand(value ? "insertText" : "delete", false, value);
    return (el.innerText || "").trim();
  }

  function norm(s) {
    return (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  }

  function chooseOption(select, wanted) {
    const w = norm(wanted);
    const opts = Array.from(select.options).filter((o) => !o.disabled);
    return opts.find((o) => norm(o.text) === w || norm(o.value) === w) ||
      opts.find((o) => norm(o.text).startsWith(w)) ||
      opts.find((o) => norm(o.text).includes(w)) || null;
  }

  async function run(action, args) {
    navigating = false;
    const urlBefore = location.href;

    if (action === "scroll") {
      const dir = (args.value || "down").toLowerCase();
      const se = document.scrollingElement || document.documentElement;
      if (dir === "top") se.scrollTo({ top: 0 });
      else if (dir === "bottom") se.scrollTo({ top: se.scrollHeight });
      else window.scrollBy({ top: (dir === "up" ? -1 : 1) * innerHeight * 0.8 });
      await sleep(250);
      return { success: true, action, url_before: urlBefore, url_after: location.href, page_changed: true };
    }

    if (args.doc_id && args.doc_id !== Obs.DOC_ID) {
      throw new ActionError("stale_element", "That page is gone; take a fresh look at the current page.");
    }
    const { el, reason } = Obs.lookup(args.element_id || "");
    if (!el) {
      throw new ActionError(reason, reason === "stale_element"
        ? "That element is no longer on the page." : "There is no element with that id on this page.");
    }
    const role = Obs.roleOf(el) || "button";
    if (!isVisible(el) && !(TOGGLE_ROLES.has(role) && el.labels && el.labels.length)) {
      throw new ActionError("not_visible", "That element is hidden.");
    }
    if (action !== "focus" && !isEnabled(el)) throw new ActionError("disabled", "That control is disabled.");

    const counter = watchMutations();
    try {
      el.scrollIntoView({ block: "center", inline: "center" });
      await frame();
      highlight(el);
      let value = null;

      if (action === "click") {
        const target = clickTarget(el);
        const blocker = interceptedBy(target);
        if (blocker) {
          throw new ActionError("intercepted", "Something is covering it: " + (blocker.innerText || blocker.tagName).slice(0, 80));
        }
        realClick(target);
      } else if (action === "type" || action === "clear") {
        if (role === "password" || el.type === "password" || Obs.isSensitiveField(el, Obs.nameOf(el, role))) {
          throw new ActionError("blocked", "Formline never types into password or other secret fields.");
        }
        const editable = el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && TEXT_ROLES.has(role)) || el.isContentEditable;
        if (!editable) throw new ActionError("invalid_action", "That isn't a text field.");
        if (el.readOnly) throw new ActionError("disabled", "That field is read-only.");
        el.focus();
        const wanted = action === "clear" ? "" : String(args.value ?? "");
        value = setValue(el, wanted);
        if (norm(value) !== norm(wanted)) {
          await settle(counter);
          return { success: false, action, error: "value_mismatch", detail: "The field kept only: " + String(value).slice(0, 80),
            url_before: urlBefore, url_after: location.href, page_changed: counter.count > 0 };
        }
      } else if (action === "select") {
        if (el.tagName !== "SELECT") {
          throw new ActionError("invalid_action", "That isn't a dropdown list; click it to open, then click the option.");
        }
        const opt = chooseOption(el, args.value);
        if (!opt) {
          const options = Array.from(el.options).map((o) => o.text.trim()).slice(0, 15).join("; ");
          throw new ActionError("not_found", "No option matches. Options: " + options);
        }
        el.value = opt.value;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        value = opt.text.trim();
      } else if (action === "check" || action === "uncheck") {
        if (!TOGGLE_ROLES.has(role)) throw new ActionError("invalid_action", "That isn't a checkbox or option button.");
        const want = action === "check";
        const current = () => (el.tagName === "INPUT" ? el.checked : el.getAttribute("aria-checked") === "true");
        if (current() !== want) realClick(clickTarget(el));
        await settle(counter);
        if (current() !== want) {
          return { success: false, action, error: "value_mismatch", detail: "It didn't change.",
            url_before: urlBefore, url_after: location.href, page_changed: counter.count > 0 };
        }
      } else if (action === "press_enter") {
        el.focus();
        const opts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
        const down = el.dispatchEvent(new KeyboardEvent("keydown", opts));
        el.dispatchEvent(new KeyboardEvent("keypress", opts));
        el.dispatchEvent(new KeyboardEvent("keyup", opts));
        if (down && el.form) el.form.requestSubmit();
      } else if (action === "focus") {
        el.focus();
      } else {
        throw new ActionError("invalid_action", "Unknown action: " + action);
      }

      await settle(counter);
      return {
        success: true, action, url_before: urlBefore, url_after: location.href,
        page_changed: navigating || counter.count > 0 || location.href !== urlBefore,
        navigating, value,
      };
    } finally {
      counter.stop();
    }
  }

  // A small pill on the page while Formline is working, so the person at the computer knows.
  let overlay = null;
  function showOverlay(text) {
    if (!text) {
      overlay?.remove();
      overlay = null;
      return;
    }
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.setAttribute("data-formline-ui", "");
      const root = overlay.attachShadow({ mode: "closed" });
      root.innerHTML = `<style>
        .pill{position:fixed;right:18px;bottom:18px;z-index:2147483647;display:flex;gap:10px;align-items:center;
          background:#0f172a;color:#fff;font:600 15px/1.3 system-ui,sans-serif;padding:12px 16px;border-radius:999px;
          box-shadow:0 8px 30px rgba(0,0,0,.25);max-width:420px}
        .dot{width:10px;height:10px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 0 rgba(34,197,94,.7);
          animation:p 1.6s infinite}
        @keyframes p{70%{box-shadow:0 0 0 10px rgba(34,197,94,0)}100%{box-shadow:0 0 0 0 rgba(34,197,94,0)}}
      </style><div class="pill"><span class="dot"></span><span class="t"></span></div>`;
      overlay._text = root.querySelector(".t");
      document.documentElement.appendChild(overlay);
    }
    overlay._text.textContent = text;
  }

  globalThis.FormlineExecutor = { run, showOverlay, ActionError };
})();
