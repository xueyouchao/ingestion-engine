"""Assemble a two-axis review prompt, ask an LLM, and upsert one pull-request comment.

The workflow YAML stays thin: this module owns prompt assembly, the API call and the comment
bookkeeping, so the prompt can be printed and checked locally with ``--print-prompt`` and no
credentials. It uses only the standard library, and it shells out to ``gh`` to post, so no token
ever appears in this file or in the YAML beyond an environment variable.

The review is a comment, never a gate. Every failure below ends in a short comment (or a quiet
exit) with status 0, because a model outage must not hold up a merge.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MARKER = "<!-- finepdfs-review -->"
GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"
GITHUB_MODELS_MODEL = "openai/gpt-4o-mini"
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
SYSTEM_PROMPT = (
    "You are a precise code reviewer. You follow the repository's own documented standards, "
    "and you answer with one markdown comment."
)
MAX_DIFF_CHARS = 60_000
REQUEST_TIMEOUT_SECONDS = 180


@dataclass(frozen=True, slots=True)
class Backend:
    """One chat-completions endpoint and the credentials and model to reach it."""

    url: str
    api_key: str
    model: str
    label: str


def fill(template: str, values: dict[str, str]) -> str:
    """Substitute ``{{KEY}}`` placeholders literally; a diff must never be interpreted as a format string."""
    text = template
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def build_prompt(template: str, diff: str, context: dict[str, str]) -> str:
    """The prompt sent to the model: the versioned instructions with this diff dropped in.

    A diff larger than :data:`MAX_DIFF_CHARS` is truncated with a visible note, so an oversized pull
    request produces a partial review rather than an API error that comments nothing useful.
    """
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n\n[diff truncated at {MAX_DIFF_CHARS} characters]\n"
    return fill(template, {**context, "DIFF": diff})


def resolve_backend(env: dict[str, str]) -> Backend | None:
    """Pick the endpoint in the documented order: an explicit key first, GitHub Models otherwise.

    A repository secret ``LLM_API_KEY`` wins, with ``LLM_BASE_URL`` and ``LLM_MODEL`` as overrides.
    With no such secret -- the state today -- the built-in ``GITHUB_TOKEN`` reaches GitHub Models,
    so the workflow works with zero setup.
    """
    api_key = env.get("LLM_API_KEY", "").strip()
    if api_key:
        base = (env.get("LLM_BASE_URL") or DEFAULT_OPENAI_URL).rstrip("/")
        model = env.get("LLM_MODEL") or DEFAULT_OPENAI_MODEL
        return Backend(f"{base}/chat/completions", api_key, model, f"configured endpoint ({model})")

    github_token = env.get("GITHUB_TOKEN", "").strip()
    if github_token:
        return Backend(
            GITHUB_MODELS_URL, github_token, GITHUB_MODELS_MODEL, f"GitHub Models ({GITHUB_MODELS_MODEL})"
        )
    return None


def chat(backend: Backend, prompt: str) -> str:
    """One chat-completions call, returning the assistant text. Raises on any transport or shape error."""
    payload = json.dumps(
        {
            "model": backend.model,
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
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        answer = json.loads(response.read().decode("utf-8"))
    return str(answer["choices"][0]["message"]["content"]).strip()


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
    """Post without letting a comment failure fail the job; on a fork the token cannot write at all."""
    try:
        upsert_comment(repo, number, body)
    except Exception as error:  # Broad on purpose: the review is non-blocking by design.
        print(f"could not post the review comment: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", default="pr.diff", help="file holding the unified diff")
    parser.add_argument("--prompt", default=".github/prompts/review.md", help="versioned prompt file")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pr-number", default=os.environ.get("PR_NUMBER", ""))
    parser.add_argument("--base-ref", default=os.environ.get("BASE_REF", ""))
    parser.add_argument("--title", default=os.environ.get("PR_TITLE", ""))
    parser.add_argument(
        "--print-prompt",
        action="store_true",
        help="assemble and print the prompt, then exit; needs no credentials, network or gh",
    )
    args = parser.parse_args(argv)

    diff_path = Path(args.diff)
    diff = diff_path.read_text(encoding="utf-8", errors="replace") if diff_path.exists() else ""
    prompt = build_prompt(
        Path(args.prompt).read_text(encoding="utf-8"),
        diff,
        {
            "REPO": args.repo or "local/repo",
            "PR_NUMBER": args.pr_number or "0",
            "BASE_REF": args.base_ref or "master",
            "PR_TITLE": args.title or "(no title available locally)",
        },
    )

    if args.print_prompt:
        print(prompt)
        return 0

    if not diff.strip():
        print("empty diff: nothing to review", file=sys.stderr)
        return 0

    if not args.repo or not args.pr_number:
        print("GITHUB_REPOSITORY or PR_NUMBER missing; cannot comment", file=sys.stderr)
        return 0

    if os.environ.get("PR_IS_FORK", "").strip().lower() == "true":
        post(args.repo, args.pr_number, note_body("skipped: a fork pull request carries no secrets to reach a model"))
        return 0

    backend = resolve_backend(os.environ)
    if backend is None:
        post(args.repo, args.pr_number, note_body("skipped: no LLM_API_KEY secret and no GITHUB_TOKEN"))
        return 0

    try:
        review = chat(backend, prompt)
    except Exception as error:  # Broad on purpose: a model outage must never gate a pull request.
        print(f"review call failed: {error}", file=sys.stderr)
        post(args.repo, args.pr_number, note_body(f"the model was unavailable ({type(error).__name__}: {error})"))
        return 0

    if not review:
        post(args.repo, args.pr_number, note_body("the model returned an empty review"))
        return 0

    post(args.repo, args.pr_number, comment_body(f"## LLM review\n\n{review}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
