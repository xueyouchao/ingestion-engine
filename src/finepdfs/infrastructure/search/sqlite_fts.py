"""A local lexical index, in SQLite FTS5 from the standard library.

This is the single shipped search backend. The source spec proposed a pinned single-node
Elasticsearch; no stable deployment was available inside the budget and no endpoint or package is
configured, so this is the fallback the spec allows behind the *same* record contract -- not a second
permanent design, and not a backend the browser or the agent can see (ADR-0008). There are no
embeddings.

The index is built from the declared chunk files, never from the corpus: the flow is still the only
writer of what is accepted, and this module only stores, deletes and ranks what the flow declared.
Every row is one chunk, so the same chunk cannot appear twice; ``retain`` deletes the rows of chunks
that are no longer declared.

Two connections exist and they are deliberately different. ``upsert``/``retain``/``record_freshness``
are the rebuild, run by the operator's ``index-search``, and may create schema. ``search``/``state`` are
read-only: SQLite is opened with ``mode=ro``, no schema is created and nothing is written, so serving
the explorer can never become a second writer or rebuild a database behind the flow's back.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from urllib.parse import quote

from finepdfs.domain.chunks import Chunk
from finepdfs.domain.search import IndexFailure, IndexState, SearchHit, SearchQuery, SearchUnavailable

SNIPPET_START = "<mark>"
SNIPPET_END = "</mark>"
SNIPPET_ELLIPSIS = "…"
SNIPPET_TOKENS = 32

# ``content='chunks'`` keeps the text in one place; the triggers below are what synchronise the FTS
# index with it, including on the ``ON CONFLICT DO UPDATE`` that makes a reindex idempotent.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    chunk_id TEXT NOT NULL UNIQUE,
    doc_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    digest TEXT NOT NULL,
    engine TEXT NOT NULL,
    language TEXT NOT NULL,
    chunking_version TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_by_doc_id ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS chunks_by_language ON chunks (language);
CREATE INDEX IF NOT EXISTS chunks_by_engine ON chunks (engine);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_after_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_after_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_after_update AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FRESHNESS_KEY = "declared_fingerprint"
_FRESHNESS_UPSERT = """
INSERT INTO index_meta (key, value) VALUES (:key, :value)
ON CONFLICT (key) DO UPDATE SET value = excluded.value
"""

_UPSERT = """
INSERT INTO chunks (
    chunk_id, doc_id, source_url, digest, engine, language, chunking_version,
    ordinal, page_number, start_offset, end_offset, text
) VALUES (
    :chunk_id, :doc_id, :source_url, :digest, :engine, :language, :chunking_version,
    :ordinal, :page_number, :start_offset, :end_offset, :text
)
ON CONFLICT (chunk_id) DO UPDATE SET
    doc_id = excluded.doc_id,
    source_url = excluded.source_url,
    digest = excluded.digest,
    engine = excluded.engine,
    language = excluded.language,
    chunking_version = excluded.chunking_version,
    ordinal = excluded.ordinal,
    page_number = excluded.page_number,
    start_offset = excluded.start_offset,
    end_offset = excluded.end_offset,
    text = excluded.text
"""

_SELECT = """
SELECT c.chunk_id, c.doc_id, c.source_url, c.digest, c.engine, c.language,
       c.chunking_version, c.ordinal, c.page_number, c.start_offset, c.end_offset,
       c.text,
       snippet(chunks_fts, 0, :snippet_start, :snippet_end, :snippet_ellipsis, :snippet_tokens) AS snippet,
       bm25(chunks_fts) AS rank
