"""The Modal stop command: terminate the recorded worker, and be safe when there is nothing to terminate.

A stop that fails must not erase the only handle on a worker that may still be billing, and a stop that
finds nothing must exit 0 so the provider can run it unconditionally (ADR-0007, ADR-0009).
"""

from __future__ import annotations

from pathlib import Path

from fake_modal import FakeStopModal, NotFoundError, worker_environment

from deploy.gpu.contract import (
    LeaseState,
    lease_file,
    read_lease,
    write_lease,
)
from deploy.modal import stop


def _record(directory: Path, provider: str = "modal", worker_id: str = "sb-abc123") -> dict[str, str]:
    """Record one lease in the state directory this environment names, and return that environment."""
    values = worker_environment(directory)
    write_lease(lease_file(values), LeaseState(provider=provider, worker_id=worker_id))
    return values


def test_stop_terminates_the_worker_named_by_the_lease(tmp_path, capsys) -> None:
    values = _record(tmp_path / "leases")
    modal = FakeStopModal()

    assert stop.main(values, sdk=modal) == 0

    assert modal.from_ids == ["sb-abc123"]
    assert modal.terminated == ["sb-abc123"]
    assert read_lease(lease_file(values)) is None, "a terminated worker is no longer recorded"
    assert "terminated Modal worker sb-abc123" in capsys.readouterr().err


def test_stop_is_idempotent_when_no_worker_is_recorded(tmp_path, capsys) -> None:
    modal = FakeStopModal()

    assert stop.main(worker_environment(tmp_path / "absent"), sdk=modal) == 0

    assert modal.from_ids == [], "nothing is terminated that was never recorded"
    assert "nothing to stop" in capsys.readouterr().err


def test_stop_tolerates_a_partially_started_worker(tmp_path) -> None:
    """The id is recorded before readiness, so this is the state a failed readiness wait leaves."""
    values = _record(tmp_path / "leases", worker_id="sb-never-ready")
    modal = FakeStopModal()

    assert stop.main(values, sdk=modal) == 0

    assert modal.terminated == ["sb-never-ready"]
    assert read_lease(lease_file(values)) is None


def test_an_already_gone_worker_is_success_not_failure(tmp_path, capsys) -> None:
    values = _record(tmp_path / "leases")
    modal = FakeStopModal(from_id_error=NotFoundError("no such sandbox"))

    assert stop.main(values, sdk=modal) == 0

    assert read_lease(lease_file(values)) is None, "nothing is billing, so the stale lease is cleared"
    assert "already gone" in capsys.readouterr().err


def test_a_failed_stop_exits_nonzero_and_keeps_the_lease_for_a_retry(tmp_path, capsys) -> None:
    values = _record(tmp_path / "leases")
    modal = FakeStopModal(terminate_error=RuntimeError("the worker is still out there"))

    assert stop.main(values, sdk=modal) == 1

    assert read_lease(lease_file(values)) is not None, "the only handle on a maybe-billing worker must survive"
    assert "the worker is still out there" in capsys.readouterr().err


def test_a_malformed_lease_is_reported_and_nothing_is_terminated(tmp_path, capsys) -> None:
    values = worker_environment(tmp_path / "leases")
    lease_file(values).parent.mkdir(parents=True)
    lease_file(values).write_text("{not json", encoding="utf-8")
    modal = FakeStopModal()

    assert stop.main(values, sdk=modal) == 1

    assert modal.from_ids == []
    assert str(lease_file(values)) in capsys.readouterr().err


def test_an_unusable_lease_identity_is_reported_not_raised(tmp_path, capsys) -> None:
    """The identity comes from the operator's environment, so stop must report a bad one like any other failure.

    It used to be read outside the handler, so it escaped as a traceback -- printing the value
    unredacted. An operator who exported the tunnel URL here would then see the credential the runbook
    says never reaches a diagnostic; a typo would see a traceback instead of one line naming the variable.
    """
    values = worker_environment(tmp_path / "leases", FINEPDFS_GPU_LEASE_ID="https://tunnel-abc123.modal.host/v1")
    modal = FakeStopModal()

    assert stop.main(values, sdk=modal) == 1

    error = capsys.readouterr().err
    assert "FINEPDFS_GPU_LEASE_ID" in error, "the message names the variable to fix"
    assert "modal.host" not in error, "and never echoes the value, which may be an address"
    assert "Traceback" not in error
    assert modal.from_ids == [], "an unusable identity terminates nothing"


def test_a_lease_owned_by_another_vendor_is_left_alone(tmp_path, capsys) -> None:
    values = _record(tmp_path / "leases", provider="runpod", worker_id="pod-123")
    modal = FakeStopModal()

    assert stop.main(values, sdk=modal) == 1

    assert modal.from_ids == []
    assert read_lease(lease_file(values)).worker_id == "pod-123"
    assert "runpod" in capsys.readouterr().err.lower()


def test_a_secret_in_a_stop_failure_is_redacted(tmp_path, capsys) -> None:
    secret = "modal-secret-value-5678"
    values = _record(tmp_path / "leases")
    values["MODAL_TOKEN_SECRET"] = secret
    modal = FakeStopModal(terminate_error=RuntimeError(f"token {secret} rejected"))

    assert stop.main(values, sdk=modal) == 1

    error = capsys.readouterr().err
    assert secret not in error
    assert "***" in error


def test_the_stop_path_follows_the_environment(tmp_path, capsys) -> None:
    """Stop terminates the worker the environment names, in the directory it names.

    This test used to compare a path helper and then clear the file itself, so it passed with stop.py
    deleted. It drives ``stop.main``: the lease lives in the state directory the environment chose, the
    same environment stops that worker, and an environment naming somewhere else finds nothing.
    """
    values = _record(tmp_path / "environment-chosen")
    modal = FakeStopModal()

    assert stop.main(values, sdk=modal) == 0

    assert modal.terminated == ["sb-abc123"], "stop read the lease from the directory the environment named"
    assert not lease_file(values).exists(), "and cleared that lease once the worker was gone"
    assert "terminated Modal worker sb-abc123" in capsys.readouterr().err

    elsewhere = FakeStopModal()
    assert stop.main(worker_environment(tmp_path / "somewhere-else"), sdk=elsewhere) == 0
    assert elsewhere.from_ids == [], "the same stop pointed at another directory terminates nothing"
