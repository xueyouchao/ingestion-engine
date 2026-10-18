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
from finepdfs.infrastructure.corpus.manifest import MANIFEST_NAME, CorpusManifest
from finepdfs.infrastructure.corpus.manifest import pdf_path as retained_pdf_path
from finepdfs.infrastructure.publishing.disk import (
    EXCLUDED,
    PAGES,
    RECORDS,
    PublishedCorpus,
)
from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex
from finepdfs.interfaces import search_paths
from finepdfs.interfaces.run_log import NOT_RECORDED, read_observed_run

KEY_PATTERN = re.compile(r"^[0-9a-f]{16}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
_UNSET = object()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# The index name and the resolver live in interfaces/search_paths.py so `index-search`, `search`,
# `explore` and `ask` cannot disagree about where the derived index is.
SEARCH_INDEX_NAME = search_paths.SEARCH_INDEX_NAME


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
        self._index = Path(index_path) if index_path is not None else self._out / SEARCH_INDEX_NAME
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
            if not KEY_PATTERN.match(key):
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
            if not isinstance(key, str) or not KEY_PATTERN.match(key):
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
            if key in records:
                record = records[key]
                documents.append(
                    {
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
                        "text": self._text_for(key),
                        "detail_url": f"/api/documents/{key}",
                    }
                )
                continue
            exclusion = exclusions[key]
            documents.append(
                {
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
                    "text": "",
                    "detail_url": f"/api/documents/{key}",
                }
            )
        return documents

    def pages(self, key: str) -> list[dict[str, Any]]:
        """Accepted Page Texts in original page order, each with a PDF fragment for that page."""
        if not KEY_PATTERN.match(key):
            raise UnknownDocument(f"no document {key}")
        path = self._out / PAGES / f"{key}.jsonl"
        if not path.is_file():
            raise UnknownDocument(f"no page export for {key}")
        record = next((record for record in self.records() if record["key"] == key), None)
        record_digest = record.get("digest") if record else None
        pages = []
        for line in _read_text(path).splitlines():
            if not line.strip():
                continue
            page = _json_object(line, path)
            number = page.get("page_number")
            if not isinstance(number, int):
                raise ExplorerError(f"{path} holds a page without an integer page_number")
            digest = page.get("digest") or record_digest
            base = self._pdf_url(digest)
            page["pdf_url"] = f"{base}#page={number}" if base else None
            pages.append(page)
        return sorted(pages, key=lambda page: page["page_number"])

    def document(self, key: str) -> dict[str, Any]:
        """One document's declared detail, including accepted text and original-page PDF links."""
        records = {record["key"]: record for record in self.records()}
        if key in records:
            record = records[key]
            detail = dict(record)
            detail.update(
                {
                    "status": "published",
                    "exclusion_kind": None,
                    "exclusion_reason": None,
                    "pages": self.pages(key),
                }
            )
            return detail
        exclusions = {exclusion["key"]: exclusion for exclusion in self.exclusions()}
        if key in exclusions:
            exclusion = exclusions[key]
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
                "pages": [],
            }
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

    def _text_for(self, key: str) -> str:
        path = self._out / PAGES / f"{key}.jsonl"
        if not path.is_file():
            return ""
        texts = []
        for line in _read_text(path).splitlines():
            if line.strip():
                page = _json_object(line, path)
                texts.append(str(page.get("text", "")))
        return "\n".join(texts)


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
    try:
        payload = json.loads(_read_text(path))
    except json.JSONDecodeError as error:
        raise ExplorerError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ExplorerError(f"{path} is not valid JSON: expected an object")
    return payload


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


