"""Assemble a two-axis review prompt, ask one model per axis, and upsert one pull-request comment.

The workflow YAML stays thin: this module owns prompt assembly, the two API calls and the comment
bookkeeping, so the prompt can be printed and checked locally with ``--print-prompt`` and no
credentials. It uses only the standard library, and it shells out to ``gh`` to post, so no token
ever appears in this file or in the YAML beyond an environment variable.

The two axes are reviewed by two different models in two concurrent calls and presented side by
side in one comment. They are never ranked, merged or averaged into a single verdict. The prompt
itself names the axes and carries their instructions; this module reads the names back out of it, so
the code holds no second spelling that could drift.

The review is a comment, never a gate. Every failure below ends in a short comment (or a quiet
exit) with status 0, because a model outage must not hold up a merge.

Each axis's call leaves an :class:`AxisOutcome`: its failure class, its wall-clock seconds and its
findings. The classes stay apart -- a read timeout, a connection the server closed without answering,
and an HTTP status are three different things with three different fixes -- because collapsing them
into one "did not run" told the reader nothing about which was owed. A dropped connection is retried
with backoff; a read timeout is not, because the call already sat out the socket's own deadline.

The comment is also the record, not a snapshot of the latest run: it names the head it reviewed and
the run URL, lists every axis's outcome, and when a run finds nothing it keeps the findings already
posted rather than erasing them, labelling them with the revision they reviewed.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MARKER = "<!-- finepdfs-review -->"
DEFAULT_BASE_URL = "https://ollama.com/v1"
# The models that answer the prompt's axes, in the order the prompt declares them. The axis names
# themselves live only in the prompt, in its ``## If the axis is <name>`` headings.
DEFAULT_MODELS = ("deepseek-v4.1-flash", "glm-5.3-flash")
AXIS_HEADING = re.compile(r"^## If the axis is (?P<name>.+?)\s*$", re.MULTILINE)
# git starts every file in a unified diff with this header; its b-side is the file's name after the
# change. git quotes a path with a space, a quote, a backslash or a non-ASCII byte under the default
# ``core.quotePath`` (``diff --git "a/pa th" "b/pa th"``), so the quotes are optional here too.
DIFF_FILE_HEADER = re.compile(r'^diff --git .* (?P<path>"?b/.+?"?)\s*$', re.MULTILINE)
# One placeholder in the prompt; fill substitutes these in a single pass over the template.
PLACEHOLDER = re.compile(r"\{\{(?P<key>[A-Z0-9_]+)\}\}")
SYSTEM_PROMPT = (
    "You are a precise code reviewer. You follow the repository's own documented standards, "
    "and you answer with one markdown comment."
)
MAX_DIFF_CHARS = 120_000
MAX_BODY_CHARS = 8_000
MAX_ERROR_BODY_CHARS = 200
# A slow model on a large diff needs more than the three minutes that timed the Spec axis out in
# three of the last four reviews: the review comments on PRs #2, #3 and #4 each report "The read
# operation timed out", and only #5 answered. The two calls run concurrently, so this bounds the
# stage rather than doubling it; the deadline sits just past it so a socket timeout, which names
# itself, normally beats the deadline, which can only say the call never returned.
REQUEST_TIMEOUT_SECONDS = 600
AXIS_RUN_DEADLINE_SECONDS = REQUEST_TIMEOUT_SECONDS + 30

# The failure classes the comment keeps apart. The first is not a failure; the rest are named for the
# thing that failed, not for the branch that noticed it.
OUTCOME_FINDINGS = "findings posted"
OUTCOME_EMPTY = "empty review"
OUTCOME_READ_TIMEOUT = "read timed out"
OUTCOME_CONNECTION_CLOSED = "connection closed without response"
OUTCOME_HTTP_STATUS = "HTTP status"
OUTCOME_UNUSABLE = "unusable response"
OUTCOME_UNREACHABLE = "endpoint unreachable"
OUTCOME_CERTIFICATE = "certificate verification failed"
OUTCOME_UNEXPECTED = "unexpected failure"
OUTCOME_ABANDONED = "abandoned at the stage deadline"

# A dropped connection is not a bad request: the server never read it, so sending it again can work.
# A read timeout is the opposite -- the call sat out the socket's whole deadline -- and a second
# attempt cannot fit inside AXIS_RUN_DEADLINE_SECONDS, so only the closed connection is retried.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
RETRYABLE_FAILURES = frozenset({OUTCOME_CONNECTION_CLOSED})

# The findings region inside the one comment, so a later run can keep it instead of replacing the whole
# body. The revision rides in the opening marker: that is what lets a reader tell stale from current.
# The token is any non-space run, not just a hex SHA, so the ``unknown`` stamp written when no revision
# is available still round-trips instead of silently making the findings unkeepable. The pattern is
# derived from the template the writer uses, so the two spellings cannot drift apart.
FINDINGS_OPEN_TEMPLATE = "<!-- finepdfs-findings revision={revision} -->"
FINDINGS_OPEN = re.compile(
    re.escape(FINDINGS_OPEN_TEMPLATE).replace(r"\{revision\}", r"(?P<revision>[^\s>]{1,64})")
)
FINDINGS_CLOSE = "<!-- /finepdfs-findings -->"
# A comment the old code posted: findings under `### <axis> -- model` headings, no markers. Read back
# once, at the upgrade, so the first findings-free run afterwards does not erase them. The old code
# wrote a section for every axis, failures included, so a section's own text has to be checked too: a
# run note kept as findings would outlive the findings it displaced.
LEGACY_AXIS_SECTION = re.compile(r"^### (?P<axis>.+?) — (?P<model>`[^`]+`)\s*$", re.MULTILINE)
LEGACY_RUN_NOTE = re.compile(r"^_(?:This axis did not run:.*|The model returned an empty review\.)_$", re.DOTALL)
LEGACY_REVISION = "pre-marker"


@dataclass(frozen=True, slots=True)
class Backend:
    """One OpenAI-compatible chat-completions endpoint and the credential to reach it."""

    url: str
    api_key: str


@dataclass(frozen=True, slots=True)
class Axis:
    """One review axis and the model that reviews it."""

    name: str
    model: str


@dataclass(frozen=True, slots=True)
class DiffSlice:
    """The diff a model is shown, and the files the cut left out of it.

    Both axes see the same slice, so the files below the cut were not shown to any model. The
    numbers travel with the text because both the comment and the prompt have to say what was left
    out: a partial review that does not name its omissions reads like a whole one. ``boundary``
    records whether the cut landed on a file header, so the note may promise no file is half-read
    only when one did.
    """

    text: str
    characters: int = 0
    limit: int = MAX_DIFF_CHARS
    files_total: int = 0
    omitted: tuple[str, ...] = ()
    partial: str | None = None
    boundary: bool = False

    @property
    def truncated(self) -> bool:
        return len(self.text) < self.characters


class ReviewCallError(RuntimeError):
    """A call that reached the endpoint but did not come back with an assistant message.

    The message names the status, the content type and a bounded snippet of the body, so the posted
    comment shows what the endpoint actually answered instead of a bare ``JSONDecodeError`` -- which
    reads as a credential problem even when the endpoint is a tombstone.
    """


class HttpStatusError(ReviewCallError):
    """The endpoint answered a status outside 2xx; its own words are the evidence to post."""


class UnusableResponseError(ReviewCallError):
    """A 2xx body this call cannot read: not JSON, or JSON of a shape without an assistant message."""


@dataclass(frozen=True, slots=True)
class AxisOutcome:
    """One axis's call: how it ended, how long it took, and what it found.

    ``status`` is one of the ``OUTCOME_*`` classes. ``detail`` carries the endpoint's own words when it
    sent any and may be empty; where this module has something to add -- the class an abandonment
    interrupted, the status an HTTP call answered -- that sentence is the detail, so a reader is never
    told an attempt answered when it did not. ``seconds`` is the call's wall clock, or the stage
    deadline itself when the axis was abandoned at it. ``attempts`` is above one only when a dropped
    connection was retried. The comment renders :attr:`label` for every axis of every run, so
    a failure is never silent: the reader can tell a slow model from a dropped socket from a refused
    request.
    """

    axis: Axis
    status: str
    detail: str
    seconds: float
    findings: str | None
    attempts: int = 1

    @property
    def label(self) -> str:
        """The per-axis line: class, wall-clock seconds, the endpoint's words, and retry count."""
        line = f"{self.status} after {self.seconds:.1f}s"
        if self.detail:
            line += f" — {self.detail}"
        if self.attempts > 1:
            line += f" ({self.attempts} attempts)"
        return line


