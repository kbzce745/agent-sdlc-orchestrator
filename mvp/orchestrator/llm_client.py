"""Unified LLM client supporting DeepSeek, OpenAI-compatible APIs, Ollama, and Mock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from orchestrator.state import LLMUsage

# Published DeepSeek official pricing in CNY per 1M tokens (api-docs.deepseek.com).
# Peak hours: Beijing time (UTC+8) Mon-Fri 09:00-12:00 and 14:00-18:00.
# Off-peak hours: 50% discount (all other hours, weekends, and Chinese public holidays).
DEEPSEEK_PRICING_CNY_PER_1M: dict[str, dict[str, dict[str, float]]] = {
    "deepseek-flash": {
        "peak": {"input_hit": 0.04, "input_miss": 2.00, "output": 8.00},
        "off_peak": {"input_hit": 0.02, "input_miss": 1.00, "output": 4.00},
    },
    "deepseek-v4-pro": {
        "peak": {"input_hit": 0.30, "input_miss": 9.00, "output": 27.00},
        "off_peak": {"input_hit": 0.15, "input_miss": 4.50, "output": 13.50},
    },
}


def is_deepseek_peak_hour(now_utc: datetime | None = None) -> bool:
    """Return True if current Beijing time (UTC+8) falls in DeepSeek peak windows."""
    utc_dt = now_utc or datetime.now(timezone.utc)
    bj_dt = utc_dt.astimezone(timezone(timedelta(hours=8)))
    if bj_dt.weekday() >= 5:  # Saturday / Sunday
        return False
    return (9 <= bj_dt.hour < 12) or (14 <= bj_dt.hour < 18)


def load_dotenv(env_path: Path) -> None:
    """Load key=value pairs from a local .env file without overriding existing env vars."""
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def estimate_cost_cny(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int = 0,
) -> float:
    """Estimate call cost in CNY based on official DeepSeek peak/off-peak pricing."""
    if provider in {"ollama", "mock"}:
        return 0.0
    model_key = "deepseek-v4-pro" if "v4-pro" in model.lower() else "deepseek-flash"
    tier = "peak" if is_deepseek_peak_hour() else "off_peak"
    rates = DEEPSEEK_PRICING_CNY_PER_1M[model_key][tier]
    cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)
    cost = (
        (cache_miss_tokens / 1_000_000) * rates["input_miss"]
        + (cache_hit_tokens / 1_000_000) * rates["input_hit"]
        + (completion_tokens / 1_000_000) * rates["output"]
    )
    return round(cost, 6)


class LLMClient:
    """Pluggable LLM client with deterministic mock mode and exact token accounting."""

    def __init__(
        self,
        provider: str = "deepseek",
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_sec: float = 120.0,
    ) -> None:
        self.provider = provider.lower().strip()
        self.timeout_sec = timeout_sec

        mvp_root = Path(__file__).resolve().parent.parent
        load_dotenv(mvp_root / ".env")
        load_dotenv(mvp_root.parent / ".env")

        if self.provider == "deepseek":
            self.base_url = (
                base_url
                or os.environ.get("DEEPSEEK_BASE_URL")
                or "https://api.deepseek.com"
            ).rstrip("/")
            self.model = model or os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash"
            self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
        elif self.provider == "openai":
            self.base_url = (
                base_url
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.openai.com/v1"
            ).rstrip("/")
            self.model = model or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
            self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or ""
        elif self.provider == "ollama":
            self.base_url = (
                base_url
                or os.environ.get("OLLAMA_HOST")
                or "http://localhost:11434"
            ).rstrip("/")
            self.model = model or os.environ.get("OLLAMA_MODEL") or "llama3:latest"
            self.api_key = ""
        elif self.provider == "mock":
            self.base_url = "mock://local"
            self.model = model or "mock-deterministic-v1"
            self.api_key = ""
        else:
            raise ValueError(
                f"Unsupported provider '{provider}'. Choose from: deepseek, openai, ollama, mock."
            )

    def chat(
        self,
        role: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        context_meta: dict[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        """Send a chat completion request and return (content, LLMUsage)."""
        if self.provider == "mock":
            return self._mock_chat(role=role, user_prompt=user_prompt, context_meta=context_meta or {})
        if self.provider in {"deepseek", "openai"}:
            return self._openai_compatible_chat(system_prompt, user_prompt, temperature)
        if self.provider == "ollama":
            return self._ollama_chat(system_prompt, user_prompt, temperature)
        raise ValueError(f"Unsupported provider: {self.provider}")

    def _openai_compatible_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> tuple[str, LLMUsage]:
        if not self.api_key:
            env_hint = "DEEPSEEK_API_KEY" if self.provider == "deepseek" else "OPENAI_API_KEY"
            raise RuntimeError(
                f"Missing API key for provider '{self.provider}'. "
                f"Please set {env_hint} in environment or mvp/.env, or run with --provider mock / ollama."
            )

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        start = time.perf_counter()
        with httpx.Client(timeout=self.timeout_sec) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        latency_ms = int((time.perf_counter() - start) * 1000)

        content = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage", {})
        prompt_tokens = int(usage_raw.get("prompt_tokens", 0))
        completion_tokens = int(usage_raw.get("completion_tokens", 0))
        cache_hit_tokens = int(usage_raw.get("prompt_cache_hit_tokens", 0))
        total_tokens = int(usage_raw.get("total_tokens", prompt_tokens + completion_tokens))

        usage = LLMUsage(
            provider=self.provider,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_hit_tokens=cache_hit_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            estimated_cost_cny=estimate_cost_cny(
                self.provider, self.model, prompt_tokens, completion_tokens, cache_hit_tokens
            ),
        )
        return content, usage

    def _ollama_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> tuple[str, LLMUsage]:
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": temperature},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        start = time.perf_counter()
        with httpx.Client(timeout=self.timeout_sec) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        latency_ms = int((time.perf_counter() - start) * 1000)

        content = data["message"]["content"]
        prompt_tokens = int(data.get("prompt_eval_count", 0))
        completion_tokens = int(data.get("eval_count", 0))
        usage = LLMUsage(
            provider=self.provider,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=latency_ms,
            estimated_cost_cny=0.0,
        )
        return content, usage

    def _mock_chat(
        self,
        role: str,
        user_prompt: str,
        context_meta: dict[str, Any],
    ) -> tuple[str, LLMUsage]:
        """Deterministic offline simulator for reproducible zero-key demonstrations."""
        scenario = context_meta.get("scenario", "")
        attempt_no = int(context_meta.get("attempt_no", 1))
        start = time.perf_counter()

        if role == "spec_analyzer":
            if "escalation" in scenario:
                spec = {
                    "title": "赛事影像上传巡检工具（含合规冲突门禁验证）",
                    "summary": "解析赛事影像上传日志并生成脱敏巡检报告，严禁暴露未脱敏手机号与下架记录。",
                    "target_file": "race_media_inspector.py",
                    "functional_requirements": [
                        "解析 JSONL 上传日志并统计摄影师与点位成功率",
                        "识别低置信度 OCR 记录（< 0.80）进入人工复核清单",
                    ],
                    "compliance_rules": [
                        "强制过滤 opt_out=true 选手肖像下架记录",
                        "严禁在输出中暴露明文手机号与签名 URL Token",
                    ],
                    "acceptance_criteria": [
                        "通过全部只读隔离验收测试与静态安全扫描",
                        "若测试断言与合规红线冲突，不得修改只读测试，需触发熔断转人工裁决",
                    ],
                }
            else:
                spec = {
                    "title": "爱云动赛事影像上传与号码布识别流水线巡检工具",
                    "summary": "解析大型路跑赛事摄影师上传与 OCR 识别日志，输出聚合质量指标、低置信度复核清单与隐私合规摘要。",
                    "target_file": "race_media_inspector.py",
                    "functional_requirements": [
                        "按摄影师与赛道点位聚合总上传量、成功率与平均上传耗时",
                        "筛选 confidence < 0.80 且上传成功的记录进入人工复核清单",
                        "容错跳过损坏或缺少必要字段的脏日志行并统计 malformed_lines",
                        "输出结构化字典与 Markdown 运维巡检报告",
                    ],
                    "compliance_rules": [
                        "彻底过滤 opt_out=true 的选手肖像下架申诉记录，仅累计 opt_out_filtered 计数",
                        "对手机号（11位）执行中间 4 位脱敏（如 138****5678）",
                        "对原图 URL 中的 token/sig 查询参数执行脱敏替换为 ***",
                    ],
                    "acceptance_criteria": [
                        "通过 Gate 1 静态语法/安全扫描与防作弊检查",
                        "通过 Gate 2 全部只读隔离 pytest 验收测试",
                    ],
                }
            content = json.dumps(spec, ensure_ascii=False, indent=2)
        elif role == "coder":
            if "escalation" in scenario:
                # In escalation_demo, the read-only test intentionally conflicts with the compliance spec
                # (demanding raw unmasked phone numbers while spec forbids PII leakage).
                # Coder stays compliant and cannot satisfy the contradictory read-only test.
                content = (
                    "分析：验收测试要求在明细中输出明文手机号，但合规需求明确要求脱敏且禁止绕过隐私保护。\n"
                    "在不违反隐私红线的前提下保持安全脱敏逻辑，仅优化日志解析健壮性。\n"
                    "```python\n"
                    f"{MOCK_REPAIRED_INSPECTOR_CODE}\n"
                    "```\n"
                )
            else:
                if attempt_no >= 1:
                    content = (
                        "根因分析：\n"
                        "1. 基线代码未过滤 opt_out=true 的选手下架申诉记录；\n"
                        "2. 未对手机号和 URL 中的 token/sig 参数执行脱敏；\n"
                        "3. json.loads 遇到损坏行时未捕获异常导致整体崩溃。\n"
                        "修复方案：补充 opt_out 合规拦截、正则脱敏函数及逐行异常容错计数。\n"
                        "```python\n"
                        f"{MOCK_REPAIRED_INSPECTOR_CODE}\n"
                        "```\n"
                    )
                else:
                    content = "```python\n# noop\n```"
        elif role == "reporter":
            status = context_meta.get("status", "AWAITING_HUMAN_REVIEW")
            if status == "ESCALATED_TO_HUMAN":
                content = (
                    "## 人工接管研判摘要（Reporter Agent）\n\n"
                    "- **熔断原因**：连续重试未能通过只读验收测试，且检测到重复错误指纹，判定为需求规范与验收用例存在冲突（非单纯代码 Bug）。\n"
                    "- **安全兜底**：工作区已自动回滚至安全基线提交，未污染主干分支，亦未触碰任何生产环境资源。\n"
                    "- **建议 Tech Lead 核查项**：核对 `tests/test_inspector.py` 中对敏感字段的断言是否违反隐私合规红线，确认后修订需求或测试集再重新触发流水线。\n"
                )
            else:
                content = (
                    "## PR 人工 Review 辅助摘要（Reporter Agent）\n\n"
                    "- **变更概览**：已完成 `race_media_inspector.py` 缺陷修复，补齐选手 `opt_out` 肖像下架过滤、手机号与签名 URL Token 脱敏、损坏日志行容错以及 Markdown 报告脱敏输出。\n"
                    "- **门禁结果**：Gate 1 静态/安全/防作弊扫描通过；Gate 2 只读隔离测试全量通过。\n"
                    "- **重点 Review 建议**：请人工重点核验脱敏正则边界（是否覆盖 `sig=` 与 `token=` 参数）及低置信度阈值 `0.80` 边界条件。\n"
                    "- **发布约束**：当前仅停留在特性分支 `AWAITING_HUMAN_REVIEW` 状态，严禁自动合入主干或发布生产。\n"
                )
        else:
            content = "OK"

        latency_ms = max(12, int((time.perf_counter() - start) * 1000) + 15)
        prompt_tokens = max(80, len(user_prompt) // 3)
        completion_tokens = max(60, len(content) // 3)
        usage = LLMUsage(
            provider="mock",
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=latency_ms,
            estimated_cost_cny=0.0,
        )
        return content, usage


MOCK_REPAIRED_INSPECTOR_CODE = '''"""Race media upload & bib-OCR pipeline inspector for iYunDong operations."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

