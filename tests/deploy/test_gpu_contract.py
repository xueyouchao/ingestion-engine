"""The vendor-neutral worker contract: one JSON endpoint, a /v1 base URL, and a durable lease.

These tests are the cheap gate ADR-0009 asks for: they prove the contract without a GPU, without the
Modal package and without the network. The provider integration test at the end is the important one:
it feeds the contract's own output to the adapter the pipeline already ships.
"""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest
from lease_holder import hold_reservation_then_die

from deploy.gpu import contract
from finepdfs.infrastructure.compute.providers import GpuStartFailed, endpoint_from_start_output

TUNNEL = "https://tunnel-abc123.modal.host"


def test_the_endpoint_is_one_json_object_with_a_v1_base_url() -> None:
    stream = io.StringIO()

    line = contract.emit_endpoint(contract.endpoint_payload(contract.base_url_with_v1(TUNNEL)), stream)

    assert stream.getvalue() == line + "\n", "stdout is exactly one line, not a log plus a payload"
    assert "\n" not in line
    payload = json.loads(line)
    assert payload == {
        "base_url": "https://tunnel-abc123.modal.host/v1",
        "model": "PaddleOCR-VL-1.6",
        "detail": contract.WORKER_DETAIL,
    }


def test_the_v1_base_path_is_added_once_and_only_once() -> None:
    assert contract.base_url_with_v1(TUNNEL) == TUNNEL + "/v1"
    assert contract.base_url_with_v1(TUNNEL + "/") == TUNNEL + "/v1"
    assert contract.base_url_with_v1(TUNNEL + "/v1") == TUNNEL + "/v1"
    assert contract.base_url_with_v1(TUNNEL + "/v1/") == TUNNEL + "/v1"


def test_an_endpoint_without_the_v1_base_path_is_refused() -> None:
    """ChatEndpoint appends only /chat/completions, so a bare host would call the wrong route."""
    with pytest.raises(contract.WorkerContractError, match="/v1"):
        contract.endpoint_payload(TUNNEL)

    with pytest.raises(contract.WorkerContractError, match="/v1"):
        contract.endpoint_payload(TUNNEL + "/v2")


@pytest.mark.parametrize("base_url", ["", "   ", "tunnel-abc123.modal.host/v1", "ftp://tunnel/v1", TUNNEL + "/v1?x=1"])
def test_a_base_url_that_is_not_one_plain_http_v1_address_is_refused(base_url: str) -> None:
    with pytest.raises(contract.WorkerContractError):
        contract.endpoint_payload(base_url)


def test_the_detail_names_the_model_and_its_versions_and_never_the_address() -> None:
    payload = contract.endpoint_payload(contract.base_url_with_v1(TUNNEL))

    detail = payload["detail"]
    assert detail.startswith("paddleocr-vl@1.6:weights-")
    assert contract.MODEL_REVISION[:12] in detail
    assert contract.VLLM_IMAGE_DIGEST[:12] in detail
    assert "://" not in detail
    assert "modal.host" not in detail
    assert TUNNEL not in detail


def test_the_detail_names_the_model_its_version_and_both_pinned_versions() -> None:
    """Read the detail the way a consumer does, instead of comparing it with the constant that built it.

    ``WORKER_DETAIL`` is assembled from the pins, so asserting equality only proves the assembly runs.
    This parses the shape, checks the model and version the vendor published, and proves each digest is
    a prefix of the pin it claims to name -- so a detail that silently reported the wrong weights or a
    host would fail here.
    """
    detail = contract.endpoint_payload(contract.base_url_with_v1(TUNNEL))["detail"]

    model, _, rest = detail.partition("@")
    version, _, attributes = rest.partition(":")
    weights, _, image = attributes.partition(":")

    assert model == "paddleocr-vl", "the detail names the served model, not a host"
    assert version == "1.6", "the detail carries the model version a reader can compare"
    assert weights.startswith("weights-") and image.startswith("image-")
    assert contract.MODEL_REVISION.startswith(weights.removeprefix("weights-")), "those are the pinned weights"
    assert contract.VLLM_IMAGE_DIGEST.startswith(image.removeprefix("image-")), "that is the pinned image"
    assert contract.VLLM_IMAGE.endswith("@sha256:" + contract.VLLM_IMAGE_DIGEST)
    assert re.fullmatch(r"paddleocr-vl@1\.6:weights-[0-9a-f]{12}:image-[0-9a-f]{12}", detail)
    assert "://" not in detail
    assert TUNNEL not in detail and "modal.host" not in detail


