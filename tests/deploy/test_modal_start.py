"""The Modal start command: one JSON object, the lease before readiness, and a nonzero exit that stop can clean up.

Every test drives the real ``start.main`` against a fake SDK. The last two run it behind the real
``CommandGpuProvider``, which is the seam the pipeline actually uses (ADR-0007, ADR-0009).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_modal import (
    TUNNEL_URL,
    FakeModal,
    FakeStopModal,
    RecordingOpener,
    ScriptRunner,
    worker_environment,
)

from deploy.gpu.contract import (
    VLLM_IMAGE,
    WORKER_DETAIL,
    WORKER_MODEL,
    WORKER_PORT,
    LeaseState,
    read_lease,
    state_path,
    write_lease,
)
from deploy.modal import start
from deploy.modal.image import HF_CACHE_PATH, HF_CACHE_VOLUME
from finepdfs.infrastructure.compute.providers import CommandGpuProvider, GpuStartFailed


def _lease_path(tmp_path: Path) -> Path:
    return tmp_path / "worker.json"


def test_start_prints_one_json_endpoint_after_recording_the_lease(tmp_path, capsys) -> None:
    """The happy path, checked on both sides of the contract: stdout and the lease on disk."""
    values = worker_environment(_lease_path(tmp_path))
    modal = FakeModal(_lease_path(tmp_path))
    opener = RecordingOpener()

    assert start.main(values, sdk=modal, opener=opener) == 0

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1, "stdout is the endpoint and nothing else"
    assert captured.err == ""
    assert json.loads(lines[0]) == {
        "base_url": TUNNEL_URL + "/v1",
        "model": WORKER_MODEL,
        "detail": WORKER_DETAIL,
    }

    lease = read_lease(state_path(values))
    assert lease == LeaseState(
        provider="modal",
        worker_id="sb-abc123",
        app_name=start.APP_NAME,
        created_at=lease.created_at,
    )
    assert lease.created_at, "the lease is aged by the moment it was taken"


def test_the_lease_is_on_disk_before_readiness_is_awaited(tmp_path, capsys) -> None:
    """A start that dies during readiness must still leave stop a worker id to terminate."""
    modal = FakeModal(_lease_path(tmp_path))

    assert start.main(worker_environment(_lease_path(tmp_path)), sdk=modal, opener=RecordingOpener()) == 0

    assert modal.sandbox.lease_was_recorded_before_readiness is True


def test_the_sandbox_is_one_worker_with_a_pinned_image_and_a_lifetime_cap(tmp_path) -> None:
    values = worker_environment(_lease_path(tmp_path), FINEPDFS_GPU_TYPE="A100", FINEPDFS_GPU_WORKER_LIFETIME="120")
    modal = FakeModal(_lease_path(tmp_path))

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 0

    assert modal.looked_up == [(start.APP_NAME, True)]
    assert modal.registry == (VLLM_IMAGE, None)
    assert modal.created["gpu"] == "A100"
    assert modal.created["encrypted_ports"] == [WORKER_PORT]
    assert modal.created["readiness_probe"] == ("tcp-probe", WORKER_PORT)
    assert modal.created["timeout"] == 120
    assert modal.created["volumes"] == {HF_CACHE_PATH: f"volume-{HF_CACHE_VOLUME}"}
    assert modal.volume_names == [(HF_CACHE_VOLUME, True)]
    assert modal.sandbox.ready_timeout == start.READY_TIMEOUT_DEFAULT
    assert modal.image.environment["PADDLEOCR_VL_MODEL"] == "PaddlePaddle/PaddleOCR-VL-1.6"
    assert modal.image.entrypoint_argv == ["python3", "/opt/finepdfs/gpu/server.py"]


def test_readiness_verifies_the_served_model_over_v1(tmp_path, capsys) -> None:
    opener = RecordingOpener()

    assert start.main(worker_environment(_lease_path(tmp_path)), sdk=FakeModal(_lease_path(tmp_path)), opener=opener) == 0

    assert opener.urls == [TUNNEL_URL + "/v1/models"]


def test_a_readiness_failure_keeps_the_lease_and_does_not_stop_the_worker_itself(tmp_path, capsys) -> None:
    """The provider runs stop after a nonzero start; running it here would hide which side failed."""
    modal = FakeModal(_lease_path(tmp_path), ready_error=RuntimeError("the readiness probe never passed"))

    assert start.main(worker_environment(_lease_path(tmp_path)), sdk=modal, opener=RecordingOpener()) == 1

    captured = capsys.readouterr()
    assert captured.out == "", "a failed start prints no endpoint"
    assert "the readiness probe never passed" in captured.err
    assert read_lease(state_path(worker_environment(_lease_path(tmp_path)))).worker_id == "sb-abc123"
    assert modal.sandbox.terminated is False


def test_a_tunnel_that_serves_another_model_fails_readiness_and_keeps_the_lease(tmp_path, capsys) -> None:
    modal = FakeModal(_lease_path(tmp_path))

    assert start.main(worker_environment(_lease_path(tmp_path)), sdk=modal, opener=RecordingOpener("other-model")) == 1

    assert WORKER_MODEL in capsys.readouterr().err
    assert read_lease(_lease_path(tmp_path)) is not None


def test_a_worker_with_no_tunnel_on_the_worker_port_is_reported(tmp_path, capsys) -> None:
    modal = FakeModal(_lease_path(tmp_path), tunnel_url=None)

    assert start.main(worker_environment(_lease_path(tmp_path)), sdk=modal, opener=RecordingOpener()) == 1

    assert str(WORKER_PORT) in capsys.readouterr().err
    assert read_lease(_lease_path(tmp_path)) is not None


def test_a_worker_that_cannot_be_created_writes_no_lease(tmp_path, capsys) -> None:
    modal = FakeModal(_lease_path(tmp_path), create_error=RuntimeError("quota exceeded"))

    assert start.main(worker_environment(_lease_path(tmp_path)), sdk=modal, opener=RecordingOpener()) == 1

    assert "quota exceeded" in capsys.readouterr().err
    assert read_lease(_lease_path(tmp_path)) is None


def test_an_existing_lease_blocks_a_second_start(tmp_path, capsys) -> None:
    """Overwriting the lease would lose the only handle on a worker that may still be billing."""
    path = _lease_path(tmp_path)
    write_lease(path, LeaseState(provider="modal", worker_id="sb-old"))
    modal = FakeModal(path)

    assert start.main(worker_environment(path), sdk=modal, opener=RecordingOpener()) == 1

    assert modal.created == {}
    assert read_lease(path).worker_id == "sb-old"
    error = capsys.readouterr().err
    assert "sb-old" in error
    assert "stop" in error.lower(), "the message says how to clear it"


def test_a_lease_that_cannot_be_written_terminates_the_worker(tmp_path, capsys) -> None:
    """No lease means stop could never find it, so the just-created worker is given back here."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    state_file = blocker / "worker.json"
    values = worker_environment(state_file)
    modal = FakeModal(state_file)

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 1

    assert modal.sandbox.terminated is True
    assert "could not be written" in capsys.readouterr().err


