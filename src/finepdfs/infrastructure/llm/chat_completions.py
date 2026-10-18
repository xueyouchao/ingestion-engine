"""An OpenAI-compatible chat-completions client over the standard library's urllib.

The agent demo needs one thing from a model endpoint: the chat-completions message shape with tool
calls. That is a JSON POST, so it does not need a provider SDK (and the spec's out-of-scope list rules
one out). A local Ollama endpoint speaks the same shape at ``/v1/chat/completions``; so does any
OpenAI-compatible server.

No secret is ever logged or placed in an error message: the API key goes into the request header and
nowhere else. A failure names the endpoint, the HTTP status and a bounded body excerpt.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence

DEFAULT_TIMEOUT_SECONDS = 120.0
_ERROR_BODY_LIMIT = 500


class ChatCompletionsError(RuntimeError):
    """The endpoint could not answer; the message names the endpoint and the failure, never the key."""


class ChatCompletionsClient:
    """POST one conversation to ``<base_url>/chat/completions`` and return the assistant message."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("a chat model needs a non-empty base URL")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("a chat model needs a non-empty model name")
        self._url = base_url.strip().rstrip("/") + "/chat/completions"
        self._model = model.strip()
        self._api_key = api_key or ""
        self._timeout = timeout

    @property
    def url(self) -> str:
        return self._url

    def complete(self, messages: Sequence[dict], tools: Sequence[dict] = ()) -> dict:
        """Return the first assistant message, tool calls included."""
        body: dict = {
            "model": self._model,
            "messages": list(messages),
            "stream": False,
            "temperature": 0,
        }
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "auto"
        request = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise ChatCompletionsError(self._http_failure(error)) from error
        except urllib.error.URLError as error:
            raise ChatCompletionsError(
                f"the chat endpoint {self._url} could not be reached: {error.reason}"
            ) from error
        except OSError as error:
            raise ChatCompletionsError(f"the chat endpoint {self._url} failed: {error}") from error
        return _assistant_message(payload, self._url)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _http_failure(self, error: urllib.error.HTTPError) -> str:
        try:
            excerpt = error.read(_ERROR_BODY_LIMIT).decode("utf-8", errors="replace").strip()
        except OSError:
            excerpt = ""
        detail = f": {excerpt}" if excerpt else ""
        return f"the chat endpoint {self._url} answered HTTP {error.code}{detail}"


def _assistant_message(payload: bytes, url: str) -> dict:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ChatCompletionsError(f"the chat endpoint {url} returned invalid JSON: {error}") from error
    if not isinstance(document, dict):
        raise ChatCompletionsError(f"the chat endpoint {url} returned a non-object response")
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ChatCompletionsError(f"the chat endpoint {url} returned no choices")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        raise ChatCompletionsError(f"the chat endpoint {url} returned a choice without a message")
    return message
