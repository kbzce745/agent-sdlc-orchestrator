"""Unit tests for the B11 MVP orchestrator, quality gates, anti-tamper, and circuit breakers."""

from __future__ import annotations

import sys
from pathlib import Path

MVP_ROOT = Path(__file__).resolve().parent.parent
if str(MVP_ROOT) not in sys.path:
    sys.path.insert(0, str(MVP_ROOT))

from orchestrator.engine import PipelineOrchestrator
from orchestrator.llm_client import LLMClient
from orchestrator.quality_gate import compute_dir_manifest, run_quality_gates
from orchestrator.state import PipelineStatus


def test_gate1_blocks_dangerous_ast_and_hardcoded_secret(tmp_path: Path) -> None:
    """Gate 1 must block eval(), os.system(), and hardcoded API keys."""
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()
    (tests_dir / "test_dummy.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (src_dir / "bad.py").write_text(
        'import os\nAPI_KEY = "sk-1234567890abcdef123456"\nos.system("echo hi")\neval("1+1")\n',
        encoding="utf-8",
    )

    manifest = compute_dir_manifest(tests_dir)
    result = run_quality_gates(tmp_path, expected_test_manifest=manifest)
    rule_ids = {f.rule_id for f in result.static_findings}

    assert not result.passed
    assert not result.static_passed
    assert "SEC-001" in rule_ids
    assert "SEC-AST-001" in rule_ids
    assert "SEC-AST-002" in rule_ids


def test_gate1_anti_tamper_blocks_modified_readonly_tests(tmp_path: Path) -> None:
    """Gate 1 must detect if an agent tampers with the read-only tests/ directory."""
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()
    (src_dir / "ok.py").write_text("x = 1\n", encoding="utf-8")
    test_file = tests_dir / "test_sample.py"
    test_file.write_text("def test_fail():\n    assert 1 == 2\n", encoding="utf-8")

    baseline_manifest = compute_dir_manifest(tests_dir)
    # Simulate an agent cheating by rewriting the test to pass
    test_file.write_text("def test_fail():\n    assert True\n", encoding="utf-8")

    result = run_quality_gates(tmp_path, expected_test_manifest=baseline_manifest)
    assert not result.passed
    assert not result.readonly_tests_intact
    assert any(f.rule_id == "ANTI-TAMPER-001" for f in result.static_findings)


def test_repair_demo_closed_loop_succeeds_and_waits_for_human_review(tmp_path: Path) -> None:
    """Scenario 1: baseline has passing + failing tests; repair fixes all and stops at Human Gate."""
    llm = LLMClient(provider="mock")
    orchestrator = PipelineOrchestrator(llm=llm, max_retries=2)
    report = orchestrator.run(
        scenario_dir=MVP_ROOT / "scenarios" / "repair_demo",
        runs_root=tmp_path / "runs",
    )

    assert report.status == PipelineStatus.AWAITING_HUMAN_REVIEW
    assert report.escalation is None
    # Attempt 0: 2 passed, 3 failed
    assert report.attempts[0].gate_result.tests_passed == 2
    assert report.attempts[0].gate_result.tests_failed == 3
    # Final attempt: 5 passed, 0 failed
    assert report.attempts[-1].gate_result.passed is True
    assert report.attempts[-1].gate_result.tests_passed == 5
    assert (Path(report.workspace_dir) / "review_report.md").exists()


def test_escalation_demo_trips_circuit_breaker_and_rolls_back(tmp_path: Path) -> None:
    """Scenario 2: contradictory read-only test trips circuit breaker, rolls back Git, escalates."""
    llm = LLMClient(provider="mock")
    orchestrator = PipelineOrchestrator(llm=llm, max_retries=2)
    report = orchestrator.run(
        scenario_dir=MVP_ROOT / "scenarios" / "escalation_demo",
        runs_root=tmp_path / "runs",
    )

    assert report.status == PipelineStatus.ESCALATED_TO_HUMAN
    assert report.escalation is not None
    assert report.escalation.trigger_rule in {"DUPLICATE_ERROR_FINGERPRINT", "MAX_RETRIES_EXCEEDED"}
    assert report.final_commit == report.baseline_commit
    assert report.escalation.rolled_back_to_commit == report.baseline_commit
