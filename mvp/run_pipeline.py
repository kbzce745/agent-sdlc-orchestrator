"""One-command CLI entrypoint for the B11 AI R&D Pipeline MVP."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

MVP_ROOT = Path(__file__).resolve().parent
if str(MVP_ROOT) not in sys.path:
    sys.path.insert(0, str(MVP_ROOT))

from orchestrator.engine import PipelineOrchestrator
from orchestrator.llm_client import LLMClient
from orchestrator.state import PipelineReport, PipelineStatus


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="B11 AI R&D Pipeline MVP — Deterministic FSM Orchestrator + Quality Gates"
    )
    parser.add_argument(
        "--scenario",
        choices=["all", "repair_demo", "escalation_demo"],
        default="all",
        help="Scenario to run (default: all).",
    )
    parser.add_argument(
        "--provider",
        choices=["deepseek", "openai", "ollama", "mock"],
        default="deepseek",
        help="LLM backend provider (default: deepseek; use 'mock' for zero-key offline demo).",
    )
    parser.add_argument("--model", default=None, help="Optional model name override.")
    parser.add_argument("--base-url", default=None, help="Optional API base URL override.")
    parser.add_argument("--max-retries", type=int, default=2, help="Max repair retries before circuit breaker.")
    return parser.parse_args()


def print_summary_table(console: Console, reports: list[PipelineReport]) -> None:
    """Render a compact summary table across all executed scenarios."""
    table = Table(title="B11 AI 研发流水线 MVP 执行总览", show_lines=True)
    table.add_column("场景", style="cyan")
    table.add_column("最终状态", style="bold")
    table.add_column("基线测试 → 终态测试")
    table.add_column("修复次数", justify="right")
    table.add_column("总 Tokens", justify="right")
    table.add_column("估算费用 (CNY)", justify="right")
    table.add_column("交付报告路径")

    for rep in reports:
        status_style = "green" if rep.status == PipelineStatus.AWAITING_HUMAN_REVIEW else "yellow"
        base_g = rep.attempts[0].gate_result
        last_g = rep.attempts[-1].gate_result
        test_progress = (
            f"{base_g.tests_passed}/{base_g.tests_total} → {last_g.tests_passed}/{last_g.tests_total}"
        )
        repair_cnt = sum(1 for a in rep.attempts if a.action == "agent_repair")
        table.add_row(
            rep.scenario,
            f"[{status_style}]{rep.status.value}[/{status_style}]",
            test_progress,
            str(repair_cnt),
            str(rep.total_tokens),
            f"¥{rep.total_cost_cny:.6f}",
            str(Path(rep.workspace_dir) / "review_report.md"),
        )
    console.print(table)


def main() -> int:
    """Run requested scenario(s) and display auditable pipeline results."""
    args = parse_args()
    console = Console()
    scenarios_root = MVP_ROOT / "scenarios"
    runs_root = MVP_ROOT / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    llm = LLMClient(provider=args.provider, model=args.model, base_url=args.base_url)
    console.print(
        Panel.fit(
            f"[bold cyan]B11 · AI 研发流水线 MVP[/bold cyan]\n"
            f"业务域：爱云动赛事影像上传与号码布识别巡检工具\n"
            f"模型后端：[bold]{llm.provider}[/bold] ({llm.model}) | max_retries={args.max_retries}",
            title="Pipeline Boot",
        )
    )

    scenario_names = (
        ["repair_demo", "escalation_demo"] if args.scenario == "all" else [args.scenario]
    )
    reports: list[PipelineReport] = []
    for name in scenario_names:
        console.rule(f"[bold blue]Running Scenario: {name}[/bold blue]")
        orchestrator = PipelineOrchestrator(
            llm=llm,
            max_retries=args.max_retries,
            logger=lambda line: console.print(f"  {line}"),
        )
        report = orchestrator.run(scenario_dir=scenarios_root / name, runs_root=runs_root)
        reports.append(report)

    console.print()
    print_summary_table(console, reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
