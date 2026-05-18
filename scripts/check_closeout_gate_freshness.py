#!/usr/bin/env python3
"""Audit closeout-gate evidence freshness without mutating repo state.

This guard checks prompt-to-artifact closeout gates after they have been
generated. It validates that closeout evidence still lines up with the current
repository: child beads remain closed, referenced commits exist, required
contract checks are still present, and README closeout-gate references do not
point at superseded evidence.

Usage:
    python3 scripts/check_closeout_gate_freshness.py
    python3 scripts/check_closeout_gate_freshness.py --self-test

Exit codes:
    0 - all audited closeout gates pass
    1 - one or more freshness checks failed
    2 - script error or self-test failure
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


AUDIT_SCHEMA = "pi.closeout_gate_freshness_audit.v1"
CONTRACT_GLOB = "docs/contracts/*closeout-gate-contract.json"
EVIDENCE_GLOB = "docs/evidence/*closeout-gate*.json"
REGISTRY_SCHEMA = "pi.closeout_evidence_registry.v1"
REGISTRY_PATH = Path("docs/evidence/closeout-evidence-registry.json")
README_CLOSEOUT_ARTIFACT_RE = re.compile(
    r"docs/evidence/[A-Za-z0-9._/-]*closeout-gate[A-Za-z0-9._/-]*\.json"
)
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
COMMIT_KEY_RE = re.compile(r"(commit|head|origin_main|origin_master|origin_legacy_mirror)")


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    message: str
    remediation: str
    path: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "severity": self.severity,
            "check": self.check,
            "message": self.message,
            "remediation": self.remediation,
        }
        if self.path is not None:
            payload["path"] = self.path
        return payload


@dataclass
class ArtifactReport:
    path: str
    schema: str | None = None
    generated_at: str | None = None
    contract_path: str | None = None
    status: str = "pass"
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)
        if finding.severity == "fail":
            self.status = "fail"

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "schema": self.schema,
            "generated_at": self.generated_at,
            "contract_path": self.contract_path,
            "status": self.status,
            "findings": [finding.to_json() for finding in self.findings],
        }


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_iso_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", value)
    if match:
        prefix, fraction, suffix = match.groups()
        value = f"{prefix}.{fraction[:6].ljust(6, '0')}{suffix}"
    try:
        return as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def load_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def relpath(repo_root: Path, path: Path) -> str:
    return path.relative_to(repo_root).as_posix()


def git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def commit_exists(repo_root: Path, commit: str) -> bool:
    result = git(repo_root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    return result.returncode == 0


def commit_is_ancestor(repo_root: Path, commit: str) -> bool:
    result = git(repo_root, ["merge-base", "--is-ancestor", commit, "HEAD"])
    return result.returncode == 0


def current_head(repo_root: Path) -> str | None:
    result = git(repo_root, ["rev-parse", "HEAD"])
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def load_bead_statuses(repo_root: Path) -> dict[str, dict[str, Any]]:
    issues_path = repo_root / ".beads/issues.jsonl"
    statuses: dict[str, dict[str, Any]] = {}
    if not issues_path.exists():
        return statuses
    with issues_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{issues_path}:{line_number}: invalid JSONL: {exc}") from exc
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                statuses[payload["id"]] = payload
    return statuses


def list_json_paths(repo_root: Path, pattern: str) -> list[Path]:
    return sorted(path for path in repo_root.glob(pattern) if path.is_file())


def read_contracts(repo_root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    contracts: list[dict[str, Any]] = []
    by_decision_schema: dict[str, dict[str, Any]] = {}
    for path in list_json_paths(repo_root, CONTRACT_GLOB):
        payload = load_json_object(path)
        payload["_path"] = relpath(repo_root, path)
        contracts.append(payload)
        decision_schema = payload.get("decision_gate_schema")
        if isinstance(decision_schema, str):
            by_decision_schema[decision_schema] = payload
    return contracts, by_decision_schema


def values_for_key(payload: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(payload, dict):
        for child_key, value in payload.items():
            if child_key == key:
                found.append(value)
            found.extend(values_for_key(value, key))
    elif isinstance(payload, list):
        for value in payload:
            found.extend(values_for_key(value, key))
    return found


def iter_commit_refs(payload: Any, path: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            next_path = (*path, key)
            if isinstance(value, str) and COMMIT_RE.fullmatch(value) and COMMIT_KEY_RE.search(key):
                refs.append((".".join(next_path), value))
            refs.extend(iter_commit_refs(value, next_path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            refs.extend(iter_commit_refs(value, (*path, str(index))))
    return refs


def iter_sha256_path_refs(payload: Any, path: tuple[str, ...] = ()) -> list[tuple[str, str, str]]:
    refs: list[tuple[str, str, str]] = []
    if isinstance(payload, dict):
        raw_path = payload.get("path") or payload.get("source_path")
        raw_hash = payload.get("sha256") or payload.get("source_hash")
        if isinstance(raw_path, str) and isinstance(raw_hash, str) and re.fullmatch(r"[0-9a-f]{64}", raw_hash):
            refs.append((".".join(path), raw_path, raw_hash))
        for key, value in payload.items():
            refs.extend(iter_sha256_path_refs(value, (*path, key)))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            refs.extend(iter_sha256_path_refs(value, (*path, str(index))))
    return refs


def check_required_keys(
    report: ArtifactReport,
    evidence: dict[str, Any],
    contract: dict[str, Any] | None,
) -> None:
    if contract is None:
        return
    for key in contract.get("required_top_level_keys") or []:
        if isinstance(key, str) and key not in evidence:
            report.add(Finding(
                "fail",
                "required_top_level_key",
                f"missing contract-required top-level key {key!r}",
                "Regenerate the closeout gate with the current contract or update the contract/evidence together.",
            ))


def ids_from_entries(entries: Any, id_key: str = "id") -> set[str]:
    result: set[str] = set()
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get(id_key), str):
                result.add(entry[id_key])
            elif isinstance(entry, str):
                result.add(entry)
    return result


def check_contract_required_ids(
    report: ArtifactReport,
    evidence: dict[str, Any],
    contract: dict[str, Any] | None,
) -> None:
    if contract is None:
        return
    child_entries = []
    for field_name in ("child_artifact_map", "child_closeout"):
        entries = evidence.get(field_name)
        if isinstance(entries, list):
            child_entries.extend(entry for entry in entries if isinstance(entry, dict))
    child_ids = {
        bead_id
        for entry in child_entries
        for bead_id in (entry.get("bead_id"), entry.get("bead"))
        if isinstance(bead_id, str)
    }
    for bead_id in contract.get("required_child_bead_ids") or []:
        if isinstance(bead_id, str) and bead_id not in child_ids:
            report.add(Finding(
                "fail",
                "required_child_bead",
                f"contract-required child bead {bead_id} is missing from child_artifact_map",
                "Regenerate the evidence so every required child bead maps to code/tests/docs, validation commands, and commit.",
            ))

    quality_gate_ids = ids_from_entries(evidence.get("quality_gate_results"))
    for gate_id in contract.get("required_quality_gate_ids") or []:
        if isinstance(gate_id, str) and gate_id not in quality_gate_ids:
            report.add(Finding(
                "fail",
                "required_quality_gate",
                f"contract-required quality gate {gate_id} is missing",
                "Regenerate the closeout gate after running the required quality gate.",
            ))

    checklist_ids = ids_from_entries(evidence.get("checklist"))
    required_checks = set(
        value for value in evidence.get("required_checks") or [] if isinstance(value, str)
    )
    for check_id in contract.get("required_check_ids") or []:
        if isinstance(check_id, str) and check_id not in checklist_ids and check_id not in required_checks:
            report.add(Finding(
                "fail",
                "required_check",
                f"contract-required check {check_id} is missing from checklist/required_checks",
                "Regenerate the closeout gate with the current checklist contract.",
            ))


def check_generated_at(
    report: ArtifactReport,
    evidence: dict[str, Any],
    now: datetime,
    max_age: timedelta,
) -> datetime | None:
    generated_at = parse_iso_datetime(evidence.get("generated_at"))
    if generated_at is None:
        report.add(Finding(
            "fail",
            "generated_at",
            "missing or unparsable generated_at timestamp",
            "Regenerate the closeout evidence with a parseable RFC3339 generated_at timestamp.",
        ))
        return None
    if generated_at - now > timedelta(minutes=5):
        report.add(Finding(
            "fail",
            "generated_at_future",
            f"generated_at is in the future: {evidence.get('generated_at')}",
            "Regenerate the artifact on a host with a sane clock.",
        ))
    if now - generated_at > max_age:
        days = (now - generated_at).total_seconds() / 86400
        report.add(Finding(
            "fail",
            "generated_at_stale",
            f"generated_at is stale: {days:.1f} days old",
            "Regenerate the closeout evidence or file follow-up beads before declaring the queue converged.",
        ))
    return generated_at


def check_quality_gates(report: ArtifactReport, evidence: dict[str, Any]) -> None:
    entries: list[dict[str, Any]] = []
    for field_name in ("quality_gate_results", "quality_gates"):
        raw_entries = evidence.get(field_name)
        if isinstance(raw_entries, list):
            entries.extend(entry for entry in raw_entries if isinstance(entry, dict))
    for entry in entries:
        gate_id = str(entry.get("id") or entry.get("command") or "<missing-id>")
        status = str(entry.get("status") or entry.get("result") or "").lower()
        command = entry.get("command")
        if status and not status.startswith("pass"):
            report.add(Finding(
                "fail",
                "quality_gate_status",
                f"quality gate {gate_id} is not pass: {status}",
                "Re-run or update the failing gate before trusting the closeout artifact.",
            ))
        if not isinstance(command, str) or not command.strip():
            report.add(Finding(
                "fail",
                "quality_gate_command",
                f"quality gate {gate_id} is missing a validation command",
                "Regenerate the artifact with the exact command that produced the gate evidence.",
            ))


def bead_id_from(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return value["id"]
    return None


def check_bead_status(
    report: ArtifactReport,
    bead_statuses: dict[str, dict[str, Any]],
    bead_id: str,
    check: str,
) -> None:
    current = bead_statuses.get(bead_id)
    if current is None:
        report.add(Finding(
            "fail",
            check,
            f"bead {bead_id} is referenced but missing from .beads/issues.jsonl",
            "Restore the bead record or regenerate the closeout artifact against the current Beads ledger.",
        ))
        return
    if current.get("status") != "closed":
        report.add(Finding(
            "fail",
            check,
            f"bead {bead_id} is currently {current.get('status')!r}, not closed",
            "Close the bead with evidence, or regenerate the closeout gate with follow_up_required=true.",
        ))


def check_beads_and_child_map(
    report: ArtifactReport,
    evidence: dict[str, Any],
    bead_statuses: dict[str, dict[str, Any]],
) -> None:
    for field_name, check_name in (
        ("final_gate_bead", "final_gate_bead_status"),
        ("parent_epic", "parent_epic_status"),
        ("source_bead", "source_bead_status"),
    ):
        bead_id = bead_id_from(evidence.get(field_name))
        if bead_id is not None:
            check_bead_status(report, bead_statuses, bead_id, check_name)

    for entry in evidence.get("child_artifact_map") or []:
        if not isinstance(entry, dict):
            continue
        bead_id = entry.get("bead_id")
        if not isinstance(bead_id, str):
            report.add(Finding(
                "fail",
                "child_bead_id",
                "child_artifact_map entry is missing bead_id",
                "Regenerate the closeout gate with explicit child bead IDs.",
            ))
            continue
        if entry.get("status") != "closed":
            report.add(Finding(
                "fail",
                "child_artifact_status",
                f"child_artifact_map records {bead_id} as {entry.get('status')!r}, not closed",
                "Regenerate the artifact after closing the child bead or mark follow-up work explicitly.",
            ))
        check_bead_status(report, bead_statuses, bead_id, "child_bead_current_status")
        commands = entry.get("validation_commands")
        if not isinstance(commands, list) or not any(isinstance(command, str) and command.strip() for command in commands):
            report.add(Finding(
                "fail",
                "child_validation_command",
                f"child bead {bead_id} is missing validation_commands",
                "Record the focused validation command(s) used for the child bead.",
            ))
        commit = entry.get("commit")
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            report.add(Finding(
                "fail",
                "child_commit",
                f"child bead {bead_id} is missing a commit hash",
                "Record the pushed commit hash for this child bead.",
            ))

    for entry in evidence.get("child_closeout") or []:
        if not isinstance(entry, dict):
            continue
        bead_id = entry.get("bead")
        if not isinstance(bead_id, str):
            report.add(Finding(
                "fail",
                "child_bead_id",
                "child_closeout entry is missing bead",
                "Regenerate the closeout gate with explicit child bead IDs.",
            ))
            continue
        if entry.get("status") != "closed":
            report.add(Finding(
                "fail",
                "child_artifact_status",
                f"child_closeout records {bead_id} as {entry.get('status')!r}, not closed",
                "Regenerate the artifact after closing the child bead or mark follow-up work explicitly.",
            ))
        if entry.get("result") != "pass":
            report.add(Finding(
                "fail",
                "child_closeout_result",
                f"child_closeout records {bead_id} result={entry.get('result')!r}, not pass",
                "Regenerate the artifact after the child closeout evidence passes.",
            ))
        if not isinstance(entry.get("evidence"), str) or not entry.get("evidence"):
            report.add(Finding(
                "fail",
                "child_evidence",
                f"child_closeout records {bead_id} without evidence",
                "Record the child evidence artifact or focused validation surface.",
            ))
        check_bead_status(report, bead_statuses, bead_id, "child_bead_current_status")


def check_commits(report: ArtifactReport, repo_root: Path, evidence: dict[str, Any]) -> None:
    seen: set[str] = set()
    for path, commit in iter_commit_refs(evidence):
        if commit in seen:
            continue
        seen.add(commit)
        if not commit_exists(repo_root, commit):
            report.add(Finding(
                "fail",
                "missing_commit",
                f"referenced commit {commit} at {path} does not exist",
                "Regenerate the artifact from a repository that contains every referenced commit.",
                path=path,
            ))
        elif not commit_is_ancestor(repo_root, commit):
            report.add(Finding(
                "fail",
                "stale_source_hash",
                f"referenced commit {commit} at {path} is not reachable from current HEAD",
                "Rebase/regenerate the closeout gate against current HEAD before relying on it.",
                path=path,
            ))


def check_sha256_refs(report: ArtifactReport, repo_root: Path, evidence: dict[str, Any]) -> None:
    for pointer, raw_path, expected_hash in iter_sha256_path_refs(evidence):
        candidate = (repo_root / raw_path).resolve()
        try:
            candidate.relative_to(repo_root.resolve())
        except ValueError:
            report.add(Finding(
                "fail",
                "source_hash_path",
                f"hashed source path escapes repository: {raw_path}",
                "Regenerate the artifact with repository-relative source paths only.",
                path=pointer,
            ))
            continue
        if not candidate.exists() or not candidate.is_file():
            report.add(Finding(
                "fail",
                "source_hash_missing_path",
                f"hashed source path is missing: {raw_path}",
                "Regenerate the artifact or restore the referenced source file.",
                path=pointer,
            ))
            continue
        actual_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            report.add(Finding(
                "fail",
                "stale_source_hash",
                f"source hash mismatch for {raw_path}",
                "Regenerate the artifact after the source file change, or refresh the source file from the recorded evidence.",
                path=pointer,
            ))


def check_missing_checks(report: ArtifactReport, evidence: dict[str, Any]) -> None:
    missing = evidence.get("missing_checks")
    if isinstance(missing, list) and missing:
        report.add(Finding(
            "fail",
            "missing_checks",
            f"artifact reports missing_checks={missing!r}",
            "File or complete follow-up work before treating this closeout gate as fresh.",
        ))
    if evidence.get("follow_up_required") is True:
        report.add(Finding(
            "fail",
            "follow_up_required",
            "artifact reports follow_up_required=true",
            "Create/complete the required follow-up beads before declaring closeout freshness.",
        ))


def audit_artifact(
    repo_root: Path,
    path: Path,
    contracts_by_schema: dict[str, dict[str, Any]],
    bead_statuses: dict[str, dict[str, Any]],
    now: datetime,
    max_age: timedelta,
) -> tuple[ArtifactReport, datetime | None]:
    rel = relpath(repo_root, path)
    report = ArtifactReport(path=rel)
    try:
        evidence = load_json_object(path)
    except Exception as exc:
        report.add(Finding(
            "fail",
            "json_parse",
            f"failed to parse JSON object: {exc}",
            "Fix the artifact JSON or regenerate it.",
        ))
        return report, None

    schema = evidence.get("schema") if isinstance(evidence.get("schema"), str) else None
    generated_raw = evidence.get("generated_at") if isinstance(evidence.get("generated_at"), str) else None
    report.schema = schema
    report.generated_at = generated_raw
    contract = contracts_by_schema.get(schema or "")
    if contract is not None:
        report.contract_path = contract.get("_path")
    else:
        report.add(Finding(
            "fail",
            "missing_contract",
            f"no closeout-gate contract found for schema {schema!r}",
            "Add a docs/contracts/*closeout-gate-contract.json entry whose decision_gate_schema matches this artifact schema.",
        ))

    generated_at = check_generated_at(report, evidence, now, max_age)
    check_required_keys(report, evidence, contract)
    check_contract_required_ids(report, evidence, contract)
    check_quality_gates(report, evidence)
    check_beads_and_child_map(report, evidence, bead_statuses)
    check_commits(report, repo_root, evidence)
    check_sha256_refs(report, repo_root, evidence)
    check_missing_checks(report, evidence)
    return report, generated_at


def audit_readme_refs(
    repo_root: Path,
    artifacts_by_path: dict[str, tuple[str | None, datetime | None]],
    freshest_by_schema: dict[str, tuple[str, datetime]],
) -> list[Finding]:
    readme_path = repo_root / "README.md"
    if not readme_path.exists():
        return []
    readme = readme_path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    for line_number, line in enumerate(readme.splitlines(), start=1):
        for match in README_CLOSEOUT_ARTIFACT_RE.finditer(line):
            artifact_path = match.group(0)
            artifact_info = artifacts_by_path.get(artifact_path)
            if artifact_info is None:
                findings.append(Finding(
                    "fail",
                    "readme_missing_artifact",
                    f"README line {line_number} references missing closeout artifact {artifact_path}",
                    "Update README.md to reference a checked-in closeout artifact, or regenerate the missing artifact.",
                    path=f"README.md:{line_number}",
                ))
                continue
            schema, generated_at = artifact_info
            if schema is None or generated_at is None:
                continue
            freshest = freshest_by_schema.get(schema)
            if freshest is None:
                continue
            freshest_path, freshest_generated_at = freshest
            if artifact_path != freshest_path and generated_at < freshest_generated_at:
                findings.append(Finding(
                    "fail",
                    "readme_older_artifact",
                    (
                        f"README line {line_number} references {artifact_path}, but "
                        f"{freshest_path} is newer for schema {schema}"
                    ),
                    "Update README.md to point at the freshest closeout evidence for that schema.",
                    path=f"README.md:{line_number}",
                ))
    return findings


def markdown_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    readme_path = repo_root / "README.md"
    if readme_path.exists():
        files.append(readme_path)
    docs_path = repo_root / "docs"
    if docs_path.exists():
        files.extend(sorted(path for path in docs_path.rglob("*.md") if path.is_file()))
    return files


def collect_markdown_closeout_refs(repo_root: Path) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for markdown_path in markdown_files(repo_root):
        rel = relpath(repo_root, markdown_path)
        text = markdown_path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in README_CLOSEOUT_ARTIFACT_RE.finditer(line):
                refs.append(
                    {
                        "artifact_path": match.group(0),
                        "source_path": rel,
                        "line": line_number,
                    }
                )
    return refs


def validate_registry_entry(
    *,
    repo_root: Path,
    entry: Any,
    index: int,
    artifact_by_path: dict[str, ArtifactReport],
    seen_paths: set[str],
) -> tuple[str | None, list[Finding]]:
    findings: list[Finding] = []
    pointer = f"{REGISTRY_PATH.as_posix()}:current_artifacts[{index}]"
    if not isinstance(entry, dict):
        findings.append(Finding(
            "fail",
            "registry_entry_shape",
            f"registry entry {index} is not an object",
            "Replace the registry entry with an object containing path, contract_path, decision_gate_schema, source_bead_or_epic, reference_paths, and advisory_claim_boundary.",
            path=pointer,
        ))
        return None, findings

    artifact_path = entry.get("path")
    if not isinstance(artifact_path, str) or not artifact_path:
        findings.append(Finding(
            "fail",
            "registry_entry_path",
            f"registry entry {index} has no artifact path",
            "Set current_artifacts[].path to a repository-relative closeout evidence artifact.",
            path=pointer,
        ))
        return None, findings

    if artifact_path in seen_paths:
        findings.append(Finding(
            "fail",
            "registry_duplicate_path",
            f"registry repeats closeout artifact {artifact_path}",
            "Keep exactly one current registry entry per closeout evidence artifact.",
            path=pointer,
        ))
    seen_paths.add(artifact_path)

    report = artifact_by_path.get(artifact_path)
    artifact_file = repo_root / artifact_path
    if report is None or not artifact_file.exists():
        findings.append(Finding(
            "fail",
            "registry_missing_artifact",
            f"registry points at missing or unaudited artifact {artifact_path}",
            "Restore the artifact, update the path, or move the entry to historical_artifacts if it is intentionally superseded.",
            path=pointer,
        ))

    contract_path = entry.get("contract_path")
    if not isinstance(contract_path, str) or not contract_path:
        findings.append(Finding(
            "fail",
            "registry_contract_path",
            f"registry entry for {artifact_path} has no contract_path",
            "Set contract_path to the governing docs/contracts/*closeout-gate-contract.json file.",
            path=pointer,
        ))
    elif not (repo_root / contract_path).exists():
        findings.append(Finding(
            "fail",
            "registry_missing_contract_file",
            f"registry contract path is missing: {contract_path}",
            "Restore the governing contract or update the registry entry.",
            path=pointer,
        ))
    elif report is not None and report.contract_path != contract_path:
        findings.append(Finding(
            "fail",
            "registry_contract_mismatch",
            f"registry maps {artifact_path} to {contract_path}, but audit resolved {report.contract_path}",
            "Update the registry contract_path so it matches the contract decision_gate_schema for the artifact.",
            path=pointer,
        ))

    decision_schema = entry.get("decision_gate_schema")
    if not isinstance(decision_schema, str) or not decision_schema:
        findings.append(Finding(
            "fail",
            "registry_decision_gate_schema",
            f"registry entry for {artifact_path} has no decision_gate_schema",
            "Set decision_gate_schema to the artifact schema governed by the contract.",
            path=pointer,
        ))
    elif report is not None and report.schema != decision_schema:
        findings.append(Finding(
            "fail",
            "registry_decision_schema_mismatch",
            f"registry maps {artifact_path} to schema {decision_schema}, but artifact schema is {report.schema}",
            "Update the registry decision_gate_schema or regenerate the artifact/contract pair.",
            path=pointer,
        ))

    source = entry.get("source_bead_or_epic")
    if not isinstance(source, str) or not source:
        findings.append(Finding(
            "fail",
            "registry_source_missing",
            f"registry entry for {artifact_path} has no source_bead_or_epic",
            "Record the source bead or epic id that the closeout artifact summarizes.",
            path=pointer,
        ))

    references = entry.get("reference_paths")
    if not isinstance(references, list) or any(not isinstance(item, str) for item in references):
        findings.append(Finding(
            "fail",
            "registry_reference_paths",
            f"registry entry for {artifact_path} has invalid reference_paths",
            "Set reference_paths to a list of README/runbook/docs paths that intentionally cite this artifact, or an empty list when it is registry-only.",
            path=pointer,
        ))

    claim_boundary = entry.get("advisory_claim_boundary")
    if not isinstance(claim_boundary, str) or not claim_boundary.strip():
        findings.append(Finding(
            "fail",
            "registry_claim_boundary",
            f"registry entry for {artifact_path} has no advisory claim boundary",
            "Describe the advisory-only boundary so this registry cannot be mistaken for release, performance, drop-in, or source-of-truth evidence.",
            path=pointer,
        ))

    return artifact_path, findings


def audit_closeout_registry(
    repo_root: Path,
    artifact_reports: list[ArtifactReport],
) -> dict[str, Any]:
    registry_file = repo_root / REGISTRY_PATH
    artifact_by_path = {report.path: report for report in artifact_reports}
    markdown_refs = collect_markdown_closeout_refs(repo_root)
    findings: list[Finding] = []
    payload: dict[str, Any] | None = None

    try:
        payload = load_json_object(registry_file)
    except Exception as exc:
        findings.append(Finding(
            "fail",
            "registry_parse",
            f"failed to load closeout evidence registry: {exc}",
            "Create or repair docs/evidence/closeout-evidence-registry.json with schema pi.closeout_evidence_registry.v1.",
            path=REGISTRY_PATH.as_posix(),
        ))
        return {
            "path": REGISTRY_PATH.as_posix(),
            "schema": None,
            "status": "fail",
            "current_artifact_count": 0,
            "historical_artifact_count": 0,
            "markdown_reference_count": len(markdown_refs),
            "findings": [finding.to_json() for finding in findings],
        }

    schema = payload.get("schema")
    if schema != REGISTRY_SCHEMA:
        findings.append(Finding(
            "fail",
            "registry_schema",
            f"registry schema is {schema!r}, expected {REGISTRY_SCHEMA}",
            "Set the registry schema to pi.closeout_evidence_registry.v1.",
            path=REGISTRY_PATH.as_posix(),
        ))

    current_entries = payload.get("current_artifacts")
    if not isinstance(current_entries, list):
        findings.append(Finding(
            "fail",
            "registry_current_artifacts",
            "registry current_artifacts is not a list",
            "List every current closeout evidence artifact under current_artifacts.",
            path=REGISTRY_PATH.as_posix(),
        ))
        current_entries = []

    historical_entries = payload.get("historical_artifacts")
    if not isinstance(historical_entries, list):
        findings.append(Finding(
            "fail",
            "registry_historical_artifacts",
            "registry historical_artifacts is not a list",
            "Keep historical/superseded closeout artifacts in historical_artifacts, even if the list is empty.",
            path=REGISTRY_PATH.as_posix(),
        ))
        historical_entries = []

    registered_paths: set[str] = set()
    for index, entry in enumerate(current_entries):
        artifact_path, entry_findings = validate_registry_entry(
            repo_root=repo_root,
            entry=entry,
            index=index,
            artifact_by_path=artifact_by_path,
            seen_paths=registered_paths,
        )
        findings.extend(entry_findings)
        if artifact_path is not None:
            registered_paths.add(artifact_path)

    for artifact_path in sorted(set(artifact_by_path) - registered_paths):
        findings.append(Finding(
            "fail",
            "registry_unregistered_artifact",
            f"current closeout artifact is not registered: {artifact_path}",
            "Add the artifact to current_artifacts with its contract, decision schema, source bead or epic, references, and advisory claim boundary.",
            path=artifact_path,
        ))

    for ref in markdown_refs:
        artifact_path = ref["artifact_path"]
        if artifact_path not in registered_paths:
            findings.append(Finding(
                "fail",
                "markdown_unregistered_closeout_artifact",
                f"{ref['source_path']}:{ref['line']} references unregistered closeout artifact {artifact_path}",
                "Register the referenced closeout artifact as current, move it to historical_artifacts and update the prose, or update the Markdown reference.",
                path=f"{ref['source_path']}:{ref['line']}",
            ))

    status = "fail" if any(finding.severity == "fail" for finding in findings) else "pass"
    return {
        "path": REGISTRY_PATH.as_posix(),
        "schema": schema,
        "status": status,
        "current_artifact_count": len(current_entries),
        "historical_artifact_count": len(historical_entries),
        "markdown_reference_count": len(markdown_refs),
        "findings": [finding.to_json() for finding in findings],
    }


def build_summary(
    repo_root: Path,
    now: datetime,
    max_age_days: int,
) -> tuple[int, dict[str, Any]]:
    repo_root = repo_root.resolve()
    contracts, contracts_by_schema = read_contracts(repo_root)
    bead_statuses = load_bead_statuses(repo_root)
    artifact_reports: list[ArtifactReport] = []
    artifacts_by_path: dict[str, tuple[str | None, datetime | None]] = {}
    freshest_by_schema: dict[str, tuple[str, datetime]] = {}

    max_age = timedelta(days=max_age_days)
    for path in list_json_paths(repo_root, EVIDENCE_GLOB):
        report, generated_at = audit_artifact(
            repo_root,
            path,
            contracts_by_schema,
            bead_statuses,
            now,
            max_age,
        )
        artifact_reports.append(report)
        artifacts_by_path[report.path] = (report.schema, generated_at)
        if report.schema is not None and generated_at is not None:
            existing = freshest_by_schema.get(report.schema)
            if existing is None or generated_at > existing[1]:
                freshest_by_schema[report.schema] = (report.path, generated_at)

    readme_findings = audit_readme_refs(repo_root, artifacts_by_path, freshest_by_schema)
    registry = audit_closeout_registry(repo_root, artifact_reports)
    fail_count = sum(1 for report in artifact_reports for finding in report.findings if finding.severity == "fail")
    fail_count += sum(1 for finding in readme_findings if finding.severity == "fail")
    fail_count += sum(1 for finding in registry["findings"] if finding.get("severity") == "fail")
    status = "fail" if fail_count else "pass"
    payload = {
        "schema": AUDIT_SCHEMA,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "status": status,
        "repo_root": str(repo_root),
        "head": current_head(repo_root),
        "max_age_days": max_age_days,
        "contracts": [
            {
                "path": contract.get("_path"),
                "schema": contract.get("schema"),
                "decision_gate_schema": contract.get("decision_gate_schema"),
            }
            for contract in contracts
        ],
        "artifacts": [report.to_json() for report in artifact_reports],
        "registry": registry,
        "readme_findings": [finding.to_json() for finding in readme_findings],
        "summary": {
            "contract_count": len(contracts),
            "artifact_count": len(artifact_reports),
            "registry_status": registry["status"],
            "registry_current_artifact_count": registry["current_artifact_count"],
            "markdown_reference_count": registry["markdown_reference_count"],
            "artifact_fail_count": sum(1 for report in artifact_reports if report.status == "fail"),
            "finding_fail_count": fail_count,
        },
    }
    return (0 if status == "pass" else 1), payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def run_checked(args: list[str], cwd: Path) -> None:
    result = subprocess.run(args, cwd=cwd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise RuntimeError(f"{args} failed\nstdout={result.stdout}\nstderr={result.stderr}")


def initialize_fixture_repo(root: Path, now: datetime) -> str:
    run_checked(["git", "init", "-q", "-b", "main"], root)
    run_checked(["git", "config", "user.email", "fixture@example.invalid"], root)
    run_checked(["git", "config", "user.name", "Fixture"], root)
    tracked = root / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    run_checked(["git", "add", "tracked.txt"], root)
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = now.isoformat()
    env["GIT_COMMITTER_DATE"] = now.isoformat()
    result = subprocess.run(
        ["git", "commit", "-q", "-m", "fixture"],
        cwd=root,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git commit failed\nstdout={result.stdout}\nstderr={result.stderr}")
    head = git(root, ["rev-parse", "HEAD"]).stdout.strip()
    return head


def registry_entry(
    *,
    path: str,
    contract_path: str,
    decision_gate_schema: str,
    source_bead_or_epic: str,
    reference_paths: list[str],
) -> dict[str, Any]:
    return {
        "path": path,
        "contract_path": contract_path,
        "decision_gate_schema": decision_gate_schema,
        "source_bead_or_epic": source_bead_or_epic,
        "reference_paths": reference_paths,
        "advisory_claim_boundary": (
            "Advisory closeout evidence only; does not replace Beads, git, RCH, "
            "Agent Mail, CI, UBS, release certification, or source files."
        ),
    }


def write_fixture_registry(root: Path, entries: list[dict[str, Any]]) -> None:
    write_json(
        root / REGISTRY_PATH,
        {
            "schema": REGISTRY_SCHEMA,
            "current_artifacts": entries,
            "historical_artifacts": [],
            "claim_boundary": (
                "This registry indexes current closeout proof artifacts. It is not a "
                "release manifest, performance evidence bundle, Beads source of truth, "
                "Agent Mail authority, or permission to mutate source files."
            ),
        },
    )


def read_fixture_registry_entries(root: Path) -> list[dict[str, Any]]:
    registry = load_json_object(root / REGISTRY_PATH)
    entries = registry.get("current_artifacts")
    return entries if isinstance(entries, list) else []


def create_non_ancestor_commit(root: Path, now: datetime) -> str:
    run_checked(["git", "switch", "-q", "-c", "stale-proof"], root)
    stale_source = root / "stale-proof.txt"
    stale_source.write_text("stale proof\n", encoding="utf-8")
    run_checked(["git", "add", "stale-proof.txt"], root)
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = (now + timedelta(minutes=1)).isoformat()
    env["GIT_COMMITTER_DATE"] = (now + timedelta(minutes=1)).isoformat()
    result = subprocess.run(
        ["git", "commit", "-q", "-m", "stale proof"],
        cwd=root,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(f"stale proof commit failed\nstdout={result.stdout}\nstderr={result.stderr}")
    stale_commit = git(root, ["rev-parse", "HEAD"]).stdout.strip()
    run_checked(["git", "switch", "-q", "main"], root)
    return stale_commit


def write_fixture(root: Path, now: datetime, commit: str, generated_at: datetime | None = None) -> None:
    generated_at = generated_at or now
    contract = {
        "schema": "pi.demo.closeout_gate_contract.v1",
        "decision_gate_schema": "pi.demo.closeout_gate.v1",
        "required_top_level_keys": [
            "schema",
            "generated_at",
            "status",
            "child_artifact_map",
            "quality_gate_results",
        ],
        "required_child_bead_ids": ["bd-demo.1"],
        "required_quality_gate_ids": ["unit"],
        "required_check_ids": ["child_beads_closed"],
    }
    evidence = {
        "schema": "pi.demo.closeout_gate.v1",
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "status": "pass",
        "required_checks": ["child_beads_closed"],
        "final_gate_bead": {"id": "bd-demo.2"},
        "parent_epic": {"id": "bd-demo"},
        "child_artifact_map": [
            {
                "bead_id": "bd-demo.1",
                "status": "closed",
                "commit": commit,
                "validation_commands": ["python3 -m json.tool docs/evidence/demo-closeout-gate.json"],
            }
        ],
        "quality_gate_results": [
            {
                "id": "unit",
                "status": "pass",
                "command": "python3 scripts/check_closeout_gate_freshness.py --self-test",
            }
        ],
        "checklist": [{"id": "child_beads_closed", "status": "pass"}],
        "missing_checks": [],
    }
    write_json(root / "docs/contracts/demo-closeout-gate-contract.json", contract)
    write_json(root / "docs/evidence/demo-closeout-gate.json", evidence)
    (root / ".beads").mkdir(parents=True, exist_ok=True)
    (root / ".beads/issues.jsonl").write_text(
        "\n".join(
            json.dumps({"id": bead_id, "status": "closed"})
            for bead_id in ("bd-demo", "bd-demo.1", "bd-demo.2")
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "Current closeout evidence: docs/evidence/demo-closeout-gate.json\n",
        encoding="utf-8",
    )
    write_fixture_registry(
        root,
        [
            registry_entry(
                path="docs/evidence/demo-closeout-gate.json",
                contract_path="docs/contracts/demo-closeout-gate-contract.json",
                decision_gate_schema="pi.demo.closeout_gate.v1",
                source_bead_or_epic="bd-demo",
                reference_paths=["README.md"],
            )
        ],
    )


def write_legacy_shape_fixture(root: Path, now: datetime) -> None:
    contract = {
        "schema": "pi.demo.legacy_closeout_gate_contract.v1",
        "decision_gate_schema": "pi.demo.legacy_closeout_gate.v1",
        "required_top_level_keys": [
            "schema",
            "generated_at",
            "child_closeout",
            "quality_gates",
            "missing_checks",
        ],
        "required_child_bead_ids": ["bd-legacy.1"],
        "required_quality_gate_ids": [],
        "required_check_ids": [],
    }
    evidence = {
        "schema": "pi.demo.legacy_closeout_gate.v1",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "child_closeout": [
            {
                "bead": "bd-legacy.1",
                "status": "closed",
                "result": "pass",
                "evidence": "docs/evidence/legacy-closeout-gate.json",
            }
        ],
        "quality_gates": [
            {
                "command": "python3 scripts/check_closeout_gate_freshness.py --self-test",
                "result": "pass",
            }
        ],
        "missing_checks": [],
    }
    write_json(root / "docs/contracts/legacy-closeout-gate-contract.json", contract)
    write_json(root / "docs/evidence/legacy-closeout-gate.json", evidence)
    with (root / ".beads/issues.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "bd-legacy.1", "status": "closed"}) + "\n")
    entries = read_fixture_registry_entries(root)
    entries.append(
        registry_entry(
            path="docs/evidence/legacy-closeout-gate.json",
            contract_path="docs/contracts/legacy-closeout-gate-contract.json",
            decision_gate_schema="pi.demo.legacy_closeout_gate.v1",
            source_bead_or_epic="bd-legacy",
            reference_paths=[],
        )
    )
    write_fixture_registry(root, entries)


def run_self_test() -> int:
    now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    root = Path(tempfile.mkdtemp(prefix="pi-closeout-gate-freshness-"))
    try:
        commit = initialize_fixture_repo(root, now)
        write_fixture(root, now, commit)

        first_status, first = build_summary(root, now, max_age_days=14)
        if first_status != 0 or first["status"] != "pass":
            print(json.dumps(first, indent=2))
            print("SELF-TEST FAIL: valid fixture should pass")
            return 2

        registry_missing_artifact = load_json_object(root / REGISTRY_PATH)
        registry_missing_artifact["current_artifacts"][0]["path"] = "docs/evidence/missing-closeout-gate.json"
        write_json(root / REGISTRY_PATH, registry_missing_artifact)
        registry_missing_status, registry_missing = build_summary(root, now, max_age_days=14)
        if registry_missing_status != 1 or "registry_missing_artifact" not in json.dumps(registry_missing):
            print(json.dumps(registry_missing, indent=2))
            print("SELF-TEST FAIL: registry missing artifact should fail")
            return 2

        write_fixture(root, now, commit)
        registry_contract_mismatch = load_json_object(root / REGISTRY_PATH)
        registry_contract_mismatch["current_artifacts"][0]["contract_path"] = (
            "docs/contracts/missing-closeout-gate-contract.json"
        )
        write_json(root / REGISTRY_PATH, registry_contract_mismatch)
        registry_contract_status, registry_contract = build_summary(root, now, max_age_days=14)
        if registry_contract_status != 1 or "registry_missing_contract_file" not in json.dumps(registry_contract):
            print(json.dumps(registry_contract, indent=2))
            print("SELF-TEST FAIL: registry missing contract file should fail")
            return 2

        write_fixture(root, now, commit)
        (root / "README.md").write_text(
            "Current closeout evidence: docs/evidence/unregistered-closeout-gate.json\n",
            encoding="utf-8",
        )
        markdown_registry_status, markdown_registry = build_summary(root, now, max_age_days=14)
        if markdown_registry_status != 1 or "markdown_unregistered_closeout_artifact" not in json.dumps(markdown_registry):
            print(json.dumps(markdown_registry, indent=2))
            print("SELF-TEST FAIL: Markdown reference to unregistered closeout artifact should fail")
            return 2

        write_fixture(root, now, commit)
        write_legacy_shape_fixture(root, now)
        legacy_status, legacy = build_summary(root, now, max_age_days=14)
        if legacy_status != 0 or "legacy-closeout-gate.json" not in json.dumps(legacy):
            print(json.dumps(legacy, indent=2))
            print("SELF-TEST FAIL: legacy child_closeout/quality_gates shape should pass")
            return 2

        write_fixture(root, now, commit, generated_at=now - timedelta(days=30))
        stale_status, stale = build_summary(root, now, max_age_days=14)
        if stale_status != 1 or "generated_at_stale" not in json.dumps(stale):
            print(json.dumps(stale, indent=2))
            print("SELF-TEST FAIL: stale generated_at should fail")
            return 2

        write_fixture(root, now, "0" * 40)
        missing_commit_status, missing_commit = build_summary(root, now, max_age_days=14)
        if missing_commit_status != 1 or "missing_commit" not in json.dumps(missing_commit):
            print(json.dumps(missing_commit, indent=2))
            print("SELF-TEST FAIL: missing commit should fail")
            return 2

        stale_commit = create_non_ancestor_commit(root, now)
        write_fixture(root, now, stale_commit)
        stale_commit_status, stale_commit_report = build_summary(root, now, max_age_days=14)
        if stale_commit_status != 1 or "stale_source_hash" not in json.dumps(stale_commit_report):
            print(json.dumps(stale_commit_report, indent=2))
            print("SELF-TEST FAIL: non-ancestor commit should fail")
            return 2

        write_fixture(root, now, commit)
        write_json(
            root / "docs/evidence/uncontracted-closeout-gate.json",
            {
                "schema": "pi.demo.uncontracted_closeout_gate.v1",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "missing_checks": [],
            },
        )
        missing_contract_status, missing_contract = build_summary(root, now, max_age_days=14)
        if missing_contract_status != 1 or "missing_contract" not in json.dumps(missing_contract):
            print(json.dumps(missing_contract, indent=2))
            print("SELF-TEST FAIL: uncontracted closeout evidence should fail")
            return 2

        write_fixture(root, now, commit)
        write_json(
            root / "docs/contracts/wrong-schema-closeout-gate-contract.json",
            {
                "schema": "pi.demo.wrong_schema_closeout_gate_contract.v1",
                "decision_gate_schema": "pi.demo.some_other_closeout_gate.v1",
                "required_top_level_keys": ["schema", "generated_at", "missing_checks"],
            },
        )
        write_json(
            root / "docs/evidence/wrong-schema-closeout-gate.json",
            {
                "schema": "pi.demo.wrong_schema_closeout_gate.v1",
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "missing_checks": [],
            },
        )
        wrong_schema_status, wrong_schema = build_summary(root, now, max_age_days=14)
        if wrong_schema_status != 1 or "missing_contract" not in json.dumps(wrong_schema):
            print(json.dumps(wrong_schema, indent=2))
            print("SELF-TEST FAIL: contract with wrong decision_gate_schema should fail")
            return 2

        write_fixture(root, now, commit)
        hash_payload = load_json_object(root / "docs/evidence/demo-closeout-gate.json")
        hash_payload["source_fingerprints"] = [
            {"path": "tracked.txt", "sha256": "0" * 64}
        ]
        write_json(root / "docs/evidence/demo-closeout-gate.json", hash_payload)
        hash_status, hash_report = build_summary(root, now, max_age_days=14)
        if hash_status != 1 or "stale_source_hash" not in json.dumps(hash_report):
            print(json.dumps(hash_report, indent=2))
            print("SELF-TEST FAIL: stale sha256 source fingerprint should fail")
            return 2

        write_fixture(root, now, commit)
        missing_child_payload = load_json_object(root / "docs/evidence/demo-closeout-gate.json")
        missing_child_payload["child_artifact_map"] = []
        write_json(root / "docs/evidence/demo-closeout-gate.json", missing_child_payload)
        missing_child_status, missing_child = build_summary(root, now, max_age_days=14)
        if missing_child_status != 1 or "required_child_bead" not in json.dumps(missing_child):
            print(json.dumps(missing_child, indent=2))
            print("SELF-TEST FAIL: missing contract-required child bead should fail")
            return 2

        write_fixture(root, now, commit)
        (root / "docs/evidence/malformed-closeout-gate.json").write_text("{", encoding="utf-8")
        malformed_status, malformed = build_summary(root, now, max_age_days=14)
        if malformed_status != 1 or "json_parse" not in json.dumps(malformed):
            print(json.dumps(malformed, indent=2))
            print("SELF-TEST FAIL: malformed closeout JSON should produce json_parse finding")
            return 2

        write_fixture(root, now, commit)
        (root / ".beads/issues.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"id": "bd-demo", "status": "closed"}),
                    json.dumps({"id": "bd-demo.1", "status": "open"}),
                    json.dumps({"id": "bd-demo.2", "status": "closed"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        reopened_status, reopened = build_summary(root, now, max_age_days=14)
        if reopened_status != 1 or "child_bead_current_status" not in json.dumps(reopened):
            print(json.dumps(reopened, indent=2))
            print("SELF-TEST FAIL: reopened child bead should fail")
            return 2

        write_fixture(root, now, commit)
        old_path = root / "docs/evidence/demo-old-closeout-gate.json"
        old_payload = load_json_object(root / "docs/evidence/demo-closeout-gate.json")
        old_payload["generated_at"] = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        write_json(old_path, old_payload)
        (root / "README.md").write_text(
            "Current closeout evidence: docs/evidence/demo-old-closeout-gate.json\n",
            encoding="utf-8",
        )
        readme_status, readme = build_summary(root, now, max_age_days=14)
        if readme_status != 1 or "readme_older_artifact" not in json.dumps(readme):
            print(json.dumps(readme, indent=2))
            print("SELF-TEST FAIL: README reference to older artifact should fail")
            return 2
    except Exception as exc:
        print(f"SELF-TEST ERROR in {root}: {exc}")
        return 2

    print(f"SELF-TEST PASS: fixture repo left at {root}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--max-age-days", type=int, default=14)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    now = datetime.now(timezone.utc)
    try:
        status, payload = build_summary(args.repo_root, now, args.max_age_days)
    except Exception as exc:
        error_payload = {
            "schema": AUDIT_SCHEMA,
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "status": "error",
            "error": str(exc),
        }
        print(json.dumps(error_payload, indent=None if args.compact else 2))
        return 2
    print(json.dumps(payload, indent=None if args.compact else 2))
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
