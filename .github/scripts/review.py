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
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
# git starts every file in a unified diff with this header; its b-side is the file's name after the change.
DIFF_FILE_HEADER = re.compile(r"^diff --git .* (?P<path>b/.+?)\s*$", re.MULTILINE)
# One placeholder in the prompt; fill substitutes these in a single pass over the template.
PLACEHOLDER = re.compile(r"\{\{(?P<key>[A-Z0-9_]+)\}\}")
SYSTEM_PROMPT = (
    "You are a precise code reviewer. You follow the repository's own documented standards, "
    "and you answer with one markdown comment."
)
MAX_DIFF_CHARS = 120_000
MAX_BODY_CHARS = 8_000
MAX_ERROR_BODY_CHARS = 200
# A slow model on a large diff needs more than the three minutes that timed the Spec axis out twice
# in three runs on a fourteen-file pull request. The two calls run concurrently, so this bounds the
# stage rather than doubling it; the deadline sits just past it so a socket timeout, which names
# itself, normally beats the deadline, which can only say the call never returned.
REQUEST_TIMEOUT_SECONDS = 600
AXIS_RUN_DEADLINE_SECONDS = REQUEST_TIMEOUT_SECONDS + 30


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
        raise ReviewCallError(f"the endpoint answered {_where(error)}: {_snippet(body)}") from None

    try:
        answer = json.loads(body)
    except json.JSONDecodeError:
        raise ReviewCallError(
            f"the endpoint answered {where} but the body is not JSON: {_snippet(body)}"
        ) from None

    try:
        content = answer["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str):
        raise ReviewCallError(
            f"the endpoint answered {where} but the JSON carries no assistant message: {_snippet(body)}"
        )
    return content.strip()


def run_axes(
    axes: tuple[Axis, ...],
    prompts: dict[str, str],
    backend: Backend,
    opener=None,
) -> list[tuple[Axis, str, str | None]]:
    """Every axis's call, made concurrently, as ``(axis, review, error)`` in the prompt's order.

    The two models are independent, so running them in sequence makes the slower one pay for the
    faster one's latency -- and time out for reasons that have nothing to do with its own answer.
    A call that is still running at :data:`AXIS_RUN_DEADLINE_SECONDS` is abandoned rather than
    awaited, so a hung socket cannot hold the job open: the surviving axis posts regardless.
    Exactly one of ``review`` and ``error`` is set: an empty ``review`` with ``error`` ``None`` means
    the model answered with nothing.
    """
    outcomes: dict[str, tuple[str, str | None]] = {}
    lock = threading.Lock()

    def run(axis: Axis) -> None:
        try:
            review = chat(backend, axis.model, prompts[axis.name], opener)
        except Exception as error:  # Broad on purpose: one axis failing must not sink the other.
            outcome: tuple[str, str | None] = ("", str(error))
        else:
            outcome = (review, None)
        with lock:
            outcomes[axis.name] = outcome

    threads = [
        threading.Thread(target=run, args=(axis,), name=f"review-{axis.name}", daemon=True)
        for axis in axes
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + AXIS_RUN_DEADLINE_SECONDS
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))

    abandoned = f"the call timed out after {AXIS_RUN_DEADLINE_SECONDS:g} seconds and was abandoned"
    results = []
    for axis in axes:
        review, error = outcomes.get(axis.name, ("", abandoned))
        results.append((axis, review, error))
    return results


def section_body(axis: Axis, outcome: str) -> str:
    """One axis's section, headed by the axis and the model that produced it; never a merged verdict."""
    return f"### {axis.name} — `{axis.model}`\n\n{outcome.strip()}\n"


def comment_body(markdown: str) -> str:
    """Wrap review text with the hidden marker, so later runs update this comment instead of adding one."""
    return f"{MARKER}\n\n{markdown.strip()}\n"


def note_body(reason: str) -> str:
    """The comment posted when no review happened. It still carries the marker, so a later run updates it."""
    return comment_body(
        "## LLM review\n\n"
        f"{reason}. This is a comment, not a gate: the pull request can merge on the CI checks alone."
    )


def _gh(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *arguments], text=True, capture_output=True, check=True)


def find_comment_id(repo: str, number: str) -> str | None:
    """The id of this workflow's existing comment, if it posted one on an earlier push."""
    result = _gh(
        [
            "api",
            f"repos/{repo}/issues/{number}/comments",
            "--paginate",
            "--jq",
            f'.[] | select(.body | contains("{MARKER}")) | .id',
        ]
    )
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return ids[-1] if ids else None


def upsert_comment(repo: str, number: str, body: str) -> None:
    """Create the review comment, or update the one this workflow posted before."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        path = handle.name
    try:
        comment_id = find_comment_id(repo, number)
        if comment_id:
            _gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "-F", f"body=@{path}"])
        else:
            _gh(["pr", "comment", number, "--repo", repo, "--body-file", path])
    finally:
        os.unlink(path)


def post(repo: str, number: str, body: str) -> None:
    """Post without letting a comment failure fail the job; on a fork the token cannot write at all.

    A fork pull request carries no secrets and a read-only token, so a branch of its own would have
    nothing it could post -- the failure belongs here, printed, with the run still exiting 0.
    """
    try:
        upsert_comment(repo, number, body)
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

    backend = resolve_backend(os.environ)
    if backend is None:
        post(
            args.repo,
            args.pr_number,
            note_body(
                "the review needs the `OLLAMA_API_KEY` repository secret to reach a model; "
                f"set it with `gh secret set OLLAMA_API_KEY --repo {args.repo}`"
            ),
        )
        return 0

    sections = []
    for axis, review, error in run_axes(axes, prompts, backend, opener):
        if error:
            print(f"{axis.name} review call failed: {error}", file=sys.stderr)
            sections.append(section_body(axis, f"_This axis did not run: {error}_"))
            continue
        if not review:
            sections.append(section_body(axis, "_The model returned an empty review._"))
            continue
        sections.append(section_body(axis, review))

    heading = "## LLM review\n\n"
    if note:
        # Above the axes, not buried after them: a reader must see the gap before the findings.
        heading += f"### What the model did not see\n\n{note}\n\n"
    post(args.repo, args.pr_number, comment_body(heading + "\n".join(sections)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
