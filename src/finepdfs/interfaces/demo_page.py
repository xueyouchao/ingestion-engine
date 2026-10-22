"""The live demo's page asset: one self-contained HTML document with its styles and its script.

The page is a view over five answers -- the status, the upload's job id, the job itself, the job's raw
transcript and the job's rendered document -- and it keeps no state beyond the id it is polling, the
transcript it is streaming and the file name its download is called after. So it lives beside the queue
and the server rather than inside either. No framework and no build step: the server hands the browser
this string as it is, and the browser needs nothing else.

What the page may show is the whole contract: the limits and the remaining budget before the upload
control, the two statements about where the bytes go and what is kept, the stage timeline with the
measured warm-up, the decision with its five features and the thresholds they were measured against, the
Engine Identity, the refusal and failure panels, and three panes -- what was sent, what came back and what
would be published. The middle pane is the raw transcript the page streams from
``/api/read/<id>/events``: the worker's own bytes, set with ``textContent`` and never parsed as HTML. The
right pane is the server's rendered Page Texts from ``/api/read/<id>/document``, already sanitized;
``insertDocument`` is the one place in this asset that inserts HTML. Its copy button and ``.md`` download
are the answer at ``/api/read/<id>/document.md``. It never receives the bearer or the worker's address, so
it cannot show them.
"""

from __future__ import annotations

