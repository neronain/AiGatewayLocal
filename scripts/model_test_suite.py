#!/usr/bin/env python3
"""MODEL-001..010 compatibility suite (PRD §18, FR-36) - command line runner.

The tests themselves live in `app.core.modeltest`, which the admin console also
drives, so a terminal run and the console badge can never disagree.

    python scripts/model_test_suite.py --base-url http://localhost:8080 \
        --admin-key edu_sk_... --model coding

    # only the vision cases
    python scripts/model_test_suite.py ... --model vision --only MODEL-006,MODEL-007
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.modeltest import TEST_VERSION, ModelTestSuite, TestResult  # noqa: E402

_LABEL = {"pass": "PASS", "fail": "FAIL", "degraded": "DEGRADED", "not_tested": "SKIP"}


async def _run(args: argparse.Namespace) -> int:
    only = {t.strip().upper() for t in args.only.split(",") if t.strip()} or None
    suite = ModelTestSuite(args.base_url, args.admin_key, args.model, args.timeout)

    print(f"\nModel test suite v{TEST_VERSION} - {args.model} @ {args.base_url}\n")

    async def show(result: TestResult) -> None:
        print(
            f"  {result.test_id} ... {_LABEL.get(result.status, result.status):9s}"
            f"{result.latency_ms:>6d} ms  {result.notes}"
        )

    try:
        results = await suite.run(only=only, progress=show)
        if not args.no_publish:
            await suite.publish(results)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await suite.aclose()

    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    degraded = sum(1 for r in results if r.status == "degraded")
    skipped = sum(1 for r in results if r.status == "not_tested")
    print(f"\n{passed} passed, {failed} failed, {degraded} degraded, {skipped} skipped")

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="EduLLM Gateway model test suite")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--admin-key", required=True)
    parser.add_argument("--model", required=True, help="model alias to test")
    parser.add_argument("--only", default="", help="comma-separated test ids")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--no-publish", action="store_true", help="do not post results to the gateway"
    )
    parser.add_argument("--json", action="store_true", help="also emit JSON to stdout")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