FROM chunks_fts
JOIN chunks AS c ON c.id = chunks_fts.rowid
WHERE chunks_fts MATCH :expression
"""

# A user's punctuation is not FTS5 syntax: tokenise the query and quote every term, then OR them so a
# multi-word question still returns evidence. BM25 ranks chunks that match more terms higher.
_WORD = re.compile(r"\w+", re.UNICODE)


class SqliteFtsSearchIndex:
    """The SQLite FTS5 adapter behind the ``SearchIndex`` port."""

    def __init__(self, path: pathlib.Path | str) -> None:
        self._path = pathlib.Path(path)

    @property
    def path(self) -> pathlib.Path:
        return self._path

    def upsert(self, chunks: Sequence[Chunk]) -> tuple[IndexFailure, ...]:
        """Store the chunks, idempotently, reporting every chunk that could not be stored."""
        failures: list[IndexFailure] = []
        seen: dict[str, Chunk] = {}
        with self._write_connection() as connection:
            for chunk in chunks:
                previous = seen.get(chunk.chunk_id)
                if previous is not None:
                    if previous != chunk:
                        failures.append(
                            IndexFailure(
                                chunk.chunk_id,
                                "two different chunks claim the same chunk_id; the first one was kept",
                            )
                        )
                    continue
                seen[chunk.chunk_id] = chunk
                try:
                    connection.execute(_UPSERT, _parameters(chunk))
                except sqlite3.Error as error:
                    failures.append(IndexFailure(chunk.chunk_id, f"{type(error).__name__}: {error}"))
        return tuple(failures)

    def retain(self, chunk_ids: Collection[str]) -> int:
        """Delete every stored chunk whose id is not declared any more; return the number removed."""
        with self._write_connection() as connection:
            connection.execute("CREATE TEMP TABLE IF NOT EXISTS current_chunk_ids (chunk_id TEXT PRIMARY KEY)")
            connection.execute("DELETE FROM current_chunk_ids")
            connection.executemany(
                "INSERT OR IGNORE INTO current_chunk_ids (chunk_id) VALUES (?)",
                ((chunk_id,) for chunk_id in chunk_ids),
            )
            removed = connection.execute(
                "DELETE FROM chunks WHERE chunk_id NOT IN (SELECT chunk_id FROM current_chunk_ids)"
            )
            return removed.rowcount

    def search(self, query: SearchQuery) -> tuple[SearchHit, ...]:
        expression = _match_expression(query.text)
        if expression is None:
            return ()

        sql = _SELECT
        parameters: dict[str, object] = {
            "expression": expression,
            "snippet_start": SNIPPET_START,
            "snippet_end": SNIPPET_END,
            "snippet_ellipsis": SNIPPET_ELLIPSIS,
            "snippet_tokens": SNIPPET_TOKENS,
            "top_k": query.top_k,
        }
        for column, value in (
            ("language", query.filters.language),
            ("engine", query.filters.engine),
            ("doc_id", query.filters.doc_id),
        ):
            if value is not None:
                sql += f" AND c.{column} = :{column}"
                parameters[column] = value
        # ``bm25`` is negative and lower is better; order by it and flip the sign only for the report.
        sql += " ORDER BY rank, c.chunk_id LIMIT :top_k"

        with self._read_only_connection() as connection:
            try:
                rows = connection.execute(sql, parameters).fetchall()
            except sqlite3.Error as error:
                raise SearchUnavailable(f"the search index at {self._path} could not be read: {error}") from error
        return tuple(_hit(row) for row in rows)

    def record_freshness(self, fingerprint: str) -> None:
        """Record which declared evidence this index now mirrors; only the rebuild writes it."""
        with self._write_connection() as connection:
            connection.execute(_FRESHNESS_UPSERT, {"key": FRESHNESS_KEY, "value": fingerprint})

    def state(self) -> IndexState:
        """What the index mirrors, or ``SearchUnavailable`` naming the state and what to run."""
        _require_readable(self._path)
        with self._read_only_connection() as connection:
            try:
                row = connection.execute(
                    "SELECT value FROM index_meta WHERE key = ?", (FRESHNESS_KEY,)
                ).fetchone()
                chunks = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
            except sqlite3.Error as error:
                raise SearchUnavailable(
                    f"the search index at {self._path} is not a search index ({error});"
                    " run finepdfs index-search"
                ) from error
        return IndexState(fingerprint=str(row[0]) if row else None, chunks=chunks)

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        """The rebuild's connection: it may create the schema, because index-search is its only caller."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        try:
            connection.executescript(_SCHEMA)
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _read_only_connection(self) -> Iterator[sqlite3.Connection]:
        """A reader's connection: ``mode=ro``, no schema, no mkdir, nothing written."""
        _require_readable(self._path)
        uri = f"file:{quote(str(self._path), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as error:
            raise SearchUnavailable(f"the search index at {self._path} could not be read: {error}") from error
        try:
            yield connection
        finally:
            connection.close()


def _require_readable(path: pathlib.Path) -> None:
    """The two states a reader can name before opening SQLite at all."""
    if not path.is_file():
        raise SearchUnavailable(f"no search index at {path}; run finepdfs index-search after a completed run")
    if path.stat().st_size == 0:
        raise SearchUnavailable(f"the search index at {path} is empty; run finepdfs index-search")


def _parameters(chunk: Chunk) -> dict[str, object]:
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "source_url": chunk.source_url,
        "digest": chunk.digest,
        "engine": chunk.engine,
        "language": chunk.language,
        "chunking_version": chunk.chunking_version,
        "ordinal": chunk.ordinal,
        "page_number": chunk.page_number,
        "start_offset": chunk.start_offset,
        "end_offset": chunk.end_offset,
        "text": chunk.text,
    }


def _match_expression(text: str) -> str | None:
    terms = _WORD.findall(text)
    if not terms:
        return None
    return " OR ".join(f'"{term}"' for term in terms)


def _hit(row: tuple) -> SearchHit:
    (
        chunk_id,
        doc_id,
        source_url,
        digest,
        engine,
        language,
        chunking_version,
        ordinal,
        page_number,
        start_offset,
        end_offset,
        text,
        snippet,
        rank,
    ) = row
    return SearchHit(
        doc_id=doc_id,
        chunk_id=chunk_id,
        source_url=source_url,
        digest=digest,
        engine=engine,
        language=language,
        chunking_version=chunking_version,
        ordinal=ordinal,
        page_number=page_number,
        start_offset=start_offset,
        end_offset=end_offset,
        text=text,
        snippet=snippet,
        score=-float(rank),
    )
