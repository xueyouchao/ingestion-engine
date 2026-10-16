"""The review script: what it posts when a model call, or the whole loop, cannot produce a review.

The comment is public, so the evidence has to be in the words: a dead or wrong endpoint must be
self-evident from the status, the content type and a bounded body snippet -- never a bare
``JSONDecodeError`` that reads as a credential problem.
"""

from __future__ import annotations

import http.client
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


def _run(
    monkeypatch,
    tmp_path,
    *,
    env: dict[str, str],
    opener=None,
    diff_text: str | None = None,
    previous_body: str | None = None,
    read_raises: bool = False,
):
    """Drive ``main`` end to end with the comment posting captured instead of reaching GitHub."""
    posted: list[str] = []
    monkeypatch.setattr(
        review, "post", lambda repo, number, body, comment_id=None: posted.append(body)
    )
    # The retry backoff is real seconds; a test that stubs a dropped socket must not sleep through it.
    monkeypatch.setattr(review, "RETRY_BACKOFF_SECONDS", 0.0, raising=False)

    def unreadable(repo, number):
        raise RuntimeError("gh: HTTP 500")

    monkeypatch.setattr(
        review,
        "find_comment",
        unreadable
        if read_raises
        else lambda repo, number: review.ExistingComment("1", previous_body)
        if previous_body is not None
        else None,
    )
    for name in (
        "OLLAMA_API_KEY",
        "LLM_BASE_URL",
        "REVIEW_VARIABLES",
        "REVIEW_MODEL_STANDARDS",
        "REVIEW_MODEL_SPEC",
        "LLM_API_KEY",
        "GITHUB_TOKEN",
        "HEAD_SHA",
        "GITHUB_SHA",
        "RUN_URL",
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
    # The ticket keeps the classes apart, so this axis is not "did not run": it was abandoned here.
    assert "**Spec** — `glm-5.3-flash`" in posted[0]
    assert "abandoned at the stage deadline" in posted[0]


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


def _gh_recorder(monkeypatch, existing_ids: list[str], existing_body: str = ""):
    """Capture every ``gh`` call; answer the comment-list request with the given ids and body."""
    calls: list[list[str]] = []

    def fake_gh(arguments):
        calls.append(list(arguments))
        if arguments[0] == "api" and arguments[1].endswith("/comments"):
            stdout = "".join(json.dumps([int(identifier), existing_body]) + "\n" for identifier in existing_ids)
            return subprocess.CompletedProcess(arguments, 0, stdout)
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


# --- Ticket 24: the failure classes stay apart, and a dropped connection is retried ---------------


def test_the_workflow_never_lets_two_reviews_overlap() -> None:
    """The probe measured four concurrent calls against one key degrading and two behaving; cap at two."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "review.yml").read_text(encoding="utf-8")

    assert "group: ${{ github.workflow }}\n" in workflow
    assert "cancel-in-progress: false" in workflow


def test_every_transport_failure_keeps_its_own_name() -> None:
    """A read timeout, a closed connection, an HTTP status and an unusable body are four things."""
    closed = urllib.error.URLError(
        http.client.RemoteDisconnected("Remote end closed connection without response")
    )
    kind, detail = review.classify_failure(closed)

    assert kind == review.OUTCOME_CONNECTION_CLOSED
    assert "Remote end closed connection without response" in detail
    assert review.classify_failure(TimeoutError("timed out"))[0] == review.OUTCOME_READ_TIMEOUT
    assert (
        review.classify_failure(review.HttpStatusError("the endpoint answered HTTP 404"))[0]
        == review.OUTCOME_HTTP_STATUS
    )
    assert (
        review.classify_failure(review.UnusableResponseError("no assistant message"))[0]
        == review.OUTCOME_UNUSABLE
    )


def test_the_comment_does_not_call_a_timeout_and_a_dropped_socket_the_same_thing(monkeypatch, tmp_path) -> None:
    """Both axes used to read "did not run": a slow model and a dropped socket looked identical."""

    def open_(request, timeout=None):
        if json.loads(request.data)["model"] == "glm-5.3-flash":
            raise TimeoutError("timed out")
        raise urllib.error.URLError(
            http.client.RemoteDisconnected("Remote end closed connection without response")
        )

    code, posted = _run(monkeypatch, tmp_path, env={"OLLAMA_API_KEY": "test-key"}, opener=open_)

    assert code == 0
    assert len(posted) == 1
    assert "read timed out" in posted[0]
    assert "connection closed without response" in posted[0]
    assert "did not run" not in posted[0]


def test_a_dropped_connection_is_retried_before_the_axis_is_given_up(monkeypatch, tmp_path) -> None:
    """A closed connection is not a bad request: the same call gets further attempts and can succeed."""
    calls: list[str] = []

    def open_(request, timeout=None):
        model = json.loads(request.data)["model"]
        calls.append(model)
        if model == "glm-5.3-flash" and calls.count(model) < 3:
            raise urllib.error.URLError(
                http.client.RemoteDisconnected("Remote end closed connection without response")
            )
        answer = {"choices": [{"message": {"content": f"findings from {model}"}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    code, posted = _run(monkeypatch, tmp_path, env={"OLLAMA_API_KEY": "test-key"}, opener=open_)

    assert code == 0
    assert calls.count("glm-5.3-flash") == 3
    assert "findings from glm-5.3-flash" in posted[0]


def test_a_read_timeout_is_not_retried(monkeypatch, tmp_path) -> None:
    """The call already sat out the read timeout; another attempt cannot fit the stage deadline."""
    calls: list[str] = []

    def open_(request, timeout=None):
        calls.append(json.loads(request.data)["model"])
        raise TimeoutError("timed out")

    code, posted = _run(monkeypatch, tmp_path, env={"OLLAMA_API_KEY": "test-key"}, opener=open_)

    assert code == 0
    assert calls.count("deepseek-v4.1-flash") == 1
    assert calls.count("glm-5.3-flash") == 1
    assert "read timed out" in posted[0]


# --- Ticket 22: the comment is a durable record, not a snapshot of the last run -------------------


def _findings_opener(text: str):
    def open_(_request, timeout=None):
        answer = {"choices": [{"message": {"content": text}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    return open_


def _dropped_opener(_request, timeout=None):
    raise urllib.error.URLError(
        http.client.RemoteDisconnected("Remote end closed connection without response")
    )


def test_findings_survive_a_run_where_every_axis_fails(monkeypatch, tmp_path) -> None:
    """The most valuable content the loop makes is the least durable: a failed run must keep it."""
    _, first = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key", "HEAD_SHA": "abc1234def", "RUN_URL": "https://example.test/runs/1"},
        opener=_findings_opener("the first run's findings"),
    )

    _, second = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key", "HEAD_SHA": "def5678abc", "RUN_URL": "https://example.test/runs/2"},
        opener=_dropped_opener,
        previous_body=first[0],
    )
    body = second[0]

    assert "the first run's findings" in body
    assert "from `abc1234`" in body  # the retained findings name the revision they reviewed
    assert "def5678" in body  # the failed attempt names the head it reviewed
    assert "connection closed without response" in body
    assert body.count(review.MARKER) == 1


def test_new_findings_replace_the_retained_ones(monkeypatch, tmp_path) -> None:
    """A run that does produce findings is the current record: the old ones must go, not pile up."""
    _, first = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key", "HEAD_SHA": "abc1234def"},
        opener=_findings_opener("old findings"),
    )

    _, second = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key", "HEAD_SHA": "def5678abc"},
        opener=_findings_opener("new findings"),
        previous_body=first[0],
    )
    body = second[0]

    assert "new findings" in body
    assert "old findings" not in body
    assert "abc1234" not in body


def test_the_revision_stamp_moves_with_the_head(monkeypatch, tmp_path) -> None:
    """Findings with no revision read as if they were about the new code; they are not."""
    _, first = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key", "HEAD_SHA": "aaaaaaa111"},
        opener=_findings_opener("findings"),
    )

    _, second = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key", "HEAD_SHA": "bbbbbbb222"},
        opener=_findings_opener("findings"),
        previous_body=first[0],
    )

    assert "from `bbbbbbb`" in second[0]
    assert "aaaaaaa" not in second[0]


def test_the_workflow_hands_over_the_revision_it_actually_diffed() -> None:
    """A queued run may check out a later merge ref, so the stamp must name the head it diffed."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "review.yml").read_text(encoding="utf-8")

    assert "REVIEWED_SHA=$(git rev-parse --verify -q HEAD^2 || git rev-parse HEAD)" in workflow
    assert "HEAD_SHA: ${{ env.REVIEWED_SHA }}" in workflow
    assert "github.run_id" in workflow


def test_findings_from_an_unknown_revision_are_still_kept(monkeypatch, tmp_path) -> None:
    """With no head SHA the stamp is ``unknown``; the reader must still find the region it wrote."""
    _, first = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key"},
        opener=_findings_opener("findings kept without a revision"),
    )

    assert "from `unknown`" in first[0]

    _, second = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key"},
        opener=_dropped_opener,
        previous_body=first[0],
    )

    assert "findings kept without a revision" in second[0]


