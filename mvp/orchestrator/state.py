"""Pydantic state models for the deterministic FSM R&D pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class PipelineStatus(str, Enum):
    """Finite-state-machine states of the Agent R&D pipeline."""

    INIT = "INIT"
    ANALYZING_SPEC = "ANALYZING_SPEC"
    GATING = "GATING"
    REPAIRING = "REPAIRING"
    AWAITING_HUMAN_REVIEW = "AWAITING_HUMAN_REVIEW"
    ESCALATED_TO_HUMAN = "ESCALATED_TO_HUMAN"


class LLMUsage(BaseModel):
    """Token, latency, and estimated CNY cost record for a single LLM call."""

    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    estimated_cost_cny: float = 0.0


class TaskSpec(BaseModel):
    """Structured task specification extracted from a requirement document."""

    title: str
    summary: str
    target_file: str = "race_media_inspector.py"
    functional_requirements: list[str] = Field(default_factory=list)
    compliance_rules: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)


class StaticGateFinding(BaseModel):
    """Single static analysis, security, or anti-tampering finding."""

    rule_id: str
    severity: Literal["BLOCKER", "WARNING"] = "BLOCKER"
    file: str
    line: int = 0
    message: str


class GateResult(BaseModel):
    """Combined outcome of Gate 1 (static/security) and Gate 2 (isolated pytest)."""

    passed: bool
    static_passed: bool
    test_passed: bool
    readonly_tests_intact: bool = True
    static_findings: list[StaticGateFinding] = Field(default_factory=list)
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    failed_test_names: list[str] = Field(default_factory=list)
    failure_summary: str = ""
    error_fingerprint: str = ""
    raw_test_output: str = ""


class AttemptRecord(BaseModel):
    """Audit record for one pipeline attempt (baseline check or agent repair)."""

    attempt_no: int
    action: Literal["baseline_check", "agent_repair"]
    agent_note: str = ""
    gate_result: GateResult
    git_commit: str = ""
    git_diff: str = ""
    llm_usage: LLMUsage | None = None


class EscalationTicket(BaseModel):
    """Structured handoff ticket generated when the circuit breaker trips."""

    trigger_rule: str
    reason: str
    rolled_back_to_commit: str
    last_error_fingerprint: str
    recommended_human_actions: list[str] = Field(default_factory=list)


class PipelineReport(BaseModel):
    """Complete auditable execution report of a pipeline run."""

    run_id: str
    scenario: str
    provider: str
    model: str
    started_at: datetime
    finished_at: datetime
    duration_sec: float
    status: PipelineStatus
    workspace_dir: str
    git_branch: str
    baseline_commit: str
    final_commit: str
    spec: TaskSpec
    attempts: list[AttemptRecord] = Field(default_factory=list)
    escalation: EscalationTicket | None = None
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_cny: float = 0.0
    review_markdown: str = ""
