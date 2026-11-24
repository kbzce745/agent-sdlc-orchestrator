"""Read-only acceptance tests for Scenario 1 (repair_demo).

Baseline run expects:
- 2 baseline aggregation tests PASS
- 3 privacy/compliance & fault-tolerance tests FAIL (triggering CoderAgent repair)
"""

from __future__ import annotations

import json

from race_media_inspector import (
    inspect_jsonl_lines,
    mask_sensitive_text,
    render_markdown_report,
)


def test_baseline_photographer_and_checkpoint_aggregation_passes() -> None:
    """Baseline passing test: normal upload logs aggregate accurately."""
    lines = [
        json.dumps(
            {
                "photo_id": "BJ2026-0001",
                "photographer_id": "PG-01",
                "checkpoint": "KM21_HALF",
                "upload_status": "success",
                "upload_ms": 420,
                "bib_number": "A1024",
                "ocr_confidence": 0.96,
                "opt_out": False,
            }
        ),
        json.dumps(
            {
                "photo_id": "BJ2026-0002",
                "photographer_id": "PG-01",
                "checkpoint": "KM21_HALF",
                "upload_status": "failed",
                "upload_ms": 1580,
                "bib_number": "",
                "ocr_confidence": 0.0,
                "opt_out": False,
            }
        ),
    ]
    res = inspect_jsonl_lines(lines)
    assert res["total_valid_records"] == 2
    assert res["upload_success_count"] == 1
    assert res["upload_failed_count"] == 1
    assert res["overall_success_rate"] == 0.5
    assert res["photographers"]["PG-01"]["avg_upload_ms"] == 1000.0
    assert res["checkpoints"]["KM21_HALF"]["success_rate"] == 0.5


def test_baseline_high_confidence_records_not_in_review_queue_passes() -> None:
    """Baseline passing test: high-confidence OCR photos do not clutter manual review."""
    lines = [
        json.dumps(
            {
                "photo_id": "BJ2026-0003",
                "photographer_id": "PG-02",
                "checkpoint": "FINISH_LINE",
                "upload_status": "success",
                "upload_ms": 300,
                "bib_number": "B2048",
                "ocr_confidence": 0.91,
                "opt_out": False,
            }
        )
    ]
    res = inspect_jsonl_lines(lines)
    assert res["low_confidence_queue"] == []


def test_compliance_opt_out_portrait_records_must_be_filtered() -> None:
    """Initially failing test: runner opt_out=True portrait withdrawal must be excluded."""
    lines = [
        json.dumps(
            {
                "photo_id": "BJ2026-OPT-1",
                "photographer_id": "PG-03",
                "checkpoint": "KM35_BRIDGE",
                "upload_status": "success",
                "upload_ms": 500,
                "bib_number": "C3001",
                "ocr_confidence": 0.45,
                "opt_out": True,
                "contact_phone": "13900112233",
            }
        ),
        json.dumps(
            {
                "photo_id": "BJ2026-OK-2",
                "photographer_id": "PG-03",
                "checkpoint": "KM35_BRIDGE",
                "upload_status": "success",
                "upload_ms": 600,
                "bib_number": "C3002",
                "ocr_confidence": 0.92,
                "opt_out": False,
            }
        ),
    ]
    res = inspect_jsonl_lines(lines)
    assert res["opt_out_filtered"] == 1
    assert res["total_valid_records"] == 1
    assert all(item["photo_id"] != "BJ2026-OPT-1" for item in res["low_confidence_queue"])


def test_compliance_phone_and_signed_url_token_must_be_masked() -> None:
    """Initially failing test: mobile numbers and signed URL tokens must never leak."""
    raw_url = "https://cdn.iyundong-demo.cn/raw/BJ2026-009.jpg?token=sec998877&sig=abc123"
    assert mask_sensitive_text("选手电话 13812345678") == "选手电话 138****5678"
    assert "sec998877" not in mask_sensitive_text(raw_url)
    assert "abc123" not in mask_sensitive_text(raw_url)

    lines = [
        json.dumps(
            {
                "photo_id": "BJ2026-009",
                "photographer_id": "PG-05",
                "checkpoint": "KM40_SPRINT",
                "upload_status": "success",
                "upload_ms": 480,
                "bib_number": "D8899",
                "ocr_confidence": 0.62,
                "opt_out": False,
                "contact_phone": "13812345678",
                "preview_url": raw_url,
            }
        )
    ]
    res = inspect_jsonl_lines(lines)
    assert len(res["low_confidence_queue"]) == 1
    queued = res["low_confidence_queue"][0]
    assert queued["contact"] == "138****5678"
    assert "sec998877" not in queued["preview_url"]
    assert "abc123" not in queued["preview_url"]

    report_md = render_markdown_report(res, event_name="2026 北京马拉松测试赛")
    assert "13812345678" not in report_md
    assert "sec998877" not in report_md
    assert "138****5678" in report_md


def test_fault_tolerance_for_corrupted_and_incomplete_lines() -> None:
    """Initially failing test: malformed JSON or missing required fields should not crash."""
    lines = [
        "{not-valid-json",
        json.dumps({"photo_id": "ONLY-ID-MISSING-FIELDS"}),
        "",
        json.dumps(
            {
                "photo_id": "BJ2026-010",
                "photographer_id": "PG-07",
                "checkpoint": "START_GATE",
                "upload_status": "success",
                "upload_ms": 260,
                "bib_number": "E1001",
                "ocr_confidence": 0.95,
                "opt_out": False,
            }
        ),
    ]
    res = inspect_jsonl_lines(lines)
    assert res["malformed_lines"] == 2
    assert res["total_valid_records"] == 1
