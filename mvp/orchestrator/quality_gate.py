"""Deterministic Quality Gate Runner: Gate 1 (Static/Security/Anti-Tamper) + Gate 2 (Pytest)."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

from orchestrator.state import GateResult, StaticGateFinding

FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_OS_ATTRS = {"system", "popen"}
SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "SEC-001",
        re.compile(r"""["']sk-[A-Za-z0-9_-]{16,}["']"""),
        "Suspected hardcoded API key detected.",
    ),
    (
        "SEC-002",
        re.compile(r"""(?i)(prod[-_]?db|production\.internal|mysql://.*prod|postgres://.*prod)"""),
        "Suspected production database or endpoint reference detected.",
    ),
    (
        "SEC-003",
        re.compile(r"""(?i)(DROP\s+TABLE|TRUNCATE\s+TABLE|DELETE\s+FROM\s+\w+\s*;)"""),
        "Destructive SQL statement detected.",
    ),
]


def compute_dir_manifest(directory: Path) -> dict[str, str]:
    """Return a deterministic relative_path -> sha256 map for all files in a directory."""
    manifest: dict[str, str] = {}
    if not directory.exists():
        return manifest
    for file_path in sorted(directory.rglob("*")):
        if file_path.is_file() and "__pycache__" not in file_path.parts:
            rel = file_path.relative_to(directory).as_posix()
            manifest[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return manifest


def run_static_and_security_gate(
    src_dir: Path,
    tests_dir: Path,
    expected_test_manifest: dict[str, str],
) -> tuple[bool, bool, list[StaticGateFinding]]:
    """Run Gate 1: anti-tamper test check, AST syntax/safety scan, and secret regex scan."""
    findings: list[StaticGateFinding] = []

    current_manifest = compute_dir_manifest(tests_dir)
    readonly_intact = current_manifest == expected_test_manifest
    if not readonly_intact:
        findings.append(
            StaticGateFinding(
                rule_id="ANTI-TAMPER-001",
                severity="BLOCKER",
                file="tests/",
                line=0,
                message="Read-only acceptance test directory was modified! Agent is forbidden from altering tests.",
            )
        )

    for py_file in sorted(src_dir.rglob("*.py")):
        rel_path = py_file.relative_to(src_dir.parent).as_posix()
        source = py_file.read_text(encoding="utf-8")

        # Regex secret / production access check
        for line_no, line in enumerate(source.splitlines(), start=1):
            for rule_id, pattern, msg in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        StaticGateFinding(
                            rule_id=rule_id,
                            severity="BLOCKER",
                            file=rel_path,
                            line=line_no,
                            message=msg,
                        )
                    )

        # AST syntax & dangerous call check
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError as exc:
            findings.append(
                StaticGateFinding(
                    rule_id="AST-SYNTAX-001",
                    severity="BLOCKER",
                    file=rel_path,
                    line=exc.lineno or 0,
                    message=f"Python syntax error: {exc.msg}",
                )
            )
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_BUILTINS:
                    findings.append(
                        StaticGateFinding(
                            rule_id="SEC-AST-001",
                            severity="BLOCKER",
                            file=rel_path,
                            line=node.lineno,
                            message=f"Forbidden dynamic execution call '{node.func.id}()' is blocked by Gate 1.",
                        )
                    )
                elif isinstance(node.func, ast.Attribute):
                    if (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "os"
                        and node.func.attr in FORBIDDEN_OS_ATTRS
                    ):
                        findings.append(
                            StaticGateFinding(
                                rule_id="SEC-AST-002",
                                severity="BLOCKER",
                                file=rel_path,
                                line=node.lineno,
                                message=f"Forbidden shell execution 'os.{node.func.attr}()' is blocked by Gate 1.",
                            )
                        )
                    if (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                    ):
                        for kw in node.keywords:
                            if (
                                kw.arg == "shell"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is True
                            ):
                                findings.append(
                                    StaticGateFinding(
                                        rule_id="SEC-AST-003",
                                        severity="BLOCKER",
                                        file=rel_path,
                                        line=node.lineno,
                                        message="subprocess with shell=True is blocked by Gate 1.",
                                    )
                                )

    static_passed = not any(f.severity == "BLOCKER" for f in findings)
    return static_passed, readonly_intact, findings


