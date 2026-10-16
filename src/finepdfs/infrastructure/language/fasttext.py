"""Naming the *language* of a page, not the script: fastText's lid model behind the same port.

`ScriptLanguageIdentifier` reports Latn for English and German alike. This adapter reports `eng` and
`deu`, which is what a language-tagged corpus needs. The model is a separate concern (see `model.py`),
so the page-by-page logic here is tested with a fake and the `fasttext` package is imported only when
the model is actually used: a run that stayed on the script adapter needs nothing installed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from finepdfs.domain.language import UNKNOWN_LANGUAGE, PageLanguage
from finepdfs.infrastructure.language.model import TextLabeller
from finepdfs.infrastructure.language.script import MIN_CHARACTERS

CHUNK_CHARACTERS = 1200
LANGUAGE_PREFIX = "__label__"

# lid.176 answers in whatever code its training data used, which for the well-covered languages is the
# two-letter ISO 639-1 code: `en`, `de`, `fr`. An operator's allow-list and GlotLID both use ISO 639-3
# (`eng`, `deu`, `fra`), and a corpus whose field mixes `en` with `eng` cannot be filtered at all, so the
# two-letter labels are normalised here. Taken from the ISO 639-3 code table (iso639-3.sil.org); labels
# that are already three letters pass through untouched.
ISO_639_3_BY_LID_LABEL: dict[str, str] = {"af": "afr", "am": "amh", "an": "arg", "ar": "ara", "as": "asm",
    "av": "ava", "az": "aze", "ba": "bak", "be": "bel", "bg": "bul", "bh": "bih", "bn": "ben", "bo": "bod",
    "br": "bre", "bs": "bos", "ca": "cat", "ce": "che", "co": "cos", "cs": "ces", "cv": "chv", "cy": "cym", "da": "dan",
    "de": "deu", "dv": "div", "el": "ell", "en": "eng", "eo": "epo", "es": "spa", "et": "est", "eu": "eus",
    "fa": "fas", "fi": "fin", "fr": "fra", "fy": "fry", "ga": "gle", "gd": "gla", "gl": "glg", "gn": "grn",
    "gu": "guj", "gv": "glv", "he": "heb", "hi": "hin", "hr": "hrv", "ht": "hat", "hu": "hun", "hy": "hye",
    "ia": "ina", "id": "ind", "ie": "ile", "io": "ido", "is": "isl", "it": "ita", "ja": "jpn", "jv": "jav",
    "ka": "kat", "kk": "kaz", "km": "khm", "kn": "kan", "ko": "kor", "ku": "kur", "kv": "kom", "kw": "cor",
    "ky": "kir", "la": "lat", "lb": "ltz", "li": "lim", "lo": "lao", "lt": "lit", "lv": "lav", "mg": "mlg",
    "mk": "mkd", "ml": "mal", "mn": "mon", "mr": "mar", "ms": "msa", "mt": "mlt", "my": "mya", "ne": "nep",
    "nl": "nld", "nn": "nno", "no": "nor", "oc": "oci", "or": "ori", "os": "oss", "pa": "pan", "pl": "pol",
    "ps": "pus", "pt": "por", "qu": "que", "rm": "roh", "ro": "ron", "ru": "rus", "sa": "san", "sc": "srd",
    "sd": "snd", "sh": "hbs", "si": "sin", "sk": "slk", "sl": "slv", "so": "som", "sq": "sqi", "sr": "srp",
    "su": "sun", "sv": "swe", "sw": "swa", "ta": "tam", "te": "tel", "tg": "tgk", "th": "tha", "tk": "tuk",
    "tl": "tgl", "tr": "tur", "tt": "tat", "ug": "uig", "uk": "ukr", "ur": "urd", "uz": "uzb", "vi": "vie",
    "vo": "vol", "wa": "wln", "yi": "yid", "yo": "yor", "zh": "zho"}


class LanguageModelUnavailable(RuntimeError):
    """Raised when the optional model library, or the model file it needs, is not installed."""


class LoadedModel(Protocol):
    """The slice of a loaded `fasttext` model this adapter uses, so a fake can stand in for it."""

    def predict(self, text: str, k: int = 1) -> tuple[list[str], list[float]]:
        """The model's `__label__`-prefixed labels and their scores, best first."""
        ...


