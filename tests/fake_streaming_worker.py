"""An OpenAI-compatible OCR worker on localhost, answering from a recorded page stream.

Standing in for a vLLM/SGLang worker is what lets the offline suite prove the streaming read end to
end with no network and no model. One worker answers both ways an adapter can ask: a blocking
``/chat/completions`` call with the page the recording holds, and a streaming one with the recording's
own ``data:`` frames, verbatim. Interface tests import this module rather than growing a second fake,
so the wire the demo streams through is the one the adapter tests pinned.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# One page of PaddleOCR-VL output as an SSE server sends it: the echoed prompt and the markdown fence
# arrive split across frames, so only concatenating the whole stream and normalizing it once recovers
# the page.
RECORDED_PAGE = FIXTURES / "paddleocr_stream_page.sse"

DONE_FRAME = "data: [DONE]"


def recorded_frames(path: Path = RECORDED_PAGE) -> list[str]:
    """The recorded stream as the lines a server writes: one ``data:`` frame per line."""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def content_of(frames: Sequence[str]) -> str:
    """The page a recorded stream carries: every frame's content delta, in the order sent."""
    return "".join(text for text in map(delta_text, frames) if text)


def _payload(frame: str) -> str:
    """One wire line's payload: what followed ``data:``, or nothing for any other line.

    A server writes ``data: {...}`` and ``data:{...}`` for the same frame, so the space is optional and
    the one place that reads a frame strips the prefix and the whitespace together -- otherwise a frame
    that omitted the space would come back out with the prefix written twice.
    """
    line = frame.strip()
    return line.removeprefix("data:").strip() if line.startswith("data:") else ""


def delta_text(frame: str) -> str | None:
    """One frame's content delta, or ``None`` for a frame that carries no text."""
    payload = _payload(frame)
    if not payload or payload == "[DONE]":
        return None
    choices = json.loads(payload).get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("delta") or {}).get("content")
    return content if isinstance(content, str) else None


class FakeStreamingWorker:
    """A worker that records every request and answers a page from the recording.

    The stream it sends is the recording's own frames, verbatim, and it always ends with ``DONE_FRAME``
    unless a test asked for one of the two ways to end early: ``fail_after`` makes the worker fail the
    stream it is sending -- it writes that many frames, then an ``error`` frame in the server's own words
    and no ``[DONE]``, the way a worker whose model falls over mid-read does -- and ``end_after`` is the
    quieter version, where the connection closes with no reason given. A frame in ``frames`` that is not
    a ``data:`` line is written as the SSE line it is, so a comment heartbeat or an ``event:`` field can
    be part of the wire a test replays.

    ``blocking_finish_reason`` is the ``finish_reason`` the blocking answer carries, so a test can show
    what the batch path does with an answer the worker cut off at the model's token limit.
    """

    def __init__(
        self,
        frames: Sequence[str] | None = None,
        *,
        fail_after: int | None = None,
        end_after: int | None = None,
        hold_after: int | None = None,
        hold: threading.Event | None = None,
        error: str = "the worker ran out of memory",
        blocking_finish_reason: str = "stop",
    ) -> None:
        self.frames = list(frames if frames is not None else recorded_frames())
        self.requests: list[dict] = []
        self._fail_after = fail_after
        self._end_after = end_after
        self._hold_after = hold_after
        self._hold = hold
        self._error = error
        self._blocking_finish_reason = blocking_finish_reason
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                worker.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if body.get("stream") is True:
                    worker._answer_stream(self)
                else:
                    worker._answer_blocking(self)

            def log_message(self, *args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def bodies(self) -> list[dict]:
        """The request bodies the worker received, in the order it received them."""
        return [request["body"] for request in self.requests]

    def _answer_stream(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        for index, frame in enumerate(self.frames):
            if index == self._hold_after and self._hold is not None:
                # A live subscriber needs time to attach and read before the rest of the page lands.
                self._hold.wait(10)
            if index == self._fail_after:
                self._write(handler, json.dumps({"error": {"message": self._error, "code": 500}}))
                return
            if index == self._end_after:
                return
            line = frame.strip()
            payload = _payload(frame)
            if line.startswith("data:"):
                # The stream ends once, below, with the frame this module declares: a recording's own
                # [DONE] is not sent twice, and a frame with no payload is not a frame.
                if payload and payload != "[DONE]":
                    self._write(handler, payload)
            elif line:
                self._write_line(handler, line)  # a comment heartbeat or an event/id field, verbatim
        self._write_line(handler, DONE_FRAME)

    def _answer_blocking(self, handler: BaseHTTPRequestHandler) -> None:
        payload = json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content_of(self.frames)},
                        "finish_reason": self._blocking_finish_reason,
                    }
                ]
            }
        ).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    @staticmethod
    def _write(handler: BaseHTTPRequestHandler, payload: str) -> None:
        """One ``data:`` frame: the payload, then the blank line that ends the frame."""
        FakeStreamingWorker._write_line(handler, f"data: {payload}")

    @staticmethod
    def _write_line(handler: BaseHTTPRequestHandler, line: str) -> None:
        """One SSE line as a server writes it, then the blank line that ends the event."""
        handler.wfile.write(f"{line}\n\n".encode())
        handler.wfile.flush()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
