"""The command line surface: every command parses, and flags mean what they say."""

from __future__ import annotations

import pytest

from finepdfs.interfaces.cli import build_parser


@pytest.mark.parametrize(
    "argv, expected_command",
    [
        (["fetch-corpus", "--domain", "noaa.gov"], "fetch-corpus"),
        (["inspect", "--corpus", "c"], "inspect"),
        (["dry-run", "--corpus", "c", "--engine", "mineru"], "dry-run"),
        (["run", "--corpus", "c", "--out", "o", "--engine", "paddleocr-vl"], "run"),
    ],
)
def test_every_command_keeps_its_own_name(argv, expected_command) -> None:
    args = build_parser().parse_args(argv)

    assert args.command == expected_command


def test_the_similarity_threshold_reaches_the_runtime(monkeypatch) -> None:
    from finepdfs.interfaces import cli

    args = build_parser().parse_args(["run", "--similarity-threshold", "0.55"])
    monkeypatch.setattr(cli, "runtime_from_environment", lambda: "a runtime")

    cli._runtime_from_args(args)

    assert args.similarity_threshold == 0.55
    import os

    assert os.environ["FINEPDFS_SIMILARITY_THRESHOLD"] == "0.55"


def test_engine_flags_do_not_shadow_the_command_name() -> None:
    args = build_parser().parse_args(["run", "--engine", "mineru", "--engine-command", "mineru", "--dpi", "150"])

    assert args.command == "run"
    assert args.engine == "mineru"
    assert args.engine_command == "mineru"
    assert args.dpi == 150


def test_an_unknown_engine_is_rejected() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--engine", "magic-ocr"])


def test_fetch_corpus_needs_a_domain() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch-corpus"])
