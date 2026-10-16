"""Choosing which language identifier a run gets: the script one by default, fastText on request."""

from __future__ import annotations

import pytest

from finepdfs.infrastructure.cocoindex.runtime import runtime_from_environment
from finepdfs.infrastructure.language.registry import UnknownLanguageMode, build_language_identifier
from finepdfs.infrastructure.language.script import ScriptLanguageIdentifier

MODEL = "/models/lid.176.ftz"


def test_the_script_adapter_is_the_default_and_keeps_the_policy_floor() -> None:
    identifier = build_language_identifier("script", model_path="")

    assert isinstance(identifier, ScriptLanguageIdentifier)
    assert identifier.identify("This resolution concerns the vandalism of weather instruments, and it " * 2, 1).language == "Latn"


def test_asking_for_fasttext_without_a_model_is_an_error_not_a_silent_downgrade() -> None:
    with pytest.raises(UnknownLanguageMode, match="FINEPDFS_LANGUAGE_MODEL"):
        build_language_identifier("fasttext", model_path="")


def test_an_unknown_mode_says_what_the_modes_are() -> None:
    with pytest.raises(UnknownLanguageMode, match="script, fasttext"):
        build_language_identifier("glotlid", model_path=MODEL)


def test_the_runtime_takes_the_language_mode_from_the_environment() -> None:
    assert isinstance(runtime_from_environment({}).curation.languages, ScriptLanguageIdentifier)

    with pytest.raises(UnknownLanguageMode):
        runtime_from_environment({"FINEPDFS_LANGUAGE": "glotlid"})
