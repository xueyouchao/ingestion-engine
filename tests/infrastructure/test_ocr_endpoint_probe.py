"""The OCR endpoint probe: does the demo's worker answer yet, and what does it say when it cannot?

A local stub stands in for the endpoint, so what is pinned is what an operator sees -- ready, not-ready
yet, or a named fault -- and that neither the bearer nor the address ever appears in a message. No
network and no GPU are needed.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from finepdfs.domain.ports import OcrEndpointProbe, OcrEndpointUnavailable
from finepdfs.infrastructure.compute.ocr_endpoint import HttpOcrEndpointProbe

MODEL_LIST = json.dumps({"object": "list", "data": [{"id": "PaddlePaddle/PaddleOCR-VL-1.6"}]}).encode("utf-8")


class _Stub:
    """One answer, served for every GET, and every request it saw."""

    def __init__(self, status: int = 200, body: bytes = MODEL_LIST, delay: float = 0.0) -> None:
        self.status = status
        self.body = body
        self.delay = delay
        self.requests: list[dict] = []


def _serving(stub: _Stub) -> tuple[str, Callable[[], None]]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            stub.requests.append({"path": self.path, "headers": dict(self.headers)})
            time.sleep(stub.delay)
            self.send_response(stub.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(stub.body)))
            self.end_headers()
            self.wfile.write(stub.body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join()

    return f"http://127.0.0.1:{server.server_address[1]}/v1", stop


def test_a_listing_endpoint_is_ready_and_asks_models_with_the_bearer() -> None:
    secret = "wm-test-secret-value"
    stub = _Stub()
    base_url, stop = _serving(stub)
    try:
        probe: OcrEndpointProbe = HttpOcrEndpointProbe(base_url, api_key=secret)
        ready = probe.ready()
    finally:
        stop()

    assert ready is True
    assert stub.requests[0]["path"] == "/v1/models"
    assert stub.requests[0]["headers"]["Authorization"] == f"Bearer {secret}"


def test_an_endpoint_that_lists_no_model_is_not_ready_yet() -> None:
    stub = _Stub(body=b'{"object": "list", "data": []}')
    base_url, stop = _serving(stub)
    try:
        ready = HttpOcrEndpointProbe(base_url).ready()
    finally:
        stop()

    assert ready is False


def test_a_2xx_that_is_not_a_model_list_is_not_ready_yet() -> None:
    stub = _Stub(body=b"<html>starting</html>")
    base_url, stop = _serving(stub)
    try:
        ready = HttpOcrEndpointProbe(base_url).ready()
    finally:
        stop()

    assert ready is False


def test_a_worker_that_is_still_loading_answers_a_5xx_and_is_not_ready() -> None:
    stub = _Stub(status=503, body=b"")
    base_url, stop = _serving(stub)
    try:
        ready = HttpOcrEndpointProbe(base_url).ready()
    finally:
        stop()

    assert ready is False


def test_a_connection_error_is_not_ready() -> None:
    assert HttpOcrEndpointProbe("http://127.0.0.1:1/v1", timeout_seconds=0.2).ready() is False


def test_a_timeout_is_not_ready() -> None:
    stub = _Stub(delay=0.3)
    base_url, stop = _serving(stub)
    try:
        ready = HttpOcrEndpointProbe(base_url, timeout_seconds=0.05).ready()
    finally:
        stop()

    assert ready is False


def test_a_refused_bearer_names_the_fault_and_leaks_neither_the_token_nor_the_address() -> None:
    secret = "wm-test-secret-value"
    stub = _Stub(status=401, body=b'{"error": "unauthorized"}')
    base_url, stop = _serving(stub)
    port = base_url.rsplit(":", 1)[-1].split("/")[0]
    try:
        probe = HttpOcrEndpointProbe(base_url, api_key=secret)
        with pytest.raises(OcrEndpointUnavailable) as caught:
            probe.ready()
    finally:
        stop()

    message = str(caught.value)
    assert message == "the OCR endpoint refused the bearer (HTTP 401)"
    assert secret not in message
    assert base_url not in message
    assert f":{port}" not in message


def test_an_empty_base_url_names_the_fault() -> None:
    with pytest.raises(OcrEndpointUnavailable) as caught:
        HttpOcrEndpointProbe("  ").ready()

    assert str(caught.value) == "the OCR endpoint has no base URL configured"


def test_a_malformed_base_url_names_the_fault_without_echoing_it() -> None:
    with pytest.raises(OcrEndpointUnavailable) as caught:
        HttpOcrEndpointProbe("gpu-box.local:8000/v1").ready()

    assert str(caught.value) == "the OCR endpoint's base URL is not an http(s) address"
    assert "gpu-box.local" not in str(caught.value)