def classify_failure(error: BaseException) -> tuple[str, str]:
    """The class of a failed call and the one-line detail a reader can act on.

    The transport failures stay apart because the fix differs: a read timeout means the model was
    still thinking when our socket gave up, a closed connection means the server dropped us without
    answering, an HTTP status is the endpoint refusing the request in its own words, and a DNS or
    refused connection never reached it at all. A certificate failure needs the endpoint's trust
    configuration fixed, so retrying the same request cannot help.
    """
    if isinstance(error, HttpStatusError):
        return OUTCOME_HTTP_STATUS, _snippet(str(error))
    if isinstance(error, UnusableResponseError):
        return OUTCOME_UNUSABLE, _snippet(str(error))
    if isinstance(error, TimeoutError):
        return OUTCOME_READ_TIMEOUT, ""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, TimeoutError):
        return OUTCOME_READ_TIMEOUT, ""
    if isinstance(reason, ssl.SSLCertVerificationError):
        return OUTCOME_CERTIFICATE, _snippet(str(reason))
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return OUTCOME_UNREACHABLE, _snippet(str(error))
    if isinstance(
        reason,
        (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError, BrokenPipeError),
    ):
        return OUTCOME_CONNECTION_CLOSED, _snippet(str(reason) or str(error))
    return OUTCOME_UNEXPECTED, _snippet(f"{type(error).__name__}: {error}")


