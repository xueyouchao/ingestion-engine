"""A read-only local explorer over a completed run's declared outputs.

The flow is the only writer. This module reads `out/run-summary.json`, the declared record, page,
chunk and exclusion exports, the derived local search index and the corpus manifest that maps a digest
to `pdfs/<digest>.pdf`. It never curates, deduplicates or publishes, and it never writes: the browser is
a view, not a second writer. Searching reads the index the CLI's `index-search` built; an absent index is
an explicit empty state, never a crash and never a silent rebuild.

    PYTHONPATH=src python -m finepdfs.interfaces.cli explore --out out --corpus corpus

Only two roots are reachable -- the configured output directory and the configured corpus directory --
and both are addressed by validated keys and digests, never by a path from the URL. The default bind
address is loopback.
"""

from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from finepdfs.application.search import SearchAcceptedEvidence
from finepdfs.domain.search import DEFAULT_TOP_K, SearchFilters
from finepdfs.domain.source_identity import is_source_identity
from finepdfs.infrastructure.corpus.manifest import MANIFEST_NAME, CorpusManifest
from finepdfs.infrastructure.corpus.manifest import pdf_path as retained_pdf_path
from finepdfs.infrastructure.publishing.disk import (
    EXCLUDED,
    RECORDS,
    DiskPageReader,
    PublishedCorpus,
)
from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex
from finepdfs.interfaces import search_paths
from finepdfs.interfaces.explorer_page import EXPLORER_HTML
from finepdfs.interfaces.run_log import NOT_RECORDED, read_observed_run

DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
_UNSET = object()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class ExplorerError(Exception):
    """A declared output could not be read; the explorer does not guess an answer."""


class UnknownDocument(ExplorerError):
    """The requested key is not one of the documents this run declared."""


class SearchRequestError(ExplorerError):
    """The search request itself is invalid; the caller has to fix it, not the index."""


