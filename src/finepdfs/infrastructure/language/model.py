"""What a language model must offer to sit behind the language identifier.

The identifier is the adapter the pipeline sees; the model is the thing that is expensive to install,
download and test. Keeping them apart is what lets the adapter's page-by-page logic be tested with a
hand-written fake, with no model file and no `fasttext` package. The real model is in `fasttext.py`.
"""

from __future__ import annotations

from typing import Protocol


class TextLabeller(Protocol):
    """Names the language of a piece of text, and how sure it is."""

    def label(self, text: str) -> tuple[str, float]:
        """The most likely ISO 639-3 code for the text and its score in [0, 1].

        A labeller that cannot answer returns the unknown language with a score of zero rather than
        raising: a page nobody can name is a page the LanguagePolicy drops, not a failed run.
        """
        ...
