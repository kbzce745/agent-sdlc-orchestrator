"""Deterministic Finite-State-Machine (FSM) Orchestrator for B11 MVP."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from orchestrator.agents import CoderAgent, ReporterAgent, SpecAnalyzerAgent
from orchestrator.git_sandbox import GitSandbox
from orchestrator.llm_client import LLMClient
from orchestrator.quality_gate import compute_dir_manifest, run_quality_gates
from orchestrator.state import (
    AttemptRecord,
    EscalationTicket,
    LLMUsage,
    PipelineReport,
    PipelineStatus,
)


class PipelineOrchestrator:
    """Runs the closed-loop R&D pipeline with quality gates, circuit breakers, and rollback."""

    def __init__(
        self,
        llm: LLMClient,
        max_retries: int = 2,
        token_budget: int = 60_000,
        pytest_timeout_sec: int = 30,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.llm = llm
        self.max_retries = max_retries
        self.token_budget = token_budget
        self.pytest_timeout_sec = pytest_timeout_sec
        self._logger = logger or (lambda msg: None)
        self.spec_agent = SpecAnalyzerAgent(llm)
        self.coder_agent = CoderAgent(llm)
        self.reporter_agent = ReporterAgent(llm)

    def _log(self, logs: list[str], msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        logs.append(line)
        self._logger(line)

    def run(self, scenario_dir: Path, runs_root: Path) -> PipelineReport:
        """Execute a complete scenario in an isolated Git sandbox and emit auditable artifacts."""
        started_at = datetime.now(timezone.utc)
        scenario_name = scenario_dir.name
        run_id = f"{scenario_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        run_dir = runs_root / run_id
        logs: list[str] = []

        usages: list[LLMUsage] = []
        attempts: list[AttemptRecord] = []
        escalation: EscalationTicket | None = None

        # State: INIT
        self._log(logs, f"STATE={PipelineStatus.INIT.value} | scenario={scenario_name} | provider={self.llm.provider} ({self.llm.model})")
        sandbox = GitSandbox.create_from_scenario(
            scenario_dir=scenario_dir,
            run_dir=run_dir,
            scenario_name=scenario_name,
        )
        tests_dir = run_dir / "tests"
        src_dir = run_dir / "src"
        readonly_manifest = compute_dir_manifest(tests_dir)
        self._log(
            logs,
            f"Git sandbox initialized at {run_dir} (branch={sandbox.branch_name}, baseline={sandbox.baseline_commit}, readonly_test_files={len(readonly_manifest)})",
        )

        # State: ANALYZING_SPEC
        self._log(logs, f"STATE={PipelineStatus.ANALYZING_SPEC.value} | SpecAnalyzerAgent reading requirements.md")
        requirement_text = (run_dir / "requirements.md").read_text(encoding="utf-8")
        spec, spec_usage = self.spec_agent.analyze(requirement_text, scenario=scenario_name)
        usages.append(spec_usage)
        self._log(
            logs,
            f"Spec parsed: '{spec.title}' | target=src/{spec.target_file} | tokens={spec_usage.total_tokens} | cost=¥{spec_usage.estimated_cost_cny:.6f}",
        )

        # Attempt 0: Baseline Gate Check
        self._log(logs, f"STATE={PipelineStatus.GATING.value} | Attempt #0 (baseline_check)")
        baseline_gate = run_quality_gates(
            workspace_dir=run_dir,
            expected_test_manifest=readonly_manifest,
            pytest_timeout_sec=self.pytest_timeout_sec,
        )
        attempts.append(
            AttemptRecord(
                attempt_no=0,
                action="baseline_check",
                agent_note="基线代码门禁巡检（定位初始通过与失败测试）",
                gate_result=baseline_gate,
                git_commit=sandbox.baseline_commit,
                git_diff="",
                llm_usage=None,
            )
        )
        self._log(
            logs,
            f"Attempt #0 GateResult: passed={baseline_gate.passed} | static={baseline_gate.static_passed} | "
            f"tests={baseline_gate.tests_passed}/{baseline_gate.tests_total} passed | "
            f"failed={baseline_gate.failed_test_names} | fingerprint={baseline_gate.error_fingerprint or 'NONE'}",
        )

        current_gate = baseline_gate
        final_commit = sandbox.baseline_commit
        seen_repair_fingerprints: set[str] = set()

        if current_gate.passed:
            final_status = PipelineStatus.AWAITING_HUMAN_REVIEW
        else:
            final_status = PipelineStatus.ESCALATED_TO_HUMAN
            for retry_idx in range(1, self.max_retries + 1):
                total_tokens_so_far = sum(u.total_tokens for u in usages)
                if total_tokens_so_far >= self.token_budget:
                    rolled_back = sandbox.rollback_to_baseline()
                    final_commit = rolled_back
                    escalation = EscalationTicket(
                        trigger_rule="TOKEN_BUDGET_EXCEEDED",
                        reason=f"Token consumption ({total_tokens_so_far}) reached budget cap ({self.token_budget}).",
                        rolled_back_to_commit=rolled_back,
                        last_error_fingerprint=current_gate.error_fingerprint,
                        recommended_human_actions=[
                            "检查上下文体量是否过大或需求颗粒度过粗",
                            "拆分任务后重新触发流水线",
                        ],
                    )
                    self._log(logs, f"CIRCUIT_BREAKER TRIPPED: {escalation.trigger_rule} -> rolled back to {rolled_back}")
                    break

                # State: REPAIRING
                self._log(
                    logs,
                    f"STATE={PipelineStatus.REPAIRING.value} | Attempt #{retry_idx}/{self.max_retries} | "
                    f"CoderAgent diagnosing fingerprint={current_gate.error_fingerprint}",
                )
                try:
                    agent_note, _, coder_usage = self.coder_agent.repair_code(
                        spec=spec,
                        requirement_text=requirement_text,
                        src_dir=src_dir,
                        last_gate=current_gate,
                        scenario=scenario_name,
                        attempt_no=retry_idx,
                    )
                    usages.append(coder_usage)
                except Exception as exc:
                    rolled_back = sandbox.rollback_to_baseline()
                    final_commit = rolled_back
                    escalation = EscalationTicket(
                        trigger_rule="CODER_EXECUTION_ERROR",
                        reason=f"CoderAgent failed to produce valid patch: {exc}",
                        rolled_back_to_commit=rolled_back,
                        last_error_fingerprint=current_gate.error_fingerprint,
                        recommended_human_actions=[
                            "检查模型输出格式或网络连通性",
                            "确认需求与源码接口签名一致",
                        ],
                    )
                    self._log(logs, f"CIRCUIT_BREAKER TRIPPED: {escalation.trigger_rule} -> rolled back to {rolled_back}")
                    break

                commit_sha, diff_text = sandbox.commit_attempt(
                    f"fix(agent): attempt #{retry_idx} for {scenario_name}"
                )
                final_commit = commit_sha

                # State: GATING
                self._log(logs, f"STATE={PipelineStatus.GATING.value} | Attempt #{retry_idx} | commit={commit_sha}")
                current_gate = run_quality_gates(
                    workspace_dir=run_dir,
                    expected_test_manifest=readonly_manifest,
                    pytest_timeout_sec=self.pytest_timeout_sec,
                )
                attempts.append(
                    AttemptRecord(
                        attempt_no=retry_idx,
                        action="agent_repair",
                        agent_note=agent_note,
                        gate_result=current_gate,
                        git_commit=commit_sha,
                        git_diff=diff_text,
                        llm_usage=coder_usage,
                    )
                )
                self._log(
                    logs,
                    f"Attempt #{retry_idx} GateResult: passed={current_gate.passed} | static={current_gate.static_passed} | "
                    f"tests={current_gate.tests_passed}/{current_gate.tests_total} passed | "
                    f"failed={current_gate.failed_test_names} | fingerprint={current_gate.error_fingerprint or 'NONE'} | "
                    f"tokens={coder_usage.total_tokens} | cost=¥{coder_usage.estimated_cost_cny:.6f}",
                )

                if not current_gate.readonly_tests_intact:
                    rolled_back = sandbox.rollback_to_baseline()
                    final_commit = rolled_back
                    escalation = EscalationTicket(
                        trigger_rule="READONLY_TEST_TAMPERED",
                        reason="Anti-tamper gate detected modification to read-only tests/ directory.",
                        rolled_back_to_commit=rolled_back,
                        last_error_fingerprint=current_gate.error_fingerprint,
                        recommended_human_actions=[
                            "立即审计 Agent 权限隔离与沙箱配置",
                            "人工接管本次变更",
                        ],
                    )
                    self._log(logs, f"CIRCUIT_BREAKER TRIPPED: {escalation.trigger_rule} -> rolled back to {rolled_back}")
                    break

                if current_gate.passed:
                    final_status = PipelineStatus.AWAITING_HUMAN_REVIEW
                    self._log(logs, f"ALL GATES PASSED on Attempt #{retry_idx}! Transitioning to {final_status.value}.")
                    break

                # Detect stuck loop (same error fingerprint repeating across repair attempts)
                if current_gate.error_fingerprint in seen_repair_fingerprints:
                    rolled_back = sandbox.rollback_to_baseline()
                    final_commit = rolled_back
                    escalation = EscalationTicket(
                        trigger_rule="DUPLICATE_ERROR_FINGERPRINT",
                        reason=(
                            f"Repeated identical failure fingerprint '{current_gate.error_fingerprint}' "
                            f"detected on Attempt #{retry_idx}. Stopping early to prevent token burn and infinite loops."
                        ),
                        rolled_back_to_commit=rolled_back,
                        last_error_fingerprint=current_gate.error_fingerprint,
                        recommended_human_actions=[
                            "核查只读验收测试断言是否与需求文档中的合规红线存在冲突",
                            "由 Tech Lead 明确规格后修订测试或需求，再重新触发流水线",
                        ],
                    )
                    self._log(logs, f"CIRCUIT_BREAKER TRIPPED: {escalation.trigger_rule} -> rolled back to {rolled_back}")
                    break
                seen_repair_fingerprints.add(current_gate.error_fingerprint)

            if final_status != PipelineStatus.AWAITING_HUMAN_REVIEW and escalation is None:
                rolled_back = sandbox.rollback_to_baseline()
                final_commit = rolled_back
                escalation = EscalationTicket(
                    trigger_rule="MAX_RETRIES_EXCEEDED",
                    reason=(
                        f"Quality gates still failing after max_retries={self.max_retries}. "
                        f"Remaining failed tests: {', '.join(current_gate.failed_test_names) or 'Gate 1 static error'}."
                    ),
                    rolled_back_to_commit=rolled_back,
                    last_error_fingerprint=current_gate.error_fingerprint,
                    recommended_human_actions=[
                        "查看最后一次失败回溯与只读测试规格，判断是否为需求歧义或跨模块依赖问题",
                        "由人工工程师接管修复并在确认后走常规 PR Review",
                    ],
                )
                self._log(logs, f"CIRCUIT_BREAKER TRIPPED: {escalation.trigger_rule} -> rolled back to {rolled_back}")

        # Generate Human-in-the-Loop PR Review / Escalation Report
        summary_md, reporter_usage = self.reporter_agent.build_summary(
            scenario=scenario_name,
            spec=spec,
            status=final_status,
            attempts=attempts,
            escalation=escalation,
        )
        usages.append(reporter_usage)

        finished_at = datetime.now(timezone.utc)
        duration_sec = round((finished_at - started_at).total_seconds(), 2)
        total_prompt = sum(u.prompt_tokens for u in usages)
        total_completion = sum(u.completion_tokens for u in usages)
        total_tokens = sum(u.total_tokens for u in usages)
        total_cost = round(sum(u.estimated_cost_cny for u in usages), 6)

        full_report_md = self._render_full_report_markdown(
            run_id=run_id,
            scenario=scenario_name,
            status=final_status,
            branch=sandbox.branch_name,
            baseline_commit=sandbox.baseline_commit,
            final_commit=final_commit,
            duration_sec=duration_sec,
            total_prompt=total_prompt,
            total_completion=total_completion,
            total_tokens=total_tokens,
            total_cost=total_cost,
            attempts=attempts,
            escalation=escalation,
            agent_summary=summary_md,
        )

        report = PipelineReport(
            run_id=run_id,
            scenario=scenario_name,
            provider=self.llm.provider,
            model=self.llm.model,
            started_at=started_at,
            finished_at=finished_at,
            duration_sec=duration_sec,
            status=final_status,
            workspace_dir=str(run_dir),
            git_branch=sandbox.branch_name,
            baseline_commit=sandbox.baseline_commit,
            final_commit=final_commit,
            spec=spec,
            attempts=attempts,
            escalation=escalation,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_tokens=total_tokens,
            total_cost_cny=total_cost,
            review_markdown=full_report_md,
        )

        (run_dir / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        (run_dir / "review_report.md").write_text(full_report_md, encoding="utf-8")
        (run_dir / "pipeline.log").write_text("\n".join(logs) + "\n", encoding="utf-8")

        self._log(
            logs,
            f"FINISHED | status={final_status.value} | duration={duration_sec}s | "
            f"total_tokens={total_tokens} | total_cost=¥{total_cost:.6f} | report={run_dir / 'review_report.md'}",
        )
        (run_dir / "pipeline.log").write_text("\n".join(logs) + "\n", encoding="utf-8")
        return report

    def _render_full_report_markdown(
        self,
        run_id: str,
        scenario: str,
        status: PipelineStatus,
        branch: str,
        baseline_commit: str,
        final_commit: str,
        duration_sec: float,
        total_prompt: int,
        total_completion: int,
        total_tokens: int,
        total_cost: float,
        attempts: list[AttemptRecord],
        escalation: EscalationTicket | None,
        agent_summary: str,
    ) -> str:
        lines = [
            f"# Agent 研发流水线交付报告 · `{scenario}`",
            "",
            f"- **Run ID:** `{run_id}`",
            f"- **最终状态:** `{status.value}`",
            f"- **模型后端:** `{self.llm.provider}` / `{self.llm.model}`",
            f"- **Git 特性分支:** `{branch}`（基线 `{baseline_commit}` → 当前 `{final_commit}`）",
            f"- **总耗时:** {duration_sec:.2f}s",
            f"- **Token 与成本统计:** prompt={total_prompt}, completion={total_completion}, "
            f"total={total_tokens}, 估算费用=¥{total_cost:.6f}",
            "",
            "## 质量门禁轨迹",
            "",
            "| 轮次 | 动作 | Gate 1 静态/安全/防作弊 | Gate 2 只读测试 | 失败指纹 | Commit |",
            "| :---: | :--- | :---: | :---: | :--- | :--- |",
        ]
        for att in attempts:
            g = att.gate_result
            g1 = "PASS" if g.static_passed else "BLOCK"
            g2 = f"{g.tests_passed}/{g.tests_total} PASS" if g.test_passed else f"{g.tests_passed}/{g.tests_total} FAIL"
            fp = f"`{g.error_fingerprint}`" if g.error_fingerprint else "-"
            lines.append(
                f"| #{att.attempt_no} | `{att.action}` | {g1} | {g2} | {fp} | `{att.git_commit}` |"
            )

        if escalation:
            lines.extend(
                [
                    "",
                    "## 熔断与人工接管故障单（Escalation Ticket）",
                    "",
                    f"- **触发规则:** `{escalation.trigger_rule}`",
                    f"- **原因说明:** {escalation.reason}",
                    f"- **自动回滚快照:** `{escalation.rolled_back_to_commit}`",
                    f"- **最后错误指纹:** `{escalation.last_error_fingerprint}`",
                    "- **建议人工介入动作:**",
                ]
            )
            for item in escalation.recommended_human_actions:
                lines.append(f"  - {item}")

        lines.extend(["", agent_summary])

        last_diff = next((a.git_diff for a in reversed(attempts) if a.git_diff), "")
        if last_diff and status == PipelineStatus.AWAITING_HUMAN_REVIEW:
            lines.extend(["", "## Git Diff（供人工 Review）", "", "```diff", last_diff, "```"])

        return "\n".join(lines) + "\n"
