"""The bundle's page asset: reader.html's layout, with a corpus search on top.

The page is the prototype's reader — the same styles, the same two panes, the same page strip, the same
find field and the same provenance footer — extended by one thing the prototype could not do: a search
over every page's declared Chunks. Typing in it turns the strip into hits; clicking one lands on that
page with the phrase marked. Everything but the images is inlined by `bundle.py`, so the file opens
from `file://` as well as from a host. It keeps no state beyond the browser tab and writes nothing.
"""

from __future__ import annotations

BUNDLE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Page reader</title>
<style>
:root {
  color-scheme: light dark; --bg:#fbfaf7; --ink:#1b1a17; --muted:#6b675e; --line:#d9d4c8;
  --accent:#0b6b4f; --mark:#ffe28a;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#141412; --ink:#eeeae0; --muted:#a09a8c; --line:#33312c; --accent:#5fd3a6; --mark:#7a5a00; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; }
header { display:flex; gap:1rem; align-items:center; padding:.9rem 1.2rem;
  border-bottom:1px solid var(--line); flex-wrap:wrap; }
header h1 { font-size:1.05rem; margin:0; letter-spacing:.01em; }
header .hint { color:var(--muted); font-size:.82rem; }
header form { display:flex; gap:.5rem; align-items:center; flex:1 1 20rem; min-width:12rem; margin:0; }
header form input { flex:1; min-width:0; font:inherit; padding:.4rem .55rem; border:1px solid var(--line);
  border-radius:.45rem; background:var(--bg); color:var(--ink); }
header form .count { color:var(--muted); font-size:.8rem; white-space:nowrap; }
.controls { display:flex; gap:1rem; align-items:center; padding:.55rem 1.2rem;
  border-bottom:1px solid var(--line); flex-wrap:wrap; color:var(--muted); font-size:.82rem; }
.controls select { font:inherit; color:var(--ink); background:var(--bg); border:1px solid var(--line);
  border-radius:.45rem; padding:.3rem .45rem; max-width:min(34rem, 60vw); }
.lane { font-size:.7rem; text-transform:uppercase; letter-spacing:.07em; padding:.12rem .45rem;
  border:1px solid var(--line); border-radius:.4rem; color:var(--muted); white-space:nowrap; }
.lane[data-lane="ocr"] { background:var(--ink); border-color:var(--ink); color:var(--bg); }
.lane[data-lane="none"] { border-style:dashed; }
.note { flex:1 1 24rem; min-width:0; }
.note strong { color:var(--ink); font-weight:600; }
.note button { font:inherit; font-size:.8rem; padding:.15rem .5rem; border:1px solid var(--line);
  border-radius:.45rem; background:none; color:var(--ink); cursor:pointer; }
.toggle { display:flex; gap:.35rem; align-items:center; margin-left:auto; }
.toggle button { font:inherit; font-size:.8rem; padding:.25rem .6rem; border:1px solid var(--line);
  border-radius:999px; background:none; color:var(--muted); cursor:pointer; }
.toggle button[aria-pressed="true"] { color:var(--ink); border-color:var(--accent);
  box-shadow:inset 0 0 0 1px var(--accent); }
nav { display:flex; gap:.4rem; padding:.7rem 1.2rem; overflow-x:auto; border-bottom:1px solid var(--line); }
nav button { flex:0 0 auto; text-align:left; background:none; color:inherit; border:1px solid var(--line);
  border-radius:.55rem; padding:.45rem .6rem; cursor:pointer; font:inherit; max-width:22rem; }
nav button[aria-current] { border-color:var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
nav button span, nav button small { display:block; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; }
nav button small { color:var(--muted); font-size:.72rem; }
nav mark { background:var(--mark); color:inherit; border-radius:2px; padding:0 1px; }
main { display:grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap:1rem;
  padding:1rem 1.2rem 2rem; align-items:start; }
@media (max-width: 900px) { main { grid-template-columns: 1fr; } }
.pane { border:1px solid var(--line); border-radius:.8rem; overflow:hidden;
  background:color-mix(in srgb, var(--bg) 94%, var(--ink) 6%); }
.pane h2 { margin:0; padding:.55rem .8rem; font-size:.8rem; letter-spacing:.08em; text-transform:uppercase;
  color:var(--muted); border-bottom:1px solid var(--line); display:flex; justify-content:space-between;
  gap:.6rem; }
.stage { position:relative; padding:.6rem; display:flex; justify-content:center;
  background:repeating-conic-gradient(from 0deg, transparent 0 25%,
    color-mix(in srgb, var(--ink) 5%, transparent) 0 50%) 0 0/18px 18px; }
.figure { position:relative; display:inline-block; max-width:100%; }
.stage img { max-width:100%; height:auto; display:block; box-shadow:0 1px 8px rgb(0 0 0 / .18); }
.figure .missing { padding:1rem 1.1rem; border:1px dashed var(--line); border-radius:.6rem;
  background:var(--paper); color:var(--muted); font-size:.88rem; line-height:1.5; }
.figure .missing strong { color:var(--ink); }
.box { position:absolute; border:1.5px solid var(--accent);
  background:color-mix(in srgb, var(--mark) 55%, transparent); border-radius:2px; pointer-events:none; }
.legend { padding:.5rem .8rem; border-top:1px solid var(--line); color:var(--muted); font-size:.8rem; }
.legend strong { color:var(--ink); font-weight:600; }
.searchbar { display:flex; gap:.5rem; padding:.55rem .8rem; border-bottom:1px solid var(--line);
  align-items:center; }
.searchbar input { flex:1; min-width:0; font:inherit; padding:.4rem .55rem; border:1px solid var(--line);
  border-radius:.45rem; background:var(--bg); color:var(--ink); }
.searchbar .count { color:var(--muted); font-size:.8rem; white-space:nowrap; }
.text { padding:.3rem .9rem 1rem; max-height:72vh; overflow:auto; white-space:pre-wrap;
  font:13.5px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
.text mark { background:var(--mark); color:inherit; border-radius:2px; padding:0 1px; }
.chunk { border-left:3px solid transparent; padding-left:.5rem; margin-left:-.5rem; display:block; }
.chunk:hover { border-left-color:var(--accent);
  background:color-mix(in srgb, var(--accent) 7%, transparent); }
.chunk-label { color:var(--muted); font-size:.7rem; letter-spacing:.06em; text-transform:uppercase;
  display:block; margin:.5rem 0 .1rem; }
.missing { color:var(--muted); }
footer { padding:.9rem 1.2rem; border-top:1px solid var(--line); color:var(--muted); font-size:.78rem; }
footer code { color:var(--ink); }
</style>
</head>
<body>
<header>
  <h1>Page reader</h1>
  <span class="hint" id="hint"></span>
  <form id="corpus" role="search">
    <input id="q" type="search" placeholder="search every page — Danny, tribal consultation"
      aria-label="search the accepted text of every page">
    <span class="count" id="hits"></span>
  </form>
</header>
<div class="controls">
  <label>document <select id="doc" aria-label="document"></select></label>
  <span class="lane" id="doc-lane" aria-live="polite"></span>
  <span class="note" id="lane-note"></span>
  <span class="toggle" id="states" aria-label="engine reading"></span>
</div>
<nav id="pages" aria-label="pages"></nav>
<main>
  <section class="pane" aria-label="the page">
    <h2><span>the page</span><span id="dpi"></span></h2>
    <div class="stage"></div>
    <div class="legend"></div>
  </section>
  <section class="pane" aria-label="accepted text">
    <h2><span>accepted text</span><span id="engine-short"></span></h2>
    <div class="searchbar">
      <input id="find" type="search" placeholder="find on this page — marks text, and the page where boxes exist"
        aria-label="find on this page">
      <span class="count" id="count"></span>
    </div>
    <div class="text"></div>
  </section>
</main>
<footer class="prov"></footer>
<script>
const DATA = __DATA__;
const state = { label: DATA.default_state, doc: null, page: null, find: "", corpus: "", hits: [], index: [] };
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const docOf = (key) => key.split(":")[0];
const pageNo = (key) => Number(key.split(":")[1]);
function nameOf(id) {
  const url = DATA.documents[id].url;
  const tail = url.split("/").pop() || url;
  try { return decodeURIComponent(tail); } catch (error) { return tail; }
}
const pagesOf = (id) => Object.keys(DATA.pages).filter((key) => docOf(key) === id)
  .sort((a, b) => pageNo(a) - pageNo(b));
const terms = (q) => fold(String(q)).lower.split(/\\s+/).filter(Boolean);

function pageView(key) {
  const page = DATA.pages[key], id = docOf(key), reading = page.readings[state.label] || null;
  const source = DATA.documents[id].reads[state.label] || {};
  return {
    key, doc: id, name: nameOf(id), url: DATA.documents[id].url, page: pageNo(key),
    of: pagesOf(id).length, image: page.image, w: page.w, h: page.h, words: page.words,
    engine: source.engine || "", routing: source.routing || "", reason: source.reason || "",
    reading, text: reading ? reading.text : "", chunks: reading ? reading.chunks : [],
  };
}

function readingsOf(key) { return DATA.pages[key].readings; }

function laneOf(id) {
  const read = (DATA.documents[id].reads || {})[state.label];
  return read ? read.routing : "";
}

function laneName(routing) {
  return routing === "ocr" ? "OCR" : routing ? "text layer" : "not in this reading";
}

function laneCounts(label) {
  const counts = { ocr: 0, "text-layer": 0, "": 0 };
  for (const id of Object.keys(DATA.documents)) {
    const read = (DATA.documents[id].reads || {})[label];
    counts[read ? read.routing : ""] += 1;
  }
  return counts;
}

function readingDifference(key) {
  const readings = readingsOf(key);
  const labels = Object.keys(readings);
  if (labels.length < 2) return { kind: "single", labels };
  const texts = new Set(labels.map((label) => readings[label].text));
  const engines = new Set(labels.map((label) => (DATA.documents[docOf(key)].reads[label] || {}).engine));
  return { kind: texts.size === 1 ? "same" : "differs", labels, sameEngine: engines.size === 1 };
}

function firstDifferingPage(exclude) {
  return Object.keys(DATA.pages).find((key) => key !== exclude && readingDifference(key).kind !== "same");
}

function buildIndex() {
  state.index = [];
  for (const [key, page] of Object.entries(DATA.pages)) {
    const reading = page.readings[state.label];
    if (!reading) continue;
    for (const chunk of reading.chunks) {
      state.index.push({ key, chunk, text: reading.text.slice(chunk.start, chunk.end) });
    }
  }
}

function corpusSearch(query) {
  const wanted = terms(query);
  if (!wanted.length) return [];
  const hits = [];
  for (const entry of state.index) {
    if (wanted.every((term) => spansOf(entry.text, [term]).length)) hits.push(entry);
  }
  return hits;
}

// The strip shows the nearest matches; the count above it still reports every match.
const HIT_LIMIT = 60;

function fold(text) {
  // A lowercased copy plus, for every character in it, the UTF-16 range of the original character that
  // produced it. Lowering can expand one character into several (the Turkish dotted capital I becomes i
  // plus a combining dot), so a match in the lowered text needs a map to be marked in the original --
  // and the ranges are UTF-16, because that is the unit `slice` counts.
  let lower = "";
  const map = [];
  let at = 0;
  for (const part of text) {
    const small = part.toLowerCase();
    lower += small;
    for (let i = 0; i < small.length; i += 1) map.push([at, at + part.length]);
    at += part.length;
  }
  return { lower, map };
}

function spansOf(text, wanted) {
  // The one matcher: search, marks and the counter all go through it, so a page cannot be found by one
  // rule and then marked by another.
  const { lower, map } = fold(text);
  const spans = [];
  for (const term of wanted) {
    let at = lower.indexOf(term);
    while (at !== -1) {
      spans.push([map[at][0], map[at + term.length - 1][1]]);
      at = lower.indexOf(term, at + term.length);
    }
  }
  spans.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const span of spans) {
    const last = merged[merged.length - 1];
    if (last && span[0] <= last[1]) last[1] = Math.max(last[1], span[1]);
    else merged.push([span[0], span[1]]);
  }
  return merged;
}

function markIn(text, wanted) {
  const merged = spansOf(text, wanted);
  if (!merged.length) return esc(text);
  let out = "", cursor = 0;
  for (const [a, b] of merged) {
    out += esc(text.slice(cursor, a)) + "<mark>" + esc(text.slice(a, b)) + "</mark>";
    cursor = b;
  }
  return out + esc(text.slice(cursor));
}

function snippet(text, wanted) {
  const first = spansOf(text, wanted)[0];
  const start = Math.max(0, (first ? first[0] : 0) - 40);
  const piece = text.slice(start, start + 140);
  return (start > 0 ? "…" : "") + markIn(piece, wanted) + (start + 140 < text.length ? "…" : "");
}

function textPane(page) {
  if (!page.reading) {
    const rows = (DATA.excluded || {})[state.label] || [];
    const excluded = rows.find((row) => row.url === page.url);
    const why = excluded ? " — " + excluded.kind + ": " + excluded.reason : "";
    return '<span class="missing">This engine published no page here' + esc(why)
      + ". The image is the PDF page itself, not this engine's reading of it.</span>";
  }
  const chunks = page.chunks.length ? page.chunks : [{ id: "", start: 0, end: page.text.length, ordinal: 0 }];
  const wanted = terms(state.find);
  const parts = [];
  let cursor = 0;
  for (const chunk of chunks) {
    if (chunk.start > cursor) parts.push(esc(page.text.slice(cursor, chunk.start)));
    parts.push('<span class="chunk" data-chunk="' + esc(chunk.id) + '">');
    parts.push('<span class="chunk-label">chunk ' + chunk.ordinal + "</span>");
    parts.push(markIn(page.text.slice(chunk.start, chunk.end), wanted));
    parts.push("</span>");
    cursor = chunk.end;
  }
  if (cursor < page.text.length) parts.push(esc(page.text.slice(cursor)));
  return parts.join("");
}

function drawBoxes(page) {
  document.querySelectorAll(".box").forEach((box) => box.remove());
  const wanted = terms(state.find);
  if (!wanted.length || !page.words) return;
  const figure = $(".figure");
  const image = figure.querySelector("img");
  if (!image || !image.clientWidth || !image.clientHeight) return;
  const sx = image.clientWidth / page.w, sy = image.clientHeight / page.h;
  for (const [x0, y0, x1, y1, word] of page.words) {
    // Folded like the query, so a word the text marks is a word the image highlights.
    const lower = fold(word).lower;
    if (!wanted.some((term) => lower.includes(term))) continue;
    const box = document.createElement("div");
    box.className = "box";
    box.style.left = (x0 * sx) + "px";
    box.style.top = (y0 * sy) + "px";
    box.style.width = Math.max(2, (x1 - x0) * sx) + "px";
    box.style.height = Math.max(2, (y1 - y0) * sy) + "px";
    figure.appendChild(box);
  }
}

function counter(page) {
  const wanted = terms(state.find);
  if (!wanted.length) return "";
  const inText = spansOf(page.text, wanted).length;
  if (!page.words) return inText + " in text";
  const onPage = page.words.filter((w) => wanted.some((term) => fold(w[4]).lower.includes(term))).length;
  return inText + " in text · " + onPage + " on page";
}

function renderNav() {
  const nav = $("#pages");
  nav.textContent = "";
  if (state.corpus && state.hits.length) {
    state.hits.slice(0, HIT_LIMIT).forEach((hit) => {
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = "<span>" + esc(nameOf(docOf(hit.key))) + "</span><small>page " + pageNo(hit.key)
        + " · " + snippet(hit.text, terms(state.corpus)) + "</small>";
      if (hit.key === state.page) button.setAttribute("aria-current", "true");
      button.addEventListener("click", () => {
        if (state.corpus) { state.find = state.corpus; $("#find").value = state.corpus; }
        openPage(hit.key);
      });
      nav.appendChild(button);
    });
    if (state.hits.length > HIT_LIMIT) {
      const more = document.createElement("button");
      more.type = "button";
      more.disabled = true;
      more.innerHTML = "<span>" + (state.hits.length - HIT_LIMIT) + " more matches</span><small>narrow the "
        + "query to see them</small>";
      nav.appendChild(more);
    }
    return;
  }
  if (state.corpus) {
    const note = document.createElement("button");
    note.type = "button";
    note.disabled = true;
    note.innerHTML = "<span>no page in this reading has every word</span><small>try fewer words, or read it "
      + "with the other engine</small>";
    nav.appendChild(note);
    return;
  }
  for (const key of pagesOf(state.doc)) {
    const view = pageView(key);
    const button = document.createElement("button");
    button.type = "button";
    const chunks = view.chunks.length;
    const lane = view.routing === "ocr" ? "OCR" : view.routing ? "text layer" : "";
    const detail = view.reading
      ? [lane, chunks + (chunks === 1 ? " chunk" : " chunks"), view.text.length + " chars"]
        .filter(Boolean).join(" · ")
      : "not published by this engine";
    button.innerHTML = "<span>page " + view.page + " of " + view.of + "</span><small>" + detail + "</small>";
    if (key === state.page) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => { openPage(key); });
    nav.appendChild(button);
  }
}

function renderLaneNote() {
  const note = $("#lane-note");
  const difference = readingDifference(state.page);
  const view = pageView(state.page);
  const parts = [];
  if (difference.kind === "single") {
    parts.push("only the <strong>" + esc(difference.labels.join(" and ")) + "</strong> reading publishes this page");
  } else if (difference.kind === "same") {
    parts.push(difference.sameEngine
      ? "identical in both readings — both runs read this page with <strong>" + esc(view.engine) + "</strong>"
      : "both readings return the same text for this page");
  } else {
    parts.push("this page reads <strong>differently</strong> in the two runs");
  }
  const other = firstDifferingPage(state.page);
  if (other && difference.kind === "same") {
    parts.push('<button type="button" id="show-differing">show a page that differs</button>');
  }
  note.innerHTML = parts.join(" · ");
  const jump = $("#show-differing");
  if (jump) jump.addEventListener("click", () => openPage(other));
  return note;
}

function imageMissing(view) {
  const offServer = !/^https?:$/.test(location.protocol);
  return '<div class="missing"><strong>The page image did not load.</strong> '
    + (offServer
      ? "This copy is open outside a web server, and a preview pane cannot read the sibling "
        + "<code>images/</code> folder. The bundle needs <code>index.html</code> and <code>images/</code> "
        + "side by side, served over http(s)."
        + (DATA.link ? ' <a href="' + esc(DATA.link) + '">Open the served copy</a>.' : "")
      : "The bundle ships <code>" + esc(view.image) + "</code> next to <code>index.html</code>; "
        + "this host did not return it. Check that <code>images/</code> sits beside the page.")
    + "</div>";
}

function renderReader() {
  const view = pageView(state.page);
  $(".stage").innerHTML = '<div class="figure"><img alt="page ' + view.page + ' of ' + view.of
    + '" src="' + view.image + '"></div>';
  $(".legend").innerHTML = view.words
    ? "<strong>Marks on the page</strong> come from the PDF's own text layer (" + view.words.length
      + " words) — the same boxes whatever engine read it."
    : "<strong>No text boxes on this page.</strong> The engine returns text alone, so a match can be marked "
      + "in the text but never placed on the page.";
  const figureImg = $(".stage img");
  if (figureImg) {
    figureImg.addEventListener("error", () => { figureImg.parentNode.innerHTML = imageMissing(view); });
    if (figureImg.complete && figureImg.naturalWidth) drawBoxes(view);
    else figureImg.addEventListener("load", () => drawBoxes(view));
  }
  $(".text").innerHTML = textPane(view);
  $("#count").textContent = counter(view);
  $("#engine-short").textContent = state.label;
  $(".prov").innerHTML = "<code>" + esc(view.engine || "not published by this engine") + "</code> · routing <code>"
    + esc(view.routing || "none") + "</code>" + (view.reason ? " (" + esc(view.reason) + ")" : "")
    + " · page " + view.page + " of " + view.of + " · " + view.text.length + " characters · " + esc(view.url);
  $("#doc").value = state.doc;
  const chip = $("#doc-lane");
  chip.textContent = laneName(view.routing);
  chip.dataset.lane = view.routing === "ocr" ? "ocr" : view.routing ? "text-layer" : "none";
  chip.title = view.routing
    ? "this document was routed to the " + laneName(view.routing) + " lane: " + view.engine
    : "this document is not in the " + state.label + " reading";
  renderLaneNote();
}

function render() {
  renderNav();
  renderReader();
}

function openPage(key) {
  state.page = key;
  state.doc = docOf(key);
  render();
}

function hitCount() {
  if (!state.corpus) return "";
  const docs = new Set(state.hits.map((hit) => docOf(hit.key))).size;
  return state.hits.length + (state.hits.length === 1 ? " chunk" : " chunks") + " in " + docs
    + (docs === 1 ? " document" : " documents")
    + (state.hits.length > HIT_LIMIT ? " (nearest " + HIT_LIMIT + " listed)" : "");
}

function renderHint() {
  const counts = laneCounts(state.label);
  $("#hint").textContent = "static · no server, no upload · " + Object.keys(DATA.pages).length
    + " pages from a real run · readings: " + DATA.states.map((run) => run.label).join(" / ")
    + " · this reading: " + counts.ocr + " documents by OCR, " + counts["text-layer"] + " by the text layer";
}

function renderStates() {
  const box = $("#states");
  box.textContent = "reading:";
  for (const run of DATA.states) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = run.label;
    button.title = run.engine + " — " + run.documents + " documents, " + run.pages + " pages, "
      + run.exclusions + (run.exclusions === 1 ? " exclusion" : " exclusions");
    if (run.label === state.label) button.setAttribute("aria-pressed", "true");
    button.addEventListener("click", () => {
      state.label = run.label;
      buildIndex();
      state.hits = corpusSearch(state.corpus);
      $("#hits").textContent = hitCount();
      renderStates();
      renderDocuments();
      renderHint();
      render();
    });
    box.appendChild(button);
  }
}

