"""The vendor-neutral worker contract: one JSON endpoint, a /v1 base URL, and a durable lease.

These tests are the cheap gate ADR-0009 asks for: they prove the contract without a GPU, without the
Modal package and without the network. The provider integration test at the end is the important one:
it feeds the contract's own output to the adapter the pipeline already ships.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

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
    state = contract.LeaseState(
        provider=contract.PROVIDER_MODAL,
        worker_id="sb-abc123",
        app_name="finepdfs-gpu-worker",
        created_at="2026-10-18T00:00:00+00:00",
    )

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


def test_the_state_file_is_named_by_the_environment_then_defaults(tmp_path) -> None:
    explicit = tmp_path / "lease.json"

    assert contract.state_path({contract.STATE_FILE: str(explicit)}) == explicit
    assert contract.state_path({}) == Path(contract.DEFAULT_STATE_FILE).expanduser()


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
