"""The review script: what it posts when a model call, or the whole loop, cannot produce a review.

The comment is public, so the evidence has to be in the words: a dead or wrong endpoint must be
self-evident from the status, the content type and a bounded body snippet -- never a bare
``JSONDecodeError`` that reads as a credential problem.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
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


def _run(monkeypatch, tmp_path, *, env: dict[str, str], opener=None):
    """Drive ``main`` end to end with the comment posting captured instead of reaching GitHub."""
    posted: list[str] = []
    monkeypatch.setattr(review, "post", lambda repo, number, body: posted.append(body))
    for name in (
        "OLLAMA_API_KEY",
        "LLM_BASE_URL",
        "REVIEW_MODEL_STANDARDS",
        "REVIEW_MODEL_SPEC",
        "LLM_API_KEY",
        "GITHUB_TOKEN",
        "PR_IS_FORK",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    diff = tmp_path / "pr.diff"
    diff.write_text("diff --git a/x b/x\n+one\n", encoding="utf-8")
    code = review.main(
        ["--diff", str(diff), "--prompt", str(PROMPT_PATH), "--repo", "owner/repo", "--pr-number", "4"],
        opener=opener,
    )
    return code, posted


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


def test_the_two_axes_use_two_models_overridable_by_variables() -> None:
    default = review.review_axes({})
    assert [(axis.name, axis.model) for axis in default] == [
        ("Standards", "deepseek-v4.1-flash"),
        ("Spec", "glm-5.3-flash"),
    ]
    overridden = review.review_axes({"REVIEW_MODEL_STANDARDS": "std-x", "REVIEW_MODEL_SPEC": "spec-y"})
    assert [axis.model for axis in overridden] == ["std-x", "spec-y"]


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
    assert models_seen == ["deepseek-v4.1-flash", "glm-5.3-flash"]
    assert len(posted) == 1
    assert "### Standards — `deepseek-v4.1-flash`" in posted[0]
    assert "### Spec — `glm-5.3-flash`" in posted[0]
    assert "findings from deepseek-v4.1-flash" in posted[0]
    assert "findings from glm-5.3-flash" in posted[0]


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
