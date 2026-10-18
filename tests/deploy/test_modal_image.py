"""The worker image: pinned by tag and digest, carrying the launcher, and free of secrets.

The image is not built here (that would need Modal); the fake records every pin and file the definition
asks for, which is what a reviewer can check offline.
"""

from __future__ import annotations

from pathlib import Path

from fake_modal import FakeImage

from deploy.gpu.contract import (
    MODEL_ID,
    MODEL_REVISION,
    VLLM_IMAGE,
    WORKER_MODEL,
    WORKER_PORT,
)
from deploy.gpu.server import MODEL_VARIABLE, PORT_VARIABLE, REVISION_VARIABLE, SERVED_MODEL_VARIABLE
from deploy.modal.image import HF_CACHE_PATH, SERVER_PATH, WORKER_ENTRYPOINT, worker_image


class FakeImageModal:
    def __init__(self) -> None:
        self.image = FakeImage()
        self.registry: tuple[str, str | None] | None = None
        self.Image = self

    def from_registry(self, tag: str, add_python: str | None = None) -> FakeImage:
        self.registry = (tag, add_python)
        return self.image


def test_the_image_is_the_pinned_vllm_release_by_tag_and_digest() -> None:
    modal = FakeImageModal()

    worker_image(modal)

    assert modal.registry == (VLLM_IMAGE, None)
    assert "v0.32.0" in VLLM_IMAGE
    assert VLLM_IMAGE.endswith("@sha256:" + "6ee133356d49ed24cee03dd0cda3d3980279a11c107ce3e752c89e76540f41c9")


def test_the_image_carries_the_model_revision_and_served_name_as_environment() -> None:
    modal = FakeImageModal()

    worker_image(modal)

    assert modal.image.environment == {
        MODEL_VARIABLE: MODEL_ID,
        REVISION_VARIABLE: MODEL_REVISION,
        SERVED_MODEL_VARIABLE: WORKER_MODEL,
        PORT_VARIABLE: str(WORKER_PORT),
        "HF_HOME": HF_CACHE_PATH,
    }


def test_the_image_entrypoint_is_the_server_launcher_it_copies_in(tmp_path: Path) -> None:
    modal = FakeImageModal()

    worker_image(modal)

    [(local, remote, copy)] = modal.image.files
    assert Path(local).name == "server.py"
    assert Path(local).exists()
    assert remote == SERVER_PATH
    assert copy is True, "the launcher must be in the image layer, not mounted at start"
    assert modal.image.entrypoint_argv == WORKER_ENTRYPOINT
    assert modal.image.entrypoint_argv == ["python3", SERVER_PATH]


def test_the_image_definition_bakes_in_no_secret() -> None:
    modal = FakeImageModal()

    worker_image(modal)

    assert not any("TOKEN" in name or "SECRET" in name for name in modal.image.environment)