EXPLORER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>PDF curation explorer</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f7f7f5;
    --panel: #ffffff;
    --ink: #1d2433;
    --muted: #5b6474;
    --line: #d8dce3;
    --accent: #225ea8;
    --ok: #17663a;
    --warn: #8a4b00;
    --bad: #9c1c1c;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14181f;
      --panel: #1d232d;
      --ink: #e8ecf3;
      --muted: #a9b3c2;
      --line: #333c4a;
      --accent: #78b7ff;
      --ok: #7fd3a2;
      --warn: #f2c078;
      --bad: #ff9c9c;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0 auto;
    max-width: 1180px;
    padding: 1.25rem;
    background: var(--bg);
    color: var(--ink);
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header h1 { margin: 0 0 .25rem; font-size: 1.6rem; }
  .scope, .meta { color: var(--muted); margin: .25rem 0; }
  .summary {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: .6rem;
    margin: 1rem 0;
  }
  .summary div {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .5rem;
    padding: .6rem .7rem;
  }
  .summary dt { color: var(--muted); font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; }
  .summary dd { margin: .15rem 0 0; font-weight: 600; overflow-wrap: anywhere; }
  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: .6rem;
    align-items: end;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .5rem;
    padding: .75rem;
  }
  .filters label { display: grid; gap: .2rem; font-size: .8rem; color: var(--muted); }
  .search {
    margin-top: .9rem;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .5rem;
    padding: .75rem;
  }
  .search form { display: flex; flex-wrap: wrap; gap: .6rem; align-items: end; }
  .search label { display: grid; gap: .2rem; font-size: .8rem; color: var(--muted); }
  #search-query { min-width: 20rem; }
  #search-doc { min-width: 10rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  #search-top-k { width: 6rem; }
  #search-results { margin-top: .75rem; }
  .hit { border-top: 1px solid var(--line); padding: .75rem 0; }
  .hit h3 { margin: 0 0 .2rem; font-size: 1rem; }
  .hit .meta { font-size: .85rem; overflow-wrap: anywhere; }
  .hit p.snippet { margin: .35rem 0; }
  .hit mark { background: color-mix(in srgb, var(--warn) 35%, transparent); color: inherit; }
  .hit details { margin-top: .35rem; }
  .hit pre {
    margin: .4rem 0 0;
    padding: .6rem;
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: .35rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .hit a, .hit button.link {
    color: var(--accent);
    background: none;
    border: 0;
    padding: 0;
    font: inherit;
    cursor: pointer;
  }
  input, select, button {
    font: inherit;
    color: var(--ink);
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: .35rem;
    padding: .38rem .5rem;
  }
  input[type="search"] { min-width: 18rem; }
  button { cursor: pointer; }
  button:hover { border-color: var(--accent); }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; background: var(--panel); }
  caption { text-align: left; padding: .5rem 0; color: var(--muted); }
  th, td { text-align: left; padding: .5rem .55rem; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
  tbody tr { cursor: pointer; }
  tbody tr:hover, tbody tr.selected { background: color-mix(in srgb, var(--accent) 12%, transparent); }
  td.url { max-width: 28rem; overflow-wrap: anywhere; }
  .status { font-weight: 600; white-space: nowrap; }
  .status-published { color: var(--ok); }
  .status-dropped, .status-failed { color: var(--bad); }
  .status-exact-duplicate, .status-near-duplicate { color: var(--warn); }
  #document-detail {
    margin-top: 1.25rem;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .5rem;
    padding: 1rem;
  }
  #document-detail h2 { margin-top: 0; overflow-wrap: anywhere; }
  #document-detail dl { display: grid; grid-template-columns: minmax(9rem, 14rem) 1fr; gap: .25rem .8rem; }
  #document-detail dt { color: var(--muted); }
  #document-detail dd { margin: 0; overflow-wrap: anywhere; }
  .page { border-top: 1px solid var(--line); padding: .8rem 0; }
  .page h4 { margin: 0 0 .35rem; }
  .page a { color: var(--accent); }
  .page pre {
    margin: .5rem 0 0;
    padding: .7rem;
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: .35rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .error { color: var(--bad); font-weight: 600; }
  @media (max-width: 700px) {
    table { display: block; overflow-x: auto; }
    #document-detail dl { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<header>
  <h1>PDF curation explorer</h1>
  <p class="scope">
    Read-only view over completed declared outputs. Counters are the measured
    <code>out/run-summary.json</code>; this read-only page never curates, deduplicates or publishes.
  </p>
  <p id="run-meta" class="meta" role="status" aria-live="polite">Loading run summary...</p>
</header>
<dl class="summary" id="summary" aria-label="Measured run summary">
  <div><dt>Corpus</dt><dd id="summary-corpus">-</dd></div>
  <div><dt>Routing</dt><dd id="summary-routing">-</dd></div>
  <div><dt>Extraction</dt><dd id="summary-extraction">-</dd></div>
  <div><dt>Published</dt><dd id="summary-published">-</dd></div>
  <div><dt>Exclusions</dt><dd id="summary-exclusions">-</dd></div>
  <div><dt>Elapsed (observed)</dt><dd id="summary-elapsed">not recorded</dd></div>
  <div><dt>Engine</dt><dd id="summary-engine">-</dd></div>
</dl>
<section class="filters" aria-label="Filters">
  <label for="filter-url">URL or text
    <input id="filter-url" type="search" placeholder="filter by URL, accepted text or reason">
  </label>
  <label for="filter-language">Language / script
    <select id="filter-language"><option value="">All</option></select>
  </label>
  <label for="filter-routing">Routing
    <select id="filter-routing"><option value="">All</option></select>
  </label>
  <label for="filter-kind">Status / exclusion kind
    <select id="filter-kind"><option value="">All</option></select>
  </label>
  <button id="refresh" type="button">Refresh now</button>
  <span id="refresh-state" class="meta" role="status" aria-live="polite"></span>
</section>
<section class="search" aria-label="Accepted evidence search">
  <form id="search-form" role="search">
    <label for="search-query">Accepted evidence search
      <input id="search-query" type="search" placeholder="words from accepted pages">
    </label>
    <label for="search-language">Language / script
      <input id="search-language" type="search" placeholder="any">
    </label>
    <label for="search-engine">Engine Identity
      <input id="search-engine" type="search" placeholder="any">
    </label>
    <label for="search-doc">Source Identity
      <input id="search-doc" type="search" placeholder="any" spellcheck="false">
    </label>
    <label for="search-top-k">Top K
      <input id="search-top-k" type="number" min="1" max="100" value="10">
    </label>
    <button id="search-submit" type="submit">Search</button>
  </form>
  <p id="search-state" class="meta" role="status" aria-live="polite">
    Search reads the local index built by finepdfs index-search after a completed run.
  </p>
  <div id="search-results"></div>
</section>
<main>
  <table id="status-table">
    <caption id="row-count">Loading declared documents...</caption>
    <thead>
      <tr>
        <th scope="col">Status</th>
        <th scope="col">URL</th>
        <th scope="col">Language / script</th>
        <th scope="col">Routing</th>
        <th scope="col">Engine</th>
        <th scope="col">Pages</th>
        <th scope="col">Reason</th>
      </tr>
    </thead>
    <tbody id="status-body"></tbody>
  </table>
  <section id="document-detail" aria-label="Document detail" hidden></section>
</main>
<script>
const state = { documents: [], summary: null, observed: null, finishedAt: "", selected: null };

const byId = (id) => document.getElementById(id);

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function formatCount(value) {
  return typeof value === "number" ? value.toLocaleString() : "-";
}

function renderSummary() {
  const summary = state.summary || {};
  const corpus = summary.corpus || {};
  const routing = summary.routing || {};
  const extraction = summary.extraction || {};
  const published = summary.published || {};
  const exclusions = summary.exclusions || {};
  const engine = summary.engine || {};
  const kinds = Object.entries(exclusions.by_kind || {})
    .filter(([, count]) => count)
    .map(([kind, count]) => kind + " " + formatCount(count))
    .join(", ") || "none";
  byId("summary-corpus").textContent =
    formatCount(corpus.crawl_records) + " crawl records / " + formatCount(corpus.documents) + " documents";
  byId("summary-routing").textContent =
    "native " + formatCount(routing.native) + ", ocr " + formatCount(routing.ocr);
  byId("summary-extraction").textContent =
    "calls " + formatCount(extraction.calls) + ", reused " + formatCount(extraction.reused);
  byId("summary-published").textContent =
    formatCount(published.documents) + " documents / " + formatCount(published.page_records) + " page records";
  byId("summary-exclusions").textContent = formatCount(exclusions.total) + " (" + kinds + ")";
  const observed = state.observed || {};
  byId("summary-elapsed").textContent = observed.recorded
    ? Number(observed.elapsed_seconds).toFixed(1) + " s (observed)"
    : "not recorded";
  byId("summary-engine").textContent = engine.identity || engine.name || "-";
  byId("run-meta").textContent = observed.recorded
    ? "Observed run finished at " + (observed.finished_at || "unknown") + " (wall clock, not declared "
      + "target state). This view polls that observation and reloads after a completed update."
    : "No run timing is recorded beside these outputs, so elapsed is not recorded. The declared summary "
      + "carries no timestamps: run again and the same bytes are rewritten.";
}

function fillSelect(select, values) {
  const current = select.value;
  select.replaceChildren(new Option("All", ""));
  values.filter(Boolean).sort().forEach((value) => select.add(new Option(value, value)));
  select.value = values.includes(current) ? current : "";
}

function refreshFilterOptions() {
  const unique = (field) => [...new Set(state.documents.map((doc) => doc[field]))];
  fillSelect(byId("filter-language"), unique("language"));
  fillSelect(byId("filter-routing"), unique("routing_decision"));
  const kinds = ["published", ...Object.keys((state.summary && state.summary.exclusions
    && state.summary.exclusions.by_kind) || {})];
  fillSelect(byId("filter-kind"), [...new Set(kinds)]);
}

function filteredDocuments() {
  const query = byId("filter-url").value.trim().toLowerCase();
  const language = byId("filter-language").value;
  const routing = byId("filter-routing").value;
  const kind = byId("filter-kind").value;
  return state.documents.filter((doc) => {
    if (language && doc.language !== language) return false;
    if (routing && doc.routing_decision !== routing) return false;
    if (kind && doc.status !== kind) return false;
    if (!query) return true;
    const haystack = [doc.url, doc.text, doc.routing_reason, doc.exclusion_reason, doc.engine, doc.language]
      .filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(query);
  });
}

function renderTable() {
  const rows = filteredDocuments();
  const body = byId("status-body");
  body.replaceChildren(...rows.map(rowFor));
  byId("row-count").textContent =
    rows.length + " of " + state.documents.length + " declared documents shown";
  markSelectedRow();
}

function rowFor(doc) {
  const tr = document.createElement("tr");
  tr.tabIndex = 0;
  tr.dataset.key = doc.key;
  tr.setAttribute("aria-label", "Inspect " + (doc.url || doc.key));
  tr.addEventListener("click", () => showDetail(doc.key));
  tr.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      showDetail(doc.key);
    }
  });
  const pages = doc.status === "published"
    ? formatCount(doc.accepted_pages) + " / " + formatCount(doc.page_count) + " accepted"
    : "-";
  tr.append(
    textNode("td", doc.status, "status status-" + doc.status),
    textNode("td", doc.url || doc.key, "url"),
    textNode("td", doc.language || "-"),
    textNode("td", doc.routing_decision || "-"),
    textNode("td", doc.engine || "-"),
    textNode("td", pages),
    textNode("td", doc.exclusion_reason || doc.routing_reason || "-")
  );
  return tr;
}

