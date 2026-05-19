#!/usr/bin/env python3
"""Build a prompt-to-artifact completion audit.

The audit is intentionally conservative: it does not mark work complete unless
every extracted requirement has direct evidence and there are no unresolved
gaps or contradictory command results.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AUDIT_SCHEMA = "pi.completion_audit.v1"
CLOSEOUT_ADMISSION_SCHEMA = "pi.completion_audit.closeout_admission_evidence.v1"
GOLDEN_REPORT_DIRECTORY = Path("tests/golden_corpus/completion_audit")
COMPLETE_AUDIT_GOLDEN = "complete_audit_projection.json"
CLOSEOUT_ADMISSION_GOLDEN = "closeout_admission_projection.json"
UPDATE_GOLDEN_ENV = "UPDATE_COMPLETION_AUDIT_GOLDEN"
SNIPPET_MAX_CHARS = 1200
COMMAND_START_RE = re.compile(
    r"^(?:env\s+)?(?:cargo|rch|python3?|pytest|bash|sh|git|br|bv|ubs|"
    r"jq|rg|sed|awk|./|timeout)\b"
)
INLINE_COMMAND_RE = re.compile(r"`([^`\n]+)`")
FENCE_RE = re.compile(r"```(?:[A-Za-z0-9_-]+)?\n(.*?)```", re.DOTALL)
CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(?P<text>.+?)\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>.+?)\s*$")
PATH_RE = re.compile(
    r"\b(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+\b|"
    r"\b(?:AGENTS|README|Cargo|CHANGELOG|LICENSE)\.(?:md|toml|json|txt)\b"
)
FAILURE_RE = re.compile(
    r"(?im)(?:^|\b)(?:test result:\s+FAILED|FAILED\b|Traceback "
    r"\(most recent call last\):|error:|exit code:\s*[1-9])"
)


class AuditError(RuntimeError):
    """Raised when audit inputs are malformed."""


@dataclass(frozen=True)
class Requirement:
    id: str
    kind: str
    text: str
    source: str
    required: bool = True
    command: str | None = None
    path: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "source": self.source,
            "required": self.required,
            "command": self.command,
            "path": self.path,
        }


@dataclass
class EvidenceMatch:
    ref: str
    status: str
    issue: str | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def compact_ws(value: str) -> str:
    return " ".join(value.strip().split())


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def command_like(value: str) -> bool:
    return bool(COMMAND_START_RE.search(value.strip()))


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AuditError(f"input file does not exist: {path}") from exc


def read_json(path: Path) -> dict[str, Any]:
    payload = read_json_value(path)
    if not isinstance(payload, dict):
        raise AuditError(f"expected object JSON in {path}")
    return payload


def read_json_value(path: Path) -> Any:
    try:
        payload = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise AuditError(f"invalid JSON in {path}: {exc}") from exc
    return payload


def file_fingerprint(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def bounded_text(text: str, limit: int = SNIPPET_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    omitted = len(text) - (half * 2)
    return f"{text[:half]}\n[... {omitted} chars truncated ...]\n{text[-half:]}"


def classify_requirement(text: str, *, command: str | None = None, path: str | None = None) -> str:
    lowered = text.lower()
    if command is not None:
        return "command"
    if path is not None:
        return "file"
    if "json" in lowered and "markdown" in lowered:
        return "artifact_bundle"
    commit_action = re.search(r"\b(?:commit|committed|git commit)\b", lowered) is not None
    push_action = re.search(r"\b(?:push|pushed|git push)\b", lowered) is not None
    if commit_action and push_action:
        return "commit_push"
    if push_action:
        return "push"
    if commit_action:
        return "commit"
    if any(word in lowered for word in ("test", "clippy", "fmt", "lint", "validate", "validation")):
        return "validation"
    return "deliverable"


def add_requirement(
    requirements: list[Requirement],
    seen: set[tuple[str, str, str | None, str | None]],
    *,
    text: str,
    source: str,
    command: str | None = None,
    path: str | None = None,
) -> None:
    cleaned = compact_ws(text)
    if not cleaned:
        return
    if command is None:
        inline_commands = [
            value for value in INLINE_COMMAND_RE.findall(cleaned) if command_like(value)
        ]
        if len(inline_commands) == 1 and cleaned.lower().startswith("run `"):
            command = inline_commands[0]
    kind = classify_requirement(cleaned, command=command, path=path)
    key = (kind, slug(cleaned), command, path)
    if key in seen:
        return
    seen.add(key)
    requirements.append(
        Requirement(
            id=f"REQ-{len(requirements) + 1:03d}",
            kind=kind,
            text=cleaned,
            source=source,
            command=command,
            path=path,
        )
    )


def extract_requirements(markdown: str) -> list[Requirement]:
    requirements: list[Requirement] = []
    seen: set[tuple[str, str, str | None, str | None]] = set()
    current_section: str | None = None

    for match in FENCE_RE.finditer(markdown):
        for raw_line in match.group(1).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if command_like(line):
                add_requirement(
                    requirements,
                    seen,
                    text=f"Run `{line}`",
                    source="fenced_code_command",
                    command=line,
                )

    for line_number, raw_line in enumerate(markdown.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            current_section = line.lstrip("#").strip().lower()
            continue
        bullet = CHECKBOX_RE.match(raw_line) or BULLET_RE.match(raw_line)
        if bullet is not None:
            add_requirement(
                requirements,
                seen,
                text=bullet.group("text"),
                source=f"line:{line_number}",
                )
        elif current_section in {
            "what",
            "how",
            "tests",
            "success criteria",
            "acceptance criteria",
            "deliverables",
        }:
            add_requirement(
                requirements,
                seen,
                text=line,
                source=f"section:{current_section}:line:{line_number}",
            )
        for inline in INLINE_COMMAND_RE.findall(line):
            if command_like(inline):
                add_requirement(
                    requirements,
                    seen,
                    text=f"Run `{inline}`",
                    source=f"line:{line_number}:inline_command",
                    command=inline,
                )

    for path in PATH_RE.findall(markdown):
        add_requirement(
            requirements,
            seen,
            text=f"Inspect or update `{path}`",
            source="named_path",
            path=path,
        )

    return requirements


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def status_from_value(value: Any) -> str:
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"ok", "pass", "passed", "success", "succeeded", "present", "pushed"}:
            return "passed"
        if lowered in {"proxy", "proxy_only", "indirect"}:
            return "proxy_only"
        if lowered in {"fail", "failed", "error", "missing"}:
            return "failed"
    return "unknown"


def command_output(command: dict[str, Any]) -> str:
    output = command.get("output")
    if isinstance(output, str):
        return output
    output_path = command.get("output_path")
    if isinstance(output_path, str) and output_path:
        path = Path(output_path)
        if path.exists():
            return read_text(path)
    return ""


def command_ref(command: dict[str, Any], index: int) -> str:
    value = command.get("command")
    if isinstance(value, str) and value:
        return f"command[{index}]:{value}"
    return f"command[{index}]"


def command_evidence_status(command: dict[str, Any]) -> tuple[str, str | None]:
    explicit_status = status_from_value(command.get("status"))
    exit_code = command.get("exit_code")
    output = command_output(command)
    contradiction_reasons: list[str] = []

    if explicit_status == "passed" and isinstance(exit_code, int) and exit_code != 0:
        contradiction_reasons.append(f"status passed but exit_code={exit_code}")
    if explicit_status == "passed" and FAILURE_RE.search(output):
        contradiction_reasons.append("status passed but output contains a failure signature")
    if contradiction_reasons:
        return "contradiction", "; ".join(contradiction_reasons)

    if command.get("proxy_only") is True:
        return "proxy_only", "command result is marked proxy_only"
    if isinstance(exit_code, int):
        return ("passed", None) if exit_code == 0 else ("failed", f"exit_code={exit_code}")
    if explicit_status != "unknown":
        return explicit_status, None
    if FAILURE_RE.search(output):
        return "failed", "output contains a failure signature"
    return "unknown", "missing exit_code/status"


def command_matches(requirement: Requirement, command: dict[str, Any]) -> bool:
    value = str(command.get("command") or "")
    covers = [str(item) for item in as_list(command.get("covers"))]
    if requirement.id in covers or requirement.text in covers:
        return True
    if requirement.command is not None:
        required = compact_ws(requirement.command)
        observed = compact_ws(value)
        return required == observed or required in observed or observed in required
    if requirement.kind == "validation":
        lowered = value.lower()
        return any(
            token in lowered
            for token in ("cargo test", "cargo check", "cargo clippy", "cargo fmt", "py_compile", "--self-test")
        )
    if requirement.kind == "push":
        return value.strip().startswith("git push")
    if requirement.kind == "commit":
        return value.strip().startswith("git commit")
    return False


def artifact_status(artifact: dict[str, Any], repo_root: Path) -> tuple[str, str | None, dict[str, Any]]:
    path_value = artifact.get("path")
    extra: dict[str, Any] = {}
    if not isinstance(path_value, str) or not path_value:
        return "failed", "artifact is missing path", extra
    path = Path(path_value)
    if not path.is_absolute():
        path = repo_root / path
    if artifact.get("proxy_only") is True:
        return "proxy_only", "artifact is marked proxy_only", extra
    explicit = status_from_value(artifact.get("status"))
    if path.exists():
        extra.update(file_fingerprint(path))
        return "passed", None, extra
    if explicit == "passed":
        return "contradiction", f"artifact status passed but path is missing: {path_value}", extra
    if explicit != "unknown":
        return explicit, None, extra
    return "failed", f"artifact path is missing: {path_value}", extra


def artifact_matches(requirement: Requirement, artifact: dict[str, Any]) -> bool:
    path_value = normalize_path(str(artifact.get("path") or ""))
    covers = [str(item) for item in as_list(artifact.get("covers"))]
    if requirement.id in covers or requirement.text in covers:
        return True
    if requirement.path is not None:
        return normalize_path(requirement.path) == path_value
    if requirement.kind == "artifact_bundle":
        return path_value.endswith(".json") or path_value.endswith(".md")
    return False


def path_in_files(path: str, evidence: dict[str, Any]) -> bool:
    expected = normalize_path(path)
    for item in as_list(evidence.get("files_changed")):
        if isinstance(item, str) and normalize_path(item) == expected:
            return True
        if isinstance(item, dict) and normalize_path(str(item.get("path") or "")) == expected:
            return True
    return False


def commit_status(commit: dict[str, Any]) -> str:
    if commit.get("proxy_only") is True:
        return "proxy_only"
    if status_from_value(commit.get("status")) == "failed":
        return "failed"
    value = commit.get("hash") or commit.get("sha")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
        return "passed"
    return status_from_value(commit.get("status"))


def push_status(push: dict[str, Any]) -> str:
    if push.get("proxy_only") is True:
        return "proxy_only"
    return status_from_value(push.get("status"))


def first_object(payload: Any, *, source: str) -> dict[str, Any]:
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            return payload[0]
        raise AuditError(f"{source} must contain at least one object")
    if isinstance(payload, dict):
        return payload
    raise AuditError(f"{source} must be an object or a list containing an object")


def normalize_issue(issue: dict[str, Any], fallback_id: str | None = None) -> dict[str, Any]:
    issue_id = str(issue.get("id") or fallback_id or "")
    if not issue_id:
        raise AuditError("bead issue is missing id")
    dependencies = [
        str(item.get("id") or item)
        for item in as_list(issue.get("dependencies"))
        if isinstance(item, (str, dict))
    ]
    dependents = [
        str(item.get("id") or item)
        for item in as_list(issue.get("dependents"))
        if isinstance(item, (str, dict))
    ]
    return {
        "id": issue_id,
        "title": issue.get("title"),
        "status": issue.get("status"),
        "assignee": issue.get("assignee"),
        "priority": issue.get("priority"),
        "issue_type": issue.get("issue_type") or issue.get("type"),
        "close_reason": issue.get("close_reason"),
        "closed_at": issue.get("closed_at"),
        "updated_at": issue.get("updated_at"),
        "dependencies": dependencies,
        "dependents": dependents,
    }


def normalize_ready_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("issues", payload.get("items", payload.get("ready", [])))
    if not isinstance(payload, list):
        return []
    items: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict):
            items.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "priority": item.get("priority"),
                    "issue_type": item.get("issue_type") or item.get("type"),
                }
            )
    return items


def parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def parse_branch_status(branch_line: str) -> dict[str, Any]:
    branch = ""
    upstream = None
    ahead_by = 0
    behind_by = 0
    if branch_line.startswith("## "):
        body = branch_line[3:]
        if "..." in body:
            branch, rest = body.split("...", 1)
            if " [" in rest:
                upstream, flags = rest.split(" [", 1)
                flags = flags.rstrip("]")
                for part in flags.split(", "):
                    if part.startswith("ahead "):
                        ahead_by = parse_int(part.removeprefix("ahead ")) or 0
                    elif part.startswith("behind "):
                        behind_by = parse_int(part.removeprefix("behind ")) or 0
            else:
                upstream = rest or None
        else:
            branch = body
    return {
        "branch": branch,
        "upstream": upstream,
        "ahead_by": ahead_by,
        "behind_by": behind_by,
        "is_pushed": bool(upstream) and ahead_by == 0,
    }


def dirty_paths_from_files(files_changed: list[Any]) -> list[str]:
    paths: list[str] = []
    for item in files_changed:
        if isinstance(item, str):
            paths.append(normalize_path(item))
        elif isinstance(item, dict):
            path = item.get("path")
            if isinstance(path, str) and path:
                paths.append(normalize_path(path))
    return sorted(dict.fromkeys(paths))


def combine_matches(matches: list[EvidenceMatch]) -> tuple[str, str | None]:
    if not matches:
        return "missing", "no direct evidence matched this requirement"
    if any(match.status == "contradiction" for match in matches):
        issues = [match.issue for match in matches if match.status == "contradiction" and match.issue]
        return "contradiction", "; ".join(issues) if issues else "contradictory evidence"
    if any(match.status == "failed" for match in matches):
        issues = [match.issue for match in matches if match.status == "failed" and match.issue]
        return "failed", "; ".join(issues) if issues else "matching evidence failed"
    if all(match.status == "proxy_only" for match in matches):
        return "proxy_only", "only proxy evidence matched this requirement"
    if any(match.status == "passed" for match in matches):
        return "covered", None
    return "uncertain", "matching evidence lacks a conclusive pass/fail status"


def evaluate_requirement(
    requirement: Requirement,
    evidence: dict[str, Any],
    repo_root: Path,
) -> dict[str, Any]:
    matches: list[EvidenceMatch] = []
    commands = [item for item in as_list(evidence.get("commands")) if isinstance(item, dict)]
    artifacts = [item for item in as_list(evidence.get("artifacts")) if isinstance(item, dict)]
    commits = [item for item in as_list(evidence.get("commits")) if isinstance(item, dict)]
    pushes = [item for item in as_list(evidence.get("pushes")) if isinstance(item, dict)]

    for index, command in enumerate(commands):
        if command_matches(requirement, command):
            status, issue = command_evidence_status(command)
            matches.append(EvidenceMatch(command_ref(command, index), status, issue))

    artifact_bundle_json = False
    artifact_bundle_md = False
    for index, artifact in enumerate(artifacts):
        status, issue, _ = artifact_status(artifact, repo_root)
        path_value = normalize_path(str(artifact.get("path") or ""))
        if requirement.kind == "artifact_bundle" and status == "passed":
            artifact_bundle_json = artifact_bundle_json or path_value.endswith(".json")
            artifact_bundle_md = artifact_bundle_md or path_value.endswith(".md")
        if artifact_matches(requirement, artifact):
            matches.append(EvidenceMatch(f"artifact[{index}]:{path_value}", status, issue))

    if requirement.kind == "artifact_bundle":
        if artifact_bundle_json and artifact_bundle_md:
            matches.append(EvidenceMatch("artifact_bundle:json+markdown", "passed", None))
        else:
            missing_parts = []
            if not artifact_bundle_json:
                missing_parts.append("JSON")
            if not artifact_bundle_md:
                missing_parts.append("Markdown")
            matches.append(
                EvidenceMatch(
                    "artifact_bundle:json+markdown",
                    "failed",
                    f"missing {' and '.join(missing_parts)} artifact evidence",
                )
            )

    if requirement.path is not None and path_in_files(requirement.path, evidence):
        matches.append(EvidenceMatch(f"files_changed:{requirement.path}", "passed", None))

    if requirement.kind not in {"command", "validation"}:
        for referenced_path in PATH_RE.findall(requirement.text):
            if path_in_files(referenced_path, evidence):
                matches.append(EvidenceMatch(f"files_changed:{referenced_path}", "passed", None))

    if requirement.kind in {"commit", "commit_push"}:
        for index, commit in enumerate(commits):
            matches.append(EvidenceMatch(f"commit[{index}]", commit_status(commit), None))

    if requirement.kind in {"push", "commit_push"}:
        for index, push in enumerate(pushes):
            matches.append(EvidenceMatch(f"push[{index}]", push_status(push), None))

    if requirement.kind == "commit_push":
        has_commit = any(match.ref.startswith("commit[") and match.status == "passed" for match in matches)
        has_push = any(match.ref.startswith("push[") and match.status == "passed" for match in matches)
        if has_commit and has_push:
            matches.append(EvidenceMatch("commit_push:commit+push", "passed", None))
        elif matches:
            missing = []
            if not has_commit:
                missing.append("commit")
            if not has_push:
                missing.append("push")
            matches.append(EvidenceMatch("commit_push:required_pair", "failed", f"missing {' and '.join(missing)} evidence"))

    explicit_statuses = [
        item for item in as_list(evidence.get("requirement_statuses")) if isinstance(item, dict)
    ]
    for item in explicit_statuses:
        if item.get("id") == requirement.id or item.get("text") == requirement.text:
            ref = item.get("ref")
            if not isinstance(ref, str) or not ref:
                ref = f"requirement_status:{requirement.id}"
            matches.append(
                EvidenceMatch(
                    ref,
                    status_from_value(item.get("status")),
                    str(item.get("issue")) if item.get("issue") else None,
                )
            )

    status, issue = combine_matches(matches)
    return {
        **requirement.to_json(),
        "evidence_status": status,
        "evidence_refs": list(dict.fromkeys(match.ref for match in matches)),
        "issue": issue,
    }


def summarize_evidence(evidence: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    commands = []
    for index, command in enumerate(as_list(evidence.get("commands"))):
        if not isinstance(command, dict):
            continue
        status, issue = command_evidence_status(command)
        entry = {
            "ref": command_ref(command, index),
            "command": command.get("command"),
            "status": status,
            "exit_code": command.get("exit_code"),
            "issue": issue,
        }
        output_path = command.get("output_path")
        if isinstance(output_path, str) and output_path:
            path = Path(output_path)
            if path.exists():
                entry["output_path"] = output_path
                entry.update(file_fingerprint(path))
                entry["snippet"] = bounded_text(read_text(path))
        commands.append(entry)

    artifacts = []
    for index, artifact in enumerate(as_list(evidence.get("artifacts"))):
        if not isinstance(artifact, dict):
            continue
        status, issue, extra = artifact_status(artifact, repo_root)
        artifacts.append(
            {
                "ref": f"artifact[{index}]",
                "path": artifact.get("path"),
                "status": status,
                "issue": issue,
                **extra,
            }
        )

    summary = {
        "files_changed": as_list(evidence.get("files_changed")),
        "commands": commands,
        "artifacts": artifacts,
        "commits": as_list(evidence.get("commits")),
        "pushes": as_list(evidence.get("pushes")),
        "unresolved_gaps": as_list(evidence.get("unresolved_gaps")),
    }
    if evidence.get("beads") is not None:
        summary["beads"] = as_list(evidence.get("beads"))
    if isinstance(evidence.get("closeout_admission"), dict):
        summary["closeout_admission"] = evidence["closeout_admission"]
    return summary


def capture_git_evidence(repo_root: Path) -> dict[str, Any]:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    status = run(["git", "status", "--short", "--branch"])
    files_changed = []
    for line in status.stdout.splitlines():
        if line.startswith("## "):
            continue
        if len(line) >= 4:
            files_changed.append({"path": line[3:], "status": line[:2].strip()})
    dirty_files = dirty_paths_from_files(files_changed)
    latest = run(["git", "log", "-1", "--format=%H%x00%s"])
    commits = []
    if latest.returncode == 0 and "\x00" in latest.stdout:
        commit_hash, subject = latest.stdout.strip().split("\x00", 1)
        commits.append({"hash": commit_hash, "subject": subject, "status": "present"})
    pushes = []
    branch_line = next((line for line in status.stdout.splitlines() if line.startswith("## ")), "")
    branch_status = parse_branch_status(branch_line)
    if branch_status["upstream"]:
        pushes.append(
            {
                "remote": str(branch_status["upstream"]).split("/", 1)[0],
                "branch": branch_status["branch"],
                "upstream": branch_status["upstream"],
                "status": "pushed" if branch_status["is_pushed"] else "failed",
                "source": "git status",
                "ahead_by": branch_status["ahead_by"],
                "behind_by": branch_status["behind_by"],
            }
        )
    return {
        "files_changed": files_changed,
        "commits": commits,
        "pushes": pushes,
        "git_status": {
            "exit_code": status.returncode,
            "stdout": status.stdout,
            "stderr": status.stderr,
            **branch_status,
            "dirty_files": dirty_files,
        },
    }


def issue_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.bead_json is not None:
        return first_object(read_json_value(args.bead_json), source=str(args.bead_json))
    if args.bead_id:
        result = subprocess.run(
            ["br", "show", args.bead_id, "--json"],
            cwd=args.repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise AuditError(f"failed to read bead {args.bead_id}: {result.stderr.strip()}")
        return first_object(json.loads(result.stdout), source=f"br show {args.bead_id}")
    raise AuditError("--closeout-admission requires --bead-id or --bead-json")


def load_optional_json(path: Path | None) -> Any | None:
    if path is None:
        return None
    return read_json_value(path)


def closeout_requirement_statuses(
    *,
    requirements: list[Requirement],
    source_bead: dict[str, Any],
    git_status: dict[str, Any],
    require_clean_worktree: bool,
) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    bead_status = str(source_bead.get("status") or "").lower()
    close_reason = source_bead.get("close_reason")
    has_close_reason = isinstance(close_reason, str) and bool(close_reason.strip())
    dirty_files = dirty_paths_from_files(as_list(git_status.get("dirty_files")))

    for requirement in requirements:
        lowered = requirement.text.lower()
        wants_bead_closeout = "bead" in lowered and any(
            token in lowered for token in ("close", "closed", "closeout")
        )
        wants_clean_worktree = "worktree" in lowered and any(
            token in lowered for token in ("clean", "dirty")
        )
        if wants_bead_closeout:
            passed = bead_status == "closed" and has_close_reason
            issue = None
            if bead_status != "closed":
                issue = f"source bead {source_bead['id']} status is {source_bead.get('status')}"
            elif not has_close_reason:
                issue = f"source bead {source_bead['id']} is closed without a close_reason"
            statuses.append(
                {
                    "id": requirement.id,
                    "text": requirement.text,
                    "status": "passed" if passed else "failed",
                    "ref": f"bead:{source_bead['id']}:closeout",
                    "issue": issue,
                }
            )
        if wants_clean_worktree:
            statuses.append(
                {
                    "id": requirement.id,
                    "text": requirement.text,
                    "status": "passed" if not dirty_files else "failed",
                    "ref": "git:worktree_clean",
                    "issue": None if not dirty_files else f"dirty files present: {', '.join(dirty_files)}",
                }
            )
    return statuses


def closeout_unresolved_gaps(
    *,
    source_bead: dict[str, Any],
    git_status: dict[str, Any],
    require_clean_worktree: bool,
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    bead_status = str(source_bead.get("status") or "").lower()
    close_reason = source_bead.get("close_reason")
    if bead_status != "closed":
        gaps.append(
            {
                "gap_id": "source_bead_not_closed",
                "severity": "high",
                "owner_bead": source_bead["id"],
                "status": bead_status or "missing",
                "issue": f"source bead {source_bead['id']} must be closed before closeout admission passes",
            }
        )
    elif not (isinstance(close_reason, str) and close_reason.strip()):
        gaps.append(
            {
                "gap_id": "source_bead_missing_close_reason",
                "severity": "high",
                "owner_bead": source_bead["id"],
                "status": "missing",
                "issue": f"source bead {source_bead['id']} is closed without direct close_reason evidence",
            }
        )

    dirty_files = dirty_paths_from_files(as_list(git_status.get("dirty_files")))
    if require_clean_worktree and dirty_files:
        gaps.append(
            {
                "gap_id": "worktree_not_clean",
                "severity": "high",
                "owner_bead": source_bead["id"],
                "status": "dirty",
                "issue": f"dirty files present: {', '.join(dirty_files)}",
            }
        )
    return gaps


def build_closeout_admission_evidence(
    *,
    objective: str,
    repo_root: Path,
    source_issue: dict[str, Any],
    ready_payload: Any | None = None,
    git_evidence: dict[str, Any] | None = None,
    agent_mail_summary: Any | None = None,
    rch_summary: Any | None = None,
    require_clean_worktree: bool = False,
    runs_br_read_commands: bool = False,
    generated_at: str | None = None,
) -> dict[str, Any]:
    source_bead = normalize_issue(source_issue)
    git_payload = git_evidence if git_evidence is not None else capture_git_evidence(repo_root)
    git_status = dict(git_payload.get("git_status") or {})
    if "dirty_files" in git_status:
        dirty_files = dirty_paths_from_files(as_list(git_status.get("dirty_files")))
    else:
        dirty_files = dirty_paths_from_files(as_list(git_payload.get("files_changed")))
    git_status["dirty_files"] = dirty_files
    requirements = extract_requirements(objective)
    requirement_statuses = closeout_requirement_statuses(
        requirements=requirements,
        source_bead=source_bead,
        git_status=git_status,
        require_clean_worktree=require_clean_worktree,
    )
    unresolved = closeout_unresolved_gaps(
        source_bead=source_bead,
        git_status=git_status,
        require_clean_worktree=require_clean_worktree,
    )
    ready_items = normalize_ready_items(ready_payload) if ready_payload is not None else []
    admission = {
        "schema": CLOSEOUT_ADMISSION_SCHEMA,
        "generated_at": generated_at or utc_now_iso(),
        "source_bead": source_bead,
        "ready_successors": ready_items,
        "read_only": True,
        "source_boundaries": {
            "runs_git_read_commands": git_evidence is None,
            "runs_br_read_commands": runs_br_read_commands,
            "mutates_beads": False,
            "mutates_agent_mail": False,
            "mutates_rch": False,
            "runs_cargo": False,
            "deletes_files": False,
        },
        "git_status": git_status,
        "agent_mail_summary": agent_mail_summary,
        "rch_summary": rch_summary,
        "blocked_requirements": [
            item for item in requirement_statuses if status_from_value(item.get("status")) == "failed"
        ],
        "operator_next_actions": [
            gap["issue"] for gap in unresolved if isinstance(gap, dict) and gap.get("issue")
        ],
    }
    return merge_evidence(
        {
            "closeout_admission": admission,
            "beads": [source_bead],
            "requirement_statuses": requirement_statuses,
            "unresolved_gaps": unresolved,
        },
        git_payload,
    )


def build_closeout_admission_evidence_from_args(
    args: argparse.Namespace,
    objective: str,
) -> dict[str, Any]:
    git_evidence = load_optional_json(args.git_evidence_json)
    if git_evidence is not None and not isinstance(git_evidence, dict):
        raise AuditError(f"expected object JSON in {args.git_evidence_json}")
    return build_closeout_admission_evidence(
        objective=objective,
        repo_root=args.repo_root,
        source_issue=issue_from_args(args),
        ready_payload=load_optional_json(args.ready_json),
        git_evidence=git_evidence,
        agent_mail_summary=load_optional_json(args.agent_mail_summary_json),
        rch_summary=load_optional_json(args.rch_summary_json),
        require_clean_worktree=args.require_clean_worktree,
        runs_br_read_commands=args.bead_json is None,
    )


def merge_evidence(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in (
        "files_changed",
        "commands",
        "artifacts",
        "commits",
        "pushes",
        "unresolved_gaps",
        "requirement_statuses",
        "beads",
    ):
        merged[key] = as_list(base.get(key)) + as_list(extra.get(key))
    for key, value in extra.items():
        if key not in merged:
            merged[key] = value
    return merged


def build_audit(
    *,
    objective: str,
    evidence: dict[str, Any],
    repo_root: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    requirements = extract_requirements(objective)
    evaluated = [evaluate_requirement(req, evidence, repo_root) for req in requirements]
    evidence_summary = summarize_evidence(evidence, repo_root)
    unresolved_gaps = evidence_summary["unresolved_gaps"]
    counts = {
        "covered": 0,
        "missing": 0,
        "failed": 0,
        "proxy_only": 0,
        "contradiction": 0,
        "uncertain": 0,
    }
    for requirement in evaluated:
        status = str(requirement["evidence_status"])
        counts[status if status in counts else "uncertain"] += 1

    completion_allowed = (
        bool(evaluated)
        and counts["missing"] == 0
        and counts["failed"] == 0
        and counts["proxy_only"] == 0
        and counts["contradiction"] == 0
        and counts["uncertain"] == 0
        and not unresolved_gaps
    )
    if completion_allowed:
        overall_status = "complete"
    elif counts["contradiction"] or counts["failed"] or unresolved_gaps:
        overall_status = "blocked"
    else:
        overall_status = "incomplete"

    return {
        "schema": AUDIT_SCHEMA,
        "generated_at": generated_at or utc_now_iso(),
        "overall_status": overall_status,
        "completion_allowed": completion_allowed,
        "summary": {
            "requirement_count": len(evaluated),
            **counts,
            "unresolved_gap_count": len(unresolved_gaps),
        },
        "requirements": evaluated,
        "evidence": evidence_summary,
        "operator_notes": [
            "Direct evidence is required for every extracted requirement.",
            "Proxy-only or contradictory evidence blocks completion.",
            "Passing tests or green status are not accepted when they do not map to a requirement.",
        ],
    }


def render_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# Completion Audit",
        "",
        f"- schema: `{audit['schema']}`",
        f"- generated_at: `{audit['generated_at']}`",
        f"- overall_status: `{audit['overall_status']}`",
        f"- completion_allowed: `{str(audit['completion_allowed']).lower()}`",
        "",
        "## Requirements",
        "",
    ]
    for req in audit["requirements"]:
        status = req["evidence_status"]
        issue = f" - {req['issue']}" if req.get("issue") else ""
        lines.append(f"- `{status}` `{req['id']}` {req['text']}{issue}")
        for ref in req.get("evidence_refs", []):
            lines.append(f"  - evidence: `{ref}`")
    lines.extend(["", "## Evidence", ""])
    for command in audit["evidence"]["commands"]:
        issue = f" ({command['issue']})" if command.get("issue") else ""
        lines.append(f"- command `{command['status']}`: `{command.get('command')}`{issue}")
    for artifact in audit["evidence"]["artifacts"]:
        issue = f" ({artifact['issue']})" if artifact.get("issue") else ""
        lines.append(f"- artifact `{artifact['status']}`: `{artifact.get('path')}`{issue}")
    if audit["evidence"]["unresolved_gaps"]:
        lines.extend(["", "## Unresolved Gaps", ""])
        for gap in audit["evidence"]["unresolved_gaps"]:
            lines.append(f"- {gap}")
    return "\n".join(lines) + "\n"


def canonical_audit_projection(audit: dict[str, Any]) -> dict[str, Any]:
    evidence_projection = {
        "command_statuses": [
            {
                "command": command["command"],
                "status": command["status"],
                "exit_code": command["exit_code"],
                "issue": command["issue"],
            }
            for command in audit["evidence"]["commands"]
        ],
        "artifact_statuses": [
            {
                "path": artifact["path"],
                "status": artifact["status"],
                "issue": artifact["issue"],
            }
            for artifact in audit["evidence"]["artifacts"]
        ],
        "unresolved_gaps": audit["evidence"]["unresolved_gaps"],
    }
    if "beads" in audit["evidence"]:
        evidence_projection["beads"] = audit["evidence"]["beads"]
    if "closeout_admission" in audit["evidence"]:
        admission = audit["evidence"]["closeout_admission"]
        evidence_projection["closeout_admission"] = {
            "schema": admission["schema"],
            "source_bead": admission["source_bead"],
            "read_only": admission["read_only"],
            "source_boundaries": admission["source_boundaries"],
            "git_status": admission["git_status"],
            "blocked_requirement_count": len(admission["blocked_requirements"]),
            "operator_next_actions": admission["operator_next_actions"],
        }
    return {
        "schema": audit["schema"],
        "generated_at": audit["generated_at"],
        "overall_status": audit["overall_status"],
        "completion_allowed": audit["completion_allowed"],
        "summary": audit["summary"],
        "requirements": [
            {
                "id": req["id"],
                "kind": req["kind"],
                "text": req["text"],
                "source": req["source"],
                "evidence_status": req["evidence_status"],
                "evidence_refs": req["evidence_refs"],
                "issue": req["issue"],
            }
            for req in audit["requirements"]
        ],
        "evidence": evidence_projection,
    }


def repo_golden_path(golden_name: str) -> tuple[Path, Path]:
    relative_path = GOLDEN_REPORT_DIRECTORY / golden_name
    return Path(__file__).resolve().parent.parent / relative_path, relative_path


def assert_audit_matches_golden(
    audit: dict[str, Any],
    golden_name: str = COMPLETE_AUDIT_GOLDEN,
) -> None:
    actual = stable_json(canonical_audit_projection(audit))
    golden_path, relative_path = repo_golden_path(golden_name)
    if os.environ.get(UPDATE_GOLDEN_ENV) == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return
    try:
        expected = golden_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AssertionError(
            f"{relative_path} is missing; update it with reviewed output from "
            f"`{UPDATE_GOLDEN_ENV}=1 python3 scripts/build_completion_audit.py --self-test`."
        ) from exc
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(relative_path),
                tofile="actual completion audit projection",
            )
        )
        raise AssertionError(
            "completion audit projection changed; update the golden only after review with "
            f"`{UPDATE_GOLDEN_ENV}=1 python3 scripts/build_completion_audit.py --self-test`\n"
            f"{diff}"
        )


def assert_condition(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def complete_fixture(tmpdir: Path) -> tuple[str, dict[str, Any]]:
    (tmpdir / "completion-audit.json").write_text("{}\n", encoding="utf-8")
    (tmpdir / "completion-audit.md").write_text("# Audit\n", encoding="utf-8")
    objective = """# Objective
