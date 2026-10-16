"""Choosing which language identifier a run gets, from a plain configuration.

The model-free script adapter is the default, because it always works. The trained one is selected by
name, and imported only then: `fasttext` is an optional extra, and a run that stays on the script
adapter must not need it installed or a model file downloaded.
"""

from __future__ import annotations

from dataclasses import dataclass

from finepdfs.domain.ports import LanguageIdentifier
from finepdfs.infrastructure.language.script import ScriptLanguageIdentifier

SCRIPT = "script"
FASTTEXT = "fasttext"
LANGUAGE_MODES = (SCRIPT, FASTTEXT)
DEFAULT_LANGUAGE_MODE = SCRIPT


class UnknownLanguageMode(ValueError):
    """Raised when a run names a language mode that does not exist."""


@dataclass(frozen=True, slots=True)
class LanguageConfig:
    """The language choice one run was asked for, and where its model lives if it needs one.

    A configuration object, not two bare strings, so the mode and its model travel together the way an
    engine's do; the registry is then the one place that knows which modes exist.
    """

    mode: str = DEFAULT_LANGUAGE_MODE
    model_path: str = ""


def build_language_identifier(config: LanguageConfig) -> LanguageIdentifier:
    """The identifier one run was asked for. Nothing heavy is imported until fastText is named."""
    if config.mode == SCRIPT:
        return ScriptLanguageIdentifier()
    if config.mode == FASTTEXT:
        from finepdfs.infrastructure.language.fasttext import (
            FastTextLanguageIdentifier,
            FastTextModel,
            LanguageModelUnavailable,
        )

        if not config.model_path:
            raise LanguageModelUnavailable(
                "FINEPDFS_LANGUAGE=fasttext needs FINEPDFS_LANGUAGE_MODEL=<path to a fastText lid model>"
            )
        return FastTextLanguageIdentifier(FastTextModel(config.model_path))
    raise UnknownLanguageMode(f"unknown language mode {config.mode!r}; known modes are {', '.join(LANGUAGE_MODES)}")
