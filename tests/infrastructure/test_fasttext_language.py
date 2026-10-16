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
    ISO_639_3_BY_LID_LABEL,
    LANGUAGE_PREFIX,
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

# The two-letter labels lid.176 actually emits, read from the model's own label set (123 of its 176
# labels; the other 53 are already three letters). It is fixed for this model, so it is the durable,
# model-free guard on the map below: a missing translation fails here with no model and no network.
LID_176_TWO_LETTER_LABELS = frozenset({
    "af", "am", "an", "ar", "as", "av", "az", "ba", "be", "bg", "bh", "bn", "bo", "br", "bs", "ca",
    "ce", "co", "cs", "cv", "cy", "da", "de", "dv", "el", "en", "eo", "es", "et", "eu", "fa", "fi",
    "fr", "fy", "ga", "gd", "gl", "gn", "gu", "gv", "he", "hi", "hr", "ht", "hu", "hy", "ia", "id",
    "ie", "io", "is", "it", "ja", "jv", "ka", "kk", "km", "kn", "ko", "ku", "kv", "kw", "ky", "la",
    "lb", "li", "lo", "lt", "lv", "mg", "mk", "ml", "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn",
    "no", "oc", "or", "os", "pa", "pl", "ps", "pt", "qu", "rm", "ro", "ru", "sa", "sc", "sd", "sh",
    "si", "sk", "sl", "so", "sq", "sr", "su", "sv", "sw", "ta", "te", "tg", "th", "tk", "tl", "tr",
    "tt", "ug", "uk", "ur", "uz", "vi", "vo", "wa", "yi", "yo", "zh",
})


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


class RawModel:
    """The shape `fasttext.load_model` returns: `predict` answers with `__label__`-prefixed labels."""

    def __init__(self, label: str, score: float) -> None:
        self._label = label
        self._score = score

    def predict(self, text: str, k: int = 1) -> tuple[list[str], list[float]]:
        return ([f"{LANGUAGE_PREFIX}{self._label}"], [self._score])


def test_the_model_answers_in_iso_639_3_not_lid_176s_own_code() -> None:
    # lid.176 answers "en"; the seam promises ISO 639-3, so the translation belongs to the model that
    # speaks lid.176 -- otherwise the identifier above it would have to know which model it wraps.
    model = FastTextModel(__file__, loader=lambda _path: RawModel("en", 0.97))

    assert model.label("anything") == ("eng", 0.97)


def test_the_identifier_records_the_labellers_iso_639_3_code_untouched() -> None:
    tag = FastTextLanguageIdentifier(FixedLabeller("eng", 0.98)).identify(ENGLISH, page_number=1)

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
    tag = FastTextLanguageIdentifier(FixedLabeller("eng", 0.4)).identify(ENGLISH, page_number=1)

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
    labeller = KeywordLabeller({"english": ("eng", 0.9), "german": ("deu", 0.6)})
    page = " ".join(["english"] * 10 + ["german"] * 10)

    tag = FastTextLanguageIdentifier(labeller, chunk_characters=4).identify(page, page_number=1)

    assert len(labeller.calls) == 20
    assert tag.language == "eng"
    assert tag.confidence == pytest.approx(0.45)


def test_a_piece_the_model_cannot_name_is_a_vote_for_nobody() -> None:
    labeller = KeywordLabeller({"english": ("eng", 0.9)})
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


def test_the_map_covers_every_two_letter_label_lid_176_can_emit() -> None:
    # Equality, not just coverage: an entry that is not a real label is as wrong as a missing one.
    assert set(ISO_639_3_BY_LID_LABEL) == LID_176_TWO_LETTER_LABELS


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
