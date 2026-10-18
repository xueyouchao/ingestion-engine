"""The Modal start command: one JSON object, the lease before readiness, and a nonzero exit that stop can clean up.

Every test drives the real ``start.main`` against a fake SDK. The last four run it behind the real
``CommandGpuProvider``, which is the seam the pipeline actually uses (ADR-0007, ADR-0009), including two
leases at once.
"""

from __future__ import annotations

import json
import re
import threading
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
from lease_holder import hold_reservation_then_die

from deploy.gpu.contract import (
    VLLM_IMAGE,
    WORKER_DETAIL,
    WORKER_MODEL,
    WORKER_PORT,
    LeaseState,
    lease_file,
    lease_reservation,
    read_lease,
    reservation_path,
    write_lease,
)
from deploy.modal import start, stop
from deploy.modal.image import HF_CACHE_PATH, HF_CACHE_VOLUME
from finepdfs.infrastructure.compute.providers import CommandGpuProvider, GpuStartFailed


def _leases(tmp_path: Path) -> Path:
    """The state directory this test's leases are recorded in."""
    return tmp_path / "leases"


def _leases_in(directory: Path) -> list[LeaseState]:
    """Every lease the state directory records, whichever file names it."""
    return [read_lease(path) for path in sorted(directory.glob("*.json"))]


def test_start_prints_one_json_endpoint_after_recording_the_lease(tmp_path, capsys) -> None:
    """The happy path, checked on both sides of the contract: stdout and the lease on disk."""
    values = worker_environment(_leases(tmp_path))
    modal = FakeModal(_leases(tmp_path))
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

    assert read_lease(lease_file(values)) == LeaseState(provider="modal", worker_id="sb-abc123")


def test_a_start_without_a_lease_identity_records_under_the_manual_name(tmp_path) -> None:
    """A start run by hand has no provider identity; the stop that follows it in the same shell must find it."""
    values = worker_environment(_leases(tmp_path))

    assert start.main(values, sdk=FakeModal(_leases(tmp_path)), opener=RecordingOpener()) == 0

    assert [path.name for path in sorted(_leases(tmp_path).glob("*.json"))] == ["manual.json"]


def test_the_lease_is_on_disk_before_readiness_is_awaited(tmp_path) -> None:
    """A start that dies during readiness must still leave stop a worker id to terminate."""
    modal = FakeModal(_leases(tmp_path))

    assert start.main(worker_environment(_leases(tmp_path)), sdk=modal, opener=RecordingOpener()) == 0

    assert modal.sandbox.lease_was_recorded_before_readiness is True


def test_the_sandbox_is_one_worker_with_a_pinned_image_and_a_lifetime_cap(tmp_path) -> None:
    values = worker_environment(_leases(tmp_path), FINEPDFS_GPU_TYPE="A100", FINEPDFS_GPU_WORKER_LIFETIME="120")
    modal = FakeModal(_leases(tmp_path))

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


def test_readiness_verifies_the_served_model_over_v1(tmp_path) -> None:
    opener = RecordingOpener()

    assert start.main(worker_environment(_leases(tmp_path)), sdk=FakeModal(_leases(tmp_path)), opener=opener) == 0

    assert opener.urls == [TUNNEL_URL + "/v1/models"]


def test_a_readiness_failure_keeps_the_lease_and_does_not_stop_the_worker_itself(tmp_path, capsys) -> None:
    """The provider runs stop after a nonzero start; running it here would hide which side failed."""
    values = worker_environment(_leases(tmp_path))
    modal = FakeModal(_leases(tmp_path), ready_error=RuntimeError("the readiness probe never passed"))

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 1

    captured = capsys.readouterr()
    assert captured.out == "", "a failed start prints no endpoint"
    assert "the readiness probe never passed" in captured.err
    assert read_lease(lease_file(values)).worker_id == "sb-abc123"
    assert modal.sandbox.terminated is False


def test_a_tunnel_that_serves_another_model_fails_readiness_and_keeps_the_lease(tmp_path, capsys) -> None:
    values = worker_environment(_leases(tmp_path))
    modal = FakeModal(_leases(tmp_path))

    assert start.main(values, sdk=modal, opener=RecordingOpener("other-model")) == 1

    assert WORKER_MODEL in capsys.readouterr().err
    assert read_lease(lease_file(values)) is not None


def test_a_worker_with_no_tunnel_on_the_worker_port_is_reported(tmp_path, capsys) -> None:
    values = worker_environment(_leases(tmp_path))
    modal = FakeModal(_leases(tmp_path), tunnel_url=None)

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 1

    assert str(WORKER_PORT) in capsys.readouterr().err
    assert read_lease(lease_file(values)) is not None


