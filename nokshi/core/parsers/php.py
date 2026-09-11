"""PHP parser (Laravel / Symfony friendly)."""

from __future__ import annotations

import re

from .base import Import, Parser, ParseResult, Symbol, blank_out_noise, line_of, match_brace, signature_until_brace

_NS = re.compile(r"^\s*namespace\s+([\w\\]+)\s*;", re.M)
_USE = re.compile(r"^\s*use\s+(?!function\s|const\s)([\w\\]+(?:\s*\{[^}]*\})?)(?:\s+as\s+\w+)?\s*;", re.M)
_TYPE = re.compile(
    r"(?<![\w$>\\])(?:(?:abstract|final|readonly)\s+)*(class|interface|trait|enum)\s+(\w+)", re.M)
_FUNC = re.compile(
    r"(?<![\w$>\\])(?:(?:public|protected|private|static|abstract|final)\s+)*function\s+&?(\w+)\s*\(", re.M)
_PROP = re.compile(
    r"^[ \t]*(?:(?:public|protected|private|static|readonly)\s+)+(?:[?\w\\|]+\s+)?\$(\w+)", re.M)


class PhpParser(Parser):
    languages = ("php",)

    def parse(self, text: str) -> ParseResult:
        clean = blank_out_noise(text)
        symbols: list[Symbol] = []
        imports: list[Import] = []

        ns_match = _NS.search(clean)
        namespace = ns_match.group(1) if ns_match else None

        for m in _USE.finditer(clean):
            raw = m.group(1)
            if "{" in raw:  # group use: use App\{Foo, Bar};
                prefix, inner = raw.split("{", 1)
                for part in inner.rstrip("}").split(","):
                    part = part.strip().split(" as ")[0].strip()
                    if part:
                        imports.append(Import(prefix.strip().rstrip("\\") + "\\" + part, "use"))
            else:
                imports.append(Import(raw.strip(), "use"))

        # types with their body ranges (so methods can be attributed to a parent)
        types: list[tuple[str, int, int]] = []
        for m in _TYPE.finditer(clean):
            kind, name = m.group(1), m.group(2)
            brace = clean.find("{", m.end())
            end = match_brace(clean, brace) if brace != -1 else m.end()
            symbols.append(Symbol(name, kind, signature_until_brace(text, m.start()),
                                  line_of(text, m.start()), line_of(text, end)))
            types.append((name, m.start(), end))

        def owner(pos: int) -> str | None:
            for name, s, e in types:
                if s < pos < e:
                    return name
            return None

        for m in _FUNC.finditer(clean):
            parent = owner(m.start())
            brace = clean.find("{", m.end())
            semi = clean.find(";", m.end())
            if brace != -1 and (semi == -1 or brace < semi):
                end = match_brace(clean, brace)
            else:  # abstract / interface method
                end = semi if semi != -1 else m.end()
            symbols.append(Symbol(m.group(1), "method" if parent else "function",
                                  signature_until_brace(text, m.start()),
                                  line_of(text, m.start()), line_of(text, end), parent))

        for m in _PROP.finditer(clean):
            parent = owner(m.start())
            if parent:
                symbols.append(Symbol(m.group(1), "property", signature_until_brace(text, m.start(), 160),
                                      line_of(text, m.start()), line_of(text, m.start()), parent))

        symbols.sort(key=lambda s: s.line_start)
        return ParseResult(symbols, imports, namespace)
