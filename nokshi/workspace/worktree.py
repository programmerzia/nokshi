"""Safe agent execution (roadmap §11–§12).

Agents never touch the primary working tree. `nokshi run` creates a git worktree on a
fresh branch under .nokshi/worktrees/, applies the Developer agent's diff there,
runs the project's tests, and only then asks the human to apply, keep, or discard.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from nokshi.core.config import Config

WORKTREES_DIR = "worktrees"
_DIFF_BLOCK = re.compile(r"```(?:diff|patch)\s*\n(.*?)```", re.S)
_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M)
_FILE_BLOCK = re.compile(r"^(?:#{2,4}\s*)?(?:FILE|File|file):\s*`?([\w./\-]+)`?\s*\n```[\w+-]*\n(.*?)```", re.M | re.S)


class WorkspaceError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].rstrip("-") or "task")


@dataclass
class Workspace:
    root: Path
    path: Path
    branch: str
    base_commit: str
    linked: list[str] = field(default_factory=list)   # symlinked dependency dirs, never part of the diff

    def _excludes(self) -> list[str]:
        return [f":(exclude){name}" for name in self.linked] + [":(exclude)*.rej", ":(exclude)*.orig"]

    def diff(self) -> str:
        """Everything the agent changed, including new files."""
        _git(self.path, "add", "-A", "-N", "--", ".", *self._excludes())  # intent-to-add so new files show in diff
        return _git(self.path, "diff", "--binary", "--", ".", *self._excludes()).stdout

    def changed_files(self) -> list[str]:
        out = _git(self.path, "status", "--porcelain", "--", ".", *self._excludes()).stdout
        return [line[3:].strip() for line in out.splitlines() if line.strip()]

    def remove(self) -> None:
        _git(self.root, "worktree", "remove", "--force", str(self.path), check=False)
        _git(self.root, "branch", "-D", self.branch, check=False)
        shutil.rmtree(self.path, ignore_errors=True)

    def commit(self, message: str) -> str:
        _git(self.path, "add", "-A", "--", ".", *self._excludes())
        proc = _git(self.path, "-c", "user.email=nokshi@local", "-c", "user.name=Nokshi",
                    "commit", "-q", "-m", message, check=False)
        if proc.returncode != 0 and "nothing to commit" not in proc.stdout + proc.stderr:
            raise WorkspaceError(proc.stderr.strip())
        return _git(self.path, "rev-parse", "--short", "HEAD").stdout.strip()


def create_workspace(cfg: Config, task: str, symlink: list[str]) -> Workspace:
    root = cfg.root
    if not (root / ".git").exists():
        raise WorkspaceError("nokshi run needs a git repository (worktrees are the safety boundary).")
    base = _git(root, "rev-parse", "HEAD").stdout.strip()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = slugify(task)
    branch = f"nokshi/{slug}-{stamp}"
    wt_dir = cfg.nokshi_dir / WORKTREES_DIR
    wt_dir.mkdir(parents=True, exist_ok=True)
    gi = cfg.nokshi_dir / ".gitignore"
    if gi.exists() and WORKTREES_DIR not in gi.read_text():
        gi.write_text(gi.read_text().rstrip("\n") + f"\n{WORKTREES_DIR}/\n")
    path = wt_dir / f"{slug}-{stamp}"
    _git(root, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
    # Bring uncommitted work along so the agent sees what the developer sees.
    dirty = _git(root, "diff", "HEAD", "--binary").stdout
    ws = Workspace(root=root, path=path, branch=branch, base_commit=base)
    if dirty.strip():
        proc = subprocess.run(["git", "apply", "--3way"], cwd=path, input=dirty, capture_output=True, text=True)
        if proc.returncode == 0:
            # Snapshot the developer's uncommitted work so ws.diff() shows only what the agent did.
            ws.commit("nokshi: snapshot of uncommitted work")
    # Dependencies are heavy and ignored by git: link them instead of reinstalling.
    for name in symlink:
        src, dst = root / name, path / name
        if src.exists() and not dst.exists():
            try:
                dst.symlink_to(src.resolve(), target_is_directory=src.is_dir())
                ws.linked.append(name)
            except OSError:
                pass
    for env_name in (".env", ".env.testing"):
        if (root / env_name).is_file() and not (path / env_name).exists():
            shutil.copy2(root / env_name, path / env_name)
    return ws


# ---------------------------------------------------------------------------
# Agent output -> changes
# ---------------------------------------------------------------------------

@dataclass
class AgentChanges:
    diff: str = ""                                  # unified diff text (may be empty)
    files: dict[str, str] = field(default_factory=dict)  # whole-file replacements: path -> content
    summary: str = ""                               # everything that is not code

    @property
    def empty(self) -> bool:
        return not self.diff.strip() and not self.files


def parse_agent_output(text: str) -> AgentChanges:
    changes = AgentChanges()
    blocks = _DIFF_BLOCK.findall(text)
    diff_parts = [b for b in blocks if _DIFF_HEADER.search(b) or b.lstrip().startswith(("--- ", "diff "))]
    changes.diff = "\n".join(p.rstrip("\n") + "\n" for p in diff_parts)
    for m in _FILE_BLOCK.finditer(text):
        changes.files[m.group(1)] = m.group(2)
    summary = _DIFF_BLOCK.sub("", text)
    summary = _FILE_BLOCK.sub("", summary)
    changes.summary = re.sub(r"\n{3,}", "\n\n", summary).strip()
    return changes


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed and not self.errors


def apply_changes(ws: Workspace, changes: AgentChanges) -> ApplyResult:
    res = ApplyResult()
    if changes.diff.strip():
        # Try strict, then 3-way, then fuzzy patch. Per-file granularity via --include.
        files = list(dict.fromkeys(_DIFF_HEADER.findall(changes.diff)))
        targets = [b for _a, b in files] or ["(unknown)"]
        proc = subprocess.run(["git", "apply", "--whitespace=nowarn"], cwd=ws.path, input=changes.diff,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            proc = subprocess.run(["git", "apply", "--3way", "--whitespace=nowarn"], cwd=ws.path,
                                  input=changes.diff, capture_output=True, text=True)
        if proc.returncode != 0 and shutil.which("patch"):
            proc = subprocess.run(["patch", "-p1", "--fuzz=1", "--forward", "--no-backup-if-mismatch"],
                                  cwd=ws.path, input=changes.diff, capture_output=True, text=True)
        if proc.returncode == 0:
            res.applied.extend(targets)
        else:
            res.failed.extend(targets)
            res.errors = (proc.stderr or proc.stdout).strip()[:2000]
            for junk in list(ws.path.rglob("*.rej")) + list(ws.path.rglob("*.orig")):
                if ".git" not in junk.parts and not junk.is_symlink():
                    junk.unlink(missing_ok=True)
    for path, content in changes.files.items():
        if ".." in Path(path).parts or Path(path).is_absolute():
            res.failed.append(path)
            res.errors += f"\nrefused unsafe path: {path}"
            continue
        dst = ws.path / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        res.applied.append(path)
    return res


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@dataclass
class TestRun:
    command: str
    returncode: int
    output: str
    seconds: float

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    def tail(self, lines: int = 40) -> str:
        return "\n".join(self.output.strip().splitlines()[-lines:])


def detect_test_command(root: Path, frameworks: list[str]) -> str | None:
    checks = [
        ("Pest", "vendor/bin/pest", "vendor/bin/pest --colors=never"),
        ("PHPUnit", "vendor/bin/phpunit", "vendor/bin/phpunit --colors=never"),
        ("Vitest", "node_modules/.bin/vitest", "node_modules/.bin/vitest run"),
        ("Jest", "node_modules/.bin/jest", "node_modules/.bin/jest --ci"),
        ("pytest", None, "python3 -m pytest -q -x"),
    ]
    for fw, marker, cmd in checks:
        if fw in frameworks and (marker is None or (root / marker).exists()):
            return cmd
    if any(f in frameworks for f in ("xUnit", "ASP.NET Core", ".NET")) and shutil.which("dotnet"):
        return "dotnet test --nologo -v quiet"
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists():
        return "python3 -m pytest -q -x"
    return None


def run_tests(ws: Workspace, command: str, timeout: int = 600) -> TestRun:
    t0 = time.time()
    try:
        proc = subprocess.run(command, shell=True, cwd=ws.path, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return TestRun(command, proc.returncode, out, time.time() - t0)
    except subprocess.TimeoutExpired as e:
        return TestRun(command, 124, f"timed out after {timeout}s\n{(e.stdout or '')[-2000:]}", time.time() - t0)


def list_workspaces(cfg: Config) -> list[tuple[str, str, str]]:
    """(path, branch, short sha) for every nokshi worktree."""
    out = _git(cfg.root, "worktree", "list", "--porcelain", check=False).stdout
    result, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur.get("branch", "").startswith("refs/heads/nokshi/"):
                result.append((cur["worktree"], cur["branch"].removeprefix("refs/heads/"), cur.get("HEAD", "")[:7]))
            cur = {}
        else:
            k, _, v = line.partition(" ")
            cur[k] = v
    return result


def remove_workspace(cfg: Config, path: str, branch: str) -> None:
    _git(cfg.root, "worktree", "remove", "--force", path, check=False)
    _git(cfg.root, "branch", "-D", branch, check=False)
    shutil.rmtree(path, ignore_errors=True)


def apply_to_working_tree(ws: Workspace) -> tuple[bool, str]:
    """Bring the worktree's changes onto the developer's real working tree."""
    diff = ws.diff()
    if not diff.strip():
        return True, "nothing to apply"
    proc = subprocess.run(["git", "apply", "--whitespace=nowarn"], cwd=ws.root, input=diff,
                          capture_output=True, text=True)
    if proc.returncode != 0:  # fall back to 3-way (stages the result)
        proc = subprocess.run(["git", "apply", "--3way", "--whitespace=nowarn"], cwd=ws.root, input=diff,
                              capture_output=True, text=True)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, f"applied {len(ws.changed_files())} file(s)"