@pytest.mark.parametrize(
    "detail",
    [
        "paddleocr-vl@1.6:https://tunnel-abc123.modal.host",
        "paddleocr-vl@1.6 weights",
        "",
    ],
)
def test_a_detail_that_carries_an_address_or_is_not_one_token_is_refused(detail: str) -> None:
    with pytest.raises(contract.WorkerContractError):
        contract.endpoint_payload(contract.base_url_with_v1(TUNNEL), detail=detail)


def test_the_existing_command_provider_reads_the_contract_payload() -> None:
    """The contract is only useful if the adapter that already ships consumes it unchanged (ADR-0009)."""
    line = contract.endpoint_line(contract.endpoint_payload(contract.base_url_with_v1(TUNNEL)))

    endpoint = endpoint_from_start_output(line, "python -m deploy.modal.start")

    assert endpoint.base_url == "https://tunnel-abc123.modal.host/v1"
    assert endpoint.model == contract.WORKER_MODEL
    assert endpoint.detail == contract.WORKER_DETAIL
    assert endpoint.api_key == ""
    assert "://" not in endpoint.detail


def test_malformed_start_output_is_refused_by_the_provider_not_carried_into_a_run() -> None:
    """A start that prints logs but no endpoint cannot start a run; this is that consumer-side check."""
    with pytest.raises(GpuStartFailed):
        endpoint_from_start_output("starting the worker...\n", "python -m deploy.modal.start")

    with pytest.raises(GpuStartFailed):
        endpoint_from_start_output('{"detail": "paddleocr-vl@1.6:weights-x:image-y"}', "start")


def test_the_lease_records_the_worker_and_no_address_or_secret(tmp_path) -> None:
    path = tmp_path / "nested" / "worker.json"
    state = contract.LeaseState(provider=contract.PROVIDER_MODAL, worker_id="sb-abc123")

    contract.write_lease(path, state)

    assert contract.read_lease(path) == state
    on_disk = path.read_text(encoding="utf-8")
    assert "sb-abc123" in on_disk
    assert TUNNEL not in on_disk
    assert not (tmp_path / "nested" / ".worker.json.tmp").exists(), "the atomic scratch file is replaced"


def test_a_missing_lease_is_none_not_an_error(tmp_path) -> None:
    assert contract.read_lease(tmp_path / "absent.json") is None


def test_a_partially_started_worker_can_still_be_read_back(tmp_path) -> None:
    """Readiness is not in the lease: the id is written before the wait, which is the whole point."""
    path = tmp_path / "worker.json"
    contract.write_lease(path, contract.LeaseState(provider=contract.PROVIDER_MODAL, worker_id="sb-half-started"))

    assert contract.read_lease(path).worker_id == "sb-half-started"


def test_an_unreadable_lease_names_the_file_that_should_have_described_the_worker(tmp_path) -> None:
    path = tmp_path / "worker.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(contract.WorkerContractError, match=r"worker\.json"):
        contract.read_lease(path)


def test_a_lease_without_a_worker_id_is_invalid(tmp_path) -> None:
    path = tmp_path / "worker.json"
    path.write_text(json.dumps({"provider": "modal"}), encoding="utf-8")

    with pytest.raises(contract.WorkerContractError, match="worker_id"):
        contract.read_lease(path)


def test_each_lease_is_recorded_in_its_own_file(tmp_path) -> None:
    """One shared state file is what let one run's stop terminate another run's live worker."""
    directory = tmp_path / "leases"
    first = {contract.STATE_DIR_VARIABLE: str(directory), contract.LEASE_ID_VARIABLE: "a" * 32}
    second = {contract.STATE_DIR_VARIABLE: str(directory), contract.LEASE_ID_VARIABLE: "b" * 32}

    assert contract.lease_file(first) == directory / f"{'a' * 32}.json"
    assert contract.lease_file(second) == directory / f"{'b' * 32}.json"
    assert contract.lease_file(first) != contract.lease_file(second), "two leases never share a file"


