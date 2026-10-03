// Formline page observer: turns the live DOM into a compact semantic snapshot.
//
// Controls get short temporary ids ("e12") that stay the same for the life of this document, so the
// backend can refer to them without CSS selectors. Every snapshot carries a random doc_id; actions
// carrying another page's doc_id are refused as stale.
//
// Privacy: nothing secret leaves this function. Password fields, hidden inputs, and fields that look
// like card numbers, SSNs, PINs or one-time codes are reported without their values, and SSN- or
// card-shaped numbers are masked in all text. Cookies and storage are never read.
(() => {
  if (globalThis.FormlineObserver) return;

  const DOC_ID = Math.random().toString(36).slice(2, 7);
  const MAX_ITEMS = 400;
  const MAX_TEXT = 300;
  const MAX_LABEL = 160;

  let counter = 0;
  const idOf = new WeakMap();
  const byId = new Map();

  function assignId(el) {
    let id = idOf.get(el);
    if (!id) {
      id = "e" + (++counter);
      idOf.set(el, id);
      byId.set(id, new WeakRef(el));
    }
    return id;
  }

  // "stale" if the id existed but its element left the page; "unknown" if it never existed here.
  function lookup(id) {
    const ref = byId.get(id);
    if (!ref) return { el: null, reason: "not_found" };
    const el = ref.deref();
    if (!el || !el.isConnected) return { el: null, reason: "stale_element" };
    return { el, reason: null };
  }

  // ---------------------------------------------------------------- privacy

  const SSN_RE = /(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)/g;
  const CARD_RE = /(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)/g;
  const SENSITIVE_RE = /pass(word|code)|\bpin\b|ssn|social.?security|card.?number|credit.?card|cvv|cvc|security.?code|routing|account.?number|one.?time|otp|verification.?code/i;
  const SENSITIVE_AUTOCOMPLETE = /cc-|password|one-time-code/i;

  function mask(text) {
    return text.replace(SSN_RE, "[redacted SSN]").replace(CARD_RE, "[redacted number]");
  }

  function clean(text, limit) {
    let t = (text || "").replace(/\s+/g, " ").trim();
    if (t.length > limit) t = t.slice(0, limit - 1) + "…";
    return mask(t);
  }

  function isSensitiveField(el, label) {
    if (el.type === "password") return true;
    if (SENSITIVE_AUTOCOMPLETE.test(el.getAttribute("autocomplete") || "")) return true;
    const hay = [label, el.name, el.id, el.getAttribute("placeholder"), el.getAttribute("aria-label")].join(" ");
    return SENSITIVE_RE.test(hay);
  }

  // ---------------------------------------------------------------- roles and names

  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "META", "LINK", "HEAD", "IFRAME",
    "OBJECT", "EMBED", "CANVAS", "VIDEO", "AUDIO", "MAP", "SVG"]);
  const CONTROL_ROLES = new Set(["button", "link", "checkbox", "radio", "switch", "tab", "menuitem",
    "menuitemcheckbox", "menuitemradio", "option", "combobox", "textbox", "searchbox", "listbox", "select",
    "slider", "spinbutton", "treeitem", "password"]);
  const BLOCK_DISPLAYS = new Set(["block", "flex", "grid", "list-item", "table", "table-row", "table-cell",
    "table-caption", "flow-root", "table-row-group", "table-header-group", "table-footer-group"]);
  const INTERACTIVE_SELECTOR = "a[href],button,input,select,textarea,summary,[role=button],[role=link]," +
    "[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],[contenteditable=true]";

  function roleOf(el) {
    const explicit = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
    if (explicit === "presentation" || explicit === "none") return null;
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === "A" && el.hasAttribute("href")) return "link";
    if (tag === "BUTTON" || tag === "SUMMARY") return "button";
    if (tag === "SELECT") return el.multiple || el.size > 1 ? "listbox" : "select";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      if (t === "hidden") return "hidden";
      if (t === "password") return "password";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (["button", "submit", "reset", "image", "file"].includes(t)) return "button";
      if (t === "range") return "slider";
      if (t === "search") return "searchbox";
      return "textbox";
    }
    if (el.isContentEditable && el.hasAttribute("contenteditable")) return "textbox";
    if (/^H[1-6]$/.test(tag)) return "heading";
    if (tag === "DIALOG") return "dialog";
    return null;
  }

  function textOf(el) {
    return (el.innerText ?? el.textContent ?? "").replace(/\s+/g, " ").trim();
  }

  function textByIds(ids) {
    return ids.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map(textOf).join(" ").trim();
  }

  function labelText(label) {
    const clone = label.cloneNode(true);
    clone.querySelectorAll("input,select,textarea").forEach((n) => n.remove());
    return (clone.textContent || "").replace(/\s+/g, " ").trim();
  }

  function nearbyLabel(el) {
    const prev = el.previousElementSibling;
    if (prev && !prev.matches(INTERACTIVE_SELECTOR)) {
      const t = textOf(prev);
      if (t && t.length <= 80) return t;
    }
    return "";
  }

  function nameOf(el, role) {
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = textByIds(lb);
      if (t) return t;
    }
    const al = el.getAttribute("aria-label");
    if (al && al.trim()) return al.trim();
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map(labelText).join(" ").trim();
      if (t) return t;
    }
    if (el.tagName === "INPUT" && ["button", "submit", "reset"].includes(el.type)) return el.value || el.type;
    if (["textbox", "searchbox", "select", "combobox", "listbox", "password", "slider", "spinbutton"].includes(role)) {
      return el.getAttribute("placeholder") || el.getAttribute("aria-placeholder") || el.title || nearbyLabel(el) || el.name || "";
    }
    const t = textOf(el);
    if (t) return t;
    const img = el.querySelector("img[alt]");
    if (img && img.alt) return img.alt;
    const svgTitle = el.querySelector("svg title");
    if (svgTitle) return (svgTitle.textContent || "").trim();
    return el.title || el.getAttribute("name") || "";
  }

  function groupOf(el, role) {
    if (role !== "radio" && role !== "checkbox") return null;
    const rg = el.closest("[role=radiogroup],[role=group]");
    if (rg) {
      const n = rg.getAttribute("aria-label") || (rg.getAttribute("aria-labelledby") && textByIds(rg.getAttribute("aria-labelledby")));
      if (n) return n;
    }
    const fs = el.closest("fieldset");
    const legend = fs && fs.querySelector("legend");
    if (legend) return textOf(legend);
    return el.name || null;
  }

  function hrefOf(el) {
    try {
      const u = new URL(el.href, location.href);
      return u.origin === location.origin ? u.pathname + u.hash : u.origin;
    } catch (e) {
      return null;
    }
  }

  function looksClickable(el, cs) {
    if (cs.cursor !== "pointer") return false;
    const parent = el.parentElement;
    if (parent && getComputedStyle(parent).cursor === "pointer") return false;
    if (el.querySelector(INTERACTIVE_SELECTOR)) return false;
    const t = textOf(el);
    return t.length > 0 && t.length <= 120;
  }

  function inViewport(el) {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  }

  function controlItem(el, role, inDialog) {
    const label = nameOf(el, role);
    const item = {
      id: assignId(el),
      role,
      label: clean(label, MAX_LABEL),
      enabled: !(el.disabled || el.getAttribute("aria-disabled") === "true" || el.closest("fieldset[disabled]")),
      in_dialog: !!inDialog,
      in_view: inViewport(el),
    };
    const sensitive = (role === "textbox" || role === "searchbox" || role === "password" || role === "combobox") &&
      isSensitiveField(el, label);
    if (role === "password") item.role = "password";
    if (["textbox", "searchbox", "password", "combobox", "spinbutton"].includes(item.role)) {
      if (el.tagName === "INPUT") item.input_type = (el.type || "text").toLowerCase();
      if (el.tagName === "TEXTAREA") item.input_type = "textarea";
      const raw = el.isContentEditable && el.tagName !== "INPUT" && el.tagName !== "TEXTAREA" ? textOf(el) : el.value;
      item.value = sensitive ? null : clean(raw, MAX_TEXT);
      if (sensitive) item.sensitive = true;
      const ph = el.getAttribute("placeholder");
      if (ph) item.placeholder = clean(ph, 80);
      if (item.role === "combobox") item.expanded = el.getAttribute("aria-expanded") === "true";
    }
    if (role === "checkbox" || role === "radio" || role === "switch" || role === "menuitemcheckbox" || role === "menuitemradio") {
      item.checked = el.tagName === "INPUT" ? el.checked : el.getAttribute("aria-checked") === "true";
      const g = groupOf(el, role);
      if (g) item.group = clean(g, 80);
    }
    if (role === "select" || role === "listbox") {
      if (el.tagName === "SELECT") {
        const opts = Array.from(el.options);
        item.options = opts.slice(0, 40).map((o) => clean(o.text, 80));
        const sel = opts.filter((o) => o.selected).map((o) => o.text);
        item.value = clean(sel.join(", "), MAX_LABEL);
      }
    }
    if (role === "option" || role === "tab" || role === "treeitem") {
      item.selected = el.getAttribute("aria-selected") === "true";
    }
    if (role === "button") {
      const pressed = el.getAttribute("aria-pressed");
      if (pressed) item.checked = pressed === "true";
      const exp = el.getAttribute("aria-expanded");
      if (exp) item.expanded = exp === "true";
    }
    if (role === "link") item.href = hrefOf(el);
    if (el.required || el.getAttribute("aria-required") === "true") item.required = true;
    let invalid = el.getAttribute("aria-invalid") === "true";
    try { invalid = invalid || el.matches(":user-invalid"); } catch (e) { /* older Chrome */ }
    if (invalid) item.invalid = true;
    const described = [el.getAttribute("aria-describedby"), el.getAttribute("aria-errormessage")].filter(Boolean).join(" ");
    if (described) {
      const d = textByIds(described);
      if (d) item.description = clean(d, MAX_LABEL);
    }
    return item;
  }

  // ---------------------------------------------------------------- snapshot

  function snapshot() {
    const items = [];
    let dialogOpen = false;

    // Returns {interactive, loose, blockish}: "loose" is inline text not yet attached to a block.
    function visit(el, inDialog) {
      if (SKIP_TAGS.has(el.tagName.toUpperCase()) || el.hasAttribute("data-formline-ui")) return { interactive: false, loose: "" };
      if (el.hidden || el.getAttribute("aria-hidden") === "true" || el.inert) return { interactive: false, loose: "" };
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || cs.visibility === "collapse") return { interactive: false, loose: "" };
      const role = roleOf(el);
      if (role === "hidden") return { interactive: false, loose: "" };

      const isDialog = role === "dialog" || role === "alertdialog" || (el.tagName === "DIALOG" && el.open);
      const nowInDialog = inDialog || isDialog;
      if (role && CONTROL_ROLES.has(role)) {
        items.push(controlItem(el, role, nowInDialog));
        return { interactive: true, loose: "" };
      }
      if (!role && looksClickable(el, cs)) {
        items.push(controlItem(el, "button", nowInDialog));
        return { interactive: true, loose: "" };
      }

      const block = el === document.body || BLOCK_DISPLAYS.has(cs.display) || role === "heading" || isDialog ||
        role === "alert" || role === "status";
      const slot = items.length;
      if (block) items.push(null);
      if (isDialog) {
        dialogOpen = true;
        items[slot] = { role: "dialog", label: clean(el.getAttribute("aria-label") ||
          (el.getAttribute("aria-labelledby") && textByIds(el.getAttribute("aria-labelledby"))) || "Dialog", MAX_LABEL), in_dialog: true };
      }

      let loose = "";
      let interactive = false;
      const kids = el.shadowRoot ? [...el.shadowRoot.childNodes, ...el.childNodes] : el.childNodes;
      for (const node of kids) {
        if (node.nodeType === Node.TEXT_NODE) {
          loose += node.nodeValue;
        } else if (node.nodeType === Node.ELEMENT_NODE) {
          const r = visit(node, nowInDialog);
          interactive = interactive || r.interactive;
          loose += r.blockish ? " " : r.loose;
        }
      }

      if (el.tagName === "LABEL" && el.control) loose = "";  // already the control's label
      if (role === "heading") {
        const level = /^H([1-6])$/.exec(el.tagName);
        items[slot] = { role: "heading", label: clean(textOf(el), MAX_TEXT), level: level ? +level[1] : (+el.getAttribute("aria-level") || 2), in_dialog: nowInDialog };
        return { interactive, loose: "", blockish: true };
      }
      if (role === "alert" || role === "status") {
        const t = clean(loose || textOf(el), MAX_TEXT);
        if (t) items[slot] = { role: "alert", label: t, in_dialog: nowInDialog };
        return { interactive, loose: "", blockish: true };
      }
      if (block) {
        const t = clean(loose, MAX_TEXT);
        if (t && !isDialog) items[slot] = { role: "text", label: t, in_dialog: nowInDialog };
        return { interactive, loose: "", blockish: true };
      }
      return { interactive, loose, blockish: false };
    }

    if (document.body) visit(document.body, false);
    const elements = items.filter(Boolean);
    const se = document.scrollingElement || document.documentElement;
    return {
      doc_id: DOC_ID,
      url: location.href,
      title: clean(document.title, 160),
      site_name: siteName(),
      elements: elements.slice(0, MAX_ITEMS),
      truncated: elements.length > MAX_ITEMS,
      dialog_open: dialogOpen,
      at_top: se.scrollTop <= 2,
      at_bottom: se.scrollTop + innerHeight >= se.scrollHeight - 2,
    };
  }

  function siteName() {
    const meta = document.querySelector('meta[property="og:site_name"],meta[name="application-name"]');
    const name = meta && meta.getAttribute("content");
    return name ? clean(name, 80) : null;
  }

  globalThis.FormlineObserver = { DOC_ID, snapshot, lookup, roleOf, isSensitiveField, nameOf };
})();