class Explorer:
    """Read-only access to one completed run's declared outputs."""

    def __init__(
        self,
        out_dir: Path | str,
        corpus_dir: Path | str,
        index_path: Path | str | None = None,
    ) -> None:
        self._out = Path(out_dir)
        self._corpus = Path(corpus_dir)
        self._published = PublishedCorpus(self._out)
        # The page exports are parsed by the same reader `read_pdf_page` uses, so the view cannot show
        # text the agent's tool would reject as stale.
        self._page_reader = DiskPageReader(self._out)
        self._index = (
            Path(index_path)
            if index_path is not None
            else self._out / search_paths.SEARCH_INDEX_NAME
        )
        self._manifest_lock = threading.Lock()
        self._manifest_signature: object = _UNSET
        self._by_url: dict[str, Any] = {}
        self._by_digest: dict[str, Any] = {}
        self._corpus_root = self._corpus.resolve()

    @property
    def out_dir(self) -> Path:
        return self._out

    @property
    def corpus_dir(self) -> Path:
        return self._corpus

    @property
    def index_path(self) -> Path:
        return self._index

    def search(
        self,
        text: str,
        *,
        filters: SearchFilters | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> dict[str, Any]:
        """Read the shared search contract from the local index; absent or unreadable is a state.

        The explorer never builds or repairs the index: it is derived state written by `index-search`
        after a run, and a view that silently reindexed would be a second writer of search state.
        """
        if not text.strip():
            raise SearchRequestError("a search needs a non-empty query")
        if top_k < 1:
            raise SearchRequestError(f"top_k must be at least 1, not {top_k}")
        reader = SearchAcceptedEvidence(
            SqliteFtsSearchIndex(self._index), self._published.read_declared_chunks
        )
        return reader.search(text, filters=filters, top_k=top_k).as_json()

    def summary(self) -> dict[str, Any]:
        """The measured counters exactly as the flow declared them, never recomputed."""
        path = self._published.summary_path()
        if not path.is_file():
            raise ExplorerError(f"no completed output at {self._out}: {path} does not exist")
        return _read_json_object(path)

    def observed_run(self) -> dict[str, Any]:
        """The observed wall-clock timing beside the outputs, or an explicit "not recorded".

        It is not declared target state: the command that ran the pipeline measured it. Absent means
        the elapsed time is not recorded, never zero and never guessed.
        """
        record = read_observed_run(self._out)
        if record is None:
            return {"recorded": False, "elapsed_seconds": None, "note": f"elapsed {NOT_RECORDED}"}
        return {"recorded": True, **record}

    def records(self) -> list[dict[str, Any]]:
        """Published records, each with the key its exports are named after and its PDF link."""
        records = []
        for path in sorted((self._out / RECORDS).glob("*.json")):
            key = path.stem
            if not is_source_identity(key):
                continue
            record = _read_json_object(path)
            record["key"] = key
            record["pdf_url"] = self._pdf_url(record.get("digest"))
            records.append(record)
        return records

    def exclusions(self) -> list[dict[str, Any]]:
        """Every declared exclusion, with its kind, reason and locally retained PDF when mapped."""
        exclusions = []
        for path in sorted((self._out / EXCLUDED).glob("*.json")):
            exclusion = _read_json_object(path)
            key = exclusion.get("key") or path.stem
            if not is_source_identity(key):
                continue
            exclusion["key"] = key
            exclusion["pdf_url"] = self._pdf_url_for_url(exclusion.get("url"))
            exclusions.append(exclusion)
        return exclusions

    def documents(self) -> list[dict[str, Any]]:
        """The status table's rows: published records and exclusions, declared state only."""
        records = {record["key"]: record for record in self.records()}
        exclusions = {exclusion["key"]: exclusion for exclusion in self.exclusions()}
        documents = []
        for key in sorted(set(records) | set(exclusions)):
            record = records.get(key)
            row = _document_row(
                key, record=record, exclusion=None if record is not None else exclusions[key]
            )
            row["text"] = self._text_for(key, record.get("digest")) if record is not None else ""
            row["detail_url"] = f"/api/documents/{key}"
            documents.append(row)
        return documents

    def pages(self, key: str) -> list[dict[str, Any]]:
        """Accepted Page Texts in original page order, each with a PDF fragment for that page."""
        if not is_source_identity(key):
            raise UnknownDocument(f"no document {key}")
        record = next((record for record in self.records() if record["key"] == key), None)
        record_digest = record.get("digest") if record else None
        pages = self._page_rows(key, record_digest)
        if pages is None:
            raise UnknownDocument(f"no page export for {key}")
        for page in pages:
            base = self._pdf_url(page.get("digest") or record_digest)
            page["pdf_url"] = f"{base}#page={page['page_number']}" if base else None
        return pages

    def document(self, key: str) -> dict[str, Any]:
        """One document's declared detail, including accepted text and original-page PDF links."""
        record = next((record for record in self.records() if record["key"] == key), None)
        if record is not None:
            detail = {**record, **_document_row(key, record=record, exclusion=None)}
            detail["pages"] = self.pages(key)
            return detail
        exclusion = next(
            (exclusion for exclusion in self.exclusions() if exclusion["key"] == key), None
        )
        if exclusion is not None:
            detail = _document_row(key, record=None, exclusion=exclusion)
            detail["pages"] = []
            return detail
        raise UnknownDocument(f"no document {key}")

    def pdf_path(self, digest: str) -> Path | None:
        """The retained PDF for a manifest digest, or None when it is absent or outside the corpus."""
        if not isinstance(digest, str) or not DIGEST_PATTERN.match(digest):
            return None
        by_digest, _ = self._manifest_state()
        if digest not in by_digest:
            return None
        path = retained_pdf_path(self._corpus, digest)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if self._corpus_root not in resolved.parents:
            return None
        return resolved

    def _pdf_url(self, digest: str | None) -> str | None:
        if not isinstance(digest, str) or self.pdf_path(digest) is None:
            return None
        return f"/pdf/{digest}.pdf"

    def _pdf_url_for_url(self, url: Any) -> str | None:
        if not isinstance(url, str):
            return None
        _, by_url = self._manifest_state()
        entry = by_url.get(url)
        return self._pdf_url(entry.digest) if entry is not None else None

    def _manifest_state(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Reload the manifest when a completed update changed it, so PDF links do not go stale."""
        path = self._corpus / MANIFEST_NAME
        try:
            stat = path.stat()
        except OSError:
            signature: object = None
        else:
            signature = (stat.st_mtime_ns, stat.st_size)
        with self._manifest_lock:
            if signature != self._manifest_signature:
                manifest = _load_manifest(self._corpus)
                self._by_url = {entry.url: entry for entry in manifest.entries}
                self._by_digest = {entry.digest: entry for entry in manifest.entries}
                self._manifest_signature = signature
            return self._by_digest, self._by_url

    def _page_rows(self, key: str, digest: Any) -> list[dict[str, Any]] | None:
        """One declared page export, read by ``DiskPageReader`` exactly as ``read_pdf_page`` reads it.

        None means no export is declared. A page whose digest no longer matches the published record,
        or whose doc_id is not this key, is not accepted evidence: the export was left behind by a
        replacement or a removal, so it is dropped rather than shown as current text. The reader keeps
        original page order, so the table's text and the detail's pages cannot disagree.
        """
        try:
            pages = self._page_reader.accepted_pages(key)
        except ValueError as error:
            raise ExplorerError(f"the page export for {key} is not accepted evidence: {error}") from error
        if pages is None:
            return None
        if not isinstance(digest, str):
            return []
        return [page.as_json() for page in pages if page.digest == digest and page.doc_id == key]

    def _text_for(self, key: str, digest: Any) -> str:
        """The accepted text of one published document, in its original page order."""
        pages = self._page_rows(key, digest) or []
        return "\n".join(str(page.get("text", "")) for page in pages)


def _document_row(
    key: str, *, record: dict[str, Any] | None, exclusion: dict[str, Any] | None
) -> dict[str, Any]:
    """One status row of declared state: a published record's fields, or an exclusion's kind and reason.

    Both the status table and the document detail read it, so the two views cannot drift into different
    answers about the same document. Callers add what only their view needs -- the table's text and
    detail link, the detail's pages.
    """
    if record is not None:
        return {
            "key": key,
            "url": record.get("url"),
            "status": "published",
            "language": record.get("language"),
            "routing_decision": record.get("routing_decision"),
            "routing_reason": record.get("routing_reason"),
            "engine": record.get("engine"),
            "page_count": record.get("page_count", 0),
            "accepted_pages": record.get("accepted_pages", 0),
            "dropped_pages": record.get("dropped_pages", 0),
            "character_count": record.get("character_count", 0),
            "exclusion_kind": None,
            "exclusion_reason": None,
            "pdf_url": record.get("pdf_url"),
        }
    exclusion = exclusion or {}
    return {
        "key": key,
        "url": exclusion.get("url"),
        "status": exclusion.get("kind", "excluded"),
        "language": None,
        "routing_decision": None,
        "routing_reason": None,
        "engine": None,
        "page_count": 0,
        "accepted_pages": 0,
        "dropped_pages": 0,
        "character_count": 0,
        "exclusion_kind": exclusion.get("kind"),
        "exclusion_reason": exclusion.get("reason"),
        "pdf_url": exclusion.get("pdf_url"),
    }


def explorer_for(
    out_dir: Path | str,
    corpus_dir: Path | str,
    index_path: Path | str | None = None,
) -> Explorer:
    """Build an explorer, refusing a directory that has no completed run summary."""
    explorer = Explorer(out_dir, corpus_dir, index_path=index_path)
    explorer.summary()
    explorer._manifest_state()
    return explorer


def _first(parameters: dict[str, list[str]], name: str) -> str | None:
    values = parameters.get(name)
    return values[0] if values else None


def _load_manifest(corpus_dir: Path) -> CorpusManifest:
    path = corpus_dir / MANIFEST_NAME
    if not path.is_file():
        return CorpusManifest()
    try:
        return CorpusManifest.load(path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ExplorerError(f"{path} could not be read as a corpus manifest: {error}") from error


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ExplorerError(f"{path} could not be read: {error}") from error


def _read_json_object(path: Path) -> dict[str, Any]:
    return _json_object(_read_text(path), path)


def _json_object(text: str, path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ExplorerError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ExplorerError(f"{path} is not valid JSON: expected an object")
    return payload


class ExplorerRequestHandler(BaseHTTPRequestHandler):
    """Serve the page and the read-only API; every path is validated, none is joined blindly."""

    explorer: Explorer
    server_version = "FinePDFsExplorer/1"
    sys_version = ""

    def do_GET(self) -> None:
        self._handle(head=False)

    def do_HEAD(self) -> None:
        self._handle(head=True)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the console quiet; the CLI already prints where it is serving."""

    def _handle(self, *, head: bool) -> None:
        path = unquote(urlsplit(self.path).path)
        if "\x00" in path or "\\" in path or ".." in path.split("/"):
            self._error(HTTPStatus.NOT_FOUND, "not found", head=head)
            return
        try:
            if path in ("/", "/index.html"):
                self._html(EXPLORER_HTML, head=head)
            elif path == "/api/summary":
                self._json(self.explorer.summary(), head=head)
            elif path == "/api/observed-run":
                self._json(self.explorer.observed_run(), head=head)
            elif path == "/api/documents":
                payload = {"summary": self.explorer.summary(), "documents": self.explorer.documents()}
                self._json(payload, head=head)
            elif path == "/api/records":
                self._json({"records": self.explorer.records()}, head=head)
            elif path == "/api/exclusions":
                self._json({"exclusions": self.explorer.exclusions()}, head=head)
            elif path == "/api/search":
                self._search(urlsplit(self.path).query, head=head)
            elif path.startswith("/api/documents/"):
                self._json(self.explorer.document(path.removeprefix("/api/documents/")), head=head)
            elif path.startswith("/api/pages/"):
                key = path.removeprefix("/api/pages/")
                self._json({"key": key, "pages": self.explorer.pages(key)}, head=head)
            elif path.startswith("/pdf/") and path.endswith(".pdf"):
                self._pdf(path.removeprefix("/pdf/").removesuffix(".pdf"), head=head)
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found", head=head)
        except UnknownDocument as error:
            self._error(HTTPStatus.NOT_FOUND, str(error), head=head)
        except SearchRequestError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error), head=head)
        except ExplorerError as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error), head=head)

    def _search(self, query_string: str, *, head: bool) -> None:
        parameters = parse_qs(query_string, keep_blank_values=True)
        text = _first(parameters, "query") or ""
        raw_top_k = _first(parameters, "top_k") or str(DEFAULT_TOP_K)
        try:
            top_k = int(raw_top_k)
        except ValueError as error:
            raise SearchRequestError(f"top_k must be an integer, not {raw_top_k!r}") from error
        filters = SearchFilters(
            language=_first(parameters, "language") or None,
            engine=_first(parameters, "engine") or None,
            doc_id=_first(parameters, "doc_id") or None,
        )
        self._json(self.explorer.search(text, filters=filters, top_k=top_k), head=head)

    def _json(self, payload: Any, *, head: bool) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _html(self, body: str, *, head: bool) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._headers("text/html; charset=utf-8", len(encoded))
        self.end_headers()
        if not head:
            self.wfile.write(encoded)

    def _error(self, status: HTTPStatus, message: str, *, head: bool = False) -> None:
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")

    def _pdf(self, digest: str, *, head: bool) -> None:
        path = self.explorer.pdf_path(digest)
        if path is None:
            self._error(HTTPStatus.NOT_FOUND, "no retained PDF for that digest", head=head)
            return
        try:
            body = path.read_bytes()
        except OSError as error:
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"could not read the retained PDF: {error}",
                head=head,
            )
            return
        start, end = 0, len(body) - 1
        status = HTTPStatus.OK
        content_range = None
        requested = self.headers.get("Range")
        if requested:
            match = RANGE_PATTERN.fullmatch(requested.strip())
            if match is None:
                self._range_not_satisfiable(len(body), head=head)
                return
            first, last = match.groups()
            if first == "" and last == "":
                self._range_not_satisfiable(len(body), head=head)
                return
            if first == "":
                length = min(int(last), len(body))
                start, end = max(len(body) - length, 0), len(body) - 1
            else:
                start = int(first)
                end = min(int(last), len(body) - 1) if last else len(body) - 1
            if start >= len(body) or start > end:
                self._range_not_satisfiable(len(body), head=head)
                return
            status = HTTPStatus.PARTIAL_CONTENT
            content_range = f"bytes {start}-{end}/{len(body)}"
        payload = body[start : end + 1]
        self.send_response(status)
        self._headers("application/pdf", len(payload))
        self.send_header("Content-Disposition", f'inline; filename="{digest}.pdf"')
        self.send_header("Accept-Ranges", "bytes")
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def _range_not_satisfiable(self, length: int, *, head: bool) -> None:
        body = json.dumps({"error": "range not satisfiable"}).encode("utf-8")
        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        self._headers("application/json; charset=utf-8", len(body))
        self.send_header("Content-Range", f"bytes */{length}")
        self.end_headers()
        if not head:
            self.wfile.write(body)


def make_server(explorer: Explorer, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """A server bound to `host:port`; port 0 asks the OS for a free port (used by tests)."""
    handler = type("BoundExplorerHandler", (ExplorerRequestHandler,), {"explorer": explorer})
    return ThreadingHTTPServer((host, port), handler)


def serve_explorer(explorer: Explorer, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Serve until interrupted; the handler itself never writes."""
    server = make_server(explorer, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