def _parse_pytest_counts(output: str) -> tuple[int, int, int, list[str]]:
    """Extract (total, passed, failed_or_error, failed_test_names) from pytest -v output."""
    failed_names = re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", output, flags=re.MULTILINE)
    passed_count = 0
    failed_count = 0
    error_count = 0

    summary_lines = [ln for ln in output.splitlines() if " in " in ln and ("passed" in ln or "failed" in ln or "error" in ln)]
    if summary_lines:
        last_summary = summary_lines[-1]
        m_pass = re.search(r"(\d+)\s+passed", last_summary)
        m_fail = re.search(r"(\d+)\s+failed", last_summary)
        m_err = re.search(r"(\d+)\s+error", last_summary)
        passed_count = int(m_pass.group(1)) if m_pass else 0
        failed_count = int(m_fail.group(1)) if m_fail else 0
        error_count = int(m_err.group(1)) if m_err else 0

    total_failed = failed_count + error_count
    total = passed_count + total_failed
    return total, passed_count, total_failed, sorted(set(failed_names))


def _build_error_fingerprint(
    static_findings: list[StaticGateFinding],
    failed_names: list[str],
    failure_summary: str,
) -> str:
    """Create a stable hash fingerprint to detect repeated identical failures (stuck loops)."""
    normalized_lines = []
    for f in static_findings:
        normalized_lines.append(f"STATIC:{f.rule_id}:{f.file}:{f.message}")
    for name in failed_names:
        normalized_lines.append(f"TEST:{name}")
    for line in failure_summary.splitlines():
        stripped = line.strip()
        if stripped.startswith(("E ", "AssertionError", "KeyError", "TypeError", "ValueError", "JSONDecodeError")):
            normalized_lines.append(stripped)
    payload = "\n".join(normalized_lines) or failure_summary[:400]
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def run_quality_gates(
    workspace_dir: Path,
    expected_test_manifest: dict[str, str],
    pytest_timeout_sec: int = 30,
) -> GateResult:
    """Execute Gate 1 (Static/Security/Anti-Tamper) and Gate 2 (Isolated Pytest)."""
    src_dir = workspace_dir / "src"
    tests_dir = workspace_dir / "tests"

    static_passed, readonly_intact, findings = run_static_and_security_gate(
        src_dir=src_dir,
        tests_dir=tests_dir,
        expected_test_manifest=expected_test_manifest,
    )

    if not static_passed:
        summary_lines = [
            f"[{f.rule_id}] {f.file}:{f.line} - {f.message}" for f in findings
        ]
        summary = "Gate 1 (Static/Security/Anti-Tamper) BLOCKED:\n" + "\n".join(summary_lines)
        return GateResult(
            passed=False,
            static_passed=False,
            test_passed=False,
            readonly_tests_intact=readonly_intact,
            static_findings=findings,
            failure_summary=summary,
            error_fingerprint=_build_error_fingerprint(findings, [], summary),
            raw_test_output="",
        )

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{src_dir}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(src_dir)
    )
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-v", "--tb=short", "--no-header", str(tests_dir)],
            cwd=str(workspace_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=pytest_timeout_sec,
            encoding="utf-8",
            errors="replace",
        )
        raw_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        test_passed = proc.returncode == 0
    except subprocess.TimeoutExpired as exc:
        raw_output = f"TIMEOUT: pytest exceeded {pytest_timeout_sec}s limit.\n{exc.stdout or ''}"
        test_passed = False

    total, passed, failed, failed_names = _parse_pytest_counts(raw_output)
    if test_passed:
        failure_summary = ""
        fingerprint = ""
    else:
        # Keep failure context compact so the Coder agent receives high-signal tracebacks only.
        marker_idx = raw_output.find("=== FAILURES ===")
        excerpt = raw_output[marker_idx:] if marker_idx != -1 else raw_output
        failure_summary = excerpt.strip()[:2800]
        fingerprint = _build_error_fingerprint([], failed_names, failure_summary)

    return GateResult(
        passed=static_passed and test_passed,
        static_passed=static_passed,
        test_passed=test_passed,
        readonly_tests_intact=readonly_intact,
        static_findings=findings,
        tests_total=total,
        tests_passed=passed,
        tests_failed=failed,
        failed_test_names=failed_names,
        failure_summary=failure_summary,
        error_fingerprint=fingerprint,
        raw_test_output=raw_output.strip(),
    )
