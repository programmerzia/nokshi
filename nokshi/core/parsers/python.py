"""Python parser — exact, via the stdlib `ast` module."""

from __future__ import annotations

import ast

from .base import Import, Parser, ParseResult, Symbol


def _sig(node: ast.AST, src_lines: list[str]) -> str:
    line = src_lines[node.lineno - 1].strip() if 0 < node.lineno <= len(src_lines) else ""
    return line.rstrip(":").strip()


class PythonParser(Parser):
    languages = ("python",)

    def parse(self, text: str) -> ParseResult:
        symbols: list[Symbol] = []
        imports: list[Import] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return ParseResult(symbols, imports)
        lines = text.splitlines()

        def visit(node: ast.AST, parent: str | None) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    symbols.append(Symbol(child.name, "class", _sig(child, lines), child.lineno,
                                          child.end_lineno or child.lineno, parent))
                    visit(child, child.name)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "method" if parent else "function"
                    symbols.append(Symbol(child.name, kind, _sig(child, lines), child.lineno,
                                          child.end_lineno or child.lineno, parent))
                    visit(child, parent or child.name)
                elif isinstance(child, ast.Import):
                    for alias in child.names:
                        imports.append(Import(alias.name, "module"))
                elif isinstance(child, ast.ImportFrom):
                    mod = "." * (child.level or 0) + (child.module or "")
                    imports.append(Import(mod, "relative" if child.level else "module"))
                    # `from pkg import submodule` may also point at a file
                    for alias in child.names:
                        if alias.name != "*":
                            imports.append(Import(f"{mod}.{alias.name}" if mod and not mod.endswith(".") else mod + alias.name, "relative" if child.level else "module"))
                elif isinstance(child, ast.Assign) and parent is None:
                    for t in child.targets:
                        if isinstance(t, ast.Name) and t.id.isupper():
                            symbols.append(Symbol(t.id, "const", _sig(child, lines)[:120], child.lineno,
                                                  child.end_lineno or child.lineno, None))
                else:
                    visit(child, parent)

        visit(tree, None)
        return ParseResult(symbols, imports)
