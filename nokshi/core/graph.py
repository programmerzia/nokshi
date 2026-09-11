"""Dependency graph (roadmap §4: "The repository should be represented as a graph").

Two kinds of edges:
- import:    resolved from import/use/using statements (PSR-4, tsconfig paths,
             relative paths, Python packages, C# namespaces).
- reference: file A mentions a type/function name that is uniquely defined in
             file B. Cheap, language-agnostic, and what makes the graph useful
             for frameworks that resolve things at runtime (Laravel container,
             DI in .NET).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import PurePosixPath

from nokshi.core.config import Config
from nokshi.core.db import Database

_IDENT = re.compile(r"\b[A-Z][A-Za-z0-9_]{3,}\b")  # PascalCase-ish type names
_COMMON = {"String", "Array", "Object", "Number", "Boolean", "Promise", "Record", "Partial", "Task",
           "List", "Dictionary", "Exception", "Error", "Date", "DateTime", "Guid", "Model", "Request",
           "Response", "Controller", "Service", "Repository", "Collection", "Builder", "Command", "Console",
           "Event", "Listener", "Provider", "Factory", "Middleware", "Route", "Schema", "Blueprint",
           "None", "True", "False", "Optional", "Union", "Any", "Dict", "Tuple", "Callable", "Iterable",
           "Enum", "Interface", "Type", "Props", "State", "Config", "Options", "Result", "Status", "Test",
           "Base", "Abstract", "Main", "Program", "Startup", "Index", "Home", "Item", "Items", "Value",
           "Component", "Module", "Client", "Server", "Handler", "Context", "Manager", "Helper", "Utils"}


def _strip_json_comments(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r",\s*([}\]])", r"\1", text)


def _load_json(cfg: Config, name: str) -> dict:
    p = cfg.root / name
    if not p.is_file():
        return {}
    try:
        return json.loads(_strip_json_comments(p.read_text(encoding="utf-8", errors="replace")))
    except json.JSONDecodeError:
        return {}


class Resolver:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.paths: dict[str, int] = {r["path"]: r["id"] for r in db.all_files()}
        self.lang: dict[int, str] = {r["id"]: r["language"] for r in db.all_files()}
        self.ns_files: dict[str, list[int]] = defaultdict(list)
        for r in db.all_files():
            if r["namespace"]:
                self.ns_files[r["namespace"]].append(r["id"])
        # PSR-4 map from composer.json
        composer = _load_json(cfg, "composer.json")
        self.psr4: list[tuple[str, str]] = []
        for section in ("autoload", "autoload-dev"):
            for prefix, dirs in composer.get(section, {}).get("psr-4", {}).items():
                for d in ([dirs] if isinstance(dirs, str) else dirs):
                    self.psr4.append((prefix.rstrip("\\"), d.strip("/")))
        self.psr4.sort(key=lambda x: -len(x[0]))
        # tsconfig / jsconfig path aliases
        ts = _load_json(cfg, "tsconfig.json") or _load_json(cfg, "jsconfig.json")
        opts = ts.get("compilerOptions", {})
        self.base_url = opts.get("baseUrl", ".").strip("./") or ""
        self.aliases: list[tuple[str, str]] = []
        for alias, targets in opts.get("paths", {}).items():
            for t in targets:
                self.aliases.append((alias.rstrip("*"), t.rstrip("*").strip("./")))
        if not self.aliases:  # common convention even without tsconfig
            for guess in ("src", "resources/js", "app", "."):
                if any(p.startswith(guess + "/") for p in self.paths) or guess == ".":
                    self.aliases.append(("@/", guess if guess != "." else ""))
                    break
        # Python packages: which dirs are packages
        self.py_dirs = {str(PurePosixPath(p).parent) for p in self.paths if p.endswith(".py")}
        # class name -> file ids (for PHP fallback & reference edges)
        self.class_files: dict[str, set[int]] = defaultdict(set)

    # -- helpers ----------------------------------------------------------
    def _probe(self, base: str) -> int | None:
        base = base.strip("/")
        if base in self.paths:
            return self.paths[base]
        for ext in (".ts", ".tsx", ".js", ".jsx", ".vue", ".mjs", ".cjs", ".d.ts", ".json", ".py", ".php", ".cs"):
            if base + ext in self.paths:
                return self.paths[base + ext]
        for idx in ("index.ts", "index.tsx", "index.js", "index.jsx", "index.vue", "__init__.py"):
            if f"{base}/{idx}" in self.paths:
                return self.paths[f"{base}/{idx}"]
        return None

    def resolve(self, src_path: str, raw: str, kind: str, language: str) -> list[int]:
        if language == "php":
            return self._php(raw)
        if language in ("javascript", "typescript", "vue"):
            return self._js(src_path, raw)
        if language == "python":
            return self._py(src_path, raw, kind)
        if language == "csharp":
            return self._cs(raw)
        return []

    def _php(self, fqcn: str) -> list[int]:
        fqcn = fqcn.strip("\\")
        for prefix, directory in self.psr4:
            if fqcn == prefix or fqcn.startswith(prefix + "\\"):
                rest = fqcn[len(prefix):].strip("\\").replace("\\", "/")
                cand = f"{directory}/{rest}.php" if directory else f"{rest}.php"
                if cand in self.paths:
                    return [self.paths[cand]]
        short = fqcn.rsplit("\\", 1)[-1]
        files = self.class_files.get(short, set())
        return sorted(files) if len(files) == 1 else []

    def _js(self, src_path: str, raw: str) -> list[int]:
        target = None
        if raw.startswith("."):
            target = str(PurePosixPath(src_path).parent.joinpath(raw))
            target = str(PurePosixPath(_normalize(target)))
        else:
            for alias, base in self.aliases:
                if raw.startswith(alias):
                    target = (base + "/" if base else "") + raw[len(alias):]
                    break
            if target is None and self.base_url and not raw.startswith("@") and "/" in raw:
                target = f"{self.base_url}/{raw}"
        if target is None:
            return []
        hit = self._probe(target)
        return [hit] if hit else []

    def _py(self, src_path: str, raw: str, kind: str) -> list[int]:
        if kind == "relative":
            level = len(raw) - len(raw.lstrip("."))
            base = PurePosixPath(src_path).parent
            for _ in range(level - 1):
                base = base.parent
            rest = raw.lstrip(".").replace(".", "/")
            target = str(base / rest) if rest else str(base)
            hit = self._probe(target)
            return [hit] if hit else []
        rel = raw.replace(".", "/")
        for prefix in ("", "src/", "app/", "lib/"):
            hit = self._probe(prefix + rel)
            if hit:
                return [hit]
        return []

    def _cs(self, namespace: str) -> list[int]:
        return list(self.ns_files.get(namespace, []))


def _normalize(p: str) -> str:
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def build_dependency_graph(cfg: Config, db: Database) -> int:
    resolver = Resolver(cfg, db)
    files = db.all_files()
    # unique type/function names -> defining file
    for s in db.all_symbols():
        if s["kind"] in ("class", "interface", "trait", "enum", "record", "struct", "type", "function") \
                and len(s["name"]) >= 4 and s["name"] not in _COMMON:
            resolver.class_files[s["name"]].add(s["file_id"])
    unique = {name: next(iter(ids)) for name, ids in resolver.class_files.items() if len(ids) == 1}

    edges: dict[tuple[int, int, str], float] = {}
    for f in files:
        fid, lang, path = f["id"], f["language"], f["path"]
        for imp in db.imports_for(fid):
            for dst in resolver.resolve(path, imp["raw"], imp["kind"], lang):
                if dst != fid:
                    weight = 0.6 if lang == "csharp" else 1.0  # namespace edges are coarser
                    edges[(fid, dst, "import")] = max(edges.get((fid, dst, "import"), 0), weight)
        if lang in ("php", "javascript", "typescript", "vue", "csharp", "python", "blade", "razor"):
            try:
                text = (cfg.root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) > cfg.max_file_kb * 1024:
                continue
            own = {s["name"] for s in db.symbols_for(fid)}
            for name in set(_IDENT.findall(text)):
                dst = unique.get(name)
                if dst is not None and dst != fid and name not in own and (fid, dst, "import") not in edges:
                    edges[(fid, dst, "reference")] = 0.5
    db.replace_deps((s, d, k, w) for (s, d, k), w in edges.items())
    return len(edges)


class Graph:
    """In-memory adjacency built from the deps table."""

    def __init__(self, db: Database):
        self.out: dict[int, dict[int, float]] = defaultdict(dict)
        self.inc: dict[int, dict[int, float]] = defaultdict(dict)
        for e in db.all_deps():
            s, d, w = e["src_id"], e["dst_id"], e["weight"]
            self.out[s][d] = max(self.out[s].get(d, 0), w)
            self.inc[d][s] = max(self.inc[d].get(s, 0), w)

    def neighbors(self, fid: int) -> dict[int, float]:
        merged = dict(self.out.get(fid, {}))
        for k, w in self.inc.get(fid, {}).items():
            merged[k] = max(merged.get(k, 0), w * 0.8)  # dependents matter slightly less
        return merged
