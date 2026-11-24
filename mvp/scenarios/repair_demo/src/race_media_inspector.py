"""Baseline v0 implementation of race_media_inspector (intentionally incomplete for repair demo)."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

LOW_CONFIDENCE_THRESHOLD = 0.80


def mask_sensitive_text(text: str) -> str:
    """Placeholder masking function (BUG: does not mask phone numbers or URL tokens yet)."""
    return text


def inspect_jsonl_lines(lines: list[str]) -> dict[str, Any]:
    """Baseline JSONL inspector (BUGS: ignores opt_out, no malformed-line guard, leaks raw PII)."""
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

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        # BUG 1: No try/except for corrupted JSON lines and no required-field check.
        record = json.loads(stripped)

        # BUG 2: Missing opt_out=True portrait withdrawal filter.
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
                # BUG 3: Leaks raw contact_phone and preview_url with signed token.
                low_confidence_queue.append(
                    {
                        "photo_id": str(record["photo_id"]),
                        "photographer_id": photographer_id,
                        "checkpoint": checkpoint,
                        "bib_number": str(record.get("bib_number", "")),
                        "ocr_confidence": round(confidence, 4),
                        "contact": str(record.get("contact_phone", "")),
                        "preview_url": str(record.get("preview_url", "")),
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
    """Render Markdown summary report."""
    lines = [
        f"# {event_name} - 上传与号码布识别巡检报告",
        f"- 有效分析记录数：{result['total_valid_records']}",
        f"- 肖像下架合规拦截（opt_out）：{result['opt_out_filtered']}",
        f"- 脏数据跳过：{result['malformed_lines']}",
    ]
    for item in result["low_confidence_queue"]:
        lines.append(f"- {item['photo_id']} | {item['contact']} | {item['preview_url']}")
    return "\n".join(lines)
