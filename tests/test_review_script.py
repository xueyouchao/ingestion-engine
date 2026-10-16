"""The review script: what it posts when a model call, or the whole loop, cannot produce a review.

The comment is public, so the evidence has to be in the words: a dead or wrong endpoint must be
self-evident from the status, the content type and a bounded body snippet -- never a bare
``JSONDecodeError`` that reads as a credential problem.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEW_PATH = REPO_ROOT / ".github" / "scripts" / "review.py"
PROMPT_PATH = REPO_ROOT / ".github" / "prompts" / "review.md"


def _load_review():
    spec = importlib.util.spec_from_file_location("review_script", REVIEW_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses with slots resolve the defining module through sys.modules.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


review = _load_review()


class _Response(io.BytesIO):
    """A stand-in for urllib's response, carrying the status and headers the script inspects."""

    def __init__(self, body: bytes, status: int = 200, content_type: str | None = "application/json") -> None:
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": content_type} if content_type else {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _opener(body: bytes, status: int = 200, content_type: str = "text/plain"):
    """The stub HTTP server: it answers every request with the same canned response."""

    def open_(_request, timeout=None):
        return _Response(body, status, content_type)

    return open_


def _run(monkeypatch, tmp_path, *, env: dict[str, str], opener=None, diff_text: str | None = None):
    """Drive ``main`` end to end with the comment posting captured instead of reaching GitHub."""
    posted: list[str] = []
    monkeypatch.setattr(review, "post", lambda repo, number, body: posted.append(body))
    for name in (
        "OLLAMA_API_KEY",
        "LLM_BASE_URL",
        "REVIEW_VARIABLES",
        "REVIEW_MODEL_STANDARDS",
        "REVIEW_MODEL_SPEC",
        "LLM_API_KEY",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    diff = tmp_path / "pr.diff"
    diff.write_text(diff_text or "diff --git a/x b/x\n+one\n", encoding="utf-8")
    code = review.main(
        ["--diff", str(diff), "--prompt", str(PROMPT_PATH), "--repo", "owner/repo", "--pr-number", "4"],
        opener=opener,
    )
    return code, posted


def _diff_chunk(name: str, size: int) -> str:
    """One file's hunk, exactly ``size`` characters, so a synthetic diff's file offsets are known."""
    head = f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n+"
    return head + "x" * (size - len(head) - 1) + "\n"


def _quoted_chunk(name: str, size: int) -> str:
    """One file whose ``diff --git`` line carries git's quoting, for a path that forces it."""
    head = f'diff --git "a/{name}" "b/{name}"\n--- "a/{name}"\n+++ "b/{name}"\n@@ -1 +1 @@\n+'
    return head + "x" * (size - len(head) - 1) + "\n"


def test_a_truncated_diff_cuts_on_a_file_boundary_and_names_the_files_left_out() -> None:
    """The Spec axis saw 12 of 17 files; the cut must name the rest and never half-read a file."""
    diff = "".join(_diff_chunk(f"file{index}.py", 400) for index in range(5))

    sliced = review.slice_diff(diff, limit=1200)

    assert sliced.text == diff[:1200]  # three whole files: the fourth boundary is at 1600
    assert sliced.truncated
    assert sliced.files_total == 5
    assert sliced.omitted == ("file3.py", "file4.py")
    assert sliced.partial is None


def test_a_single_file_larger_than_the_limit_is_named_as_shown_in_part() -> None:
    """When nothing fits whole, say which file is cut rather than leaving the reader to guess."""
    diff = _diff_chunk("huge.py", 900)

    sliced = review.slice_diff(diff, limit=400)

    assert sliced.text == diff[:400]
    assert sliced.partial == "huge.py"
    assert sliced.omitted == ()
    assert "huge.py" in review.truncation_note(sliced)
    assert "in part" in review.truncation_note(sliced)


def test_a_diff_under_the_cap_gets_no_note() -> None:
    """A whole diff is not a truncated one; no note means nothing was left out."""
    diff = _diff_chunk("small.py", 200)

    sliced = review.slice_diff(diff, limit=60_000)

    assert sliced.truncated is False
    assert review.truncation_note(sliced) == ""


def test_a_cut_with_no_file_boundary_says_so_and_names_no_file_as_shown_in_part() -> None:
    """A headerless diff has nothing to cut on: the note must say that, not invent a boundary."""
    sliced = review.slice_diff("x" * 500, limit=200)

    assert sliced.text == "x" * 200
    assert sliced.files_total == 0
    assert sliced.omitted == ()
    assert sliced.partial is None
    assert "No `diff --git` file boundary was found at or before the cut" in review.truncation_note(sliced)


def test_headers_that_all_start_past_the_cut_are_named_yet_never_shown_whole() -> None:
    """A preamble longer than the cap leaves no boundary to cut on; the files past it are still named."""
    diff = "preamble\n" * 30 + _diff_chunk("later.py", 100)

    sliced = review.slice_diff(diff, limit=50)

    assert sliced.text == diff[:50]
    assert sliced.partial is None
    assert sliced.files_total == 1
    assert sliced.omitted == ("later.py",)
    assert "1 of 1 files were not shown to any model" in review.truncation_note(sliced)


def test_a_git_quoted_path_is_still_a_file_boundary() -> None:
    """Git quotes a path with a space or a non-ASCII byte; unmatched, that file lands on the wrong side."""
    diff = _diff_chunk("plain.py", 400) + _quoted_chunk("pa th.py", 400) + _quoted_chunk("päth.py", 400)

    sliced = review.slice_diff(diff, limit=600)

    assert sliced.text == diff[:400]
    assert sliced.files_total == 3
    assert sliced.omitted == ("pa th.py", "päth.py")
    assert sliced.partial is None


def test_a_git_quoted_octal_escaped_path_is_still_a_file_boundary() -> None:
    """Default ``core.quotePath`` escapes a non-ASCII byte as ``\\303\\251``; that header is a boundary too."""
    escaped = "p\\303\\251th.py"
    diff = _quoted_chunk(escaped, 400) + _diff_chunk("second.py", 400)

    sliced = review.slice_diff(diff, limit=600)

    assert sliced.text == diff[:400]
    assert sliced.files_total == 2
    assert sliced.omitted == ("second.py",)
    assert sliced.partial is None


def test_a_truncated_diff_names_its_unseen_files_in_the_comment_and_in_the_prompt(monkeypatch, tmp_path) -> None:
    """A reader, and the model, must both be able to tell what the 120k-character cut left out."""
    prompts_seen: list[str] = []

    def open_(request, timeout=None):
        prompts_seen.append(json.loads(request.data)["messages"][1]["content"])
        answer = {"choices": [{"message": {"content": "findings"}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    # Twenty equal 9000-character files: boundaries at 0, 9000, ..., 117000, and 117000 <= 120000 < 126000.
    diff = "".join(_diff_chunk(f"file{index:02d}.py", 9000) for index in range(20))
    code, posted = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key"},
        opener=open_,
        diff_text=diff,
    )

    assert code == 0
    assert len(posted) == 1
    assert "7 of 20 files were not shown" in posted[0]
    assert "`file13.py`" in posted[0]
    assert "`file19.py`" in posted[0]
    assert "`file12.py`" not in posted[0]
    assert len(prompts_seen) == 2
    for prompt in prompts_seen:
        assert "7 of 20 files were not shown" in prompt
        assert "`file13.py`" in prompt


def test_an_oversized_diff_without_file_headers_still_posts_a_comment(monkeypatch, tmp_path) -> None:
    """A headerless oversized diff used to raise inside slice_diff, so the run died with no comment."""
    monkeypatch.setattr(review, "MAX_DIFF_CHARS", 400)
    diff = "".join(f"line {index}\n" for index in range(200))  # ~2000 characters, no `diff --git` anywhere
    prompts_seen: list[str] = []

    def open_(request, timeout=None):
        prompts_seen.append(json.loads(request.data)["messages"][1]["content"])
        answer = {"choices": [{"message": {"content": "findings"}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    code, posted = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key"},
        opener=open_,
        diff_text=diff,
    )

    note = "No `diff --git` file boundary was found at or before the cut"
    assert code == 0
    assert len(posted) == 1
    assert note in posted[0]
    assert len(prompts_seen) == 2
    for prompt in prompts_seen:
        assert note in prompt


def test_the_workflow_does_not_spell_an_axis_name() -> None:
    """A renamed axis must not silently orphan a workflow key: the prompt headings are the spelling."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "review.yml").read_text(encoding="utf-8")

    assert re.search(r"REVIEW_MODEL_(?!<AXIS>)[A-Z0-9_]", workflow) is None
    assert "toJSON(vars)" in workflow


def test_a_repository_variable_passed_as_json_overrides_an_axis_model(monkeypatch, tmp_path) -> None:
    """The workflow hands every variable over at once, so the override still reaches its axis."""
    models_seen: list[str] = []

    def open_(request, timeout=None):
        models_seen.append(json.loads(request.data)["model"])
        answer = {"choices": [{"message": {"content": "findings"}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    code, _ = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key", "REVIEW_VARIABLES": '{"REVIEW_MODEL_SPEC": "spec-y"}'},
        opener=open_,
    )

    assert code == 0
    assert sorted(models_seen) == ["deepseek-v4.1-flash", "spec-y"]


def test_a_malformed_variable_blob_keeps_the_default_models() -> None:
    """A bad blob must not take the loop down with it: no variables means the default models."""
    assert review.review_variables({"REVIEW_VARIABLES": "not json"}) == {}
    assert review.review_variables({"REVIEW_VARIABLES": "[1, 2]"}) == {}
    assert review.review_variables({}) == {}


def test_a_fork_flag_in_the_environment_no_longer_skips_the_review(monkeypatch, tmp_path) -> None:
    """The old branch posted where a fork's token cannot write, so it produced nothing: it is gone."""

    def open_(request, timeout=None):
        answer = {"choices": [{"message": {"content": "the review ran"}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    code, posted = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key", "PR_IS_FORK": "true"},
        opener=open_,
    )

    assert code == 0
    assert len(posted) == 1
    assert "the review ran" in posted[0]
    assert "skipped" not in posted[0]


def test_a_body_that_looks_like_a_placeholder_is_not_substituted_a_second_time() -> None:
    """Substituting key by key re-reads what was already inserted: one pass, over the template only."""
    filled = review.fill(
        "{{PR_BODY}}|{{AXIS}}",
        {"PR_BODY": "the body quotes {{AXIS}}", "AXIS": "Spec"},
    )

    assert filled == "the body quotes {{AXIS}}|Spec"


def test_the_prompt_carries_a_placeholder_shaped_body_as_text(tmp_path, capsys) -> None:
    """A body mentioning {{DIFF}} is data, not a slot: the diff must not be spliced into it."""
    diff = tmp_path / "pr.diff"
    diff.write_text("diff --git a/x b/x\n+one\n", encoding="utf-8")

    code = review.main(
        [
            "--diff",
            str(diff),
            "--prompt",
            str(PROMPT_PATH),
            "--pr-number",
            "4",
            "--body",
            "the intent is {{DIFF}}",
            "--print-prompt",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "the intent is {{DIFF}}" in out


def test_a_plain_text_200_names_the_status_and_the_body_instead_of_raising(monkeypatch, tmp_path) -> None:
    """The tombstone endpoint answers 200 text/plain 'OK'; that is what the comment must say."""
    code, posted = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key"},
        opener=_opener(b"OK", status=200, content_type="text/plain"),
    )

    assert code == 0
    assert len(posted) == 1
    message = posted[0]
    assert "JSONDecodeError" not in message
    assert "200" in message
    assert "text/plain" in message
    assert "OK" in message


def test_json_without_an_assistant_message_is_reported_by_shape(monkeypatch, tmp_path) -> None:
    """A JSON body that parses but carries no choices[0].message.content must not raise KeyError."""
    code, posted = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key"},
        opener=_opener(b'{"error":{"message":"Unauthorized"}}', content_type="application/json"),
    )

    assert code == 0
    assert len(posted) == 1
    assert "no assistant message" in posted[0]
    assert "Unauthorized" in posted[0]


def test_an_http_error_reports_the_status_and_the_body(monkeypatch, tmp_path) -> None:
    """A rejected model id or wrong endpoint answers 4xx; the comment must carry the body verbatim."""

    def open_(_request, timeout=None):
        raise urllib.error.HTTPError(
            "https://ollama.com/v1/chat/completions",
            404,
            "Not Found",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"error":{"message":"model not found"}}'),
        )

    code, posted = _run(monkeypatch, tmp_path, env={"OLLAMA_API_KEY": "test-key"}, opener=open_)

    assert code == 0
    assert len(posted) == 1
    assert "404" in posted[0]
    assert "application/json" in posted[0]
    assert "model not found" in posted[0]


def test_without_the_secret_one_actionable_comment_names_the_gh_command(monkeypatch, tmp_path) -> None:
    """No key is not a dead end: name the secret and the one line that sets it, and call no model."""

    def open_(*_args, **_kwargs):
        raise AssertionError("a model must not be called without OLLAMA_API_KEY")

    code, posted = _run(
        monkeypatch,
        tmp_path,
        env={"GITHUB_TOKEN": "ghs_retired", "LLM_API_KEY": "an-old-key"},
        opener=open_,
    )

    assert code == 0
    assert len(posted) == 1
    message = posted[0]
    assert review.MARKER in message
    assert "OLLAMA_API_KEY" in message
    assert "gh secret set OLLAMA_API_KEY" in message


def test_only_the_ollama_key_yields_a_backend() -> None:
    """There is no built-in-credential fallback left: the old tokens buy nothing."""
    assert review.resolve_backend({"GITHUB_TOKEN": "x", "LLM_API_KEY": "y"}) is None
    assert review.resolve_backend({}) is None
    backend = review.resolve_backend({"OLLAMA_API_KEY": "k"})
    assert backend is not None
    assert backend.url == "https://ollama.com/v1/chat/completions"


def test_the_axes_are_named_once_in_the_prompt_and_the_code_pairs_them_with_models() -> None:
    """The prompt's headings are the only spelling of an axis name; the code reads them back."""
    template = PROMPT_PATH.read_text(encoding="utf-8")
    default = review.review_axes(template, {})
    assert [(axis.name, axis.model) for axis in default] == [
        ("Standards", "deepseek-v4.1-flash"),
        ("Spec", "glm-5.3-flash"),
    ]
    overridden = review.review_axes(
        template, {"REVIEW_MODEL_STANDARDS": "std-x", "REVIEW_MODEL_SPEC": "spec-y"}
    )
    assert [axis.model for axis in overridden] == ["std-x", "spec-y"]


def test_renaming_an_axis_in_the_prompt_needs_no_second_edit() -> None:
    """Rename the heading and the code follows it, so the model is never left without its section."""
    renamed = PROMPT_PATH.read_text(encoding="utf-8").replace(
        "## If the axis is Spec", "## If the axis is Conformance"
    )

    axes = review.review_axes(renamed, {})

    assert [axis.name for axis in axes] == ["Standards", "Conformance"]
    assert [axis.model for axis in axes] == ["deepseek-v4.1-flash", "glm-5.3-flash"]


def test_the_prompt_carries_the_pull_request_body_as_the_statement_of_intent(capsys, tmp_path) -> None:
    """Spec cannot read the gitignored ticket in CI, so the body must reach the prompt as the intent."""
    diff = tmp_path / "pr.diff"
    diff.write_text("diff --git a/x b/x\n+one\n", encoding="utf-8")

    code = review.main(
        [
            "--diff",
            str(diff),
            "--prompt",
            str(PROMPT_PATH),
            "--pr-number",
            "4",
            "--body",
            "the body states the intent",
            "--print-prompt",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "BEGIN PULL REQUEST BODY" in out
    assert "the body states the intent" in out


def test_each_axis_is_reviewed_independently_and_labelled_with_its_model(monkeypatch, tmp_path) -> None:
    """Two calls, one per axis, never merged; the one comment names the axis and its model."""
    models_seen: list[str] = []

    def open_(request, timeout=None):
        model = json.loads(request.data)["model"]
        models_seen.append(model)
        answer = {"choices": [{"message": {"content": f"findings from {model}"}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    code, posted = _run(monkeypatch, tmp_path, env={"OLLAMA_API_KEY": "test-key"}, opener=open_)

    assert code == 0
    # The two calls now overlap, so the order they are made in is not fixed; each model is asked once.
    assert sorted(models_seen) == ["deepseek-v4.1-flash", "glm-5.3-flash"]
    assert len(posted) == 1
    assert "### Standards — `deepseek-v4.1-flash`" in posted[0]
    assert "### Spec — `glm-5.3-flash`" in posted[0]
    assert "findings from deepseek-v4.1-flash" in posted[0]
    assert "findings from glm-5.3-flash" in posted[0]


def test_both_axis_calls_are_in_flight_at_once(monkeypatch, tmp_path) -> None:
    """Run sequentially, the slower model waits behind the faster one; both calls must overlap."""
    together = threading.Barrier(2, timeout=5)

    def open_(request, timeout=None):
        together.wait()
        model = json.loads(request.data)["model"]
        answer = {"choices": [{"message": {"content": f"findings from {model}"}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    code, posted = _run(monkeypatch, tmp_path, env={"OLLAMA_API_KEY": "test-key"}, opener=open_)

    assert code == 0
    assert len(posted) == 1
    assert "findings from deepseek-v4.1-flash" in posted[0]
    assert "findings from glm-5.3-flash" in posted[0]


def test_a_hung_axis_is_abandoned_at_the_deadline_and_the_other_axis_still_posts(monkeypatch, tmp_path) -> None:
    """The Spec axis timed out on a large diff: the comment must keep Standards and bound the wait."""

    def open_(request, timeout=None):
        if json.loads(request.data)["model"] == "glm-5.3-flash":
            time.sleep(3)
        answer = {"choices": [{"message": {"content": "standards findings"}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    monkeypatch.setattr(review, "AXIS_RUN_DEADLINE_SECONDS", 0.2, raising=False)
    started = time.monotonic()
    code, posted = _run(monkeypatch, tmp_path, env={"OLLAMA_API_KEY": "test-key"}, opener=open_)
    elapsed = time.monotonic() - started

    assert code == 0
    assert elapsed < 2
    assert len(posted) == 1
    assert "standards findings" in posted[0]
    assert "### Spec — `glm-5.3-flash`" in posted[0]
    assert "did not run" in posted[0]
    assert "timed out" in posted[0]


def test_an_axis_that_fails_still_leaves_the_other_axis_in_the_one_comment(monkeypatch, tmp_path) -> None:
    def open_(request, timeout=None):
        model = json.loads(request.data)["model"]
        if model == "glm-5.3-flash":
            return _Response(b"OK", status=200, content_type="text/plain")
        answer = {"choices": [{"message": {"content": "standards findings"}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    code, posted = _run(monkeypatch, tmp_path, env={"OLLAMA_API_KEY": "test-key"}, opener=open_)

    assert code == 0
    assert len(posted) == 1
    assert "standards findings" in posted[0]
    assert "text/plain" in posted[0]
    assert "OK" in posted[0]


def _gh_recorder(monkeypatch, existing_ids: list[str]):
    """Capture every ``gh`` call; answer the comment-list request with the given ids."""
    calls: list[list[str]] = []

    def fake_gh(arguments):
        calls.append(list(arguments))
        if arguments[0] == "api" and arguments[1].endswith("/comments"):
            return subprocess.CompletedProcess(arguments, 0, "".join(f"{identifier}\n" for identifier in existing_ids))
        return subprocess.CompletedProcess(arguments, 0, "")

    monkeypatch.setattr(review, "_gh", fake_gh)
    return calls


def test_a_later_run_updates_the_existing_comment_instead_of_adding_another(monkeypatch) -> None:
    """The marker comment's promise: the second run PATCHes it and never opens a second one."""
    calls = _gh_recorder(monkeypatch, ["6222255138"])

    review.upsert_comment("owner/repo", "4", review.comment_body("## LLM review\n\nsecond run"))

    assert calls[0][:2] == ["api", "repos/owner/repo/issues/4/comments"]
    assert calls[1][:4] == ["api", "-X", "PATCH", "repos/owner/repo/issues/comments/6222255138"]
    assert not any(call[0] == "pr" for call in calls)


def test_the_first_run_creates_the_comment(monkeypatch) -> None:
    """With no marker comment yet, the run creates one: the other half of the upsert."""
    calls = _gh_recorder(monkeypatch, [])

    review.upsert_comment("owner/repo", "4", review.comment_body("## LLM review\n\nfirst run"))

    assert calls[1][0] == "pr"
    assert calls[1][1] == "comment"
    assert not any(call[:2] == ["api", "-X"] for call in calls)
