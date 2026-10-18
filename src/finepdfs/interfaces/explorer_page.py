"""The explorer's page asset: one self-contained HTML document with its styles and script.

The page is a view over the read-only API and keeps no state of its own, so it lives beside the
explorer rather than inside the module that reads declared exports and serves HTTP ranges. No
framework and no build step: the browser is served this string as it is.
"""

from __future__ import annotations

EXPLORER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>PDF curation explorer</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f7f7f5;
    --panel: #ffffff;
    --ink: #1d2433;
    --muted: #5b6474;
    --line: #d8dce3;
    --accent: #225ea8;
    --ok: #17663a;
    --warn: #8a4b00;
    --bad: #9c1c1c;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14181f;
      --panel: #1d232d;
      --ink: #e8ecf3;
      --muted: #a9b3c2;
      --line: #333c4a;
      --accent: #78b7ff;
      --ok: #7fd3a2;
      --warn: #f2c078;
      --bad: #ff9c9c;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0 auto;
    max-width: 1180px;
    padding: 1.25rem;
    background: var(--bg);
    color: var(--ink);
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header h1 { margin: 0 0 .25rem; font-size: 1.6rem; }
  .scope, .meta { color: var(--muted); margin: .25rem 0; }
  .summary {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: .6rem;
    margin: 1rem 0;
  }
  .summary div {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .5rem;
    padding: .6rem .7rem;
  }
  .summary dt { color: var(--muted); font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; }
  .summary dd { margin: .15rem 0 0; font-weight: 600; overflow-wrap: anywhere; }
  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: .6rem;
    align-items: end;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .5rem;
    padding: .75rem;
  }
  .filters label { display: grid; gap: .2rem; font-size: .8rem; color: var(--muted); }
  .search {
    margin-top: .9rem;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .5rem;
    padding: .75rem;
  }
  .search form { display: flex; flex-wrap: wrap; gap: .6rem; align-items: end; }
  .search label { display: grid; gap: .2rem; font-size: .8rem; color: var(--muted); }
  #search-query { min-width: 20rem; }
  #search-doc { min-width: 10rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  #search-top-k { width: 6rem; }
  #search-results { margin-top: .75rem; }
  .hit { border-top: 1px solid var(--line); padding: .75rem 0; }
  .hit h3 { margin: 0 0 .2rem; font-size: 1rem; }
  .hit .meta { font-size: .85rem; overflow-wrap: anywhere; }
  .hit p.snippet { margin: .35rem 0; }
  .hit mark { background: color-mix(in srgb, var(--warn) 35%, transparent); color: inherit; }
  .hit details { margin-top: .35rem; }
  .hit pre {
    margin: .4rem 0 0;
    padding: .6rem;
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: .35rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .hit a, .hit button.link {
    color: var(--accent);
    background: none;
    border: 0;
    padding: 0;
    font: inherit;
    cursor: pointer;
  }
  input, select, button {
    font: inherit;
    color: var(--ink);
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: .35rem;
    padding: .38rem .5rem;
  }
  input[type="search"] { min-width: 18rem; }
  button { cursor: pointer; }
  button:hover { border-color: var(--accent); }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; background: var(--panel); }
  caption { text-align: left; padding: .5rem 0; color: var(--muted); }
  th, td { text-align: left; padding: .5rem .55rem; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
  tbody tr { cursor: pointer; }
  tbody tr:hover, tbody tr.selected { background: color-mix(in srgb, var(--accent) 12%, transparent); }
  td.url { max-width: 28rem; overflow-wrap: anywhere; }
  .status { font-weight: 600; white-space: nowrap; }
  .status-published { color: var(--ok); }
  .status-dropped, .status-failed { color: var(--bad); }
  .status-exact-duplicate, .status-near-duplicate { color: var(--warn); }
  #document-detail {
    margin-top: 1.25rem;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .5rem;
    padding: 1rem;
  }
  #document-detail h2 { margin-top: 0; overflow-wrap: anywhere; }
  #document-detail dl { display: grid; grid-template-columns: minmax(9rem, 14rem) 1fr; gap: .25rem .8rem; }
  #document-detail dt { color: var(--muted); }
  #document-detail dd { margin: 0; overflow-wrap: anywhere; }
  .page { border-top: 1px solid var(--line); padding: .8rem 0; }
  .page h4 { margin: 0 0 .35rem; }
  .page a { color: var(--accent); }
  .page pre {
    margin: .5rem 0 0;
    padding: .7rem;
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: .35rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .error { color: var(--bad); font-weight: 600; }
  @media (max-width: 700px) {
    table { display: block; overflow-x: auto; }
    #document-detail dl { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<header>
  <h1>PDF curation explorer</h1>
  <p class="scope">
    Read-only view over completed declared outputs. Counters are the measured
    <code>out/run-summary.json</code>; this read-only page never curates, deduplicates or publishes.
  </p>
  <p id="run-meta" class="meta" role="status" aria-live="polite">Loading run summary...</p>
</header>
<dl class="summary" id="summary" aria-label="Measured run summary">
  <div><dt>Corpus</dt><dd id="summary-corpus">-</dd></div>
  <div><dt>Routing</dt><dd id="summary-routing">-</dd></div>
  <div><dt>Extraction</dt><dd id="summary-extraction">-</dd></div>
  <div><dt>Published</dt><dd id="summary-published">-</dd></div>
  <div><dt>Exclusions</dt><dd id="summary-exclusions">-</dd></div>
  <div><dt>Elapsed (observed)</dt><dd id="summary-elapsed">not recorded</dd></div>
  <div><dt>Engine</dt><dd id="summary-engine">-</dd></div>
</dl>
<section class="filters" aria-label="Filters">
  <label for="filter-url">URL or text
    <input id="filter-url" type="search" placeholder="filter by URL, accepted text or reason">
  </label>
  <label for="filter-language">Language / script
    <select id="filter-language"><option value="">All</option></select>
  </label>
  <label for="filter-routing">Routing
    <select id="filter-routing"><option value="">All</option></select>
  </label>
  <label for="filter-kind">Status / exclusion kind
    <select id="filter-kind"><option value="">All</option></select>
  </label>
  <button id="refresh" type="button">Refresh now</button>
  <span id="refresh-state" class="meta" role="status" aria-live="polite"></span>
</section>
<section class="search" aria-label="Accepted evidence search">
  <form id="search-form" role="search">
    <label for="search-query">Accepted evidence search
      <input id="search-query" type="search" placeholder="words from accepted pages">
    </label>
    <label for="search-language">Language / script
      <input id="search-language" type="search" placeholder="any">
    </label>
    <label for="search-engine">Engine Identity
      <input id="search-engine" type="search" placeholder="any">
    </label>
    <label for="search-doc">Source Identity
      <input id="search-doc" type="search" placeholder="any" spellcheck="false">
    </label>
    <label for="search-top-k">Top K
      <input id="search-top-k" type="number" min="1" max="100" value="10">
    </label>
    <button id="search-submit" type="submit">Search</button>
  </form>
  <p id="search-state" class="meta" role="status" aria-live="polite">
    Search reads the local index built by finepdfs index-search after a completed run.
  </p>
  <div id="search-results"></div>
</section>
<main>
  <table id="status-table">
    <caption id="row-count">Loading declared documents...</caption>
    <thead>
      <tr>
        <th scope="col">Status</th>
        <th scope="col">URL</th>
        <th scope="col">Language / script</th>
        <th scope="col">Routing</th>
        <th scope="col">Engine</th>
        <th scope="col">Pages</th>
        <th scope="col">Reason</th>
      </tr>
    </thead>
    <tbody id="status-body"></tbody>
  </table>
  <section id="document-detail" aria-label="Document detail" hidden></section>
</main>
<script>
const state = { documents: [], summary: null, observed: null, finishedAt: "", selected: null };

const byId = (id) => document.getElementById(id);

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function formatCount(value) {
  return typeof value === "number" ? value.toLocaleString() : "-";
}

function renderSummary() {
  const summary = state.summary || {};
  const corpus = summary.corpus || {};
  const routing = summary.routing || {};
  const extraction = summary.extraction || {};
  const published = summary.published || {};
  const exclusions = summary.exclusions || {};
  const engine = summary.engine || {};
  const kinds = Object.entries(exclusions.by_kind || {})
    .filter(([, count]) => count)
    .map(([kind, count]) => kind + " " + formatCount(count))
    .join(", ") || "none";
  byId("summary-corpus").textContent =
    formatCount(corpus.crawl_records) + " crawl records / " + formatCount(corpus.documents) + " documents";
  byId("summary-routing").textContent =
    "native " + formatCount(routing.native) + ", ocr " + formatCount(routing.ocr);
  byId("summary-extraction").textContent =
    "calls " + formatCount(extraction.calls) + ", reused " + formatCount(extraction.reused);
  byId("summary-published").textContent =
    formatCount(published.documents) + " documents / " + formatCount(published.page_records) + " page records";
  byId("summary-exclusions").textContent = formatCount(exclusions.total) + " (" + kinds + ")";
  const observed = state.observed || {};
  byId("summary-elapsed").textContent = observed.recorded
    ? Number(observed.elapsed_seconds).toFixed(1) + " s (observed)"
    : "not recorded";
  byId("summary-engine").textContent = engine.identity || engine.name || "-";
  byId("run-meta").textContent = observed.recorded
    ? "Observed run finished at " + (observed.finished_at || "unknown") + " (wall clock, not declared "
      + "target state). This view polls that observation and reloads after a completed update."
    : "No run timing is recorded beside these outputs, so elapsed is not recorded. The declared summary "
      + "carries no timestamps: run again and the same bytes are rewritten.";
}

function fillSelect(select, values) {
  const current = select.value;
  select.replaceChildren(new Option("All", ""));
  values.filter(Boolean).sort().forEach((value) => select.add(new Option(value, value)));
  select.value = values.includes(current) ? current : "";
}

function refreshFilterOptions() {
  const unique = (field) => [...new Set(state.documents.map((doc) => doc[field]))];
  fillSelect(byId("filter-language"), unique("language"));
  fillSelect(byId("filter-routing"), unique("routing_decision"));
  const kinds = ["published", ...Object.keys((state.summary && state.summary.exclusions
    && state.summary.exclusions.by_kind) || {})];
  fillSelect(byId("filter-kind"), [...new Set(kinds)]);
}

function filteredDocuments() {
  const query = byId("filter-url").value.trim().toLowerCase();
  const language = byId("filter-language").value;
  const routing = byId("filter-routing").value;
  const kind = byId("filter-kind").value;
  return state.documents.filter((doc) => {
    if (language && doc.language !== language) return false;
    if (routing && doc.routing_decision !== routing) return false;
    if (kind && doc.status !== kind) return false;
    if (!query) return true;
    const haystack = [doc.url, doc.text, doc.routing_reason, doc.exclusion_reason, doc.engine, doc.language]
      .filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(query);
  });
}

function renderTable() {
  const rows = filteredDocuments();
  const body = byId("status-body");
  body.replaceChildren(...rows.map(rowFor));
  byId("row-count").textContent =
    rows.length + " of " + state.documents.length + " declared documents shown";
  markSelectedRow();
}

function rowFor(doc) {
  const tr = document.createElement("tr");
  tr.tabIndex = 0;
  tr.dataset.key = doc.key;
  tr.setAttribute("aria-label", "Inspect " + (doc.url || doc.key));
  tr.addEventListener("click", () => showDetail(doc.key));
  tr.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      showDetail(doc.key);
    }
  });
  const pages = doc.status === "published"
    ? formatCount(doc.accepted_pages) + " / " + formatCount(doc.page_count) + " accepted"
    : "-";
  tr.append(
    textNode("td", doc.status, "status status-" + doc.status),
    textNode("td", doc.url || doc.key, "url"),
    textNode("td", doc.language || "-"),
    textNode("td", doc.routing_decision || "-"),
    textNode("td", doc.engine || "-"),
    textNode("td", pages),
    textNode("td", doc.exclusion_reason || doc.routing_reason || "-")
  );
  return tr;
}

