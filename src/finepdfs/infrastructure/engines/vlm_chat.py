"""Talking to a document vision model over an OpenAI-compatible chat endpoint.

PaddleOCR-VL, MinerU and RolmOCR can all be served by vLLM behind /v1/chat/completions, so one small
transport serves them all. The wire format is the only thing here that knows about HTTP.

A page is asked for one of two ways, and the caller chooses: one blocking answer, or -- opt-in, never
required (ADR-0014) -- a server-sent event stream whose deltas are handed to the caller as they arrive.
``ChatEndpoint`` only says where the server is, so nothing about streaming lives there.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple


class VlmTransportError(RuntimeError):
    """Raised when the model server refused the request or answered something unusable."""


@dataclass(frozen=True, slots=True)
class ChatEndpoint:
    """Where a model server lives, and which model to ask for."""

    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 300.0

    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def describe(self) -> str:
        return f"{self.model}@{self.base_url}"


class StreamedAnswer(NamedTuple):
    """What a streaming call received: the whole answer, and why the server stopped sending it.

    ``text`` is every content delta joined in the order sent, so it is the blocking answer's text when
    the worker read the same page twice. ``finish_reason`` is the reason the server named (``"stop"``,
    ``"length"``, ...) and ``None`` when it named none before ``[DONE]``, which is how a stream cut
    short by the model's own token limit is told apart from a whole page.
    """

    text: str
    finish_reason: str | None


class VlmChatTransport:
    """One chat completion, with an optional page image."""

    def __init__(self, endpoint: ChatEndpoint, opener: Callable[..., Any] | None = None) -> None:
        self._endpoint = endpoint
        self._opener = opener or urllib.request.urlopen

    @property
    def endpoint(self) -> ChatEndpoint:
        return self._endpoint

    def complete_with_page_image(self, prompt: str, image_png: bytes) -> str:
        return self._post(_page_messages(prompt, image_png))

    def stream_with_page_image(
        self, prompt: str, image_png: bytes, on_delta: Callable[[str], None]
    ) -> StreamedAnswer:
        """Ask for the page as a stream, handing each delta to ``on_delta`` exactly as it arrived.

        This is the one call that sets ``"stream": true``. A delta is the worker's own text, before any
        normalization: the echoed prompt, the fences and any layout marker the model emitted are all in
        there, because a caller that shows them is showing what the worker did.
        """
        return self._post_streaming(_page_messages(prompt, image_png), on_delta)

    def _post(self, messages: list[dict]) -> str:
        request = self._request(messages, stream=False)
        try:
            with self._opener(request, timeout=self._endpoint.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise VlmTransportError(f"could not reach {self._endpoint.chat_completions_url()}: {error}") from error
        return _first_message_content(payload, self._endpoint)

    def _post_streaming(self, messages: list[dict], on_delta: Callable[[str], None]) -> StreamedAnswer:
        request = self._request(messages, stream=True)
        try:
            with self._opener(request, timeout=self._endpoint.timeout_seconds) as response:
                return _streamed_answer(response, self._endpoint, on_delta)
        except (urllib.error.URLError, OSError) as error:
            raise VlmTransportError(f"could not reach {self._endpoint.chat_completions_url()}: {error}") from error

    def _request(self, messages: list[dict], *, stream: bool) -> urllib.request.Request:
        body: dict[str, Any] = {"model": self._endpoint.model, "temperature": 0, "messages": messages}
        if stream:
            body["stream"] = True
        headers = {"Content-Type": "application/json"}
        if self._endpoint.api_key:
            headers["Authorization"] = f"Bearer {self._endpoint.api_key}"
        return urllib.request.Request(
            self._endpoint.chat_completions_url(), data=json.dumps(body).encode("utf-8"), headers=headers
        )


def _page_messages(prompt: str, image_png: bytes) -> list[dict]:
    image_url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
    return [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ],
        }
    ]


def _streamed_answer(response: Any, endpoint: ChatEndpoint, on_delta: Callable[[str], None]) -> StreamedAnswer:
    """Read one server-sent event stream: every ``data:`` frame's delta until ``[DONE]`` ends it.

    A frame that carries an ``error`` is the worker failing this read, and a stream that stops without
    ``[DONE]`` never claimed to be finished: both are named as the failure they are, in the endpoint's
    own words, rather than returned as a short page.
    """
    url = endpoint.chat_completions_url()
    pieces: list[str] = []
    finish_reason: str | None = None
    try:
        for line in response:
            payload = line.decode("utf-8").strip().removeprefix("data:").strip() if line.strip() else ""
            if not payload:  # the blank line that ends a frame, or a comment the server sent
                continue
            if payload == "[DONE]":
                return StreamedAnswer(text="".join(pieces), finish_reason=finish_reason)
            try:
                frame = json.loads(payload)
            except ValueError as error:
                raise VlmTransportError(f"{url} sent a frame that is not JSON: {payload}") from error
            if not isinstance(frame, dict):
                raise VlmTransportError(f"{url} sent a frame that is not an answer: {payload}")
            if "error" in frame:
                raise VlmTransportError(f"{url} failed its stream: {_error_words(frame['error'])}")
            text, finish_reason = _frame_answer(frame, finish_reason)
            if text:
                pieces.append(text)
                on_delta(text)
    except OSError as error:  # the connection dropped: the read failed, and the caller has to know
        raise VlmTransportError(f"{url} broke its stream off: {error}") from error
    raise VlmTransportError(f"{url} ended its stream without [DONE]")


def _frame_answer(frame: dict, finish_reason: str | None) -> tuple[str, str | None]:
    """One frame's content delta and the finish reason it carries, if it carries either."""
    choices = frame.get("choices") or []
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "", finish_reason  # the usage frame some servers send after the last choice
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content") if isinstance(delta, dict) else None
    reason = choice.get("finish_reason")
    return (content if isinstance(content, str) else "", reason if isinstance(reason, str) else finish_reason)


def _error_words(error: Any) -> str:
    """What the worker said went wrong, without squashing a message into a repr of its dict."""
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def _first_message_content(payload: dict, endpoint: ChatEndpoint) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise VlmTransportError(
            f"{endpoint.chat_completions_url()} answered without message content: {payload}"
        ) from error
    if isinstance(content, str):
        return content
    if (
        isinstance(content, list)  # some servers answer with content parts
        and content
        and all(isinstance(part, dict) and isinstance(part.get("text"), str) for part in content)
    ):
        return "".join(part["text"] for part in content)
    # Anything else is an error or a tool call, never page text: str() would turn None into a page.
    raise VlmTransportError(f"{endpoint.chat_completions_url()} answered with content that is not text: {content!r}")


def strip_model_preamble(text: str, prompt: str) -> str:
    """Remove an echoed prompt and markdown fences that a model sometimes wraps its answer in."""
    lines = text.strip().splitlines()
    while lines and lines[0].strip().strip("`") in {prompt.strip(), f"{prompt.strip()}<|endoftext|>"}:
        lines.pop(0)
    cleaned = "\n".join(lines).strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(cleaned.splitlines()[1:])
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return cleaned.strip()