function renderDocuments() {
  const select = $("#doc");
  select.textContent = "";
  const ids = Object.keys(DATA.documents).sort((a, b) => nameOf(a).localeCompare(nameOf(b)));
  const groups = [["ocr", "read by OCR"], ["text-layer", "read by the text layer"], ["", "not in this reading"]];
  for (const [routing, heading] of groups) {
    const members = ids.filter((id) => (laneOf(id) || "") === routing);
    if (!members.length) continue;
    const group = document.createElement("optgroup");
    group.label = heading + " (" + members.length + ")";
    for (const id of members) {
      const option = document.createElement("option");
      option.value = id;
      const count = pagesOf(id).length;
      option.textContent = nameOf(id) + " — " + count + (count === 1 ? " page" : " pages");
      group.appendChild(option);
    }
    select.appendChild(group);
  }
}

function applyCorpus(query) {
  state.corpus = query;
  state.hits = corpusSearch(query);
  $("#hits").textContent = hitCount();
  if (state.hits.length) {
    $("#find").value = query;
    state.find = query;
  }
}

function boot() {
  const first = Object.keys(DATA.documents)[0];
  state.doc = first;
  state.page = pagesOf(first)[0];
  renderHint();
  $("#dpi").textContent = "rendered at " + DATA.dpi + " dpi";
  buildIndex();
  renderDocuments();
  renderStates();
  $("#doc").addEventListener("change", () => {
    state.corpus = "";
    $("#q").value = "";
    $("#hits").textContent = "";
    state.hits = [];
    openPage(pagesOf($("#doc").value)[0]);
  });
  $("#corpus").addEventListener("submit", (event) => {
    event.preventDefault();
    applyCorpus($("#q").value.trim());
    if (state.hits.length) { state.page = state.hits[0].key; state.doc = docOf(state.page); }
    render();
  });
  $("#q").addEventListener("input", (event) => {
    state.corpus = event.target.value.trim();
    state.hits = corpusSearch(state.corpus);
    $("#hits").textContent = hitCount();
    renderNav();
  });
  $("#find").addEventListener("input", (event) => {
    state.find = event.target.value.trim();
    renderReader();
  });
  window.addEventListener("resize", () => drawBoxes(pageView(state.page)));
  const query = new URLSearchParams(location.search).get("q");
  if (query) {
    $("#q").value = query;
    applyCorpus(query);
    if (state.hits.length) { state.page = state.hits[0].key; state.doc = docOf(state.page); }
  }
  render();
}

boot();
</script>
</body>
</html>
"""
