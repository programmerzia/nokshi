"""Token counting.

Budgets are enforced *before* a model call (roadmap §6), so the counter must
work offline. We use tiktoken's cl100k_base when it is installed and its
encoding files are available; otherwise a heuristic calibrated for source code.
Both are estimates for non-OpenAI tokenizers, so budgets keep a reserve.
"""

from __future__ import annotations

import re
from functools import lru_cache

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|\s+|.", re.DOTALL)


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken  # type: ignore

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def backend() -> str:
    return "tiktoken/cl100k_base" if _encoder() else "heuristic"


def _heuristic(text: str) -> int:
    """Approximate BPE behaviour: identifiers ~ 1 token per 5-6 chars, punctuation ~1 each,
    whitespace runs mostly merge. Calibrated against cl100k on mixed PHP/TS/Python/C#."""
    total = 0
    for m in _WORD.finditer(text):
        tok = m.group(0)
        if tok.isspace():
            total += 1 if "\n" in tok else (1 if len(tok) > 3 else 0)
        elif tok[0].isalpha() or tok[0] == "_":
            total += max(1, (len(tok) + 4) // 5)
        elif tok.isdigit():
            total += max(1, (len(tok) + 2) // 3)
        else:
            total += 1
    return int(total * 1.05)


def count(text: str) -> int:
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    return _heuristic(text)
