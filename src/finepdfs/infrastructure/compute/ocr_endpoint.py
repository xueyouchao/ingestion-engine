"""Asking the demo's OCR endpoint whether it answers yet, through its own OpenAI-compatible API.

ADR-0012 makes a cold worker a stage of the demo's page and not an error, and the question behind that
stage is one GET: does ``{base_url}/models`` list a model? vLLM answers it as soon as it is serving, so it
is a readiness check that needs no vendor and no second protocol -- the same base URL and bearer the chat
adapter already sends. A worker that is still loading is "not ready yet" (a 5xx, a 429, a connection error
or a timeout the next poll may find gone); a bearer the endpoint refuses, a route it does not serve, or an
address that is not one is a fault that waiting cannot fix, and is raised as one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from finepdfs.domain.ports import OcrEndpointProbe, OcrEndpointUnavailable

# A readiness check is not a read: it must answer quickly enough to be polled, and a slow answer is the
# same fact as no answer -- the worker is not serving yet.
DEFAULT_PROBE_TIMEOUT_SECONDS = 10.0


class HttpOcrEndpointProbe:
    """Whether an OpenAI-compatible endpoint lists a model right now.

    ``base_url`` is the address the engine is reached at, already carrying ``/v1`` (ADR-0012: the demo
    shows the address as configured, and an operator pastes it from the Warm Control). The bearer is
    optional because a local worker may have none.

    Nothing said out loud carries the address or the token: a leaked bearer is a bill, and the address of
    a private worker is not a visitor's business. A fault names what is wrong with the configuration and,
    for a refusal, the status code the endpoint answered.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._base_url = base_url.strip()
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._opener = opener or urllib.request.urlopen

    @property
    def base_url(self) -> str:
        return self._base_url

    def models_url(self) -> str:
        """Where readiness is asked: the OpenAI-compatible model list, one route below the base URL."""
        return self._base_url.rstrip("/") + "/models"

    def ready(self) -> bool:
        """True when the endpoint lists at least one model, False when it may answer later."""
        request = urllib.request.Request(self._checked_url(), headers=self._headers())
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise OcrEndpointUnavailable(f"the OCR endpoint refused the bearer (HTTP {error.code})") from None
            if 400 <= error.code < 500 and error.code != 429:
                # A 4xx is the caller's own configuration: a route the worker does not serve (a base URL
                # pasted without ``/v1``), a path that no longer exists. Polling cannot fix it, and a
                # fifteen-minute warm-up would only hide it. 429 is the worker saying "not now".
                raise OcrEndpointUnavailable(
                    f"the OCR endpoint answered HTTP {error.code}; it does not serve a model list at this base URL"
                ) from None
            # A 5xx is the worker still starting, and a 429 may pass, so both may change on the next poll.
            return False
        except (urllib.error.URLError, OSError, TimeoutError):
            return False
        return _lists_a_model(body)

    def _checked_url(self) -> str:
        """The model list's URL, or a fault naming what is wrong with the configuration it was built off."""
        if not self._base_url:
            raise OcrEndpointUnavailable("the OCR endpoint has no base URL configured")
        parts = urllib.parse.urlsplit(self._base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise OcrEndpointUnavailable("the OCR endpoint's base URL is not an http(s) address")
        return self.models_url()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers


class LocalEngineProbe:
    """Readiness for an engine this host runs itself: it is ready as soon as it is built.

    A local engine is a command, not a server, so there is nothing to wait for and no warming stage to
    show. Reporting that honestly is what lets the demo use ``docling`` or a local ``paddleocr`` without
    being told its "endpoint has no base URL".
    """

    def ready(self) -> bool:
        return True


def endpoint_probe(base_url: str, api_key: str = "") -> OcrEndpointProbe:
    """The probe an engine asks for: an HTTP one when it has an address, otherwise the local answer.

    The caller passes ``registry.engine_endpoint``, so this never decides which kind of engine it has --
    it only turns that answer into the right question.
    """
    if base_url is None:
        return LocalEngineProbe()
    return HttpOcrEndpointProbe(base_url, api_key)


def _lists_a_model(body: bytes) -> bool:
    """Whether an OpenAI-compatible model list names at least one model; anything else is not ready."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    models = payload.get("data") if isinstance(payload, dict) else payload
    return isinstance(models, list) and len(models) > 0