def test_the_lease_identity_variable_is_the_one_the_provider_passes() -> None:
    """The provider mints the identity; these scripts read it. One concept, one spelling."""
    from finepdfs.infrastructure.compute.providers import GPU_LEASE_ID

    assert contract.LEASE_ID_VARIABLE == GPU_LEASE_ID


def test_the_state_directory_is_named_by_the_environment_then_defaults(tmp_path) -> None:
    explicit = tmp_path / "leases"

    assert contract.state_directory({contract.STATE_DIR_VARIABLE: str(explicit)}) == explicit
    assert contract.state_directory({}) == Path(contract.DEFAULT_STATE_DIR).expanduser()
    # A present-but-empty value is the unset value, not the path ".": exporting an empty variable must
    # not turn stop into an "unreadable lease" report about a directory.
    assert contract.state_directory({contract.STATE_DIR_VARIABLE: ""}) == Path(contract.DEFAULT_STATE_DIR).expanduser()
    assert contract.state_directory({contract.STATE_DIR_VARIABLE: "   "}) == Path(contract.DEFAULT_STATE_DIR).expanduser()


def test_a_start_run_by_hand_records_under_one_fixed_name(tmp_path) -> None:
    """No provider mints an identity for a hand-run start, so start and stop in one shell must still meet."""
    values = {contract.STATE_DIR_VARIABLE: str(tmp_path)}

    assert contract.lease_file(values).name == contract.UNIDENTIFIED_LEASE + ".json"
    assert contract.lease_file({**values, contract.LEASE_ID_VARIABLE: ""}).name == (
        contract.UNIDENTIFIED_LEASE + ".json"
    )


@pytest.mark.parametrize("lease_id", ["../escape", "a/b", "a b", "x" * 65, "?"])
def test_a_lease_identity_that_is_not_one_file_name_is_refused(tmp_path, lease_id: str) -> None:
    """The identity comes from the environment, so it may not name a path outside the state directory."""
    with pytest.raises(contract.WorkerContractError, match=contract.LEASE_ID_VARIABLE):
        contract.lease_file({contract.STATE_DIR_VARIABLE: str(tmp_path), contract.LEASE_ID_VARIABLE: lease_id})


def test_secrets_are_redacted_from_text_that_would_be_printed() -> None:
    environ = {
        "MODAL_TOKEN_SECRET": "sekret-token-value",
        "HF_TOKEN": "hf_abcdefgh",
        "FINEPDFS_GPU_TYPE": "L4",
    }

    redacted = contract.redact("auth failed: sekret-token-value hf_abcdefgh on L4", environ)

    assert "sekret-token-value" not in redacted
    assert "hf_abcdefgh" not in redacted
    assert redacted.count("***") == 2
    assert "L4" in redacted, "only credential-looking variables are redacted"


def test_a_short_value_under_a_secret_name_is_left_alone() -> None:
    """Redacting a two-character value would shred every message that happens to contain it."""
    assert contract.redact("key is ab", {"HF_TOKEN": "ab"}) == "key is ab"


def test_a_url_in_text_is_redacted_even_though_no_variable_names_it() -> None:
    """The tunnel URL is the credential (runbook): redaction cannot rely on an environment name."""
    redacted = contract.redact(
        f"the tunnel {TUNNEL}/v1/models refused; ftp://tunnel-abc123.modal.host/v1; key sekret",
        {"MODAL_TOKEN_SECRET": "sekret"},
    )

    assert TUNNEL not in redacted
    assert "modal.host" not in redacted
    assert "sekret" not in redacted
    assert redacted.count("***") == 3, "a mistyped scheme must not be a way to print the tunnel host"