DEMO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Read your own document</title>
<style>
  /* The same palette as the reader bundle the live demo is linked from (bundle_page.py), so the two
     pages read as one site: warm paper, thin warm rules, one green accent. The status colours are the
     only additions -- a refusal, a failure and a healthy read have to differ from the accent. */
  :root {
    color-scheme: light dark;
    --bg: #fbfaf7;
    --panel: var(--bg);
    --ink: #1b1a17;
    --muted: #6b675e;
    --line: #d9d4c8;
    --accent: #0b6b4f;
    /* The label colour a filled control needs; the accent is too light to carry white in dark mode. */
    --on-accent: #ffffff;
    --warn: #8a4b00;
    --bad: #9c2b1c;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #141412;
      --panel: var(--bg);
      --ink: #eeeae0;
      --muted: #a09a8c;
      --line: #33312c;
      --accent: #5fd3a6;
      --on-accent: #10201a;
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
    font: 15px/1.5 ui-sans-serif, system-ui, sans-serif;
  }
  header .back { margin: 0 0 .5rem; min-height: 1.2em; }
  header .back a { color: var(--muted); text-decoration: none; font-size: .85rem; }
  header .back a:hover { color: var(--ink); }
  header h1 { margin: 0 0 .25rem; font-size: 1.65rem; }
  header p { margin: .2rem 0; color: var(--muted); }
  section, .panel {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .55rem;
    padding: .9rem 1rem;
    margin: 1rem 0;
  }
  h2 { margin: 0 0 .55rem; font-size: 1.05rem; }
  h3 { margin: .8rem 0 .35rem; font-size: .95rem; color: var(--muted); }
  .statements p { margin: .3rem 0; }
  .statements strong { color: var(--accent); }
  .limits {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: .55rem;
    margin: 0;
  }
  .limits div { background: var(--bg); border: 1px solid var(--line); border-radius: .4rem; padding: .5rem .65rem; }
  .limits dt { color: var(--muted); font-size: .76rem; text-transform: uppercase; letter-spacing: .04em; }
  .limits dd { margin: .15rem 0 0; font-weight: 600; overflow-wrap: anywhere; }
  .upload { display: flex; flex-wrap: wrap; gap: .7rem; align-items: center; }
  .upload input[type=file] { max-width: 100%; }
  button {
    font: inherit;
    padding: .45rem .9rem;
    border-radius: .4rem;
    border: 1px solid var(--accent);
    background: var(--accent);
    color: var(--on-accent);
    cursor: pointer;
  }
  button:disabled { opacity: .55; cursor: progress; }
  .note { color: var(--muted); font-size: .85rem; margin: .5rem 0 0; }
  .timeline { display: flex; flex-wrap: wrap; gap: .45rem; list-style: none; margin: 0; padding: 0; }
  .timeline li {
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: .2rem .75rem;
    color: var(--muted);
    font-size: .88rem;
  }
  .timeline li[data-state=active] { border-color: var(--accent); color: var(--accent); font-weight: 600; }
  /* A finished stage recedes and is ticked: the accent's own green would read as "in progress", and no
     second green sits far enough from it to tell the two apart at a glance (WCAG 1.4.11 wants ~3:1
     between colours that carry meaning, which two greens of one hue cannot give). One colour for the
     current stage, grey and a glyph for the ones behind it. */
  .timeline li[data-state=done] { border-color: var(--line); color: var(--muted); }
  .timeline li[data-state=done]::before { content: "✓  "; }
  .timeline li[data-state=skipped] { opacity: .55; text-decoration: line-through; }
  .warm { margin: .6rem 0 0; color: var(--muted); font-size: .9rem; }
  .warm strong { color: var(--warn); }
  .lane { font-size: 1.2rem; font-weight: 700; margin: 0; }
  .reason { margin: .3rem 0 .8rem; }
  table { width: 100%; border-collapse: collapse; font-size: .9rem; }
  th, td { text-align: left; padding: .3rem .45rem; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; }
  td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
  .engine { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
  /* The page is three panes -- what was sent, what came back, what would be published. They stack in
     that order on a narrow screen; only a wide enough one puts them side by side, so a phone reads the
     same three sentences top to bottom rather than in a squeezed row. */
  .panes { display: grid; grid-template-columns: 1fr; gap: 1rem; margin: 1rem 0; align-items: start; }
  .panes > section { margin: 0; }
  @media (min-width: 60rem) {
    .panes { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.15fr); }
  }
  /* Both panes carry the same copy/download row: one rule for the two, since they differ in what they
     show and not in how their controls look. */
  .raw-controls, .document-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: .6rem;
    margin: .1rem 0 .55rem;
  }
  .raw-controls button, .document-controls button { padding: .3rem .7rem; font-size: .85rem; }
  /* The rendered pane is the product: the document's own typography, not the transcribed look of the
     raw transcript pane. Every element here is in the server's allowlist; nothing is styled that cannot
     arrive. */
  #document { overflow-x: auto; }
  .document-page { border: 1px solid var(--line); border-radius: .4rem; padding: .65rem .75rem; margin: .7rem 0; }
  .document-page-number {
    margin: 0 0 .4rem;
    color: var(--muted);
    font-size: .8rem;
    text-transform: uppercase;
    letter-spacing: .04em;
  }
  .document-page pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    overflow: auto;
    padding: .5rem;
    background: var(--bg);
    border-radius: .3rem;
  }
  .document-page blockquote {
    margin: .5rem 0;
    padding-left: .7rem;
    border-left: 2px solid var(--line);
    color: var(--muted);
  }
  .skeleton, .document-empty, .unread { color: var(--muted); font-style: italic; }
  /* The raw transcript pane is the worker's own output, so it is monospaced and scrollable: markers and
     broken table syntax must look unlike the published page the right-hand pane will carry. */
  .raw-page { border: 1px solid var(--line); border-radius: .4rem; padding: .55rem .7rem; margin: .55rem 0; }
  .raw-page header { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
  .raw-truncated { color: var(--warn); font-size: .85rem; margin: .35rem 0 .1rem; }
  .raw-text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    margin: .35rem 0 0;
    padding: .5rem;
    max-height: 30rem;
    overflow: auto;
    background: var(--bg);
    border-radius: .3rem;
    font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .refusal { border-color: var(--bad); }
  .refusal h2, .failure h2 { color: var(--bad); }
  .label { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
  .hidden { display: none; }
  footer { color: var(--muted); font-size: .82rem; margin: 1.4rem 0 .4rem; }
</style>
</head>
<body>
<header>
  <p class="back">{{back}}</p>
  <h1>Read your own document</h1>
  <p>Upload a document &mdash; a PDF, or a scan as PNG, JPEG or TIFF &mdash; and watch the pipeline decide
  how to read it, then read it.</p>
</header>

<section class="statements" id="statements">
  <p><strong>Where your bytes go:</strong> when the decision chooses the OCR lane, the page images are
  sent to whichever OCR worker the demo is wired to &mdash; the deployed one on a rented GPU, or an engine
  this host runs when the demo is started with one &mdash; and read there.</p>
  <p><strong>What is kept:</strong> nothing is kept after the response &mdash; no uploaded file, no page
  image, no extracted text and no job state across a restart.</p>
</section>

<section id="limits">
  <h2>The demo's limits</h2>
  <dl class="limits" id="limits-list">
    <div><dt>pages per upload</dt><dd id="limit-page-cap">&hellip;</dd></div>
    <div><dt>largest upload</dt><dd id="limit-upload-bytes">&hellip;</dd></div>
    <div><dt>reads per client</dt><dd id="limit-rate">&hellip;</dd></div>
    <div><dt>OCR pages left today</dt><dd id="limit-budget">&hellip;</dd></div>
    <div><dt>waiting behind another read</dt><dd id="limit-queue">&hellip;</dd></div>
    <div><dt>cold worker wait</dt><dd id="limit-warm">&hellip;</dd></div>
  </dl>
  <p class="note" id="limits-note">These are checked before a read starts, so a refusal is never a surprise.</p>
</section>

<div class="panes">
<section id="input-pane" class="pane" role="region" aria-labelledby="input-heading">
  <h2 id="input-heading">1. What you sent</h2>
  <form class="upload" id="upload-form">
    <input type="file" id="upload-file" accept="application/pdf,image/png,image/jpeg,image/tiff" required>
    <button type="submit" id="upload-button">Read this document</button>
  </form>
  <p class="note" id="upload-note">One document at a time. A scan is read as a page, so it goes to the OCR
  lane &mdash; a three-page TIFF is three pages, and the page cap counts them. The server answers a job and
  this page polls it.</p>
  <div id="timeline">
    <h3>Stage</h3>
    <ol class="timeline" id="timeline-stages">
      <li data-stage="queued">queued</li>
      <li data-stage="routing">routing</li>
      <li data-stage="warming">warming the OCR worker</li>
      <li data-stage="reading">reading the pages</li>
    </ol>
    <p class="warm" id="warm-measured">The warm-up is reported as the number that was measured.</p>
  </div>
</section>

<section id="raw" class="pane hidden" role="region" aria-labelledby="raw-heading">
  <h2 id="raw-heading">2. What came back: the raw transcript</h2>
  <p class="note" id="raw-note" role="status">The worker's own output, before any normalization.</p>
  <div class="raw-controls">
    <button type="button" id="raw-copy" disabled>Copy transcript</button>
    <span class="note" id="raw-copy-note"></span>
  </div>
  <div id="raw-transcript" role="log" aria-live="off" aria-label="Raw transcript"></div>
</section>

<section id="document-pane" class="pane hidden" role="region" aria-labelledby="document-heading">
  <h2 id="document-heading">3. What would be published: the rendered document</h2>
  <p class="note" id="document-note" role="status"></p>
  <div class="document-controls">
    <button type="button" id="document-copy" disabled>Copy document</button>
    <button type="button" id="document-download" disabled>Download .md</button>
    <span class="note" id="document-action-note"></span>
  </div>
  <div id="document" aria-label="Rendered document"></div>
</section>
</div>

<section id="decision" class="hidden">
  <h2>The decision</h2>
  <p class="lane" id="decision-lane"></p>
  <p class="reason" id="decision-reason"></p>
  <table>
    <thead><tr><th>feature</th><th>measured</th><th>threshold</th></tr></thead>
    <tbody id="feature-rows"></tbody>
  </table>
</section>

<section id="engine-identity" class="hidden">
  <h2>Engine Identity</h2>
  <p class="engine" id="engine-value"></p>
</section>

<section id="refusal" class="panel refusal hidden">
  <h2>Refused: <span id="refusal-kind"></span></h2>
  <p class="label">why the demo refused it</p>
  <p id="refusal-reason"></p>
</section>

<section id="failure" class="panel failure hidden">
  <h2>That read failed</h2>
  <p class="label">lane</p>
  <p id="failure-lane"></p>
  <p class="label">the error's own words</p>
  <p id="failure-reason"></p>
</section>

<p class="note hidden" id="error"></p>
<footer>The demo shows what the model returned; it does not attest that the text is right.</footer>

<script>
"use strict";

const STAGES = ["queued", "routing", "warming", "reading"];
const FEATURES = [
  { key: "page_count", label: "pages", threshold: null },
  { key: "characters_per_page", label: "characters per page", threshold: "min_characters_per_page", atLeast: true },
  { key: "text_coverage", label: "share of pages with text", threshold: "min_text_coverage", atLeast: true },
  { key: "garbled_ratio", label: "garbled character ratio", threshold: "max_garbled_ratio", atLeast: false },
  {
    key: "image_dominated_share",
    label: "share of image-dominated pages",
    threshold: "max_image_dominated_share",
    atLeast: false,
  },
];

const byId = (id) => document.getElementById(id);
const state = {
  job: null,
  seen: new Set(),
  timer: null,
  raw: null,
  rawJob: null,
  events: null,
  document: null,
  fileName: "",
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function num(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "\\u2014";
  return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, "").replace(/\\.$/, "");
}

function bytes(value) {
  if (value >= 1024 * 1024) return (value / (1024 * 1024)).toFixed(1).replace(/\\.0$/, "") + " MiB";
  if (value >= 1024) return (value / 1024).toFixed(1).replace(/\\.0$/, "") + " KiB";
  return value + " bytes";
}

function seconds(value) {
  if (value >= 60 && value % 60 === 0) return value / 60 + (value === 60 ? " minute" : " minutes");
  return value + (value === 1 ? " second" : " seconds");
}

async function loadStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("status answered " + response.status);
    renderStatus(await response.json());
  } catch (error) {
    showError("could not read the demo's limits: " + error.message);
  }
}

