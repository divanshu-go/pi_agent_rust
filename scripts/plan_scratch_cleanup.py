#!/usr/bin/env python3
"""Plan scratch cleanup without deleting files.

This script inventories matching entries under approved scratch roots and emits
operator evidence that is safe to review before asking for explicit deletion
approval. It never removes files and intentionally has no apply mode.

Usage:
  python3 scripts/plan_scratch_cleanup.py
  python3 scripts/plan_scratch_cleanup.py --root /tmp --pattern 'franken*' --json
  python3 scripts/plan_scratch_cleanup.py --self-test
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable

SCHEMA = "pi.scratch_cleanup_plan.v1"
DEFAULT_ROOTS = ("/tmp", "/data/tmp/pi_agent_rust_cargo")
DEFAULT_PATTERNS = ("franken*", "pi_agent_rust*", "pi-agent-rust*")


@dataclass(frozen=True)
class EntryPlan:
    path: str
    root: str
    name: str
    kind: str
    matched_pattern: str
    shallow_bytes: int
    mtime_epoch: float
    age_seconds: int
    owner_hint: str
    group: str
    review_action: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_allowed_root(path: Path, allowed_roots: Iterable[Path]) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    for allowed in allowed_roots:
        try:
            allowed_resolved = allowed.resolve(strict=False)
        except OSError:
            continue
        if resolved == allowed_resolved or allowed_resolved in resolved.parents:
            return True
    return False


def entry_kind(path: Path) -> str:
    try:
        if path.is_symlink():
            return "symlink"
        if path.is_dir():
            return "directory"
        if path.is_file():
            return "file"
    except OSError:
        return "unreadable"
    return "other"


def classify_group(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("franken_engine") or lowered.startswith("franken-engine"):
        return "franken_engine"
    if lowered.startswith("franken_node") or lowered.startswith("franken-node"):
        return "franken_node"
    if lowered.startswith("pi_agent_rust") or lowered.startswith("pi-agent-rust"):
        return "pi_agent_rust"
    if lowered.startswith("franken"):
        return "franken_other"
    return "other"


def owner_hint(root: Path, path: Path) -> str:
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        relative_parts = path.parts
    if str(root).rstrip("/") == "/data/tmp/pi_agent_rust_cargo" and relative_parts:
        return relative_parts[0] or "unknown"
    name = path.name
    for marker in ("codex", "claude", "agent", "ubuntu"):
        if marker in name.lower():
            return marker
    return "unknown"


def matched_pattern(name: str, patterns: Iterable[str]) -> str | None:
    for pattern in patterns:
        if fnmatch.fnmatchcase(name, pattern):
            return pattern
    return None


def scan_root(
    root: Path,
    patterns: tuple[str, ...],
    now_epoch: float,
    min_age_seconds: int,
    allowed_roots: tuple[Path, ...],
) -> tuple[list[EntryPlan], list[str]]:
    warnings: list[str] = []
    entries: list[EntryPlan] = []

    if not is_allowed_root(root, allowed_roots):
        warnings.append(f"refusing root outside allowlist: {root}")
        return entries, warnings
    if not root.exists():
        warnings.append(f"root does not exist: {root}")
        return entries, warnings
    if not root.is_dir():
        warnings.append(f"root is not a directory: {root}")
        return entries, warnings

    try:
        with os.scandir(root) as iterator:
            dir_entries = list(iterator)
    except OSError as exc:
        warnings.append(f"unable to scan {root}: {exc}")
        return entries, warnings

    for dir_entry in sorted(dir_entries, key=lambda item: item.name):
        pattern = matched_pattern(dir_entry.name, patterns)
        if pattern is None:
            continue
        path = Path(dir_entry.path)
        try:
            stat = dir_entry.stat(follow_symlinks=False)
        except OSError as exc:
            warnings.append(f"unable to stat {path}: {exc}")
            continue
        age_seconds = max(0, int(now_epoch - stat.st_mtime))
        if age_seconds < min_age_seconds:
            continue
        kind = entry_kind(path)
        entries.append(
            EntryPlan(
                path=str(path),
                root=str(root),
                name=dir_entry.name,
                kind=kind,
                matched_pattern=pattern,
                shallow_bytes=max(0, int(stat.st_size)),
                mtime_epoch=stat.st_mtime,
                age_seconds=age_seconds,
                owner_hint=owner_hint(root, path),
                group=classify_group(dir_entry.name),
                review_action="manual_approval_required",
            )
        )
    return entries, warnings


def build_plan(
    roots: list[Path],
    patterns: tuple[str, ...],
    min_age_seconds: int,
    allowed_roots: tuple[Path, ...],
    entry_limit: int,
) -> dict[str, Any]:
    now_epoch = datetime.now(timezone.utc).timestamp()
    entries: list[EntryPlan] = []
    warnings: list[str] = []
    for root in roots:
        root_entries, root_warnings = scan_root(
            root,
            patterns,
            now_epoch,
            min_age_seconds,
            allowed_roots,
        )
        entries.extend(root_entries)
        warnings.extend(root_warnings)

    entries.sort(key=lambda entry: (entry.root, entry.group, entry.name))
    group_counts = Counter(entry.group for entry in entries)
    owner_counts = Counter(entry.owner_hint for entry in entries)
    kind_counts = Counter(entry.kind for entry in entries)

    limited_entries = entries[:entry_limit] if entry_limit >= 0 else entries
    omitted = max(0, len(entries) - len(limited_entries))
    return {
        "schema": SCHEMA,
        "generated_at": utc_now_iso(),
        "destructive_actions_executed": False,
        "delete_apply_mode_available": False,
        "approval_required_for_cleanup": True,
        "arg_max_safe_scan": True,
        "roots": [str(root) for root in roots],
        "patterns": list(patterns),
        "min_age_seconds": min_age_seconds,
        "totals": {
            "matched_entries": len(entries),
            "listed_entries": len(limited_entries),
            "omitted_entries": omitted,
            "shallow_bytes": sum(entry.shallow_bytes for entry in entries),
            "by_group": dict(sorted(group_counts.items())),
            "by_owner_hint": dict(sorted(owner_counts.items())),
            "by_kind": dict(sorted(kind_counts.items())),
        },
        "entries": [asdict(entry) for entry in limited_entries],
        "warnings": warnings,
        "operator_note": (
            "This is a read-only inventory. Do not remove any listed path without "
            "a separate explicit approval that names the exact cleanup command and risk."
        ),
    }


def render_text(plan: dict[str, Any]) -> str:
    totals = plan["totals"]
    lines = [
        "Scratch Cleanup Plan",
        f"schema: {plan['schema']}",
        "mode: read-only; no destructive actions executed",
        f"matched entries: {totals['matched_entries']}",
        f"listed entries: {totals['listed_entries']}",
        f"omitted entries: {totals['omitted_entries']}",
        f"shallow bytes: {totals['shallow_bytes']}",
        "by group:",
    ]
    for group, count in totals["by_group"].items():
        lines.append(f"  {group}: {count}")
    if plan["warnings"]:
        lines.append("warnings:")
        for warning in plan["warnings"]:
            lines.append(f"  - {warning}")
    lines.append("entries:")
    for entry in plan["entries"]:
        lines.append(
            f"  - {entry['path']} [{entry['kind']}, group={entry['group']}, "
            f"owner={entry['owner_hint']}, age_seconds={entry['age_seconds']}]"
        )
    lines.append(plan["operator_note"])
    return "\n".join(lines)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only scratch cleanup inventory planner.",
    )
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        help="scratch root to scan; may be repeated; defaults to /tmp and /data/tmp/pi_agent_rust_cargo",
    )
    parser.add_argument(
        "--pattern",
        action="append",
        dest="patterns",
        help="fnmatch pattern for top-level entries; may be repeated",
    )
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=0.0,
        help="only include entries at least this old",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="maximum entries to list; use -1 for all",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--self-test", action="store_true", help="run fixture-backed self-test")
    return parser.parse_args(argv)


def run_self_test() -> int:
    with TemporaryDirectory(prefix="pi-scratch-cleanup-plan-") as tmp:
        root = Path(tmp)
        (root / "franken_engine_alpha").mkdir()
        (root / "franken_node_beta").write_text("beta", encoding="utf-8")
        (root / "pi_agent_rust_gamma").mkdir()
        (root / "ignore_me").write_text("ignored", encoding="utf-8")
        os.symlink(root / "franken_node_beta", root / "franken_engine_link")

        plan = build_plan(
            roots=[root],
            patterns=("franken*", "pi_agent_rust*"),
            min_age_seconds=0,
            allowed_roots=(root,),
            entry_limit=10,
        )

        assert plan["schema"] == SCHEMA
        assert plan["destructive_actions_executed"] is False
        assert plan["delete_apply_mode_available"] is False
        assert plan["approval_required_for_cleanup"] is True
        assert plan["totals"]["matched_entries"] == 4
        assert plan["totals"]["by_group"]["franken_engine"] == 2
        assert plan["totals"]["by_group"]["franken_node"] == 1
        assert plan["totals"]["by_group"]["pi_agent_rust"] == 1
        assert any(entry["kind"] == "symlink" for entry in plan["entries"])
        assert all(Path(entry["path"]).exists() for entry in plan["entries"])

        limited = build_plan(
            roots=[root],
            patterns=("franken*",),
            min_age_seconds=0,
            allowed_roots=(root,),
            entry_limit=1,
        )
        assert limited["totals"]["listed_entries"] == 1
        assert limited["totals"]["omitted_entries"] == 2

        refused = build_plan(
            roots=[Path("/not-allowed-fixture")],
            patterns=("franken*",),
            min_age_seconds=0,
            allowed_roots=(root,),
            entry_limit=10,
        )
        assert refused["totals"]["matched_entries"] == 0
        assert refused["warnings"]

    print("Scratch cleanup planner self-test passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return run_self_test()

    if args.min_age_hours < 0:
        print("ERROR: --min-age-hours must be non-negative", file=sys.stderr)
        return 2
    if args.limit < -1:
        print("ERROR: --limit must be -1 or greater", file=sys.stderr)
        return 2

    roots = [Path(root) for root in (args.roots or DEFAULT_ROOTS)]
    patterns = tuple(args.patterns or DEFAULT_PATTERNS)
    allowed_roots = tuple(Path(root) for root in DEFAULT_ROOTS)
    min_age_seconds = int(args.min_age_hours * 3600)
    plan = build_plan(
        roots=roots,
        patterns=patterns,
        min_age_seconds=min_age_seconds,
        allowed_roots=allowed_roots,
        entry_limit=args.limit,
    )

    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(render_text(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
