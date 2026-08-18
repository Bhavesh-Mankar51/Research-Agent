from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.agent.service import ResearchService
from app.db.repository import ResearchRepository
from app.db.session import init_models


async def _run(app_name: str, *, use_db: bool, force_refresh: bool) -> dict[str, Any]:
    repo = None
    if use_db:
        try:
            await init_models()
            repo = ResearchRepository()
        except Exception as exc:
            print(f"! database unavailable ({exc}); continuing without it", file=sys.stderr)
    service = ResearchService(repo=repo)
    return await service.execute(app_name, force_refresh=force_refresh)


def _print_human(result: dict[str, Any]) -> None:
    report = result.get("report")
    if not report:
        print("No report produced.", file=sys.stderr)
        return

    def line(label: str, value: Any) -> None:
        print(f"{label:<18} {value}")

    print(f"\n=== {report['canonical_name']} ===")
    print(report["one_liner"])
    print()
    line("Category", report["category"])
    line("Auth", ", ".join(report["auth_methods"]) or "unknown")
    line("Access", report["access_tier"])
    line("API", f"{', '.join(report['api_styles'])} ({report['api_breadth']})")
    mcp_url = report.get("mcp_url")
    line("MCP", report["mcp_status"] + (f" — {mcp_url}" if mcp_url else ""))
    line("Verdict", report["verdict"])
    if report.get("blocker"):
        line("Blocker", report["blocker"])

    if report.get("unknowns"):
        print("\nUnknowns:")
        for item in report["unknowns"]:
            print(f"  - {item}")

    print("\nEvidence:")
    for item in report.get("evidence", []):
        print(f"  - {item['url']}")

    verification = result.get("verification") or {}
    print(f"\nVerification: {'PASSED' if verification.get('passed') else 'ISSUES FOUND'}")
    for issue in verification.get("issues", []):
        print(f"  [{issue['severity']}] {issue['field']}: {issue['issue']}")
    if report.get("human_review_needed"):
        reason = report.get("human_review_reason") or "see issues above"
        print(f"\n!! Human review needed: {reason}")

    usage = result.get("usage", {})
    tools = result.get("tool_stats", {})
    print(
        f"\n{usage.get('calls', 0)} model calls | "
        f"in {usage.get('input_tokens', 0)} (+{usage.get('cache_read_tokens', 0)} cached) / "
        f"out {usage.get('output_tokens', 0)} tokens | "
        f"${usage.get('cost_usd', 0):.4f} | "
        f"{tools.get('tool_calls', 0)} tool calls ({tools.get('cache_hits', 0)} deduped)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Research one app's integration surface.")
    parser.add_argument("app_name", help="A single app name, e.g. 'Zendesk'.")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a summary.")
    parser.add_argument("--no-db", action="store_true", help="Skip Postgres entirely.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore any cached report.")
    args = parser.parse_args()

    try:
        result = asyncio.run(
            _run(args.app_name, use_db=not args.no_db, force_refresh=args.force_refresh)
        )
    except Exception as exc:
        print(f"Run failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