def test_a_findings_free_run_posts_nothing_when_the_comment_cannot_be_read(monkeypatch, tmp_path) -> None:
    """A failed read is not an absent comment: replacing a body we could not read could erase findings."""
    code, posted = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key"},
        opener=_dropped_opener,
        read_raises=True,
    )

    assert code == 0
    assert posted == []


def test_a_run_with_findings_still_posts_when_the_old_comment_cannot_be_read(monkeypatch, tmp_path) -> None:
    """Fresh findings are worth more than the stale ones a failed read could not show, so they are posted.

    This pins the rule at the seam this file owns -- what ``main`` hands to ``post``. The second read
    inside ``upsert_comment``, which decides patch-or-create from it, is pinned by
    ``test_a_later_run_updates_the_existing_comment_instead_of_adding_another`` and
    ``test_the_first_run_creates_the_comment``.
    """
    code, posted = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key"},
        opener=_findings_opener("fresh findings"),
        read_raises=True,
    )

    assert code == 0
    assert len(posted) == 1
    assert "fresh findings" in posted[0]


def test_an_axis_abandoned_mid_retry_still_names_the_class_it_reached(monkeypatch, tmp_path) -> None:
    """A retry must not hide why the axis was in trouble when the deadline takes the retry too."""
    calls: list[str] = []

    def open_(request, timeout=None):
        model = json.loads(request.data)["model"]
        calls.append(model)
        if model == "glm-5.3-flash":
            if calls.count(model) == 1:
                raise urllib.error.URLError(
                    http.client.RemoteDisconnected("Remote end closed connection without response")
                )
            time.sleep(5)
        answer = {"choices": [{"message": {"content": "standards findings"}}]}
        return _Response(json.dumps(answer).encode("utf-8"), content_type="application/json")

    monkeypatch.setattr(review, "AXIS_RUN_DEADLINE_SECONDS", 0.6)
    started = time.monotonic()
    code, posted = _run(monkeypatch, tmp_path, env={"OLLAMA_API_KEY": "test-key"}, opener=open_)
    elapsed = time.monotonic() - started

    assert code == 0
    assert elapsed < 3
    assert "abandoned at the stage deadline" in posted[0]
    assert "the last attempt ended with: connection closed without response" in posted[0]
    assert "standards findings" in posted[0]