def test_a_rejected_base_url_is_not_quoted_back_by_the_contract() -> None:
    """An operator needs to know which rule failed, not the address that is also the credential."""
    with pytest.raises(contract.WorkerContractError) as caught:
        contract.endpoint_payload(TUNNEL)  # right scheme, wrong path

    message = str(caught.value)
    assert "/v1" in message, "the message still says what was wrong"
    assert TUNNEL not in message
    assert "modal.host" not in message


def test_a_base_url_with_a_query_is_refused_without_repeating_it() -> None:
    with pytest.raises(contract.WorkerContractError) as caught:
        contract.endpoint_payload(TUNNEL + "/v1?token=sekret")

    message = str(caught.value)
    assert "query or fragment" in message
    assert "sekret" not in message
    assert "modal.host" not in message


@pytest.mark.parametrize(
    "detail",
    [
        "paddleocr-vl",  # nothing names a version
        "paddleocr-vl@",  # an empty version
        "paddleocr-vl@:",  # an empty version before the attributes
        "paddleocr-vl@:weights-x:image-y",
        "@1.6:weights-x:image-y",  # nothing names the model
        "paddleocr-vl@1.6@extra:weights-x",  # more than one identity in one token
    ],
)
def test_a_detail_that_does_not_name_a_model_and_a_version_is_refused(detail: str) -> None:
    """The detail is the deployment half of the memo key: without a version it cannot tell two apart."""
    with pytest.raises(contract.WorkerContractError, match="model@version"):
        contract.endpoint_payload(contract.base_url_with_v1(TUNNEL), detail=detail)


def test_a_detail_that_carries_a_url_is_refused_without_repeating_it() -> None:
    with pytest.raises(contract.WorkerContractError, match="URL") as caught:
        contract.endpoint_payload(contract.base_url_with_v1(TUNNEL), detail=f"paddleocr-vl@1.6:{TUNNEL}")

    assert TUNNEL not in str(caught.value)


def _second_start_is_refused(path: Path) -> str:
    """Ask for a reservation that is already held, and return the message that refused it."""
    with (
        pytest.raises(contract.WorkerContractError, match="reserv") as caught,
        contract.lease_reservation(path),
    ):
        raise AssertionError("a second reservation must not be granted")
    return str(caught.value)


def test_a_lease_reservation_is_held_until_the_lease_is_written_and_is_never_read_as_a_lease(tmp_path) -> None:
    """Reading the lease and creating the worker must be one critical section, so the gate is owned."""
    path = tmp_path / "leases" / "manual.json"
    reservation = contract.reservation_path(path)

    with contract.lease_reservation(path):
        assert reservation.exists()
        assert sorted(path.parent.glob("*.json")) == [], "a reservation is not a lease"
        _second_start_is_refused(path)

    assert reservation.exists(), "the lock file stays: unlinking it could split the lock's inode"
    assert contract.read_lease(path) is None, "a lock file is never read as a lease"


def test_a_lease_reservation_is_released_when_the_start_fails(tmp_path) -> None:
    """Sandbox.create or write_lease failing must not leave a reservation nobody can clear."""
    path = tmp_path / "manual.json"

    with pytest.raises(RuntimeError, match="quota"), contract.lease_reservation(path):
        raise RuntimeError("quota exceeded")

    with contract.lease_reservation(path):
        pass


def test_a_live_reservation_refuses_a_second_one_without_inventing_a_holder(tmp_path) -> None:
    """Each acquisition opens its own descriptor, and flock locks per descriptor: threads conflict too.

    The refusal names the lease, not a pid: the lock file is bookkeeping, not a claim about who is
    alive, and an unrelated process that inherited the pid must not be reported as the owner.
    """
    path = tmp_path / "manual.json"

    with contract.lease_reservation(path):
        message = _second_start_is_refused(path)

    assert "manual.json" in message, "the message names the lease that is taken"
    assert "(pid" not in message, "the holder is unnameable: a pid here would be a guess"
    assert str(os.getpid()) not in message


