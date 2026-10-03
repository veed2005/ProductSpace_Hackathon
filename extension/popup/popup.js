// Formline popup: pairing (phone -> code -> paired) and connection status.
const DEFAULT_SERVER = "http://localhost:8000";
const $ = (id) => document.getElementById(id);

function show(section) {
  for (const id of ["paired", "step-phone", "step-code"]) $(id).hidden = id !== section;
}

function error(text) {
  $("error").hidden = !text;
  $("error").textContent = text || "";
}

function maskPhone(p) {
  return p ? "(•••) •••-" + p.slice(-4) : "";
}

async function server() {
  const { serverUrl } = await chrome.storage.local.get("serverUrl");
  return (serverUrl || DEFAULT_SERVER).replace(/\/+$/, "");
}

async function post(path, body) {
  const res = await fetch((await server()) + path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const detail = data.detail;
    throw new Error((detail && (detail.message || detail)) || "Formline couldn't be reached.");
  }
  return data;
}

function browserLabel() {
  const p = navigator.userAgentData?.platform || navigator.platform || "";
  return "Chrome on " + (p.replace("Win32", "Windows").replace("MacIntel", "Mac") || "this computer");
}

// ---------------------------------------------------------------- status view

async function renderStatus() {
  const cfg = await chrome.storage.local.get(["installationId", "profileName", "phone"]);
  if (!cfg.installationId) return false;
  show("paired");
  $("who-name").textContent = cfg.profileName || "your profile";
  $("who-phone").textContent = maskPhone(cfg.phone);
  const st = await chrome.runtime.sendMessage({ kind: "formline_status" }).catch(() => null);
  const status = (st && st.status) || "connecting";
  const text = { connected: "CONNECTED", connecting: "Connecting…", disconnected: "Not connected",
    unauthorized: "Pairing removed", unpaired: "Not paired" }[status] || status;
  $("status").className = "status " + status;
  $("status-text").textContent = text;
  $("page").textContent = st && st.tab ? "Current page: " + (st.tab.title || st.tab.url) : "No web page open.";
  return true;
}

// ---------------------------------------------------------------- pairing

async function renderPairing() {
  const { pairing, serverUrl } = await chrome.storage.local.get(["pairing", "serverUrl"]);
  $("server").value = serverUrl || DEFAULT_SERVER;
  if (pairing && pairing.expiresAt > Date.now()) {
    showCodeStep(pairing);
  } else {
    show("step-phone");
    $("phone").focus();
  }
}

function showCodeStep(p) {
  show("step-code");
  $("code-help").textContent = (p.delivery === "call" ? "We're calling " : "We texted a code to ") +
    p.phone + (p.delivery === "call" ? " with your code." : ".");
  $("profile-fields").hidden = !p.needProfile;
  $("pin-fields").hidden = !(p.needProfile || p.needPin);
  $("choose-fields").hidden = !(p.profiles && p.profiles.length);
  if (p.profiles) $("choose").innerHTML = p.profiles.map((n) => `<option>${n.replace(/</g, "&lt;")}</option>`).join("");
  $("dev-code").hidden = !p.devCode;
  $("dev-code").textContent = p.devCode ? "Development server (no Twilio): your code is " + p.devCode : "";
  $("code").focus();
}

$("server").addEventListener("change", async () => {
  await chrome.storage.local.set({ serverUrl: $("server").value.trim() || DEFAULT_SERVER });
});

$("send-code").addEventListener("click", async () => {
  error("");
  $("send-code").disabled = true;
  try {
    await chrome.storage.local.set({ serverUrl: $("server").value.trim() || DEFAULT_SERVER });
    const delivery = document.querySelector("input[name=delivery]:checked").value;
    const r = await post("/browser/pair/start", { phone: $("phone").value, delivery });
    const pairing = { id: r.pairing_id, phone: r.phone, delivery: r.delivery, devCode: r.dev_code || null,
      expiresAt: Date.now() + r.expires_in * 1000 };
    await chrome.storage.local.set({ pairing });
    showCodeStep(pairing);
  } catch (e) {
    error(e.message);
  } finally {
    $("send-code").disabled = false;
  }
});

$("confirm").addEventListener("click", async () => {
  error("");
  $("confirm").disabled = true;
  try {
    const { pairing } = await chrome.storage.local.get("pairing");
    const body = { pairing_id: pairing.id, code: $("code").value, label: browserLabel() };
    if (!$("profile-fields").hidden) body.name = $("pname").value;
    if (!$("choose-fields").hidden) body.name = $("choose").value;
    if (!$("pin-fields").hidden) body.pin = $("pin").value;
    const r = await post("/browser/pair/confirm", body);
    if (r.status === "paired") {
      await chrome.storage.local.set({ installationId: r.installation_id, token: r.token,
        profileName: r.profile_name, phone: r.phone });
      await chrome.storage.local.remove("pairing");
      await chrome.runtime.sendMessage({ kind: "formline_reconnect" });
      await renderStatus();
      return;
    }
    const next = { ...pairing, needProfile: r.status === "need_profile", needPin: r.status === "need_pin",
      profiles: r.profiles || null };
    await chrome.storage.local.set({ pairing: next });
    showCodeStep(next);
    error(r.status === "need_profile" ? "This number is new to Formline: add your name and choose a PIN."
      : r.status === "need_pin" ? "Choose a new 4-digit PIN." : "Choose who you are.");
  } catch (e) {
    error(e.message);
  } finally {
    $("confirm").disabled = false;
  }
});

$("restart").addEventListener("click", async () => {
  await chrome.storage.local.remove("pairing");
  error("");
  renderPairing();
});

$("unpair").addEventListener("click", async () => {
  const cfg = await chrome.storage.local.get(["installationId", "token"]);
  try {
    await fetch((await server()) + "/browser/unpair", {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + cfg.token },
      body: JSON.stringify({ installation_id: cfg.installationId }),
    });
  } catch (e) { /* offline: forget locally anyway */ }
  await chrome.storage.local.remove(["installationId", "token", "profileName", "phone"]);
  await chrome.runtime.sendMessage({ kind: "formline_reconnect" });
  renderPairing();
});

(async () => {
  if (!(await renderStatus())) renderPairing();
  setInterval(async () => { if (!$("paired").hidden) renderStatus(); }, 1500);
})();
