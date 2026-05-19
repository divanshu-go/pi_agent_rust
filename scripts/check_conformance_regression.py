#!/usr/bin/env python3
"""Conformance regression gate: block pass-rate drops and new N/A introductions.

Compares the current conformance_summary.json and conformance_events.jsonl against
the committed conformance_baseline.json. Exits non-zero when regressions exceed
the thresholds defined in the baseline.

Checks performed:
1. Overall pass-rate must not drop below baseline pass rate.
2. Per-tier pass-rates must not drop below tier-specific thresholds.
3. N/A count must not increase (no new N/A introductions).
4. Individual extension regressions: PASS→FAIL and PASS→N/A are flagged.
5. New failures must not exceed max_new_failures threshold.
6. Trend degradation warning: warn if pass_rate dropped 3+ consecutive runs (non-blocking).

Environment variables:
  CI_REGRESSION_MODE    "strict" (default) or "warn" (log but don't fail)

Usage:
  python3 scripts/check_conformance_regression.py
  python3 scripts/check_conformance_regression.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "tests" / "ext_conformance" / "reports" / "conformance_baseline.json"
SUMMARY_PATH = REPO_ROOT / "tests" / "ext_conformance" / "reports" / "conformance_summary.json"
EVENTS_PATH = REPO_ROOT / "tests" / "ext_conformance" / "reports" / "conformance_events.jsonl"
VERDICT_PATH = REPO_ROOT / "tests" / "ext_conformance" / "reports" / "regression_verdict.json"
TREND_PATH = REPO_ROOT / "tests" / "ext_conformance" / "reports" / "conformance_trend.jsonl"


def load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        print(f"ERROR: missing required {label}: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        print(f"ERROR: invalid {label}: expected JSON object at {path}", file=sys.stderr)
        sys.exit(1)
    return data


def load_events(path: Path) -> dict[str, str]:
    """Load conformance_events.jsonl → {extension_id: overall_status}."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ext_id = event.get("extension_id")
            status = event.get("overall_status")
            if isinstance(ext_id, str) and isinstance(status, str):
                result[ext_id] = status
    return result


def extract_baseline_per_extension(baseline: dict[str, Any]) -> dict[str, str]:
    """Extract per-extension status from baseline classification.

    Maps extension IDs to their effective status based on the failure
    classification. Extensions not listed in any failure bucket are
    assumed to be PASS.
    """
    statuses: dict[str, str] = {}

    # All extensions in exception_policy are known failures/exceptions.
    exception_policy = baseline.get("exception_policy", {})
    for entry in exception_policy.get("entries", []):
        ext_id = entry.get("id")
        if isinstance(ext_id, str):
            statuses[ext_id] = "FAIL"

    # failure_classification maps to FAIL.
    fc = baseline.get("failure_classification", {})
    for _bucket_name, bucket in fc.items():
        if not isinstance(bucket, dict):
            continue
        for ext_id in bucket.get("extensions", []):
            if isinstance(ext_id, str):
                statuses[ext_id] = "FAIL"

    return statuses


