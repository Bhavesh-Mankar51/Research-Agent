from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from app.agent.service import ResearchService
from app.db.repository import ResearchRepository
from app.db.session import init_models


async def _research_one(
    service: ResearchService,
    app_name: str,
    *,
    semaphore: asyncio.Semaphore,
    force_refresh: bool,
    index: int,
    total: int,
) -> dict[str, Any]:
    async with semaphore:
        started = time.monotonic()
        print(f"[{index}/{total}] {app_name} …", file=sys.stderr, flush=True)
        try:
            result = await service.execute(app_name, force_refresh=force_refresh)
        except Exception as exc:
            print(f"[{index}/{total}] {app_name} FAILED: {exc}", file=sys.stderr, flush=True)
            return {"app_name": app_name, "ok": False, "error": str(exc)[:500]}

        report = result.get("report") or {}
        verification = result.get("verification") or {}
        usage = result.get("usage") or {}
        elapsed = time.monotonic() - started
        print(
            f"[{index}/{total}] {app_name} → {report.get('verdict', '?')} / "
            f"{report.get('access_tier', '?')} / mcp={report.get('mcp_status', '?')} "
            f"| verify={'pass' if verification.get('passed') else 'ISSUES'} "
            f"| ${usage.get('cost_usd', 0):.4f} | {elapsed:.0f}s",
            file=sys.stderr,
            flush=True,
        )
        return {"app_name": app_name, "ok": True, "elapsed_s": round(elapsed, 1), **result}


async def run_batch(
    app_names: list[str],
    *,
    concurrency: int = 3,
    force_refresh: bool = False,
    use_db: bool = True,
) -> dict[str, Any]:
    repo = None
    if use_db:
        try:
            await init_models()
            repo = ResearchRepository()
        except Exception as exc:
            print(f"! database unavailable ({exc}); continuing without it", file=sys.stderr)

    service = ResearchService(repo=repo)
    _ = service.provider

    semaphore = asyncio.Semaphore(concurrency)
    total = len(app_names)
    started = time.monotonic()
    results = await asyncio.gather(
        *(
            _research_one(
                service,
                name,
                semaphore=semaphore,
                force_refresh=force_refresh,
                index=i,
                total=total,
            )
            for i, name in enumerate(app_names, start=1)
        )
    )
    elapsed = time.monotonic() - started

    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    cost = sum((r.get("usage") or {}).get("cost_usd", 0.0) for r in ok)
    calls = sum((r.get("usage") or {}).get("calls", 0) for r in ok)
    verified = sum(1 for r in ok if (r.get("verification") or {}).get("passed"))
    flagged = sum(1 for r in ok if (r.get("report") or {}).get("human_review_needed"))

    summary = {
        "requested": total,
        "succeeded": len(ok),
        "failed": len(failed),
        "verification_passed": verified,
        "human_review_needed": flagged,
        "total_cost_usd": round(cost, 4),
        "total_model_calls": calls,
        "wall_clock_s": round(elapsed, 1),
        "concurrency": concurrency,
    }
    print(f"\n{json.dumps(summary, indent=2)}", file=sys.stderr)
    return {"summary": summary, "results": results}


def _load_apps(args: argparse.Namespace) -> list[str]:
    if args.apps:
        lines = Path(args.apps).read_text(encoding="utf-8").splitlines()
        return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    return args.app_names


def main() -> int:
    parser = argparse.ArgumentParser(description="Research many apps' integration surfaces.")
    parser.add_argument("app_names", nargs="*", help="App names, e.g. Zendesk Stripe.")
    parser.add_argument("--apps", help="File with one app name per line ('#' comments allowed).")
    parser.add_argument("--concurrency", type=int, default=3, help="Apps researched in parallel.")
    parser.add_argument("--out", help="Write the full batch result JSON here.")
    parser.add_argument("--no-db", action="store_true", help="Skip Postgres entirely.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore cached reports.")
    args = parser.parse_args()

    app_names = _load_apps(args)
    if not app_names:
        parser.error("no apps given: pass names positionally or use --apps FILE")

    payload = asyncio.run(
        run_batch(
            app_names,
            concurrency=args.concurrency,
            force_refresh=args.force_refresh,
            use_db=not args.no_db,
        )
    )

    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(json.dumps(payload, indent=2))
    return 0 if payload["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
