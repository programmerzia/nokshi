"""Nokshi test suite. Run: pytest -q"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nokshi.agents.providers import estimate_cost
from nokshi.context.builder import CONFIDENT_SCORE, build_context, changed_files_from_diff, redact
from nokshi.context.ranking import rank_files, tokenize
from nokshi.core import tokens
from nokshi.core.config import Config, load_config, write_default_config
from nokshi.core.db import Database
from nokshi.core.graph import Graph
from nokshi.core.parsers import language_for, parser_for
from nokshi.core.parsers.base import blank_out_noise, split_identifier
from nokshi.core.scanner import discover, index_repository, is_test_path, module_of

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

PHP_SERVICE = '''<?php
namespace App\\Services;

use App\\Models\\Invoice;
use App\\Services\\Payments\\{PaymentProvider, StripeProvider};

/** docblock with { brace and "quote */
final class PaymentService
{
    private string $note = "class Fake { function nope() {} }";

    public function __construct(private PaymentProvider $provider) {}

    public function charge(Invoice $invoice, int $amount): void
    {
        if ($amount <= 0) { throw new \\InvalidArgumentException('x'); }
        $this->provider->charge($amount); // comment with {
    }

    abstract protected function hook(): void;
}
'''

TS_MODULE = '''import { ref } from 'vue'
import { formatMoney } from '@/utils/money'
import Foo from './foo'
const x = require('./legacy')
export const parse = (s: string): number => {
  return 1
}
export async function fetchAll<T>(url: string): Promise<T[]> { return [] }
export interface Invoice { id: number }
export type Money = { cents: number }
export default class CartStore {
  private items: string[] = []
  add(item: string): void { this.items.push(item) }
  async load(): Promise<void> { if (true) { return } }
}
'''

CS_FILE = '''using System;
using Api.Services;

namespace Api.Controllers;

[ApiController]
public class RefundsController : ControllerBase
{
    private readonly IRefundService _svc;
    public RefundsController(IRefundService svc) { _svc = svc; }

    [HttpPost]
    public async Task<IActionResult> Create([FromBody] RefundRequest req)
    {
        return Ok(await _svc.RefundAsync(req.Id));
    }
    public string Name { get; set; } = "x";
    private int Helper() => 1;
}
public record RefundRequest(string Id);
'''

PY_FILE = '''from . import helpers
from .helpers import normalize
import os

MAX = 3

class Scorer:
    def score(self, t: str) -> int:
        return len(normalize(t))

def main():
    pass
'''


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    files = {
        "composer.json": json.dumps({"require": {"laravel/framework": "^12"}, "autoload": {"psr-4": {"App\\": "app/"}}}),
        "package.json": json.dumps({"dependencies": {"vue": "^3"}, "devDependencies": {"typescript": "^5"}}),
        "tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["resources/js/*"]}}}',
        "app/Services/PaymentService.php": PHP_SERVICE,
        "app/Services/Payments/PaymentProvider.php": "<?php\nnamespace App\\Services\\Payments;\ninterface PaymentProvider { public function charge(int $a): array; public function refund(string $id): array; }\n",
        "app/Services/Payments/StripeProvider.php": "<?php\nnamespace App\\Services\\Payments;\nclass StripeProvider implements PaymentProvider { public function charge(int $a): array { return []; } public function refund(string $id): array { return []; } }\n",
        "app/Models/Invoice.php": "<?php\nnamespace App\\Models;\nclass Invoice { public $currency = 'usd'; }\n",
        "app/Models/User.php": "<?php\nnamespace App\\Models;\nclass User { public function isAdmin(): bool { return false; } }\n",
        "app/Http/Controllers/UserController.php": "<?php\nnamespace App\\Http\\Controllers;\nuse App\\Models\\User;\nclass UserController { public function index() { return User::all(); } }\n",
        "tests/Unit/PaymentServiceTest.php": "<?php\nnamespace Tests\\Unit;\nuse App\\Services\\PaymentService;\nclass PaymentServiceTest { public function test_charge() {} }\n",
        "resources/js/store.ts": TS_MODULE,
        "resources/js/utils/money.ts": "export const formatMoney = (c: number) => c\n",
        "resources/js/foo.ts": "export default 1\n",
        "resources/js/legacy.js": "module.exports = {}\n",
        "src/Api/Controllers/RefundsController.cs": CS_FILE,
        "src/Api/Services/RefundService.cs": "namespace Api.Services\n{\n    public interface IRefundService { }\n    public class RefundService : IRefundService { }\n}\n",
        "scripts/__init__.py": "",
        "scripts/score.py": PY_FILE,
        "scripts/helpers.py": "def normalize(t):\n    return t\n",
        ".env": "DB_PASSWORD=\"topsecret\"\n",
        "config/app.php": "<?php return ['key' => 'x'];\n",
        "vendor/x/Junk.php": "<?php class Junk {}\n",
        "node_modules/y/index.js": "x",
        "README.md": "# demo\n",
    }
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return root


@pytest.fixture()
def indexed(repo: Path) -> tuple[Config, Database]:
    cfg = load_config(repo)
    write_default_config(cfg)
    db = Database(cfg.db_path)
    index_repository(cfg, db)
    return cfg, db


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------

def test_blank_out_noise_preserves_length_and_lines():
    src = 'a = "str { }"; // c {\n/* multi\nline */ b'
    out = blank_out_noise(src)
    assert len(out) == len(src) and out.count("\n") == src.count("\n")
    assert "{" not in out.replace("{", "", 0) or out.count("{") == 0


def test_split_identifier():
    assert split_identifier("PaymentService") == ["payment", "service"]
    assert split_identifier("get_user_id") == ["get", "user", "id"]
    assert split_identifier("HTTPServer") == ["http", "server"]


def test_php_parser_symbols_and_imports():
    res = parser_for("php").parse(PHP_SERVICE)
    names = {(s.kind, s.name, s.parent) for s in res.symbols}
    assert ("class", "PaymentService", None) in names
    assert ("method", "charge", "PaymentService") in names
    assert ("method", "hook", "PaymentService") in names
    assert ("property", "note", "PaymentService") in names
    assert not any(s.name == "Fake" for s in res.symbols), "class inside a string must be ignored"
    raws = {i.raw for i in res.imports}
    assert raws == {"App\\Models\\Invoice", "App\\Services\\Payments\\PaymentProvider", "App\\Services\\Payments\\StripeProvider"}
    assert res.namespace == "App\\Services"
    cls = next(s for s in res.symbols if s.name == "PaymentService")
    assert cls.line_start == 8 and cls.line_end == 21


def test_ts_parser():
    res = parser_for("typescript").parse(TS_MODULE)
    kinds = {(s.kind, s.name) for s in res.symbols}
    assert {("function", "parse"), ("function", "fetchAll"), ("interface", "Invoice"), ("type", "Money"),
            ("class", "CartStore"), ("method", "add"), ("method", "load")} <= kinds
    imports = {(i.raw, i.kind) for i in res.imports}
    assert ("vue", "module") in imports and ("@/utils/money", "relative") in imports
    assert ("./foo", "relative") in imports and ("./legacy", "relative") in imports


def test_vue_sfc_parser():
    sfc = "<template><div>{{ x }}</div></template>\n<script setup lang=\"ts\">\nimport A from './a'\nfunction go() {}\n</script>\n"
    res = parser_for("vue").parse(sfc)
    assert [s.name for s in res.symbols] == ["go"]
    assert res.symbols[0].line_start == 4
    assert res.imports[0].raw == "./a"


def test_csharp_parser():
    res = parser_for("csharp").parse(CS_FILE)
    kinds = {(s.kind, s.name, s.parent) for s in res.symbols}
    assert ("class", "RefundsController", None) in kinds
    assert ("constructor", "RefundsController", "RefundsController") in kinds
    assert ("method", "Create", "RefundsController") in kinds
    assert ("method", "Helper", "RefundsController") in kinds
    assert ("property", "Name", "RefundsController") in kinds
    assert ("record", "RefundRequest", None) in kinds
    assert {i.raw for i in res.imports} == {"System", "Api.Services"}
    assert res.namespace == "Api.Controllers"


def test_python_parser():
    res = parser_for("python").parse(PY_FILE)
    names = {(s.kind, s.name) for s in res.symbols}
    assert {("class", "Scorer"), ("method", "score"), ("function", "main"), ("const", "MAX")} <= names
    assert any(i.raw == ".helpers" and i.kind == "relative" for i in res.imports)
    assert any(i.raw == "os" for i in res.imports)


def test_language_detection():
    assert language_for("a/b.blade.php") == "blade"
    assert language_for("x.d.ts") == "typescript"
    assert language_for("Dockerfile") == "docker"
    assert language_for(".env") is None, ".env must never be indexed"
    assert language_for("weird.xyz") is None


# ---------------------------------------------------------------------------
# scanner / graph
# ---------------------------------------------------------------------------

def test_scan_respects_ignores_and_detects_frameworks(indexed):
    cfg, db = indexed
    paths = {r["path"] for r in db.all_files()}
    assert "app/Services/PaymentService.php" in paths
    assert not any(p.startswith(("vendor/", "node_modules/")) for p in paths)
    assert ".env" not in paths
    fw = json.loads(db.get_meta("frameworks"))
    assert "Laravel" in fw and "Vue" in fw and "TypeScript" in fw


def test_helpers():
    assert is_test_path("tests/Unit/FooTest.php") and is_test_path("src/a.spec.ts") and is_test_path("x/test_a.py")
    assert not is_test_path("app/Services/Payment.php")
    assert module_of("app/Services/Payments/Stripe.php") == "app/Services"
    assert module_of("README.md") == "(root)"


def test_dependency_resolution(indexed):
    cfg, db = indexed
    by_id = {r["id"]: r["path"] for r in db.all_files()}
    edges = {(by_id[e["src_id"]], by_id[e["dst_id"]], e["kind"]) for e in db.all_deps()}
    # PSR-4 + group use
    assert ("app/Services/PaymentService.php", "app/Services/Payments/PaymentProvider.php", "import") in edges
    assert ("app/Services/PaymentService.php", "app/Services/Payments/StripeProvider.php", "import") in edges
    assert ("app/Services/PaymentService.php", "app/Models/Invoice.php", "import") in edges
    # tsconfig alias, relative with/without extension, require()
    assert ("resources/js/store.ts", "resources/js/utils/money.ts", "import") in edges
    assert ("resources/js/store.ts", "resources/js/foo.ts", "import") in edges
    assert ("resources/js/store.ts", "resources/js/legacy.js", "import") in edges
    # C# namespace, Python relative
    assert ("src/Api/Controllers/RefundsController.cs", "src/Api/Services/RefundService.cs", "import") in edges
    assert ("scripts/score.py", "scripts/helpers.py", "import") in edges
    # reference edge (implements without use statement)
    assert ("app/Services/Payments/StripeProvider.php", "app/Services/Payments/PaymentProvider.php", "reference") in edges


def test_incremental_index(indexed):
    cfg, db = indexed
    before = {r["path"]: r["sha256"] for r in db.all_files()}
    (cfg.root / "app/Models/User.php").write_text("<?php\nnamespace App\\Models;\nclass User { public function isAdmin(): bool { return true; } public function ban() {} }\n")
    (cfg.root / "app/Models/Invoice.php").unlink()
    (cfg.root / "app/Models/Refund.php").write_text("<?php\nnamespace App\\Models;\nclass Refund {}\n")
    stats = index_repository(cfg, db)
    assert stats.added == 1 and stats.updated == 1 and stats.removed == 1
    after = {r["path"]: r["sha256"] for r in db.all_files()}
    assert "app/Models/Invoice.php" not in after and "app/Models/Refund.php" in after
    assert after["app/Models/User.php"] != before["app/Models/User.php"]
    assert stats.unchanged == len(before) - 2
    assert any(s["name"] == "ban" for s in db.all_symbols())
    # no dangling edges to the deleted file
    ids = {r["id"] for r in db.all_files()}
    assert all(e["src_id"] in ids and e["dst_id"] in ids for e in db.all_deps())


# ---------------------------------------------------------------------------
# ranking / context
# ---------------------------------------------------------------------------

def test_tokenize_drops_stopwords_and_splits_identifiers():
    toks = tokenize("Add refund support to PaymentService when the invoice is paid")
    assert "refund" in toks and "payment" in toks and "paymentservice" in toks and "invoice" in toks
    assert "add" not in toks and "the" not in toks


def test_ranking_prefers_relevant_module(indexed):
    cfg, db = indexed
    ranked = rank_files(cfg, db, "Add refund support to the payment flow")
    top = [c.path for c in ranked[:4]]
    assert any("Payment" in p for p in top)
    assert "app/Http/Controllers/UserController.php" not in top
    assert "app/Models/User.php" not in top


def test_explicit_file_forces_inclusion(indexed):
    cfg, db = indexed
    pkg = build_context(cfg, db, "unrelated words", explicit_files=["UserController.php"], budget=4000)
    assert "app/Http/Controllers/UserController.php" in pkg.files_full


def test_budget_is_enforced(indexed):
    cfg, db = indexed
    for budget in (1500, 3000, 8000):
        pkg = build_context(cfg, db, "Add refund support to the payment flow", budget=budget)
        assert pkg.tokens <= budget, f"{pkg.tokens} > {budget}"
        assert pkg.budget == budget
    assert pkg.baseline_tokens > pkg.tokens
    assert "# Task" in pkg.markdown and "PaymentService" in pkg.markdown
    run = db.conn.execute("SELECT COUNT(*) n FROM context_runs").fetchone()["n"]
    assert run == 3


def test_context_json(indexed):
    cfg, db = indexed
    pkg = build_context(cfg, db, "refund payment", budget=4000)
    data = json.loads(pkg.to_json())
    assert data["budget"] == 4000 and data["files"]
    assert {f["representation"] for f in data["files"]} <= {"full", "regions", "signatures", "map"}


def test_signatures_only_mode(indexed):
    cfg, db = indexed
    pkg = build_context(cfg, db, "refund payment", budget=6000, no_full=True)
    assert not pkg.files_full
    assert "(signatures only)" in pkg.markdown


def test_rules_included(indexed):
    cfg, db = indexed
    cfg.rules_path.write_text("# rules\n- Refunds must be idempotent by charge id\n")
    db.add_memory("decision", "Stripe is the only provider for now")
    pkg = build_context(cfg, db, "refund", budget=3000)
    assert "idempotent" in pkg.markdown and "Stripe is the only provider" in pkg.markdown


def test_redaction():
    src = 'DB_PASSWORD="hunter2hunter2"\nkey = "sk-abcdefghijklmnopqrstuvwxyz1234"\n"Default": "Server=x;Password=abc123;"\nname = "PaymentService"\n'
    out = redact(src)
    assert "hunter2" not in out and "sk-abcdef" not in out and "abc123" not in out
    assert "PaymentService" in out


def test_changed_files_from_diff():
    d = "diff --git a/app/A.php b/app/A.php\n--- a\n+++ b\ndiff --git a/x/y.ts b/x/y.ts\n"
    assert changed_files_from_diff(d) == ["app/A.php", "x/y.ts"]


def test_graph_neighbors(indexed):
    cfg, db = indexed
    g = Graph(db)
    svc = db.file_by_path("app/Services/PaymentService.php")["id"]
    test = db.file_by_path("tests/Unit/PaymentServiceTest.php")["id"]
    assert test in g.inc[svc]
    assert svc in g.neighbors(test)


# ---------------------------------------------------------------------------
# tokens / cost
# ---------------------------------------------------------------------------

def test_token_counter_is_monotonic_and_reasonable():
    small = tokens.count("hello world")
    big = tokens.count(PHP_SERVICE)
    assert 0 < small < big
    assert 80 < big < 400  # ~ 60 lines of PHP


def test_cost_estimate():
    assert estimate_cost("claude-sonnet-4-6", 1_000_000, 0) == pytest.approx(3.0)
    assert estimate_cost("claude-haiku-4-5-20251001-extra", 0, 1_000_000) == pytest.approx(5.0)  # prefix match
    assert estimate_cost("unknown-model", 1_000_000, 1_000_000) == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

def test_cli_end_to_end(repo: Path):
    def run(*args):
        return subprocess.run(["nokshi", "--root", str(repo), *args], capture_output=True, text=True, timeout=60)

    assert run("init").returncode == 0
    r = run("analyze"); assert r.returncode == 0 and "symbols indexed" in r.stdout
    r = run("status"); assert r.returncode == 0 and "Laravel" in r.stdout
    r = run("search", "charge"); assert "PaymentService" in r.stdout
    r = run("graph", "PaymentService"); assert "PaymentProvider" in r.stdout
    r = run("context", "add refund support", "--budget", "3000", "--json"); assert r.returncode == 0
    assert json.loads(r.stdout)["budget"] == 3000
    r = run("remember", "Refunds are idempotent", "--kind", "decision"); assert r.returncode == 0
    r = run("memory"); assert "idempotent" in r.stdout
    r = run("plan", "add refund support", "--dry-run", "--budget", "3000"); assert r.returncode == 0 and "# Task" in r.stdout
    (repo / "app/Models/User.php").write_text("<?php\nnamespace App\\Models;\nclass User { public $banned = false; }\n")
    r = run("review", "--dry-run"); assert r.returncode == 0 and "diff --git a/app/Models/User.php" in r.stdout
    r = run("cost"); assert r.returncode == 0 and "Context packages" in r.stdout


# ---------------------------------------------------------------------------
# workspace (nokshi run)
# ---------------------------------------------------------------------------

from nokshi.workspace.worktree import (
    apply_changes,
    apply_to_working_tree,
    create_workspace,
    detect_test_command,
    list_workspaces,
    parse_agent_output,
    run_tests,
    slugify,
)

AGENT_OUTPUT = '''Here is the change.

```diff
diff --git a/app/Models/User.php b/app/Models/User.php
--- a/app/Models/User.php
+++ b/app/Models/User.php
@@ -1,3 +1,3 @@
 <?php
 namespace App\\Models;
-class User { public function isAdmin(): bool { return false; } }
+class User { public function isAdmin(): bool { return true; } }
```

FILE: app/Services/RefundService.php
```php
<?php
namespace App\\Services;
class RefundService {}
```

## Summary
files_changed: User.php, RefundService.php
'''


def test_parse_agent_output():
    ch = parse_agent_output(AGENT_OUTPUT)
    assert "diff --git a/app/Models/User.php" in ch.diff
    assert ch.files == {"app/Services/RefundService.php": "<?php\nnamespace App\\Services;\nclass RefundService {}\n"}
    assert "## Summary" in ch.summary and "```" not in ch.summary
    assert parse_agent_output("no code here").empty
    assert slugify("Add Stripe refund support!!") == "add-stripe-refund-support"


def test_workspace_lifecycle(indexed):
    cfg, db = indexed
    # uncommitted work in the primary tree must be visible in the worktree but not in the agent diff
    (cfg.root / "README.md").write_text("# demo\nwip\n")
    (cfg.root / ".venv").mkdir()  # not tracked by git -> must be linked into the worktree
    ws = create_workspace(cfg, "Add refund support", symlink=[".venv"])
    try:
        assert ws.path.is_dir() and ws.branch.startswith("nokshi/add-refund-support")
        assert (ws.path / "README.md").read_text().endswith("wip\n")
        assert (ws.path / ".venv").is_symlink()
        assert ws.diff().strip() == ""

        res = apply_changes(ws, parse_agent_output(AGENT_OUTPUT))
        assert res.ok, res.errors
        assert "return true" in (ws.path / "app/Models/User.php").read_text()
        assert (ws.path / "app/Services/RefundService.php").exists()
        diff = ws.diff()
        assert "app/Models/User.php" in diff and "RefundService.php" in diff and "README" not in diff

        # primary tree untouched until approval
        assert "return false" in (cfg.root / "app/Models/User.php").read_text()
        ok, msg = apply_to_working_tree(ws)
        assert ok, msg
        assert "return true" in (cfg.root / "app/Models/User.php").read_text()
        assert (cfg.root / "app/Services/RefundService.php").exists()
        assert [w[1] for w in list_workspaces(cfg)] == [ws.branch]
    finally:
        ws.remove()
    assert not ws.path.exists() and list_workspaces(cfg) == []


def test_failed_patch_is_reported_and_cleaned(indexed):
    cfg, db = indexed
    bad = AGENT_OUTPUT.replace(" namespace App\\Models;\n-class User", " namespace WRONG;\n-class User")
    ws = create_workspace(cfg, "bad patch", symlink=[])
    try:
        res = apply_changes(ws, parse_agent_output(bad))
        assert not res.ok and "app/Models/User.php" in res.failed
        assert not list(ws.path.rglob("*.rej"))
        assert res.applied == ["app/Services/RefundService.php"]  # whole-file writes still land
    finally:
        ws.remove()


def test_unsafe_paths_refused(indexed):
    cfg, db = indexed
    ws = create_workspace(cfg, "escape", symlink=[])
    try:
        ch = parse_agent_output("FILE: ../../etc/evil.txt\n```\nx\n```\n")
        res = apply_changes(ws, ch)
        assert not res.ok and "refused unsafe path" in res.errors
    finally:
        ws.remove()


def test_run_tests_and_detection(indexed, tmp_path):
    cfg, db = indexed
    ws = create_workspace(cfg, "t", symlink=[])
    try:
        assert run_tests(ws, "exit 0").passed
        r = run_tests(ws, "echo boom; exit 3")
        assert not r.passed and "boom" in r.tail()
        assert run_tests(ws, "sleep 5", timeout=1).returncode == 124
    finally:
        ws.remove()
    assert detect_test_command(cfg.root, ["Laravel"]) is None            # no vendor/bin/phpunit present
    (cfg.root / "vendor/bin").mkdir(parents=True); (cfg.root / "vendor/bin/phpunit").write_text("")
    assert detect_test_command(cfg.root, ["Laravel", "PHPUnit"]).startswith("vendor/bin/phpunit")
    assert detect_test_command(cfg.root, ["pytest"]) == "python3 -m pytest -q -x"


def test_cli_run_dry_and_workspaces(repo: Path):
    def run(*args):
        return subprocess.run(["nokshi", "--root", str(repo), *args], capture_output=True, text=True, timeout=60)
    assert run("analyze").returncode == 0
    r = run("run", "add refund", "--dry-run"); assert r.returncode == 0 and "# Instructions" in r.stdout and "unified diffs" in r.stdout
    r = run("workspaces"); assert r.returncode == 0 and "No nokshi worktrees" in r.stdout


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------

from nokshi.agents.providers import Completion
from nokshi.context import summaries as summaries_mod
from nokshi.context.summaries import structural_summary, summarize_files


def test_structural_summary(indexed):
    cfg, db = indexed
    row = db.file_by_path("app/Services/PaymentService.php")
    text = structural_summary(db, row)
    assert "PaymentService" in text and "charge" in text and "Invoice" in text


def test_summarize_files_with_mock_provider(indexed, monkeypatch):
    cfg, db = indexed
    cfg.summaries.min_tokens = 1  # every structural file qualifies in the tiny fixture
    calls = []

    def fake_complete(provider, model, system, user, max_tokens=200):
        calls.append(model)
        assert "Path:" in user and "Signatures:" in user
        return Completion("Summarises payments and refunds.", 100, 10, 0, model, provider)

    monkeypatch.setattr(summaries_mod, "complete", fake_complete)
    dry = summarize_files(cfg, db, dry_run=True)
    assert dry.candidates > 0 and dry.written == 0
    stats = summarize_files(cfg, db)
    assert stats.written == dry.candidates and stats.failed == 0
    assert set(calls) == {cfg.provider.cheap_model}
    assert db.file_by_path("app/Services/PaymentService.php")["summary"].startswith("Summarises")
    # cached: nothing to do until the file changes
    assert summarize_files(cfg, db).candidates == 0
    (cfg.root / "app/Services/PaymentService.php").write_text("<?php\nnamespace App\\Services;\nclass PaymentService { public function refund() {} }\n")
    index_repository(cfg, db)
    assert summarize_files(cfg, db, dry_run=True).candidates == 1
    # summaries feed the package and cost report
    pkg = build_context(cfg, db, "payments refunds", budget=1200, no_full=True)
    assert "Summarises payments" in pkg.markdown
    assert db.usage_summary(0)["totals"]["n"] >= dry.candidates


def test_summarize_reports_provider_errors(indexed, monkeypatch):
    cfg, db = indexed
    cfg.summaries.min_tokens = 1
    from nokshi.agents.providers import ProviderError

    def boom(*a, **k):
        raise ProviderError("HTTP 401")

    monkeypatch.setattr(summaries_mod, "complete", boom)
    stats = summarize_files(cfg, db)
    assert stats.written == 0 and stats.failed >= 1 and "401" in stats.errors[0]


def test_cli_task_dry_run_and_version(repo: Path):
    def run(*args):
        return subprocess.run(["nokshi", "--root", str(repo), *args], capture_output=True, text=True, timeout=60)
    assert "Intelligent Context" in run("--version").stdout
    assert run("analyze").returncode == 0
    r = run("task", "add refund", "--dry-run")
    assert r.returncode == 0 and "PLANNING" in r.stderr and "# Task" in r.stdout


# ---------------------------------------------------------------------------
# Vague-task guard rails: a task that matches nothing must not spend the budget
# on noise, and must say so instead of quietly returning a weak guess.
# ---------------------------------------------------------------------------

def test_legal_text_is_never_indexed(repo: Path):
    """A font/library licence is attribution text, not engineering context."""
    (repo / "assets").mkdir(exist_ok=True)
    (repo / "assets" / "OFL.txt").write_text("Copyright 2020 The Poppins Project Authors\n" + "licence " * 400)
    (repo / "LICENSE").write_text("MIT License\n" + "permission " * 400)
    cfg = load_config(repo)
    write_default_config(cfg)
    found = discover(cfg)
    assert not any("OFL.txt" in f or f == "LICENSE" for f in found), found


def test_a_vague_task_earns_no_full_source(indexed):
    """35% of a noise score is still noise — the full-source tier needs an absolute floor."""
    cfg, db = indexed
    pkg = build_context(cfg, db, "make this project as a saas")
    assert pkg.top_score < CONFIDENT_SCORE
    assert not [s for s in pkg.selections if s.representation in ("full", "regions")], \
        [(s.candidate.path, s.representation) for s in pkg.selections]


def test_a_specific_task_still_gets_full_source(indexed):
    """The floor must not punish a task that genuinely matches."""
    cfg, db = indexed
    pkg = build_context(cfg, db, "refund support in PaymentService")
    assert pkg.top_score >= CONFIDENT_SCORE
    assert [s for s in pkg.selections if s.representation in ("full", "regions")]


def test_cli_warns_on_a_vague_task(repo: Path):
    def run(*args):
        return subprocess.run(["nokshi", "--root", str(repo), *args], capture_output=True, text=True, timeout=60)
    assert run("analyze").returncode == 0
    r = run("ctx", "make this project as a saas")
    assert r.returncode == 0
    # either branch of the guard is acceptable; what matters is that the user is told
    # the task was too vague and what to do about it, instead of getting silent filler.
    assert ("No file matched this task" in r.stderr) or ("Low confidence" in r.stderr)
    assert "Name a file, class or module" in r.stderr
    assert "--signatures-only" in r.stderr

    # a task that does name something must stay quiet
    r2 = run("ctx", "refund support in PaymentService")
    assert r2.returncode == 0
    assert "Low confidence" not in r2.stderr and "No file matched" not in r2.stderr


def test_tests_cannot_crowd_out_the_source_they_test(indexed):
    """Tests share a module signal with their siblings; on a weak task they must not fill the package."""
    cfg, db = indexed
    pkg = build_context(cfg, db, "refund support in PaymentService")
    loaded = [s for s in pkg.selections if s.representation != "map"]
    tests = [s for s in loaded if s.candidate.is_test]
    assert len(tests) <= max(1, int(len(loaded) * 0.5)), [s.candidate.path for s in loaded]


def test_a_task_about_tests_still_gets_tests(indexed):
    """The cap must lift when the task is actually about tests."""
    cfg, db = indexed
    pkg = build_context(cfg, db, "add a regression test for PaymentService refunds")
    loaded = [s for s in pkg.selections if s.representation != "map"]
    assert any(s.candidate.is_test for s in loaded), [s.candidate.path for s in loaded]


def test_a_named_file_contributes_source_even_when_the_task_is_additive(indexed):
    """An "add X" task names something that does not exist yet, so no region matches X.

    The file the user pointed at must still contribute source, not drop to signatures.
    """
    cfg, db = indexed
    pkg = build_context(cfg, db, "add tenant_id scoping to app/Services/PaymentService.php")
    named = [s for s in pkg.selections if s.candidate.path == "app/Services/PaymentService.php"]
    assert named, [s.candidate.path for s in pkg.selections]
    assert named[0].representation in ("full", "regions"), named[0].representation
