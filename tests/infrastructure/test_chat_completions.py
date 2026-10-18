"""The chat-completions transport: request shape, auth header, tool passthrough and clean failures.

A local stub server stands in for the model endpoint, so what is pinned is the wire contract an
OpenAI-compatible server sees (JSON body, tools, ``tool_choice``) and what an operator sees when it
fails. No network and no model are needed, and a secret must never appear in an error.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from finepdfs.infrastructure.llm.chat_completions import ChatCompletionsClient, ChatCompletionsError

MESSAGES = [{"role": "user", "content": "search for alpine"}]
TOOLS = [
    {
        "type": "function",
        "function": {"name": "search_pdfs", "parameters": {"type": "object", "properties": {}}},
    }
]
TOOL_ANSWER = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "search_pdfs", "arguments": '{"query": "alpine"}'},
        }
    ],
}


class _Stub:
    def __init__(self, status: int = 200, message: dict | None = None, body: bytes | None = None) -> None:
        self.status = status
        self.message = message or TOOL_ANSWER
        self.body = body
        self.requests: list[dict] = []


def _serving(stub: _Stub):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            stub.requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": json.loads(self.rfile.read(length) or b"{}"),
                }
            )
            if stub.body is not None:
                body = stub.body
            elif stub.status == 200:
                body = json.dumps({"choices": [{"message": stub.message}]}).encode("utf-8")
            else:
                body = b'{"error": "unauthorized"}'
            self.send_response(stub.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join()

    return server, stop


def test_the_request_carries_the_model_messages_and_tool_schemas() -> None:
    stub = _Stub()
    server, stop = _serving(stub)
    try:
        client = ChatCompletionsClient(f"http://127.0.0.1:{server.server_address[1]}/v1", "qwen3:1.7b")

        message = client.complete(MESSAGES, tools=TOOLS)
    finally:
        stop()

    assert message == TOOL_ANSWER
    request = stub.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["body"]["model"] == "qwen3:1.7b"
    assert request["body"]["messages"] == MESSAGES
    assert request["body"]["tools"] == TOOLS
    assert request["body"]["tool_choice"] == "auto"
    assert request["body"]["stream"] is False
    assert "Authorization" not in request["headers"]


def test_the_api_key_goes_in_the_header_and_never_in_an_error() -> None:
    secret = "sk-test-secret-value"
    stub = _Stub(status=401)
    server, stop = _serving(stub)
    try:
        client = ChatCompletionsClient(
            f"http://127.0.0.1:{server.server_address[1]}/v1", "deepseek-v4.1-flash", api_key=secret
        )
        with pytest.raises(ChatCompletionsError) as caught:
            client.complete(MESSAGES)
    finally:
        stop()

    assert stub.requests[0]["headers"]["Authorization"] == f"Bearer {secret}"
    assert "HTTP 401" in str(caught.value)
    assert "unauthorized" in str(caught.value)
    assert secret not in str(caught.value)


def test_an_unreachable_endpoint_names_the_endpoint_not_a_key() -> None:
    client = ChatCompletionsClient("http://127.0.0.1:1/v1", "qwen3:1.7b", api_key="sk-nope", timeout=0.2)

    with pytest.raises(ChatCompletionsError) as caught:
        client.complete(MESSAGES)

    assert "http://127.0.0.1:1/v1/chat/completions" in str(caught.value)
    assert "sk-nope" not in str(caught.value)


def test_a_response_without_choices_is_a_named_failure() -> None:
    stub = _Stub(status=200, body=b'{"choices": []}')
    server, stop = _serving(stub)
    try:
        client = ChatCompletionsClient(f"http://127.0.0.1:{server.server_address[1]}/v1", "m")
        with pytest.raises(ChatCompletionsError, match="no choices"):
            client.complete(MESSAGES)
    finally:
        stop()


def test_a_base_url_and_a_model_are_required() -> None:
    with pytest.raises(ValueError, match="base URL"):
        ChatCompletionsClient("   ", "m")
    with pytest.raises(ValueError, match="model"):
        ChatCompletionsClient("http://127.0.0.1:11434/v1", "")
