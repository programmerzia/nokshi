from __future__ import annotations

from pathlib import PurePosixPath

from .base import Import, Parser, ParseResult, Symbol, split_identifier
from .csharp import CSharpParser
from .javascript import JavaScriptParser
from .php import PhpParser
from .python import PythonParser

__all__ = ["Import", "ParseResult", "Parser", "Symbol", "split_identifier",
           "language_for", "parser_for", "STRUCTURAL_LANGUAGES"]

EXTENSIONS: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".php": "php", ".blade.php": "blade",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript",
    ".vue": "vue",
    ".cs": "csharp", ".cshtml": "razor", ".razor": "razor",
    ".sql": "sql", ".prisma": "prisma",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".xml": "xml",
    ".csproj": "xml", ".sln": "text", ".ini": "ini",
    ".md": "markdown", ".mdx": "markdown", ".txt": "text", ".rst": "text",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".rb": "ruby",
    ".dockerfile": "docker", ".graphql": "graphql", ".gql": "graphql",
}
SPECIAL_NAMES = {"Dockerfile": "docker", "Makefile": "make", "composer.json": "json",
                 "package.json": "json", ".env.example": "env"}  # only the example, never real .env files

_PARSERS: list[Parser] = [PythonParser(), PhpParser(), JavaScriptParser(), CSharpParser()]
_BY_LANG: dict[str, Parser] = {lang: p for p in _PARSERS for lang in p.languages}
STRUCTURAL_LANGUAGES = frozenset(_BY_LANG)


def language_for(path: str) -> str | None:
    name = PurePosixPath(path).name
    if name in SPECIAL_NAMES:
        return SPECIAL_NAMES[name]
    lower = name.lower()
    if lower.endswith(".blade.php"):
        return "blade"
    if lower.endswith(".d.ts"):
        return "typescript"
    suffix = PurePosixPath(lower).suffix
    return EXTENSIONS.get(suffix)


def parser_for(language: str) -> Parser | None:
    return _BY_LANG.get(language)
