"""The Modal stop command: terminate the recorded worker, and be safe when there is nothing to terminate.

A stop that fails must not erase the only handle on a worker that may still be billing, and a stop that
finds nothing must exit 0 so the provider can run it unconditionally (ADR-0007, ADR-0009).
"""

from __future__ import annotations

from pathlib import Path

from fake_modal import FakeStopModal, NotFoundError, worker_environment

from deploy.gpu.contract import (
    LeaseState,
    clear_lease,
    read_lease,
    state_path,
    write_lease,
)
from deploy.modal import stop


def _record(path: Path, provider: str = "modal", worker_id: str = "sb-abc123") -> None:
    write_lease(path, LeaseState(provider=provider, worker_id=worker_id, app_name="finepdfs-gpu-worker"))


def test_stop_terminates_the_worker_named_by_the_lease(tmp_path, capsys) -> None:
    path = tmp_path / "worker.json"
    _record(path)
    modal = FakeStopModal()

    assert stop.main(worker_environment(path), sdk=modal) == 0

    assert modal.from_ids == ["sb-abc123"]
    assert modal.terminated == ["sb-abc123"]
    assert read_lease(path) is None, "a terminated worker is no longer recorded"
    assert "terminated Modal worker sb-abc123" in capsys.readouterr().err


def test_stop_is_idempotent_when_no_worker_is_recorded(tmp_path, capsys) -> None:
    modal = FakeStopModal()

    assert stop.main(worker_environment(tmp_path / "absent.json"), sdk=modal) == 0

    assert modal.from_ids == [], "nothing is terminated that was never recorded"
    assert "nothing to stop" in capsys.readouterr().err


def test_stop_tolerates_a_partially_started_worker(tmp_path) -> None:
    """The id is recorded before readiness, so this is the state a failed readiness wait leaves."""
    path = tmp_path / "worker.json"
    _record(path, worker_id="sb-never-ready")
    modal = FakeStopModal()

    assert stop.main(worker_environment(path), sdk=modal) == 0

    assert modal.terminated == ["sb-never-ready"]
    assert read_lease(path) is None


def test_an_already_gone_worker_is_success_not_failure(tmp_path, capsys) -> None:
    path = tmp_path / "worker.json"
    _record(path)
    modal = FakeStopModal(from_id_error=NotFoundError("no such sandbox"))

    assert stop.main(worker_environment(path), sdk=modal) == 0

    assert read_lease(path) is None, "nothing is billing, so the stale lease is cleared"
    assert "already gone" in capsys.readouterr().err


def test_a_failed_stop_exits_nonzero_and_keeps_the_lease_for_a_retry(tmp_path, capsys) -> None:
    path = tmp_path / "worker.json"
    _record(path)
    modal = FakeStopModal(terminate_error=RuntimeError("the worker is still out there"))

    assert stop.main(worker_environment(path), sdk=modal) == 1

    assert read_lease(path) is not None, "the only handle on a maybe-billing worker must survive"
    assert "the worker is still out there" in capsys.readouterr().err


def test_a_malformed_lease_is_reported_and_nothing_is_terminated(tmp_path, capsys) -> None:
    path = tmp_path / "worker.json"
    path.write_text("{not json", encoding="utf-8")
    modal = FakeStopModal()

    assert stop.main(worker_environment(path), sdk=modal) == 1

    assert modal.from_ids == []
    assert str(path) in capsys.readouterr().err


def test_a_lease_owned_by_another_vendor_is_left_alone(tmp_path, capsys) -> None:
    path = tmp_path / "worker.json"
    _record(path, provider="runpod", worker_id="pod-123")
    modal = FakeStopModal()

    assert stop.main(worker_environment(path), sdk=modal) == 1

    assert modal.from_ids == []
    assert read_lease(path).worker_id == "pod-123"
    assert "runpod" in capsys.readouterr().err.lower()


def test_a_secret_in_a_stop_failure_is_redacted(tmp_path, capsys) -> None:
    secret = "modal-secret-value-5678"
    path = tmp_path / "worker.json"
    _record(path)
    modal = FakeStopModal(terminate_error=RuntimeError(f"token {secret} rejected"))

    assert stop.main(worker_environment(path, MODAL_TOKEN_SECRET=secret), sdk=modal) == 1

    error = capsys.readouterr().err
    assert secret not in error
    assert "***" in error


def test_the_stop_path_follows_the_environment(tmp_path) -> None:
    path = tmp_path / "custom.json"
    _record(path)

    assert state_path(worker_environment(path)) == path
    clear_lease(path)
    assert not path.exists()
