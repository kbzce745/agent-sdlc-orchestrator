"""Isolated Git sandbox for branch-scoped changes, diff capture, and rollback."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitSandbox:
    """Manages an isolated workspace with its own Git repo, feature branch, and rollback."""

    def __init__(self, workspace_dir: Path, branch_name: str) -> None:
        self.workspace_dir = workspace_dir
        self.branch_name = branch_name
        self.baseline_commit: str = ""

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.workspace_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return proc.stdout.strip()

    @classmethod
    def create_from_scenario(
        cls,
        scenario_dir: Path,
        run_dir: Path,
        scenario_name: str,
    ) -> "GitSandbox":
        """Copy scenario files into an isolated run directory and initialize Git tracking."""
        if run_dir.exists():
            shutil.rmtree(run_dir)
        shutil.copytree(
            scenario_dir,
            run_dir,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )

        sandbox = cls(workspace_dir=run_dir, branch_name=f"agent/{scenario_name}")
        (run_dir / ".gitignore").write_text(
            "__pycache__/\n*.py[cod]\n.pytest_cache/\n", encoding="utf-8"
        )
        sandbox._git("init", "-b", "main")
        sandbox._git("config", "user.name", "B11-AI-RD-Agent")
        sandbox._git("config", "user.email", "ai-rd-agent@local.sandbox")
        sandbox._git("add", ".")
        sandbox._git("commit", "-m", f"chore: baseline snapshot for {scenario_name}")
        sandbox.baseline_commit = sandbox._git("rev-parse", "--short", "HEAD")
        sandbox._git("checkout", "-b", sandbox.branch_name)
        return sandbox

    def commit_attempt(self, message: str) -> tuple[str, str]:
        """Stage src/ changes, commit if modified, and return (commit_sha, diff_vs_baseline)."""
        self._git("add", "src")
        status = self._git("status", "--porcelain")
        if status:
            self._git("commit", "-m", message)
        commit_sha = self._git("rev-parse", "--short", "HEAD")
        diff_text = self._git("diff", self.baseline_commit, "HEAD", "--", "src")
        return commit_sha, diff_text

    def rollback_to_baseline(self) -> str:
        """Hard-reset the sandbox branch back to baseline commit and clean untracked files."""
        self._git("reset", "--hard", self.baseline_commit)
        self._git("clean", "-fd")
        return self._git("rev-parse", "--short", "HEAD")
