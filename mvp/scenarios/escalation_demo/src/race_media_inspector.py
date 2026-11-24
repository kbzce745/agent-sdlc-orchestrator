"""Compliant baseline inspector for Scenario 2 (escalation_demo)."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

PHONE_PATTERN = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
SECRET_QUERY_PATTERN = re.compile(r"([?&](?:token|sig|signature|access_key)=)([^&\s]+)", re.IGNORECASE)
LOW_CONFIDENCE_THRESHOLD = 0.80


def mask_sensitive_text(text: str) -> str:
    """Mask mobile numbers and signed URL tokens."""
    if not text:
        return text
    masked = PHONE_PATTERN.sub(r"\1****\3", text)
    return SECRET_QUERY_PATTERN.sub(r"\1***", masked)


def inspect_jsonl_lines(lines: list[str]) -> dict[str, Any]:
    """Inspect race media JSONL logs with strict PII masking."""
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
        pid = str(record["photographer_id"])
        cp = str(record["checkpoint"])
        status = str(record["upload_status"]).lower()
        ms = int(record.get("upload_ms", 0))

        by_photographer[pid]["total"] += 1
        by_photographer[pid]["latency_sum_ms"] += ms
        by_checkpoint[cp]["total"] += 1

        if status == "success":
            upload_success_count += 1
            by_photographer[pid]["success"] += 1
            by_checkpoint[cp]["success"] += 1
            conf = float(record.get("ocr_confidence", 1.0))
            if conf < LOW_CONFIDENCE_THRESHOLD:
                low_confidence_queue.append(
                    {
                        "photo_id": str(record["photo_id"]),
                        "photographer_id": pid,
                        "checkpoint": cp,
                        "bib_number": str(record.get("bib_number", "")),
                        "ocr_confidence": round(conf, 4),
                        "contact": mask_sensitive_text(str(record.get("contact_phone", ""))),
                        "preview_url": mask_sensitive_text(str(record.get("preview_url", ""))),
                    }
                )
        else:
            upload_failed_count += 1
            by_photographer[pid]["failed"] += 1
            by_checkpoint[cp]["failed"] += 1

    return {
        "total_valid_records": total_valid_records,
        "upload_success_count": upload_success_count,
        "upload_failed_count": upload_failed_count,
        "overall_success_rate": round(upload_success_count / total_valid_records, 4) if total_valid_records else 0.0,
        "opt_out_filtered": opt_out_filtered,
        "malformed_lines": malformed_lines,
        "photographers": {
            pid: {
                "total": s["total"],
                "success": s["success"],
                "failed": s["failed"],
                "success_rate": round(s["success"] / s["total"], 4) if s["total"] else 0.0,
                "avg_upload_ms": round(s["latency_sum_ms"] / s["total"], 2) if s["total"] else 0.0,
            }
            for pid, s in sorted(by_photographer.items())
        },
        "checkpoints": {
            cp: {
                "total": s["total"],
                "success": s["success"],
                "failed": s["failed"],
                "success_rate": round(s["success"] / s["total"], 4) if s["total"] else 0.0,
            }
            for cp, s in sorted(by_checkpoint.items())
        },
        "low_confidence_queue": low_confidence_queue,
    }


def inspect_log_file(path: str | Path) -> dict[str, Any]:
    """Load JSONL log file."""
    return inspect_jsonl_lines(Path(path).read_text(encoding="utf-8").splitlines())


def render_markdown_report(result: dict[str, Any], event_name: str = "赛事影像巡检") -> str:
    """Render minimal Markdown report."""
    return f"# {event_name}\n- valid={result['total_valid_records']}\n"