def test_a_lock_file_naming_an_unrelated_live_pid_does_not_block_the_lease(tmp_path) -> None:
    """A crashed start leaves ``manual.json.lock`` behind, and the pid in it may belong to anyone now.

    Pid reuse is what broke crash recovery: the file named a process that had never reserved anything,
    the old liveness check called that process the holder, and the next start refused to rent for ever
    (this reproducer failed against that design with "another start (pid ...) is reserving"). Nothing
    holds the lock here, so the reservation is granted whatever a leftover file happens to say.
    """
    path = tmp_path / "manual.json"
    reservation = contract.reservation_path(path)
    reservation.parent.mkdir(parents=True, exist_ok=True)
    reservation.write_text(str(os.getpid()), encoding="utf-8")

    with contract.lease_reservation(path) as held:
        assert held == reservation

    with contract.lease_reservation(path):
        pass


def test_a_crashed_holder_releases_the_reservation_when_its_process_dies(tmp_path) -> None:
    """The kernel releases the lock with the process, which is what makes a crash recoverable.

    A start killed while renting leaves its worker id unrecorded and its lock held. The child here takes
    the real reservation and dies without releasing it. The parent first shows the lock is held while the
    child is alive -- a reservation that refused nothing would prove nothing about the release -- then
    lets the child die and asks for the same reservation again. No pid is read anywhere, so the only thing
    that can free the lock is the OS releasing it with the process.
    """
    path = tmp_path / "manual.json"
    lock_file = tmp_path / "manual.json.lock"
    holder = hold_reservation_then_die(path, tmp_path)

    try:
        holder.wait_until_holding()
        assert lock_file.exists(), "the live child's lock file is on disk while it holds the lock"
        _second_start_is_refused(path)
        status = holder.die_holding()
    finally:
        if holder.process.poll() is None:  # pragma: no cover - only on a failed assertion above
            holder.kill()

    assert status == 1, "the child died holding the reservation rather than releasing it first"
    assert lock_file.exists(), "the dead start's lock file is still there"

    with contract.lease_reservation(path) as granted:
        assert granted == lock_file, "and it is not the lock: the reservation is granted again"


def test_the_deployed_app_pins_one_container_a_zero_floor_and_the_l4_rate() -> None:
    """The deploy posture is a pin, not a deploy-time variable: one GPU, scale to zero between visits.

    The rate is Modal's published L4 price observed on 2026-10-19, with the page it came from, because
    the Warm Control's number and the runbook's number must be the same number.
    """
    assert contract.DEPLOYED_APP_NAME == "finepdfs-demo-ocr"
    assert contract.DEPLOYED_SERVER_NAME == "OcrWorkerServer"
    assert contract.DEPLOYED_SECRET_NAME == "finepdfs-demo-ocr-bearer"
    assert contract.DEPLOYED_GPU == "L4"
    assert contract.DEPLOYED_MAX_CONTAINERS == 1
    assert contract.DEPLOYED_MIN_CONTAINERS == 0
    assert 120 <= contract.DEPLOYED_SCALEDOWN_WINDOW <= 300, "the low minutes ADR-0012 asks for"
    assert contract.DEPLOYED_STARTUP_TIMEOUT >= 900, "the first weights load was measured in minutes"
    assert contract.DEPLOYED_EXIT_GRACE_PERIOD > 0
    assert contract.GPU_RATE_PER_SECOND_USD == 0.000222
    assert contract.GPU_RATE_SOURCE == "https://modal.com/pricing"
    assert contract.GPU_RATE_OBSERVED == "2026-10-19"


def test_a_held_warm_container_costs_the_pinned_rate_per_minute_and_per_hour() -> None:
    """The number the Warm Control names comes from the contract, so no command can invent one."""
    cost = contract.warm_floor_cost(1)

    assert cost.containers == 1
    assert cost.per_minute_usd == pytest.approx(0.01332)
    assert cost.per_hour_usd == pytest.approx(0.7992)
    assert cost.per_minute() == "$0.0133"
    assert cost.per_hour() == "$0.80"

    assert contract.warm_floor_cost(0).per_minute() == "$0.0000"
    assert contract.warm_floor_cost(0).per_hour() == "$0.00"
    assert contract.warm_floor_cost(2).per_minute() == "$0.0266"
