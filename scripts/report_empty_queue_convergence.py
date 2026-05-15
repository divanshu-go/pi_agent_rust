#!/usr/bin/env python3
"""Report whether the Beads queue has actually converged.

This is a read-only operator report. It packages the manual proof agents run at
session boundaries: Beads queue state, optional br/bv robot output, degraded
Agent Mail/RCH signals, and closeout-gate freshness. It never mutates Beads,
git, Agent Mail, RCH, runpack inputs, or source files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPORT_SCHEMA = "pi.swarm.empty_queue_convergence_report.v1"
POLICY = "read_only_no_mutation"
IW_DRIFT_T1_PREFIX = "IW-DRIFT-T1:"
DEFAULT_STALE_IN_PROGRESS_HOURS = 2
NON_BLOCKING_DEP_TYPES = {"parent-child", "related"}
DEFERRED_PLANNING_LABELS = {
    "idea-wizard",
    "planning",
    "roadmap",
}
DEFERRED_PLANNING_TERMS = (
    "backlog",
    "idea-wizard",
    "roadmap",
    "work_to_plan",
)
AGENT_MAIL_SCHEMA_CORRUPT_FIXTURE = Path(
    "tests/fixtures/agent_mail/schema_corrupt_health.json"
)


@dataclass(frozen=True)
class LoadedJson:
    path: str | None
    payload: Any | None
    error: str | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_dumps(payload: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def parse_iso_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_json(path: Path | None) -> LoadedJson:
    if path is None:
        return LoadedJson(path=None, payload=None, error=None)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return LoadedJson(path=str(path), payload=None, error=str(exc))
    try:
        return LoadedJson(path=str(path), payload=json.loads(text), error=None)
    except json.JSONDecodeError as exc:
        return LoadedJson(path=str(path), payload=None, error=f"invalid JSON: {exc}")


def read_fixture_json(repo_root: Path, relative_path: Path) -> LoadedJson:
    return read_json(repo_root / relative_path)


def read_issues(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    issues: list[dict[str, Any]] = []
    if not path.exists():
        return issues, f"{path}: file does not exist"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return issues, str(exc)
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return issues, f"{path}:{line_number}: invalid JSONL record: {exc}"
        if isinstance(record, dict):
            issues.append(record)
    return issues, None


def normalize_issue_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("issues", "items", "ready", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def issue_summary(issue: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    updated_at = parse_iso_datetime(issue.get("updated_at"))
    age_hours = None
    if now is not None and updated_at is not None:
        age_hours = round((now - updated_at).total_seconds() / 3600, 2)
    summary = {
        "id": issue.get("id"),
        "title": issue.get("title"),
        "status": issue.get("status"),
        "priority": issue.get("priority"),
        "issue_type": issue.get("issue_type"),
        "assignee": issue.get("assignee"),
        "updated_at": issue.get("updated_at"),
        "labels": issue.get("labels", []),
    }
    if age_hours is not None:
        summary["age_hours"] = age_hours
    return summary


def dependency_blocks(issue: dict[str, Any], issue_by_id: dict[str, dict[str, Any]]) -> bool:
    for dep in issue.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        dep_type = str(dep.get("type", "")).strip()
        if dep_type in NON_BLOCKING_DEP_TYPES:
            continue
        depends_on = dep.get("depends_on_id") or dep.get("id")
        blocker = issue_by_id.get(str(depends_on))
        blocker_status = str(blocker.get("status", "")) if blocker else "missing"
        if blocker_status not in {"closed", "tombstone"}:
            return True
    return False


def compute_ready_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issue_by_id = {str(issue.get("id")): issue for issue in issues if issue.get("id")}
    ready: list[dict[str, Any]] = []
    for issue in issues:
        if issue.get("status") != "open":
            continue
        if dependency_blocks(issue, issue_by_id):
            continue
        ready.append(issue)
    return sorted(ready, key=lambda item: (int(item.get("priority", 99)), str(item.get("id", ""))))


def child_issues_by_parent(issues: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    children: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        for dep in issue.get("dependencies") or []:
            if not isinstance(dep, dict):
                continue
            if dep.get("type") != "parent-child":
                continue
            parent_id = dep.get("depends_on_id") or dep.get("id")
            if parent_id:
                children.setdefault(str(parent_id), []).append(issue)
    return children


def is_deferred_planning_epic(issue: dict[str, Any]) -> bool:
    if issue.get("status") != "deferred":
        return False
    if issue.get("issue_type") != "epic":
        return False
    labels = {str(label).lower() for label in issue.get("labels") or []}
    if labels & DEFERRED_PLANNING_LABELS:
        return True
    haystack = " ".join(
        str(issue.get(field, "")).lower()
        for field in ("title", "description", "notes")
    )
    return any(term in haystack for term in DEFERRED_PLANNING_TERMS)


def deferred_planning_items(
    issues: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    children_by_parent = child_issues_by_parent(issues)
    items: list[dict[str, Any]] = []
    for issue in issues:
        if not is_deferred_planning_epic(issue):
            continue
        children = children_by_parent.get(str(issue.get("id")), [])
        child_statuses: dict[str, int] = {}
        for child in children:
            status = str(child.get("status", "unknown"))
            child_statuses[status] = child_statuses.get(status, 0) + 1
        active_child_count = child_statuses.get("open", 0) + child_statuses.get("in_progress", 0)
        if active_child_count:
            continue
        items.append(
            {
                **issue_summary(issue, now=now),
                "child_count": len(children),
                "child_statuses": dict(sorted(child_statuses.items())),
                "planning_reason": "deferred roadmap/planning epic has no open or in-progress child work",
            }
        )
    return sorted(items, key=lambda item: (int(item.get("priority", 99)), str(item.get("id", ""))))


def analyze_beads(
    issues: list[dict[str, Any]],
    *,
    br_ready: list[dict[str, Any]] | None,
    now: datetime,
    stale_hours: float,
) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for issue in issues:
        status = str(issue.get("status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1

    ready_source = "br_ready_json" if br_ready is not None else "computed_from_beads_jsonl"
    ready_issues = br_ready if br_ready is not None else compute_ready_issues(issues)
    in_progress = [issue for issue in issues if issue.get("status") == "in_progress"]
    deferred = [issue for issue in issues if issue.get("status") == "deferred"]
    tombstones = [issue for issue in issues if issue.get("status") == "tombstone"]
    planning_items = deferred_planning_items(issues, now=now)

    stale_in_progress = []
    for issue in in_progress:
        updated_at = parse_iso_datetime(issue.get("updated_at"))
        if updated_at is None:
            stale_in_progress.append({**issue_summary(issue, now=now), "stale_reason": "missing_updated_at"})
            continue
        age_hours = (now - updated_at).total_seconds() / 3600
        if age_hours >= stale_hours:
            stale_in_progress.append({**issue_summary(issue, now=now), "stale_reason": "age_exceeds_threshold"})

    return {
        "source": ready_source,
        "total_count": len(issues),
        "by_status": dict(sorted(by_status.items())),
        "ready_count": len(ready_issues),
        "open_count": by_status.get("open", 0),
        "in_progress_count": len(in_progress),
        "deferred_count": len(deferred),
        "deferred_planning_count": len(planning_items),
        "tombstone_count": len(tombstones),
        "stale_in_progress_count": len(stale_in_progress),
        "ready_items": [issue_summary(issue, now=now) for issue in ready_issues[:25]],
        "in_progress_items": [issue_summary(issue, now=now) for issue in in_progress[:25]],
        "stale_in_progress_items": stale_in_progress[:25],
        "deferred_only_items": [issue_summary(issue, now=now) for issue in deferred[:25]],
        "deferred_planning_items": planning_items[:25],
    }


def iter_bv_plan_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    tracks = ((payload.get("plan") or {}).get("tracks")) if isinstance(payload.get("plan"), dict) else []
    if not isinstance(tracks, list):
        return []
    items: list[dict[str, Any]] = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        for item in track.get("items") or []:
            if isinstance(item, dict):
                items.append(item)
    return items


def analyze_bv(payload: Any, issue_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if payload is None:
        return {
            "status": "unavailable",
            "source": None,
            "actionable_count": None,
            "tombstone_mismatch_count": 0,
            "tombstone_mismatches": [],
            "warnings": ["no bv robot JSON supplied"],
        }
    items = iter_bv_plan_items(payload)
    mismatches: list[dict[str, Any]] = []
    for item in items:
        issue_id = str(item.get("id", ""))
        issue = issue_by_id.get(issue_id)
        item_status = str(item.get("status", ""))
        live_status = str(issue.get("status", "missing")) if issue else "missing"
        if item_status == "tombstone" or live_status == "tombstone":
            mismatches.append(
                {
                    "id": issue_id,
                    "title": item.get("title") or (issue or {}).get("title"),
                    "bv_status": item_status,
                    "beads_status": live_status,
                }
            )
    return {
        "status": "ok" if not mismatches else "mismatch",
        "source": "bv_plan_json",
        "actionable_count": len(items),
        "tombstone_mismatch_count": len(mismatches),
        "tombstone_mismatches": mismatches,
        "warnings": [],
    }


def analyze_agent_mail(payload: Any, source_path: str | None, error: str | None) -> dict[str, Any]:
    if error is not None:
        return {
            "status": "unavailable",
            "source": source_path,
            "degraded": True,
            "health_level": None,
            "semantic_readiness": None,
            "recovery_mode": None,
            "recovery_action": None,
            "warnings": [error],
            "recommended_operator_action": "Use Beads status/comments as the coordination fallback.",
        }
    if payload is None:
        return {
            "status": "unavailable",
            "source": None,
            "degraded": False,
            "health_level": None,
            "semantic_readiness": None,
            "recovery_mode": None,
            "recovery_action": None,
            "warnings": ["no Agent Mail health JSON supplied"],
            "recommended_operator_action": "Optional: attach Agent Mail health JSON for coordination posture.",
        }
    status = str(payload.get("status", "unknown")) if isinstance(payload, dict) else "invalid"
    health_level = payload.get("health_level") if isinstance(payload, dict) else None
    semantic = payload.get("semantic_readiness") if isinstance(payload, dict) else None
    recovery = payload.get("recovery") if isinstance(payload, dict) else None
    semantic_status = semantic.get("status") if isinstance(semantic, dict) else None
    semantic_detail = semantic.get("detail") if isinstance(semantic, dict) else None
    recovery_mode = recovery.get("mode") if isinstance(recovery, dict) else None
    recovery_action = recovery.get("next_action") if isinstance(recovery, dict) else None
    degraded = status not in {"ok", "healthy"} or health_level in {"red", "degraded", "yellow"} or semantic_status == "fail"
    return {
        "status": "degraded" if degraded else "ok",
        "source": source_path,
        "degraded": degraded,
        "health_level": health_level,
        "semantic_readiness": semantic_status,
        "semantic_readiness_detail": semantic_detail,
        "recovery_mode": recovery_mode,
        "recovery_action": recovery_action,
        "warnings": [] if not degraded else ["Agent Mail is degraded; this does not block Beads-only progress."],
        "recommended_operator_action": (
            "Use Beads status/comments as the soft lock until Agent Mail is repaired."
            if degraded
            else "Agent Mail appears usable."
        ),
    }


def analyze_optional_status(payload: Any, source_path: str | None, error: str | None, name: str) -> dict[str, Any]:
    if error is not None:
        return {"status": "unavailable", "source": source_path, "warnings": [error]}
    if payload is None:
        return {
            "status": "unavailable",
            "source": None,
            "warnings": [f"no {name} JSON supplied"],
        }
    if isinstance(payload, dict):
        raw_status = payload.get("status") or payload.get("health_level") or payload.get("decision")
        return {
            "status": str(raw_status or "present"),
            "source": source_path,
            "schema": payload.get("schema"),
            "warnings": [],
        }
    return {"status": "invalid", "source": source_path, "warnings": [f"{name} JSON root is not an object"]}


def find_iw_drift_t1(issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    for issue in issues:
        title = str(issue.get("title", ""))
        if title.startswith(IW_DRIFT_T1_PREFIX):
            return issue
    return None


def analyze_closeout(
    payload: Any,
    source_path: str | None,
    error: str | None,
    iw_drift_t1: dict[str, Any] | None,
) -> dict[str, Any]:
    base = {
        "source": source_path,
        "linked_freshness_bead": issue_summary(iw_drift_t1) if iw_drift_t1 else None,
    }
    if error is not None:
        return {
            **base,
            "status": "unavailable",
            "schema": None,
            "warnings": [error],
            "recommended_operator_action": "Regenerate or provide closeout freshness JSON before declaring an empty queue clean.",
        }
    if payload is None:
        action = (
            f"Work or close {iw_drift_t1.get('id')} before using closeout freshness in empty-queue proof."
            if iw_drift_t1 and iw_drift_t1.get("status") != "closed"
            else "Provide closeout freshness JSON when available."
        )
        return {
            **base,
            "status": "unavailable",
            "schema": None,
            "warnings": ["no closeout freshness JSON supplied"],
            "recommended_operator_action": action,
        }
    status = str(payload.get("status", "unknown")) if isinstance(payload, dict) else "invalid"
    return {
        **base,
        "status": status,
        "schema": payload.get("schema") if isinstance(payload, dict) else None,
        "warnings": [] if status in {"pass", "ready", "ok"} else ["closeout freshness is not passing"],
        "recommended_operator_action": (
            "Closeout freshness supports empty-queue proof."
            if status in {"pass", "ready", "ok"}
            else "Refresh closeout-gate evidence before declaring the queue clean."
        ),
    }


def build_next_actions(
    beads: dict[str, Any],
    bv: dict[str, Any],
    agent_mail: dict[str, Any],
    closeout: dict[str, Any],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if beads["ready_count"]:
        first = beads["ready_items"][0]
        actions.append(
            {
                "action": "start_ready_bead",
                "issue_id": first.get("id"),
                "reason": "ready Beads work exists; claim the highest-value non-overlapping item.",
            }
        )
    if beads["stale_in_progress_count"]:
        actions.append(
            {
                "action": "review_stale_in_progress",
                "issue_ids": [item.get("id") for item in beads["stale_in_progress_items"]],
                "reason": "in-progress work exceeded the stale threshold.",
            }
        )
    if bv["tombstone_mismatch_count"]:
        actions.append(
            {
                "action": "ignore_or_repair_bv_tombstone_mismatch",
                "issue_ids": [item.get("id") for item in bv["tombstone_mismatches"]],
                "reason": "bv surfaced tombstoned work; tombstones are not actionable.",
            }
        )
    if agent_mail["degraded"]:
        actions.append(
            {
                "action": "use_beads_fallback",
                "reason": agent_mail["recommended_operator_action"],
            }
        )
    if not beads["ready_count"] and not beads["in_progress_count"]:
        if beads["deferred_planning_count"]:
            actions.append(
                {
                    "action": "create_or_refine_backlog",
                    "issue_ids": [item.get("id") for item in beads["deferred_planning_items"]],
                    "epics": [
                        {"id": item.get("id"), "title": item.get("title")}
                        for item in beads["deferred_planning_items"]
                    ],
                    "reason": (
                        "deferred roadmap/planning epics remain without open child work; create "
                        "or refine actionable child Beads before declaring the queue clean."
                    ),
                }
            )
        elif closeout["status"] in {"pass", "ready", "ok"}:
            actions.append(
                {
                    "action": "stop_queue_clean",
                    "reason": "no ready or in-progress work remains and closeout freshness is passing.",
                }
            )
        else:
            actions.append(
                {
                    "action": "create_or_finish_audit_bead",
                    "reason": closeout["recommended_operator_action"],
                }
            )
    if not actions:
        actions.append(
            {
                "action": "continue_monitoring",
                "reason": "no immediate corrective action was selected.",
            }
        )
    return actions


def overall_status(beads: dict[str, Any], bv: dict[str, Any], closeout: dict[str, Any]) -> str:
    if beads["stale_in_progress_count"] or bv["tombstone_mismatch_count"]:
        return "needs_attention"
    if beads["ready_count"]:
        return "ready_work_available"
    if beads["in_progress_count"]:
        return "work_in_progress"
    if beads["deferred_planning_count"]:
        return "work_to_plan"
    if closeout["status"] in {"pass", "ready", "ok"}:
        return "queue_clean"
    return "needs_attention"


def build_report(
    *,
    repo_root: Path,
    beads_path: Path,
    br_ready_json: LoadedJson,
    bv_plan_json: LoadedJson,
    agent_mail_health_json: LoadedJson,
    rch_json: LoadedJson,
    closeout_freshness_json: LoadedJson,
    now: datetime,
    stale_hours: float,
) -> dict[str, Any]:
    issues, issues_error = read_issues(beads_path)
    br_ready = None
    if br_ready_json.error is None and br_ready_json.payload is not None:
        br_ready = normalize_issue_records(br_ready_json.payload)

    beads = analyze_beads(issues, br_ready=br_ready, now=now, stale_hours=stale_hours)
    if issues_error is not None:
        beads["load_error"] = issues_error
    issue_by_id = {str(issue.get("id")): issue for issue in issues if issue.get("id")}

    bv = analyze_bv(bv_plan_json.payload, issue_by_id)
    if bv_plan_json.error is not None:
        bv["status"] = "unavailable"
        bv["warnings"] = [bv_plan_json.error]
    bv["source_path"] = bv_plan_json.path

    agent_mail = analyze_agent_mail(
        agent_mail_health_json.payload,
        agent_mail_health_json.path,
        agent_mail_health_json.error,
    )
    rch = analyze_optional_status(rch_json.payload, rch_json.path, rch_json.error, "RCH")
    closeout = analyze_closeout(
        closeout_freshness_json.payload,
        closeout_freshness_json.path,
        closeout_freshness_json.error,
        find_iw_drift_t1(issues),
    )

    actions = build_next_actions(beads, bv, agent_mail, closeout)
    status = overall_status(beads, bv, closeout)
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": status,
        "policy": POLICY,
        "source_root": str(repo_root),
        "source_files": {
            "beads": str(beads_path),
            "br_ready_json": br_ready_json.path,
            "bv_plan_json": bv_plan_json.path,
            "agent_mail_health_json": agent_mail_health_json.path,
            "rch_json": rch_json.path,
            "closeout_freshness_json": closeout_freshness_json.path,
        },
        "thresholds": {
            "stale_in_progress_hours": stale_hours,
        },
        "summary": {
            "ready_count": beads["ready_count"],
            "in_progress_count": beads["in_progress_count"],
            "deferred_count": beads["deferred_count"],
            "deferred_planning_count": beads["deferred_planning_count"],
            "tombstone_count": beads["tombstone_count"],
            "bv_actionable_count": bv["actionable_count"],
            "bv_tombstone_mismatch_count": bv["tombstone_mismatch_count"],
            "agent_mail_status": agent_mail["status"],
            "rch_status": rch["status"],
            "closeout_freshness_status": closeout["status"],
        },
        "beads": beads,
        "bv": bv,
        "coordination": {
            "agent_mail": agent_mail,
            "rch": rch,
        },
        "closeout_freshness": closeout,
        "next_actions": actions,
    }


def print_text_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(
        "status={status} ready={ready} in_progress={in_progress} deferred={deferred} "
        "deferred_planning={deferred_planning} bv_tombstone_mismatches={mismatches} "
        "agent_mail={agent_mail} closeout={closeout}".format(
            status=report["status"],
            ready=summary["ready_count"],
            in_progress=summary["in_progress_count"],
            deferred=summary["deferred_count"],
            deferred_planning=summary["deferred_planning_count"],
            mismatches=summary["bv_tombstone_mismatch_count"],
            agent_mail=summary["agent_mail_status"],
            closeout=summary["closeout_freshness_status"],
        )
    )
    for action in report["next_actions"]:
        issue = f" issue={action['issue_id']}" if action.get("issue_id") else ""
        issues = ""
        if action.get("issue_ids"):
            issues = " issues=" + ",".join(str(item) for item in action["issue_ids"])
        print(f"- next_action={action['action']}{issue}{issues}: {action['reason']}")


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(payload, pretty=True), encoding="utf-8")
    return path


def write_issues(path: Path, issues: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json_dumps(issue) for issue in issues), encoding="utf-8")
    return path


def assert_condition(condition: bool, message: str, report: dict[str, Any] | None = None) -> None:
    if condition:
        return
    if report is not None:
        sys.stderr.write(json_dumps(report, pretty=True))
    raise AssertionError(message)


def fixture_issue(
    issue_id: str,
    title: str,
    *,
    status: str = "open",
    priority: int = 2,
    updated_at: str = "2026-05-15T11:30:00Z",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "title": title,
        "description": "fixture",
        "status": status,
        "priority": priority,
        "issue_type": "task",
        "updated_at": updated_at,
        "labels": labels or [],
    }


def run_self_test() -> int:
    now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory(prefix="pi_empty_queue_convergence_") as temp_dir:
        root = Path(temp_dir)
        beads_path = root / ".beads/issues.jsonl"
        ready_issue = fixture_issue("bd-ready", "Ready work", priority=1)
        drift_t1 = fixture_issue("bd-fresh", "IW-DRIFT-T1: Add closeout-gate freshness auditor")
        write_issues(beads_path, [ready_issue, drift_t1])
        ready_json = write_json(root / "ready.json", [ready_issue])
        bv_json = write_json(
            root / "bv.json",
            {"plan": {"tracks": [{"items": [{"id": "bd-ready", "status": "open", "title": "Ready work"}]}]}},
        )
        report = build_report(
            repo_root=root,
            beads_path=beads_path,
            br_ready_json=read_json(ready_json),
            bv_plan_json=read_json(bv_json),
            agent_mail_health_json=LoadedJson(None, None, None),
            rch_json=LoadedJson(None, None, None),
            closeout_freshness_json=LoadedJson(None, None, None),
            now=now,
            stale_hours=DEFAULT_STALE_IN_PROGRESS_HOURS,
        )
        assert_condition(report["status"] == "ready_work_available", "ready work should win", report)
        assert_condition(report["summary"]["ready_count"] == 1, "ready count should come from br ready", report)

        clean_beads = root / "clean/.beads/issues.jsonl"
        deferred = fixture_issue("bd-epic", "Deferred epic", status="deferred")
        write_issues(clean_beads, [deferred])
        closeout_json = write_json(root / "closeout.json", {"schema": "fixture.closeout.v1", "status": "pass"})
        clean_report = build_report(
            repo_root=root,
            beads_path=clean_beads,
            br_ready_json=LoadedJson(None, [], None),
            bv_plan_json=LoadedJson(None, {"plan": {"tracks": []}}, None),
            agent_mail_health_json=LoadedJson(None, None, None),
            rch_json=LoadedJson(None, None, None),
            closeout_freshness_json=read_json(closeout_json),
            now=now,
            stale_hours=DEFAULT_STALE_IN_PROGRESS_HOURS,
        )
        assert_condition(clean_report["status"] == "queue_clean", "empty queue should be clean", clean_report)
        assert_condition(
            clean_report["next_actions"][0]["action"] == "stop_queue_clean",
            "clean queue should tell operator to stop",
            clean_report,
        )

        planning_beads = root / "planning/.beads/issues.jsonl"
        planning_epic = fixture_issue(
            "bd-plan",
            "Swarm operations follow-up roadmap",
            status="deferred",
            labels=["idea-wizard", "swarm"],
        )
        planning_epic["issue_type"] = "epic"
        closed_child = fixture_issue(
            "bd-plan.1",
            "Closed child",
            status="closed",
        )
        closed_child["dependencies"] = [
            {"type": "parent-child", "depends_on_id": "bd-plan"},
        ]
        write_issues(planning_beads, [planning_epic, closed_child])
        planning_report = build_report(
            repo_root=root,
            beads_path=planning_beads,
            br_ready_json=LoadedJson(None, [], None),
            bv_plan_json=LoadedJson(None, {"plan": {"tracks": []}}, None),
            agent_mail_health_json=LoadedJson(None, None, None),
            rch_json=LoadedJson(None, None, None),
            closeout_freshness_json=read_json(closeout_json),
            now=now,
            stale_hours=DEFAULT_STALE_IN_PROGRESS_HOURS,
        )
        assert_condition(
            planning_report["status"] == "work_to_plan",
            "deferred planning epic should block queue_clean",
            planning_report,
        )
        assert_condition(
            planning_report["summary"]["deferred_planning_count"] == 1,
            "planning epic should be counted explicitly",
            planning_report,
        )
        assert_condition(
            planning_report["next_actions"][0]["action"] == "create_or_refine_backlog",
            "planning epic should ask for backlog refinement",
            planning_report,
        )
        assert_condition(
            planning_report["next_actions"][0]["issue_ids"] == ["bd-plan"],
            "planning next action should name the deferred epic",
            planning_report,
        )

        stale_beads = root / "stale/.beads/issues.jsonl"
        stale = fixture_issue(
            "bd-stale",
            "Stale work",
            status="in_progress",
            updated_at="2026-05-15T08:30:00Z",
        )
        write_issues(stale_beads, [stale])
        stale_report = build_report(
            repo_root=root,
            beads_path=stale_beads,
            br_ready_json=LoadedJson(None, [], None),
            bv_plan_json=LoadedJson(None, {"plan": {"tracks": []}}, None),
            agent_mail_health_json=LoadedJson(None, None, None),
            rch_json=LoadedJson(None, None, None),
            closeout_freshness_json=LoadedJson(None, None, None),
            now=now,
            stale_hours=DEFAULT_STALE_IN_PROGRESS_HOURS,
        )
        assert_condition(stale_report["status"] == "needs_attention", "stale work should need attention", stale_report)
        assert_condition(
            stale_report["summary"]["in_progress_count"] == 1,
            "stale report should count in-progress work",
            stale_report,
        )

        tombstone_beads = root / "tombstone/.beads/issues.jsonl"
        tombstone = fixture_issue("bd-old", "Deleted item", status="tombstone")
        write_issues(tombstone_beads, [tombstone])
        tombstone_report = build_report(
            repo_root=root,
            beads_path=tombstone_beads,
            br_ready_json=LoadedJson(None, [], None),
            bv_plan_json=LoadedJson(None, {"plan": {"tracks": [{"items": [{"id": "bd-old", "status": "tombstone"}]}]}}, None),
            agent_mail_health_json=LoadedJson(None, None, None),
            rch_json=LoadedJson(None, None, None),
            closeout_freshness_json=LoadedJson(None, None, None),
            now=now,
            stale_hours=DEFAULT_STALE_IN_PROGRESS_HOURS,
        )
        assert_condition(
            tombstone_report["summary"]["bv_tombstone_mismatch_count"] == 1,
            "bv tombstone mismatch should be explicit",
            tombstone_report,
        )

        corrupt_mail = read_fixture_json(
            Path(__file__).resolve().parents[1],
            AGENT_MAIL_SCHEMA_CORRUPT_FIXTURE,
        )
        mail_report = build_report(
            repo_root=root,
            beads_path=beads_path,
            br_ready_json=read_json(ready_json),
            bv_plan_json=read_json(bv_json),
            agent_mail_health_json=corrupt_mail,
            rch_json=LoadedJson(None, {"status": "ok", "schema": "fixture.rch.v1"}, None),
            closeout_freshness_json=LoadedJson(None, None, None),
            now=now,
            stale_hours=DEFAULT_STALE_IN_PROGRESS_HOURS,
        )
        assert_condition(
            mail_report["coordination"]["agent_mail"]["status"] == "degraded",
            "corrupt Agent Mail should be degraded, not fatal",
            mail_report,
        )
        assert_condition(
            mail_report["status"] == "ready_work_available",
            "corrupt Agent Mail should not hide ready Beads work",
            mail_report,
        )
        assert_condition(
            any(action["action"] == "use_beads_fallback" for action in mail_report["next_actions"]),
            "corrupt Agent Mail should recommend Beads fallback",
            mail_report,
        )
        assert_condition(
            mail_report["coordination"]["agent_mail"]["semantic_readiness_detail"]
            == "sqlite schema missing required health_check tables: projects, agents, messages, message_recipients",
            "corrupt Agent Mail fixture should preserve missing-table detail",
            mail_report,
        )
        assert_condition(
            mail_report["coordination"]["agent_mail"]["recovery_action"]
            == "Run `am doctor repair --yes` or restore from archive backup",
            "corrupt Agent Mail fixture should preserve exact recovery action",
            mail_report,
        )
    print("SELF-TEST PASS")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root. Defaults to this script's parent repository.",
    )
    parser.add_argument(
        "--beads-jsonl",
        type=Path,
        help="Path to Beads issues JSONL. Defaults to <repo-root>/.beads/issues.jsonl.",
    )
    parser.add_argument("--br-ready-json", type=Path, help="Optional captured `br ready --json` output.")
    parser.add_argument("--bv-plan-json", type=Path, help="Optional captured `bv --robot-plan` output.")
    parser.add_argument("--agent-mail-health-json", type=Path, help="Optional captured Agent Mail health JSON.")
    parser.add_argument("--rch-json", type=Path, help="Optional captured RCH/headroom JSON.")
    parser.add_argument("--closeout-freshness-json", type=Path, help="Optional closeout/runpack freshness JSON.")
    parser.add_argument(
        "--stale-in-progress-hours",
        type=float,
        default=DEFAULT_STALE_IN_PROGRESS_HOURS,
        help="Age threshold for stale in-progress Beads.",
    )
    parser.add_argument("--json", action="store_true", help="emit pretty JSON")
    parser.add_argument("--self-test", action="store_true", help="run fixture-backed self-test")
    return parser.parse_args()


def resolve_input_path(repo_root: Path, path: Path | None) -> Path | None:
    if path is None:
        return None
    return path if path.is_absolute() else repo_root / path


def main() -> int:
    args = parse_args()
    if args.self_test:
        return run_self_test()

    repo_root = args.repo_root.resolve()
    beads_path = resolve_input_path(repo_root, args.beads_jsonl) or repo_root / ".beads/issues.jsonl"
    assert beads_path is not None
    report = build_report(
        repo_root=repo_root,
        beads_path=beads_path,
        br_ready_json=read_json(resolve_input_path(repo_root, args.br_ready_json)),
        bv_plan_json=read_json(resolve_input_path(repo_root, args.bv_plan_json)),
        agent_mail_health_json=read_json(resolve_input_path(repo_root, args.agent_mail_health_json)),
        rch_json=read_json(resolve_input_path(repo_root, args.rch_json)),
        closeout_freshness_json=read_json(resolve_input_path(repo_root, args.closeout_freshness_json)),
        now=utc_now(),
        stale_hours=args.stale_in_progress_hours,
    )
    if args.json:
        sys.stdout.write(json_dumps(report, pretty=True))
    else:
        print_text_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