function renderStatus(status) {
  const limits = status.limits;
  byId("limit-page-cap").textContent = limits.page_cap;
  byId("limit-upload-bytes").textContent = bytes(limits.upload_bytes);
  byId("limit-rate").textContent =
    limits.rate_limit_reads + " per " + seconds(limits.rate_limit_window_seconds);
  byId("limit-budget").textContent = status.ocr_pages_remaining + " of " + limits.daily_ocr_page_budget;
  byId("limit-queue").textContent = limits.queue_depth + " document" + (limits.queue_depth === 1 ? "" : "s");
  byId("limit-warm").textContent = "up to " + seconds(limits.warm_timeout_seconds);
  const depth = status.queue.active + status.queue.waiting;
  byId("limits-note").textContent = status.ocr_endpoint_configured
    ? "OCR is wired to a worker; " + depth + " read" + (depth === 1 ? "" : "s") + " in the queue right now."
    : "No OCR worker is wired, so a document that needs OCR will be reported as a failed read.";
}

function renderTimeline(stage, outcome, warmMs) {
  state.seen.add(stage);
  const done = stage === "done";
  const current = done ? STAGES.length : STAGES.indexOf(stage) + 1;
  for (const item of byId("timeline-stages").children) {
    const index = STAGES.indexOf(item.dataset.stage);
    const seen = state.seen.has(item.dataset.stage);
    let mark = "pending";
    // A finished job marks the stages it really reached: a native read never warms anything, and saying
    // otherwise on the page would claim a wait that did not happen.
    if (done) mark = seen ? "done" : "skipped";
    else if (index === current - 1) mark = "active";
    else if (index < current - 1 || seen) mark = "done";
    item.dataset.state = mark;
  }
  if (outcome === "read") {
    byId("warm-measured").textContent =
      warmMs === null || warmMs === undefined
        ? "Warm-up: none \u2014 the text layer needed no worker."
        : "Warm-up: " + (warmMs / 1000).toFixed(1) + " s measured, from the first readiness question.";
  } else if (stage === "done") {
    byId("warm-measured").textContent = "";
  } else {
    byId("warm-measured").textContent = stage === "warming" ? "Waiting for the cold OCR worker to answer\u2026" : "";
  }
}