def test_a_secret_in_a_vendor_error_is_redacted(tmp_path, capsys) -> None:
    secret = "modal-secret-value-1234"
    values = worker_environment(_lease_path(tmp_path), MODAL_TOKEN_SECRET=secret)
    modal = FakeModal(_lease_path(tmp_path), create_error=RuntimeError(f"auth rejected for {secret}"))

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 1

    error = capsys.readouterr().err
    assert secret not in error
    assert "***" in error


def test_the_command_provider_leases_the_real_start_output_and_stops_it(tmp_path, capsys) -> None:
    values = worker_environment(_lease_path(tmp_path))
    modal = FakeModal(_lease_path(tmp_path))
    stop_modal = FakeStopModal()
    runner = ScriptRunner(values, modal, RecordingOpener(), stop_modal)
    provider = CommandGpuProvider(start_command="start", stop_command="stop", runner=runner)

    with provider.lease() as endpoint:
        assert endpoint.base_url == TUNNEL_URL + "/v1"
        assert endpoint.model == WORKER_MODEL
        assert endpoint.detail == WORKER_DETAIL
        assert "://" not in endpoint.detail
        assert read_lease(_lease_path(tmp_path)) is not None

    assert [call[0] for call in runner.calls] == ["start", "stop"]
    assert stop_modal.terminated == ["sb-abc123"]
    assert read_lease(_lease_path(tmp_path)) is None, "the provider released the worker"


def test_a_readiness_failure_still_invokes_the_stop_command(tmp_path, capsys) -> None:
    """The failure ADR-0009 is written around: no endpoint, a created worker, and a stop that runs."""
    values = worker_environment(_lease_path(tmp_path))
    modal = FakeModal(_lease_path(tmp_path), ready_error=RuntimeError("never ready"))
    stop_modal = FakeStopModal()
    runner = ScriptRunner(values, modal, RecordingOpener(), stop_modal)
    provider = CommandGpuProvider(start_command="start", stop_command="stop", runner=runner)

    with pytest.raises(GpuStartFailed), provider.lease():
        pass

    assert [call[0] for call in runner.calls] == ["start", "stop"]
    assert stop_modal.terminated == ["sb-abc123"]
    assert read_lease(_lease_path(tmp_path)) is None, "the partially started worker was given back"