def _diff_path(path: str) -> str:
    """The file name from a ``diff --git`` header's b-side, with git's quoting stripped."""
    name = path.strip().strip('"')
    return name[2:] if name.startswith("b/") else name


def slice_diff(diff: str, limit: int = MAX_DIFF_CHARS) -> DiffSlice:
    """The diff to show a model, cut at a file boundary so no file is half-read.

    Per-file delivery would mean one call per file per axis, growing the cost and the latency with
    the pull request and merging several answers into one axis's section; a larger cap costs one
    request and keeps the answer whole. Both are only as good as the account of what was left out,
    so the cut names its files and :func:`truncation_note` says so in the comment and in the prompt.
    """
    characters = len(diff)
    if characters <= limit:
        return DiffSlice(diff, characters=characters, limit=limit)
    starts = [(match.start(), _diff_path(match.group("path"))) for match in DIFF_FILE_HEADER.finditer(diff)]
    boundaries = [index for index, (start, _) in enumerate(starts) if 0 < start <= limit]
    if boundaries:
        index = boundaries[-1]
        return DiffSlice(
            diff[: starts[index][0]],
            characters=characters,
            limit=limit,
            files_total=len(starts),
            omitted=tuple(path for _, path in starts[index:]),
            boundary=True,
        )
    first_files = [index for index, (start, _) in enumerate(starts) if start <= limit]
    if first_files:
        # Not even the first file fits whole: show its head and admit the rest was never in view.
        index = first_files[-1]
        return DiffSlice(
            diff[:limit],
            characters=characters,
            limit=limit,
            files_total=len(starts),
            omitted=tuple(path for _, path in starts[index + 1 :]),
            partial=starts[index][1],
        )
    # No header at or before the cut, so there is no boundary to stop on and every file lies below
    # it. The old max() over that empty choice raised before anything could be posted at all.
    return DiffSlice(
        diff[:limit],
        characters=characters,
        limit=limit,
        files_total=len(starts),
        omitted=tuple(path for _, path in starts),
    )