function showError(message) {
  const panel = byId("error");
  panel.textContent = message;
  panel.classList.remove("hidden");
}

function hideResults() {
  for (const id of ["decision", "engine-identity", "document-pane", "refusal", "failure"]) {
    byId(id).classList.add("hidden");
  }
  byId("error").classList.add("hidden");
}

// -- the raw transcript (ADR-0014) -----------------------------------------------------------------
//
// The middle pane is the worker's own output, escaped and never parsed: every function here that
// touches the DOM only ever assigns ``textContent`` or ``replaceChildren``. Its entire state is the
// event stream read from sequence zero, so a reloaded page replays what the server retained instead
// of showing a delta this page happened to remember.

function rawModel() {
  return { deltas: [], dropped: {}, failure: null, outcome: null, finished: false, available: true };
}

function applyRawEvent(model, name, data) {
  if (name === "delta") {
    model.deltas.push({ page: data.page, text: String(data.text) });
  } else if (name === "truncated") {
    model.dropped[data.page] = data.dropped_bytes;
  } else if (name === "error") {
    model.failure = { lane: String(data.lane || ""), reason: String(data.reason || "") };
  } else if (name === "done") {
    model.finished = true;
    model.outcome = data.outcome === undefined ? null : data.outcome;
  }
}

function rawPages(model) {
  const pages = new Map();
  const at = (number) => {
    if (!pages.has(number)) pages.set(number, { number: number, text: "", dropped: 0 });
    return pages.get(number);
  };
  for (const delta of model.deltas) at(delta.page).text += delta.text;
  for (const number of Object.keys(model.dropped)) at(Number(number)).dropped = model.dropped[number];
  return [...pages.values()].sort((left, right) => left.number - right.number);
}

