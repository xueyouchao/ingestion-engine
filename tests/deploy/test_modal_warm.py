"""The Warm Control: report and change the floor without a redeploy, and name what it costs.

Every test drives the real ``warm.main`` against a module-like fake SDK. What is pinned is what an
operator can see: the paste-ready ``/v1`` base URL, the served model, the detail without a page DPI
suffix, the floor, and the price of that floor per minute and per hour.
"""

from __future__ import annotations

from fake_modal import TUNNEL_URL, FakeWarmModal

from deploy.gpu import contract
from deploy.gpu.contract import (
    DEPLOYED_APP_NAME,
    DEPLOYED_SERVER_NAME,
    WORKER_DETAIL,
    WORKER_MODEL,
)
from deploy.modal import warm


def test_status_prints_the_paste_ready_v1_url_the_model_and_the_versioned_detail(capsys) -> None:
    modal = FakeWarmModal(url=TUNNEL_URL, floor=0)

    assert warm.main(["status"], sdk=modal) == 0

    out = capsys.readouterr().out
    assert f"base_url: {TUNNEL_URL}/v1" in out
    assert f"model: {WORKER_MODEL}" in out
    assert f"detail: {WORKER_DETAIL}" in out
    assert "@dpi" not in out, "the registry appends @dpi200; printing it here would double it"


def test_status_names_the_current_floor_and_what_it_costs_per_minute(capsys) -> None:
    modal = FakeWarmModal(url=TUNNEL_URL, floor=0)

    assert warm.main(["status"], sdk=modal) == 0

    out = capsys.readouterr().out
    assert "floor: 0" in out
    assert "cost_per_minute: $0.0000" in out
    assert modal.server.updates == [{}], "status reads the autoscaler with no change requested"
    assert modal.looked_up == [(DEPLOYED_APP_NAME, DEPLOYED_SERVER_NAME)]


def test_on_sets_the_floor_to_one_and_names_the_minute_and_hour_price(capsys) -> None:
    modal = FakeWarmModal(url=TUNNEL_URL, floor=0)

    assert warm.main(["on"], sdk=modal) == 0

    out = capsys.readouterr().out
    assert modal.server.updates == [{"min_containers": 1}]
    assert "floor: 1" in out
    assert "cost_per_minute: $0.0133" in out
    assert "cost_per_hour: $0.80" in out
    assert contract.GPU_RATE_SOURCE in out
    assert contract.GPU_RATE_OBSERVED in out


def test_off_returns_the_floor_to_zero(capsys) -> None:
    modal = FakeWarmModal(url=TUNNEL_URL, floor=1)

    assert warm.main(["off"], sdk=modal) == 0

    out = capsys.readouterr().out
    assert modal.server.updates == [{"min_containers": 0}]
    assert "floor: 0" in out
    assert "cost_per_minute: $0.0000" in out


def test_a_deployed_server_with_no_url_is_an_error_not_an_invented_address(capsys) -> None:
    modal = FakeWarmModal(url=None)

    assert warm.main(["status"], sdk=modal) == 1

    assert "is it deployed" in capsys.readouterr().err.lower()


def test_an_unknown_subcommand_prints_the_usage_and_exits_nonzero(capsys) -> None:
    modal = FakeWarmModal()

    assert warm.main(["hold"], sdk=modal) == 2

    assert modal.server.updates == []
    assert "status" in capsys.readouterr().err

