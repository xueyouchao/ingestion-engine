"""TEMPORARY ticket-24 probe: classify review-endpoint calls made three ways.

Delete this file and ``.github/workflows/upstream-probe.yml`` before the pull request. Both exist only
to measure, inside Actions where ``OLLAMA_API_KEY`` lives, whether the endpoint closes connections
because several requests are in flight at once or because it is simply flaky.
"""

import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request

BASE_URL = (os.environ.get("LLM_BASE_URL") or "https://ollama.com/v1").rstrip("/")
URL = f"{BASE_URL}/chat/completions"
KEY = os.environ["OLLAMA_API_KEY"]
TIMEOUT_SECONDS = 300
MODELS = ("deepseek-v4.1-flash", "glm-5.3-flash")
REPETITIONS = 3

# One fixed prompt for every call: a representative slice of the review job's shape, large enough
# that the answer takes tens of seconds to generate, which is the window the real loop dies in.
_DIFF = "\n".join(
    f"diff --git a/file{index}.py b/file{index}.py\n--- a/file{index}.py\n+++ b/file{index}.py\n"
    f"@@ -1,4 +1,6 @@\n+def helper_{index}(value):\n+    return value * {index}\n"
    for index in range(30)
)
PROMPT = (
    "You are a precise code reviewer. Write a detailed markdown review, at least 400 words and at "
    "least eight bullets, of the diff below. Use `file:line` where you can.\n\n"
    f"--- BEGIN DIFF ---\n{_DIFF}\n--- END DIFF ---"
)

_LOCK = threading.Lock()
ROWS: list[tuple] = []


def classify(error: Exception) -> str:
    """The failure class the comment must keep distinguishable from the other two."""
    if isinstance(error, urllib.error.HTTPError):
        return "http_status"
    if isinstance(error, TimeoutError):
        return "read_timeout"
    if isinstance(error, urllib.error.URLError):
        if isinstance(error.reason, TimeoutError):
            return "read_timeout"
        return "connection_closed"
    return "other"


def call(model: str, scenario: str, rep: int, slot: str) -> None:
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a precise code reviewer."},
                {"role": "user", "content": PROMPT},
            ],
            "temperature": 0.2,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {KEY}",
        },
    )
    started = time.monotonic()
    klass = "ok"
    detail = ""
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
        answer = json.loads(body)
        detail = f"{len(answer['choices'][0]['message']['content'])} chars"
    except Exception as error:  # noqa: BLE001 -- the class is the measurement.
        klass = classify(error)
        detail = f"{type(error).__name__}: {error}"[:160]
    seconds = time.monotonic() - started
    with _LOCK:
        ROWS.append((model, scenario, rep, slot, klass, seconds, detail))
        print(
            f"RESULT model={model} scenario={scenario} rep={rep} slot={slot} "
            f"class={klass} secs={seconds:.1f} detail={detail}",
            flush=True,
        )


def together(specs: list[tuple]) -> None:
    threads = [threading.Thread(target=call, args=spec) for spec in specs]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print(f"WALL scenario={specs[0][1]} rep={specs[0][2]} secs={time.monotonic() - started:.1f}", flush=True)


def sequence(specs: list[tuple]) -> None:
    started = time.monotonic()
    for spec in specs:
        call(*spec)
    print(f"WALL scenario={specs[0][1]} rep={specs[0][2]} secs={time.monotonic() - started:.1f}", flush=True)


def main() -> None:
    if not KEY:
        raise SystemExit("OLLAMA_API_KEY is not set")
    for rep in range(1, REPETITIONS + 1):
        for model in MODELS:
            call(model, "alone", rep, "a")
        together([(model, "concurrent2", rep, f"s{index}") for index, model in enumerate(MODELS)])
        sequence([(model, "sequential2", rep, f"s{index}") for index, model in enumerate(MODELS)])
        together(
            [(model, "concurrent4", rep, f"s{index}") for index, model in enumerate(MODELS) for _ in range(2)]
        )

    print("SUMMARY_BEGIN", flush=True)
    for model in MODELS:
        for scenario in ("alone", "concurrent2", "sequential2", "concurrent4"):
            group = [row for row in ROWS if row[0] == model and row[1] == scenario]
            if not group:
                continue
            seconds = sorted(row[5] for row in group)
            classes: dict[str, int] = {}
            for row in group:
                classes[row[4]] = classes.get(row[4], 0) + 1
            print(
                f"SUMMARY model={model} scenario={scenario} calls={len(group)} "
                f"classes={classes} p50_secs={statistics.median(seconds):.1f} max_secs={seconds[-1]:.1f}",
                flush=True,
            )
    print("SUMMARY_END", flush=True)


if __name__ == "__main__":
    main()
