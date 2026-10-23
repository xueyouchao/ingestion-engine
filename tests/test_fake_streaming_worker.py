"""The shared fake worker's own contract: the SSE wire it sends, and how it ends a stream.

``tests/fake_streaming_worker.py`` stands in for a vLLM/SGLang worker in two suites at once, so what it
puts on the wire is a contract in its own right: a frame it is handed is sent as one ``data:`` line, and
every stream it starts ends with the ``[DONE]`` frame it declares -- unless a test asked it to fail or to
stop early, which is what those options exist to show.
"""

from __future__ import annotations

import json
import urllib.request

from tests.fake_streaming_worker import DONE_FRAME, FakeStreamingWorker, delta_text, recorded_frames


def _wire_lines(worker: FakeStreamingWorker) -> list[str]:
    """The lines the fake worker sends for one streaming request, exactly as they arrive."""
    request = urllib.request.Request(
        worker.base_url + "/chat/completions",
        data=json.dumps({"model": "m", "stream": True, "messages": []}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return [line.decode("utf-8").rstrip("\n") for line in response if line.strip()]


def test_every_stream_the_fake_sends_ends_with_the_done_frame_it_declares() -> None:
    """The stream's end is stated once, by the frame the fake declares, not by whichever recording."""
    frames = [frame for frame in recorded_frames() if delta_text(frame)]
    with FakeStreamingWorker(frames) as worker:
        lines = _wire_lines(worker)

    assert lines[-1] == DONE_FRAME
    assert DONE_FRAME not in lines[:-1], "the stream is ended once, not twice"


def test_a_frame_written_without_the_space_after_data_is_sent_as_one_data_frame() -> None:
    """A server that omits the space is sent as one frame, never as ``data: data:``."""
    frame = 'data:{"choices": [{"index": 0, "delta": {"content": "a whole page"}}]}'
    with FakeStreamingWorker([frame]) as worker:
        lines = _wire_lines(worker)

    assert lines[0] == 'data: {"choices": [{"index": 0, "delta": {"content": "a whole page"}}]}'
    assert not any(line.startswith("data: data:") for line in lines)
