// Reelbox: a fictional film-diary site, built to behave like Letterboxd where it matters for search:
// - the header search field is collapsed (zero width, transparent) until the magnifying glass is clicked,
//   slides open with a CSS transition, and a second click closes it again;
// - its suggestions react only to key-by-key typing (keyup) and arrive after a short delay;
// - Enter submits only through the browser's own form submission (no script handles the key);
// - decoys: a profile-only search box, and a LOG dialog whose film search needs a suggestion to be clicked;
// - explore.html has a search widget with no form and no Enter: only its Go button works.
const FILMS = [
  { id: "avengers-2012", title: "The Avengers", year: 2012, director: "Joss Whedon", rating: 3.6 },
  { id: "age-of-ultron", title: "Avengers: Age of Ultron", year: 2015, director: "Joss Whedon", rating: 3.0 },
  { id: "infinity-war", title: "Avengers: Infinity War", year: 2018, director: "Anthony and Joe Russo", rating: 3.8 },
  { id: "endgame", title: "Avengers: Endgame", year: 2019, director: "Anthony and Joe Russo", rating: 3.9 },
  { id: "the-avengers-1998", title: "The Avengers (1998)", year: 1998, director: "Jeremiah Chechik", rating: 1.8 },
  { id: "past-lives", title: "Past Lives", year: 2023, director: "Celine Song", rating: 4.2 },
  { id: "paddington-2", title: "Paddington 2", year: 2017, director: "Paul King", rating: 4.3 },
  { id: "the-iron-giant", title: "The Iron Giant", year: 1999, director: "Brad Bird", rating: 4.1 },
  { id: "arrival", title: "Arrival", year: 2016, director: "Denis Villeneuve", rating: 4.1 },
  { id: "the-ritual", title: "The Ritual", year: 2017, director: "David Bruckner", rating: 3.2 },
];

function matches(q) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  return FILMS.filter((f) => words.length && words.every((w) => f.title.toLowerCase().includes(w)));
}

// Suggestions the way typeahead plugins do it: on keyup, debounced, then a "network" delay.
function typeahead(input, list, onPick) {
  let timer = null;
  input.addEventListener("keyup", () => {
    clearTimeout(timer);
    timer = setTimeout(() => setTimeout(() => {
      const hits = input.value.trim().length >= 2 ? matches(input.value).slice(0, 6) : [];
      list.innerHTML = hits.map((f) => `<li role="option"><a href="film.html?id=${f.id}" data-id="${f.id}">${esc(f.title)} (${f.year})</a></li>`).join("");
      list.hidden = !hits.length;
      input.setAttribute("aria-expanded", String(!!hits.length));
      if (onPick) list.querySelectorAll("a").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); onPick(a.dataset.id); }));
    }, 300), 300);
  });
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function filmLink(f) {
  return `<li class="film"><a href="film.html?id=${f.id}"><span class="poster" aria-hidden="true">${esc(f.title[0])}</span>
    <span class="t">${esc(f.title)}</span><span class="y">${f.year}</span></a></li>`;
}

function header() {
  document.querySelector("header").innerHTML = `
    <a class="logo" href="index.html">reel<b>box</b></a>
    <nav aria-label="Main"><a href="index.html">Films</a><a href="#" onclick="return false">Lists</a>
      <a href="#" onclick="return false">Members</a><a href="#" onclick="return false">Journal</a></nav>
    <div class="nav-search" id="nav-search">
      <form class="search-form" action="search.html" method="get" autocomplete="off">
        <input type="text" name="q" class="search-field" placeholder="Search…" role="combobox" aria-expanded="false" aria-autocomplete="list">
        <ul class="ac" role="listbox" hidden></ul>
      </form>
      <button class="search-toggle" type="button">
        <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><use href="#icon-search"></use></svg>
      </button>
    </div>
    <button class="log-button" type="button">+ LOG</button>
    <a class="avatar" href="profile.html">linky</a>
    <svg style="display:none"><symbol id="icon-search" viewBox="0 0 24 24"><circle cx="10" cy="10" r="7" stroke="currentColor" fill="none" stroke-width="2"/><path d="M15 15l6 6" stroke="currentColor" stroke-width="2"/></symbol></svg>`;
  const box = document.getElementById("nav-search");
  const field = box.querySelector(".search-field");
  // Like many sites: the click starts an opening animation and the field only becomes visible when it ends
  // (about half a second later). Clicking again, while opening or open, closes it.
  let opening = null;
  box.querySelector(".search-toggle").addEventListener("click", () => {
    if (opening || box.classList.contains("open")) {
      clearTimeout(opening);
      opening = null;
      box.classList.remove("open", "opening");
      return;
    }
    box.classList.add("opening");
    opening = setTimeout(() => {
      opening = null;
      box.classList.replace("opening", "open");
      field.focus();
    }, 450);
  });
  typeahead(field, box.querySelector(".ac"));
  document.querySelector(".log-button").addEventListener("click", openLogDialog);
}