function rawText(model) {
  // The copy button copies the stream, in arrival order: page labels and separators would be this
  // page's invention, not the worker's output.
  return model.deltas.map((delta) => delta.text).join("");
}

function rawNote(model) {
  if (model.deltas.length) {
    if (!model.finished) return "Streaming this lane's raw output as it arrives. Nothing here is parsed as HTML.";
    return model.failure
      ? "The raw output this lane produced before it failed, byte for byte, before any normalization."
      : "The raw output this lane produced, byte for byte, before any normalization.";
  }
  if (!model.finished) {
    return model.available
      ? "Waiting for this lane's first raw output."
      : "The raw transcript is no longer kept for this job.";
  }
  if (model.outcome === "refused") {
    return "No lane read this upload, so this lane does not stream.";
  }
  if (model.outcome === "failed") {
    return "The read failed before any raw output arrived; the failure panel names the lane and the reason.";
  }
  return "This lane does not stream: it returns a page whole, with no raw output to show.";
}

function rawTruncation(page, dropped) {
  return "Page " + page + ": the transcript dropped the oldest " + dropped +
    " bytes of this page; the most recent output is what remains below.";
}

function renderRaw(model) {
  byId("raw").classList.remove("hidden");
  byId("raw-note").textContent = rawNote(model);
  byId("raw-copy").disabled = rawText(model).length === 0;
  const container = byId("raw-transcript");
  container.setAttribute("aria-busy", model.finished ? "false" : "true");
  container.replaceChildren();
  for (const page of rawPages(model)) {
    const article = node("article", null, "raw-page");
    article.dataset.page = String(page.number);
    const head = document.createElement("header");
    head.textContent = "page " + page.number;
    article.append(head);
    if (page.dropped > 0) {
      article.append(node("p", rawTruncation(page.number, page.dropped), "raw-truncated"));
    }
    article.append(node("pre", page.text, "raw-text"));
    container.append(article);
  }
}

function closeRawStream() {
  if (state.events !== null) {
    state.events.close();
    state.events = null;
  }
}

function openRawStream(jobId) {
  // One stream per job: a new upload closes the old one before it asks for another, and ``rawJob``
  // makes a late handler from the old job harmless if one still fires.
  closeRawStream();
  state.raw = rawModel();
  state.rawJob = jobId;
  renderRaw(state.raw);
  const source = new EventSource("/api/read/" + encodeURIComponent(jobId) + "/events");
  state.events = source;
  const apply = (event) => {
    if (state.rawJob !== jobId) return;
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    applyRawEvent(state.raw, event.type, data);
    renderRaw(state.raw);
  };
  for (const name of ["delta", "truncated", "page"]) source.addEventListener(name, apply);
  source.addEventListener("error", (event) => {
    // The server's own ``error`` event carries the read's words; the EventSource failure has no data
    // and means this endpoint will answer nothing more for the job (a finished job frees its transcript).
    if (event.data) {
      apply(event);
      return;
    }
    closeRawStream();
    state.raw.available = false;
    renderRaw(state.raw);
  });
  source.addEventListener("done", (event) => {
    apply(event);
    closeRawStream();
  });
}

function settleRaw(job, jobId) {
  // A text-layer read can finish before this page attaches, and the events endpoint then answers 410.
  // The poll's terminal answer is the remaining evidence: a finished read that streamed no delta is a
  // lane with nothing to stream, never a stream this page is still waiting on.
  if (job.stage !== "done" || state.raw === null || state.rawJob !== jobId) return;
  if (!state.raw.finished) {
    state.raw.finished = true;
    state.raw.outcome = job.outcome;
  }
  renderRaw(state.raw);
}