function markSelectedRow() {
  document.querySelectorAll("#status-body tr").forEach((row) => {
    row.classList.toggle("selected", row.dataset.key === state.selected);
  });
}

function pdfLink(label, href) {
  const link = document.createElement("a");
  link.textContent = label;
  if (typeof href === "string" && href.startsWith("/pdf/")) {
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener";
  }
  return link;
}

function renderDetail(detail) {
  const panel = byId("document-detail");
  panel.replaceChildren();
  panel.append(textNode("h2", detail.url || detail.key));
  const list = document.createElement("dl");
  const rows = [
    ["Source URL", detail.url || "-"],
    ["Status", detail.status || "-"],
    ["Language / script", detail.language || "not routed"],
    ["Engine Identity", detail.engine || "none (excluded before extraction)"],
    ["Routing", detail.routing_decision
      ? detail.routing_decision + ": " + (detail.routing_reason || "")
      : "not routed"],
    ["Page counts", detail.status === "published"
      ? detail.accepted_pages + " accepted, " + detail.dropped_pages + " dropped, "
        + detail.page_count + " original"
      : "no accepted pages"],
    ["Exclusion reason", detail.exclusion_reason || "-"],
  ];
  rows.forEach(([label, value]) => {
    list.append(textNode("dt", label), textNode("dd", value));
  });
  panel.append(list);
  if (detail.pdf_url) {
    const paragraph = document.createElement("p");
    paragraph.append(pdfLink("Open retained PDF from page 1", detail.pdf_url + "#page=1"));
    panel.append(paragraph);
  }
  const pages = Array.isArray(detail.pages) ? detail.pages : [];
  panel.append(textNode("h3", "Accepted pages (" + pages.length + ")"));
  if (!pages.length) {
    panel.append(textNode("p", "No accepted page export: this document was excluded."));
    return;
  }
  pages.forEach((page) => {
    const article = document.createElement("article");
    article.className = "page";
    const header = document.createElement("div");
    header.append(textNode("h4", "Page " + page.page_number));
    if (page.pdf_url) {
      header.append(pdfLink("Open PDF page " + page.page_number, page.pdf_url));
    }
    const text = document.createElement("pre");
    text.textContent = page.text || "";
    article.append(header, text);
    panel.append(article);
  });
}