// "+ LOG": add a film to your diary. Its search needs a suggestion to be clicked; Enter does nothing.
function openLogDialog() {
  let dlg = document.getElementById("log-dialog");
  if (!dlg) {
    dlg = document.createElement("div");
    dlg.id = "log-dialog";
    dlg.setAttribute("role", "dialog");
    dlg.setAttribute("aria-label", "Add to your films");
    dlg.innerHTML = `<h2>Add to your films…</h2>
      <input type="text" class="log-search" placeholder="Search for film…" aria-label="Search for film…">
      <ul class="ac log-ac" role="listbox" hidden></ul>
      <button type="button" class="close">Close</button>`;
    document.body.appendChild(dlg);
    dlg.querySelector(".close").addEventListener("click", () => { dlg.hidden = true; });
    dlg.querySelector(".log-search").addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
    typeahead(dlg.querySelector(".log-search"), dlg.querySelector(".log-ac"), (id) => {
      dlg.querySelector("h2").textContent = "Log " + FILMS.find((f) => f.id === id).title + "? (not part of this demo)";
    });
  }
  dlg.hidden = false;
}

function renderProfile() {
  const filter = document.getElementById("profile-search");
  filter.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    document.getElementById("profile-results").textContent = `No reviews or lists by Linky match “${filter.value}”.`;
  });
}

// A search widget with no form and no Enter handling: only the Go button searches.
function renderExplore() {
  const input = document.getElementById("explore-q");
  document.getElementById("explore-go").addEventListener("click", () => {
    const hits = matches(input.value);
    document.getElementById("explore-results").innerHTML = hits.length
      ? `<h2>${hits.length} result${hits.length === 1 ? "" : "s"} for “${esc(input.value)}”</h2><ul class="posters">${hits.map(posterLink).join("")}</ul>`
      : `<p>No films match “${esc(input.value)}”.</p>`;
  });
}

function renderHome() {
  document.getElementById("more").addEventListener("click", (e) => e.preventDefault());  // a tempting dead end
  document.getElementById("popular").innerHTML = FILMS.filter((f) => ["infinity-war", "past-lives", "paddington-2", "the-iron-giant"]
    .includes(f.id)).map(filmLink).join("");
}

// Search results look like Letterboxd's poster grid: the link has no text, only a tooltip attribute and
// a "has-menu" class, drawn over the poster image next to it.
function posterLink(f) {
  const img = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='210'%3E%3Crect width='140' " +
    "height='210' fill='%232c3440'/%3E%3C/svg%3E";
  return `<li class="poster-container"><div class="film-poster"><img alt="${esc(f.title)}" src="${img}">
    <a href="film.html?id=${f.id}" class="frame has-menu" data-original-title="${esc(f.title)} (${f.year})"><span class="overlay"></span></a>
  </div></li>`;
}

function renderSearch() {
  const q = (new URLSearchParams(location.search).get("q") || "").trim();
  document.getElementById("query").textContent = q;
  const hits = matches(q);
  document.getElementById("results").innerHTML = hits.length ? hits.map(posterLink).join("")
    : `<li>There were no matches for your search term.</li>`;
  document.getElementById("count").textContent = `Found ${hits.length} film${hits.length === 1 ? "" : "s"} matching “${q}”`;
}

function renderFilm() {
  const f = FILMS.find((x) => x.id === new URLSearchParams(location.search).get("id"));
  if (!f) return;
  document.title = `${f.title} (${f.year}) • Reelbox`;
  document.getElementById("film").innerHTML = `<h1>${esc(f.title)}</h1>
    <p class="meta">${f.year} · Directed by ${esc(f.director)}</p>
    <p>Average rating <b>${f.rating.toFixed(1)}</b> out of 5</p>
    <button type="button" onclick="this.textContent='On your watchlist'">Add to watchlist</button>`;
}

header();