async function copyRaw() {
  if (state.raw === null) return;
  const text = rawText(state.raw);
  if (!text) return;
  const note = byId("raw-copy-note");
  try {
    await navigator.clipboard.writeText(text);
    note.textContent = "Copied " + text.length + " characters.";
  } catch (error) {
    note.textContent = "Copy failed; select the transcript text and copy it by hand.";
  }
}

// -- the rendered document (ADR-0014) ---------------------------------------------------------------
//
// The right pane is the product: the server renders Page Text and sanitizes it, and this page inserts
// that one answer. ``insertDocument`` is the only place in this asset that assigns HTML -- every other
// pane, and every note here, is written with ``textContent``. The fragment carries the page numbers,
// their numeric order and the skeleton for a page that has not completed, so this pane orders and
// labels nothing itself: it shows the server's answer, again, each time the job poll ticks.

function documentModel(jobId) {
  return { job: jobId, completed: 0, total: null, finished: false, failed: false, absent: false };
}

function documentNote(model) {
  // The counts are the server's, said in words beside the fragment: the pane adds no status of its own.
  const completed = model.completed;
  const total = model.total;
  const counted =
    total === null || total === undefined
      ? completed + (completed === 1 ? " page has" : " pages have")
      : completed + " of " + total + (total === 1 ? " page has" : " pages have");
  if (model.failed) {
    // A failed read read nothing more: the fragment names each page it never reached as not read, and
    // the note must not call what is left a document the pipeline would publish.
    return counted + " completed. The read failed, so the pages that never completed were not read; " +
      "what is here is the Page Text of the pages that were read.";
  }
  if (model.finished) {
    return counted + " completed. This is the document the pipeline would publish; the raw transcript " +
      "beside it is what the worker emitted.";
  }
  if (completed === 0) {
    return "No page has completed yet. Each page appears, in its own 1-based number, as the lane finishes it.";
  }
  return counted + " completed; a page still being read shows a skeleton, and its own page replaces " +
    "it when it completes.";
}

function documentAbsent() {
  // The 503 answer says the [demo] extra is missing. The pane says so in these words -- never the
  // server's sentence dropped into markup -- and points at what still works.
  return "Rendering is not installed on this server, so this pane cannot show the rendered document. " +
    "The document's markdown is still downloadable, and the raw transcript beside it is the worker's own output.";
}

function documentDownloadName(fileName) {
  // The download is named after the document the visitor uploaded; the browser hands this page the
  // file's base name, so the extension is the only thing this page changes.
  const name = String(fileName || "");
  if (!name) return "document.md";
  const dot = name.lastIndexOf(".");
  return (dot > 0 ? name.slice(0, dot) : name) + ".md";
}

function insertDocument(container, fragment) {
  // The one place this page turns an answer into HTML. The server rendered the Page Texts and
  // sanitized them against ADR-0014's allowlist; this page neither orders nor escapes nor repairs it.
  container.innerHTML = fragment;
}

function setDocumentNote(text) {
  byId("document-note").textContent = text;
}

function setDocumentControls(enabled) {
  byId("document-copy").disabled = !enabled;
  byId("document-download").disabled = !enabled;
}

async function documentMarkdown() {
  // Both the copy button and the download use the same answer: the completed pages as the corpus's one
  // projection. It needs no optional dependency, so it still works when rendering is absent.
  const note = byId("document-action-note");
  const jobId = state.document === null ? null : state.document.job;
  if (jobId === null) return null;
  let response;
  try {
    response = await fetch("/api/read/" + encodeURIComponent(jobId) + "/document.md", { cache: "no-store" });
  } catch (error) {
    note.textContent = "The document's markdown could not be read: " + error.message;
    return null;
  }
  if (!response.ok) {
    note.textContent = "The document's markdown answered " + response.status + ".";
    return null;
  }
  return await response.text();
}

async function copyDocument() {
  const text = await documentMarkdown();
  if (text === null) return;
  const note = byId("document-action-note");
  if (!text) {
    note.textContent = "No page has completed yet, so there is nothing to copy.";
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    note.textContent = "Copied " + text.length + " characters of the document's markdown.";
  } catch (error) {
    note.textContent = "Copy failed; select the document text and copy it by hand.";
  }
}