def truncation_note(slice_: DiffSlice) -> str:
    """What the cut left unseen, named for the reader; empty when the diff was shown whole.

    It is appended inside the prompt's diff markers as well as the comment, so the model knows what
    it was not shown and cannot mistake a partial diff for the whole change.
    """
    if not slice_.truncated:
        return ""
    if slice_.partial:
        head = (
            f"The diff is {slice_.characters} characters and the review reads at most {slice_.limit}. "
            f"The first file alone is larger than that, so `{slice_.partial}` is shown only in part "
            "and its tail was not read."
        )
    elif slice_.boundary:
        head = (
            f"The diff is {slice_.characters} characters and the review reads at most {slice_.limit}, "
            "stopping at a file boundary so no file is shown half-read."
        )
    else:
        head = (
            f"The diff is {slice_.characters} characters and the review reads at most {slice_.limit}. "
            "No `diff --git` file boundary was found at or before the cut, so nothing below it is "
            "shown whole."
        )
    if not slice_.omitted:
        return head
    unseen = (
        f"**{len(slice_.omitted)} of {slice_.files_total} files were not shown to any model** "
        "and remain unverified:"
    )
    listing = "\n".join(f"- `{name}`" for name in slice_.omitted)
    return f"{head} {unseen}\n\n{listing}"


def fill(template: str, values: dict[str, str]) -> str:
    """Substitute ``{{KEY}}`` placeholders in one pass over the template, and only there.

    Replacing key by key re-reads the text already dropped in: a pull-request body that quotes
    ``{{DIFF}}`` would be replaced by the diff. One pass over the template cannot see what it has
    inserted, and a placeholder with no value is left as written rather than blanked.
    """

    def substitute(match: re.Match[str]) -> str:
        return values.get(match.group("key"), match.group(0))

    return PLACEHOLDER.sub(substitute, template)


def build_prompt(template: str, diff: str, context: dict[str, str], note: str = "") -> str:
    """The prompt sent to one model: the versioned instructions with this diff and axis dropped in.

    ``diff`` is already sliced by :func:`slice_diff`; ``note`` is that slice's
    :func:`truncation_note`, appended inside the diff markers so a partial diff says so where the
    model reads it.
    """
    if note:
        diff = f"{diff}\n\n{note}"
    return fill(template, {**context, "DIFF": diff})


