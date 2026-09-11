"""Project configuration.

Layers (later wins): built-in defaults -> <repo>/.nokshi/config.toml -> environment.
The config file is created by `nokshi init` and is meant to be committed.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

NOKSHI_DIR = ".nokshi"
DB_FILE = "index.db"
CONFIG_FILE = "config.toml"
RULES_FILE = "rules.md"

DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".nokshi", ".idea", ".vscode",
    "node_modules", "vendor", "dist", "build", "out", "coverage", ".next", ".nuxt",
    "__pycache__", ".venv", "venv", "env", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "bin", "obj", "storage", "bootstrap/cache", "public/build", "target", ".turbo", ".cache",
}
DEFAULT_IGNORE_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock", "poetry.lock",
    "Pipfile.lock", "Cargo.lock", "*.min.js", "*.min.css", "*.map", "*.lock", "*.log",
    "*.sqlite", "*.db", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico", "*.webp",
    "*.pdf", "*.zip", "*.gz", "*.tar", "*.woff", "*.woff2", "*.ttf", "*.eot", "*.mp4", "*.mp3",
    "*.pyc", "*.dll", "*.exe", "*.so", "*.class", "*.jar",
    # never index secrets
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "id_rsa*", "*.secret",
    "secrets.json", "secrets.yaml", "secrets.yml", "*credentials*.json", ".npmrc", ".pypirc", ".netrc",
}

DEFAULT_CONFIG_TOML = """# Nokshi project configuration — commit this file.

[index]
# Extra directories / glob patterns to skip (added to built-in defaults).
ignore_dirs = []
ignore_files = []
# Files above this size are indexed by metadata only (no symbol extraction).
max_file_kb = 512

[budget]
# Token budgets per agent role (see roadmap §6). Enforced before any model call.
planner = 6000
developer = 16000
reviewer = 10000
# Default budget for `nokshi context` when --budget is not given.
context = 12000
# Tokens reserved for headers, rules and the task itself inside a package.
reserve = 800

[ranking]
# Relative weights of the ranking signals. Tune per repository.
lexical = 1.0
explicit_reference = 3.0
dependency = 0.6
git_recency = 0.3
module_match = 0.4
test_relevance = 0.5
memory = 0.5

[run]
# Command `nokshi run` uses to validate the agent's change inside the worktree.
# Auto-detected (pest/phpunit/vitest/jest/pytest/dotnet test) when empty.
test_command = ""
test_timeout = 600
# Heavy, git-ignored directories symlinked from your working tree into each worktree.
symlink = ["vendor", "node_modules", ".venv"]
# Maximum automatic retries when the patch fails to apply or tests fail.
max_retries = 1

[summaries]
# `nokshi summarize` writes 2–3 sentence AI summaries for files with at least this many tokens,
# using [provider].cheap_model. Summaries feed ranking and replace raw code in the signatures tier.
min_tokens = 600
# Parallel requests while summarizing.
concurrency = 4

[provider]
# anthropic | openai | openrouter | local | none. API keys are read from the environment:
# ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY. "local" talks to any OpenAI-compatible
# server (Ollama, LM Studio, vLLM) at NOKSHI_API_BASE (default http://localhost:11434/v1).
name = "anthropic"
model = "claude-sonnet-4-6"
# Cheaper model for summaries/classification (roadmap §16 "model routing").
cheap_model = "claude-haiku-4-5-20251001"
"""


@dataclass
class Budgets:
    planner: int = 6000
    developer: int = 16000
    reviewer: int = 10000
    context: int = 12000
    reserve: int = 800


@dataclass
class RankingWeights:
    lexical: float = 1.0
    explicit_reference: float = 3.0
    dependency: float = 0.6
    git_recency: float = 0.3
    module_match: float = 0.4
    test_relevance: float = 0.5
    memory: float = 0.5


@dataclass
class RunConfig:
    test_command: str = ""
    test_timeout: int = 600
    symlink: list[str] = field(default_factory=lambda: ["vendor", "node_modules", ".venv"])
    max_retries: int = 1


@dataclass
class SummariesConfig:
    min_tokens: int = 600
    concurrency: int = 4


@dataclass
class ProviderConfig:
    name: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    cheap_model: str = "claude-haiku-4-5-20251001"


@dataclass
class Config:
    root: Path
    ignore_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_IGNORE_DIRS))
    ignore_files: set[str] = field(default_factory=lambda: set(DEFAULT_IGNORE_FILES))
    max_file_kb: int = 512
    budgets: Budgets = field(default_factory=Budgets)
    weights: RankingWeights = field(default_factory=RankingWeights)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    run: RunConfig = field(default_factory=RunConfig)
    summaries: SummariesConfig = field(default_factory=SummariesConfig)

    @property
    def nokshi_dir(self) -> Path:
        return self.root / NOKSHI_DIR

    @property
    def db_path(self) -> Path:
        return self.nokshi_dir / DB_FILE

    @property
    def rules_path(self) -> Path:
        return self.nokshi_dir / RULES_FILE

    @property
    def config_path(self) -> Path:
        return self.nokshi_dir / CONFIG_FILE


def find_root(start: Path | None = None) -> Path:
    """Walk upward to find a directory containing .nokshi/ or .git/; fall back to start."""
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / NOKSHI_DIR).is_dir() or (candidate / ".git").exists():
            return candidate
    return cur


def _apply(dc, data: dict) -> None:
    for key, value in data.items():
        if hasattr(dc, key):
            setattr(dc, key, type(getattr(dc, key))(value))


def load_config(root: Path | None = None) -> Config:
    root = (root or find_root()).resolve()
    cfg = Config(root=root)
    path = cfg.config_path
    if path.is_file():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        idx = data.get("index", {})
        cfg.ignore_dirs |= set(idx.get("ignore_dirs", []))
        cfg.ignore_files |= set(idx.get("ignore_files", []))
        cfg.max_file_kb = int(idx.get("max_file_kb", cfg.max_file_kb))
        _apply(cfg.budgets, data.get("budget", {}))
        _apply(cfg.weights, data.get("ranking", {}))
        _apply(cfg.provider, data.get("provider", {}))
        _apply(cfg.run, data.get("run", {}))
        _apply(cfg.summaries, data.get("summaries", {}))

    ignore_file = root / ".nokshiignore"
    if ignore_file.is_file():
        for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            (cfg.ignore_dirs if line.endswith("/") else cfg.ignore_files).add(line.rstrip("/"))

    if name := os.environ.get("NOKSHI_PROVIDER"):
        cfg.provider.name = name
    if model := os.environ.get("NOKSHI_MODEL"):
        cfg.provider.model = model
    return cfg


def write_default_config(cfg: Config) -> bool:
    """Create .nokshi/config.toml and rules.md if missing. Returns True if created."""
    cfg.nokshi_dir.mkdir(parents=True, exist_ok=True)
    created = False
    if not cfg.config_path.exists():
        cfg.config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        created = True
    if not cfg.rules_path.exists():
        cfg.rules_path.write_text(
            "# Project rules\n\n"
            "Rules listed here are included in every context package and review.\n"
            "Keep them short and concrete. Examples:\n\n"
            "- Business logic lives in Services; controllers only validate and delegate.\n"
            "- Every new public method needs a unit test.\n",
            encoding="utf-8",
        )
        created = True
    gitignore = cfg.nokshi_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("index.db\nindex.db-*\n", encoding="utf-8")
    return created
