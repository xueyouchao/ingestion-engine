"""The live demo's page asset: one self-contained HTML document with its styles and its script.

The page is a view over three answers -- the status, the upload's job id and the job itself -- and it
keeps no state of its own beyond the id it is polling. So it lives beside the queue and the server
rather than inside either. No framework and no build step: the server hands the browser this string as
it is, and the browser needs nothing else.

What the page may show is the whole contract: the limits and the remaining budget before the upload
control, the two statements about where the bytes go and what is kept, the stage timeline with the
measured warm-up, the decision with its five features and the thresholds they were measured against, the
Engine Identity, the per-page text with its original page number, and the refusal and failure panels. It
never receives the bearer or the worker's address, so it cannot show them.
"""

from __future__ import annotations

DEMO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Read your own PDF</title>
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
    max-width: 980px;
    padding: 1.25rem;
    background: var(--bg);
    color: var(--ink);
    font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
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
    color: #ffffff;
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
  .timeline li[data-state=done] { border-color: var(--ok); color: var(--ok); }
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
  .page { border: 1px solid var(--line); border-radius: .4rem; padding: .65rem .75rem; margin: .7rem 0; }
  .page header { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
  .page pre { white-space: pre-wrap; word-wrap: break-word; margin: .4rem 0 0; font: inherit; }
  .refusal { border-color: var(--bad); }
  .refusal h2, .failure h2 { color: var(--bad); }
  .label { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
  .hidden { display: none; }
  footer { color: var(--muted); font-size: .82rem; margin: 1.4rem 0 .4rem; }
</style>
</head>
<body>
<header>
  <h1>Read your own PDF</h1>
  <p>Upload a document and watch the pipeline decide how to read it, then read it.</p>
</header>

<section class="statements" id="statements">
  <p><strong>Where your bytes go:</strong> the PDF is sent to a rented GPU to be read when the decision
  chooses the OCR lane.</p>
  <p><strong>What is kept:</strong> nothing is kept after the response &mdash; no uploaded PDF, no page
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

<section>
  <h2>Upload</h2>
  <form class="upload" id="upload-form">
    <input type="file" id="upload-file" accept="application/pdf" required>
    <button type="submit" id="upload-button">Read this PDF</button>
  </form>
  <p class="note" id="upload-note">One PDF at a time. The server answers a job and this page polls it.</p>
</section>

<section id="timeline">
  <h2>Stage</h2>
  <ol class="timeline" id="timeline-stages">
    <li data-stage="queued">queued</li>
    <li data-stage="routing">routing</li>
    <li data-stage="warming">warming the OCR worker</li>
    <li data-stage="reading">reading the pages</li>
  </ol>
  <p class="warm" id="warm-measured">The warm-up is reported as the number that was measured.</p>
</section>

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

<section id="pages" class="hidden">
  <h2>Page text</h2>
  <p class="note" id="pages-summary"></p>
  <div id="page-texts"></div>
</section>

<section id="refusal" class="panel refusal hidden">
  <h2>Refused: <span id="refusal-kind"></span></h2>
  <p class="label">the gate that closed</p>
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
const state = { job: null, seen: new Set(), timer: null };

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
    let mark = "pending";
    if (index < current - 1 || (done && state.seen.has(item.dataset.stage))) mark = "done";
    else if (done) mark = "skipped";
    else if (index === current - 1) mark = "active";
    item.dataset.state = mark;
  }
  if (outcome === "read") {
    byId("warm-measured").textContent =
      warmMs === null || warmMs === undefined
        ? "Warm-up: the worker was already serving, so no wait was measured."
        : "Warm-up: " + (warmMs / 1000).toFixed(1) + " s measured waiting for the cold OCR worker.";
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
  for (const id of ["decision", "engine-identity", "pages", "refusal", "failure"]) {
    byId(id).classList.add("hidden");
  }
  byId("error").classList.add("hidden");
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
  byId("pages").classList.remove("hidden");
  byId("pages-summary").textContent =
    reading.page_count + " page" + (reading.page_count === 1 ? "" : "s") + ", " +
    reading.character_count + " characters read by this lane.";
  const container = byId("page-texts");
  container.replaceChildren();
  for (const page of reading.pages) {
    const article = node("article", null, "page");
    const head = document.createElement("header");
    head.textContent = "page " + page.number;
    article.append(head, node("pre", page.text));
    container.append(article);
  }
}

function renderJob(job) {
  renderTimeline(job.stage, job.outcome, job.warmed_ms);
  if (job.stage !== "done") return;
  stopPolling();
  byId("upload-button").disabled = false;
  if (job.outcome === "read") renderReading(job.reading);
  else if (job.outcome === "refused") renderRefusal(job.refusal);
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
  stopPolling();
  const ask = async () => {
    try {
      const response = await fetch("/api/read/" + encodeURIComponent(jobId), { cache: "no-store" });
      if (response.status === 404) throw new Error("the server no longer knows this job");
      if (!response.ok) throw new Error("the job answered " + response.status);
      const job = await response.json();
      renderJob(job);
      if (job.stage !== "done") state.timer = setTimeout(ask, 900);
    } catch (error) {
      stopPolling();
      byId("upload-button").disabled = false;
      showError("the poll failed: " + error.message);
    }
  };
  ask();
}

async function upload(file) {
  hideResults();
  state.seen = new Set();
  byId("upload-button").disabled = true;
  renderTimeline("queued", null, null);
  try {
    const response = await fetch("/api/read", {
      method: "POST",
      headers: { "Content-Type": "application/pdf", "X-Upload-Name": file.name || "upload.pdf" },
      body: file,
    });
    const payload = await response.json();
    if (response.status === 202) {
      poll(payload.job);
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
    showError("choose a PDF first");
    return;
  }
  upload(file);
});

loadStatus();
</script>
</body>
</html>
"""
