#!/usr/bin/env python3
"""Fail on UBS warning/critical findings that touch staged Rust lines.

`ubs --staged --only=rust` scans whole staged files. For large Rust modules that
already have known baseline findings, that can bury the actionable signal for a
small patch. This gate keeps UBS in the loop while reducing the result to the
lines introduced or modified in the staged diff.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    severity: str
    path: str
    line: int
    message: str


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
UBS_LOCATION_RE = re.compile(r"(?P<path>/[^:\n]+?\.rs):(?P<line>\d+)\b")
SUMMARY_RE = re.compile(r"^(Critical|Warning|Info):\s+(\d+)\s*$")


def run(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def repo_root() -> Path:
    proc = run(["git", "rev-parse", "--show-toplevel"], Path.cwd())
    return Path(proc.stdout.strip()).resolve()


def staged_rust_files(root: Path) -> list[str]:
    proc = run(
        [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--diff-filter=ACMRT",
            "--",
            "*.rs",
        ],
        root,
    )
    return [item for item in proc.stdout.split("\0") if item]


def staged_added_lines(root: Path) -> set[tuple[str, int]]:
    proc = run(
        [
            "git",
            "diff",
            "--cached",
            "--unified=0",
            "--no-ext-diff",
            "--",
            "*.rs",
        ],
        root,
    )

    changed: set[tuple[str, int]] = set()
    current_file: str | None = None
    new_line = 0

    for raw_line in proc.stdout.splitlines():
        if raw_line.startswith("+++ "):
            path = raw_line[4:]
            current_file = path[2:] if path.startswith("b/") else None
            continue

        hunk = HUNK_RE.match(raw_line)
        if hunk:
            new_line = int(hunk.group(1))
            continue

        if current_file is None:
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            changed.add((current_file, new_line))
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        elif raw_line.startswith("\\"):
            continue
        else:
            new_line += 1

    return changed


def normalize_path(root: Path, value: str) -> str | None:
    path = Path(value)
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def parse_ubs_output(root: Path, output: str) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    totals: dict[str, int] = {}
    severity = ""
    message = ""

    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        summary = SUMMARY_RE.match(stripped)
        if summary:
            totals[summary.group(1).lower()] = int(summary.group(2))
            continue

        if "Critical" in stripped:
            severity = "critical"
            message = ""
            continue
        if "Warning" in stripped:
            severity = "warning"
            message = ""
            continue
        if "Info" in stripped:
            severity = "info"
            message = ""
            continue

        location = UBS_LOCATION_RE.search(raw_line)
        if location and severity:
            rel_path = normalize_path(root, location.group("path"))
            if rel_path is not None:
                findings.append(
                    Finding(
                        severity=severity,
                        path=rel_path,
                        line=int(location.group("line")),
                        message=message,
                    )
                )
            continue

        if severity and stripped and not stripped.startswith(("✓", "▓", "─", "╔", "║", "╚")):
            message = stripped

    return findings, totals


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run UBS on staged Rust files and fail on changed-line warnings/critical findings.",
    )
    parser.add_argument(
        "--fail-on-info",
        action="store_true",
        help="Also fail when UBS info findings land on staged changed lines.",
    )
    parser.add_argument(
        "--print-ubs-output",
        action="store_true",
        help="Print raw UBS output after the delta summary.",
    )
    args = parser.parse_args()

    root = repo_root()
    files = staged_rust_files(root)
    if not files:
        print("UBS staged delta: no staged Rust files; skipping.")
        return 0

    changed = staged_added_lines(root)
    if not changed:
        print("UBS staged delta: staged Rust files have no added/modified lines; skipping.")
        return 0

    ubs = shutil.which("ubs")
    if ubs is None:
        print("UBS staged delta: 'ubs' command not found.", file=sys.stderr)
        return 127

    env = os.environ.copy()
    env["UBS_OUTPUT_FORMAT"] = "text"
    proc = subprocess.run(
        [ubs, "--staged", "--only=rust", "--format=text", "--ci", "."],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    findings, totals = parse_ubs_output(root, proc.stdout)

    changed_findings = [
        finding
        for finding in findings
        if (finding.path, finding.line) in changed
    ]
    fail_severities = {"critical", "warning"}
    if args.fail_on_info:
        fail_severities.add("info")
    failures = [finding for finding in changed_findings if finding.severity in fail_severities]

    print(
        "UBS staged delta: "
        f"{len(files)} staged Rust file(s), "
        f"{len(changed)} changed line(s), "
        f"{len(findings)} parsed UBS location(s), "
        f"{len(changed_findings)} finding location(s) on changed lines."
    )
    if totals:
        print(
            "UBS staged totals: "
            f"critical={totals.get('critical', 0)} "
            f"warning={totals.get('warning', 0)} "
            f"info={totals.get('info', 0)}"
        )

    if proc.returncode != 0 and not findings and not totals:
        print(f"UBS command failed with exit {proc.returncode}.", file=sys.stderr)
        print(proc.stdout[-4000:], file=sys.stderr)
        return proc.returncode

    if failures:
        print("UBS staged delta failed on changed-line findings:", file=sys.stderr)
        for finding in failures:
            detail = f" - {finding.severity}: {finding.path}:{finding.line}"
            if finding.message:
                detail += f" - {finding.message}"
            print(detail, file=sys.stderr)
        if args.print_ubs_output:
            print(proc.stdout)
        return 1

    if changed_findings:
        print("UBS staged delta info-only findings on changed lines:")
        for finding in changed_findings:
            detail = f" - {finding.severity}: {finding.path}:{finding.line}"
            if finding.message:
                detail += f" - {finding.message}"
            print(detail)

    if proc.returncode != 0:
        print(
            "UBS command returned nonzero for whole-file findings; "
            "continuing because no warning/critical finding lands on a staged changed line."
        )

    print("UBS staged delta passed: no warning/critical findings on staged changed lines.")
    if args.print_ubs_output:
        print(proc.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
