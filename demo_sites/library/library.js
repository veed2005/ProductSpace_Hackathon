// Maple County Public Library: a fictional, multi-page library site (every step is a full page load).
// Loans live in localStorage so renewals stick across pages.
const DAY = 86400000;

function fmt(ms) {
  return new Date(ms).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

function loans() {
  const saved = JSON.parse(localStorage.getItem("mcpl_loans") || "null");
  if (saved) return saved;
  const now = Date.now();
  return [
    { id: "b1", title: "The Overstory", author: "Richard Powers", due: now + 2 * DAY, renewals: 2, hold: false },
    { id: "b2", title: "Lessons in Chemistry", author: "Bonnie Garmus", due: now + 1 * DAY, renewals: 2, hold: true },
    { id: "b3", title: "The Body Keeps the Score", author: "Bessel van der Kolk", due: now + 9 * DAY, renewals: 1, hold: false },
  ];
}

function saveLoans(list) {
  localStorage.setItem("mcpl_loans", JSON.stringify(list));
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function header() {
  document.querySelector("header").innerHTML = `
    <a class="brand" href="index.html"><span class="leaf">❦</span> Maple County Public Library</a>
    <nav aria-label="Library menu">
      <a href="index.html">Catalog</a><a href="#" onclick="alert('Events calendar is not part of this demo.')">Events</a>
      <a href="account.html">My Account</a><a href="#" onclick="alert('All branches are open 9 to 8.')">Hours &amp; Locations</a>
    </nav>
    <span class="patron">Signed in: Margaret E.</span>`;
}

function renderAccount() {
  const list = loans();
  document.getElementById("count").textContent = list.length;
  document.getElementById("loans").innerHTML = list.map((b) => `
    <tr>
      <td><input type="checkbox" id="pick-${b.id}" value="${b.id}" ${b.hold || !b.renewals ? "disabled" : ""}
        aria-label="Select ${esc(b.title)}"></td>
      <td><label for="pick-${b.id}"><b>${esc(b.title)}</b></label><br><span class="muted">${esc(b.author)}</span></td>
      <td>${fmt(b.due)}</td>
      <td>${b.hold ? "Can't renew: another patron has a hold" : `${b.renewals} renewal${b.renewals === 1 ? "" : "s"} left`}</td>
    </tr>`).join("");
  document.getElementById("renew-selected").addEventListener("click", () => {
    const ids = [...document.querySelectorAll("#loans input:checked")].map((c) => c.value);
    const err = document.getElementById("renew-error");
    if (!ids.length) {
      err.hidden = false;
      err.textContent = "Select at least one item to renew.";
      return;
    }
    location.href = "renew.html?items=" + ids.join(",");
  });
}

function renderReview() {
  const ids = new URLSearchParams(location.search).get("items")?.split(",") || [];
  const items = loans().filter((b) => ids.includes(b.id));
  document.getElementById("review").innerHTML = items.map((b) => `
    <li><b>${esc(b.title)}</b> by ${esc(b.author)}: due ${fmt(b.due)}, new due date <b>${fmt(b.due + 21 * DAY)}</b></li>`).join("");
  document.getElementById("confirm-renewal").addEventListener("click", () => {
    const list = loans().map((b) => ids.includes(b.id) ? { ...b, due: b.due + 21 * DAY, renewals: b.renewals - 1 } : b);
    saveLoans(list);
    localStorage.setItem("mcpl_last", JSON.stringify(ids));
    setTimeout(() => { location.href = "renewed.html"; }, 600);
    document.getElementById("confirm-renewal").textContent = "Renewing…";
    document.getElementById("confirm-renewal").disabled = true;
  });
}

function renderDone() {
  const ids = JSON.parse(localStorage.getItem("mcpl_last") || "[]");
  const items = loans().filter((b) => ids.includes(b.id));
  document.getElementById("done").innerHTML = items.map((b) =>
    `<li><b>${esc(b.title)}</b> is now due <b>${fmt(b.due)}</b>.</li>`).join("") || "<li>Nothing was renewed.</li>";
}

header();