def test_the_missing_secret_notice_does_not_erase_findings_it_could_not_read(monkeypatch, tmp_path) -> None:
    """The no-model path posts too and is findings-free, so the retention rule has to cover it as well."""
    code, posted = _run(monkeypatch, tmp_path, env={}, opener=None, read_raises=True)

    assert code == 0
    assert posted == []


def test_findings_the_old_code_posted_are_read_back_once(monkeypatch, tmp_path) -> None:
    """A comment from before the markers carries findings as plain sections; the upgrade must keep them."""
    legacy = (
        f"{review.MARKER}\n\n## LLM review\n\n"
        "### Standards — `deepseek-v4.1-flash`\n\n"
        "- `src/finepdfs/domain/ports.py:14` promises an ISO 639-3 code.\n\n"
        "### Spec — `glm-5.3-flash`\n\n"
        "- `docs/GLOSSARY.md` still spells the retired term.\n"
    )

    code, posted = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key"},
        opener=_dropped_opener,
        previous_body=legacy,
    )

    assert code == 0
    assert len(posted) == 1
    assert "promises an ISO 639-3 code" in posted[0]
    assert "from `pre-marker`" in posted[0]
    assert "they are kept rather than erased" in posted[0]


def test_an_old_comment_that_only_says_it_did_not_run_yields_no_findings() -> None:
    """The legacy reader must not invent findings out of a run note, or it would keep a failure forever."""
    note = (
        f"{review.MARKER}\n\n## LLM review\n\n"
        "the model did not answer. This is a comment, not a gate: the pull request can merge on the "
        "CI checks alone.\n"
    )

    assert review.extract_findings(note) is None