- Implement completion audit generator in `scripts/build_completion_audit.py`.
- Run `python3 -m py_compile scripts/build_completion_audit.py`.
- Run `python3 scripts/build_completion_audit.py --self-test`.
- Emit JSON plus Markdown audit artifacts.
- Commit and push the finished work.
"""
    evidence = {
        "files_changed": ["scripts/build_completion_audit.py"],
        "commands": [
            {
                "command": "python3 -m py_compile scripts/build_completion_audit.py",
                "exit_code": 0,
                "output": "compile ok\n",
            },
            {
                "command": "python3 scripts/build_completion_audit.py --self-test",
                "exit_code": 0,
                "output": "SELF-TEST PASS\n",
            },
            {
                "command": "git push origin main",
                "exit_code": 0,
                "output": "main -> main\n",
            },
        ],
        "artifacts": [
            {"path": "completion-audit.json", "status": "present"},
            {"path": "completion-audit.md", "status": "present"},
        ],
        "commits": [
            {
                "hash": "043ba328a",
                "subject": "feat(audit): add completion audit generator",
                "status": "present",
            }
        ],
        "pushes": [{"remote": "origin", "branch": "main", "status": "pushed"}],
    }
    return objective, evidence


def closeout_adapter_fixture(tmpdir: Path) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    (tmpdir / "completion-audit.json").write_text("{}\n", encoding="utf-8")
    (tmpdir / "completion-audit.md").write_text("# Audit\n", encoding="utf-8")
    objective = """# Objective