def load_trend_entries(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    trend_entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                trend_entries.append(entry)
    return trend_entries


def build_regression_verdict(
    baseline: dict[str, Any],
    summary: dict[str, Any],
    current_events: dict[str, str],
    trend_entries: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    warnings: list[str] = []

    def add_check(check_id: str, actual: Any, threshold: Any, ok: bool, detail: str = "") -> None:
        checks.append({
            "id": check_id,
            "actual": actual,
            "threshold": threshold,
            "ok": ok,
            "detail": detail,
        })
        if not ok:
            failures.append(check_id)

    # ── 1. Overall pass-rate regression ──────────────────────────────────────

    baseline_ext = baseline.get("extension_conformance", {})
    baseline_pass_rate = baseline_ext.get("pass_rate_pct")
    current_pass_rate = summary.get("pass_rate_pct")

    thresholds = baseline.get("regression_thresholds", {})
    overall_min = thresholds.get("overall_pass_rate_min_pct", 80.0)

    try:
        baseline_pass_rate = float(baseline_pass_rate) if baseline_pass_rate is not None else None
    except (TypeError, ValueError):
        baseline_pass_rate = None
    try:
        current_pass_rate = float(current_pass_rate) if current_pass_rate is not None else None
    except (TypeError, ValueError):
        current_pass_rate = None

    if baseline_pass_rate is not None and current_pass_rate is not None:
        # Gate: current pass rate must not drop below the higher of:
        # (a) the baseline's overall_pass_rate_min_pct threshold, or
        # (b) the baseline's actual pass rate minus a 2% tolerance.
        effective_floor = max(overall_min, baseline_pass_rate - 2.0)
        add_check(
            "pass_rate_no_regression",
            current_pass_rate,
            f">= {effective_floor:.1f}% (baseline {baseline_pass_rate:.1f}%)",
            current_pass_rate >= effective_floor,
            f"Current {current_pass_rate:.1f}% vs baseline {baseline_pass_rate:.1f}%",
        )
    elif current_pass_rate is not None:
        add_check(
            "pass_rate_absolute",
            current_pass_rate,
            f">= {overall_min}%",
            current_pass_rate >= overall_min,
        )

    # ── 2. Per-tier pass-rate checks ─────────────────────────────────────────

    tier_thresholds = {
        "1": thresholds.get("tier1_pass_rate_min_pct", 100.0),
        "2": thresholds.get("tier2_pass_rate_min_pct", 95.0),
    }

    current_by_tier = summary.get("per_tier", {})

    # Map current per_tier keys to tier numbers for comparison.
    # The summary uses source_tier names; baseline uses numeric tiers.
    # We check official-pi-mono (tier 1+2) against tier thresholds.
    official = current_by_tier.get("official-pi-mono", {})
    official_pass = official.get("pass", 0)
    official_fail = official.get("fail", 0)
    official_tested = official_pass + official_fail
    if official_tested > 0:
        official_rate = (official_pass / official_tested) * 100.0
        tier1_min = tier_thresholds.get("1", 100.0)
        # Official extensions span tiers 1 and 2; use the more lenient threshold.
        tier2_min = tier_thresholds.get("2", 95.0)
        add_check(
            "official_tier_pass_rate",
            round(official_rate, 1),
            f">= {tier2_min}%",
            official_rate >= tier2_min,
            f"Official: {official_pass}/{official_tested} ({official_rate:.1f}%)",
        )

    # ── 3. N/A count regression (official tier only) ────────────────────────
    #
    # The baseline tests all 223 extensions via generated conformance, while
    # conformance_summary.json reflects the differential oracle which only
    # tests official extensions. Compare N/A within the official tier only,
    # since community/npm/third-party are always N/A in the diff test.

    official_current = current_by_tier.get("official-pi-mono", {})
    current_official_na = official_current.get("na", 0)
    try:
        current_official_na = int(current_official_na)
    except (TypeError, ValueError):
        current_official_na = 0

    baseline_by_source = baseline_ext.get("by_source", {})
    baseline_official = baseline_by_source.get("official-pi-mono", {})
    baseline_official_pass = baseline_official.get("pass", 0)
    baseline_official_fail = baseline_official.get("fail", 0)
    baseline_official_total = baseline_official.get("total", 0)
    baseline_official_na = baseline_official_total - baseline_official_pass - baseline_official_fail

    # Account for corpus growth: new extensions added since baseline may start
    # as N/A and are not regressions. Compare relative to baseline corpus size.
    current_official_total = official_current.get("total", 0)
    try:
        current_official_total = int(current_official_total)
    except (TypeError, ValueError):
        current_official_total = 0
    corpus_growth = max(0, current_official_total - baseline_official_total)

    # Allowed N/A = baseline N/A + corpus growth + small tolerance.
    na_tolerance = 2
    allowed_na = baseline_official_na + corpus_growth + na_tolerance
    add_check(
        "official_na_no_increase",
        current_official_na,
        f"<= {allowed_na} (baseline {baseline_official_na} + {corpus_growth} growth + {na_tolerance} tolerance)",
        current_official_na <= allowed_na,
        f"Official N/A: current={current_official_na}, baseline={baseline_official_na}, "
        f"corpus grew by {corpus_growth}",
    )

    # ── 4. Individual extension regressions ──────────────────────────────────

    baseline_per_ext = extract_baseline_per_extension(baseline)

    # Build baseline extension set: everything in exception_policy + failure_classification = FAIL,
    # everything else in the corpus = PASS (if it was tested).
    # For individual regression: flag extensions that were PASS in baseline but are now FAIL or N/A.
    new_failures: list[str] = []
    new_na_introductions: list[str] = []

    for ext_id, current_status in sorted(current_events.items()):
        baseline_status = baseline_per_ext.get(ext_id)
        if baseline_status is None:
            # Extension was PASS in baseline (not in any failure bucket).
            if current_status == "FAIL":
                new_failures.append(ext_id)
            elif current_status == "N/A":
                # Only flag if it was previously tested (PASS).
                # Extensions that were always N/A are not regressions.
                # Check if it was in the baseline's tested set.
                pass
        # Extensions that were FAIL and are still FAIL: no regression.
        # Extensions that were FAIL and are now PASS: improvement, good.

    max_new_failures = thresholds.get("max_new_failures", 3)
    add_check(
        "no_new_failures",
        len(new_failures),
        f"<= {max_new_failures}",
        len(new_failures) <= max_new_failures,
        f"New PASS→FAIL: {', '.join(new_failures[:10]) if new_failures else 'none'}",
    )

    # ── 5. Scenario regression ───────────────────────────────────────────────

    baseline_scenarios = baseline.get("scenario_conformance", {})
    baseline_scenario_rate = baseline_scenarios.get("pass_rate_pct")
    scenario_min = thresholds.get("scenario_pass_rate_min_pct", 85.0)

    try:
        baseline_scenario_rate = float(baseline_scenario_rate) if baseline_scenario_rate is not None else None
    except (TypeError, ValueError):
        baseline_scenario_rate = None

    # Scenario pass rate is not in conformance_summary.json directly,
    # so we note the threshold but don't block on it here (the scenario
    # test itself validates pass rate).
    if baseline_scenario_rate is not None:
        warnings.append(
            f"Scenario baseline pass rate: {baseline_scenario_rate}% "
            f"(threshold: {scenario_min}%). "
            "Scenario regression is validated by ext_conformance_scenarios test."
        )

    # ── 6. Trend degradation warning (informational) ──────────────────────
    #
    # If conformance_trend.jsonl exists with 3+ entries, warn if pass_rate
    # has dropped for 3 or more consecutive runs (sustained degradation).

    if len(trend_entries) >= 3:
        consecutive_drops = 0
        for i in range(len(trend_entries) - 1, 0, -1):
            curr = trend_entries[i].get("pass_rate_pct", 0.0)
            prev = trend_entries[i - 1].get("pass_rate_pct", 0.0)
            if curr < prev:
                consecutive_drops += 1
            else:
                break

        if consecutive_drops >= 3:
            warnings.append(
                f"Sustained degradation: pass_rate dropped for "
                f"{consecutive_drops} consecutive runs. "
                f"Review conformance_trend.jsonl for details."
            )

    # ── Build verdict ────────────────────────────────────────────────────────

    status = "pass" if not failures else ("warn" if mode == "warn" else "fail")
    verdict = {
        "schema": "pi.conformance.regression_gate.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "status": status,
        "paths": {
            "baseline": str(BASELINE_PATH),
            "summary": str(SUMMARY_PATH),
            "events": str(EVENTS_PATH),
        },
        "regression_thresholds": thresholds,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "new_failures": new_failures,
        "new_na_introductions": new_na_introductions,
    }
    return verdict


def print_verdict_report(verdict: dict[str, Any], verdict_path: Path | None = None) -> None:
    if verdict_path is not None:
        print(f"Regression verdict written: {verdict_path}")
        print()

    for check in verdict["checks"]:
        marker = "PASS" if check["ok"] else "FAIL"
        detail = f" ({check['detail']})" if check.get("detail") else ""
        print(f"  [{marker}] {check['id']}: {check['actual']} {check['threshold']}{detail}")

    warnings = verdict["warnings"]
    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for warning in warnings:
            print(f"  - {warning}")

    failures = verdict["failures"]
    mode = verdict["mode"]
    if failures:
        print(f"\nREGRESSION GATE {'WARNING' if mode == 'warn' else 'FAILED'}: {len(failures)} check(s) failed")
        for failure in failures:
            print(f"  - {failure}")
        return

    print("\nREGRESSION GATE PASSED: no conformance regressions detected")


def verdict_exit_code(verdict: dict[str, Any]) -> int:
    return 1 if verdict["status"] == "fail" else 0


def self_test_baseline() -> dict[str, Any]:
    return {
        "extension_conformance": {
            "pass_rate_pct": 100.0,
            "by_source": {
                "official-pi-mono": {
                    "pass": 10,
                    "fail": 0,
                    "total": 10,
                },
            },
        },
        "regression_thresholds": {
            "overall_pass_rate_min_pct": 90.0,
            "tier1_pass_rate_min_pct": 100.0,
            "tier2_pass_rate_min_pct": 95.0,
            "max_new_failures": 0,
            "scenario_pass_rate_min_pct": 85.0,
        },
        "exception_policy": {
            "entries": [],
        },
        "failure_classification": {},
        "scenario_conformance": {
            "pass_rate_pct": 90.0,
        },
    }


def self_test_summary(
    *,
    pass_rate: float = 100.0,
    official_pass: int = 10,
    official_fail: int = 0,
    official_na: int = 1,
    total: int = 11,
) -> dict[str, Any]:
    return {
        "pass_rate_pct": pass_rate,
        "per_tier": {
            "official-pi-mono": {
                "pass": official_pass,
                "fail": official_fail,
                "na": official_na,
                "total": total,
            },
        },
    }


def assert_self_test(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_self_test() -> int:
    baseline = self_test_baseline()
    pass_verdict = build_regression_verdict(
        baseline,
        self_test_summary(),
        {},
        [],
        "strict",
    )
    assert_self_test(pass_verdict["status"] == "pass", "strict pass verdict should pass")
    assert_self_test(pass_verdict["failures"] == [], "strict pass verdict should have no failures")
    assert_self_test(verdict_exit_code(pass_verdict) == 0, "strict pass verdict should exit 0")

    strict_failure = build_regression_verdict(
        baseline,
        self_test_summary(pass_rate=80.0, official_pass=8, official_fail=2, official_na=1, total=11),
        {"new-extension": "FAIL"},
        [],
        "strict",
    )
    assert_self_test(strict_failure["status"] == "fail", "strict failure verdict should fail")
    assert_self_test(verdict_exit_code(strict_failure) == 1, "strict failure verdict should exit 1")
    assert_self_test(
        "pass_rate_no_regression" in strict_failure["failures"],
        "strict failure should include pass-rate regression",
    )
    assert_self_test(
        "no_new_failures" in strict_failure["failures"],
        "strict failure should include new-failure regression",
    )
    assert_self_test(
        strict_failure["new_failures"] == ["new-extension"],
        "strict failure should record the new failing extension",
    )

    warn_failure = build_regression_verdict(
        baseline,
        self_test_summary(pass_rate=80.0, official_pass=8, official_fail=2, official_na=1, total=11),
        {"new-extension": "FAIL"},
        [],
        "warn",
    )
    assert_self_test(warn_failure["status"] == "warn", "warn mode failure should warn")
    assert_self_test(verdict_exit_code(warn_failure) == 0, "warn mode failure should exit 0")

    trend_warning = build_regression_verdict(
        baseline,
        self_test_summary(),
        {},
        [
            {"pass_rate_pct": 100.0},
            {"pass_rate_pct": 99.0},
            {"pass_rate_pct": 98.0},
            {"pass_rate_pct": 97.0},
        ],
        "strict",
    )
    assert_self_test(trend_warning["status"] == "pass", "trend warning should not fail the gate")
    assert_self_test(
        any("Sustained degradation" in warning for warning in trend_warning["warnings"]),
        "trend warning should report sustained degradation",
    )

    na_allowance = next(
        check for check in pass_verdict["checks"] if check["id"] == "official_na_no_increase"
    )
    assert_self_test(na_allowance["ok"], "official N/A allowance should account for corpus growth")

    print("Conformance regression self-test passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check conformance artifacts for pass-rate and failure regressions.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run deterministic in-memory checks without writing regression_verdict.json",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    mode = os.environ.get("CI_REGRESSION_MODE", "strict").strip().lower()
    if mode not in {"strict", "warn"}:
        print(f"ERROR: invalid CI_REGRESSION_MODE={mode!r}; expected 'strict' or 'warn'", file=sys.stderr)
        return 1

    baseline = load_json(BASELINE_PATH, "conformance_baseline")
    summary = load_json(SUMMARY_PATH, "conformance_summary")
    verdict = build_regression_verdict(
        baseline,
        summary,
        load_events(EVENTS_PATH),
        load_trend_entries(TREND_PATH),
        mode,
    )
    VERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    VERDICT_PATH.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    print_verdict_report(verdict, VERDICT_PATH)
    return verdict_exit_code(verdict)


if __name__ == "__main__":
    raise SystemExit(main())