async function downloadDocument() {
  const text = await documentMarkdown();
  if (text === null) return;
  const note = byId("document-action-note");
  if (!text) {
    note.textContent = "No page has completed yet, so there is nothing to download.";
    return;
  }
  const name = documentDownloadName(state.fileName);
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  // The browser has started reading the blob by now; revoking at once can cancel it in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  note.textContent = "Downloaded " + name + " (" + text.length + " characters).";
}

async function refreshDocument(jobId) {
  // The pane is asked again on the job poll's own tick, so a skeleton becomes its page as soon as that
  // page completes. The fragment is replaced whole: its page numbers, order and states are the
  // server's answer, not an accumulation this page keeps.
  if (state.document === null || state.document.job !== jobId) return;
  let response;
  try {
    response = await fetch("/api/read/" + encodeURIComponent(jobId) + "/document", { cache: "no-store" });
  } catch (error) {
    setDocumentNote("The document pane could not be read: " + error.message);
    return;
  }
  if (state.document === null || state.document.job !== jobId) return;
  byId("document-pane").classList.remove("hidden");
  if (response.status === 503) {
    state.document.absent = true;
    byId("document").replaceChildren();
    setDocumentNote(documentAbsent());
    setDocumentControls(true);
    return;
  }
  if (!response.ok) {
    setDocumentNote("The document answered " + response.status + ".");
    return;
  }
  const fragment = await response.text();
  if (state.document === null || state.document.job !== jobId) return;
  state.document.completed = Number(response.headers.get("X-Document-Completed") || 0);
  const total = response.headers.get("X-Document-Pages");
  state.document.total = total === null ? null : Number(total);
  insertDocument(byId("document"), fragment);
  setDocumentNote(documentNote(state.document));
  setDocumentControls(true);
}

function renderRefusal(refusal) {
  hideResults();
  byId("refusal-kind").textContent = refusal.kind;
  byId("refusal-reason").textContent = refusal.reason;
  byId("refusal").classList.remove("hidden");
}

function renderFailure(failure) {
  hideResults();
  byId("failure-lane").textContent = failure.lane || "no lane was chosen";
  byId("failure-reason").textContent = failure.reason;
  byId("failure").classList.remove("hidden");
}

function renderFeatureRows(reading) {
  const body = byId("feature-rows");
  body.replaceChildren();
  for (const feature of FEATURES) {
    const row = document.createElement("tr");
    row.append(node("td", feature.label), node("td", num(reading.features[feature.key]), "num"));
    let compared = "\\u2014";
    if (feature.threshold) {
      const bound = reading.thresholds[feature.threshold];
      compared = (feature.atLeast ? "at least " : "at most ") + num(bound);
    }
    row.append(node("td", compared, "num"));
    body.append(row);
  }
}

function renderReading(reading) {
  hideResults();
  byId("decision").classList.remove("hidden");
  byId("decision-lane").textContent = reading.lane === "ocr" ? "OCR lane" : "Text-layer lane";
  byId("decision-reason").textContent = reading.reason;
  renderFeatureRows(reading);
  byId("engine-identity").classList.remove("hidden");
  byId("engine-value").textContent = reading.engine;
}

function renderJob(job, jobId) {
  renderTimeline(job.stage, job.outcome, job.warmed_ms);
  settleRaw(job, jobId);
  if (job.stage !== "done") {
    // The job poll's own tick is the document pane's tick: a skeleton is replaced by its page as soon
    // as that page completes (ADR-0014), not only when the whole read ends.
    refreshDocument(jobId);
    return;
  }
  stopPolling();
  byId("upload-button").disabled = false;
  // An OCR read charges the day's budget, so the standing limits are stale the moment a job finishes.
  loadStatus();
  if (job.outcome === "refused") {
    // Nothing was read, so there is no document to show; the refusal panel is the whole answer.
    state.document = null;
    renderRefusal(job.refusal);
    return;
  }
  if (state.document !== null) {
    // What the end of the job was, before the last ask: a failed read's pane names what it never read.
    state.document.finished = true;
    state.document.failed = job.outcome === "failed";
  }
  // One last ask, so the finished Reading -- not the last mid-read view -- is what the visitor sees.
  refreshDocument(jobId);
  if (job.outcome === "read") renderReading(job.reading);
  else if (job.outcome === "failed") renderFailure(job.failure);
  else showError("the job finished without an outcome");
}