- Close bead `bd-fixture` with direct evidence in the close reason.
- Update `scripts/build_completion_audit.py`.
- Run `python3 scripts/build_completion_audit.py --self-test`.
- Emit JSON plus Markdown audit artifacts.
- Commit and push the finished work.
- Leave the worktree clean.
"""
    evidence = {
        "commands": [
            {
                "command": "python3 scripts/build_completion_audit.py --self-test",
                "exit_code": 0,
                "output": "SELF-TEST PASS\n",
            }
        ],
        "artifacts": [
            {"path": "completion-audit.json", "status": "present"},
            {"path": "completion-audit.md", "status": "present"},
        ],
    }
    issue = {
        "id": "bd-fixture",
        "title": "Fixture closeout",
        "status": "closed",
        "assignee": "FixtureAgent",
        "priority": 2,
        "issue_type": "task",
        "close_reason": "Completed with direct evidence: commit abc1234, pushed ref, self-test, and audit artifacts.",
        "closed_at": "2026-01-02T03:00:00+00:00",
        "updated_at": "2026-01-02T03:00:00+00:00",
        "dependencies": [],
        "dependents": [{"id": "bd-fixture.2", "status": "open"}],
    }
    git_evidence = {
        "files_changed": [{"path": "scripts/build_completion_audit.py", "status": "M"}],
        "commits": [
            {
                "hash": "abc1234",
                "subject": "bd-fixture add closeout admission adapter",
                "status": "present",
            }
        ],
        "pushes": [
            {
                "remote": "origin",
                "branch": "main",
                "upstream": "origin/main",
                "status": "pushed",
                "source": "fixture",
                "ahead_by": 0,
                "behind_by": 0,
            }
        ],
        "git_status": {
            "exit_code": 0,
            "stdout": "## main...origin/main\n",
            "stderr": "",
            "branch": "main",
            "upstream": "origin/main",
            "ahead_by": 0,
            "behind_by": 0,
            "is_pushed": True,
            "dirty_files": [],
        },
    }
    ready_items = [
        {
            "id": "bd-fixture.2",
            "title": "Next adapter child",
            "status": "open",
            "priority": 2,
            "issue_type": "feature",
        }
    ]
    return objective, evidence, issue, git_evidence, ready_items


def closeout_adapter_audit(
    *,
    objective: str,
    evidence: dict[str, Any],
    issue: dict[str, Any],
    git_evidence: dict[str, Any],
    ready_items: list[dict[str, Any]],
    repo_root: Path,
    generated_at: str,
    require_clean_worktree: bool = True,
) -> dict[str, Any]:
    adapter_evidence = build_closeout_admission_evidence(
        objective=objective,
        repo_root=repo_root,
        source_issue=issue,
        ready_payload=ready_items,
        git_evidence=git_evidence,
        agent_mail_summary={
            "thread_id": "bd-fixture",
            "start_message_id": 10,
            "completion_message_id": 11,
            "reservation_ids": [1, 2],
        },
        rch_summary={"required": False, "reason": "python-only fixture"},
        require_clean_worktree=require_clean_worktree,
        generated_at=generated_at,
    )
    return build_audit(
        objective=objective,
        evidence=merge_evidence(evidence, adapter_evidence),
        repo_root=repo_root,
        generated_at=generated_at,
    )


def run_self_test() -> int:
    fixed_now = "2026-01-02T03:04:05+00:00"
    with tempfile.TemporaryDirectory(prefix="completion_audit_selftest_") as raw_tmp:
        tmpdir = Path(raw_tmp)
        objective, evidence = complete_fixture(tmpdir)
        audit = build_audit(
            objective=objective,
            evidence=evidence,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(audit["overall_status"] == "complete", "complete fixture should pass")
        assert_condition(audit["completion_allowed"] is True, "complete fixture should allow completion")
        assert_audit_matches_golden(audit)

        missing_command = json.loads(json.dumps(evidence))
        missing_command["commands"] = missing_command["commands"][1:]
        missing = build_audit(
            objective=objective,
            evidence=missing_command,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        missing_items = [
            req for req in missing["requirements"] if req["evidence_status"] == "missing"
        ]
        assert_condition(missing["completion_allowed"] is False, "missing command should block")
        assert_condition(missing_items, "missing command should surface a missing requirement")

        proxy_only = json.loads(json.dumps(evidence))
        proxy_only["commands"][0]["proxy_only"] = True
        proxy_only["commands"][0].pop("exit_code", None)
        proxy = build_audit(
            objective=objective,
            evidence=proxy_only,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(proxy["completion_allowed"] is False, "proxy-only evidence should block")
        assert_condition(
            any(req["evidence_status"] == "proxy_only" for req in proxy["requirements"]),
            "proxy-only status should be visible",
        )

        contradictory = json.loads(json.dumps(evidence))
        contradictory["commands"][0]["status"] = "passed"
        contradictory["commands"][0]["exit_code"] = 1
        contradiction = build_audit(
            objective=objective,
            evidence=contradictory,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(
            contradiction["overall_status"] == "blocked",
            "contradictory green status should block",
        )
        assert_condition(
            any(req["evidence_status"] == "contradiction" for req in contradiction["requirements"]),
            "contradiction should be mapped to a requirement",
        )

        gaps = json.loads(json.dumps(evidence))
        gaps["unresolved_gaps"] = ["follow-up validation missing"]
        gap_audit = build_audit(
            objective=objective,
            evidence=gaps,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(gap_audit["overall_status"] == "blocked", "unresolved gaps should block")

        (
            closeout_objective,
            closeout_evidence,
            closeout_issue,
            closeout_git,
            closeout_ready,
        ) = closeout_adapter_fixture(tmpdir)
        closeout_audit = closeout_adapter_audit(
            objective=closeout_objective,
            evidence=closeout_evidence,
            issue=closeout_issue,
            git_evidence=closeout_git,
            ready_items=closeout_ready,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(
            closeout_audit["completion_allowed"] is True,
            "closeout adapter fixture should allow completion",
        )
        assert_audit_matches_golden(closeout_audit, CLOSEOUT_ADMISSION_GOLDEN)

        missing_push_git = json.loads(json.dumps(closeout_git))
        missing_push_git["pushes"][0]["status"] = "failed"
        missing_push_git["pushes"][0]["ahead_by"] = 1
        missing_push_git["git_status"]["is_pushed"] = False
        missing_push = closeout_adapter_audit(
            objective=closeout_objective,
            evidence=closeout_evidence,
            issue=closeout_issue,
            git_evidence=missing_push_git,
            ready_items=closeout_ready,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(missing_push["completion_allowed"] is False, "missing pushed ref should block")
        assert_condition(
            any(req["kind"] == "commit_push" and req["evidence_status"] == "failed" for req in missing_push["requirements"]),
            "missing pushed ref should map to commit/push requirement",
        )

        failed_command_evidence = json.loads(json.dumps(closeout_evidence))
        failed_command_evidence["commands"][0]["exit_code"] = 1
        failed_command = closeout_adapter_audit(
            objective=closeout_objective,
            evidence=failed_command_evidence,
            issue=closeout_issue,
            git_evidence=closeout_git,
            ready_items=closeout_ready,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(failed_command["completion_allowed"] is False, "failed command should block")

        proxy_command_evidence = json.loads(json.dumps(closeout_evidence))
        proxy_command_evidence["commands"][0]["proxy_only"] = True
        proxy_command_evidence["commands"][0].pop("exit_code", None)
        proxy_command = closeout_adapter_audit(
            objective=closeout_objective,
            evidence=proxy_command_evidence,
            issue=closeout_issue,
            git_evidence=closeout_git,
            ready_items=closeout_ready,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(
            proxy_command["completion_allowed"] is False,
            "proxy-only command should block closeout admission",
        )

        missing_artifact_evidence = json.loads(json.dumps(closeout_evidence))
        missing_artifact_evidence["artifacts"][1]["path"] = "missing-completion-audit.md"
        missing_artifact = closeout_adapter_audit(
            objective=closeout_objective,
            evidence=missing_artifact_evidence,
            issue=closeout_issue,
            git_evidence=closeout_git,
            ready_items=closeout_ready,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(missing_artifact["completion_allowed"] is False, "missing artifact should block")

        stale_close_issue = json.loads(json.dumps(closeout_issue))
        stale_close_issue["close_reason"] = ""
        stale_close = closeout_adapter_audit(
            objective=closeout_objective,
            evidence=closeout_evidence,
            issue=stale_close_issue,
            git_evidence=closeout_git,
            ready_items=closeout_ready,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(stale_close["completion_allowed"] is False, "stale close reason should block")
        assert_condition(
            any(req["issue"] and "close_reason" in req["issue"] for req in stale_close["requirements"]),
            "stale close reason should be visible on a requirement",
        )

        dirty_git = json.loads(json.dumps(closeout_git))
        dirty_git["git_status"]["dirty_files"] = ["scripts/build_completion_audit.py"]
        dirty_worktree = closeout_adapter_audit(
            objective=closeout_objective,
            evidence=closeout_evidence,
            issue=closeout_issue,
            git_evidence=dirty_git,
            ready_items=closeout_ready,
            repo_root=tmpdir,
            generated_at=fixed_now,
        )
        assert_condition(dirty_worktree["completion_allowed"] is False, "dirty worktree should block when required clean")
        assert_condition(
            any(req["issue"] and "dirty files" in req["issue"] for req in dirty_worktree["requirements"]),
            "dirty worktree should be visible on a requirement",
        )

    print("SELF-TEST PASS: completion audit fixtures are conservative")
    return 0


def load_objective(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if args.objective_file is not None:
        parts.append(read_text(args.objective_file))
    if args.objective_text:
        parts.append(args.objective_text)
    if args.bead_json is not None:
        issue = first_object(read_json_value(args.bead_json), source=str(args.bead_json))
        title = issue.get("title")
        description = issue.get("description")
        if title or description:
            parts.append(f"# {title or issue.get('id')}\n\n{description or ''}")
    if args.bead_id:
        result = subprocess.run(
            ["br", "show", args.bead_id, "--json"],
            cwd=args.repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise AuditError(f"failed to read bead {args.bead_id}: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        issue = payload[0] if isinstance(payload, list) and payload else payload
        if isinstance(issue, dict):
            title = issue.get("title")
            description = issue.get("description")
            parts.append(f"# {title}\n\n{description or ''}")
    if not parts:
        raise AuditError("provide --objective-file, --objective-text, or --bead-id")
    return "\n\n".join(parts)


def load_evidence(args: argparse.Namespace, objective: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    if args.evidence_json is not None:
        evidence = read_json(args.evidence_json)
    if args.capture_git:
        evidence = merge_evidence(evidence, capture_git_evidence(args.repo_root))
    if args.closeout_admission:
        evidence = merge_evidence(
            evidence,
            build_closeout_admission_evidence_from_args(args, objective),
        )
    return evidence


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objective-file", type=Path, help="Markdown objective or acceptance criteria")
    parser.add_argument("--objective-text", help="Inline objective text")
    parser.add_argument("--bead-id", help="Read bead title and description with br show")
    parser.add_argument("--bead-json", type=Path, help="Use a br show JSON fixture for objective and closeout admission evidence")
    parser.add_argument("--ready-json", type=Path, help="Use a br ready JSON fixture for closeout admission successors")
    parser.add_argument("--git-evidence-json", type=Path, help="Use git evidence JSON instead of running git read commands")
    parser.add_argument("--agent-mail-summary-json", type=Path, help="Optional Agent Mail fixture summary to embed in closeout admission evidence")
    parser.add_argument("--rch-summary-json", type=Path, help="Optional RCH fixture summary to embed in closeout admission evidence")
    parser.add_argument("--evidence-json", type=Path, help="JSON evidence bundle")
    parser.add_argument("--out-json", type=Path, help="Write audit JSON")
    parser.add_argument("--out-md", type=Path, help="Write audit Markdown")
    parser.add_argument("--repo-root", type=Path, default=Path("."), help="Repository root")
    parser.add_argument("--capture-git", action="store_true", help="Add current git status/latest commit evidence")
    parser.add_argument("--closeout-admission", action="store_true", help="Merge read-only Beads/git closeout admission evidence")
    parser.add_argument("--require-clean-worktree", action="store_true", help="Block closeout admission when git evidence has dirty files")
    parser.add_argument("--self-test", action="store_true", help="Run fixture-backed self-test")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.self_test:
        return run_self_test()
    args.repo_root = args.repo_root.resolve()
    objective = load_objective(args)
    evidence = load_evidence(args, objective)
    audit = build_audit(objective=objective, evidence=evidence, repo_root=args.repo_root)
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(stable_json(audit), encoding="utf-8")
    if args.out_md is not None:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        args.out_md.write_text(render_markdown(audit), encoding="utf-8")
    if args.out_json is None and args.out_md is None:
        print(stable_json(audit), end="")
    return 0 if audit["completion_allowed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
