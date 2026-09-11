"""AI provider abstraction and cost accounting (roadmap §13, §16 item 12, §17).

Uses only urllib so the tool has no SDK dependencies. Every call returns
usage counts; cost is estimated from a small pricing table that you can
override in the config (USD per million tokens).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

# USD per 1M tokens: (input, output). Update as prices change; unknown models fall back to a default.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
}
DEFAULT_PRICE = (3.0, 15.0)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICING.get(model)
    if price is None:  # prefix match, e.g. versioned model ids
        for k, v in PRICING.items():
            if model.startswith(k):
                price = v
                break
    inp, out = price or DEFAULT_PRICE
    return input_tokens / 1e6 * inp + output_tokens / 1e6 * out


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    model: str
    provider: str

    @property
    def cost_usd(self) -> float:
        if self.provider == "local":
            return 0.0
        return estimate_cost(self.model, self.input_tokens, self.output_tokens)


class ProviderError(RuntimeError):
    pass


def _post(url: str, headers: dict[str, str], body: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ProviderError(f"HTTP {e.code} from {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise ProviderError(f"Network error calling {url}: {e.reason}") from e


def complete(provider: str, model: str, system: str, user: str, max_tokens: int = 4000) -> Completion:
    """Call a provider. NOKSHI_API_BASE overrides the endpoint base (proxies, self-hosted gateways)."""
    provider = provider.lower()
    base_override = os.environ.get("NOKSHI_API_BASE", "").rstrip("/")
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        data = _post(
            (base_override or "https://api.anthropic.com") + "/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {"model": model, "max_tokens": max_tokens, "system": system,
             "messages": [{"role": "user", "content": user}]},
        )
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        return Completion(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                          usage.get("cache_read_input_tokens", 0), data.get("model", model), provider)

    if provider in ("openai", "openrouter", "local"):
        if provider == "openai":
            key = os.environ.get("OPENAI_API_KEY")
            url = (base_override or "https://api.openai.com/v1") + "/chat/completions"
            headers = {"Authorization": f"Bearer {key}"}
        elif provider == "openrouter":
            key = os.environ.get("OPENROUTER_API_KEY")
            url = (base_override or "https://openrouter.ai/api/v1") + "/chat/completions"
            headers = {"Authorization": f"Bearer {key}", "HTTP-Referer": "https://github.com/nokshi",
                       "X-Title": "Nokshi"}
        else:  # any OpenAI-compatible server: Ollama, LM Studio, vLLM, llama.cpp
            key = os.environ.get("LOCAL_API_KEY", "local")
            url = (base_override or "http://localhost:11434/v1") + "/chat/completions"
            headers = {"Authorization": f"Bearer {key}"}
        if not key:
            raise ProviderError(f"{provider.upper()}_API_KEY is not set")
        data = _post(url, headers, {"model": model, "max_tokens": max_tokens,
                                    "messages": [{"role": "system", "content": system},
                                                 {"role": "user", "content": user}]})
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content", "") or ""
        usage = data.get("usage", {})
        return Completion(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                          (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                          data.get("model", model), provider)

    raise ProviderError(f"Unknown provider '{provider}'. Use anthropic, openai, openrouter or local.")


SYSTEM_PROMPTS = {
    "planner": (
        "You are Nokshi's Planner agent. You receive a task and a token-budgeted context package "
        "built from deterministic repository analysis. Produce a concise, numbered execution plan: "
        "steps, exact files/symbols to change or create, dependencies between steps, risks, and how to "
        "verify each step. If context is missing, list the specific files you need under 'Needs context'. "
        "Never write full implementations; this is planning only."
    ),
    "developer": (
        "You are Nokshi's Developer agent. You receive a task, project rules and a token-budgeted context "
        "package with the relevant source. Implement the task.\n"
        "OUTPUT FORMAT (strict):\n"
        "1. For every modified file, one unified diff inside a ```diff fence, with correct `diff --git a/<path> b/<path>`, "
        "`--- a/<path>`, `+++ b/<path>` headers and accurate @@ hunk headers. Paths are repository-relative. Use the exact "
        "existing lines as context (3 lines) so the patch applies cleanly.\n"
        "2. For every NEW file, write `FILE: <path>` on its own line followed by the complete file in a code fence "
        "(not a diff).\n"
        "3. Finish with a short `## Summary` section: files_changed, decisions, tests (what to run / what you added).\n"
        "Rules: follow the project rules; do not rewrite files you were only shown as signatures — if you need a file's "
        "full content, list it under `## Needs context` and stop; never invent APIs that are not in the context; keep "
        "changes minimal and add or update tests for new behaviour."
    ),
    "fix": (
        "You are Nokshi's Developer agent in RETRY mode. Your previous patch did not apply or tests failed. "
        "You get the exact error output and the current content of the affected files. Produce corrected output in the "
        "same strict format (```diff fences per modified file with exact context lines; `FILE: <path>` + full content "
        "for new files; `## Summary`)."
    ),
    "reviewer": (
        "You are Nokshi's Reviewer agent. You receive a git diff, the task it should implement, project "
        "rules and relevant surrounding code. Report: 1) Critical issues (bugs, security, data loss, "
        "broken contracts), 2) Warnings (rule violations, missing tests, edge cases), 3) Suggestions, "
        "4) Score /10 with one-line justification. Be specific: cite file and line. No praise padding."
    ),
    "summarizer": (
        "You write terse technical summaries of source files for an engineering index. Given a file path, its "
        "symbol signatures and an excerpt, answer in 2–3 sentences, max 70 words, plain text, no markdown: what the "
        "file is responsible for, its key public operations, and what it depends on or is used by if evident. "
        "Do not restate the file name. Do not speculate beyond the code shown."
    ),
    "ask": (
        "You are Nokshi, an engineering assistant answering questions about a codebase using only the "
        "provided context package. Cite file paths. If the answer is not in the context, say which files "
        "would be needed instead of guessing."
    ),
}
