"""The Modal image and entrypoint for the PaddleOCR-VL worker.

One image, no secrets baked in. It is the pinned vLLM OpenAI image (model support for PaddleOCR-VL
arrived in vLLM 0.11.1; this pins the stable release observed on 2026-10-18 by tag and amd64 manifest
digest), plus the launcher below as its entrypoint. Weights are downloaded by vLLM from the pinned
Hugging Face revision at container start into a Modal Volume, so a run does not pay the multi-gigabyte
download again; no credential is part of the image or the command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deploy.gpu.contract import (
    MODEL_ID,
    MODEL_REVISION,
    VLLM_IMAGE,
    WORKER_MODEL,
    WORKER_PORT,
)
from deploy.gpu.server import (
    MODEL_VARIABLE,
    PORT_VARIABLE,
    REVISION_VARIABLE,
    SERVED_MODEL_VARIABLE,
)

SERVER_PATH = "/opt/finepdfs/gpu/server.py"
# The vLLM image installs `python3`; `python` is not guaranteed to be on its PATH.
WORKER_ENTRYPOINT = ["python3", SERVER_PATH]

# Weights are hundreds of megabytes and identical between runs, so they live on a workspace volume
# instead of being downloaded into every fresh sandbox.
HF_CACHE_VOLUME = "finepdfs-hf-cache"
HF_CACHE_PATH = "/root/.cache/huggingface"


def worker_image(modal: Any) -> Any:
    """Build the worker image through the SDK the caller supplied.

    The SDK is passed in rather than imported here so this module stays importable without Modal: the
    offline suite builds it against a fake image and asserts every pin (ADR-0009).
    """
    server = Path(__file__).resolve().parents[1] / "gpu" / "server.py"
    image = modal.Image.from_registry(VLLM_IMAGE, add_python=None)
    return (
        image.env(
            {
                MODEL_VARIABLE: MODEL_ID,
                REVISION_VARIABLE: MODEL_REVISION,
                SERVED_MODEL_VARIABLE: WORKER_MODEL,
                PORT_VARIABLE: str(WORKER_PORT),
                "HF_HOME": HF_CACHE_PATH,
            }
        )
        .add_local_file(server, SERVER_PATH, copy=True)
        .entrypoint(list(WORKER_ENTRYPOINT))
    )
