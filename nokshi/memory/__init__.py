"""Project memory: rules.md (versioned, human-edited) + decisions/notes stored in the index."""

from __future__ import annotations

from nokshi.core.config import Config
from nokshi.core.db import Database

KINDS = ("decision", "note", "bug", "pattern")


def rules_text(cfg: Config) -> str:
    return cfg.rules_path.read_text(encoding="utf-8", errors="replace").strip() if cfg.rules_path.is_file() else ""


def rules_bullets(cfg: Config) -> str:
    """Only the list items — headings and explanatory prose in rules.md are for humans."""
    return "\n".join(l for l in rules_text(cfg).splitlines() if l.strip().startswith(("-", "*", "1", "2", "3", "4", "5", "6", "7", "8", "9")))


def remember(db: Database, kind: str, body: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    db.add_memory(kind, body.strip())


def forget(db: Database, memory_id: int) -> int:
    return db.delete_memory(memory_id)


def memories(db: Database):
    return db.memories()


def memory_block(cfg: Config, db: Database) -> str:
    """Markdown block injected into every context package."""
    parts = []
    bullets = rules_bullets(cfg)
    if bullets:
        parts.append(bullets)
    for m in memories(db):
        parts.append(f"- ({m['kind']}) {m['body']}")
    return "\n".join(parts)