PHONE_PATTERN = re.compile(r"(?<!\\d)(1[3-9]\\d)(\\d{4})(\\d{4})(?!\\d)")
SECRET_QUERY_PATTERN = re.compile(r"([?&](?:token|sig|signature|access_key)=)([^&\\s]+)", re.IGNORECASE)
LOW_CONFIDENCE_THRESHOLD = 0.80


def mask_sensitive_text(text: str) -> str:
    """Mask mainland China mobile numbers and signed URL secret query parameters."""
    if not text:
        return text
    masked = PHONE_PATTERN.sub(r"\\1****\\3", text)
    masked = SECRET_QUERY_PATTERN.sub(r"\\1***", masked)
    return masked


def inspect_jsonl_lines(lines: list[str]) -> dict[str, Any]:
    """Inspect race upload & OCR JSONL records with privacy filtering and fault tolerance."""
    total_valid_records = 0
    malformed_lines = 0
    opt_out_filtered = 0
    upload_success_count = 0
    upload_failed_count = 0

    by_photographer: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "success": 0, "failed": 0, "latency_sum_ms": 0}
    )
    by_checkpoint: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "success": 0, "failed": 0}
    )
    low_confidence_queue: list[dict[str, Any]] = []

    required_fields = {"photo_id", "photographer_id", "checkpoint", "upload_status", "upload_ms"}

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            malformed_lines += 1
            continue

        if not isinstance(record, dict) or not required_fields.issubset(record.keys()):
            malformed_lines += 1
            continue

        if bool(record.get("opt_out", False)):
            opt_out_filtered += 1
            continue

        total_valid_records += 1
        photographer_id = str(record["photographer_id"])
        checkpoint = str(record["checkpoint"])
        status = str(record["upload_status"]).lower()
        upload_ms = int(record.get("upload_ms", 0))

        pg = by_photographer[photographer_id]
        pg["total"] += 1
        pg["latency_sum_ms"] += upload_ms

        cp = by_checkpoint[checkpoint]
        cp["total"] += 1

        if status == "success":
            upload_success_count += 1
            pg["success"] += 1
            cp["success"] += 1

            confidence = float(record.get("ocr_confidence", 1.0))
            if confidence < LOW_CONFIDENCE_THRESHOLD:
                low_confidence_queue.append(
                    {
                        "photo_id": str(record["photo_id"]),
                        "photographer_id": photographer_id,
                        "checkpoint": checkpoint,
                        "bib_number": str(record.get("bib_number", "")),
                        "ocr_confidence": round(confidence, 4),
                        "contact": mask_sensitive_text(str(record.get("contact_phone", ""))),
                        "preview_url": mask_sensitive_text(str(record.get("preview_url", ""))),
                    }
                )
        else:
            upload_failed_count += 1
            pg["failed"] += 1
            cp["failed"] += 1

    photographer_summary: dict[str, dict[str, Any]] = {}
    for pid, stats in sorted(by_photographer.items()):
        total = stats["total"]
        photographer_summary[pid] = {
            "total": total,
            "success": stats["success"],
            "failed": stats["failed"],
            "success_rate": round(stats["success"] / total, 4) if total else 0.0,
            "avg_upload_ms": round(stats["latency_sum_ms"] / total, 2) if total else 0.0,
        }

    checkpoint_summary: dict[str, dict[str, Any]] = {}
    for cp_name, stats in sorted(by_checkpoint.items()):
        total = stats["total"]
        checkpoint_summary[cp_name] = {
            "total": total,
            "success": stats["success"],
            "failed": stats["failed"],
            "success_rate": round(stats["success"] / total, 4) if total else 0.0,
        }

    overall_success_rate = (
        round(upload_success_count / total_valid_records, 4) if total_valid_records else 0.0
    )

    return {
        "total_valid_records": total_valid_records,
        "upload_success_count": upload_success_count,
        "upload_failed_count": upload_failed_count,
        "overall_success_rate": overall_success_rate,
        "opt_out_filtered": opt_out_filtered,
        "malformed_lines": malformed_lines,
        "photographers": photographer_summary,
        "checkpoints": checkpoint_summary,
        "low_confidence_queue": low_confidence_queue,
    }


