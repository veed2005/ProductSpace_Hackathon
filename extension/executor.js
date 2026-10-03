// Formline executor: performs one constrained action on an element from the current snapshot.
//
// There is no "run this script" action. Every action names an element id from FormlineObserver, and
// is checked here before it happens: the id must belong to this document and still be on the page,
// the element must be visible and enabled, and the action must fit the element (you can't type into
// a checkbox or a password field). After acting, it waits for the page to settle and reports what
// changed, so the backend judges success from the page, not from the model's say-so.
//
// `search` is the one composite action: it finds the site's own search box (opening it if it's hidden
// behind a search button), types the words key by key, waits for suggestions, and submits, so the
// fiddly part of searching a website is done by code the same careful way every time.
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

  // Finite CSS animations/transitions still running (a menu sliding open). Endless ones (spinners) don't count.
  function animating() {
    try {
      return document.getAnimations().some((a) => a.playState === "running" &&
        Number.isFinite(a.effect && a.effect.getComputedTiming().endTime));
    } catch (e) {
      return false;
    }
  }

  // Wait until the DOM has been quiet for 300 ms and nothing is animating (at least 150 ms, at most
  // `maxMs`), or a navigation starts.
  async function settle(counter, maxMs = 3000) {
    const start = performance.now();
    let last = counter.count;
    let quietSince = performance.now();
    while (performance.now() - start < maxMs) {
      await sleep(50);
      if (navigating) return;
      if (counter.count !== last) {
        last = counter.count;
        quietSince = performance.now();
      }
      if (performance.now() - start >= 150 && performance.now() - quietSince >= 300 && !animating()) return;
    }
  }

  function watchMutations() {
    const counter = { count: 0, added: [], weight: 0 };  // weight: elements added, including their insides
    const mo = new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.target instanceof Element && m.target.closest("[data-formline-ui]")) continue;
        counter.count++;
        for (const n of m.addedNodes) {
          if (n.nodeType !== Node.ELEMENT_NODE) continue;
          counter.added.push(n);
          counter.weight += 1 + n.getElementsByTagName("*").length;
        }
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

  function keyCode(ch) {
    if (/^[a-z]$/i.test(ch)) return "Key" + ch.toUpperCase();
    if (/^[0-9]$/.test(ch)) return "Digit" + ch;
    return ch === " " ? "Space" : "";
  }

  // Type the way a keyboard does: for every character keydown, keypress, beforeinput, the value change, input,
  // keyup. Typeahead widgets that listen for key events (not just "input") then react as they would to a person.
  async function typeText(el, text) {
    el.focus();
    if (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA") {  // contenteditable
      document.execCommand("selectAll", false);
      document.execCommand(text ? "insertText" : "delete", false, text);
      return (el.innerText || "").trim();
    }
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setValue = Object.getOwnPropertyDescriptor(proto, "value").set;
    if (el.value) {
      setValue.call(el, "");
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }));
    }
    for (const ch of text) {
      const k = { key: ch, code: keyCode(ch), bubbles: true, cancelable: true, view: window };
      const down = el.dispatchEvent(new KeyboardEvent("keydown", k));
      el.dispatchEvent(new KeyboardEvent("keypress", { ...k, charCode: ch.charCodeAt(0) }));
      if (down && el.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: ch }))) {
        setValue.call(el, el.value + ch);
        el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: ch }));
      }
      el.dispatchEvent(new KeyboardEvent("keyup", k));
      if (text.length <= 80) await sleep(10);
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return el.value;
  }

  function pressEnter(el) {
    const k = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true, view: window };
    const down = el.dispatchEvent(new KeyboardEvent("keydown", k));
    el.dispatchEvent(new KeyboardEvent("keypress", k));
    el.dispatchEvent(new KeyboardEvent("keyup", k));
    return down;  // false if the page handled Enter itself
  }

  // Did the page respond within `ms`: a navigation, a new address, or new content drawn in place (results)?
  // One innerHTML swap is a single mutation, so new content is measured by elements added, not by events.
  async function reacted(counter, urlBefore, ms) {
    const start = performance.now();
    const base = counter.count;
    const baseWeight = counter.weight;
    while (performance.now() - start < ms) {
      await sleep(50);
      if (navigating || location.href !== urlBefore) return true;
      if (counter.weight - baseWeight >= 5 || counter.count - base > 8) return true;
    }
    return false;
  }

  // Suggestions that appeared under a box after typing: options in the list it controls, a role=listbox, or
  // anything newly added just below it. Returned with snapshot ids so the agent can click one.
  function suggestionsFor(input, added) {
    const containers = new Set();
    for (const attr of ["aria-controls", "aria-owns"]) {
      for (const id of (input.getAttribute(attr) || "").split(/\s+/)) {
        const el = id && document.getElementById(id);
        if (el) containers.add(el);
      }
    }
    document.querySelectorAll("[role=listbox]").forEach((e) => containers.add(e));
    for (const n of added) if (n.isConnected) containers.add(n);
    const ir = input.getBoundingClientRect();
    const out = [];
    const seen = new Set();
    for (const c of containers) {
      if (!Obs.shownEnough(c)) continue;
      const r = c.getBoundingClientRect();
      if (r.top < ir.top - 4 || r.top > ir.bottom + 320 || r.right < ir.left - 80 || r.left > ir.right + 320) continue;
      const opts = c.matches("[role=option], a[href], li") ? [c] : [...c.querySelectorAll("[role=option], a[href], button, li")];
      for (let o of opts) {
        const inner = o.tagName === "LI" ? o.querySelector("a[href], button, [role=option]") : null;
        if (inner) o = inner;
        if (seen.has(o) || !Obs.shownEnough(o)) continue;
        const label = Obs.clean(Obs.nameOf(o, "option"));
        if (!label) continue;
        seen.add(o);
        out.push({ id: Obs.assignId(o), label });
        if (out.length >= 8) return out;
      }
    }
    return out;
  }

  async function waitForSuggestions(input, counter, ms) {
    const start = performance.now();
    while (performance.now() - start < ms) {
      await sleep(100);
      if (navigating) return [];
      const found = suggestionsFor(input, counter.added);
      if (found.length) {
        await sleep(150);  // let the list finish drawing
        return suggestionsFor(input, counter.added);
      }
    }
    return [];
  }

  // ------------------------------------------------------------ search

  const SCOPED = /(\w+['’]s\b|\bwithin\b|\bfilter\b|\bthis (page|list|section)\b|\byour\b|\bmy\b)/i;
  const SUBMIT_WORDS = /search|^go$|^find|submit|^ok$|→|›|»/i;

  function searchBoxes() {
    return [...document.querySelectorAll("input, textarea")].filter((i) =>
      !["hidden", "checkbox", "radio", "submit", "button", "image", "file", "password"].includes(i.type) &&
      !i.disabled && !i.readOnly && (i.type === "search" || Obs.looksLikeSearch(i)));
  }

  // The site's own search, not a filter for one person's reviews or a box inside a dialog.
  function searchScore(i) {
    let s = 0;
    const label = Obs.nameOf(i, "searchbox") || "";
    if (i.type === "search") s += 1;
    if (["q", "query", "search", "s", "keyword", "keywords"].includes((i.name || "").toLowerCase())) s += 2;
    if (i.closest("header, nav, [role=banner], [role=navigation]")) s += 3;
    if (i.closest("[role=search], search") || /search/i.test((i.form && i.form.getAttribute("action")) || "")) s += 3;
    if (i.closest("[role=dialog], dialog, [aria-modal=true]")) s -= 5;
    if (SCOPED.test(label)) s -= 4;
    return s;
  }

  function bestSearchBox({ visible }) {
    const boxes = searchBoxes().filter((i) => (visible ? Obs.shownEnough(i) && searchScore(i) > -3 : !Obs.shownEnough(i)));
    boxes.sort((a, b) => searchScore(b) - searchScore(a));
    return boxes[0] || null;
  }

  // The button, link or icon that opens a hidden search box.
  function searchToggle(input) {
    let best = null;
    let bestScore = 0;
    const controls = [...document.querySelectorAll("button, a, [role=button], summary, label")].filter((c) => Obs.shownEnough(c));
    for (const c of controls) {
      const name = Obs.nameOf(c, Obs.roleOf(c) || "button") || "";
      let s = 0;
      if (/search|magnif/i.test(name)) s += 3;
      if (SCOPED.test(name)) s -= 4;
      if (input) {
        const targets = [c.getAttribute("aria-controls"), c.getAttribute("data-target"), c.getAttribute("href")]
          .filter(Boolean).join(" ").split(/\s+/);
        if (targets.some((t) => { const el = document.getElementById(t.replace(/^#/, "")); return el && (el === input || el.contains(input)); })) s += 5;
        if (c.tagName === "LABEL" && c.control === input) s += 4;
        let up = input.parentElement;
        for (let d = 0; up && d < 4; d++, up = up.parentElement) {
          if (up.contains(c)) { s += 3 - Math.min(d, 2); break; }
        }
      }
      if (c.closest("header, nav, [role=banner]")) s += 1;
      if (s > bestScore) {
        best = c;
        bestScore = s;
      }
    }
    return bestScore >= 3 ? best : null;
  }

  function searchButtonNear(input, exclude) {
    let scope = input.parentElement;
    for (let d = 0; scope && d < 4; d++, scope = scope.parentElement) {
      const hit = [...scope.querySelectorAll("button, input[type=submit], input[type=button], [role=button], a")].find((b) =>
        b !== input && b !== exclude && !b.hasAttribute("aria-expanded") && Obs.shownEnough(b) &&
        SUBMIT_WORDS.test(Obs.clean(Obs.nameOf(b, "button"))));
      if (hit) return hit;
    }
    return null;
  }

  async function waitFor(fn, ms) {
    const start = performance.now();
    while (performance.now() - start < ms) {
      const v = fn();
      if (v) return v;
      await sleep(100);
    }
    return fn();
  }

  async function runSearch(args) {
    const query = String(args.value || "").trim();
    if (!query) throw new ActionError("invalid_action", "search needs the words to search for.");
    const urlBefore = location.href;
    const steps = [];
    let input = null;
    let toggle = null;
    if (args.element_id) {
      const { el, reason } = Obs.lookup(args.element_id);
      if (!el) throw new ActionError(reason, "That search box is no longer on the page.");
      input = el;
    }
    if (!input) input = bestSearchBox({ visible: true });
    if (!input || !Obs.shownEnough(input)) {
      const hidden = input || bestSearchBox({ visible: false });
      toggle = searchToggle(hidden);
      if (!toggle) {
        throw new ActionError("not_found", "There's no site search box on this page, and no search button to open one. " +
          "Look for a search link or menu, or use a box on the page by its id.");
      }
      highlight(toggle);
      realClick(toggle);
      steps.push(`opened the search with “${Obs.clean(Obs.nameOf(toggle, "button"))}”`);
      // Wait for it to finish opening (sites animate this); never click the toggle twice, that closes it.
      input = await waitFor(() => (hidden && Obs.shownEnough(hidden) && !animating() ? hidden : null) ||
        (!animating() && bestSearchBox({ visible: true })), 2500);
      if (!input) throw new ActionError("not_visible", "Clicked the search button, but no search box appeared.");
    }
    const box = Obs.clean(Obs.nameOf(input, "searchbox")) || "the search box";
    input.scrollIntoView({ block: "center" });
    highlight(input);
    const counter = watchMutations();
    try {
      await typeText(input, query);
      steps.push(`typed “${query}” into “${box}”`);
      const suggestions = await waitForSuggestions(input, counter, 1500);
      // Submitting may load a new page and end this script: leave the story so far with the service worker.
      const report = (extra) => {
        try { chrome.runtime.sendMessage({ kind: "formline_search_progress", detail: [...steps, ...extra].join("; ") }); } catch (e) { /* reloaded */ }
      };
      report(["submitted it; a new page opened"]);
      let how = null;
      const handled = !pressEnter(input);
      if (await reacted(counter, urlBefore, 700)) {
        how = "Enter";
      } else if (!handled) {
        if (input.form) {
          input.form.requestSubmit();
          if (await reacted(counter, urlBefore, 900)) how = "the search form";
        }
        if (!how) {
          const btn = searchButtonNear(input, toggle);
          if (btn) {
            highlight(btn);
            realClick(btn);
            if (await reacted(counter, urlBefore, 900)) how = `the “${Obs.clean(Obs.nameOf(btn, "button"))}” button`;
          }
        }
      }
      if (how) steps.push(`submitted with ${how}`);
      await settle(counter, 3000);
      const changed = navigating || location.href !== urlBefore;
      let detail = steps.join("; ");
      if (how && changed) detail += "; the results page opened";
      else if (how) detail += "; the page updated";
      else if (suggestions.length) {
        detail += "; nothing was submitted, but these suggestions appeared (click one): " +
          suggestions.map((s) => `[${s.id}] ${s.label}`).join("; ");
      } else detail += "; nothing happened after submitting";
      return { success: !!how || suggestions.length > 0, action: "search", url_before: urlBefore, url_after: location.href,
        page_changed: true, navigating, value: query, detail,
        error: !how && !suggestions.length ? "no_effect" : null };
    } finally {
      counter.stop();
    }
  }

  // ------------------------------------------------------------ single actions

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
    if (action === "search") return runSearch(args);

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
        if ((el.tagName === "INPUT" || el.tagName === "TEXTAREA") && !Obs.shownEnough(el)) {
          throw new ActionError("not_visible", "That box isn't open on screen yet (the search action opens search boxes).");
        }
        const wanted = action === "clear" ? "" : String(args.value ?? "");
        value = await typeText(el, wanted);
        if (norm(value) !== norm(wanted)) {
          await settle(counter);
          return { success: false, action, error: "value_mismatch", detail: "The field kept only: " + String(value).slice(0, 80),
            url_before: urlBefore, url_after: location.href, page_changed: counter.count > 0 };
        }
        // A search or autocomplete box: give its suggestions a moment to arrive before the next look.
        if (role === "searchbox" || role === "combobox" || el.getAttribute("aria-autocomplete")) {
          await waitForSuggestions(el, counter, 1200);
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
        const handled = !pressEnter(el);
        // A synthetic Enter doesn't submit a form the way a real key does: if nothing reacted, submit it.
        if (!(await reacted(counter, urlBefore, 600)) && !handled && el.form) el.form.requestSubmit();
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
