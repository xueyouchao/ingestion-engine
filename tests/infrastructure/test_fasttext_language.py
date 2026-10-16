"""Naming the language, not the script: a model behind a small seam, and the fastText adapter.

The unit tests inject a fake labeller, so they need neither the model nor the `fasttext` package. The
one network test at the bottom uses the real model and skips cleanly when it is not on this machine.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from finepdfs.domain.language import UNKNOWN_LANGUAGE
from finepdfs.infrastructure.language.fasttext import (
    FastTextLanguageIdentifier,
    FastTextModel,
    LanguageModelUnavailable,
)
from finepdfs.infrastructure.language.registry import build_language_identifier
from finepdfs.infrastructure.language.script import ScriptLanguageIdentifier

ENGLISH = "This resolution concerns the vandalism of weather instruments in remote stations, and it " * 2
GERMAN = (
    "Diese Entschließung betrifft die Beschädigung von Wetterinstrumenten an abgelegenen Stationen "
    "und die daraus entstehenden Reparaturkosten für die betroffenen Behörden. " * 2
)


class FixedLabeller:
    """A stand-in for the model: the same language and score, whatever text it is shown."""

    def __init__(self, language: str, confidence: float) -> None:
        self._language = language
        self._confidence = confidence
        self.calls: list[str] = []

    def label(self, text: str) -> tuple[str, float]:
        self.calls.append(text)
        return self._language, self._confidence


class KeywordLabeller:
    """A stand-in for the model: one answer per keyword it finds, and unknown for the rest."""

    def __init__(self, answers: dict[str, tuple[str, float]]) -> None:
        self._answers = answers
        self.calls: list[str] = []

    def label(self, text: str) -> tuple[str, float]:
        self.calls.append(text)
        for keyword, answer in self._answers.items():
            if keyword in text:
                return answer
        return UNKNOWN_LANGUAGE, 0.0


def test_a_page_is_named_in_the_corpus_language_codes_not_the_models_own() -> None:
    # lid.176 answers "en"; the corpus, the allow-list and GlotLID all use ISO 639-3, so "eng".
    tag = FastTextLanguageIdentifier(FixedLabeller("en", 0.98)).identify(ENGLISH, page_number=1)

    assert tag.language == "eng"
    assert tag.confidence == 0.98
    assert tag.page_number == 1


def test_a_page_with_too_little_text_is_unknown_not_a_guess() -> None:
    labeller = FixedLabeller("eng", 0.99)

    tag = FastTextLanguageIdentifier(labeller).identify("3", page_number=5)

    assert tag.language == UNKNOWN_LANGUAGE
    assert tag.confidence == 0.0
    assert labeller.calls == []


def test_a_label_that_is_already_iso_639_3_is_left_alone() -> None:
    tag = FastTextLanguageIdentifier(FixedLabeller("deu", 0.9)).identify(GERMAN, page_number=2)

    assert tag.language == "deu"


def test_a_low_confidence_is_reported_as_it_is_for_the_policy_to_drop() -> None:
    tag = FastTextLanguageIdentifier(FixedLabeller("en", 0.4)).identify(ENGLISH, page_number=1)

    assert tag.language == "eng"
    assert tag.confidence == 0.4


def test_a_page_that_fits_is_shown_to_the_model_whole() -> None:
    labeller = FixedLabeller("eng", 0.9)

    FastTextLanguageIdentifier(labeller).identify(ENGLISH, page_number=1)

    assert len(labeller.calls) == 1
    assert labeller.calls[0].split() == ENGLISH.split()


def test_a_long_page_is_labelled_in_pieces_and_the_winning_language_is_the_strongest_vote() -> None:
    # A chunk size of four characters makes every word of this page its own chunk, which is what makes
    # the arithmetic below a hand-written expectation rather than a restatement of the splitter.
    labeller = KeywordLabeller({"english": ("en", 0.9), "german": ("de", 0.6)})
    page = " ".join(["english"] * 10 + ["german"] * 10)

    tag = FastTextLanguageIdentifier(labeller, chunk_characters=4).identify(page, page_number=1)

    assert len(labeller.calls) == 20
    assert tag.language == "eng"
    assert tag.confidence == pytest.approx(0.45)


def test_a_piece_the_model_cannot_name_is_a_vote_for_nobody() -> None:
    labeller = KeywordLabeller({"english": ("en", 0.9)})
    page = " ".join(["english"] * 5 + ["gibberish"] * 5)

    tag = FastTextLanguageIdentifier(labeller, chunk_characters=4).identify(page, page_number=1)

    assert tag.language == "eng"
    assert tag.confidence == pytest.approx(0.45)


def test_a_page_no_piece_of_which_can_be_named_is_unknown() -> None:
    labeller = FixedLabeller(UNKNOWN_LANGUAGE, 0.0)

    tag = FastTextLanguageIdentifier(labeller).identify(ENGLISH, page_number=1)

    assert tag.language == UNKNOWN_LANGUAGE
    assert tag.confidence == 0.0


def test_a_model_file_that_is_not_there_is_an_error_that_says_where_to_put_it() -> None:
    with pytest.raises(LanguageModelUnavailable, match="FINEPDFS_LANGUAGE_MODEL"):
        FastTextModel("/nonexistent/models/lid.176.ftz")


@pytest.mark.network
def test_the_real_model_tells_english_from_german_where_the_script_adapter_cannot() -> None:
    identifier = build_language_identifier("fasttext", _real_model())

    english = identifier.identify(ENGLISH, page_number=1)
    german = identifier.identify(GERMAN, page_number=2)

    assert (english.language, german.language) == ("eng", "deu")
    assert min(english.confidence, german.confidence) >= 0.65
    assert ScriptLanguageIdentifier().identify(GERMAN, page_number=2).language == "Latn"


def _real_model() -> str:
    """The downloaded model this machine was given, or a clean skip: it is not vendored."""
    path = os.environ.get("FINEPDFS_LANGUAGE_MODEL", "")
    if not path or not Path(path).is_file():
        pytest.skip("set FINEPDFS_LANGUAGE_MODEL to a fastText lid model to exercise the real tagger")
    if importlib.util.find_spec("fasttext") is None:
        pytest.skip('the optional extra is not installed: pip install "finepdfs[language]"')
    return path