def inspect_log_file(path: str | Path) -> dict[str, Any]:
    """Load a JSONL log file from disk and return inspection metrics."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return inspect_jsonl_lines(lines)


def render_markdown_report(result: dict[str, Any], event_name: str = "赛事影像巡检") -> str:
    """Render a masked, operations-ready Markdown summary report."""
    lines = [
        f"# {event_name} - 上传与号码布识别巡检报告",
        "",
        "## 核心汇总",
        f"- 有效分析记录数：{result['total_valid_records']}",
        f"- 上传成功率：{result['overall_success_rate'] * 100:.2f}% "
        f"({result['upload_success_count']}/{result['total_valid_records']})",
        f"- 肖像下架合规拦截（opt_out）：{result['opt_out_filtered']} 条（已彻底剔除明细）",
        f"- 脏数据/格式异常跳过：{result['malformed_lines']} 行",
        f"- 低置信度待人工复核：{len(result['low_confidence_queue'])} 条",
        "",
        "## 摄影师维度统计",
        "| 摄影师 ID | 总数 | 成功 | 失败 | 成功率 | 平均耗时 (ms) |",
        "| :--- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for pid, stats in result["photographers"].items():
        lines.append(
            f"| {pid} | {stats['total']} | {stats['success']} | {stats['failed']} | "
            f"{stats['success_rate'] * 100:.1f}% | {stats['avg_upload_ms']:.1f} |"
        )

    lines.extend(["", "## 低置信度待人工复核清单（已脱敏）"])
    if not result["low_confidence_queue"]:
        lines.append("- 无待复核记录")
    else:
        for item in result["low_confidence_queue"]:
            lines.append(
                f"- `{item['photo_id']}` | 点位 `{item['checkpoint']}` | "
                f"号码布 `{item['bib_number']}` (置信度 {item['ocr_confidence']:.2f}) | "
                f"联系 `{mask_sensitive_text(item['contact'])}` | "
                f"预览 `{mask_sensitive_text(item['preview_url'])}`"
            )
    return "\\n".join(lines)
'''