def test_a_worker_that_cannot_be_created_writes_no_lease(tmp_path, capsys) -> None:
    values = worker_environment(_leases(tmp_path))
    modal = FakeModal(_leases(tmp_path), create_error=RuntimeError("quota exceeded"))

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 1

    assert "quota exceeded" in capsys.readouterr().err
    assert read_lease(lease_file(values)) is None


def test_an_existing_lease_blocks_a_second_start(tmp_path, capsys) -> None:
    """Overwriting the lease would lose the only handle on a worker that may still be billing."""
    values = worker_environment(_leases(tmp_path))
    write_lease(lease_file(values), LeaseState(provider="modal", worker_id="sb-old"))
    modal = FakeModal(_leases(tmp_path))

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 1

    assert modal.created == {}
    assert read_lease(lease_file(values)).worker_id == "sb-old"
    error = capsys.readouterr().err
    assert "sb-old" in error
    assert "stop" in error.lower(), "the message says how to clear it"


def test_a_lease_that_cannot_be_written_terminates_the_worker(tmp_path, capsys, monkeypatch) -> None:
    """No lease means stop could never find it, so the just-created worker is given back here."""
    lease_dir = _leases(tmp_path)
    values = worker_environment(lease_dir)
    modal = FakeModal(lease_dir)

    def unreachable(path, state):
        raise OSError("the state directory went away")

    monkeypatch.setattr(start, "write_lease", unreachable)

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 1

    assert modal.sandbox.terminated is True
    assert "could not be written" in capsys.readouterr().err
    with lease_reservation(lease_dir / "manual.json"):
        pass


def test_a_state_directory_that_cannot_be_reserved_rents_no_worker(tmp_path, capsys) -> None:
    """The reservation comes first, so a lease that cannot even be reserved costs no GPU at all."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    lease_dir = blocker / "leases"
    values = worker_environment(lease_dir)
    modal = FakeModal(lease_dir)

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 1

    assert modal.created == {}, "nothing is rented when there is nowhere to record it"
    assert "reservation" in capsys.readouterr().err


def test_a_secret_in_a_vendor_error_is_redacted(tmp_path, capsys) -> None:
    secret = "modal-secret-value-1234"
    values = worker_environment(_leases(tmp_path), MODAL_TOKEN_SECRET=secret)
    modal = FakeModal(_leases(tmp_path), create_error=RuntimeError(f"auth rejected for {secret}"))

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 1

    error = capsys.readouterr().err
    assert secret not in error
    assert "***" in error


def test_the_command_provider_leases_the_real_start_output_and_stops_it(tmp_path) -> None:
    values = worker_environment(_leases(tmp_path))
    stop_modal = FakeStopModal()
    runner = ScriptRunner(values, RecordingOpener(), stop_modal)
    provider = CommandGpuProvider(start_command="start", stop_command="stop", runner=runner)

    with provider.lease() as endpoint:
        assert endpoint.base_url == TUNNEL_URL + "/v1"
        assert endpoint.model == WORKER_MODEL
        assert endpoint.detail == WORKER_DETAIL
        # Independently of the constant that built it: the consumer's view of the memo name.
        assert re.fullmatch(r"paddleocr-vl@1\.6:weights-[0-9a-f]{12}:image-[0-9a-f]{12}", endpoint.detail)
        assert "://" not in endpoint.detail
        assert [state.worker_id for state in _leases_in(_leases(tmp_path))] == ["sb-0001"]

    assert [call[0] for call in runner.calls] == ["start", "stop"]
    assert stop_modal.terminated == ["sb-0001"]
    assert _leases_in(_leases(tmp_path)) == [], "the provider released the worker"


def test_a_readiness_failure_still_invokes_the_stop_command(tmp_path) -> None:
    """The failure ADR-0009 is written around: no endpoint, a created worker, and a stop that runs."""
    values = worker_environment(_leases(tmp_path))
    stop_modal = FakeStopModal()
    runner = ScriptRunner(
        values, RecordingOpener(), stop_modal, modal_options={"ready_error": RuntimeError("never ready")}
    )
    provider = CommandGpuProvider(start_command="start", stop_command="stop", runner=runner)

    with pytest.raises(GpuStartFailed), provider.lease():
        pass

    assert [call[0] for call in runner.calls] == ["start", "stop"]
    assert stop_modal.terminated == ["sb-0001"]
    assert _leases_in(_leases(tmp_path)) == [], "the partially started worker was given back"


def test_two_leases_each_rent_and_release_their_own_worker(tmp_path, monkeypatch) -> None:
    """A run B must start beside A, and B's stop must not touch A's worker (ADR-0007: a lease per run).

    This is the concurrency regression. One shared default lease file made run B refuse to start, and
    then its unconditional stop terminated run A's live worker and cleared A's lease -- the per-run lease
    ADR-0007 promises, broken by the state file, not by the provider. HOME is redirected so a script that
    fell back to the default location would write into ``tmp_path`` and still be visible here.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    lease_dir = _leases(tmp_path)
    values = worker_environment(lease_dir)
    stop_modal = FakeStopModal()
    runner = ScriptRunner(values, RecordingOpener(), stop_modal)
    provider_a = CommandGpuProvider(start_command="start", stop_command="stop", runner=runner)
    provider_b = CommandGpuProvider(start_command="start", stop_command="stop", runner=runner)

    with provider_a.lease():
        with provider_b.lease():
            worker_a, worker_b = (sandbox.object_id for sandbox in runner.created)
            assert worker_a != worker_b, "each run rents its own worker"
            assert sorted(state.worker_id for state in _leases_in(lease_dir)) == sorted([worker_a, worker_b])

        assert stop_modal.terminated == [worker_b], "leaving B gives back B's worker and only B's"
        assert runner.created[0].terminated is False, "run A's live worker must survive run B's stop"
        assert [state.worker_id for state in _leases_in(lease_dir)] == [worker_a], "A's lease must survive"

    assert stop_modal.terminated == [worker_b, worker_a]
    assert _leases_in(lease_dir) == [], "both runs released what they held"


