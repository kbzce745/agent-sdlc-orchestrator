"""Role-separated Agents: SpecAnalyzer, Coder (src-only), and Reporter."""

from __future__ import annotations

import json
import re
from pathlib import Path

from orchestrator.llm_client import LLMClient
from orchestrator.state import AttemptRecord, EscalationTicket, GateResult, LLMUsage, PipelineStatus, TaskSpec


def _extract_json_block(text: str) -> dict:
    """Extract the first valid JSON object from an LLM response."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in LLM output.")
    return json.loads(candidate[start : end + 1])


def _extract_python_code(text: str) -> tuple[str, str]:
    """Extract (agent_note, python_code) from an LLM response."""
    match = re.search(r"```python\s*(.*?)\s*```", text, flags=re.DOTALL)
    if not match:
        match = re.search(r"```\s*(.*?)\s*```", text, flags=re.DOTALL)
    if not match:
        raise ValueError("CoderAgent output did not contain a fenced Python code block.")
    note = text[: match.start()].strip()
    code = match.group(1).strip() + "\n"
    return note, code


class SpecAnalyzerAgent:
    """Reads requirements.md and produces a structured TaskSpec JSON."""

    SYSTEM_PROMPT = (
        "You are SpecAnalyzerAgent in an enterprise AI R&D team. "
        "Parse the requirement document into a strict JSON object with keys: "
        "title, summary, target_file, functional_requirements (list[str]), "
        "compliance_rules (list[str]), acceptance_criteria (list[str]). "
        "Return JSON only."
    )

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def analyze(self, requirement_text: str, scenario: str) -> tuple[TaskSpec, LLMUsage]:
        """Parse requirement Markdown into a validated TaskSpec."""
        user_prompt = (
            f"请解析以下内部工具需求文档，提取结构化任务规格（target_file 保持为 race_media_inspector.py）：\n\n"
            f"{requirement_text}"
        )
        raw, usage = self.llm.chat(
            role="spec_analyzer",
            system_prompt=self.SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.0,
            context_meta={"scenario": scenario},
        )
        try:
            parsed = _extract_json_block(raw)
            spec = TaskSpec.model_validate(parsed)
        except Exception:
            # Graceful fallback if a lightweight local model returns non-strict JSON.
            spec = TaskSpec(
                title=f"Parsed Spec ({scenario})",
                summary=requirement_text.splitlines()[0].lstrip("# ").strip(),
                target_file="race_media_inspector.py",
                functional_requirements=["满足需求文档中的日志解析、聚合统计与报告输出要求"],
                compliance_rules=["过滤 opt_out=true 记录，脱敏手机号与签名 URL Token"],
                acceptance_criteria=["通过全部静态门禁与只读验收测试"],
            )
        return spec, usage


class CoderAgent:
    """Modifies ONLY workspace/src/<target_file> based on spec and structured gate failures."""

    SYSTEM_PROMPT = (
        "You are CoderAgent in an enterprise AI R&D team. "
        "Rules:\n"
        "1. You can ONLY output the complete updated Python code for the target source file.\n"
        "2. NEVER use eval, exec, os.system, subprocess(shell=True), hardcoded API keys, or production DB URLs.\n"
        "3. Strictly honor ALL privacy/compliance rules in the requirement document (opt_out filtering, phone/token masking). "
        "NEVER violate privacy compliance rules even if a failing test demands raw unmasked PII.\n"
        "4. First briefly explain the root cause in 2-4 Chinese bullet points, then provide the FULL Python module "
        "inside a single ```python ... ``` code block."
    )

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def repair_code(
        self,
        spec: TaskSpec,
        requirement_text: str,
        src_dir: Path,
        last_gate: GateResult,
        scenario: str,
        attempt_no: int,
    ) -> tuple[str, Path, LLMUsage]:
        """Generate repaired source code and write strictly inside src_dir."""
        target_path = (src_dir / spec.target_file).resolve()
        if src_dir.resolve() not in target_path.parents:
            raise PermissionError(f"Path traversal blocked: {target_path} is outside {src_dir}")

        current_code = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
        user_prompt = (
            f"## 需求文档\n{requirement_text}\n\n"
            f"## 结构化任务规格\n{spec.model_dump_json(indent=2)}\n\n"
            f"## 当前源码 (`src/{spec.target_file}`)\n```python\n{current_code}\n```\n\n"
            f"## 质量门禁失败摘要（第 {attempt_no} 次修复）\n"
            f"- 通过测试数：{last_gate.tests_passed} / {last_gate.tests_total}\n"
            f"- 失败用例：{', '.join(last_gate.failed_test_names) or 'N/A'}\n"
            f"- 失败详情：\n```txt\n{last_gate.failure_summary}\n```\n\n"
            f"请输出简短根因说明 + 修复后的完整 `src/{spec.target_file}` 代码（单个 ```python``` 代码块）。"
        )
        raw, usage = self.llm.chat(
            role="coder",
            system_prompt=self.SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.1,
            context_meta={"scenario": scenario, "attempt_no": attempt_no},
        )
        note, code = _extract_python_code(raw)
        target_path.write_text(code, encoding="utf-8")
        return note, target_path, usage


class ReporterAgent:
    """Produces a structured Human-in-the-Loop PR Review or Escalation Markdown report."""

    SYSTEM_PROMPT = (
        "You are ReporterAgent preparing a concise Chinese engineering review summary for a Tech Lead. "
        "Highlight what changed, gate results, compliance checks, and exact items requiring human verification. "
        "Never claim the code is auto-merged or deployed to production."
    )

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def build_summary(
        self,
        scenario: str,
        spec: TaskSpec,
        status: PipelineStatus,
        attempts: list[AttemptRecord],
        escalation: EscalationTicket | None,
    ) -> tuple[str, LLMUsage]:
        """Generate a human-readable review summary section."""
        attempt_briefs = []
        for att in attempts:
            g = att.gate_result
            attempt_briefs.append(
                f"- Attempt #{att.attempt_no} ({att.action}): passed={g.passed}, "
                f"tests={g.tests_passed}/{g.tests_total}, failed={g.failed_test_names}, "
                f"commit={att.git_commit}"
            )
        esc_text = escalation.model_dump_json(indent=2) if escalation else "无（门禁全部通过）"
        user_prompt = (
            f"场景：{scenario}\n"
            f"任务：{spec.title}\n"
            f"最终状态：{status.value}\n"
            f"执行轨迹：\n" + "\n".join(attempt_briefs) + "\n"
            f"熔断信息：{esc_text}\n\n"
            "请输出简洁的 Markdown 审查/接管摘要（包含变更概览、门禁状态、人工重点核查项）。"
        )
        content, usage = self.llm.chat(
            role="reporter",
            system_prompt=self.SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.1,
            context_meta={"scenario": scenario, "status": status.value},
        )
        return content.strip(), usage