def _load_model(path: str) -> LoadedModel:
    """The real `fasttext` model, imported only when one is actually asked for."""
    try:
        import fasttext  # the third-party package: this module's own name costs it nothing
    except ImportError as error:
        raise LanguageModelUnavailable(
            'the fasttext package is not installed: pip install "finepdfs[language]"'
        ) from error
    return fasttext.load_model(path)


class FastTextModel:
    """fastText's lid.176 model, loaded once for a run, answering in ISO 639-3.

    `fasttext` is imported here rather than at module import time, because it is an optional extra
    (`pip install "finepdfs[language]"`) and the model file is downloaded, not vendored. The model is
    loaded when the run is assembled, so a wrong path or a missing extra says so at startup instead of
    halfway through a corpus.

    lid.176 answers in whatever code its training data used -- `en`, `de`, `fr` -- but the seam it
    implements promises ISO 639-3, so the two-letter labels are translated here, beside the model that
    emits them, and every implementer of `TextLabeller` really does return `eng`/`deu`/`fra`. The model
    is CC-BY-SA-3.0 licensed; lid.176.bin and lid.176.ftz are published by Meta at
    dl.fbaipublicfiles.com/fasttext/supervised-models/. The loader is injectable so the translation is
    tested with a fake model object, no `fasttext` package and no network.
    """

    def __init__(self, path: str | Path, loader: Callable[[str], LoadedModel] = _load_model) -> None:
        self._path = Path(path)
        if not self._path.is_file():
            raise LanguageModelUnavailable(
                f"no fastText model at {self._path}: download lid.176.ftz and point FINEPDFS_LANGUAGE_MODEL at it"
            )
        self._model = loader(str(self._path))

    def label(self, text: str) -> tuple[str, float]:
        labels, scores = self._model.predict(text, k=1)
        if not labels:
            return UNKNOWN_LANGUAGE, 0.0
        label = labels[0].removeprefix(LANGUAGE_PREFIX)
        return ISO_639_3_BY_LID_LABEL.get(label, label), float(scores[0])


class FastTextLanguageIdentifier:
    """Turns a model's opinion about one page into a Page Language.

    A page is labelled in paragraph-sized pieces rather than whole: lid.176 is trained on short text,
    and a five-thousand-character page is a bag of words in which the most frequent language wins
    whatever the tail says. Each piece is one vote, so a piece the model cannot name dilutes the
    winner instead of silently disappearing, and the confidence is the winning language's average
    score over the pieces.

    The confidence is the model's own score, passed through untouched: which pages are trusted is the
    LanguagePolicy's, not the adapter's, so a page the model is unsure about is reported as unsure and
    dropped above. The script adapter's floor is reused for the same reason it has one: below forty
    letters neither model nor model-free rule has enough to go on.
    """

    def __init__(
        self,
        model: TextLabeller,
        min_characters: int = MIN_CHARACTERS,
        chunk_characters: int = CHUNK_CHARACTERS,
    ) -> None:
        self._model = model
        self._min_characters = min_characters
        self._chunk_characters = chunk_characters

    def identify(self, text: str, page_number: int) -> PageLanguage:
        if sum(character.isalpha() for character in text) < self._min_characters:
            return _unknown(page_number)

        chunks = _chunks(text, self._chunk_characters)
        scores: dict[str, float] = {}
        for chunk in chunks:
            language, confidence = self._model.label(chunk)
            if language in ("", UNKNOWN_LANGUAGE):
                continue
            scores[language] = scores.get(language, 0.0) + confidence

        if not scores:
            return _unknown(page_number)
        language, total = max(sorted(scores.items()), key=lambda item: item[1])
        return PageLanguage(page_number=page_number, language=language, confidence=total / len(chunks))


def _chunks(text: str, size: int) -> tuple[str, ...]:
    """The page in pieces of at most `size` characters, split on whitespace.

    Splitting on whitespace also strips the layout characters the model has no use for: it expects
    UTF-8 words, not a page image.
    """
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for word in text.split():
        extra = len(word) if not current else len(word) + 1
        if current and length + extra > size:
            chunks.append(" ".join(current))
            current, length = [word], len(word)
        else:
            current.append(word)
            length += extra
    if current:
        chunks.append(" ".join(current))
    return tuple(chunks)


def _unknown(page_number: int) -> PageLanguage:
    return PageLanguage(page_number=page_number, language=UNKNOWN_LANGUAGE, confidence=0.0)
