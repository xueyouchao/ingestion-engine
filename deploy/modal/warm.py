"""The Warm Control: report and change the deployed worker's floor, and name what it costs.

ADR-0012 lets a session hold the OCR worker warm by setting ``min_containers``, which Modal's autoscaler
update changes without a redeploy. This command is the operator-facing half:

    python3 -m deploy.modal.warm status   # the URL, model, detail, floor and price of that floor
    python3 -m deploy.modal.warm on       # hold one container warm, and say what it costs
    python3 -m deploy.modal.warm off      # return to the resting zero

``status`` prints the serving URL already carrying the ``/v1`` base path, so an operator pastes it into a
``ChatEndpoint`` unchanged. It prints the pinned Engine Identity detail *without* a page-DPI suffix: the
engine registry appends ``@dpi200`` itself (ADR-0005 makes page DPI an identity input), and printing it here as well
would produce ``@dpi200@dpi200``. The price comes from the shared contract, so this command and the
runbook cannot disagree about the number. No action calls ``modal deploy``: ``status`` reads the current
floor through an autoscaler update with no fields set, which is the read the installed SDK exposes.
"""

from __future__ import annotations

import os
import sys
from typing import Any, TextIO

from deploy.gpu.contract import (
    DEPLOYED_APP_NAME,
    DEPLOYED_GPU,
    DEPLOYED_MIN_CONTAINERS,
    DEPLOYED_SERVER_NAME,
    GPU_RATE_OBSERVED,
    GPU_RATE_PER_SECOND_USD,
    GPU_RATE_SOURCE,
    WARM_FLOOR_CONTAINERS,
    WORKER_DETAIL,
    WORKER_MODEL,
    base_url_with_v1,
    redact,
    warm_floor_cost,
)

COMMANDS = ("status", "on", "off")
USAGE = "usage: python -m deploy.modal.warm status|on|off"


class WarmControlError(RuntimeError):
    """Raised when the deployed app cannot be read or its floor cannot be changed."""


def _import_sdk() -> Any:
    """Import Modal at call time, so the offline suite needs no vendor package."""
    import modal

    return modal


def main(argv: list[str] | None = None, sdk: Any = None, stream: TextIO | None = None) -> int:
    """Report or change the warm floor, printing only what an operator can act on."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    out = stream or sys.stdout
    if len(arguments) != 1 or arguments[0] not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2
    command = arguments[0]
    try:
        modal = sdk if sdk is not None else _import_sdk()
        server = modal.Server.from_name(DEPLOYED_APP_NAME, DEPLOYED_SERVER_NAME)
        serving_url = server.get_url()
        if not serving_url:
            raise WarmControlError(
                f"{DEPLOYED_APP_NAME!r} has no serving URL; is it deployed? (modal deploy deploy/modal/serve.py)"
            )
        if command == "status":
            floor = _current_floor(server)
            print(f"base_url: {base_url_with_v1(serving_url)}", file=out)
            print(f"model: {WORKER_MODEL}", file=out)
            print(f"detail: {WORKER_DETAIL}", file=out)
            _print_floor(out, floor, hourly=False)
        else:
            held = WARM_FLOOR_CONTAINERS if command == "on" else DEPLOYED_MIN_CONTAINERS
            floor = _set_floor(server, held)
            _print_floor(out, floor, hourly=True)
    except Exception as error:
        print(redact(f"error: {error}", os.environ), file=sys.stderr)
        return 1
    return 0


def _current_floor(server: Any) -> int:
    """The floor Modal reports for this Server, read through an update that requests no change.

    ``Server.update_autoscaler`` with every field unset is the only public read of the current floor the
    installed SDK exposes, and its docstring says an unspecified parameter retains its current value -- so
    status requests a change to nothing. The runbook prints that docstring rather than asking a reader to
    trust this sentence, and this call was never made against a deployed app.
    """
    return _floor_of(server.update_autoscaler())


def _set_floor(server: Any, containers: int) -> int:
    return _floor_of(server.update_autoscaler(min_containers=containers))


def _floor_of(settings: Any) -> int:
    reported = getattr(settings, "min_containers", None)
    return DEPLOYED_MIN_CONTAINERS if reported is None else reported


def _print_floor(out: TextIO, floor: int, *, hourly: bool) -> None:
    cost = warm_floor_cost(floor)
    print(f"floor: {floor}", file=out)
    print(f"cost_per_minute: {cost.per_minute()}", file=out)
    if hourly:
        print(f"cost_per_hour: {cost.per_hour()}", file=out)
    print(
        f"rate: ${GPU_RATE_PER_SECOND_USD}/s per {DEPLOYED_GPU} container "
        f"(source: {GPU_RATE_SOURCE}, observed: {GPU_RATE_OBSERVED})",
        file=out,
    )


if __name__ == "__main__":
    raise SystemExit(main())