def resolve_backend(env: dict[str, str]) -> Backend | None:
    """The endpoint and key, or ``None`` when the repository secret is absent; there is no fallback.

    ``OLLAMA_API_KEY`` is required. ``LLM_BASE_URL`` defaults to the Ollama cloud OpenAI-compatible
    base. No built-in credential reaches a model, so a missing key posts an actionable comment
    instead of calling one.
    """
    api_key = env.get("OLLAMA_API_KEY", "").strip()
    if not api_key:
        return None
    base = (env.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return Backend(f"{base}/chat/completions", api_key)


def axis_names(template: str) -> tuple[str, ...]:
    """The axis names, read from the prompt's own ``## If the axis is <name>`` headings.

    The prompt is the one place an axis is named, because it also carries that axis's instructions;
    the code only pairs the names with models. Renaming an axis is then one edit in the prompt, and
    cannot leave a model with a name the prompt does not instruct.
    """
    names = tuple(match.group("name").strip() for match in AXIS_HEADING.finditer(template))
    if len(names) != len(DEFAULT_MODELS):
        raise ValueError(
            f"the prompt names {len(names)} axes under `## If the axis is <name>`; "
            f"{len(DEFAULT_MODELS)} default models are configured"
        )
    return names


def _variable_suffix(name: str) -> str:
    """An axis name as the upper-case suffix of its ``REVIEW_MODEL_`` repository variable."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def review_axes(template: str, env: dict[str, str]) -> tuple[Axis, ...]:
    """The axes the prompt declares, each paired with the model that reviews it.

    The prompt's section order fixes the pairing with :data:`DEFAULT_MODELS`; ``REVIEW_MODEL_<AXIS>``
    overrides one axis's model, so a renamed axis is overridden under its new name.
    """
    return tuple(
        Axis(name, env.get(f"REVIEW_MODEL_{_variable_suffix(name)}") or DEFAULT_MODELS[index])
        for index, name in enumerate(axis_names(template))
    )


def review_variables(env: dict[str, str]) -> dict[str, str]:
    """The repository variables the workflow hands over as one JSON blob, or ``{}``.

    The workflow passes ``toJSON(vars)`` instead of one ``REVIEW_MODEL_<AXIS>`` key per axis, so its
    YAML names no axis: the prompt's headings stay the only spelling, and a renamed axis cannot leave
    a key pointing at a name the prompt no longer carries. A blob that is missing, malformed or not
    an object of strings yields no variables rather than an error, because the review is a comment
    and must not fail on its own configuration.
    """
    raw = env.get("REVIEW_VARIABLES", "").strip()
    if not raw:
        return {}
    try:
        variables = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(variables, dict):
        return {}
    return {name: value for name, value in variables.items() if isinstance(value, str)}


def _snippet(text: str, limit: int = MAX_ERROR_BODY_CHARS) -> str:
    """A bounded, single-line excerpt of a response body fit for a public comment."""
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[:limit] + "..."
    return collapsed or "(empty body)"


def _where(response) -> str:
    """The status and content type of a response, as far as the response exposes them."""
    status = getattr(response, "status", None)
    headers = getattr(response, "headers", None)
    content_type = headers.get("Content-Type") if headers is not None else None
    return f"HTTP {status if status is not None else 'unknown'} with Content-Type {content_type or 'unknown'}"


def chat(backend: Backend, model: str, prompt: str, opener=None) -> str:
    """One chat-completions call, returning the assistant text.

    A connection failure propagates. An HTTP error, a body that is not JSON, and JSON without an
    assistant message each raise :class:`ReviewCallError` naming the status, the content type and a
    bounded body snippet.
    """
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        backend.url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {backend.api_key}",
        },
    )
    open_ = opener or urllib.request.urlopen
    try:
        with open_(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            where = _where(response)
    except urllib.error.HTTPError as error:
        # A rejected model id or a wrong endpoint answers 4xx; its body is the useful part.
        body = error.read().decode("utf-8", errors="replace")
        raise HttpStatusError(f"the endpoint answered {_where(error)}: {_snippet(body)}") from None

    try:
        answer = json.loads(body)
    except json.JSONDecodeError:
        raise UnusableResponseError(
            f"the endpoint answered {where} but the body is not JSON: {_snippet(body)}"
        ) from None

    try:
        content = answer["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str):
        raise UnusableResponseError(
            f"the endpoint answered {where} but the JSON carries no assistant message: {_snippet(body)}"
        )
    return content.strip()


def run_axes(
    axes: tuple[Axis, ...],
    prompts: dict[str, str],
    backend: Backend,
    opener=None,
) -> list[AxisOutcome]:
    """Every axis's call, made concurrently, as one :class:`AxisOutcome` per axis in prompt order.

    The two models are independent, so running them in sequence makes the slower one pay for the
    faster one's latency -- and time out for reasons that have nothing to do with its own answer.
    A call that is still running at :data:`AXIS_RUN_DEADLINE_SECONDS` is abandoned rather than
    awaited, so a hung socket cannot hold the job open: the surviving axis posts regardless.

    A connection the server closed without answering is retried, with backoff, up to
    :data:`RETRY_ATTEMPTS`. The guard only keeps the next attempt's *start* inside the stage deadline;
    an attempt that then runs long is abandoned like any other, and the class the axis last reached
    travels with the abandonment so a retry does not hide why the axis was in trouble.
    """
    outcomes: dict[str, AxisOutcome] = {}
    last_failure: dict[str, AxisOutcome] = {}
    started_attempts: dict[str, int] = {}
    lock = threading.Lock()
    deadline = time.monotonic() + AXIS_RUN_DEADLINE_SECONDS

    def run(axis: Axis) -> None:
        started = time.monotonic()
        backoff = RETRY_BACKOFF_SECONDS
        attempts = 0
        while True:
            attempts += 1
            with lock:
                started_attempts[axis.name] = attempts
            try:
                review = chat(backend, axis.model, prompts[axis.name], opener)
            except Exception as error:  # Broad on purpose: one axis failing must not sink the other.
                status, detail = classify_failure(error)
                outcome = AxisOutcome(axis, status, detail, time.monotonic() - started, None, attempts)
                with lock:
                    last_failure[axis.name] = outcome
                if (
                    status in RETRYABLE_FAILURES
                    and attempts < RETRY_ATTEMPTS
                    and time.monotonic() + backoff < deadline
                ):
                    time.sleep(backoff)
                    backoff *= 2
                    continue
            else:
                status = OUTCOME_FINDINGS if review else OUTCOME_EMPTY
                outcome = AxisOutcome(
                    axis, status, "", time.monotonic() - started, review or None, attempts
                )
            with lock:
                outcomes[axis.name] = outcome
            return

    threads = [
        threading.Thread(target=run, args=(axis,), name=f"review-{axis.name}", daemon=True)
        for axis in axes
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))

    results = []
    for axis in axes:
        with lock:
            outcome = outcomes.get(axis.name)
            known = last_failure.get(axis.name)
            attempts = started_attempts.get(axis.name, 0)
        if outcome is None:
            detail = f"the last attempt that ended was: {known.status}" if known else ""
            outcome = AxisOutcome(axis, OUTCOME_ABANDONED, detail, AXIS_RUN_DEADLINE_SECONDS, None, attempts)
        results.append(outcome)
    return results


def section_body(axis: Axis, findings: str) -> str:
    """One axis's findings, headed by the axis and the model that produced them; never a merged verdict."""
    return f"### {axis.name} — `{axis.model}`\n\n{findings.strip()}\n"


def comment_body(markdown: str) -> str:
    """Wrap review text with the hidden marker, so later runs update this comment instead of adding one."""
    return f"{MARKER}\n\n{markdown.strip()}\n"


@dataclass(frozen=True, slots=True)
class ExistingComment:
    """The one comment this workflow owns: the id it is patched by, and the body it already holds."""

    id: str
    body: str


@dataclass(frozen=True, slots=True)
class RetainedFindings:
    """Findings kept from an earlier run, and the revision they were written against."""

    revision: str
    markdown: str


def findings_marker(revision: str) -> str:
    """The opening marker, from the same template the reader parses: one spelling, so they cannot drift."""
    return FINDINGS_OPEN_TEMPLATE.format(revision=revision)


def extract_findings(body: str) -> RetainedFindings | None:
    """The findings region of a previously posted comment, or ``None`` when it holds none.

    The region is delimited by its own markers rather than re-derived from the headings, so a run
    keeps exactly what the last run found and nothing it said about how the run went.

    A comment posted before this branch exists has no markers: it carries its findings as plain
    ``### <axis> -- model`` sections, the way the old code wrote them. Those are still findings, and
    the first findings-free run after the upgrade is exactly when a reader would miss them, so they
    are read back and stamped as ``pre-marker`` rather than dropped -- but only the sections that
    actually found something. The old code wrote a section for a failed axis too, and its body there is
    a single run note in italics; keeping that would let one failed run's "did not run" line outlive
    the findings it displaced.
    """
    opening = FINDINGS_OPEN.search(body)
    if opening is not None:
        # The last closing marker, not the first: the review prompt asks a model for lines from the
        # diff, and this branch's own diff contains the closing marker, so findings that quote it must
        # not have their tail silently cut off.
        end = body.rfind(FINDINGS_CLOSE)
        if end <= opening.end():
            return None
        markdown = body[opening.end() : end].strip()
        return RetainedFindings(opening.group("revision"), markdown) if markdown else None

    headings = list(LEGACY_AXIS_SECTION.finditer(body))
    kept = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        markdown = body[heading.end() : end].split(FINDINGS_CLOSE)[0].strip()
        if not markdown or LEGACY_RUN_NOTE.match(markdown):
            continue
        kept.append(f"### {heading.group('axis')} — {heading.group('model')}\n\n{markdown}\n")
    if not kept:
        return None
    return RetainedFindings(LEGACY_REVISION, "\n".join(kept).strip())


def reviewed_revision(env: dict[str, str]) -> str:
    """The short head SHA this run reviewed; the workflow passes it, and ``GITHUB_SHA`` is a fallback."""
    raw = (env.get("HEAD_SHA") or env.get("GITHUB_SHA") or "").strip()
    return raw[:7] if raw else "unknown"


def render_comment(
    revision: str,
    run_url: str,
    outcomes: list[AxisOutcome],
    previous_body: str | None = None,
    preamble: str = "",
    note: str = "",
) -> str:
    """The one comment: this run's per-axis record, then the findings of record.

    A run that found something is the current record and replaces the previous findings. A run that
    found nothing keeps them, names the revision they are from, and still records its own attempt
    above them -- a failure may not erase a success, and stale findings may not read as current.
    """
    current = [outcome for outcome in outcomes if outcome.findings]
    retention = ""
    if current:
        findings_revision: str | None = revision
        findings = "\n\n".join(section_body(outcome.axis, outcome.findings or "") for outcome in current)
    else:
        retained = extract_findings(previous_body) if previous_body else None
        if retained is None:
            findings_revision, findings = None, ""
        else:
            findings_revision, findings = retained.revision, retained.markdown
            retention = (
                f"The findings below are from `{retained.revision}`; the run above reviewed "
                f"`{revision}` and produced none of its own, so they are kept rather than erased.\n"
            )

    parts = ["## LLM review", ""]
    if run_url:
        parts.append(f"Reviewed `{revision}` — [the run]({run_url})")
    else:
        parts.append(f"Reviewed `{revision}` (no run URL is available here)")
    parts.append("")

    record = [f"- {preamble}"] if preamble else []
    record += [f"- **{o.axis.name}** — `{o.axis.model}` — {o.label}" for o in outcomes]
    if record:
        parts += ["### This run", "", *record, ""]
    if note:
        parts += ["#### What the model did not see", "", note, ""]
    if findings_revision:
        parts += [f"## Findings from `{findings_revision}`", ""]
        if retention:
            parts.append(retention)
        parts += [
            findings_marker(findings_revision),
            "",
            findings,
            "",
            FINDINGS_CLOSE,
        ]
    return comment_body("\n".join(parts))


def _gh(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *arguments], text=True, capture_output=True, check=True)


