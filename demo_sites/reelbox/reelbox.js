// Reelbox: a fictional film-diary site (Letterboxd-like) for testing search. Multi-page; the header search
// box is hidden until the icon-only search button is clicked, like many real sites.
const FILMS = [
  { id: "avengers-2012", title: "The Avengers", year: 2012, director: "Joss Whedon", rating: 3.6 },
  { id: "age-of-ultron", title: "Avengers: Age of Ultron", year: 2015, director: "Joss Whedon", rating: 3.0 },
  { id: "infinity-war", title: "Avengers: Infinity War", year: 2018, director: "Anthony and Joe Russo", rating: 3.8 },
  { id: "endgame", title: "Avengers: Endgame", year: 2019, director: "Anthony and Joe Russo", rating: 3.9 },
  { id: "the-avengers-1998", title: "The Avengers (1998)", year: 1998, director: "Jeremiah Chechik", rating: 1.8 },
  { id: "past-lives", title: "Past Lives", year: 2023, director: "Celine Song", rating: 4.2 },
  { id: "paddington-2", title: "Paddington 2", year: 2017, director: "Paul King", rating: 4.3 },
  { id: "the-iron-giant", title: "The Iron Giant", year: 1999, director: "Brad Bird", rating: 4.1 },
];

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
    <button class="nav-search-toggle" type="button" aria-expanded="false" title="">
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><use href="#icon-search"></use></svg>
    </button>
    <form class="nav-search" action="search.html" method="get" hidden>
      <input type="search" name="q" placeholder="Search" autocomplete="off">
      <button type="submit" class="go">Go</button>
    </form>
    <a class="avatar" href="#" onclick="return false">evan</a>
    <svg style="display:none"><symbol id="icon-search" viewBox="0 0 24 24"><circle cx="10" cy="10" r="7" stroke="currentColor" fill="none" stroke-width="2"/><path d="M15 15l6 6" stroke="currentColor" stroke-width="2"/></symbol></svg>`;
  const toggle = document.querySelector(".nav-search-toggle");
  const form = document.querySelector(".nav-search");
  toggle.addEventListener("click", () => {
    form.hidden = !form.hidden;
    toggle.setAttribute("aria-expanded", String(!form.hidden));
    if (!form.hidden) form.querySelector("input").focus();
  });
}

function renderHome() {
  document.getElementById("popular").innerHTML = FILMS.filter((f) => ["infinity-war", "past-lives", "paddington-2", "the-iron-giant"]
    .includes(f.id)).map(filmLink).join("");
}

function renderSearch() {
  const q = (new URLSearchParams(location.search).get("q") || "").trim();
  document.getElementById("query").textContent = q;
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const hits = FILMS.filter((f) => words.length && words.every((w) => f.title.toLowerCase().includes(w)));
  document.getElementById("results").innerHTML = hits.length ? hits.map(filmLink).join("")
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
