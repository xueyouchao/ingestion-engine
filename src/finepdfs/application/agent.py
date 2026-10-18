"""The agent tools and a small, bounded, visible loop over accepted evidence.

Two tools, both reading the same record contract a citation names:

* ``search_pdfs(query, filters, top_k)`` returns ranked current chunks from the shared lexical index;
* ``read_pdf_page(doc_id, page_number)`` returns the exact accepted Page Text of one original page, or
  an explicit not-found/unsupported answer for an absent, excluded, duplicate, unpublished, retracted
  or stale page.

The loop is deliberately not a framework: it speaks the OpenAI-compatible chat-completions message
shape, caps tool executions (six by default), and reports every call and result. A chat model is
injected, so a fake can drive the loop in tests and an HTTP transport can drive it in the CLI without
changing this module.

The citation contract is enforced, not requested. A final answer may cite only pages whose exact text
``read_pdf_page`` returned: a citation for any other page is stripped from the answer, a search snippet
does not make a page citable, and a page number written in prose ("page 37") is not a citation at all.
When no verified citation is left the loop makes one bounded repair turn asking the model to read the
supporting page, and if that does not produce a verified citation the answer becomes an explicit
insufficient-evidence reply. A contradictory insufficient-evidence tail after a supported answer is
dropped. The tool-call budget and the visible trace are unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from finepdfs.application.page_reads import read_pdf_page
from finepdfs.application.search import search_pdfs
from finepdfs.domain.chunks import DeclaredChunkSet
from finepdfs.domain.ports import DeclaredPageReader, SearchIndex
from finepdfs.domain.search import SearchFilters
from finepdfs.domain.source_identity import SOURCE_IDENTITY_DIGITS

TOOL_SEARCH = "search_pdfs"
TOOL_READ = "read_pdf_page"
MAX_TOOL_CALLS = 6
DEFAULT_AGENT_TOP_K = 5
MAX_AGENT_TOP_K = 10

SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_SEARCH,
        "description": (
            "Search the currently accepted, published evidence. Returns ranked chunks with doc_id, "
            "chunk_id, page_number, source_url, digest, snippet, text and a lexical score. Use it to "
            "find pages, then read_pdf_page for the exact accepted page text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to look for in accepted pages."},
                "filters": {
                    "type": "object",
                    "properties": {
                        "language": {"type": "string"},
                        "engine": {"type": "string"},
                        "doc_id": {"type": "string", "description": "16-character source identity."},
                    },
                    "additionalProperties": False,
                },
                "top_k": {
                    "type": "integer",
                    "description": f"How many hits to return, 1 to {MAX_AGENT_TOP_K}.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_READ,
        "description": (
            "Read the exact accepted text of one original page of a published document. Returns "
            "status 'accepted' with the page text, or an explicit 'not-found'/'unsupported' status "
            "with a reason. It never reconstructs text from Markdown."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "16-character source identity."},
                "page_number": {"type": "integer", "description": "Original 1-based PDF page number."},
            },
            "required": ["doc_id", "page_number"],
            "additionalProperties": False,
        },
    },
}

TOOL_SCHEMAS = (SEARCH_SCHEMA, READ_SCHEMA)

_CITATION = re.compile(
    rf'["\']?({SOURCE_IDENTITY_DIGITS})["\']?\s*(?:,\s*)?(?:p\.|page\s+)\s*(\d+)',
    re.IGNORECASE,
)
# The prompt names the citation format as [DOC p.PAGE]; a model that copies the placeholder back has
# written no doc_id, so the tag names no document. It is stripped as an unverified marker, never
# resolved to whichever page happened to be read: that would invent a citation the model never wrote.
_PLACEHOLDER_CITATION = re.compile(r"<doc_id>\s*(?:p\.|page\s+)\s*\d+\s*</doc_id>", re.IGNORECASE)
# A model that answers and then contradicts itself ends with this marker on its own line; it is a tail
# to drop, not a second answer.
_INSUFFICIENT_LINE = re.compile(r"^[\s>*_#-]*INSUFFICIENT EVIDENCE\b.*$", re.IGNORECASE | re.MULTILINE)


class ChatModel(Protocol):
    """An OpenAI-compatible chat model: messages in, one assistant message dict out."""

    def complete(self, messages: Sequence[dict], tools: Sequence[dict] = ()) -> dict: ...


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One executed tool call and the result the model was given."""

    name: str
    arguments: dict
    result: dict

    def as_json(self) -> dict:
        return {"tool": self.name, "arguments": self.arguments, "result": self.result}