function markSelectedRow() {
  document.querySelectorAll("#status-body tr").forEach((row) => {
    row.classList.toggle("selected", row.dataset.key === state.selected);
  });
}

function pdfLink(label, href) {
  const link = document.createElement("a");
  link.textContent = label;
  if (typeof href === "string" && href.startsWith("/pdf/")) {
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener";
  }
  return link;
}

function renderDetail(detail) {
  const panel = byId("document-detail");
  panel.replaceChildren();
  panel.append(textNode("h2", detail.url || detail.key));
  const list = document.createElement("dl");
  const rows = [
    ["Source URL", detail.url || "-"],
    ["Status", detail.status || "-"],
    ["Language / script", detail.language || "not routed"],
    ["Engine Identity", detail.engine || "none (excluded before extraction)"],
    ["Routing", detail.routing_decision
      ? detail.routing_decision + ": " + (detail.routing_reason || "")
      : "not routed"],
    ["Page counts", detail.status === "published"
      ? detail.accepted_pages + " accepted, " + detail.dropped_pages + " dropped, "
        + detail.page_count + " original"
      : "no accepted pages"],
    ["Exclusion reason", detail.exclusion_reason || "-"],
  ];
  rows.forEach(([label, value]) => {
    list.append(textNode("dt", label), textNode("dd", value));
  });
  panel.append(list);
  if (detail.pdf_url) {
    const paragraph = document.createElement("p");
    paragraph.append(pdfLink("Open retained PDF from page 1", detail.pdf_url + "#page=1"));
    panel.append(paragraph);
  }
  const pages = Array.isArray(detail.pages) ? detail.pages : [];
  panel.append(textNode("h3", "Accepted pages (" + pages.length + ")"));
  if (!pages.length) {
    panel.append(textNode("p", "No accepted page export: this document was excluded."));
    return;
  }
  pages.forEach((page) => {
    const article = document.createElement("article");
    article.className = "page";
    const header = document.createElement("div");
    header.append(textNode("h4", "Page " + page.page_number));
    if (page.pdf_url) {
      header.append(pdfLink("Open PDF page " + page.page_number, page.pdf_url));
    }
    const text = document.createElement("pre");
    text.textContent = page.text || "";
    article.append(header, text);
    panel.append(article);
  });
}