def test_an_unknown_review_call_error_is_not_reported_as_an_unusable_body() -> None:
    """Only the two subclasses know their class; a bare base-class error is not a 2xx body we could not read."""
    status, detail = review.classify_failure(review.ReviewCallError("something else went wrong"))

    assert status == review.OUTCOME_UNEXPECTED
    assert "something else went wrong" in detail


def test_a_pre_marker_comment_that_only_reports_failed_axes_yields_no_findings() -> None:
    """Master posts a section for a failed axis too; its run note is not a finding and must not be kept."""
    body = (
        f"{review.MARKER}\n\n## LLM review\n\n"
        "### Standards — `deepseek-v4.1-flash`\n\n"
        "_This axis did not run: <urlopen error Remote end closed connection without response>_\n\n"
        "### Spec — `glm-5.3-flash`\n\n"
        "_The model returned an empty review._\n"
    )

    assert review.extract_findings(body) is None


def test_a_pre_marker_comment_keeps_only_the_axis_that_found_something() -> None:
    """One axis failing in the old format must not carry its note into the retained findings."""
    body = (
        f"{review.MARKER}\n\n## LLM review\n\n"
        "### Standards — `deepseek-v4.1-flash`\n\n"
        "_This axis did not run: The read operation timed out_\n\n"
        "### Spec — `glm-5.3-flash`\n\n"
        "- `docs/GLOSSARY.md` still spells the retired term.\n"
    )

    retained = review.extract_findings(body)

    assert retained is not None
    assert retained.revision == "pre-marker"
    assert "### Spec — `glm-5.3-flash`" in retained.markdown
    assert "still spells the retired term" in retained.markdown
    assert "### Standards" not in retained.markdown
    assert "did not run" not in retained.markdown


def test_a_pre_marker_body_from_a_failing_run_still_posts_nothing_to_keep(monkeypatch, tmp_path) -> None:
    """End to end: upgrade onto a comment whose only content is master's two failure notes."""
    legacy = (
        f"{review.MARKER}\n\n## LLM review\n\n"
        "### Standards — `deepseek-v4.1-flash`\n\n"
        "_This axis did not run: <urlopen error Remote end closed connection without response>_\n\n"
        "### Spec — `glm-5.3-flash`\n\n"
        "_This axis did not run: <urlopen error Remote end closed connection without response>_\n"
    )

    code, posted = _run(
        monkeypatch,
        tmp_path,
        env={"OLLAMA_API_KEY": "test-key"},
        opener=_dropped_opener,
        previous_body=legacy,
    )

    assert code == 0
    assert len(posted) == 1
    assert "did not run" not in posted[0]
    assert "finepdfs-findings revision=" not in posted[0]