def find_comment(repo: str, number: str) -> ExistingComment | None:
    """The id and body of this workflow's existing comment, if an earlier push posted one.

    One API call carries both, because the body is what decides whether this run may replace the
    comment or must keep what is there. Each result is a compact JSON ``[id, body]`` on its own line,
    so a body full of newlines cannot be mistaken for the end of the record.
    """
    result = _gh(
        [
            "api",
            f"repos/{repo}/issues/{number}/comments",
            "--paginate",
            "--jq",
            f'.[] | select(.body | contains("{MARKER}")) | [.id, .body] | @json',
        ]
    )
    found: list[ExistingComment] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            identifier, body = json.loads(line)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        found.append(ExistingComment(str(identifier), body if isinstance(body, str) else ""))
    return found[-1] if found else None


def read_comment(repo: str, number: str) -> tuple[ExistingComment | None, bool]:
    """The existing comment and whether reading it worked.

    A failed read is not the absence of a comment: treating the two alike would let a findings-free run
    replace a body it could not see, which is exactly the erasure this module exists to prevent. The
    caller gets the flag so it can decline to overwrite what it could not read.
    """
    try:
        return find_comment(repo, number), True
    except Exception as error:  # Broad on purpose: the review is non-blocking by design.
        print(f"could not read the existing review comment: {error}", file=sys.stderr)
        return None, False