async function showDetail(key) {
  try {
    const response = await fetch("/api/documents/" + encodeURIComponent(key), { cache: "no-store" });
    if (!response.ok) throw new Error("detail request failed with HTTP " + response.status);
    const detail = await response.json();
    state.selected = key;
    renderDetail(detail);
    byId("document-detail").hidden = false;
    markSelectedRow();
    byId("document-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setError(error);
  }
}

async function loadAll() {
  const response = await fetch("/api/documents", { cache: "no-store" });
  if (!response.ok) throw new Error("summary request failed with HTTP " + response.status);
  const payload = await response.json();
  state.documents = Array.isArray(payload.documents) ? payload.documents : [];
  state.summary = payload.summary || {};
  state.observed = await loadObserved();
  state.finishedAt = (state.observed && state.observed.recorded && state.observed.finished_at) || "";
  renderSummary();
  refreshFilterOptions();
  renderTable();
  if (state.selected) await showDetail(state.selected);
}

function setError(error) {
  const status = byId("refresh-state");
  status.textContent = "Error: " + (error && error.message ? error.message : error);
  status.classList.add("error");
}

async function loadObserved() {
  const response = await fetch("/api/observed-run", { cache: "no-store" });
  if (!response.ok) return { recorded: false };
  return await response.json();
}

async function refresh(reason) {
  const status = byId("refresh-state");
  status.classList.remove("error");
  try {
    await loadAll();
    if (reason === "manual") status.textContent = "Reloaded at " + new Date().toLocaleTimeString();
  } catch (error) {
    setError(error);
  }
}

function startPolling() {
  setInterval(async () => {
    try {
      const response = await fetch("/api/observed-run", { cache: "no-store" });
      if (!response.ok) return;
      const observed = await response.json();
      const finishedAt = (observed.recorded && observed.finished_at) || "";
      if (finishedAt && finishedAt !== state.finishedAt) {
        await loadAll();
        byId("refresh-state").textContent =
          "An observed run finished at " + finishedAt + " (wall clock, not declared state); the view refreshed.";
      }
    } catch (error) {
      setError(error);
    }
  }, 5000);
}

function snippetFragment(snippet) {
  const fragment = document.createDocumentFragment();
  const parts = String(snippet || "").split(new RegExp("<mark>|</mark>"));
  let highlighted = false;
  parts.forEach((part) => {
    if (part === "<mark>") { highlighted = true; return; }
    if (part === "</mark>") { highlighted = false; return; }
    if (part) fragment.append(textNode(highlighted ? "mark" : "span", part));
  });
  return fragment;
}

function searchHit(hit, index) {
  const article = document.createElement("article");
  article.className = "hit";
  article.append(textNode("h3", (index + 1) + ". " + hit.doc_id + " p." + hit.page_number
    + " (score " + Number(hit.score).toFixed(3) + ")"));
  article.append(textNode("p", hit.source_url + " · chunk " + hit.chunk_id + " · "
    + (hit.engine || "-") + " · " + (hit.language || "-"), "meta"));
  const snippet = document.createElement("p");
  snippet.className = "snippet";
  snippet.append(snippetFragment(hit.snippet));
  const details = document.createElement("details");
  details.append(textNode("summary", "Matched chunk text"));
  const text = document.createElement("pre");
  text.textContent = hit.text || "";
  details.append(text);
  const inspect = document.createElement("button");
  inspect.type = "button";
  inspect.className = "link";
  inspect.textContent = "Inspect " + hit.doc_id + " p." + hit.page_number;
  inspect.addEventListener("click", () => showDetail(hit.doc_id));
  article.append(snippet, details, inspect);
  return article;
}

function renderSearch(payload) {
  const state = byId("search-state");
  const results = byId("search-results");
  results.replaceChildren();
  state.classList.remove("error");
  if (!payload.available) {
    state.textContent = payload.reason || "Search is unavailable.";
    state.classList.add("error");
    return;
  }
  const hits = Array.isArray(payload.hits) ? payload.hits : [];
  state.textContent = hits.length
    ? hits.length + ' hit(s) for "' + payload.query + '" from accepted, current chunks'
    : 'No accepted evidence matched "' + payload.query + '".';
  hits.forEach((hit, index) => results.append(searchHit(hit, index)));
}

async function searchEvidence(event) {
  event.preventDefault();
  const query = byId("search-query").value.trim();
  const state = byId("search-state");
  if (!query) {
    state.textContent = "Type words to search accepted evidence.";
    return;
  }
  const params = new URLSearchParams({ query, top_k: byId("search-top-k").value || "10" });
  [["language", "search-language"], ["engine", "search-engine"], ["doc_id", "search-doc"]]
    .forEach(([name, id]) => {
      const value = byId(id).value.trim();
      if (value) params.set(name, value);
    });
  state.classList.remove("error");
  state.textContent = "Searching accepted evidence...";
  try {
    const response = await fetch("/api/search?" + params.toString(), { cache: "no-store" });
    if (!response.ok) throw new Error("search failed with HTTP " + response.status);
    renderSearch(await response.json());
  } catch (error) {
    state.textContent = "Error: " + (error && error.message ? error.message : error);
    state.classList.add("error");
  }
}

["filter-url", "filter-language", "filter-routing", "filter-kind"].forEach((id) => {
  byId(id).addEventListener("input", renderTable);
});
byId("search-form").addEventListener("submit", searchEvidence);
byId("refresh").addEventListener("click", () => refresh("manual"));
refresh("initial");
startPolling();
</script>
</body>
</html>
"""