function stopPolling() {
  if (state.timer !== null) {
    clearTimeout(state.timer);
    state.timer = null;
  }
}

function poll(jobId) {
  state.job = jobId;
  state.document = documentModel(jobId);
  stopPolling();
  const ask = async () => {
    try {
      const response = await fetch("/api/read/" + encodeURIComponent(jobId), { cache: "no-store" });
      if (response.status === 404) throw new Error("the server no longer knows this job");
      if (!response.ok) throw new Error("the job answered " + response.status);
      const job = await response.json();
      renderJob(job, jobId);
      if (job.stage !== "done") state.timer = setTimeout(ask, 900);
    } catch (error) {
      stopPolling();
      byId("upload-button").disabled = false;
      showError("the poll failed: " + error.message);
    }
  };
  ask();
}

// What the server admits, kept beside the input's ``accept`` list. A browser or an OS can report a type
// this demo does not admit for a file it does -- ``application/octet-stream`` for a .tif on Windows,
// ``image/jpg`` or ``image/x-tiff`` -- and sending that would earn a 400 for a scan the server can read.
// So the browser's own type is used only when the server admits it, and the extension decides otherwise.
const ADMITTED_TYPES = ["application/pdf", "image/png", "image/jpeg", "image/tiff"];

function guessType(name) {
  // What the extension says, when this page knows it. ``null`` means "no idea", so nothing is ever
  // uploaded claiming to be a PDF by default -- a .webp or a .docx would then reach the reader as a
  // broken PDF and be reported as unreadable instead of being refused for what it is.
  const lower = (name || "").toLowerCase();
  if (lower.endsWith(".pdf")) return "application/pdf";
  if (lower.endsWith(".png")) return "image/png";
  if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
  if (lower.endsWith(".tif") || lower.endsWith(".tiff")) return "image/tiff";
  return null;
}

function uploadType(file) {
  // The browser's own type when the server admits it; the extension when it does not (``image/jpg``,
  // ``application/octet-stream`` for a .tif); and then the browser's answer again, so the refusal names
  // the type the file really is rather than a guess. Never ``null``: a header cannot be undefined.
  if (ADMITTED_TYPES.includes(file.type)) return file.type;
  return guessType(file.name) || file.type || "application/octet-stream";
}

function uploadHeaders(file) {
  // No name header when the file has none: the server then names it for its media type (``upload.png``),
  // which is truer than a bare ``upload`` with no extension.
  const headers = { "Content-Type": uploadType(file) };
  // Percent-encoded: a header value is a ByteString, so one character above U+00FF -- a Chinese or
  // Japanese name, an emoji from a phone camera roll -- makes fetch throw before a byte is sent. The
  // server decodes it again, so the job still shows the visitor's own name.
  if (file.name) headers["X-Upload-Name"] = encodeURIComponent(file.name);
  return headers;
}

async function upload(file) {
  hideResults();
  closeRawStream();
  state.raw = null;
  state.rawJob = null;
  state.document = null;
  // The download is named after the document the visitor chose; the job answer is not asked for it.
  state.fileName = file.name || "";
  byId("raw").classList.add("hidden");
  byId("raw-copy-note").textContent = "";
  byId("document-pane").classList.add("hidden");
  byId("document").replaceChildren();
  byId("document-note").textContent = "";
  byId("document-action-note").textContent = "";
  setDocumentControls(false);
  state.seen = new Set();
  byId("upload-button").disabled = true;
  renderTimeline("queued", null, null);
  try {
    const response = await fetch("/api/read", {
      method: "POST",
      headers: uploadHeaders(file),
      body: file,
    });
    const payload = await response.json();
    if (response.status === 202) {
      poll(payload.job);
      openRawStream(payload.job);
      return;
    }
    byId("upload-button").disabled = false;
    if (payload.refusal) renderRefusal(payload.refusal);
    else showError(payload.error || "the upload was refused");
  } catch (error) {
    byId("upload-button").disabled = false;
    showError("the upload failed: " + error.message);
  }
}

byId("upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const file = byId("upload-file").files[0];
  if (!file) {
    showError("choose a document first");
    return;
  }
  upload(file);
});

byId("raw-copy").addEventListener("click", copyRaw);
byId("document-copy").addEventListener("click", copyDocument);
byId("document-download").addEventListener("click", downloadDocument);

loadStatus();
</script>
</body>
</html>
"""