def upsert_comment(repo: str, number: str, body: str, comment_id: str | None = None) -> None:
    """Create the review comment, or update the one this workflow posted before."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        path = handle.name
    try:
        if comment_id is None:
            existing = find_comment(repo, number)
            comment_id = existing.id if existing else None
        if comment_id:
            _gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "-F", f"body=@{path}"])
        else:
            _gh(["pr", "comment", number, "--repo", repo, "--body-file", path])
    finally:
        os.unlink(path)


def post(repo: str, number: str, body: str, comment_id: str | None = None) -> None:
    """Post without letting a comment failure fail the job; on a fork the token cannot write at all.

    A fork pull request carries no secrets and a read-only token, so a branch of its own would have
    nothing it could post -- the failure belongs here, printed, with the run still exiting 0.
    """
    try:
        upsert_comment(repo, number, body, comment_id)
    except Exception as error:  # Broad on purpose: the review is non-blocking by design.
        print(f"could not post the review comment: {error}", file=sys.stderr)


def main(argv: list[str] | None = None, opener=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", default="pr.diff", help="file holding the unified diff")
    parser.add_argument("--prompt", default=".github/prompts/review.md", help="versioned prompt file")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pr-number", default=os.environ.get("PR_NUMBER", ""))
    parser.add_argument("--base-ref", default=os.environ.get("BASE_REF", ""))
    parser.add_argument("--title", default=os.environ.get("PR_TITLE", ""))
    parser.add_argument("--body", default=os.environ.get("PR_BODY", ""))
    parser.add_argument(
        "--print-prompt",
        action="store_true",
        help="assemble and print both axis prompts, then exit; needs no credentials, network or gh",
    )
    args = parser.parse_args(argv)

    diff_path = Path(args.diff)
    diff = diff_path.read_text(encoding="utf-8", errors="replace") if diff_path.exists() else ""
    body = args.body.strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f"\n\n[body truncated at {MAX_BODY_CHARS} characters]"
    template = Path(args.prompt).read_text(encoding="utf-8")
    axes = review_axes(template, review_variables(os.environ))
    context = {
        "REPO": args.repo or "local/repo",
        "PR_NUMBER": args.pr_number or "0",
        "BASE_REF": args.base_ref or "master",
        "PR_TITLE": args.title or "(no title available locally)",
        "PR_BODY": body or "(no pull request body available)",
    }
    sliced = slice_diff(diff, MAX_DIFF_CHARS)
    note = truncation_note(sliced)
    prompts = {
        axis.name: build_prompt(template, sliced.text, {**context, "AXIS": axis.name}, note) for axis in axes
    }

    if args.print_prompt:
        for axis in axes:
            print(f"===== {axis.name} (model: {axis.model}) =====")
            print(prompts[axis.name])
        return 0

    if not diff.strip():
        print("empty diff: nothing to review", file=sys.stderr)
        return 0

    if not args.repo or not args.pr_number:
        print("GITHUB_REPOSITORY or PR_NUMBER missing; cannot comment", file=sys.stderr)
        return 0

    env = os.environ
    revision = reviewed_revision(env)
    run_url = (env.get("RUN_URL") or "").strip()
    previous, read_ok = read_comment(args.repo, args.pr_number)

    def deliver(body: str) -> None:
        """One rule for every path out of ``main``, so no path can be the one that erases findings.

        A body this run could not read the comment for may only be replaced by one that carries
        findings of its own; a body with no findings region would take whatever the old comment held
        with it, which is the erasure this module exists to prevent. The rule is here rather than at
        each call site because the missing-secret path posts too, and it is findings-free.
        """
        if not read_ok and FINDINGS_OPEN.search(body) is None:
            print(
                "not posting: this run found nothing and the existing comment could not be read, "
                "so replacing it could erase findings",
                file=sys.stderr,
            )
            return
        post(args.repo, args.pr_number, body, previous.id if previous else None)

    backend = resolve_backend(env)
    if backend is None:
        deliver(
            render_comment(
                revision,
                run_url,
                [],
                previous.body if previous else None,
                preamble=(
                    "no model was called: the review needs the `OLLAMA_API_KEY` repository secret; "
                    f"set it with `gh secret set OLLAMA_API_KEY --repo {args.repo}`. This comment is not "
                    "a gate: the pull request can merge on the CI checks alone."
                ),
            )
        )
        return 0

    outcomes = run_axes(axes, prompts, backend, opener)
    for outcome in outcomes:
        if not outcome.findings:
            print(f"{outcome.axis.name} review call: {outcome.label}", file=sys.stderr)

    deliver(
        render_comment(
            revision,
            run_url,
            outcomes,
            previous.body if previous else None,
            note=note,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
