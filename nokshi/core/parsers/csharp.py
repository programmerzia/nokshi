"""C# parser (ASP.NET Core / EF Core friendly)."""

from __future__ import annotations

import re

from .base import (Import, ParseResult, Parser, Symbol, blank_out_noise, line_of, match_brace,
                   signature_until_brace)

_USING = re.compile(r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?!var\s)([\w.]+)\s*;", re.M)
_NS = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
_TYPE = re.compile(
    r"^[ \t]*(?:\[[^\]]*\]\s*)*(?:(?:public|internal|private|protected|static|abstract|sealed|partial|readonly|file)\s+)*"
    r"(class|interface|record\s+struct|record\s+class|record|struct|enum)\s+(\w+)", re.M)
_METHOD = re.compile(
    r"^[ \t]+(?:\[[^\]]*\]\s*)*(?:(?:public|internal|private|protected|static|abstract|virtual|override|async|sealed|extern|partial|new)\s+)+"
    r"[\w<>\[\],.? ]+?\s+(\w+)\s*(?:<[^>]*>)?\s*\(", re.M)
_PROP = re.compile(
    r"^[ \t]+(?:\[[^\]]*\]\s*)*(?:(?:public|internal|private|protected|static|virtual|override|required|new)\s+)+"
    r"[\w<>\[\],.? ]+?\s+(\w+)\s*\{\s*(?:get|set|init)", re.M)
_CTOR = re.compile(r"^[ \t]+(?:(?:public|internal|private|protected|static)\s+)+(\w+)\s*\([^)]*\)\s*(?::\s*(?:this|base)\s*\([^)]*\))?\s*\{", re.M)
_CS_KEYWORDS = {"if", "for", "foreach", "while", "switch", "catch", "return", "using", "lock", "else", "new"}


class CSharpParser(Parser):
    languages = ("csharp",)

    def parse(self, text: str) -> ParseResult:
        clean = blank_out_noise(text)
        symbols: list[Symbol] = []
        imports = [Import(m.group(1), "namespace") for m in _USING.finditer(clean)]
        ns = _NS.search(clean)
        namespace = ns.group(1) if ns else None

        types: list[tuple[str, int, int]] = []
        for m in _TYPE.finditer(clean):
            kind = m.group(1).split()[0]
            brace = clean.find("{", m.end())
            semi = clean.find(";", m.end())
            if brace != -1 and (semi == -1 or brace < semi):
                end = match_brace(clean, brace)
            else:
                end = semi if semi != -1 else m.end()
            symbols.append(Symbol(m.group(2), kind, signature_until_brace(text, m.start()),
                                  line_of(text, m.start()), line_of(text, end)))
            types.append((m.group(2), m.start(), end))

        def owner(pos: int) -> str | None:
            best = None
            for name, s, e in types:  # innermost wins
                if s < pos < e and (best is None or s > best[1]):
                    best = (name, s)
            return best[0] if best else None

        for m in _METHOD.finditer(clean):
            parent = owner(m.start())
            name = m.group(1)
            if not parent or name in _CS_KEYWORDS:
                continue
            if any(name == t[0] and abs(m.start() - t[1]) < 5 for t in types):
                continue
            brace = clean.find("{", m.end())
            semi = clean.find(";", m.end())
            arrow = clean.find("=>", m.end())
            if arrow != -1 and (brace == -1 or arrow < brace) and (semi != -1 and arrow < semi):
                end = semi
            elif brace != -1 and (semi == -1 or brace < semi):
                end = match_brace(clean, brace)
            else:
                end = semi if semi != -1 else m.end()
            kind = "constructor" if name == parent else "method"
            symbols.append(Symbol(name, kind, signature_until_brace(text, m.start()),
                                  line_of(text, m.start()), line_of(text, end), parent))

        for m in _CTOR.finditer(clean):
            parent = owner(m.start())
            if parent and m.group(1) == parent:
                brace = clean.find("{", m.end() - 1)
                end = match_brace(clean, brace) if brace != -1 else m.end()
                symbols.append(Symbol(parent, "constructor", signature_until_brace(text, m.start()),
                                      line_of(text, m.start()), line_of(text, end), parent))

        for m in _PROP.finditer(clean):
            parent = owner(m.start())
            if parent:
                symbols.append(Symbol(m.group(1), "property", signature_until_brace(text, m.start(), 160),
                                      line_of(text, m.start()), line_of(text, m.start()), parent))

        symbols.sort(key=lambda s: s.line_start)
        return ParseResult(symbols, imports, namespace)
