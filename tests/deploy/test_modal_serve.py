"""The deployed app: one container, a zero floor, one bearer, and a hook that waits for the worker.

The app is built at module scope -- ``modal deploy`` needs to find ``app`` there -- so the fake SDK is
installed as ``sys.modules["modal"]`` before this module is imported into the test. Nothing here reaches
Modal, a GPU or the network; the pins are asserted against the fake and the readiness wait against a fake
worker process.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest
from fake_modal import FakeClock, FakeDeployModal, FakePopen, FakeProcess, FakeResponse, ModelsOpener

from deploy.gpu.contract import (
    DEPLOYED_APP_NAME,
    DEPLOYED_EXIT_GRACE_PERIOD,
    DEPLOYED_GPU,
    DEPLOYED_MAX_CONTAINERS,
    DEPLOYED_MIN_CONTAINERS,
    DEPLOYED_SCALEDOWN_WINDOW,
    DEPLOYED_SECRET_NAME,
    DEPLOYED_SECRET_VARIABLE,
    DEPLOYED_SERVER_NAME,
    DEPLOYED_STARTUP_TIMEOUT,
    MODEL_ID,
    MODEL_REVISION,
    WORKER_MODEL,
    WORKER_PORT,
)
from deploy.gpu.server import (
    API_KEY_VARIABLE,
    MODEL_VARIABLE,
    PORT_VARIABLE,
    REVISION_VARIABLE,
    SERVED_MODEL_VARIABLE,
    serve_argv,
)
from deploy.modal.image import HF_CACHE_PATH, HF_CACHE_VOLUME, WORKER_ENTRYPOINT

BEARER = "s3cret-demo-bearer"


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch):
    """Import ``serve`` with the fake SDK installed, and forget it again afterwards."""
    modal = FakeDeployModal()
    monkeypatch.setitem(sys.modules, "modal", modal)
    sys.modules.pop("deploy.modal.serve", None)
    module = importlib.import_module("deploy.modal.serve")
    yield modal, module
    sys.modules.pop("deploy.modal.serve", None)


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        MODEL_VARIABLE: MODEL_ID,
        REVISION_VARIABLE: MODEL_REVISION,
        SERVED_MODEL_VARIABLE: WORKER_MODEL,
        PORT_VARIABLE: str(WORKER_PORT),
        API_KEY_VARIABLE: BEARER,
    }
    values.update(overrides)
    return values


def test_the_app_is_built_at_module_scope_for_modal_deploy(deployed) -> None:
    modal, serve = deployed

    assert serve.app is modal.apps[0]
    assert serve.app.name == DEPLOYED_APP_NAME
    assert DEPLOYED_SERVER_NAME in serve.app.servers


def test_the_server_declares_the_pinned_image_the_volume_and_the_secret(deployed) -> None:
    modal, serve = deployed
    options = serve.app.servers[DEPLOYED_SERVER_NAME].options

    assert options["image"] is modal.image
    assert modal.image.environment[MODEL_VARIABLE] == MODEL_ID
    assert modal.image.environment[SERVED_MODEL_VARIABLE] == WORKER_MODEL
    assert options["volumes"] == {HF_CACHE_PATH: f"volume-{HF_CACHE_VOLUME}"}
    assert modal.volumes == [(HF_CACHE_VOLUME, True)]
    [secret] = modal.secrets
    assert secret.name == DEPLOYED_SECRET_NAME
    assert secret.required_keys == [DEPLOYED_SECRET_VARIABLE], "the Secret must carry the launcher's variable"


def test_the_server_is_one_container_with_a_zero_floor_and_a_generous_startup_timeout(deployed) -> None:
    _modal, serve = deployed
    options = serve.app.servers[DEPLOYED_SERVER_NAME].options

    assert options["gpu"] == DEPLOYED_GPU == "L4"
    assert options["port"] == WORKER_PORT
    assert options["max_containers"] == DEPLOYED_MAX_CONTAINERS == 1
    assert options["min_containers"] == DEPLOYED_MIN_CONTAINERS == 0
    assert options["scaledown_window"] == DEPLOYED_SCALEDOWN_WINDOW
    assert options["startup_timeout"] == DEPLOYED_STARTUP_TIMEOUT
    assert options["exit_grace_period"] == DEPLOYED_EXIT_GRACE_PERIOD


def test_the_vendor_proxy_auth_is_off_in_favour_of_the_bearer_the_app_checks(deployed) -> None:
    _modal, serve = deployed
    options = serve.app.servers[DEPLOYED_SERVER_NAME].options

    assert options["unauthenticated"] is True, "ADR-0012: the adapter cannot send Modal-Key/Modal-Secret"
    assert DEPLOYED_SECRET_VARIABLE == API_KEY_VARIABLE, "the Secret is read as the launcher's variable"


def test_the_start_hook_spawns_the_worker_and_waits_for_models_with_the_bearer(deployed) -> None:
    _modal, serve = deployed
    process = FakeProcess()
    popen = FakePopen(process)
    opener = ModelsOpener(failures=1)
    environ = _environment()

    started = serve.start_worker(environ, popen=popen, opener=opener, sleep=lambda _: None)

    assert started is process
    assert popen.commands == [list(WORKER_ENTRYPOINT)], "the hook spawns the shared launcher"
    assert popen.environments == [{**environ}]
    launcher = serve_argv(environ)
    assert launcher[launcher.index("--api-key") + 1] == BEARER, "the launcher is what passes the bearer to vLLM"
    assert opener.urls == [f"http://127.0.0.1:{WORKER_PORT}/v1/models"] * 2
    assert opener.api_keys == [BEARER, BEARER]


def test_the_readiness_wait_asks_models_with_the_bearer(deployed, monkeypatch: pytest.MonkeyPatch) -> None:
    _modal, serve = deployed
    seen: list[tuple[str, str | None]] = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization")))
        return FakeResponse({"object": "list", "data": [{"id": WORKER_MODEL, "object": "model"}]})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    serve.wait_for_models(f"http://127.0.0.1:{WORKER_PORT}/v1", BEARER, sleep=lambda _: None)

    assert seen == [(f"http://127.0.0.1:{WORKER_PORT}/v1/models", f"Bearer {BEARER}")]


def test_the_start_hook_refuses_to_serve_without_a_bearer(deployed) -> None:
    """Fail closed: an endpoint nobody authenticated is a bill waiting to happen (ADR-0012)."""
    _modal, serve = deployed
    popen = FakePopen()

    with pytest.raises(serve.ServeConfigurationError, match=API_KEY_VARIABLE):
        serve.start_worker(
            _environment(**{API_KEY_VARIABLE: ""}), popen=popen, opener=ModelsOpener(), sleep=lambda _: None
        )

    assert popen.commands == [], "the worker is not even spawned without a bearer"


def test_a_worker_that_never_answers_models_is_stopped_instead_of_reported_ready(deployed) -> None:
    _modal, serve = deployed
    process = FakeProcess()
    popen = FakePopen(process)

    with pytest.raises(serve.ServeConfigurationError, match="/v1/models"):
        serve.start_worker(
            _environment(),
            popen=popen,
            opener=ModelsOpener(failures=99),
            sleep=lambda _: None,
            clock=FakeClock(0, DEPLOYED_STARTUP_TIMEOUT + 1),
        )

    assert process.terminated, "a worker that never became ready must not be left running"


def test_the_enter_hook_starts_the_worker_and_the_exit_hook_stops_it(deployed, monkeypatch) -> None:
    _modal, serve = deployed
    process = FakeProcess()
    started: list[dict[str, str]] = []

    def fake_start(environ):
        started.append(dict(environ))
        return process

    monkeypatch.setattr(serve, "start_worker", fake_start)
    registration = serve.app.servers[DEPLOYED_SERVER_NAME]

    worker = registration.user_cls()
    worker.start()
    worker.stop()

    assert started == [dict(os.environ)]
    assert getattr(registration.user_cls.start, "modal_enter", False) is True
    assert getattr(registration.user_cls.stop, "modal_exit", False) is True
    assert process.terminated is True
