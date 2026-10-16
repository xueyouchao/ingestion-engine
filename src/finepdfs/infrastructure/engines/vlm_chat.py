"""Talking to a document vision model over an OpenAI-compatible chat endpoint.

PaddleOCR-VL, MinerU and RolmOCR can all be served by vLLM behind /v1/chat/completions, so one small
transport serves them all. The wire format is the only thing here that knows about HTTP.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


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


class VlmChatTransport:
    """One chat completion, with an optional page image."""

    def __init__(self, endpoint: ChatEndpoint, opener: Callable[..., Any] | None = None) -> None:
        self._endpoint = endpoint
        self._opener = opener or urllib.request.urlopen

    @property
    def endpoint(self) -> ChatEndpoint:
        return self._endpoint

    def complete_with_page_image(self, prompt: str, image_png: bytes) -> str:
        image_url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
        return self._post(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ]
        )

    def complete(self, prompt: str) -> str:
        return self._post(messages=[{"role": "user", "content": prompt}])

    def _post(self, messages: list[dict]) -> str:
        body = json.dumps({"model": self._endpoint.model, "temperature": 0, "messages": messages}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._endpoint.api_key:
            headers["Authorization"] = f"Bearer {self._endpoint.api_key}"
        request = urllib.request.Request(self._endpoint.chat_completions_url(), data=body, headers=headers)
        try:
            with self._opener(request, timeout=self._endpoint.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise VlmTransportError(f"could not reach {self._endpoint.chat_completions_url()}: {error}") from error
        return _first_message_content(payload, self._endpoint)


def _first_message_content(payload: dict, endpoint: ChatEndpoint) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise VlmTransportError(
            f"{endpoint.chat_completions_url()} answered without message content: {payload}"
        ) from error
    if isinstance(content, list):  # some servers answer with content parts
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


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