async function showDetail(key) {
  try {
    const response = await fetch("/api/documents/" + encodeURIComponent(key), { cache: "no-store" });
    if (!response.ok) throw new Error("detail request failed with HTTP " + response.status);
    const detail = await response.json();
    state.selected = key;
    renderDetail(detail);
    byId("document-detail").hidden = false;
    markSelectedRow();
    byId("document-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setError(error);
  }
}

async function loadAll() {
  const response = await fetch("/api/documents", { cache: "no-store" });
  if (!response.ok) throw new Error("summary request failed with HTTP " + response.status);
  const payload = await response.json();
  state.documents = Array.isArray(payload.documents) ? payload.documents : [];
  state.summary = payload.summary || {};
  state.observed = await loadObserved();
  state.finishedAt = (state.observed && state.observed.recorded && state.observed.finished_at) || "";
  renderSummary();
  refreshFilterOptions();
  renderTable();
  if (state.selected) await showDetail(state.selected);
}

function setError(error) {
  const status = byId("refresh-state");
  status.textContent = "Error: " + (error && error.message ? error.message : error);
  status.classList.add("error");
}

async function loadObserved() {
  const response = await fetch("/api/observed-run", { cache: "no-store" });
  if (!response.ok) return { recorded: false };
  return await response.json();
}

async function refresh(reason) {
  const status = byId("refresh-state");
  status.classList.remove("error");
  try {
    await loadAll();
    if (reason === "manual") status.textContent = "Reloaded at " + new Date().toLocaleTimeString();
  } catch (error) {
    setError(error);
  }
}

function startPolling() {
  setInterval(async () => {
    try {
      const response = await fetch("/api/observed-run", { cache: "no-store" });
      if (!response.ok) return;
      const observed = await response.json();
      const finishedAt = (observed.recorded && observed.finished_at) || "";
      if (finishedAt && finishedAt !== state.finishedAt) {
        await loadAll();
        byId("refresh-state").textContent =
          "An observed run finished at " + finishedAt + " (wall clock, not declared state); the view refreshed.";
      }
    } catch (error) {
      setError(error);
    }
  }, 5000);
}

function snippetFragment(snippet) {
  const fragment = document.createDocumentFragment();
  const parts = String(snippet || "").split(new RegExp("<mark>|</mark>"));
  let highlighted = false;
  parts.forEach((part) => {
    if (part === "<mark>") { highlighted = true; return; }
    if (part === "</mark>") { highlighted = false; return; }
    if (part) fragment.append(textNode(highlighted ? "mark" : "span", part));
  });
  return fragment;
}

function searchHit(hit, index) {
  const article = document.createElement("article");
  article.className = "hit";
  article.append(textNode("h3", (index + 1) + ". " + hit.doc_id + " p." + hit.page_number
    + " (score " + Number(hit.score).toFixed(3) + ")"));
  article.append(textNode("p", hit.source_url + " · chunk " + hit.chunk_id + " · "
    + (hit.engine || "-") + " · " + (hit.language || "-"), "meta"));
  const snippet = document.createElement("p");
  snippet.className = "snippet";
  snippet.append(snippetFragment(hit.snippet));
  const details = document.createElement("details");
  details.append(textNode("summary", "Matched chunk text"));
  const text = document.createElement("pre");
  text.textContent = hit.text || "";
  details.append(text);
  const inspect = document.createElement("button");
  inspect.type = "button";
  inspect.className = "link";
  inspect.textContent = "Inspect " + hit.doc_id + " p." + hit.page_number;
  inspect.addEventListener("click", () => showDetail(hit.doc_id));
  article.append(snippet, details, inspect);
  return article;
}

function renderSearch(payload) {
  const state = byId("search-state");
  const results = byId("search-results");
  results.replaceChildren();
  state.classList.remove("error");
  if (!payload.available) {
    state.textContent = payload.reason || "Search is unavailable.";
    state.classList.add("error");
    return;
  }
  const hits = Array.isArray(payload.hits) ? payload.hits : [];
  state.textContent = hits.length
    ? hits.length + ' hit(s) for "' + payload.query + '" from accepted, current chunks'
    : 'No accepted evidence matched "' + payload.query + '".';
  hits.forEach((hit, index) => results.append(searchHit(hit, index)));
}

async function searchEvidence(event) {
  event.preventDefault();
  const query = byId("search-query").value.trim();
  const state = byId("search-state");
  if (!query) {
    state.textContent = "Type words to search accepted evidence.";
    return;
  }
  const params = new URLSearchParams({ query, top_k: byId("search-top-k").value || "10" });
  [["language", "search-language"], ["engine", "search-engine"], ["doc_id", "search-doc"]]
    .forEach(([name, id]) => {
      const value = byId(id).value.trim();
      if (value) params.set(name, value);
    });
  state.classList.remove("error");
  state.textContent = "Searching accepted evidence...";
  try {
    const response = await fetch("/api/search?" + params.toString(), { cache: "no-store" });
    if (!response.ok) throw new Error("search failed with HTTP " + response.status);
    renderSearch(await response.json());
  } catch (error) {
    state.textContent = "Error: " + (error && error.message ? error.message : error);
    state.classList.add("error");
  }
}

["filter-url", "filter-language", "filter-routing", "filter-kind"].forEach((id) => {
  byId(id).addEventListener("input", renderTable);
});
byId("search-form").addEventListener("submit", searchEvidence);
byId("refresh").addEventListener("click", () => refresh("manual"));
refresh("initial");
startPolling();
</script>
</body>
</html>
"""
