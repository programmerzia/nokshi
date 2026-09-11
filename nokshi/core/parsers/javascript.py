"""JavaScript / TypeScript / Vue parser."""

from __future__ import annotations

import re

from .base import (Import, ParseResult, Parser, Symbol, blank_out_noise, line_of, match_brace,
                   signature_until_brace)

_IMPORT = re.compile(r"""(?:^|[;\s])import\s+(?:[^'";]*?\s+from\s+)?['"]([^'"]+)['"]""", re.M)
_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
_DYN_IMPORT = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")
_EXPORT_FROM = re.compile(r"""export\s+(?:\*|\{[^}]*\})\s+from\s+['"]([^'"]+)['"]""")

_CLASS = re.compile(r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)", re.M)
_IFACE = re.compile(r"^[ \t]*(?:export\s+)?(?:declare\s+)?(interface|enum)\s+(\w+)", re.M)
_TYPE = re.compile(r"^[ \t]*(?:export\s+)?type\s+(\w+)\s*(?:<[^=]*>)?\s*=", re.M)
_FUNC = re.compile(r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*[<(]", re.M)
_ARROW = re.compile(
    r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*[^=]+?)?=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*(?::\s*[\w<>\[\]|, ]+)?\s*=>", re.M)
_METHOD = re.compile(
    r"^[ \t]+(?:(?:public|private|protected|static|readonly|async|override|get|set)\s+)*(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^{;]+)?\s*\{", re.M)
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "constructor", "else", "do", "try", "with"}


class JavaScriptParser(Parser):
    languages = ("javascript", "typescript", "vue")

    def parse(self, text: str) -> ParseResult:
        src = text
        if "<template" in text or "<script" in text:  # .vue single-file component
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", text, re.S)
            if scripts:
                # keep line numbers roughly: blank out everything except script bodies
                parts = []
                last = 0
                for m in re.finditer(r"<script[^>]*>(.*?)</script>", text, re.S):
                    parts.append("".join("\n" if c == "\n" else " " for c in text[last:m.start(1)]))
                    parts.append(m.group(1))
                    last = m.end(1)
                parts.append("".join("\n" if c == "\n" else " " for c in text[last:]))
                src = "".join(parts)

        clean = blank_out_noise(src)
        symbols: list[Symbol] = []
        imports: list[Import] = []

        for rx in (_IMPORT, _REQUIRE, _DYN_IMPORT, _EXPORT_FROM):
            for m in rx.finditer(src):  # use raw src: paths are inside string literals
                raw = m.group(1)
                kind = "relative" if raw.startswith((".", "/", "@/", "~/")) else "module"
                imports.append(Import(raw, kind))

        classes: list[tuple[str, int, int]] = []
        for m in _CLASS.finditer(clean):
            brace = clean.find("{", m.end())
            end = match_brace(clean, brace) if brace != -1 else m.end()
            symbols.append(Symbol(m.group(1), "class", signature_until_brace(src, m.start()),
                                  line_of(src, m.start()), line_of(src, end)))
            classes.append((m.group(1), m.start(), end))
        for m in _IFACE.finditer(clean):
            brace = clean.find("{", m.end())
            end = match_brace(clean, brace) if brace != -1 else m.end()
            symbols.append(Symbol(m.group(2), m.group(1), signature_until_brace(src, m.start()),
                                  line_of(src, m.start()), line_of(src, end)))
        for m in _TYPE.finditer(clean):
            symbols.append(Symbol(m.group(1), "type", signature_until_brace(src, m.start(), 160),
                                  line_of(src, m.start()), line_of(src, m.start())))

        def owner(pos: int) -> str | None:
            for name, s, e in classes:
                if s < pos < e:
                    return name
            return None

        for m in _FUNC.finditer(clean):
            brace = clean.find("{", m.end())
            end = match_brace(clean, brace) if brace != -1 else m.end()
            symbols.append(Symbol(m.group(1), "function", signature_until_brace(src, m.start()),
                                  line_of(src, m.start()), line_of(src, end), owner(m.start())))
        for m in _ARROW.finditer(clean):
            if owner(m.start()):
                continue
            brace = clean.find("{", m.end())
            nl = clean.find("\n", m.end())
            if brace != -1 and (nl == -1 or brace < nl + 2):
                end = match_brace(clean, brace)
            else:
                end = nl if nl != -1 else m.end()
            symbols.append(Symbol(m.group(1), "function", signature_until_brace(src, m.start()),
                                  line_of(src, m.start()), line_of(src, end)))
        for m in _METHOD.finditer(clean):
            parent = owner(m.start())
            name = m.group(1)
            if not parent or name in _JS_KEYWORDS:
                continue
            brace = clean.find("{", m.end() - 1)
            end = match_brace(clean, brace) if brace != -1 else m.end()
            symbols.append(Symbol(name, "method", signature_until_brace(src, m.start()),
                                  line_of(src, m.start()), line_of(src, end), parent))

        symbols.sort(key=lambda s: s.line_start)
        return ParseResult(symbols, imports)