def test_a_failed_start_cannot_stop_another_leases_worker(tmp_path, monkeypatch) -> None:
    """B's start fails, so B's stop runs with no worker of its own: it must leave A's worker alone."""
    monkeypatch.setenv("HOME", str(tmp_path))
    lease_dir = _leases(tmp_path)
    values = worker_environment(lease_dir)
    stop_modal = FakeStopModal()
    runner = ScriptRunner(values, RecordingOpener(), stop_modal)
    failing = ScriptRunner(
        values, RecordingOpener(), stop_modal, modal_options={"create_error": RuntimeError("quota exceeded")}
    )
    provider_a = CommandGpuProvider(start_command="start", stop_command="stop", runner=runner)
    provider_b = CommandGpuProvider(start_command="start", stop_command="stop", runner=failing)

    with provider_a.lease():
        worker_a = runner.created[0].object_id
        with pytest.raises(GpuStartFailed), provider_b.lease():
            pass

        assert stop_modal.terminated == [], "B rented nothing, so B's stop has nothing to terminate"
        assert runner.created[0].terminated is False
        assert [state.worker_id for state in _leases_in(lease_dir)] == [worker_a]


def test_a_readiness_failure_never_prints_the_tunnel_url(tmp_path, capsys) -> None:
    """The leased base URL is the credential, so an error may name the failure and not the address."""
    values = worker_environment(_leases(tmp_path))
    modal = FakeModal(_leases(tmp_path))
    opener = RecordingOpener(error=OSError(f"connection refused for {TUNNEL_URL}/v1/models"))

    assert start.main(values, sdk=modal, opener=opener) == 1

    error = capsys.readouterr().err
    assert "connection refused" in error, "the failure is still reported"
    assert TUNNEL_URL not in error
    assert "modal.host" not in error


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_ready_timeout_is_the_default(tmp_path, value: str) -> None:
    """Exporting an empty variable is the unset value here, as it is for the state file and the detail."""
    values = worker_environment(_leases(tmp_path), FINEPDFS_GPU_READY_TIMEOUT=value)
    modal = FakeModal(_leases(tmp_path))

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 0

    assert modal.sandbox.ready_timeout == start.READY_TIMEOUT_DEFAULT


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_worker_lifetime_is_the_default(tmp_path, value: str) -> None:
    """The lifetime cap is the leak backstop, so an empty value must not fail the start that sets it."""
    values = worker_environment(_leases(tmp_path), FINEPDFS_GPU_WORKER_LIFETIME=value)
    modal = FakeModal(_leases(tmp_path))

    assert start.main(values, sdk=modal, opener=RecordingOpener()) == 0

    assert modal.created["timeout"] == start.LIFETIME_DEFAULT