@dataclass(frozen=True, slots=True)
class Citation:
    """A document/page an answer actually cites, from evidence a tool returned.

    ``verified`` is true only when ``read_pdf_page`` returned that exact page as accepted; a page seen
    only in a search snippet locates evidence but does not prove the cited page says what was claimed.
    """

    doc_id: str
    page_number: int
    source_url: str
    chunk_id: str | None = None
    verified: bool = False

    @property
    def citation(self) -> str:
        return f"{self.doc_id} p.{self.page_number}"

    def as_json(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "page_number": self.page_number,
            "source_url": self.source_url,
            "chunk_id": self.chunk_id,
            "citation": self.citation,
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class AgentAnswer:
    """What one bounded question produced: the answer, its citations, and every tool call."""

    question: str
    answer: str
    citations: tuple[Citation, ...]
    tool_calls: tuple[ToolCall, ...]
    insufficient_evidence: bool = False
    budget_exhausted: bool = False

    def as_json(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "insufficient_evidence": self.insufficient_evidence,
            "budget_exhausted": self.budget_exhausted,
            "citations": [citation.as_json() for citation in self.citations],
            "tool_calls": [call.as_json() for call in self.tool_calls],
        }


class AcceptedEvidenceTools:
    """The two agent tools, wired to an index and to the declared page exports.

    Both take the same evidence the browser reads. The tools are the only place a query or a page
    request is validated, so the loop cannot be talked into an unbounded read.
    """

    def __init__(
        self,
        index: SearchIndex,
        pages: DeclaredPageReader,
        declared: Callable[[], DeclaredChunkSet],
        *,
        default_top_k: int = DEFAULT_AGENT_TOP_K,
        max_top_k: int = MAX_AGENT_TOP_K,
    ) -> None:
        if default_top_k < 1:
            raise ValueError(f"default_top_k must be at least 1, not {default_top_k}")
        if max_top_k < default_top_k:
            raise ValueError(f"max_top_k {max_top_k} must be at least default_top_k {default_top_k}")
        self._index = index
        self._pages = pages
        self._declared = declared
        self._default_top_k = default_top_k
        self._max_top_k = max_top_k

    @property
    def schemas(self) -> tuple[dict, ...]:
        return TOOL_SCHEMAS

    def call(self, name: str, arguments: dict) -> dict:
        """Execute one tool call; an invalid request is a named error result, not an exception."""
        if name == TOOL_SEARCH:
            return self._search(arguments)
        if name == TOOL_READ:
            return self._read(arguments)
        return {"error": f"unknown tool {name!r}; available tools are {TOOL_SEARCH} and {TOOL_READ}"}

    def _search(self, arguments: dict) -> dict:
        try:
            text = arguments.get("query")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"{TOOL_SEARCH} needs a non-empty query")
            filters = _filters(arguments.get("filters"))
            top_k = self._top_k(arguments.get("top_k"))
            results = search_pdfs(self._index, self._declared, text, filters, top_k)
        except ValueError as error:
            return {"error": str(error)}
        payload = results.as_json()
        payload["tool"] = TOOL_SEARCH
        payload["top_k"] = results.query.top_k
        return payload

    def _read(self, arguments: dict) -> dict:
        try:
            page_number = _page_number(arguments.get("page_number"))
            result = read_pdf_page(self._pages, arguments.get("doc_id"), page_number)
        except (ValueError, TypeError) as error:
            return {"error": str(error)}
        payload = result.as_json()
        payload["tool"] = TOOL_READ
        return payload

    def _top_k(self, value: object) -> int:
        if value is None:
            return self._default_top_k
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"top_k must be an integer, not {value!r}")
        if value < 1:
            raise ValueError(f"top_k must be at least 1, not {value}")
        return min(value, self._max_top_k)


