"""Structural parsers.

Roadmap §4: "Use deterministic tools for deterministic work." Symbol and import
discovery never touches an LLM. Python uses the stdlib `ast` (exact). PHP, JS/TS,
Vue and C# use tolerant regex + brace matching, which handles real-world code
including strings and comments. The `Parser` interface is small so a tree-sitter
backend can be dropped in per language later without changing callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str            # class | interface | trait | enum | function | method | property | type | const
    signature: str
    line_start: int
    line_end: int
    parent: str | None = None


@dataclass(frozen=True)
class Import:
    raw: str             # module / path / FQCN as written
    kind: str            # module | relative | namespace | use


@dataclass
class ParseResult:
    symbols: list[Symbol]
    imports: list[Import]
    namespace: str | None = None


class Parser:
    languages: tuple[str, ...] = ()

    def parse(self, text: str) -> ParseResult:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def blank_out_noise(text: str) -> str:
    """Replace comment and string contents with spaces, preserving length and
    newlines so that offsets/line numbers stay valid for the original text."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif ch == "#" and not (nxt == "[" ):  # PHP/Python-style line comment (keep #[Attribute])
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif ch == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(_blank_keep_newlines(text[i:j]))
            i = j
        elif ch in ("'", '"', "`"):
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(ch + _blank_keep_newlines(text[i + 1:j - 1]) + (ch if j - 1 < n else ""))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _blank_keep_newlines(s: str) -> str:
    return "".join("\n" if c == "\n" else " " for c in s)


def match_brace(clean: str, open_pos: int) -> int:
    """Given index of '{' in noise-free text, return index of the matching '}' (or len-1)."""
    depth = 0
    for i in range(open_pos, len(clean)):
        c = clean[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(clean) - 1


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def first_line(text: str, start: int) -> str:
    end = text.find("\n", start)
    return text[start: end if end != -1 else len(text)].strip()


def signature_until_brace(text: str, start: int, limit: int = 300) -> str:
    """Declaration text from `start` up to the first '{' or ';' (collapsed whitespace)."""
    seg = text[start:start + limit]
    cut = len(seg)
    for stop in ("{", ";"):
        k = seg.find(stop)
        if k != -1:
            cut = min(cut, k)
    return re.sub(r"\s+", " ", seg[:cut]).strip()


def split_identifier(name: str) -> list[str]:
    """PaymentService -> [payment, service]; get_user_id -> [get, user, id]."""
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    return [p.lower() for p in re.split(r"[^A-Za-z0-9]+|\s+", parts) if p]