def test_two_overlapping_manual_starts_rent_one_worker(tmp_path, capsys) -> None:
    """Two hand-run starts share ``manual.json``, so the gate and the create must be one critical section.

    Without a reservation both read no lease before either writes, both create a worker, and the later
    write drops the earlier id: stop can only find the later worker, and the earlier GPU bills until its
    lifetime cap. The first start is parked inside ``Sandbox.create`` -- exactly the window the old code
    left open -- while the second start runs to completion.
    """
    lease_dir = _leases(tmp_path)
    values = worker_environment(lease_dir)
    creating = threading.Event()
    release = threading.Event()

    class SlowCreateModal(FakeModal):
        def _create(self, **kwargs):
            sandbox = super()._create(**kwargs)
            creating.set()
            assert release.wait(timeout=10), "the first start was never released"
            return sandbox

    first_modal = SlowCreateModal(lease_dir, object_id="sb-first")
    outcome: dict[str, int] = {}
    first = threading.Thread(
        target=lambda: outcome.update(code=start.main(values, sdk=first_modal, opener=RecordingOpener())),
        daemon=True,
    )
    first.start()
    try:
        assert creating.wait(timeout=10), "the first start never reached Sandbox.create"

        second_modal = FakeModal(lease_dir, object_id="sb-second")
        assert start.main(values, sdk=second_modal, opener=RecordingOpener()) == 1

        assert second_modal.created == {}, "the second start must not rent a second worker"
        assert "reserv" in capsys.readouterr().err, "the start says another one holds the lease"
    finally:
        release.set()
        first.join(timeout=10)

    assert not first.is_alive()
    assert outcome["code"] == 0, "the first start still succeeds"
    assert read_lease(lease_file(values)).worker_id == "sb-first", "the first worker's id survives"
    with lease_reservation(lease_file(values)):
        pass


def test_a_start_after_a_crashed_start_can_still_rent(tmp_path, capsys) -> None:
    """A start killed while renting must not block the lease: the OS frees the lock when it dies.

    The old design read the pid the dead start left in ``manual.json.lock`` and asked whether a process
    with that pid existed. Once the pid was recycled that was an unrelated live process, so the lease was
    blocked for ever -- the crash-recovery bug this replaces. The child here takes the real reservation and
    dies without releasing it, and both directions are shown: the start is refused while the child holds
    it, and it rents once the child is gone. Every wait is bounded, so a stuck child fails instead of
    hanging the suite.
    """
    lease_dir = _leases(tmp_path)
    values = worker_environment(lease_dir)
    path = lease_file(values)
    holder = hold_reservation_then_die(path, tmp_path)

    try:
        holder.wait_until_holding()
        assert reservation_path(path).exists(), "the live child's lock file is on disk"

        blocked = FakeModal(lease_dir, object_id="sb-blocked")
        assert start.main(values, sdk=blocked, opener=RecordingOpener()) == 1
        assert blocked.created == {}, "a start is refused while another one reserves the lease"
        assert "reserv" in capsys.readouterr().err
        assert read_lease(path) is None, "the refused start recorded nothing"

        status = holder.die_holding()
    finally:
        if holder.process.poll() is None:  # pragma: no cover - only on a failed assertion above
            holder.kill()

    assert status == 1, "the child died holding the reservation rather than releasing it first"
    assert reservation_path(path).exists(), "the dead start's lock file is still on disk"

    assert start.main(values, sdk=FakeModal(lease_dir), opener=RecordingOpener()) == 0

    assert read_lease(path).worker_id == "sb-abc123"
    assert [candidate.name for candidate in sorted(lease_dir.glob("*.json"))] == ["manual.json"], (
        "the lock file is bookkeeping; the lease is the only *.json"
    )


def test_a_successful_start_leaves_the_lock_file_and_stop_frees_the_lease_for_the_next_one(tmp_path) -> None:
    """A clean start releases the reservation and keeps its lock file; stop clears only the lease."""
    lease_dir = _leases(tmp_path)
    values = worker_environment(lease_dir)
    path = lease_file(values)
    reservation = reservation_path(path)

    assert start.main(values, sdk=FakeModal(lease_dir), opener=RecordingOpener()) == 0

    assert reservation.exists(), "the lock file stays after a clean release, and is not a lease"
    assert path.exists()

    assert stop.main(values, sdk=FakeStopModal()) == 0
    assert not path.exists(), "stop forgets the worker"
    assert not sorted(lease_dir.glob("*.json")), "no lease survives the stop, and the .lock is not one"
    assert reservation.exists(), "the stop removed the lease and left the lock file alone"
    assert read_lease(path) is None, "a state directory holding only a .lock reads as no lease"

    assert start.main(values, sdk=FakeModal(lease_dir, object_id="sb-def456"), opener=RecordingOpener()) == 0

    assert read_lease(path).worker_id == "sb-def456"
    assert reservation.exists()