def run_agent(
    question: str,
    model: ChatModel,
    tools: AcceptedEvidenceTools,
    *,
    max_tool_calls: int = MAX_TOOL_CALLS,
    on_event: Callable[[dict], None] | None = None,
) -> AgentAnswer:
    """Answer one question with at most ``max_tool_calls`` visible tool executions."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("an agent question needs non-empty text")
    if max_tool_calls < 1:
        raise ValueError(f"max_tool_calls must be at least 1, not {max_tool_calls}")

    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(max_tool_calls)},
        {"role": "user", "content": question.strip()},
    ]
    trace: list[ToolCall] = []
    used = 0
    budget_exhausted = False
    repaired = False
    answer = ""

    while True:
        message = _assistant_message(model.complete(messages, tools=tools.schemas))
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            answer = _content_text(message.get("content")).strip()
            if not repaired and used < max_tool_calls and _needs_repair(answer, trace):
                repaired = True
                messages.append({"role": "user", "content": _REPAIR_PROMPT})
                continue
            break

        skipped = 0
        for call in calls:
            call_id, name, arguments = _tool_call(call)
            if used >= max_tool_calls:
                skipped += 1
                # The model asked for a call it cannot have; the round trip still needs an answer for
                # it, or a strict endpoint would reject the next request.
                messages.append(_tool_message(call_id, name, {"error": "tool budget exhausted; not executed"}))
                continue
            used += 1
            result = tools.call(name, arguments)
            outcome = ToolCall(name=name, arguments=arguments, result=result)
            trace.append(outcome)
            messages.append(_tool_message(call_id, name, result))
            if on_event is not None:
                on_event(
                    {
                        "event": "tool_call",
                        "index": used,
                        "tool": name,
                        "arguments": arguments,
                        "result": result,
                    }
                )
        if used >= max_tool_calls or skipped:
            budget_exhausted = True
            break

    if budget_exhausted:
        messages.append({"role": "user", "content": _BUDGET_PROMPT})
        forced = _assistant_message(model.complete(messages, tools=()))
        answer = _content_text(forced.get("content")).strip()
        if not answer:
            answer = "INSUFFICIENT EVIDENCE: the agent returned no answer within the tool budget."

    answer, insufficient, citations = _finalise(answer, trace)
    return AgentAnswer(
        question=question.strip(),
        answer=answer,
        citations=citations,
        tool_calls=tuple(trace),
        insufficient_evidence=insufficient,
        budget_exhausted=budget_exhausted,
    )


def _system_prompt(max_tool_calls: int) -> str:
    return (
        "You answer questions about a small corpus of PDFs using only the provided tools.\n"
        f"1. Call {TOOL_SEARCH} to find candidate accepted chunks. A hit names doc_id, chunk_id, "
        "page_number, source_url, digest, snippet, text and score. Start with top_k 5 unless the "
        "question is very narrow, so the answer is likely inside the returned chunks.\n"
        f"2. Call {TOOL_READ} with a doc_id and page_number to read the exact accepted text of one "
        "original page. If a snippet does not contain the answer, read the page it points at before "
        "answering.\n"
        f"You have at most {max_tool_calls} tool calls in total. Every call and every result is visible.\n"
        "Cite every factual claim in plain text as [DOC p.PAGE], where DOC is the exact 16-character "
        "doc_id a tool returned and PAGE is the original page number, for example "
        "[9bf665589f7333a8 p.8]. That bracketed pair is the only citation format: never write the "
        "placeholder text 'doc_id', never cite a page without its doc_id, and never wrap a citation in "
        "another tag. Cite only a page that read_pdf_page returned as accepted: a search snippet "
        "locates a page but does not verify what that page says. A page number written in prose, such "
        "as 'page 37', is not a citation and will not be accepted.\n"
        "Never invent a document, page, quotation or URL.\n"
        "If the tools do not contain the answer, reply exactly 'INSUFFICIENT EVIDENCE: <what is "
        "missing>' and cite nothing."
    )


_BUDGET_PROMPT = (
    "The tool budget is exhausted. Answer now from the evidence already returned, citing each claim "
    "as [DOC p.PAGE] in plain text, where DOC is the exact doc_id a tool returned and PAGE the page "
    "number, or reply exactly 'INSUFFICIENT EVIDENCE: <what is missing>'. Do not request another tool, "
    "never cite a page without its doc_id, and do not wrap citations in other tags. Cite only a page "
    "read_pdf_page returned; if no returned page supports the claim, reply INSUFFICIENT EVIDENCE."
)

_REPAIR_PROMPT = (
    "Your answer is not backed by a page you read, so it cannot stand. Call read_pdf_page with the "
    "doc_id and page_number of the page that supports the claim, then answer again citing it as "
    "[DOC p.PAGE] with that exact doc_id and page number. A page number written in prose or cited "
    "without its doc_id is not a citation. If no page already returned by the tools supports the "
    "claim, reply exactly 'INSUFFICIENT EVIDENCE: <what is missing>'."
)

_INSUFFICIENT_FALLBACK = (
    "INSUFFICIENT EVIDENCE: no page that read_pdf_page returned supports this answer. "
    "A search snippet, a prose page number or an unread page is not a verified citation."
)


def _filters(value: object) -> SearchFilters:
    if value is None:
        return SearchFilters()
    if not isinstance(value, dict):
        raise ValueError(f"filters must be an object, not {value!r}")
    unknown = set(value) - {"language", "engine", "doc_id"}
    if unknown:
        raise ValueError(f"unknown filter(s): {', '.join(sorted(unknown))}")
    for name, item in value.items():
        if item is not None and not isinstance(item, str):
            raise ValueError(f"filter {name} must be a string, not {item!r}")
    return SearchFilters(
        language=value.get("language"),
        engine=value.get("engine"),
        doc_id=value.get("doc_id"),
    )


def _page_number(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError(f"page_number must be a 1-based integer, not {value!r}")
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    if isinstance(value, int):
        return value
    raise ValueError(f"page_number must be a 1-based integer, not {value!r}")


def _assistant_message(message: object) -> dict:
    if not isinstance(message, dict):
        raise ValueError("the chat model returned a non-object message")
    normalised: dict = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        normalised["tool_calls"] = message["tool_calls"]
    return normalised


def _tool_call(call: object) -> tuple[str, str, dict]:
    """Read one OpenAI tool call: its id, its name and its JSON arguments."""
    if not isinstance(call, dict):
        return "", "", {"error": "malformed tool call"}
    call_id = call.get("id")
    function = call.get("function")
    if not isinstance(function, dict):
        return str(call_id or ""), "", {"error": "malformed tool call: no function"}
    name = function.get("name")
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return str(call_id or ""), str(name or ""), arguments


def _tool_message(call_id: str, name: str, result: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps(result, ensure_ascii=False),
    }


def _content_text(content: object) -> str:
    return content if isinstance(content, str) else ""


def _claims_insufficient(answer: str) -> bool:
    return answer.lstrip("*_# ").upper().startswith("INSUFFICIENT EVIDENCE")


def _needs_repair(answer: str, trace: Sequence[ToolCall]) -> bool:
    """Whether the answer cites something the tools did not verify and a repair turn could fix.

    Two shapes need it: a claim with no verified citation at all (prose "page 37", a snippet-only
    citation), and a verified answer that also carries a citation for a page nobody read.
    """
    if _claims_insufficient(answer):
        return False
    text, citations = _only_verified_citations(answer, _verified_evidence(trace))
    return not citations or text != answer


def _finalise(answer: str, trace: Sequence[ToolCall]) -> tuple[str, bool, tuple[Citation, ...]]:
    """Apply the citation contract: verified pages only, no contradictory tail, else insufficient."""
    answer = answer.strip()
    if _claims_insufficient(answer):
        return _without_citations(answer), True, ()
    answer = _strip_insufficient_tail(answer)
    if not answer:
        return _INSUFFICIENT_FALLBACK, True, ()
    verified = _verified_evidence(trace)
    stripped, citations = _only_verified_citations(answer, verified)
    if not citations:
        return _INSUFFICIENT_FALLBACK, True, ()
    return stripped, False, citations


def _verified_evidence(trace: Sequence[ToolCall]) -> dict[tuple[str, int], Citation]:
    """The pages ``read_pdf_page`` returned as accepted; the only pages an answer may cite."""
    chunk_ids: dict[tuple[str, int], str] = {}
    for outcome in trace:
        for hit in outcome.result.get("hits") or []:
            if isinstance(hit, dict) and hit.get("chunk_id"):
                chunk_ids.setdefault(
                    (str(hit.get("doc_id")), int(hit.get("page_number", 0))), str(hit["chunk_id"])
                )
    verified: dict[tuple[str, int], Citation] = {}
    for outcome in trace:
        result = outcome.result
        if result.get("status") != "accepted":
            continue
        key = (str(result.get("doc_id")), int(result.get("page_number", 0)))
        verified[key] = Citation(
            doc_id=key[0],
            page_number=key[1],
            source_url=str(result.get("source_url", "")),
            chunk_id=chunk_ids.get(key),
            verified=True,
        )
    return verified


def _only_verified_citations(
    answer: str, verified: dict[tuple[str, int], Citation]
) -> tuple[str, tuple[Citation, ...]]:
    """Remove every citation whose page was not read, and return the ones that were.

    One written format is a citation: a doc_id a tool returned, followed by its page number. A
    placeholder tag that carries a page number and no doc_id names no document, so it is removed like
    any other unverified marker; prose such as "page 37" is left in the text but is not a citation, so
    an answer resting on it has none.
    """
    found: list[Citation] = []
    seen: set[tuple[str, int]] = set()
    spans: list[tuple[int, int]] = []
    for match in _CITATION.finditer(answer):
        key = (match.group(1).lower(), int(match.group(2)))
        if key in verified:
            if key not in seen:
                seen.add(key)
                found.append(verified[key])
            continue
        spans.append(_span_with_brackets(answer, match.start(), match.end()))
    spans.extend(match.span() for match in _PLACEHOLDER_CITATION.finditer(answer))
    if not spans:
        return answer, tuple(found)
    for start, end in sorted(spans, reverse=True):
        answer = answer[:start] + answer[end:]
    answer = re.sub(r"[ \t]{2,}", " ", answer)
    answer = re.sub(r"\s+([.,;:!?])", r"\1", answer)
    answer = re.sub(r"\[\s*\]|\(\s*\)", "", answer)
    return answer.strip(), tuple(found)


def _without_citations(answer: str) -> str:
    """An insufficient-evidence reply cites nothing, so every marker is removed from it."""
    answer = _CITATION.sub("", answer)
    answer = _PLACEHOLDER_CITATION.sub("", answer)
    answer = re.sub(r"\[\s*\]|\(\s*\)", "", answer)
    answer = re.sub(r"[ \t]{2,}", " ", answer)
    return re.sub(r"\s+([.,;:!?])", r"\1", answer).strip()


def _span_with_brackets(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a citation match to the brackets or parentheses that only wrap it."""
    if start > 0 and end < len(text) and (text[start - 1], text[end]) in {("[", "]"), ("(", ")")}:
        return start - 1, end + 1
    return start, end


def _strip_insufficient_tail(answer: str) -> str:
    """Drop a contradictory ``INSUFFICIENT EVIDENCE`` segment after an already supported answer."""
    marker = _INSUFFICIENT_LINE.search(answer)
    if marker is None or marker.start() == 0:
        return answer
    return answer[: marker.start()].rstrip()
