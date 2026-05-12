#!/usr/bin/env python3
"""Build a read-only swarm operator runpack from existing evidence artifacts.

The runpack is an operator handoff bundle. It is not a release performance
claim, and it does not replace Beads, Agent Mail, doctor, cargo_headroom, or
claim-readiness artifacts as sources of truth.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNPACK_SCHEMA = "pi.swarm.operator_runpack.v1"
RUNPACK_CONTRACT_SCHEMA = "pi.swarm.operator_runpack_contract.v1"
SAFETY_SCORECARD_SCHEMA = "pi.swarm.safety_scorecard.v1"
TAIL_LATENCY_SCHEMA = "pi.operator_tail_latency.v1"
BOTTLENECK_ATTRIBUTION_SCHEMA = "pi.swarm.bottleneck_attribution_dashboard.v1"
FLIGHT_RECORDER_REPORT_SCHEMA = "pi.swarm.flight_recorder.report.v1"
HOST_PREFLIGHT_SCHEMA = "pi.doctor.swarm_resource_preflight.v1"
HOSTCALL_SWARM_PROFILE_SCHEMA = "pi.ext.hostcall_admission_swarm_profile.v1"
SESSION_RECOVERY_SWARM_PROFILE_SCHEMA = "pi.session_store_v2.recovery_swarm_profile.v1"
RPC_SWARM_E2E_SCHEMA = "pi.rpc.concurrent_swarm_e2e.v1"
RCH_ARTIFACT_SYNC_SCHEMA = "pi.rch.artifact_sync_preflight.v1"
GIT_CONTEXT_SCHEMA = "pi.swarm.git_context.v1"
RUNPACK_CAPTURE_SCHEMA = "pi.swarm.operator_runpack_capture.v1"
AUTOPILOT_INPUT_PACK_SCHEMA = "pi.swarm.autopilot_input_pack.v1"
AUTOPILOT_INPUT_PACK_CONTRACT_SCHEMA = "pi.swarm.autopilot_input_pack_contract.v1"
AUTOPILOT_PLAN_SCHEMA = "pi.swarm.autopilot_plan.v1"
AUTOPILOT_PLAN_CONTRACT_SCHEMA = "pi.swarm.autopilot_plan_contract.v1"
BUDGET_DRIFT_SCHEMA = "pi.swarm.budget_drift.v1"
AUTOPILOT_HANDOFF_SCHEMA = "pi.swarm.autopilot_handoff.v1"
AUTOPILOT_E2E_SCHEMA = "pi.swarm.autopilot_e2e.v1"
AUTOPILOT_E2E_EVENT_SCHEMA = "pi.swarm.autopilot_e2e.event.v1"
AUTOPILOT_DECISION_GATE_SCHEMA = "pi.swarm.autopilot_decision_gate.v1"
AUTOPILOT_DECISION_GATE_CONTRACT_SCHEMA = (
    "pi.swarm.autopilot_decision_gate_contract.v1"
)
RUNPACK_CONTRACT_PATH = Path("docs/contracts/swarm-operator-runpack-contract.json")
AUTOPILOT_INPUT_PACK_CONTRACT_PATH = Path(
    "docs/contracts/swarm-autopilot-input-pack-contract.json"
)
AUTOPILOT_PLAN_CONTRACT_PATH = Path("docs/contracts/swarm-autopilot-plan-contract.json")
AUTOPILOT_DECISION_GATE_CONTRACT_PATH = Path(
    "docs/contracts/swarm-autopilot-decision-gate-contract.json"
)
GOLDEN_REPORT_DIRECTORY = Path("tests/golden_corpus/swarm_operator_runpack")
COMPLETE_RUNPACK_GOLDEN = "complete_runpack_projection.json"
AUTOPILOT_PLAN_GOLDEN = "autopilot_plan_projection.json"
UPDATE_GOLDEN_ENV = "UPDATE_SWARM_OPERATOR_RUNPACK_GOLDEN"
DEFAULT_MAX_ITEMS = 8
DEFAULT_STALE_AFTER_HOURS = 24
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 12
CAPTURE_SNIPPET_MAX_CHARS = 1200
SCORECARD_MAX_PER_DIMENSION = 2
SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "bearer",
    "body",
    "cookie",
    "key",
    "password",
    "prompt",
    "registration_token",
    "secret",
    "token",
    "transcript",
)
SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"(?:api[_-]?key|authorization|password|registration_token|secret|token)"
    r"\s*[:=]\s*[\"']?[^\"'\s,}]+)"
)
BOTTLENECK_CORE_SOURCE_IDS = (
    "doctor_swarm",
    "smoke_harness",
    "activity_digest",
    "cargo_admission",
)
BOTTLENECK_OPTIONAL_SOURCE_IDS = (
    "tail_latency",
    "flight_recorder",
    "host_preflight",
    "hostcall_swarm_profile",
    "session_recovery_swarm_profile",
    "rpc_swarm_e2e",
    "rch_artifact_sync",
)
BOTTLENECK_SURFACES: dict[str, tuple[str, ...]] = {
    "provider_streaming": ("tail_latency", "flight_recorder", "rpc_swarm_e2e"),
    "local_tools": ("smoke_harness", "flight_recorder", "rpc_swarm_e2e"),
    "extension_hostcalls": ("hostcall_swarm_profile", "tail_latency", "flight_recorder"),
    "persistence": (
        "session_recovery_swarm_profile",
        "smoke_harness",
        "flight_recorder",
        "rpc_swarm_e2e",
    ),
    "rch_sync_retrieval": ("rch_artifact_sync", "cargo_admission"),
    "queue_pressure": ("cargo_admission", "activity_digest", "hostcall_swarm_profile"),
    "cgroup_numa_context": ("host_preflight", "doctor_swarm"),
}
AUTOPILOT_REQUIRED_SOURCE_IDS = (
    "doctor_swarm",
    "cargo_admission",
    "beads",
    "agent_mail_status",
    "agent_mail_reservations",
    "git_status",
)
AUTOPILOT_OPTIONAL_SOURCE_IDS = (
    "beads_ready",
    "activity_digest",
    "host_preflight",
    "operator_runpack",
)
AUTOPILOT_SOURCE_COMMAND_IDS: dict[str, tuple[str, ...]] = {
    "doctor_swarm": ("doctor_swarm",),
    "cargo_admission": ("cargo_admission",),
    "beads": ("beads_list",),
    "beads_ready": ("beads_ready",),
    "agent_mail_status": ("agent_mail_status",),
    "agent_mail_reservations": ("agent_mail_reservations",),
    "git_status": (
        "git_status_porcelain",
        "git_branch",
        "git_head",
        "git_upstream",
        "git_ahead_behind",
        "git_recent_commits",
        "git_recent_remote_commits",
    ),
    "activity_digest": (),
    "host_preflight": (),
    "operator_runpack": (),
}
AUTOPILOT_FORBIDDEN_ACTIONS = (
    "destructive git reset",
    "destructive git clean",
    "recursive filesystem deletion",
    "file deletion",
    "automatic commit",
    "automatic file reservation",
)
AUTOPILOT_PLAN_ALLOWED_ACTIONS = (
    "stop_and_surface_blocker",
    "wait_for_rch",
    "split_by_surface",
    "use_beads_soft_lock",
    "reopen_stale_bead_candidate",
    "capture_handoff",
    "adjust_swarm_budget",
    "claim_ready_bead",
    "run_docs_only_work",
)
AUTOPILOT_PLAN_SEVERITIES = ("critical", "high", "medium", "low", "info")
AUTOPILOT_PLAN_CONFIDENCE = ("high", "medium", "low")
AUTOPILOT_PLAN_DANGEROUS_COMMAND_FRAGMENTS = (
    "git reset --hard",
    "git clean -fd",
    "rm -rf",
)
AUTOPILOT_E2E_REQUIRED_SCENARIOS = (
    "healthy_ready_claim",
    "empty_ready_queue",
    "degraded_agent_mail_soft_lock",
    "saturated_rch_queue",
    "stale_in_progress_bead",
    "unrelated_dirty_worktree",
    "malformed_source_fail_closed",
)
AUTOPILOT_DECISION_GATE_CHILD_BEADS = (
    "bd-h3uv0.1",
    "bd-h3uv0.2",
    "bd-h3uv0.3",
    "bd-h3uv0.4",
    "bd-h3uv0.5",
    "bd-h3uv0.6",
    "bd-h3uv0.7",
)
AUTOPILOT_DECISION_GATE_REQUIRED_CHECKS = (
    "child_beads_closed",
    "input_pack_contract",
    "planner_contract",
    "work_partitions",
    "failure_actions",
    "budget_drift",
    "e2e_logging",
    "runpack_handoff",
    "safety_guards",
    "pushed_commits",
    "quality_gates",
)
AUTOPILOT_DECISION_GATE_REQUIRED_QUALITY_GATES = (
    "py_compile",
    "runpack_self_test",
    "autopilot_e2e",
    "json_contracts",
    "cargo_fmt",
    "cargo_check_all_targets_rch",
    "cargo_clippy_all_targets_rch",
    "staged_ubs",
    "beads_ledger_reconcile",
)
WORK_PARTITION_INSPECT_SENTINEL = "<inspect-bead-before-reserving>"
WORK_SURFACE_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "autopilot_runpack",
        "keywords": (
            "autopilot",
            "operator runpack",
            "runpack",
            "bead-to-file",
            "work partition",
            "partition",
            "launch control",
        ),
        "suggested_reservation": (
            "scripts/build_swarm_operator_runpack.py",
            "docs/contracts/swarm-autopilot-*.json",
            "docs/swarm-operations-runbook.md",
            "tests/golden_corpus/swarm_operator_runpack/*.json",
        ),
    },
    {
        "id": "provider_streaming",
        "keywords": (
            "provider",
            "streaming",
            "openai",
            "responses",
            "anthropic",
            "gemini",
            "cohere",
            "azure",
            "bedrock",
            "vertex",
            "copilot",
            "gitlab",
        ),
        "suggested_reservation": (
            "src/provider.rs",
            "src/providers/**/*.rs",
            "tests/provider_streaming*.rs",
        ),
    },
    {
        "id": "builtin_tools",
        "keywords": (
            "tool",
            "tools",
            "read tool",
            "write tool",
            "bash tool",
            "grep tool",
            "find tool",
            "ls tool",
            "hashline",
            "conformance",
        ),
        "suggested_reservation": (
            "src/tools.rs",
            "tests/conformance.rs",
            "tests/conformance/**/*.json",
        ),
    },
    {
        "id": "session_persistence",
        "keywords": (
            "session",
            "session index",
            "sqlite",
            "jsonl",
            "persistence",
            "compaction",
            "replay",
            "branching",
        ),
        "suggested_reservation": (
            "src/session.rs",
            "src/session_index.rs",
            "tests/session*.rs",
            "tests/storage*.rs",
        ),
    },
    {
        "id": "extension_runtime",
        "keywords": (
            "extension",
            "extensions",
            "quickjs",
            "hostcall",
            "hostcalls",
            "capability",
            "policy",
            "shim",
        ),
        "suggested_reservation": (
            "src/extensions.rs",
            "src/extensions_js.rs",
            "tests/extensions*.rs",
        ),
    },
    {
        "id": "interactive_surface",
        "keywords": ("interactive", "tui", "terminal", "rpc", "stdin", "config", "resources"),
        "suggested_reservation": (
            "src/interactive.rs",
            "src/tui.rs",
            "src/rpc.rs",
            "src/config.rs",
            "src/resources.rs",
        ),
    },
)
FAILURE_ACTION_CATALOG_SCHEMA = "pi.swarm.failure_action_catalog.v1"
FAILURE_ACTION_MAX_EXCERPT_CHARS = 520
FAILURE_ACTION_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "FAIL-RCH-QUEUE-SATURATED",
        "category": "rch",
        "confidence": "high",
        "terms": ("rch_queue_saturated", "queue_saturated", "slot_pressure=saturated"),
        "secondary_terms": ("backoff", "saturated", "queue"),
        "title": "RCH queue is saturated; do not start broad cargo validation",
        "explanation": (
            "Cargo admission or queue forecast says RCH capacity is saturated. "
            "Keep work narrow and wait before launching heavyweight checks."
        ),
        "safe_commands": (
            ("Inspect RCH queue", "rch queue"),
            ("Inspect RCH workers", "rch status --workers --jobs"),
            (
                "Refresh cargo admission",
                "./scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets",
            ),
        ),
        "escalation": (
            "If saturation persists, split validation by surface or pause heavy "
            "cargo work instead of allowing local fallback."
        ),
    },
    {
        "id": "FAIL-RCH-ARTIFACT-RETRIEVAL-DISK",
        "category": "rch",
        "confidence": "high",
        "terms": ("artifact retrieval", "artifact sync", "retrieve artifacts", "rch-e21"),
        "secondary_terms": ("no space left on device", "disk", "space", "storage"),
        "title": "RCH artifact retrieval is blocked by disk or artifact-sync pressure",
        "explanation": (
            "The remote build may have completed, but artifact retrieval or local "
            "staging is failing. Treat this as an operational storage/sync blocker, "
            "not as proof of a Rust regression."
        ),
        "safe_commands": (
            ("Inspect RCH jobs and workers", "rch status --workers --jobs"),
            ("Inspect queue pressure", "rch queue"),
            ("Inspect local scratch headroom", "df -h /data/tmp /tmp"),
        ),
        "escalation": (
            "If headroom is low or retrieval keeps failing, surface the RCH error "
            "code and worker id; do not delete cache or build directories without "
            "explicit operator approval."
        ),
    },
    {
        "id": "FAIL-CARGO-LOCAL-TARGET-DISK",
        "category": "cargo",
        "confidence": "high",
        "terms": ("no space left on device", "cargo_target_dir", "tmpdir", "target/debug"),
        "secondary_terms": ("cargo", "target", "tmp", "filesystem"),
        "title": "Cargo needs isolated high-capacity target and temp directories",
        "explanation": (
            "The failure matches local target/TMPDIR pressure. Retry through RCH "
            "with explicit per-agent scratch paths before treating compiler output "
            "as authoritative."
        ),
        "safe_commands": (
            (
                "Create per-agent scratch directories",
                "mkdir -p /data/tmp/pi_agent_rust_cargo/${USER:-agent}/target /data/tmp/pi_agent_rust_cargo/${USER:-agent}/tmp",
            ),
            (
                "Retry compiler check through RCH",
                "env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/target TMPDIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/tmp rch exec -- cargo check --all-targets",
            ),
        ),
        "escalation": (
            "If the same disk error appears with /data/tmp scratch paths, capture "
            "`df -h /data/tmp /tmp` and stop before cleanup."
        ),
    },
    {
        "id": "FAIL-RCH-REMOTE-COMPILE",
        "category": "rch",
        "confidence": "medium",
        "terms": ("[rch] remote", "remote compile", "remote build", "worker"),
        "secondary_terms": ("error[", "cargo check failed", "cargo clippy failed", "rustc"),
        "title": "Remote RCH execution reached the compiler and failed",
        "explanation": (
            "The failure appears to come from the remote compiler run, so inspect "
            "the Rust diagnostic before changing RCH configuration."
        ),
        "safe_commands": (
            ("Inspect RCH worker health", "rch status --workers --jobs"),
            (
                "Re-run the focused compiler command through RCH",
                "env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/target TMPDIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/tmp rch exec -- cargo check --all-targets",
            ),
        ),
        "escalation": (
            "If the diagnostic is not a code error, preserve the worker id, RCH "
            "code, and stderr excerpt for RCH triage."
        ),
    },
    {
        "id": "FAIL-AGENT-MAIL-SCHEMA",
        "category": "agent_mail",
        "confidence": "high",
        "terms": ("schema missing", "missing required", "projects, agents, messages"),
        "secondary_terms": ("agent mail", "sqlite", "message_recipients", "database"),
        "title": "Agent Mail database schema is missing required tables",
        "explanation": (
            "Agent Mail coordination cannot be trusted for reservations or inbox "
            "state until the mailbox schema is repaired or restored."
        ),
        "safe_commands": (
            ("Inspect Agent Mail health", "am doctor check --verbose"),
            ("Preview Agent Mail repair", "am doctor repair --dry-run"),
            ("Use Beads soft lock while Mail is red", "br list --status=in_progress --json"),
        ),
        "escalation": (
            "Run repair only after the dry-run output is understood; continue with "
            "Beads assignment as the temporary coordination lock."
        ),
    },
    {
        "id": "FAIL-AGENT-MAIL-DEGRADED-READONLY",
        "category": "agent_mail",
        "confidence": "medium",
        "terms": (
            "degraded_read_only",
            "read-only",
            "archive writes disabled",
            "fallback_action=use_beads_soft_lock",
        ),
        "secondary_terms": ("agent mail", "mail", "reservation", "inbox", "degraded"),
        "title": "Agent Mail is degraded or read-only",
        "explanation": (
            "Mail may still provide partial read evidence, but it should not be "
            "treated as the write-side reservation ledger."
        ),
        "safe_commands": (
            ("Inspect active Beads ownership", "br list --status=in_progress --json"),
            ("Inspect target bead", "br show <issue-id> --json"),
            ("Retry Agent Mail health later", "am doctor check --verbose"),
        ),
        "escalation": (
            "Do not assume reservation writes landed while Mail is degraded; use "
            "Beads status and narrow file surfaces until Mail is healthy."
        ),
    },
    {
        "id": "FAIL-BEADS-JSONL-DRIFT",
        "category": "beads",
        "confidence": "medium",
        "terms": ("jsonl drift", "beads db", "beads database", "br doctor"),
        "secondary_terms": ("stale", "drift", "warning", "integrity"),
        "title": "Beads database and JSONL evidence may be out of sync",
        "explanation": (
            "The Beads ledger itself is warning about stale or drifting state. "
            "Refresh Beads evidence before relying on ready/in-progress lists."
        ),
        "safe_commands": (
            ("Run Beads doctor", "br doctor"),
            ("Inspect ready queue", "br ready --json"),
            ("Inspect active ownership", "br list --status=in_progress --json"),
        ),
        "escalation": (
            "If doctor reports corruption or ambiguous recovery, stop and surface "
            "the exact doctor output instead of editing the Beads DB by hand."
        ),
    },
    {
        "id": "FAIL-BEADS-STALE-OWNER",
        "category": "beads",
        "confidence": "medium",
        "terms": ("stale in-progress", "stale bead", "stale owner"),
        "secondary_terms": ("beads", "assignee", "in_progress"),
        "title": "A Beads assignee may be stale",
        "explanation": (
            "The captured Beads state shows old in-progress ownership. Confirm "
            "abandonment before reopening or taking over the work."
        ),
        "safe_commands": (
            ("Inspect stale bead", "br show <issue-id> --json"),
            ("Inspect all active Beads", "br list --status=in_progress --json"),
            ("Reopen only after confirmation", "br update <issue-id> --status open"),
        ),
        "escalation": (
            "Do not force-release reservations or alter another agent's files unless "
            "Mail or recent commits confirm the claim is abandoned."
        ),
    },
    {
        "id": "FAIL-RCH-UNKNOWN",
        "category": "rch",
        "confidence": "low",
        "terms": ("rch-", "[rch]", "rch "),
        "secondary_terms": (),
        "title": "RCH reported an unclassified operational failure",
        "explanation": (
            "The signal mentions RCH but does not match a safer, more specific "
            "catalog entry. Preserve the excerpt and inspect RCH status before retrying."
        ),
        "safe_commands": (
            ("Inspect RCH status", "rch status --workers --jobs"),
            ("Inspect RCH queue", "rch queue"),
            ("Run RCH doctor", "rch doctor"),
        ),
        "escalation": (
            "Surface the RCH code, worker id, and redacted excerpt if doctor does "
            "not identify a safe self-recovery path."
        ),
    },
)
TIMESTAMP_KEYS = (
    "generated_at",
    "generatedAt",
    "timestamp",
    "created_at",
    "started_at",
    "run_started_at",
    "completed_at",
)


class RunpackError(RuntimeError):
    """Raised when a provided source cannot safely contribute to the runpack."""


@dataclass(frozen=True)
class SourcePayload:
    id: str
    path: str | None
    status: str
    schema: str | None
    payload: Any | None
    issue: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    redacted_count: int = 0
    redacted_fields: tuple[str, ...] = ()

    def to_status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "status": self.status,
            "schema": self.schema,
            "issue": self.issue,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass
class RedactionStats:
    redacted_count: int = 0
    fields: set[str] | None = None

    def __post_init__(self) -> None:
        if self.fields is None:
            self.fields = set()

    def merge(self, other: "RedactionStats") -> None:
        self.redacted_count += other.redacted_count
        self.fields.update(other.fields or set())

    def to_json(self) -> dict[str, Any]:
        return {
            "redacted_count": self.redacted_count,
            "fields": sorted(self.fields or []),
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_string(value: str, field: str) -> tuple[str, RedactionStats]:
    stats = RedactionStats()
    if SENSITIVE_VALUE_RE.search(value):
        stats.redacted_count += 1
        stats.fields.add(field)
        return SENSITIVE_VALUE_RE.sub("[REDACTED]", value), stats
    return value, stats


def redact_json(value: Any, field: str = "value") -> tuple[Any, RedactionStats]:
    stats = RedactionStats()
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            child_field = f"{field}.{key}" if field else str(key)
            if is_sensitive_key(str(key)):
                out[key] = "[REDACTED]"
                stats.redacted_count += 1
                stats.fields.add(child_field)
                continue
            redacted, child_stats = redact_json(item, child_field)
            stats.merge(child_stats)
            out[key] = redacted
        return out, stats
    if isinstance(value, list):
        out_list = []
        for index, item in enumerate(value):
            redacted, child_stats = redact_json(item, f"{field}[{index}]")
            stats.merge(child_stats)
            out_list.append(redacted)
        return out_list, stats
    if isinstance(value, str):
        return redact_string(value, field)
    return value, stats


def json_dumps(payload: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def file_fingerprint(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def shell_join(command: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def bounded_text(value: str, max_chars: int = CAPTURE_SNIPPET_MAX_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    omitted = len(value) - max_chars
    return value[:max_chars] + f"\n[... {omitted} chars omitted ...]"


def normalize_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def no_overwrite_write_text(path: Path, content: str) -> None:
    if path.exists():
        raise RunpackError(f"refusing to overwrite capture artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def no_overwrite_write_json(path: Path, payload: Any) -> None:
    no_overwrite_write_text(path, json_dumps(payload, pretty=True))


def capture_unavailable(
    command_id: str,
    command: list[str],
    *,
    cwd: Path,
    reason: str,
) -> tuple[dict[str, Any], str]:
    return (
        {
            "id": command_id,
            "command": shell_join(command),
            "cwd": str(cwd),
            "status": "not_available",
            "exit_code": None,
            "issue": reason,
            "stdout_path": None,
            "stderr_snippet": "",
            "redaction_summary": {"redacted_count": 0, "fields": []},
        },
        "",
    )


def capture_command(
    command_id: str,
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    stdout_path: Path | None = None,
) -> tuple[dict[str, Any], str]:
    result: dict[str, Any] = {
        "id": command_id,
        "command": shell_join(command),
        "cwd": str(cwd),
        "started_at": utc_now_iso(),
        "timeout_seconds": timeout_seconds,
        "stdout_path": str(stdout_path) if stdout_path is not None else None,
    }
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        result.update({"status": "not_available", "exit_code": None, "issue": str(exc)})
    except subprocess.TimeoutExpired as exc:
        stdout = normalize_output(exc.stdout)
        stderr = normalize_output(exc.stderr)
        result.update({"status": "timeout", "exit_code": None, "issue": "command timed out"})
    else:
        stdout = normalize_output(completed.stdout)
        stderr = normalize_output(completed.stderr)
        result.update(
            {
                "status": "ok" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
                "issue": None if completed.returncode == 0 else "command exited non-zero",
            }
        )
    if stdout_path is not None:
        no_overwrite_write_text(stdout_path, stdout)

    stdout_snippet, stdout_stats = redact_string(
        bounded_text(stdout), f"capture.{command_id}.stdout"
    )
    stderr_snippet, stderr_stats = redact_string(
        bounded_text(stderr), f"capture.{command_id}.stderr"
    )
    stdout_stats.merge(stderr_stats)
    result.update(
        {
            "stdout_snippet": stdout_snippet,
            "stderr_snippet": stderr_snippet,
            "redaction_summary": stdout_stats.to_json(),
        }
    )
    return result, stdout


def json_payload_from_stdout(stdout: str) -> Any | None:
    stripped = stdout.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, (dict, list)):
        return payload
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, (dict, list)):
            return payload
    return None


def json_object_from_stdout(stdout: str) -> dict[str, Any] | None:
    payload = json_payload_from_stdout(stdout)
    return payload if isinstance(payload, dict) else None


def json_schema(value: Any) -> str | None:
    if isinstance(value, dict):
        schema = value.get("schema")
        if isinstance(schema, str):
            return schema
    return None


def load_json_source(
    source_id: str,
    path: Path | None,
    *,
    expected_schema: str | None = None,
) -> SourcePayload:
    if path is None:
        return SourcePayload(source_id, None, "not_provided", None, None)
    if not path.exists():
        raise RunpackError(f"{source_id} source path does not exist: {path}")
    size_bytes, sha256 = file_fingerprint(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunpackError(f"{source_id} source is malformed JSON: {path}: {exc}") from exc
    redacted, stats = redact_json(payload, source_id)
    schema = json_schema(redacted)
    if expected_schema is not None and schema != expected_schema:
        raise RunpackError(
            f"{source_id} source schema mismatch: expected {expected_schema}, got {schema}"
        )
    return SourcePayload(
        source_id,
        str(path),
        "ok",
        schema,
        redacted,
        size_bytes=size_bytes,
        sha256=sha256,
        redacted_count=stats.redacted_count,
        redacted_fields=tuple(sorted(stats.fields or [])),
    )


def load_cargo_admission(path: Path | None) -> SourcePayload:
    if path is None:
        return SourcePayload("cargo_admission", None, "not_provided", None, None)
    if not path.exists():
        raise RunpackError(f"cargo_admission source path does not exist: {path}")
    size_bytes, sha256 = file_fingerprint(path)
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        redacted, stats = redact_json(payload, "cargo_admission")
        return SourcePayload(
            "cargo_admission",
            str(path),
            "ok",
            json_schema(redacted),
            redacted,
            size_bytes=size_bytes,
            sha256=sha256,
            redacted_count=stats.redacted_count,
            redacted_fields=tuple(sorted(stats.fields or [])),
        )
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            redacted, stats = redact_json(payload, "cargo_admission")
            return SourcePayload(
                "cargo_admission",
                str(path),
                "ok",
                json_schema(redacted),
                redacted,
                size_bytes=size_bytes,
                sha256=sha256,
                redacted_count=stats.redacted_count,
                redacted_fields=tuple(sorted(stats.fields or [])),
            )
    raise RunpackError(f"cargo_admission source did not contain a JSON object: {path}")


def load_git_status(path: Path | None) -> SourcePayload:
    if path is None:
        return SourcePayload("git_status", None, "not_provided", None, None)
    if not path.exists():
        raise RunpackError(f"git_status source path does not exist: {path}")
    size_bytes, sha256 = file_fingerprint(path)
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RunpackError(f"git_status source is malformed JSON: {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RunpackError(f"git_status source JSON must be an object: {path}")
        redacted, stats = redact_json(payload, "git_status")
        lines = (
            redacted.get("porcelain_lines")
            if isinstance(redacted.get("porcelain_lines"), list)
            else []
        )
        redacted["dirty"] = bool(lines)
        return SourcePayload(
            "git_status",
            str(path),
            "ok",
            json_schema(redacted),
            redacted,
            size_bytes=size_bytes,
            sha256=sha256,
            redacted_count=stats.redacted_count,
            redacted_fields=tuple(sorted(stats.fields or [])),
        )
    lines = [line.rstrip("\n") for line in text.splitlines()]
    return SourcePayload(
        "git_status",
        str(path),
        "ok",
        None,
        {"dirty": bool(lines), "porcelain_lines": lines},
        size_bytes=size_bytes,
        sha256=sha256,
    )


def source_payloads(args: argparse.Namespace) -> list[SourcePayload]:
    sources = [
        load_json_source("doctor_swarm", args.doctor_json),
        load_json_source(
            "claim_readiness",
            args.claim_readiness_json,
            expected_schema="pi.swarm.claim_readiness_report.v1",
        ),
        load_json_source(
            "smoke_harness",
            args.smoke_summary_json,
            expected_schema="pi.swarm.smoke_harness.v1",
        ),
        load_json_source(
            "activity_digest",
            args.activity_digest_json,
            expected_schema="pi.swarm.activity_digest.v1",
        ),
        load_cargo_admission(args.cargo_admission_json),
        load_json_source("beads", args.beads_json),
        load_git_status(args.git_status_file),
    ]
    if args.tail_latency_json is not None:
        sources.append(
            load_json_source(
                "tail_latency",
                args.tail_latency_json,
                expected_schema=TAIL_LATENCY_SCHEMA,
            )
        )
    if args.flight_recorder_report_json is not None:
        sources.append(
            load_json_source(
                "flight_recorder",
                args.flight_recorder_report_json,
                expected_schema=FLIGHT_RECORDER_REPORT_SCHEMA,
            )
        )
    if args.host_preflight_json is not None:
        sources.append(
            load_json_source(
                "host_preflight",
                args.host_preflight_json,
                expected_schema=HOST_PREFLIGHT_SCHEMA,
            )
        )
    if args.hostcall_swarm_profile_json is not None:
        sources.append(
            load_json_source(
                "hostcall_swarm_profile",
                args.hostcall_swarm_profile_json,
                expected_schema=HOSTCALL_SWARM_PROFILE_SCHEMA,
            )
        )
    if args.session_recovery_swarm_profile_json is not None:
        sources.append(
            load_json_source(
                "session_recovery_swarm_profile",
                args.session_recovery_swarm_profile_json,
                expected_schema=SESSION_RECOVERY_SWARM_PROFILE_SCHEMA,
            )
        )
    if args.rpc_swarm_e2e_json is not None:
        sources.append(
            load_json_source(
                "rpc_swarm_e2e",
                args.rpc_swarm_e2e_json,
                expected_schema=RPC_SWARM_E2E_SCHEMA,
            )
        )
    if args.rch_artifact_sync_json is not None:
        sources.append(
            load_json_source(
                "rch_artifact_sync",
                args.rch_artifact_sync_json,
                expected_schema=RCH_ARTIFACT_SYNC_SCHEMA,
            )
        )
    return sources


def autopilot_source_payloads(args: argparse.Namespace) -> list[SourcePayload]:
    beads_ready_json = getattr(args, "beads_ready_json", None)
    agent_mail_status_json = getattr(args, "agent_mail_status_json", None)
    agent_mail_reservations_json = getattr(args, "agent_mail_reservations_json", None)
    operator_runpack_json = getattr(args, "operator_runpack_json", None)
    return [
        load_json_source("doctor_swarm", args.doctor_json),
        load_cargo_admission(args.cargo_admission_json),
        load_json_source("beads", args.beads_json),
        load_json_source("beads_ready", beads_ready_json),
        load_json_source("agent_mail_status", agent_mail_status_json),
        load_json_source("agent_mail_reservations", agent_mail_reservations_json),
        load_json_source(
            "host_preflight",
            getattr(args, "host_preflight_json", None),
            expected_schema=HOST_PREFLIGHT_SCHEMA,
        ),
        load_git_status(args.git_status_file),
        load_json_source(
            "activity_digest",
            args.activity_digest_json,
            expected_schema="pi.swarm.activity_digest.v1",
        ),
        load_json_source(
            "operator_runpack",
            operator_runpack_json,
            expected_schema=RUNPACK_SCHEMA,
        ),
    ]


def first_stdout_line(stdout: str) -> str | None:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def parse_ahead_behind(stdout: str) -> tuple[int | None, int | None]:
    parts = stdout.strip().split()
    if len(parts) != 2:
        return None, None
    try:
        left, right = int(parts[0]), int(parts[1])
    except ValueError:
        return None, None
    return left, right


def capture_git_context(
    repo_root: Path,
    capture_dir: Path,
    timeout_seconds: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    commands: list[dict[str, Any]] = []
    status_result, status_stdout = capture_command(
        "git_status_porcelain",
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(status_result)
    branch_result, branch_stdout = capture_command(
        "git_branch",
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(branch_result)
    head_result, head_stdout = capture_command(
        "git_head",
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(head_result)
    upstream_result, upstream_stdout = capture_command(
        "git_upstream",
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(upstream_result)
    ahead_result, ahead_stdout = capture_command(
        "git_ahead_behind",
        ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(ahead_result)
    recent_result, recent_stdout = capture_command(
        "git_recent_commits",
        ["git", "log", "--oneline", "-5"],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(recent_result)
    remote_result, remote_stdout = capture_command(
        "git_recent_remote_commits",
        ["git", "log", "--remotes", "--oneline", "-5"],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(remote_result)
    ahead, behind = parse_ahead_behind(ahead_stdout)
    context = {
        "schema": GIT_CONTEXT_SCHEMA,
        "generated_at": utc_now_iso(),
        "branch": first_stdout_line(branch_stdout),
        "head": first_stdout_line(head_stdout),
        "upstream": {
            "name": first_stdout_line(upstream_stdout),
            "ahead": ahead,
            "behind": behind,
            "status": upstream_result.get("status"),
        },
        "porcelain_lines": status_stdout.splitlines(),
        "recent_commits": recent_stdout.splitlines(),
        "recent_remote_commits": remote_stdout.splitlines(),
        "command_statuses": [
            {
                "id": command.get("id"),
                "status": command.get("status"),
                "exit_code": command.get("exit_code"),
            }
            for command in commands
        ],
    }
    no_overwrite_write_json(capture_dir / "git-status.json", context)
    return context, commands


def maybe_capture_json_source(
    *,
    args: argparse.Namespace,
    attr: str,
    source_id: str,
    command_id: str,
    command: list[str],
    output_path: Path,
    repo_root: Path,
    timeout_seconds: int,
    commands: list[dict[str, Any]],
    generated_source_paths: dict[str, str],
) -> None:
    if getattr(args, attr) is not None:
        generated_source_paths[source_id] = str(getattr(args, attr))
        return
    result, stdout = capture_command(
        command_id,
        command,
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(result)
    payload = json_payload_from_stdout(stdout)
    if payload is None:
        return
    no_overwrite_write_json(output_path, payload)
    setattr(args, attr, output_path)
    generated_source_paths[source_id] = str(output_path)


def maybe_capture_agent_mail(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    timeout_seconds: int,
    commands: list[dict[str, Any]],
    capture_dir: Path,
    generated_source_paths: dict[str, str],
) -> None:
    am_path = shutil.which("am")
    if am_path is None:
        result, _ = capture_unavailable(
            "agent_mail_status",
            ["am", "robot", "status", "--format", "json", "--project", str(repo_root)],
            cwd=repo_root,
            reason="am CLI was not found",
        )
        commands.append(result)
        return
    agent_name = args.agent_name or os.environ.get("AGENT_NAME") or os.environ.get("USER")
    status_command = [
        am_path,
        "robot",
        "status",
        "--format",
        "json",
        "--project",
        str(repo_root),
    ]
    reservation_command = [
        am_path,
        "robot",
        "reservations",
        "--format",
        "json",
        "--project",
        str(repo_root),
    ]
    if agent_name:
        status_command.extend(["--agent", agent_name])
        reservation_command.extend(["--agent", agent_name])
    status_result, status_stdout = capture_command(
        "agent_mail_status",
        status_command,
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(status_result)
    if json_object_from_stdout(status_stdout) is not None:
        status_path = capture_dir / "agent-mail-status.json"
        no_overwrite_write_text(status_path, status_stdout)
        args.agent_mail_status_json = status_path
        generated_source_paths["agent_mail_status"] = str(status_path)
    reservation_result, reservation_stdout = capture_command(
        "agent_mail_reservations",
        reservation_command,
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(reservation_result)
    if json_object_from_stdout(reservation_stdout) is not None:
        reservations_path = capture_dir / "agent-mail-reservations.json"
        no_overwrite_write_text(reservations_path, reservation_stdout)
        args.agent_mail_reservations_json = reservations_path
        generated_source_paths["agent_mail_reservations"] = str(reservations_path)


def maybe_capture_rch(
    *,
    repo_root: Path,
    timeout_seconds: int,
    commands: list[dict[str, Any]],
    capture_dir: Path,
) -> None:
    rch_path = shutil.which("rch")
    if rch_path is None:
        result, _ = capture_unavailable(
            "rch_queue",
            ["rch", "queue", "--json"],
            cwd=repo_root,
            reason="rch CLI was not found",
        )
        commands.append(result)
        return
    queue_result, queue_stdout = capture_command(
        "rch_queue",
        [rch_path, "queue", "--json"],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(queue_result)
    if json_object_from_stdout(queue_stdout) is not None:
        no_overwrite_write_text(capture_dir / "rch-queue.json", queue_stdout)
    status_result, status_stdout = capture_command(
        "rch_status",
        [rch_path, "status"],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    commands.append(status_result)
    no_overwrite_write_text(capture_dir / "rch-status.txt", status_stdout)


def capture_current_sources(args: argparse.Namespace) -> None:
    if not getattr(args, "capture_current", False):
        args.capture_manifest = None
        return
    repo_root = args.project_root.resolve()
    capture_dir = (
        args.capture_dir.resolve()
        if args.capture_dir is not None
        else Path(tempfile.mkdtemp(prefix="pi_swarm_runpack_capture_")).resolve()
    )
    capture_dir.mkdir(parents=True, exist_ok=True)
    commands: list[dict[str, Any]] = []
    generated_source_paths: dict[str, str] = {}
    timeout_seconds = args.capture_timeout_seconds

    if args.git_status_file is None:
        _, git_commands = capture_git_context(repo_root, capture_dir, timeout_seconds)
        commands.extend(git_commands)
        args.git_status_file = capture_dir / "git-status.json"
        generated_source_paths["git_status"] = str(args.git_status_file)
    else:
        generated_source_paths["git_status"] = str(args.git_status_file)

    maybe_capture_json_source(
        args=args,
        attr="claim_readiness_json",
        source_id="claim_readiness",
        command_id="claim_readiness",
        command=[
            sys.executable,
            "scripts/report_swarm_claim_readiness.py",
            "--repo-root",
            str(repo_root),
            "--json",
        ],
        output_path=capture_dir / "claim-readiness.json",
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        commands=commands,
        generated_source_paths=generated_source_paths,
    )

    maybe_capture_json_source(
        args=args,
        attr="beads_json",
        source_id="beads",
        command_id="beads_list",
        command=["br", "list", "--json"],
        output_path=capture_dir / "beads.json",
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        commands=commands,
        generated_source_paths=generated_source_paths,
    )

    maybe_capture_json_source(
        args=args,
        attr="beads_ready_json",
        source_id="beads_ready",
        command_id="beads_ready",
        command=["br", "ready", "--json"],
        output_path=capture_dir / "beads-ready.json",
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        commands=commands,
        generated_source_paths=generated_source_paths,
    )

    cargo_headroom = repo_root / "scripts" / "cargo_headroom.sh"
    if args.cargo_admission_json is None and cargo_headroom.exists():
        maybe_capture_json_source(
            args=args,
            attr="cargo_admission_json",
            source_id="cargo_admission",
            command_id="cargo_admission",
            command=[
                str(cargo_headroom),
                "--runner",
                "rch",
                "--admit-only",
                "check",
                "--all-targets",
            ],
            output_path=capture_dir / "cargo-admission.json",
            repo_root=repo_root,
            timeout_seconds=timeout_seconds,
            commands=commands,
            generated_source_paths=generated_source_paths,
        )
    elif args.cargo_admission_json is not None:
        generated_source_paths["cargo_admission"] = str(args.cargo_admission_json)

    pi_path = shutil.which("pi")
    if args.doctor_json is None and pi_path is not None:
        maybe_capture_json_source(
            args=args,
            attr="doctor_json",
            source_id="doctor_swarm",
            command_id="doctor_swarm",
            command=[pi_path, "doctor", "--only", "swarm", "--format", "json"],
            output_path=capture_dir / "doctor-swarm.json",
            repo_root=repo_root,
            timeout_seconds=timeout_seconds,
            commands=commands,
            generated_source_paths=generated_source_paths,
        )
    elif args.doctor_json is not None:
        generated_source_paths["doctor_swarm"] = str(args.doctor_json)
    else:
        result, _ = capture_unavailable(
            "doctor_swarm",
            ["pi", "doctor", "--only", "swarm", "--format", "json"],
            cwd=repo_root,
            reason="pi CLI was not found in PATH",
        )
        commands.append(result)

    default_activity = repo_root / "tests" / "full_suite_gate" / "swarm_activity_digest.json"
    if args.activity_digest_json is None and default_activity.exists():
        args.activity_digest_json = default_activity
        generated_source_paths["activity_digest"] = str(default_activity)
    elif args.activity_digest_json is not None:
        generated_source_paths["activity_digest"] = str(args.activity_digest_json)

    maybe_capture_agent_mail(
        args=args,
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        commands=commands,
        capture_dir=capture_dir,
        generated_source_paths=generated_source_paths,
    )
    maybe_capture_rch(
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        commands=commands,
        capture_dir=capture_dir,
    )

    statuses = [str(command.get("status")) for command in commands]
    args.capture_manifest = {
        "schema": RUNPACK_CAPTURE_SCHEMA,
        "mode": "current",
        "status": "ok" if all(status == "ok" for status in statuses) else "degraded",
        "generated_at": utc_now_iso(),
        "capture_dir": str(capture_dir),
        "project_root": str(repo_root),
        "generated_source_paths": generated_source_paths,
        "commands": commands,
    }


def capture_summary_from_args(args: argparse.Namespace) -> dict[str, Any]:
    manifest = getattr(args, "capture_manifest", None)
    if isinstance(manifest, dict):
        return manifest
    provided_paths = {
        "doctor_swarm": args.doctor_json,
        "claim_readiness": args.claim_readiness_json,
        "smoke_harness": args.smoke_summary_json,
        "activity_digest": args.activity_digest_json,
        "cargo_admission": args.cargo_admission_json,
        "beads": args.beads_json,
        "beads_ready": getattr(args, "beads_ready_json", None),
        "agent_mail_status": getattr(args, "agent_mail_status_json", None),
        "agent_mail_reservations": getattr(args, "agent_mail_reservations_json", None),
        "git_status": args.git_status_file,
        "operator_runpack": getattr(args, "operator_runpack_json", None),
    }
    return {
        "schema": RUNPACK_CAPTURE_SCHEMA,
        "mode": "provided_sources",
        "status": "not_run",
        "generated_at": None,
        "capture_dir": None,
        "project_root": None,
        "generated_source_paths": {
            key: str(value) for key, value in provided_paths.items() if value is not None
        },
        "commands": [],
    }


def summarize_agent_mail_read_state(
    capture_summary: dict[str, Any],
    doctor_summary: dict[str, Any],
    max_items: int,
) -> dict[str, Any]:
    commands = [
        command
        for command in capture_summary.get("commands", [])
        if isinstance(command, dict) and str(command.get("id", "")).startswith("agent_mail")
    ]
    command_statuses = [str(command.get("status")) for command in commands]
    if not commands:
        status = "not_captured"
    elif all(command_status == "ok" for command_status in command_statuses):
        status = "ok"
    elif any(command_status in {"failed", "timeout"} for command_status in command_statuses):
        status = "degraded"
    else:
        status = "not_available"
    return {
        "status": status,
        "capture_mode": capture_summary.get("mode"),
        "doctor_finding_count": len(doctor_summary.get("agent_mail_findings") or []),
        "build_slots_observed": doctor_summary.get("agent_mail_build_slots") is not None,
        "commands": bounded(
            [
                {
                    "id": command.get("id"),
                    "status": command.get("status"),
                    "exit_code": command.get("exit_code"),
                    "issue": command.get("issue"),
                }
                for command in commands
            ],
            max_items,
        ),
    }


def infer_validation_status(text: str) -> str:
    lowered = text.lower()
    if "error:" in lowered or "failed" in lowered or "exit code: 1" in lowered:
        return "failed"
    if "warning:" in lowered:
        return "warning"
    if "self-test pass" in lowered or "finished" in lowered or "pass" in lowered:
        return "passed"
    return "unknown"


def summarize_validation_outputs(
    paths: list[Path],
    max_items: int,
) -> tuple[dict[str, Any], RedactionStats]:
    stats = RedactionStats()
    if not paths:
        return {"status": "not_provided", "outputs": []}, stats
    outputs: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        if not path.exists():
            raise RunpackError(f"validation output path does not exist: {path}")
        size_bytes, sha256 = file_fingerprint(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        snippet, child_stats = redact_string(
            bounded_text(text), f"validation_outputs[{index}].snippet"
        )
        stats.merge(child_stats)
        outputs.append(
            {
                "path": str(path),
                "size_bytes": size_bytes,
                "sha256": sha256,
                "inferred_status": infer_validation_status(text),
                "snippet": snippet,
            }
        )
    status_counts = Counter(output["inferred_status"] for output in outputs)
    if status_counts.get("failed"):
        status = "failed"
    elif status_counts.get("warning"):
        status = "warning"
    elif outputs and all(output["inferred_status"] == "passed" for output in outputs):
        status = "passed"
    else:
        status = "unknown"
    return {
        "status": status,
        "outputs": bounded(outputs, max_items),
        "redaction_summary": stats.to_json(),
    }, stats


def build_resume_commands(args: argparse.Namespace) -> list[dict[str, str]]:
    if getattr(args, "capture_dir", None) is not None:
        capture_dir = (
            str(args.capture_dir).rstrip("/")
            + "-next-$(date -u +%Y%m%dT%H%M%SZ)"
        )
    else:
        capture_dir = "/data/tmp/pi_swarm_runpack/${USER:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
    target_root = "/data/tmp/pi_agent_rust_cargo/${USER:-agent}"
    return [
        {
            "purpose": "Inspect branch and dirty files",
            "command": "git status --short --branch",
        },
        {
            "purpose": "Inspect active Beads ownership",
            "command": "br list --status=in_progress --json",
        },
        {
            "purpose": "Find next ready work",
            "command": "br ready --json",
        },
        {
            "purpose": "Regenerate this handoff bundle",
            "command": (
                f"capture_dir={capture_dir}; "
                "python3 scripts/build_swarm_operator_runpack.py "
                '--capture-current --capture-dir "$capture_dir" '
                '--out-json "$capture_dir/operator-runpack.json" '
                '--out-md "$capture_dir/operator-runpack.md"'
            ),
        },
        {
            "purpose": "Check Beads/drop-in ledger invariant",
            "command": "./scripts/reconcile_beads_ledger.sh",
        },
        {
            "purpose": "Run compiler check through RCH",
            "command": (
                f"CARGO_TARGET_DIR={target_root}/target TMPDIR={target_root}/tmp "
                "rch exec -- cargo check --all-targets"
            ),
        },
        {
            "purpose": "Run clippy through RCH",
            "command": (
                f"CARGO_TARGET_DIR={target_root}/target TMPDIR={target_root}/tmp "
                "rch exec -- cargo clippy --all-targets -- -D warnings"
            ),
        },
    ]


def bounded(items: list[Any], max_items: int) -> list[Any]:
    return items[: max(0, max_items)]


def summarize_doctor(source: SourcePayload, max_items: int) -> dict[str, Any]:
    payload = source.payload
    if not isinstance(payload, dict):
        return {"status": source.status, "findings": []}
    findings = payload.get("findings")
    if not isinstance(findings, list):
        findings = []
    swarm_findings: list[dict[str, Any]] = []
    agent_mail_findings: list[dict[str, Any]] = []
    build_slot_finding: dict[str, Any] | None = None
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("category") != "swarm":
            continue
        item = {
            "severity": finding.get("severity"),
            "title": finding.get("title"),
            "detail": finding.get("detail"),
            "remediation": finding.get("remediation"),
            "data": finding.get("data"),
        }
        swarm_findings.append(item)
        title = str(finding.get("title") or "")
        data = finding.get("data") if isinstance(finding.get("data"), dict) else {}
        data_schema = data.get("schema") if isinstance(data, dict) else None
        if "Agent Mail" in title or "reservation" in title:
            agent_mail_findings.append(item)
        if data_schema == "pi.doctor.agent_mail_build_slots.v1" or "build slot" in title.lower():
            build_slot_finding = item
    severity_counts = Counter(str(item.get("severity") or "unknown") for item in swarm_findings)
    return {
        "status": source.status,
        "overall": payload.get("overall"),
        "summary": payload.get("summary"),
        "finding_count": len(swarm_findings),
        "severity_counts": dict(sorted(severity_counts.items())),
        "findings": bounded(swarm_findings, max_items),
        "agent_mail_findings": bounded(agent_mail_findings, max_items),
        "agent_mail_build_slots": build_slot_finding,
    }


def summarize_claim_readiness(source: SourcePayload, max_items: int) -> dict[str, Any]:
    payload = source.payload
    if not isinstance(payload, dict):
        return {"status": source.status}
    artifact_statuses = payload.get("artifact_statuses")
    if not isinstance(artifact_statuses, list):
        artifact_statuses = []
    counts = Counter(str(item.get("status") or "unknown") for item in artifact_statuses if isinstance(item, dict))
    blocking = [
        {
            "id": item.get("id"),
            "category": item.get("category"),
            "status": item.get("status"),
            "issue_kinds": item.get("issue_kinds"),
        }
        for item in artifact_statuses
        if isinstance(item, dict)
        and item.get("release_blocking") is True
        and item.get("status") not in {"ready", "historical_snapshot"}
    ]
    return {
        "status": source.status,
        "overall_status": payload.get("overall_status"),
        "max_age_days": payload.get("max_age_days"),
        "artifact_status_counts": dict(sorted(counts.items())),
        "blocking_artifacts": bounded(blocking, max_items),
        "stale_claims": payload.get("stale_claims", {}).get("summary")
        if isinstance(payload.get("stale_claims"), dict)
        else None,
    }


def summarize_tail_latency(source: SourcePayload, max_items: int) -> dict[str, Any]:
    payload = source.payload
    if not isinstance(payload, dict):
        return {"status": source.status}
    metrics = payload.get("metrics")
    if not isinstance(metrics, list):
        metrics = []
    summarized_metrics: list[dict[str, Any]] = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        snapshot = metric.get("snapshot") if isinstance(metric.get("snapshot"), dict) else {}
        tail = snapshot.get("tail") if isinstance(snapshot.get("tail"), dict) else {}
        summarized_metrics.append(
            {
                "id": metric.get("id"),
                "label": metric.get("label"),
                "count": snapshot.get("count"),
                "sample_count": tail.get("sample_count"),
                "p95_us": tail.get("p95_us"),
                "p99_us": tail.get("p99_us"),
                "p999_us": tail.get("p999_us"),
                "max_us": snapshot.get("max_us"),
            }
        )
    return {
        "status": source.status,
        "schema": payload.get("schema"),
        "generated_at": payload.get("generated_at"),
        "purpose": payload.get("purpose"),
        "telemetry_enabled": payload.get("telemetry_enabled"),
        "sample_window": payload.get("sample_window"),
        "redaction_summary": payload.get("redaction_summary"),
        "metrics": bounded(summarized_metrics, max_items),
    }


def top_level_timestamp(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in TIMESTAMP_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def classify_bottleneck_source(
    source: SourcePayload,
    *,
    generated_at: datetime,
    stale_after_hours: int,
    required: bool,
) -> dict[str, Any]:
    if source.status != "ok":
        return {
            "id": source.id,
            "role": "required_surface" if required else "optional_diagnostic",
            "status": source.status,
            "schema": source.schema,
            "classification": "blocker" if required else "optional_diagnostic",
            "freshness_hours": None,
            "timestamp": None,
            "issue": source.issue or "source was not provided",
        }
    timestamp = top_level_timestamp(source.payload)
    if required and timestamp is None:
        return {
            "id": source.id,
            "role": "required_surface",
            "status": source.status,
            "schema": source.schema,
            "classification": "fresh",
            "freshness_hours": None,
            "timestamp": None,
            "issue": None,
        }
    if timestamp is None:
        return {
            "id": source.id,
            "role": "optional_diagnostic",
            "status": source.status,
            "schema": source.schema,
            "classification": "optional_diagnostic",
            "freshness_hours": None,
            "timestamp": None,
            "issue": "provided optional diagnostic is missing a top-level timestamp",
        }
    try:
        source_time = parse_utc(timestamp)
    except ValueError:
        return {
            "id": source.id,
            "role": "optional_diagnostic",
            "status": source.status,
            "schema": source.schema,
            "classification": "blocker",
            "freshness_hours": None,
            "timestamp": timestamp,
            "issue": "provided optional diagnostic has an invalid timestamp",
        }
    age_hours = (generated_at - source_time).total_seconds() / 3600
    if age_hours < 0:
        return {
            "id": source.id,
            "role": "optional_diagnostic",
            "status": source.status,
            "schema": source.schema,
            "classification": "blocker",
            "freshness_hours": round(age_hours, 2),
            "timestamp": source_time.isoformat(),
            "issue": "provided optional diagnostic timestamp is in the future",
        }
    if age_hours > stale_after_hours:
        return {
            "id": source.id,
            "role": "optional_diagnostic",
            "status": source.status,
            "schema": source.schema,
            "classification": "historical_snapshot",
            "freshness_hours": round(age_hours, 2),
            "timestamp": source_time.isoformat(),
            "issue": f"source is older than stale_after_hours={stale_after_hours}",
        }
    return {
        "id": source.id,
        "role": "optional_diagnostic",
        "status": source.status,
        "schema": source.schema,
        "classification": "fresh",
        "freshness_hours": round(age_hours, 2),
        "timestamp": source_time.isoformat(),
        "issue": None,
    }


def surface_status(classifications: list[dict[str, Any]]) -> str:
    if any(item.get("classification") == "blocker" for item in classifications):
        return "blocked"
    if any(item.get("classification") == "fresh" for item in classifications):
        return "covered"
    if any(item.get("classification") == "historical_snapshot" for item in classifications):
        return "historical_snapshot"
    return "optional_diagnostic_missing"


def summarize_surface(
    surface_id: str,
    source_ids: tuple[str, ...],
    classifications_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    classifications = [
        classifications_by_id[source_id]
        for source_id in source_ids
        if source_id in classifications_by_id
    ]
    return {
        "id": surface_id,
        "status": surface_status(classifications),
        "source_ids": list(source_ids),
        "classifications": [
            {
                "id": item.get("id"),
                "classification": item.get("classification"),
                "issue": item.get("issue"),
            }
            for item in classifications
        ],
    }


def extract_tail_latency_bottlenecks(
    source: SourcePayload, max_items: int
) -> list[dict[str, Any]]:
    payload = source.payload if isinstance(source.payload, dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), list) else []
    findings: list[dict[str, Any]] = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        snapshot = metric.get("snapshot") if isinstance(metric.get("snapshot"), dict) else {}
        tail = snapshot.get("tail") if isinstance(snapshot.get("tail"), dict) else {}
        p99 = tail.get("p99_us")
        p999 = tail.get("p999_us")
        findings.append(
            {
                "surface": "provider_streaming",
                "source": source.id,
                "label": metric.get("label") or metric.get("id"),
                "signal": "tail_latency",
                "p99_us": p99,
                "p999_us": p999,
                "max_us": snapshot.get("max_us"),
            }
        )
    return bounded(findings, max_items)


def extract_flight_recorder_bottlenecks(
    source: SourcePayload, max_items: int
) -> list[dict[str, Any]]:
    payload = source.payload if isinstance(source.payload, dict) else {}
    components = payload.get("dominant_latency_components")
    if not isinstance(components, list):
        components = []
    findings: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        findings.append(
            {
                "surface": "provider_streaming",
                "source": source.id,
                "label": component.get("component") or component.get("name"),
                "signal": "flight_recorder_dominant_latency_component",
                "count": component.get("count"),
                "total_us": component.get("total_us"),
            }
        )
    failures = payload.get("coordination_failures")
    if isinstance(failures, list) and failures:
        findings.append(
            {
                "surface": "queue_pressure",
                "source": source.id,
                "label": "coordination_failures",
                "signal": "flight_recorder_coordination_failures",
                "count": len(failures),
            }
        )
    return bounded(findings, max_items)


def extract_hostcall_bottlenecks(source: SourcePayload, max_items: int) -> list[dict[str, Any]]:
    payload = source.payload if isinstance(source.payload, dict) else {}
    profiles = payload.get("profiles") if isinstance(payload.get("profiles"), list) else []
    findings: list[dict[str, Any]] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        findings.append(
            {
                "surface": "extension_hostcalls",
                "source": source.id,
                "label": profile.get("mode") or profile.get("name"),
                "signal": "hostcall_swarm_profile",
                "accepted_requests": profile.get("accepted_requests"),
                "completed_requests": profile.get("completed_requests"),
                "p99_tail_latency_steps": profile.get("p99_tail_latency_steps"),
                "max_tail_latency_steps": profile.get("max_tail_latency_steps"),
            }
        )
    return bounded(findings, max_items)


def extract_session_bottlenecks(source: SourcePayload) -> list[dict[str, Any]]:
    payload = source.payload if isinstance(source.payload, dict) else {}
    timings = payload.get("timings_us") if isinstance(payload.get("timings_us"), dict) else {}
    if not timings:
        return []
    slowest = sorted(
        ((key, value) for key, value in timings.items() if isinstance(value, (int, float))),
        key=lambda item: item[1],
        reverse=True,
    )
    if not slowest:
        return []
    name, value = slowest[0]
    return [
        {
            "surface": "persistence",
            "source": source.id,
            "label": name,
            "signal": "session_recovery_swarm_profile_slowest_timing",
            "elapsed_us": value,
        }
    ]


def extract_rch_sync_bottlenecks(source: SourcePayload) -> list[dict[str, Any]]:
    payload = source.payload if isinstance(source.payload, dict) else {}
    violations = payload.get("violations") if isinstance(payload.get("violations"), list) else []
    status = payload.get("status")
    if not violations and status in {None, "pass", "ok"}:
        return []
    return [
        {
            "surface": "rch_sync_retrieval",
            "source": source.id,
            "label": "rch_artifact_sync",
            "signal": "artifact_sync_preflight",
            "status": status,
            "violation_count": len(violations),
        }
    ]


def extract_core_bottlenecks(runpack: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    rch = runpack["rch_admission"]
    queue_forecast = (
        rch.get("queue_forecast")
        if isinstance(rch.get("queue_forecast"), dict)
        else {}
    )
    if rch.get("decision") in {"backoff", "degraded", "deny"}:
        findings.append(
            {
                "surface": "rch_sync_retrieval",
                "source": "cargo_admission",
                "label": "cargo/RCH admission",
                "signal": "admission_decision",
                "decision": rch.get("decision"),
                "recommended_action": queue_forecast.get("recommended_action"),
                "slot_pressure": queue_forecast.get("slot_pressure"),
            }
        )
    if queue_forecast.get("recommended_action") in {"backoff", "split"}:
        findings.append(
            {
                "surface": "queue_pressure",
                "source": "cargo_admission",
                "label": "RCH queue forecast",
                "signal": "queue_forecast",
                "recommended_action": queue_forecast.get("recommended_action"),
                "queue_depth": queue_forecast.get("queue_depth"),
                "active_builds": queue_forecast.get("active_builds"),
                "queued_builds": queue_forecast.get("queued_builds"),
            }
        )
    activity = runpack["activity_digest"]
    if activity.get("saturated") is True:
        findings.append(
            {
                "surface": "queue_pressure",
                "source": "activity_digest",
                "label": "swarm activity saturation",
                "signal": "activity_digest_saturation",
                "reasons": activity.get("reasons"),
                "evidence_pointers": activity.get("evidence_pointers"),
            }
        )
    doctor = runpack["doctor_swarm"]
    severity_counts = (
        doctor.get("severity_counts")
        if isinstance(doctor.get("severity_counts"), dict)
        else {}
    )
    if doctor.get("overall") in {"warn", "fail"} or severity_counts.get("warn") or severity_counts.get("fail"):
        findings.append(
            {
                "surface": "cgroup_numa_context",
                "source": "doctor_swarm",
                "label": "doctor swarm findings",
                "signal": "doctor_swarm_overall",
                "overall": doctor.get("overall"),
                "severity_counts": severity_counts,
            }
        )
    return findings


def build_bottleneck_attribution(
    runpack: dict[str, Any],
    by_id: dict[str, SourcePayload],
    *,
    generated_at: datetime,
    stale_after_hours: int,
    max_items: int,
) -> dict[str, Any]:
    classifications: list[dict[str, Any]] = []
    for source_id in BOTTLENECK_CORE_SOURCE_IDS:
        source = by_id[source_id]
        classifications.append(
            classify_bottleneck_source(
                source,
                generated_at=generated_at,
                stale_after_hours=stale_after_hours,
                required=True,
            )
        )
    for source_id in BOTTLENECK_OPTIONAL_SOURCE_IDS:
        source = by_id.get(source_id, SourcePayload(source_id, None, "not_provided", None, None))
        classifications.append(
            classify_bottleneck_source(
                source,
                generated_at=generated_at,
                stale_after_hours=stale_after_hours,
                required=False,
            )
        )
    classifications_by_id = {item["id"]: item for item in classifications}
    surface_coverage = {
        surface_id: summarize_surface(surface_id, source_ids, classifications_by_id)
        for surface_id, source_ids in BOTTLENECK_SURFACES.items()
    }
    bottlenecks = extract_core_bottlenecks(runpack)
    if by_id.get("tail_latency") is not None:
        bottlenecks.extend(extract_tail_latency_bottlenecks(by_id["tail_latency"], max_items))
    if by_id.get("flight_recorder") is not None:
        bottlenecks.extend(extract_flight_recorder_bottlenecks(by_id["flight_recorder"], max_items))
    if by_id.get("hostcall_swarm_profile") is not None:
        bottlenecks.extend(extract_hostcall_bottlenecks(by_id["hostcall_swarm_profile"], max_items))
    if by_id.get("session_recovery_swarm_profile") is not None:
        bottlenecks.extend(extract_session_bottlenecks(by_id["session_recovery_swarm_profile"]))
    if by_id.get("rch_artifact_sync") is not None:
        bottlenecks.extend(extract_rch_sync_bottlenecks(by_id["rch_artifact_sync"]))
    blocked_sources = [
        item["id"] for item in classifications if item.get("classification") == "blocker"
    ]
    historical_sources = [
        item["id"] for item in classifications if item.get("classification") == "historical_snapshot"
    ]
    missing_optional = [
        item["id"] for item in classifications if item.get("classification") == "optional_diagnostic"
    ]
    blocked_surfaces = [
        surface_id
        for surface_id, surface in surface_coverage.items()
        if surface.get("status") == "blocked"
    ]
    status = "ready"
    if blocked_sources or historical_sources or blocked_surfaces:
        status = "degraded"
    return {
        "schema": BOTTLENECK_ATTRIBUTION_SCHEMA,
        "generated_at": generated_at.isoformat(),
        "status": status,
        "purpose": "operator_diagnostic_not_release_performance_claim",
        "stale_after_hours": stale_after_hours,
        "surface_coverage": surface_coverage,
        "input_classification": classifications,
        "bottlenecks": bounded(bottlenecks, max_items),
        "missing_optional_diagnostics": missing_optional,
        "historical_snapshots": historical_sources,
        "blocked_inputs": blocked_sources,
        "operator_notes": [
            "Use this dashboard for swarm bottleneck attribution only.",
            "Do not turn diagnostic evidence into release-facing performance or drop-in claims without claim-integrity gates.",
        ],
    }


def parse_issue_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("issues"), list):
        return [item for item in payload["issues"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def issue_priority(issue: dict[str, Any]) -> int:
    value = issue.get("priority")
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 99


def normalized_bead_candidate(issue: dict[str, Any]) -> dict[str, Any]:
    description = issue.get("description")
    if not isinstance(description, str):
        description = issue.get("body") if isinstance(issue.get("body"), str) else None
    candidate = {
        "id": issue.get("id"),
        "title": issue.get("title"),
        "status": issue.get("status"),
        "priority": issue_priority(issue),
        "assignee": issue.get("assignee"),
        "updated_at": issue.get("updated_at"),
        "labels": issue.get("labels") if isinstance(issue.get("labels"), list) else [],
    }
    if description:
        candidate["description"] = description
    return candidate


def sort_bead_candidates(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [normalized_bead_candidate(issue) for issue in issues]
    return sorted(
        normalized,
        key=lambda issue: (
            issue.get("priority", 99),
            str(issue.get("updated_at") or ""),
            str(issue.get("id") or ""),
        ),
    )


def summarize_beads(
    source: SourcePayload,
    *,
    generated_at: datetime,
    stale_after_hours: int,
    max_items: int,
) -> dict[str, Any]:
    issues = parse_issue_list(source.payload)
    status_counts = Counter(str(issue.get("status") or "unknown") for issue in issues)
    active = [issue for issue in issues if issue.get("status") in {"open", "in_progress"}]
    open_candidates = sort_bead_candidates(
        [issue for issue in issues if issue.get("status") == "open"]
    )
    stale: list[dict[str, Any]] = []
    for issue in active:
        updated_at = str(issue.get("updated_at") or "")
        try:
            updated = parse_utc(updated_at)
        except ValueError:
            age_hours = None
        else:
            age_hours = max(0.0, (generated_at - updated).total_seconds() / 3600)
        if age_hours is None or age_hours >= stale_after_hours:
            stale.append(
                {
                    "id": issue.get("id"),
                    "title": issue.get("title"),
                    "status": issue.get("status"),
                    "assignee": issue.get("assignee"),
                    "updated_at": updated_at,
                    "age_hours": round(age_hours, 2) if age_hours is not None else None,
                }
            )
    return {
        "status": source.status,
        "total_issues": len(issues),
        "status_counts": dict(sorted(status_counts.items())),
        "active_count": len(active),
        "open_candidate_count": len(open_candidates),
        "open_candidates": bounded(open_candidates, max_items),
        "stale_after_hours": stale_after_hours,
        "stale": bounded(stale, max_items),
    }


def summarize_beads_ready(source: SourcePayload, max_items: int) -> dict[str, Any]:
    issues = parse_issue_list(source.payload)
    ready_candidates = sort_bead_candidates(issues)
    return {
        "status": source.status,
        "ready_count": len(ready_candidates),
        "candidates": bounded(ready_candidates, max_items),
    }


def summarize_smoke_harness(source: SourcePayload, max_items: int) -> dict[str, Any]:
    payload = source.payload
    if not isinstance(payload, dict):
        return {"status": source.status}
    scenarios = payload.get("scenarios") if isinstance(payload.get("scenarios"), dict) else {}
    scenario_statuses = {
        name: scenario.get("status")
        for name, scenario in scenarios.items()
        if isinstance(scenario, dict)
    }
    return {
        "status": source.status,
        "harness_status": payload.get("status"),
        "correlation_id": payload.get("correlation_id"),
        "scenario_statuses": scenario_statuses,
        "failed_scenarios": bounded(payload.get("failed_scenarios") or [], max_items),
        "reservation_count": len(payload.get("reservation_ids") or []),
        "artifact_paths": payload.get("artifacts"),
        "artifact_manifest": bounded(payload.get("artifact_manifest") or [], max_items),
    }


def summarize_activity_digest(source: SourcePayload, max_items: int) -> dict[str, Any]:
    payload = source.payload
    if not isinstance(payload, dict):
        return {"status": source.status}
    saturation = payload.get("saturation") if isinstance(payload.get("saturation"), dict) else {}
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
    return {
        "status": source.status,
        "source_path": source.path,
        "saturated": saturation.get("saturated"),
        "signals": bounded(saturation.get("signals") or [], max_items),
        "reasons": bounded(saturation.get("reasons") or [], max_items),
        "evidence_pointers": bounded(saturation.get("evidence_pointers") or [], max_items),
        "recommendations": bounded(recommendations, max_items),
    }


def summarize_cargo_admission(source: SourcePayload) -> dict[str, Any]:
    payload = source.payload
    if not isinstance(payload, dict):
        return {"status": source.status}
    forecast = payload.get("rch_queue_forecast")
    queue_forecast = forecast if isinstance(forecast, dict) else {}
    return {
        "status": source.status,
        "decision": payload.get("decision"),
        "reason": payload.get("reason"),
        "requested_runner": payload.get("requested_runner"),
        "resolved_runner": payload.get("resolved_runner"),
        "command_class": payload.get("command_class"),
        "allow_local_fallback": payload.get("allow_local_fallback"),
        "cargo_target_dir": payload.get("cargo_target_dir"),
        "tmpdir": payload.get("tmpdir"),
        "storage_remediation": payload.get("storage_remediation"),
        "queue_forecast": {
            "status": queue_forecast.get("status"),
            "recommended_action": queue_forecast.get("recommended_action"),
            "reason": queue_forecast.get("reason"),
            "slot_pressure": queue_forecast.get("slot_pressure"),
            "queue_depth": queue_forecast.get("queue_depth"),
            "active_builds": queue_forecast.get("active_builds"),
            "queued_builds": queue_forecast.get("queued_builds"),
            "slots_available": queue_forecast.get("slots_available"),
            "slots_total": queue_forecast.get("slots_total"),
            "workers_healthy": queue_forecast.get("workers_healthy"),
            "workers_total": queue_forecast.get("workers_total"),
            "estimated_wait_seconds": queue_forecast.get("estimated_wait_seconds"),
        },
    }


def numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def nested_dict(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    item = value.get(key)
    return item if isinstance(item, dict) else {}


def resource_preflight_from_doctor(source: SourcePayload) -> dict[str, Any] | None:
    payload = source.payload
    if not isinstance(payload, dict):
        return None
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return None
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        data = finding.get("data")
        if isinstance(data, dict) and data.get("schema") == HOST_PREFLIGHT_SCHEMA:
            return data
    return None


def budget_recommendations(profile: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(profile, dict):
        return {}
    return nested_dict(profile, "recommended_budgets")


def budget_int(budgets: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = numeric_value(budgets.get(key))
        if value is not None:
            return max(0, int(value))
    return None


def resource_effective_cpu(profile: dict[str, Any] | None) -> float | None:
    cpu = nested_dict(profile, "cpu")
    return numeric_value(first_non_empty(cpu.get("effective_cores"), cpu.get("effective")))


def resource_effective_memory(profile: dict[str, Any] | None) -> float | None:
    memory = nested_dict(profile, "memory")
    return numeric_value(
        first_non_empty(
            memory.get("effective_limit_bytes"),
            memory.get("cgroup_limit_bytes"),
            profile.get("memory_limit_bytes") if isinstance(profile, dict) else None,
        )
    )


def headroom_paths_by_env(profile: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    tmpfs = nested_dict(profile, "tmpfs_headroom")
    paths = tmpfs.get("paths")
    if not isinstance(paths, list):
        return {}
    return {
        str(item.get("env_name")): item
        for item in paths
        if isinstance(item, dict) and item.get("env_name")
    }


def resource_profile_summary(profile: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(profile, dict):
        return {"status": "not_provided"}
    budgets = budget_recommendations(profile)
    return {
        "status": profile.get("status"),
        "generated_at": profile.get("generated_at"),
        "effective_cpu_cores": resource_effective_cpu(profile),
        "effective_memory_bytes": resource_effective_memory(profile),
        "critical_failures": profile.get("critical_failures") or [],
        "source_errors": profile.get("source_errors") or [],
        "recommended_budgets": {
            "agent_concurrency": budget_int(budgets, "agent_concurrency", "agent_fanout"),
            "rch_verification_fanout": budget_int(budgets, "rch_verification_fanout"),
            "max_queue_depth": budget_int(budgets, "max_queue_depth"),
            "max_rss_bytes": budget_int(budgets, "max_rss_bytes"),
        },
        "headroom_paths": {
            env_name: {
                "path": item.get("path"),
                "ready": item.get("ready"),
                "available_kb": item.get("available_kb"),
                "problem": item.get("problem"),
            }
            for env_name, item in headroom_paths_by_env(profile).items()
        },
    }


def add_budget_drift_signal(
    signals: list[dict[str, Any]],
    *,
    signal_id: str,
    severity: str,
    evidence_path: str,
    expected: Any,
    current: Any,
    recommendation: str,
) -> None:
    signals.append(
        {
            "id": signal_id,
            "severity": severity,
            "evidence_path": evidence_path,
            "expected": expected,
            "current": current,
            "recommendation": recommendation,
        }
    )


def budget_drift_status(signals: list[dict[str, Any]]) -> str:
    if any(signal.get("severity") == "critical" for signal in signals):
        return "deny_new_work"
    if signals:
        return "degraded"
    return "stable"


def budget_drift_adjustments(
    status: str,
    budgets: dict[str, Any],
    *,
    current_active_agents: int,
) -> dict[str, Any]:
    agent_budget = budget_int(budgets, "agent_concurrency", "agent_fanout")
    rch_budget = budget_int(budgets, "rch_verification_fanout")
    queue_budget = budget_int(budgets, "max_queue_depth")
    if status == "deny_new_work":
        return {
            "admit_new_agents": 0,
            "agent_concurrency": current_active_agents,
            "rch_verification_fanout": 0,
            "max_queue_depth": 0,
            "reason": "deny_new_work until critical budget drift clears",
        }
    if status == "degraded":
        reduced_agent_budget = None if agent_budget is None else max(1, agent_budget // 2)
        reduced_rch_budget = None if rch_budget is None else max(1, rch_budget // 2)
        return {
            "admit_new_agents": None
            if reduced_agent_budget is None
            else max(0, reduced_agent_budget - current_active_agents),
            "agent_concurrency": reduced_agent_budget,
            "rch_verification_fanout": reduced_rch_budget,
            "max_queue_depth": queue_budget,
            "reason": "reduce fanout until two stable drift samples are observed",
        }
    return {
        "admit_new_agents": None
        if agent_budget is None
        else max(0, agent_budget - current_active_agents),
        "agent_concurrency": agent_budget,
        "rch_verification_fanout": rch_budget,
        "max_queue_depth": queue_budget,
        "reason": "last accepted budget profile is still valid",
    }


def build_budget_drift_report(
    *,
    accepted_profile_source: SourcePayload,
    doctor_source: SourcePayload,
    cargo: dict[str, Any],
    beads: dict[str, Any],
    agent_mail: dict[str, Any],
    max_items: int,
) -> dict[str, Any]:
    current_profile = resource_preflight_from_doctor(doctor_source)
    accepted_profile = (
        accepted_profile_source.payload
        if isinstance(accepted_profile_source.payload, dict)
        else current_profile
    )
    profile_status = "ok"
    if accepted_profile_source.status != "ok" and current_profile is not None:
        profile_status = "current_only"
    elif accepted_profile is None:
        profile_status = accepted_profile_source.status

    signals: list[dict[str, Any]] = []
    if accepted_profile is None and current_profile is None:
        add_budget_drift_signal(
            signals,
            signal_id="missing_budget_profile",
            severity="warning",
            evidence_path="source_statuses.host_preflight",
            expected="last accepted budget profile",
            current="not_provided",
            recommendation="capture pi doctor --only swarm --format json before raising fanout",
        )

    current_profile = current_profile or accepted_profile
    accepted_summary = resource_profile_summary(accepted_profile)
    current_summary = resource_profile_summary(current_profile)
    accepted_budgets = budget_recommendations(accepted_profile)

    accepted_cpu = resource_effective_cpu(accepted_profile)
    current_cpu = resource_effective_cpu(current_profile)
    if accepted_cpu and current_cpu and current_cpu < accepted_cpu:
        ratio = current_cpu / accepted_cpu
        add_budget_drift_signal(
            signals,
            signal_id="cpu_quota_reduced",
            severity="critical" if ratio < 0.5 else "warning",
            evidence_path="normalized_inputs.budget_drift.current_profile.effective_cpu_cores",
            expected=accepted_cpu,
            current=current_cpu,
            recommendation="lower agent and RCH fanout to match current cgroup CPU quota",
        )

    accepted_memory = resource_effective_memory(accepted_profile)
    current_memory = resource_effective_memory(current_profile)
    if accepted_memory and current_memory and current_memory < accepted_memory:
        ratio = current_memory / accepted_memory
        add_budget_drift_signal(
            signals,
            signal_id="memory_headroom_reduced",
            severity="critical" if ratio < 0.5 else "warning",
            evidence_path="normalized_inputs.budget_drift.current_profile.effective_memory_bytes",
            expected=int(accepted_memory),
            current=int(current_memory),
            recommendation="reduce active agents and avoid broad validation until memory headroom recovers",
        )

    for env_name, current_path in (
        ("CARGO_TARGET_DIR", cargo.get("cargo_target_dir")),
        ("TMPDIR", cargo.get("tmpdir")),
    ):
        accepted_path = headroom_paths_by_env(accepted_profile).get(env_name, {}).get("path")
        if accepted_path and current_path and str(accepted_path) != str(current_path):
            add_budget_drift_signal(
                signals,
                signal_id=f"{env_name.lower()}_path_drift",
                severity="warning",
                evidence_path=f"normalized_inputs.cargo_admission.{env_name.lower()}",
                expected=accepted_path,
                current=current_path,
                recommendation=f"re-run preflight with the current {env_name} before increasing fanout",
            )
    for env_name, item in headroom_paths_by_env(current_profile).items():
        if item.get("ready") is False:
            add_budget_drift_signal(
                signals,
                signal_id=f"{env_name.lower()}_headroom_not_ready",
                severity="critical",
                evidence_path=f"normalized_inputs.budget_drift.current_profile.headroom_paths.{env_name}",
                expected="ready",
                current=item.get("problem") or "not_ready",
                recommendation=f"fix {env_name} scratch headroom before admitting new work",
            )

    critical_failures = current_summary.get("critical_failures")
    if isinstance(critical_failures, list):
        for failure in critical_failures:
            add_budget_drift_signal(
                signals,
                signal_id="resource_preflight_critical_failure",
                severity="critical",
                evidence_path="normalized_inputs.budget_drift.current_profile.critical_failures",
                expected="no critical failures",
                current=failure,
                recommendation="deny new work until swarm resource preflight is no longer failing",
            )

    queue = cargo.get("queue_forecast") if isinstance(cargo.get("queue_forecast"), dict) else {}
    max_queue_depth = budget_int(accepted_budgets, "max_queue_depth")
    queue_depth = int_value(queue.get("queue_depth"))
    if max_queue_depth is not None and queue_depth > max_queue_depth:
        add_budget_drift_signal(
            signals,
            signal_id="rch_queue_depth_over_budget",
            severity="critical",
            evidence_path="normalized_inputs.cargo_admission.queue_forecast.queue_depth",
            expected=max_queue_depth,
            current=queue_depth,
            recommendation="pause broad validation until RCH queue depth returns under budget",
        )
    if queue.get("recommended_action") == "backoff" or queue.get("slot_pressure") == "saturated":
        add_budget_drift_signal(
            signals,
            signal_id="rch_queue_saturated",
            severity="critical",
            evidence_path="normalized_inputs.cargo_admission.queue_forecast",
            expected="proceed",
            current=queue.get("recommended_action") or queue.get("slot_pressure"),
            recommendation="deny new heavyweight work until RCH queue pressure clears",
        )
    elif queue.get("recommended_action") == "split":
        add_budget_drift_signal(
            signals,
            signal_id="rch_queue_split_recommended",
            severity="warning",
            evidence_path="normalized_inputs.cargo_admission.queue_forecast.recommended_action",
            expected="proceed",
            current="split",
            recommendation="keep verification surface-scoped until queue pressure clears",
        )

    status_counts = beads.get("status_counts") if isinstance(beads.get("status_counts"), dict) else {}
    active_agents = max(
        int_value(status_counts.get("in_progress")),
        int_value(agent_mail.get("active_reservation_count")),
    )
    agent_budget = budget_int(accepted_budgets, "agent_concurrency", "agent_fanout")
    if agent_budget is not None:
        if active_agents > agent_budget:
            add_budget_drift_signal(
                signals,
                signal_id="active_agents_over_budget",
                severity="critical",
                evidence_path="normalized_inputs.beads.status_counts.in_progress",
                expected=agent_budget,
                current=active_agents,
                recommendation="do not admit more agents until active ownership drops under budget",
            )
        elif active_agents >= max(1, int(agent_budget * 0.8)):
            add_budget_drift_signal(
                signals,
                signal_id="active_agents_near_budget",
                severity="warning",
                evidence_path="normalized_inputs.beads.status_counts.in_progress",
                expected=f"<80% of {agent_budget}",
                current=active_agents,
                recommendation="avoid increasing fanout unless the next sample remains stable",
            )

    status = budget_drift_status(signals)
    return {
        "schema": BUDGET_DRIFT_SCHEMA,
        "status": status,
        "profile_status": profile_status,
        "accepted_profile": accepted_summary,
        "current_profile": current_summary,
        "current_observation": {
            "cargo_target_dir": cargo.get("cargo_target_dir"),
            "tmpdir": cargo.get("tmpdir"),
            "queue_depth": queue_depth,
            "active_builds": queue.get("active_builds"),
            "queued_builds": queue.get("queued_builds"),
            "slot_pressure": queue.get("slot_pressure"),
            "queue_recommended_action": queue.get("recommended_action"),
            "active_agents": active_agents,
            "active_reservations": agent_mail.get("active_reservation_count"),
        },
        "signals": bounded(signals, max_items),
        "recommended_adjustments": budget_drift_adjustments(
            status,
            accepted_budgets,
            current_active_agents=active_agents,
        ),
        "hysteresis": {
            "stable_samples_required": 2,
            "degraded_recovery_policy": "hold reduced fanout until two consecutive stable samples",
        },
    }


def replay_budget_drift_hysteresis(
    reports: list[dict[str, Any]],
    *,
    stable_samples_required: int = 2,
) -> dict[str, Any]:
    effective_statuses: list[str] = []
    stable_run = 0
    holding_recovery = False
    saw_drift = False
    for report in reports:
        status = str(report.get("status") or "degraded")
        if status == "stable":
            stable_run += 1
            if saw_drift and stable_run < stable_samples_required:
                effective_statuses.append("degraded")
                holding_recovery = True
            else:
                effective_statuses.append("stable")
        else:
            stable_run = 0
            saw_drift = True
            effective_statuses.append(status)
    return {
        "schema": BUDGET_DRIFT_SCHEMA,
        "stable_samples_required": stable_samples_required,
        "effective_statuses": effective_statuses,
        "hysteresis_applied": holding_recovery,
    }


def summarize_git_status(source: SourcePayload, max_items: int) -> dict[str, Any]:
    payload = source.payload
    if not isinstance(payload, dict):
        return {"status": source.status}
    lines = payload.get("porcelain_lines") if isinstance(payload.get("porcelain_lines"), list) else []
    entries = []
    for line in lines:
        text = str(line)
        entries.append({"status": text[:2], "path": text[3:] if len(text) > 3 else text})
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), dict) else {}
    return {
        "status": source.status,
        "schema": payload.get("schema"),
        "generated_at": payload.get("generated_at"),
        "branch": payload.get("branch"),
        "head": payload.get("head"),
        "upstream": {
            "name": upstream.get("name"),
            "ahead": upstream.get("ahead"),
            "behind": upstream.get("behind"),
            "status": upstream.get("status"),
        },
        "dirty": bool(lines),
        "change_count": len(lines),
        "sample": bounded(entries, max_items),
        "recent_commits": bounded(payload.get("recent_commits") or [], max_items),
        "recent_remote_commits": bounded(
            payload.get("recent_remote_commits") or [], max_items
        ),
    }


def summarize_operator_runpack(source: SourcePayload, max_items: int) -> dict[str, Any]:
    payload = source.payload
    if not isinstance(payload, dict):
        return {"status": source.status}
    scorecard = payload.get("swarm_scale_safety_scorecard")
    if not isinstance(scorecard, dict):
        scorecard = {}
    bottleneck = payload.get("bottleneck_attribution")
    if not isinstance(bottleneck, dict):
        bottleneck = {}
    actions = payload.get("operator_next_actions")
    if not isinstance(actions, list):
        actions = []
    return {
        "status": source.status,
        "schema": payload.get("schema"),
        "generated_at": payload.get("generated_at"),
        "runpack_status": payload.get("status"),
        "scorecard_status": scorecard.get("overall_status"),
        "bottleneck_status": bottleneck.get("status"),
        "operator_next_actions": bounded(actions, max_items),
    }


def command_provenance(
    capture_summary: dict[str, Any],
    max_items: int,
) -> list[dict[str, Any]]:
    commands = capture_summary.get("commands")
    if not isinstance(commands, list):
        return []
    provenance: list[dict[str, Any]] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        provenance.append(
            {
                "id": command.get("id"),
                "command": command.get("command"),
                "cwd": command.get("cwd"),
                "started_at": command.get("started_at"),
                "status": command.get("status"),
                "exit_code": command.get("exit_code"),
                "issue": command.get("issue"),
                "stdout_path": command.get("stdout_path"),
                "stdout_snippet": command.get("stdout_snippet"),
                "stderr_snippet": command.get("stderr_snippet"),
                "redaction_summary": command.get("redaction_summary"),
            }
        )
    return bounded(provenance, max_items)


def merge_command_redaction(capture_summary: dict[str, Any]) -> RedactionStats:
    stats = RedactionStats()
    commands = capture_summary.get("commands")
    if not isinstance(commands, list):
        return stats
    for command in commands:
        if not isinstance(command, dict):
            continue
        redaction = command.get("redaction_summary")
        if not isinstance(redaction, dict):
            continue
        stats.redacted_count += int_value(redaction.get("redacted_count"))
        fields = redaction.get("fields")
        if isinstance(fields, list):
            stats.fields.update(str(field) for field in fields)
    return stats


def command_status(commands: list[dict[str, Any]], command_id: str) -> str:
    for command in commands:
        if command.get("id") == command_id:
            value = command.get("status")
            return str(value) if value is not None else "unknown"
    return "not_captured"


def agent_mail_source_status(source: SourcePayload) -> str:
    if source.status != "ok":
        return source.status
    payload = source.payload
    if not isinstance(payload, dict):
        return "ok"
    health_level = str(payload.get("health_level") or "").lower()
    status = str(payload.get("status") or payload.get("overall_status") or "ok").lower()
    if health_level in {"red", "error", "critical"}:
        return "degraded"
    if status in {"error", "failed", "fail", "degraded", "red"}:
        return "degraded"
    return status


def reservation_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("reservations", "active_reservations", "granted", "items", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def first_non_empty(*values: Any) -> Any | None:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def reservation_path(item: dict[str, Any]) -> str | None:
    value = first_non_empty(
        item.get("path"),
        item.get("path_pattern"),
        item.get("pathPattern"),
        item.get("glob"),
        item.get("file"),
    )
    return str(value) if value is not None else None


def summarize_reservation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "agent": first_non_empty(
            item.get("agent"),
            item.get("agent_name"),
            item.get("holder"),
            item.get("owner"),
        ),
        "path": reservation_path(item),
        "exclusive": item.get("exclusive"),
        "reason": item.get("reason"),
        "expires_ts": item.get("expires_ts"),
        "released_ts": item.get("released_ts"),
    }


def summarize_agent_mail_autopilot(
    status_source: SourcePayload,
    reservations_source: SourcePayload,
    capture_summary: dict[str, Any],
    max_items: int,
) -> dict[str, Any]:
    commands = [
        command
        for command in command_provenance(capture_summary, max_items)
        if str(command.get("id") or "").startswith("agent_mail")
    ]
    command_statuses = [str(command.get("status")) for command in commands]
    if not commands:
        status = "not_captured"
    elif all(item == "ok" for item in command_statuses):
        status = "ok"
    elif any(item in {"failed", "timeout"} for item in command_statuses):
        status = "degraded"
    else:
        status = "not_available"
    read_status = agent_mail_source_status(status_source)
    reservation_status = agent_mail_source_status(reservations_source)
    if status_source.status == "ok" or reservations_source.status == "ok":
        status = "ok"
        if read_status != "ok" or reservation_status != "ok":
            status = "degraded"
    reservations = reservation_items(reservations_source.payload)
    active_reservations = [
        item for item in reservations if item.get("released_ts") in {None, ""}
    ]
    return {
        "status": status,
        "capture_mode": capture_summary.get("mode"),
        "read_status": read_status,
        "reservation_status": reservation_status,
        "reservation_count": len(reservations),
        "active_reservation_count": len(active_reservations),
        "active_reservations": bounded(
            [summarize_reservation(item) for item in active_reservations],
            max_items,
        ),
        "fallback_action": "use_beads_soft_lock" if status != "ok" else None,
        "commands": commands,
    }


def classify_autopilot_source(
    source: SourcePayload,
    *,
    generated_at: datetime,
    stale_after_hours: int,
    required: bool,
) -> dict[str, Any]:
    if source.status != "ok":
        return {
            "id": source.id,
            "required": required,
            "status": source.status,
            "schema": source.schema,
            "classification": "blocker" if required else "optional_missing",
            "freshness_hours": None,
            "timestamp": None,
            "issue": source.issue or "source was not provided",
        }
    timestamp = top_level_timestamp(source.payload)
    if timestamp is None:
        return {
            "id": source.id,
            "required": required,
            "status": source.status,
            "schema": source.schema,
            "classification": "freshness_unknown",
            "freshness_hours": None,
            "timestamp": None,
            "issue": "source has no top-level timestamp",
        }
    try:
        source_time = parse_utc(timestamp)
    except ValueError:
        return {
            "id": source.id,
            "required": required,
            "status": source.status,
            "schema": source.schema,
            "classification": "blocker",
            "freshness_hours": None,
            "timestamp": timestamp,
            "issue": "source timestamp is invalid",
        }
    age_hours = (generated_at - source_time).total_seconds() / 3600
    if age_hours < 0:
        return {
            "id": source.id,
            "required": required,
            "status": source.status,
            "schema": source.schema,
            "classification": "blocker",
            "freshness_hours": round(age_hours, 2),
            "timestamp": source_time.isoformat(),
            "issue": "source timestamp is in the future",
        }
    if age_hours > stale_after_hours:
        return {
            "id": source.id,
            "required": required,
            "status": source.status,
            "schema": source.schema,
            "classification": "stale",
            "freshness_hours": round(age_hours, 2),
            "timestamp": source_time.isoformat(),
            "issue": f"source is older than stale_after_hours={stale_after_hours}",
        }
    return {
        "id": source.id,
        "required": required,
        "status": source.status,
        "schema": source.schema,
        "classification": "fresh",
        "freshness_hours": round(age_hours, 2),
        "timestamp": source_time.isoformat(),
        "issue": None,
    }


def derive_autopilot_input_pack_status(
    pack: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    status_by_id = {
        str(item.get("id")): str(item.get("status"))
        for item in pack.get("source_statuses", [])
        if isinstance(item, dict)
    }
    for source_id in AUTOPILOT_REQUIRED_SOURCE_IDS:
        if status_by_id.get(source_id) != "ok":
            reasons.append(f"required source `{source_id}` status is `{status_by_id.get(source_id)}`")
    for item in pack.get("source_classification", []):
        if not isinstance(item, dict) or item.get("required") is not True:
            continue
        if item.get("classification") in {"blocker", "stale"}:
            reasons.append(f"required source `{item.get('id')}` is {item.get('classification')}")
    agent_mail = pack.get("normalized_inputs", {}).get("agent_mail", {})
    if isinstance(agent_mail, dict) and agent_mail.get("status") != "ok":
        reasons.append(f"Agent Mail status is `{agent_mail.get('status')}`")
    budget_drift = pack.get("normalized_inputs", {}).get("budget_drift", {})
    if isinstance(budget_drift, dict) and budget_drift.get("status") in {
        "degraded",
        "deny_new_work",
    }:
        reasons.append(f"budget drift status is `{budget_drift.get('status')}`")
    if not pack.get("command_provenance"):
        reasons.append("command provenance was not captured")
    return ("degraded" if reasons else "ready", reasons)


def build_autopilot_input_pack(args: argparse.Namespace) -> dict[str, Any]:
    generated_at = parse_utc(args.generated_at) if args.generated_at else parse_utc(utc_now_iso())
    sources = autopilot_source_payloads(args)
    by_id = {source.id: source for source in sources}
    capture_summary = capture_summary_from_args(args)
    redaction = RedactionStats()
    for source in sources:
        redaction.redacted_count += source.redacted_count
        redaction.fields.update(source.redacted_fields)
    redaction.merge(merge_command_redaction(capture_summary))
    doctor_summary = summarize_doctor(by_id["doctor_swarm"], args.max_items)
    cargo_summary = summarize_cargo_admission(by_id["cargo_admission"])
    beads_summary = summarize_beads(
        by_id["beads"],
        generated_at=generated_at,
        stale_after_hours=args.stale_after_hours,
        max_items=args.max_items,
    )
    agent_mail_summary = summarize_agent_mail_autopilot(
        by_id["agent_mail_status"],
        by_id["agent_mail_reservations"],
        capture_summary,
        args.max_items,
    )
    budget_drift = build_budget_drift_report(
        accepted_profile_source=by_id["host_preflight"],
        doctor_source=by_id["doctor_swarm"],
        cargo=cargo_summary,
        beads=beads_summary,
        agent_mail=agent_mail_summary,
        max_items=args.max_items,
    )
    pack = {
        "schema": AUTOPILOT_INPUT_PACK_SCHEMA,
        "generated_at": generated_at.isoformat(),
        "status": "unknown",
        "purpose": "dry_run_swarm_autopilot_input_not_source_of_truth",
        "stale_after_hours": args.stale_after_hours,
        "capture": capture_summary,
        "source_statuses": [source.to_status() for source in sources],
        "source_classification": [
            classify_autopilot_source(
                source,
                generated_at=generated_at,
                stale_after_hours=args.stale_after_hours,
                required=source.id in AUTOPILOT_REQUIRED_SOURCE_IDS,
            )
            for source in sources
        ],
        "command_provenance": command_provenance(capture_summary, args.max_items),
        "normalized_inputs": {
            "doctor_swarm": doctor_summary,
            "cargo_admission": cargo_summary,
            "beads": beads_summary,
            "beads_ready": summarize_beads_ready(
                by_id["beads_ready"],
                args.max_items,
            ),
            "agent_mail": agent_mail_summary,
            "budget_drift": budget_drift,
            "git_state": summarize_git_status(by_id["git_status"], args.max_items),
            "activity_digest": summarize_activity_digest(
                by_id["activity_digest"],
                args.max_items,
            ),
            "operator_runpack": summarize_operator_runpack(
                by_id["operator_runpack"],
                args.max_items,
            ),
        },
        "redaction_summary": redaction.to_json(),
        "planner_guards": {
            "dry_run_only": True,
            "source_of_truth": "upstream_source_artifacts",
            "no_prose_scraping": True,
            "requires_command_provenance": True,
            "forbidden_actions": list(AUTOPILOT_FORBIDDEN_ACTIONS),
        },
        "degraded_reasons": [],
    }
    status, reasons = derive_autopilot_input_pack_status(pack)
    pack["status"] = status
    pack["degraded_reasons"] = reasons
    return pack


def plan_command(purpose: str, command: str) -> dict[str, str]:
    return {"purpose": purpose, "command": command}


def omitted_command(action: str, reason: str) -> dict[str, str]:
    return {"action": action, "reason": reason}


def normalized_section(input_pack: dict[str, Any], section: str) -> dict[str, Any]:
    normalized = input_pack.get("normalized_inputs")
    if not isinstance(normalized, dict):
        return {}
    value = normalized.get(section)
    return value if isinstance(value, dict) else {}


def first_candidate(candidates: Any) -> dict[str, Any] | None:
    if not isinstance(candidates, list) or not candidates:
        return None
    candidate = candidates[0]
    return candidate if isinstance(candidate, dict) else None


def unique_strings(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def issue_search_text(issue: dict[str, Any]) -> str:
    fragments: list[str] = []
    for key in ("title", "description", "body"):
        value = issue.get(key)
        if isinstance(value, str):
            fragments.append(value)
    labels = issue.get("labels")
    if isinstance(labels, list):
        fragments.extend(str(label) for label in labels)
    return " ".join(fragments).lower()


def infer_issue_work_surfaces(issue: dict[str, Any]) -> list[dict[str, Any]]:
    search_text = issue_search_text(issue)
    matches: list[dict[str, Any]] = []
    for rule in WORK_SURFACE_RULES:
        keywords = rule.get("keywords")
        if not isinstance(keywords, tuple):
            continue
        if any(str(keyword).lower() in search_text for keyword in keywords):
            matches.append(rule)
    return matches


def surface_reservation_globs(surfaces: list[dict[str, Any]]) -> list[str]:
    globs: list[str] = []
    for surface in surfaces:
        suggested = surface.get("suggested_reservation")
        if not isinstance(suggested, tuple):
            continue
        globs.extend(str(item) for item in suggested)
    if not globs:
        return [WORK_PARTITION_INSPECT_SENTINEL]
    return unique_strings(tuple(globs))


def glob_static_prefix(pattern: str) -> str:
    wildcard_index = len(pattern)
    for token in ("*", "?", "["):
        found = pattern.find(token)
        if found >= 0:
            wildcard_index = min(wildcard_index, found)
    return pattern[:wildcard_index].rstrip("/")


def path_patterns_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == WORK_PARTITION_INSPECT_SENTINEL or right == WORK_PARTITION_INSPECT_SENTINEL:
        return False
    if left == right:
        return True
    if fnmatch.fnmatchcase(right, left) or fnmatch.fnmatchcase(left, right):
        return True
    left_prefix = glob_static_prefix(left)
    right_prefix = glob_static_prefix(right)
    if left_prefix and right_prefix:
        return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)
    return False


def overlaps_any(pattern: str, candidates: list[str]) -> bool:
    return any(path_patterns_overlap(pattern, candidate) for candidate in candidates)


def dedupe_avoid_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str | None]] = set()
    out: list[dict[str, Any]] = []
    for entry in entries:
        key = (
            str(entry.get("source") or ""),
            str(entry.get("path") or ""),
            entry.get("holder") if isinstance(entry.get("holder"), str) else None,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def alternate_surface_options(
    surface_ids: set[str],
    hot_patterns: list[str],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for rule in WORK_SURFACE_RULES:
        surface_id = str(rule.get("id") or "")
        if surface_id in surface_ids:
            continue
        suggested = rule.get("suggested_reservation")
        if not isinstance(suggested, tuple):
            continue
        globs = [str(item) for item in suggested]
        if any(overlaps_any(pattern, hot_patterns) for pattern in globs):
            continue
        options.append(
            {
                "surface_id": surface_id,
                "suggested_reservation": globs,
            }
        )
    return bounded(options, max_items)


def build_work_partition_recommendations(
    input_pack: dict[str, Any],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    beads_ready = normalized_section(input_pack, "beads_ready")
    ready_candidates = beads_ready.get("candidates")
    if not isinstance(ready_candidates, list):
        return []
    agent_mail = normalized_section(input_pack, "agent_mail")
    git_state = normalized_section(input_pack, "git_state")
    beads = normalized_section(input_pack, "beads")
    active_reservations = agent_mail.get("active_reservations")
    if not isinstance(active_reservations, list):
        active_reservations = []
    dirty_entries = git_state.get("sample") if isinstance(git_state.get("sample"), list) else []
    stale_candidates = beads.get("stale") if isinstance(beads.get("stale"), list) else []
    reservation_evidence_degraded = (
        agent_mail.get("status") != "ok" or agent_mail.get("reservation_status") != "ok"
    )
    git_evidence_degraded = git_state.get("status") not in {"ok", None}
    partitions: list[dict[str, Any]] = []
    for candidate in ready_candidates:
        if not isinstance(candidate, dict):
            continue
        issue_id = str(candidate.get("id") or "<issue-id>")
        surfaces = infer_issue_work_surfaces(candidate)
        surface_ids = {str(surface.get("id") or "") for surface in surfaces}
        suggested_reservation = surface_reservation_globs(surfaces)
        avoid: list[dict[str, Any]] = []
        degraded_caveats: list[str] = []
        if reservation_evidence_degraded:
            degraded_caveats.append(
                "Agent Mail reservation evidence is degraded or unavailable; verify Beads ownership before trusting exclusivity."
            )
        if git_evidence_degraded:
            degraded_caveats.append(
                "Git dirty-path evidence is degraded or unavailable; inspect git status before reserving."
            )
        if not surfaces:
            degraded_caveats.append(
                "No known file family matched the bead labels, title, or body; inspect the bead before reserving files."
            )
        for reservation in active_reservations:
            if not isinstance(reservation, dict):
                continue
            path = reservation_path(reservation)
            if path is None:
                continue
            if any(path_patterns_overlap(pattern, path) for pattern in suggested_reservation):
                avoid.append(
                    {
                        "source": "agent_mail",
                        "path": path,
                        "holder": reservation.get("agent"),
                        "reason": "active Agent Mail reservation overlaps the suggested file family",
                    }
                )
        for dirty_entry in dirty_entries:
            if not isinstance(dirty_entry, dict):
                continue
            path = dirty_entry.get("path")
            if not isinstance(path, str):
                continue
            if any(path_patterns_overlap(pattern, path) for pattern in suggested_reservation):
                avoid.append(
                    {
                        "source": "git_state",
                        "path": path,
                        "holder": None,
                        "reason": f"worktree already has a dirty path with status {dirty_entry.get('status')}",
                    }
                )
        for stale in stale_candidates:
            if not isinstance(stale, dict) or str(stale.get("id") or "") == issue_id:
                continue
            stale_surfaces = infer_issue_work_surfaces(stale)
            stale_surface_ids = {str(surface.get("id") or "") for surface in stale_surfaces}
            if not surface_ids.intersection(stale_surface_ids):
                continue
            for path in surface_reservation_globs(stale_surfaces):
                if any(path_patterns_overlap(pattern, path) for pattern in suggested_reservation):
                    avoid.append(
                        {
                            "source": "beads",
                            "path": path,
                            "holder": stale.get("assignee"),
                            "reason": f"stale in-progress bead {stale.get('id')} may already own this surface",
                        }
                    )
        avoid = dedupe_avoid_entries(avoid)
        confidence = "high"
        if not surfaces:
            confidence = "low"
        elif avoid or degraded_caveats or len(surface_ids) > 1:
            confidence = "medium"
        hot_patterns = [entry["path"] for entry in avoid if isinstance(entry.get("path"), str)]
        hot_patterns.extend(
            reservation_path(item)
            for item in active_reservations
            if isinstance(item, dict) and reservation_path(item) is not None
        )
        hot_patterns.extend(
            entry.get("path")
            for entry in dirty_entries
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        )
        partitions.append(
            {
                "issue_id": issue_id,
                "title": candidate.get("title"),
                "status": candidate.get("status"),
                "priority": candidate.get("priority"),
                "assignee": candidate.get("assignee"),
                "surface_ids": sorted(surface_ids) if surface_ids else ["unknown"],
                "suggested_reservation": suggested_reservation,
                "avoid": avoid,
                "alternate_surfaces": alternate_surface_options(
                    surface_ids,
                    [str(pattern) for pattern in hot_patterns if pattern],
                    max_items=2,
                )
                if avoid
                else [],
                "confidence": confidence,
                "degraded_caveats": degraded_caveats,
                "evidence_paths": [
                    "normalized_inputs.beads_ready.candidates",
                    "normalized_inputs.agent_mail.active_reservations",
                    "normalized_inputs.git_state.sample",
                    "normalized_inputs.beads.stale",
                ],
            }
        )
    return bounded(partitions, max_items)


def failure_signal_text(value: Any) -> str:
    if isinstance(value, dict):
        fragments: list[str] = []
        for key in (
            "id",
            "status",
            "exit_code",
            "issue",
            "command",
            "stdout_snippet",
            "stderr_snippet",
            "reason",
            "decision",
            "read_status",
            "reservation_status",
            "fallback_action",
            "title",
            "assignee",
            "age_hours",
        ):
            item = value.get(key)
            if item not in {None, ""}:
                fragments.append(f"{key}={item}")
        return " ".join(fragments)
    return str(value)


def add_failure_signal(
    signals: list[dict[str, Any]],
    *,
    source: str,
    evidence_path: str,
    payload: Any,
    active: bool = True,
) -> None:
    if not active:
        return
    text = failure_signal_text(payload).strip()
    if not text:
        return
    signals.append(
        {
            "source": source,
            "evidence_path": evidence_path,
            "text": text,
        }
    )


def gather_failure_signals(input_pack: dict[str, Any]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for index, command in enumerate(input_pack.get("command_provenance", [])):
        if not isinstance(command, dict):
            continue
        add_failure_signal(
            signals,
            source=f"command:{command.get('id')}",
            evidence_path=f"command_provenance[{index}]",
            payload=command,
            active=command.get("status") not in {None, "ok"},
        )
    for index, source in enumerate(input_pack.get("source_statuses", [])):
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("id") or "")
        add_failure_signal(
            signals,
            source=f"source:{source.get('id')}",
            evidence_path=f"source_statuses[{index}]",
            payload=source,
            active=(
                source.get("status") not in {None, "ok"}
                and source_id in AUTOPILOT_REQUIRED_SOURCE_IDS
            ),
        )
    cargo = normalized_section(input_pack, "cargo_admission")
    if cargo:
        queue = cargo.get("queue_forecast") if isinstance(cargo.get("queue_forecast"), dict) else {}
        add_failure_signal(
            signals,
            source="cargo_admission",
            evidence_path="normalized_inputs.cargo_admission",
            payload={
                "decision": cargo.get("decision"),
                "reason": cargo.get("reason"),
                "status": cargo.get("status"),
                "queue_reason": queue.get("reason"),
                "queue_action": queue.get("recommended_action"),
                "slot_pressure": queue.get("slot_pressure"),
            },
            active=(
                cargo.get("status") != "ok"
                or cargo.get("decision") in {"backoff", "degraded", "deny"}
                or queue.get("recommended_action") in {"backoff", "split"}
                or queue.get("slot_pressure") == "saturated"
            ),
        )

    agent_mail = normalized_section(input_pack, "agent_mail")
    if agent_mail:
        add_failure_signal(
            signals,
            source="agent_mail",
            evidence_path="normalized_inputs.agent_mail",
            payload=agent_mail,
            active=agent_mail.get("status") != "ok",
        )

    beads = normalized_section(input_pack, "beads")
    stale = beads.get("stale") if isinstance(beads.get("stale"), list) else []
    for index, item in enumerate(stale):
        if not isinstance(item, dict):
            continue
        add_failure_signal(
            signals,
            source=f"beads:{item.get('id')}",
            evidence_path=f"normalized_inputs.beads.stale[{index}]",
            payload={
                **item,
                "reason": "stale in-progress Beads owner",
            },
        )
    return signals


def rule_matches_failure_signal(rule: dict[str, Any], signal_text: str) -> bool:
    text = signal_text.lower()
    terms = tuple(str(term).lower() for term in rule.get("terms", ()))
    secondary_terms = tuple(str(term).lower() for term in rule.get("secondary_terms", ()))
    if not terms:
        return False
    if not any(term in text for term in terms):
        return False
    return not secondary_terms or any(term in text for term in secondary_terms)


def build_failure_action(
    rule: dict[str, Any],
    signal: dict[str, Any],
) -> dict[str, Any]:
    excerpt, redaction = redact_string(
        bounded_text(str(signal.get("text") or ""), FAILURE_ACTION_MAX_EXCERPT_CHARS),
        f"failure_actions.{rule.get('id')}.raw_excerpt",
    )
    return {
        "id": rule.get("id"),
        "catalog_schema": FAILURE_ACTION_CATALOG_SCHEMA,
        "category": rule.get("category"),
        "title": rule.get("title"),
        "match_confidence": rule.get("confidence"),
        "explanation": rule.get("explanation"),
        "evidence_paths": [signal.get("evidence_path")],
        "matched_source": signal.get("source"),
        "safe_commands": [
            plan_command(str(purpose), str(command))
            for purpose, command in rule.get("safe_commands", ())
        ],
        "escalation": rule.get("escalation"),
        "raw_excerpt": excerpt,
        "redaction_summary": redaction.to_json(),
    }


def merge_failure_action_evidence(
    action: dict[str, Any],
    signal: dict[str, Any],
) -> None:
    evidence_path = signal.get("evidence_path")
    if evidence_path not in action["evidence_paths"]:
        action["evidence_paths"].append(evidence_path)
    if action.get("raw_excerpt"):
        return
    excerpt, redaction = redact_string(
        bounded_text(str(signal.get("text") or ""), FAILURE_ACTION_MAX_EXCERPT_CHARS),
        f"failure_actions.{action.get('id')}.raw_excerpt",
    )
    action["raw_excerpt"] = excerpt
    action["redaction_summary"] = redaction.to_json()


def build_unknown_failure_action(signal: dict[str, Any]) -> dict[str, Any]:
    excerpt, redaction = redact_string(
        bounded_text(str(signal.get("text") or ""), FAILURE_ACTION_MAX_EXCERPT_CHARS),
        "failure_actions.FAIL-UNKNOWN-OPERATIONAL.raw_excerpt",
    )
    return {
        "id": "FAIL-UNKNOWN-OPERATIONAL",
        "catalog_schema": FAILURE_ACTION_CATALOG_SCHEMA,
        "category": "unknown",
        "title": "Unclassified operational failure; stop and surface the redacted excerpt",
        "match_confidence": "low",
        "explanation": (
            "The planner found a failing operational signal that does not match "
            "the current catalog. It must not infer a root cause from this excerpt."
        ),
        "evidence_paths": [signal.get("evidence_path")],
        "matched_source": signal.get("source"),
        "safe_commands": [
            plan_command("Inspect git state", "git status --short --branch"),
            plan_command("Inspect active Beads ownership", "br list --status=in_progress --json"),
            plan_command("Inspect the target bead", "br show <issue-id> --json"),
        ],
        "escalation": (
            "Preserve the redacted raw excerpt and create or update a Beads issue "
            "for catalog coverage if this failure recurs."
        ),
        "raw_excerpt": excerpt,
        "redaction_summary": redaction.to_json(),
    }


def build_failure_action_recommendations(
    input_pack: dict[str, Any],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    actions_by_id: dict[str, dict[str, Any]] = {}
    unknown_signal: dict[str, Any] | None = None
    for signal in gather_failure_signals(input_pack):
        matched_rule: dict[str, Any] | None = None
        for rule in FAILURE_ACTION_RULES:
            if rule_matches_failure_signal(rule, str(signal.get("text") or "")):
                matched_rule = rule
                break
        if matched_rule is None:
            if unknown_signal is None:
                unknown_signal = signal
            continue
        rule_id = str(matched_rule["id"])
        if rule_id in actions_by_id:
            merge_failure_action_evidence(actions_by_id[rule_id], signal)
        else:
            actions_by_id[rule_id] = build_failure_action(matched_rule, signal)
    actions = list(actions_by_id.values())
    if unknown_signal is not None:
        actions.append(build_unknown_failure_action(unknown_signal))
    return bounded(actions, max_items)


def autopilot_plan_action(
    *,
    action: str,
    title: str,
    severity: str,
    confidence: str,
    preconditions: list[str],
    evidence_paths: list[str],
    commands: list[dict[str, str]],
    rationale: str,
    omitted_commands: list[dict[str, str]] | None = None,
    forbidden_actions: list[str] | None = None,
) -> dict[str, Any]:
    if action not in AUTOPILOT_PLAN_ALLOWED_ACTIONS:
        raise RunpackError(f"unknown autopilot action: {action}")
    if severity not in AUTOPILOT_PLAN_SEVERITIES:
        raise RunpackError(f"unknown autopilot severity: {severity}")
    if confidence not in AUTOPILOT_PLAN_CONFIDENCE:
        raise RunpackError(f"unknown autopilot confidence: {confidence}")
    return {
        "id": action,
        "rank": 0,
        "action": action,
        "title": title,
        "severity": severity,
        "confidence": confidence,
        "preconditions": preconditions,
        "evidence_paths": evidence_paths,
        "commands": commands,
        "omitted_commands": omitted_commands or [],
        "forbidden_actions": forbidden_actions or [],
        "rationale": rationale,
    }


def required_input_blockers(input_pack: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    status_by_id = {
        str(item.get("id")): str(item.get("status"))
        for item in input_pack.get("source_statuses", [])
        if isinstance(item, dict)
    }
    for source_id in AUTOPILOT_REQUIRED_SOURCE_IDS:
        if status_by_id.get(source_id) != "ok":
            blockers.append(f"required source `{source_id}` status is `{status_by_id.get(source_id)}`")
    for item in input_pack.get("source_classification", []):
        if not isinstance(item, dict) or item.get("required") is not True:
            continue
        classification = item.get("classification")
        if classification in {"blocker", "stale"}:
            blockers.append(f"required source `{item.get('id')}` is {classification}")
    if not input_pack.get("command_provenance"):
        blockers.append("command provenance is empty")
    return sorted(set(blockers))


def action_sort_key(action: dict[str, Any]) -> tuple[int, int, str]:
    severity_order = {severity: index for index, severity in enumerate(AUTOPILOT_PLAN_SEVERITIES)}
    action_order = {name: index for index, name in enumerate(AUTOPILOT_PLAN_ALLOWED_ACTIONS)}
    return (
        severity_order.get(str(action.get("severity")), 99),
        action_order.get(str(action.get("action")), 99),
        str(action.get("id") or ""),
    )


def assign_action_ranks(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(actions, key=action_sort_key)
    for index, action in enumerate(ranked, start=1):
        action["rank"] = index
        action["id"] = f"AUTO-{index:03d}-{action['action']}"
    return ranked


def assert_autopilot_plan_commands_are_safe(plan: dict[str, Any]) -> None:
    command_groups = [
        (action, "commands") for action in plan.get("actions", []) if isinstance(action, dict)
    ]
    command_groups.extend(
        (action, "safe_commands")
        for action in plan.get("failure_actions", [])
        if isinstance(action, dict)
    )
    for action, command_key in command_groups:
        if not isinstance(action, dict):
            continue
        for command in action.get(command_key, []):
            if not isinstance(command, dict):
                raise AssertionError("autopilot plan command entries must be objects")
            text = str(command.get("command") or "").lower()
            for fragment in AUTOPILOT_PLAN_DANGEROUS_COMMAND_FRAGMENTS:
                assert fragment not in text, (
                    f"autopilot plan emitted a dangerous runnable command fragment: {fragment}"
                )


def derive_autopilot_plan_status(
    input_pack: dict[str, Any],
    actions: list[dict[str, Any]],
) -> str:
    action_names = {action.get("action") for action in actions}
    if "stop_and_surface_blocker" in action_names:
        return "blocked"
    degraded_actions = {
        "wait_for_rch",
        "use_beads_soft_lock",
        "reopen_stale_bead_candidate",
        "capture_handoff",
        "adjust_swarm_budget",
    }
    if input_pack.get("status") != "ready" or action_names.intersection(degraded_actions):
        return "degraded"
    return "ready"


def build_autopilot_plan(
    input_pack: dict[str, Any],
    *,
    max_items: int,
) -> dict[str, Any]:
    if input_pack.get("schema") != AUTOPILOT_INPUT_PACK_SCHEMA:
        raise RunpackError(
            "autopilot plan requires "
            f"{AUTOPILOT_INPUT_PACK_SCHEMA}, got {input_pack.get('schema')}"
        )

    actions: list[dict[str, Any]] = []
    blockers = required_input_blockers(input_pack)
    work_partitions = build_work_partition_recommendations(input_pack, max_items=max_items)
    failure_actions = build_failure_action_recommendations(input_pack, max_items=max_items)
    if blockers:
        actions.append(
            autopilot_plan_action(
                action="stop_and_surface_blocker",
                title="Surface missing, stale, or unverifiable planner evidence",
                severity="critical",
                confidence="high",
                preconditions=[
                    "Do not infer healthy swarm state from partial evidence.",
                    "Regenerate or inspect each missing required source before claiming new work.",
                ],
                evidence_paths=[
                    "source_statuses",
                    "source_classification",
                    "degraded_reasons",
                    "command_provenance",
                ],
                commands=[
                    plan_command("Inspect planner input status", "python3 -m json.tool <autopilot-input-pack.json>"),
                    plan_command("Refresh current evidence bundle", "python3 scripts/build_swarm_operator_runpack.py --capture-current --capture-dir <capture-dir> --out-autopilot-input-pack-json <capture-dir>/autopilot-input-pack.json"),
                ],
                omitted_commands=[
                    omitted_command("claim work", "ready Beads evidence is not trustworthy until required sources are present"),
                    omitted_command("start heavy validation", "RCH admission evidence may be missing or stale"),
                ],
                forbidden_actions=[
                    "destructive cleanup",
                    "automatic ownership mutation",
                ],
                rationale="; ".join(blockers[:max_items]),
            )
        )

    cargo = normalized_section(input_pack, "cargo_admission")
    queue_forecast = (
        cargo.get("queue_forecast") if isinstance(cargo.get("queue_forecast"), dict) else {}
    )
    decision = cargo.get("decision")
    queue_action = queue_forecast.get("recommended_action")
    if decision in {"backoff", "degraded", "deny"} or queue_action == "backoff":
        actions.append(
            autopilot_plan_action(
                action="wait_for_rch",
                title="Back off heavyweight Cargo work until RCH recovers",
                severity="high",
                confidence="high",
                preconditions=[
                    "Do not run heavyweight cargo locally during swarm contention.",
                    "Recheck admission before starting check, clippy, test, or release builds.",
                ],
                evidence_paths=[
                    "normalized_inputs.cargo_admission.decision",
                    "normalized_inputs.cargo_admission.queue_forecast.recommended_action",
                    "normalized_inputs.cargo_admission.queue_forecast.slot_pressure",
                ],
                commands=[
                    plan_command("Inspect RCH queue", "rch queue"),
                    plan_command("Inspect RCH workers", "rch status"),
                    plan_command("Refresh cargo admission", "./scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets"),
                ],
                omitted_commands=[
                    omitted_command("cargo check --all-targets", "heavy validation waits for a fresh RCH admit decision"),
                ],
                forbidden_actions=["local heavyweight cargo fallback"],
                rationale=f"cargo decision={decision}; queue recommended_action={queue_action}",
            )
        )
    if queue_action == "split":
        actions.append(
            autopilot_plan_action(
                action="split_by_surface",
                title="Split validation or implementation by narrow surfaces",
                severity="medium",
                confidence="medium",
                preconditions=[
                    "Keep each validation slice small enough for the current RCH queue.",
                    "Use file ownership evidence before assigning overlapping work.",
                ],
                evidence_paths=[
                    "normalized_inputs.cargo_admission.queue_forecast.recommended_action",
                    "normalized_inputs.operator_runpack.operator_next_actions",
                    "work_partitions",
                ],
                commands=[
                    plan_command("Inspect ready work", "br ready --json"),
                    plan_command("Inspect current ownership", "br list --status=in_progress --json"),
                    plan_command("Capture current dirty paths", "git status --short --branch"),
                ],
                omitted_commands=[
                    omitted_command("broad all-targets validation", "queue forecast recommends split work first"),
                ],
                forbidden_actions=["broad duplicate file ownership"],
                rationale="RCH queue forecast recommends split validation.",
            )
        )

    budget_drift = normalized_section(input_pack, "budget_drift")
    if budget_drift.get("status") in {"degraded", "deny_new_work"}:
        status = str(budget_drift.get("status"))
        adjustments = (
            budget_drift.get("recommended_adjustments")
            if isinstance(budget_drift.get("recommended_adjustments"), dict)
            else {}
        )
        signals = budget_drift.get("signals") if isinstance(budget_drift.get("signals"), list) else []
        actions.append(
            autopilot_plan_action(
                action="adjust_swarm_budget",
                title="Reduce swarm fanout based on live budget drift",
                severity="critical" if status == "deny_new_work" else "high",
                confidence="high" if signals else "medium",
                preconditions=[
                    "Do not raise fanout from stale startup preflight evidence.",
                    "Hold reduced fanout until the drift watcher sees two stable samples.",
                ],
                evidence_paths=[
                    "normalized_inputs.budget_drift.status",
                    "normalized_inputs.budget_drift.signals",
                    "normalized_inputs.budget_drift.recommended_adjustments",
                ],
                commands=[
                    plan_command("Inspect budget drift", "python3 -m json.tool <autopilot-input-pack.json>"),
                    plan_command("Refresh swarm resource preflight", "pi doctor --only swarm --format json"),
                    plan_command("Refresh cargo admission", "./scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets"),
                    plan_command("Inspect active ownership", "br list --status=in_progress --json"),
                ],
                omitted_commands=[
                    omitted_command("increase swarm fanout", "live budget drift is not stable"),
                    omitted_command("start broad cargo validation", "admission must match the adjusted fanout first"),
                ],
                forbidden_actions=["local heavyweight cargo fallback"],
                rationale=(
                    f"budget_drift status={status}; "
                    f"admit_new_agents={adjustments.get('admit_new_agents')}; "
                    f"rch_verification_fanout={adjustments.get('rch_verification_fanout')}"
                ),
            )
        )

    agent_mail = normalized_section(input_pack, "agent_mail")
    if agent_mail.get("status") != "ok":
        actions.append(
            autopilot_plan_action(
                action="use_beads_soft_lock",
                title="Use Beads assignment as the coordination lock",
                severity="high",
                confidence="high",
                preconditions=[
                    "Agent Mail read/reservation status is degraded or unavailable.",
                    "Announce/reserve through Agent Mail only after health recovers.",
                ],
                evidence_paths=[
                    "normalized_inputs.agent_mail.status",
                    "normalized_inputs.agent_mail.read_status",
                    "normalized_inputs.agent_mail.reservation_status",
                    "normalized_inputs.beads.active_count",
                ],
                commands=[
                    plan_command("Inspect active ownership", "br list --status=in_progress --json"),
                    plan_command("Inspect candidate bead before editing", "br show <issue-id> --json"),
                    plan_command("Claim through Beads", "br update <issue-id> --status in_progress --assignee \"${AGENT_NAME:-agent}\""),
                ],
                omitted_commands=[
                    omitted_command("Agent Mail reservation", "coordination transport is degraded; retry after health is green"),
                ],
                forbidden_actions=["automatic file reservation"],
                rationale=f"Agent Mail status={agent_mail.get('status')}; fallback={agent_mail.get('fallback_action')}",
            )
        )

    beads = normalized_section(input_pack, "beads")
    stale = beads.get("stale") if isinstance(beads.get("stale"), list) else []
    stale_candidate = first_candidate(stale)
    if stale_candidate is not None:
        issue_id = str(stale_candidate.get("id") or "<issue-id>")
        actions.append(
            autopilot_plan_action(
                action="reopen_stale_bead_candidate",
                title=f"Review stale in-progress bead {issue_id}",
                severity="medium",
                confidence="medium",
                preconditions=[
                    "Confirm no recent human or agent work exists for the stale assignee.",
                    "Do not reopen work that has fresh commits, comments, or reservations.",
                ],
                evidence_paths=[
                    "normalized_inputs.beads.stale",
                    "normalized_inputs.beads.stale_after_hours",
                ],
                commands=[
                    plan_command("Inspect stale bead", f"br show {issue_id} --json"),
                    plan_command("Inspect active ownership", "br list --status=in_progress --json"),
                    plan_command("Reopen only after confirming abandonment", f"br update {issue_id} --status open"),
                ],
                omitted_commands=[
                    omitted_command("force-release unrelated reservations", "reservation ownership must be verified in Agent Mail first"),
                ],
                forbidden_actions=["destructive cleanup of another agent worktree"],
                rationale=(
                    f"{issue_id} is {stale_candidate.get('status')} "
                    f"and age_hours={stale_candidate.get('age_hours')}"
                ),
            )
        )

    git_state = normalized_section(input_pack, "git_state")
    if git_state.get("dirty") is True:
        actions.append(
            autopilot_plan_action(
                action="capture_handoff",
                title="Capture handoff context before changing dirty worktree state",
                severity="medium",
                confidence="high",
                preconditions=[
                    "Treat dirty files as concurrent work unless they directly overlap the active bead.",
                    "Stage only the files changed for the current bead.",
                ],
                evidence_paths=[
                    "normalized_inputs.git_state.dirty",
                    "normalized_inputs.git_state.sample",
                    "normalized_inputs.git_state.upstream",
                ],
                commands=[
                    plan_command("Inspect dirty files", "git status --short --branch"),
                    plan_command("Capture handoff bundle", "python3 scripts/build_swarm_operator_runpack.py --capture-current --capture-dir <capture-dir> --out-json <capture-dir>/operator-runpack.json --out-autopilot-input-pack-json <capture-dir>/autopilot-input-pack.json --out-autopilot-plan-json <capture-dir>/autopilot-plan.json"),
                ],
                omitted_commands=[
                    omitted_command("workspace cleanup", "dirty files may belong to another active agent"),
                ],
                forbidden_actions=["destructive git cleanup"],
                rationale=f"git_state reports {git_state.get('change_count')} dirty paths.",
            )
        )

    beads_ready = normalized_section(input_pack, "beads_ready")
    ready_candidate = first_candidate(beads_ready.get("candidates"))
    if ready_candidate is not None and not blockers:
        issue_id = str(ready_candidate.get("id") or "<issue-id>")
        actions.append(
            autopilot_plan_action(
                action="claim_ready_bead",
                title=f"Claim ready bead {issue_id}",
                severity="medium",
                confidence="high",
                preconditions=[
                    "The ready queue source is fresh and produced the candidate.",
                    "No active in-progress bead already owns the same work.",
                    "Review the diagnostic work partition before requesting Agent Mail reservations.",
                    "Inspect the bead before editing files.",
                ],
                evidence_paths=[
                    "normalized_inputs.beads_ready.candidates",
                    "normalized_inputs.beads_ready.ready_count",
                    "work_partitions",
                    "command_provenance",
                ],
                commands=[
                    plan_command("Inspect candidate bead", f"br show {issue_id} --json"),
                    plan_command("Check active ownership", "br list --status=in_progress --json"),
                    plan_command("Claim candidate", f"br update {issue_id} --status in_progress --assignee \"${{AGENT_NAME:-agent}}\""),
                ],
                omitted_commands=[
                    omitted_command("automatic Agent Mail reservation", "planner is dry-run and does not mutate reservation state"),
                ],
                forbidden_actions=["automatic commit", "automatic file reservation"],
                rationale=(
                    f"ready queue candidate priority={ready_candidate.get('priority')} "
                    f"title={ready_candidate.get('title')}"
                ),
            )
        )

    ready_count = int_value(beads_ready.get("ready_count"))
    active_count = int_value(beads.get("active_count"))
    open_count = int_value(beads.get("open_candidate_count"))
    if ready_count == 0 and active_count == 0 and open_count == 0 and not blockers:
        actions.append(
            autopilot_plan_action(
                action="run_docs_only_work",
                title="Switch to source or docs-only work while the queue is empty",
                severity="low",
                confidence="medium",
                preconditions=[
                    "No ready, open, or in-progress Beads are present in the captured evidence.",
                    "Keep validation lightweight unless RCH admission is fresh and green.",
                ],
                evidence_paths=[
                    "normalized_inputs.beads_ready.ready_count",
                    "normalized_inputs.beads.active_count",
                    "normalized_inputs.beads.open_candidate_count",
                ],
                commands=[
                    plan_command("Check ready queue again", "br ready --json"),
                    plan_command("Run docs/script self-test", "python3 scripts/build_swarm_operator_runpack.py --self-test"),
                    plan_command("Check formatting", "cargo fmt --check"),
                    plan_command("Check diff whitespace", "git diff --check"),
                ],
                omitted_commands=[
                    omitted_command("claim placeholder epic", "no ready implementation bead is present"),
                ],
                forbidden_actions=["invent broad work without a bead"],
                rationale="Captured Beads evidence contains no ready, open, or in-progress work.",
            )
        )

    if not actions:
        actions.append(
            autopilot_plan_action(
                action="capture_handoff",
                title="Capture a current handoff bundle before choosing next work",
                severity="info",
                confidence="medium",
                preconditions=["Use the bundle as advisory context, not as a source of truth."],
                evidence_paths=["capture", "command_provenance"],
                commands=[
                    plan_command("Capture handoff bundle", "python3 scripts/build_swarm_operator_runpack.py --capture-current --capture-dir <capture-dir> --out-json <capture-dir>/operator-runpack.json --out-autopilot-input-pack-json <capture-dir>/autopilot-input-pack.json --out-autopilot-plan-json <capture-dir>/autopilot-plan.json"),
                ],
                omitted_commands=[
                    omitted_command("automatic claim", "no single higher-confidence action was derived"),
                ],
                forbidden_actions=["automatic commit", "automatic file reservation"],
                rationale="Planner found no blocker, ready candidate, stale bead, dirty state, or RCH pressure action.",
            )
        )

    ranked_actions = assign_action_ranks(actions)
    plan = {
        "schema": AUTOPILOT_PLAN_SCHEMA,
        "generated_at": input_pack.get("generated_at"),
        "status": "unknown",
        "purpose": "dry_run_swarm_autopilot_plan_not_source_of_truth",
        "input_pack_schema": input_pack.get("schema"),
        "input_pack_status": input_pack.get("status"),
        "work_partitions": bounded(work_partitions, max_items),
        "budget_drift": normalized_section(input_pack, "budget_drift"),
        "failure_actions": bounded(failure_actions, max_items),
        "actions": bounded(ranked_actions, max_items),
        "omitted_actions": [
            omitted_command("destructive cleanup", "planner never recommends destructive git or filesystem cleanup"),
            omitted_command("automatic commit", "operator must stage, validate, and commit explicitly"),
            omitted_command("automatic file reservation", "Agent Mail or Beads remains the ownership source of truth"),
        ],
        "forbidden_actions": list(AUTOPILOT_FORBIDDEN_ACTIONS),
        "redaction_summary": input_pack.get("redaction_summary"),
        "planner_guards": {
            "dry_run_only": True,
            "source_of_truth": "upstream_source_artifacts",
            "commands_require_operator_execution": True,
            "dangerous_runnable_commands_blocked": True,
            "work_partitions_are_diagnostic_only": True,
        },
        "degraded_reasons": input_pack.get("degraded_reasons", []),
    }
    plan["status"] = derive_autopilot_plan_status(input_pack, ranked_actions)
    assert_autopilot_plan_commands_are_safe(plan)
    return plan


def int_value(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return 0


def source_status_for(runpack: dict[str, Any], source_id: str) -> str | None:
    for source in runpack.get("source_statuses", []):
        if isinstance(source, dict) and source.get("id") == source_id:
            status = source.get("status")
            return str(status) if status is not None else None
    return None


def required_evidence_gaps(
    runpack: dict[str, Any],
    *,
    required_source_ids: tuple[str, ...],
    evidence_paths: tuple[str, ...],
) -> list[str]:
    missing = [
        f"source_statuses[{source_id}].status"
        for source_id in required_source_ids
        if source_status_for(runpack, source_id) != "ok"
    ]
    for path in evidence_paths:
        try:
            value = get_dotted(runpack, path)
        except KeyError:
            missing.append(path)
            continue
        if value is None:
            missing.append(path)
    return missing


def scorecard_dimension(
    *,
    runpack: dict[str, Any],
    dimension_id: str,
    title: str,
    required_source_ids: tuple[str, ...],
    evidence_paths: tuple[str, ...],
    blockers: list[str],
    warnings: list[str],
    detail: str,
) -> dict[str, Any]:
    missing_evidence = required_evidence_gaps(
        runpack,
        required_source_ids=required_source_ids,
        evidence_paths=evidence_paths,
    )
    all_blockers = list(blockers)
    if missing_evidence:
        all_blockers.insert(0, "missing required evidence")
    if all_blockers:
        score = 0
        status = "red"
    elif warnings:
        score = 1
        status = "yellow"
    else:
        score = SCORECARD_MAX_PER_DIMENSION
        status = "green"
    return {
        "id": dimension_id,
        "title": title,
        "status": status,
        "score": score,
        "max_score": SCORECARD_MAX_PER_DIMENSION,
        "required_source_ids": list(required_source_ids),
        "evidence_paths": list(evidence_paths),
        "missing_evidence": missing_evidence,
        "green_requires": {
            "all_required_sources_ok": all(
                source_status_for(runpack, source_id) == "ok"
                for source_id in required_source_ids
            ),
            "all_required_evidence_present": not missing_evidence,
            "no_blockers": not all_blockers,
        },
        "blockers": all_blockers,
        "warnings": warnings,
        "detail": detail,
    }


def build_swarm_scale_safety_scorecard(runpack: dict[str, Any]) -> dict[str, Any]:
    doctor = runpack["doctor_swarm"]
    agent_mail = runpack["agent_mail"]
    rch = runpack["rch_admission"]
    evidence = runpack["evidence_readiness"]
    git_state = runpack["git_state"]
    beads = runpack["beads"]
    activity = runpack["activity_digest"]
    smoke = runpack["smoke_harness"]

    severity_counts = (
        doctor.get("severity_counts")
        if isinstance(doctor.get("severity_counts"), dict)
        else {}
    )
    coordination_blockers: list[str] = []
    coordination_warnings: list[str] = []
    if doctor.get("overall") == "fail" or int_value(severity_counts.get("fail")):
        coordination_blockers.append("doctor swarm findings include failures")
    if doctor.get("overall") == "warn" or int_value(severity_counts.get("warn")):
        coordination_warnings.append("doctor swarm findings include warnings")
    if not agent_mail.get("build_slots"):
        coordination_blockers.append("Agent Mail build-slot evidence is absent")

    queue_forecast = (
        rch.get("queue_forecast")
        if isinstance(rch.get("queue_forecast"), dict)
        else {}
    )
    rch_decision = rch.get("decision")
    queue_action = queue_forecast.get("recommended_action")
    cargo_blockers: list[str] = []
    cargo_warnings: list[str] = []
    if rch_decision in {"backoff", "deny"}:
        cargo_blockers.append(f"cargo/RCH admission decision is {rch_decision}")
    elif rch_decision == "degraded":
        cargo_warnings.append("cargo/RCH admission fell back to degraded mode")
    elif rch_decision not in {"allow", "admit"}:
        cargo_warnings.append(f"cargo/RCH admission decision is {rch_decision}")
    if queue_action == "backoff":
        cargo_blockers.append("RCH queue forecast recommends backoff")
    elif queue_action == "split":
        cargo_warnings.append("RCH queue forecast recommends split validation")
    if queue_forecast.get("slot_pressure") == "saturated":
        cargo_blockers.append("RCH queue forecast reports saturated slots")

    stale_claims = (
        evidence.get("stale_claims")
        if isinstance(evidence.get("stale_claims"), dict)
        else {}
    )
    stale_count = int_value(stale_claims.get("stale_count"))
    perf_blockers: list[str] = []
    perf_warnings: list[str] = []
    if evidence.get("overall_status") != "ready":
        perf_blockers.append("claim-readiness evidence is not ready")
    if evidence.get("blocking_artifacts"):
        perf_blockers.append("claim-readiness evidence has blocking artifacts")
    if stale_count:
        perf_warnings.append(f"claim-readiness evidence has {stale_count} stale claims")

    scenario_statuses = (
        smoke.get("scenario_statuses")
        if isinstance(smoke.get("scenario_statuses"), dict)
        else {}
    )
    dirty_scenario = scenario_statuses.get("dirty_worktree_preserved")
    dirty_blockers: list[str] = []
    dirty_warnings: list[str] = []
    if dirty_scenario != "pass":
        dirty_blockers.append("smoke harness did not prove dirty-worktree preservation")
    if git_state.get("dirty"):
        dirty_warnings.append("current captured git state is dirty")

    stalled_blockers: list[str] = []
    stalled_warnings: list[str] = []
    stale_beads = beads.get("stale") if isinstance(beads.get("stale"), list) else []
    if stale_beads:
        stalled_blockers.append(f"{len(stale_beads)} active Beads entries are stale")
    if int_value(beads.get("active_count")) == 0:
        stalled_warnings.append("Beads capture has no active work entries")

    resource_blockers: list[str] = []
    resource_warnings: list[str] = []
    if activity.get("saturated") is True:
        resource_blockers.append("activity digest reports swarm saturation")
    if queue_action == "backoff":
        resource_blockers.append("RCH queue forecast is in backoff")
    elif queue_action == "split":
        resource_warnings.append("RCH queue forecast needs split validation")
    if queue_forecast.get("slot_pressure") == "saturated":
        resource_blockers.append("RCH slot pressure is saturated")

    failed_scenarios = (
        smoke.get("failed_scenarios")
        if isinstance(smoke.get("failed_scenarios"), list)
        else []
    )
    non_pass_scenarios = [
        name
        for name, status in scenario_statuses.items()
        if status != "pass"
    ]
    coverage_blockers: list[str] = []
    coverage_warnings: list[str] = []
    if smoke.get("harness_status") != "pass":
        coverage_blockers.append("smoke harness status is not pass")
    if failed_scenarios:
        coverage_blockers.append("smoke harness reports failed scenarios")
    if non_pass_scenarios:
        coverage_blockers.append("smoke harness has non-pass scenario statuses")
    if not smoke.get("artifact_manifest"):
        coverage_blockers.append("smoke harness artifact manifest is empty")

    bottleneck = runpack["bottleneck_attribution"]
    bottleneck_blockers: list[str] = []
    bottleneck_warnings: list[str] = []
    blocked_inputs = bottleneck.get("blocked_inputs")
    historical_snapshots = bottleneck.get("historical_snapshots")
    missing_optional = bottleneck.get("missing_optional_diagnostics")
    if isinstance(blocked_inputs, list) and blocked_inputs:
        bottleneck_blockers.append("bottleneck attribution has blocked inputs")
    if isinstance(historical_snapshots, list) and historical_snapshots:
        bottleneck_warnings.append("bottleneck attribution includes historical snapshots")
    if isinstance(missing_optional, list) and missing_optional:
        bottleneck_warnings.append("bottleneck attribution has missing optional diagnostics")
    if bottleneck.get("status") != "ready":
        bottleneck_warnings.append("bottleneck attribution dashboard is degraded")

    dimensions = [
        scorecard_dimension(
            runpack=runpack,
            dimension_id="coordination_health",
            title="Coordination health",
            required_source_ids=("doctor_swarm", "smoke_harness"),
            evidence_paths=(
                "doctor_swarm.overall",
                "doctor_swarm.agent_mail_build_slots",
                "agent_mail.build_slots",
                "agent_mail.smoke_reservation_count",
            ),
            blockers=coordination_blockers,
            warnings=coordination_warnings,
            detail="Agent Mail and doctor evidence show whether coordination lanes are observable and unstuck.",
        ),
        scorecard_dimension(
            runpack=runpack,
            dimension_id="cargo_rch_posture",
            title="Cargo/RCH posture",
            required_source_ids=("cargo_admission",),
            evidence_paths=(
                "rch_admission.decision",
                "rch_admission.queue_forecast.status",
                "rch_admission.queue_forecast.recommended_action",
            ),
            blockers=cargo_blockers,
            warnings=cargo_warnings,
            detail="Cargo admission and RCH queue evidence decide whether heavy validation can start safely.",
        ),
        scorecard_dimension(
            runpack=runpack,
            dimension_id="perf_evidence_freshness",
            title="Performance evidence freshness",
            required_source_ids=("claim_readiness",),
            evidence_paths=(
                "evidence_readiness.overall_status",
                "evidence_readiness.blocking_artifacts",
                "evidence_readiness.stale_claims",
            ),
            blockers=perf_blockers,
            warnings=perf_warnings,
            detail="Claim-readiness artifacts must be ready, non-blocking, and fresh enough for release handoff.",
        ),
        scorecard_dimension(
            runpack=runpack,
            dimension_id="dirty_worktree_tolerance",
            title="Dirty-worktree tolerance",
            required_source_ids=("git_status", "smoke_harness"),
            evidence_paths=(
                "git_state.dirty",
                "git_state.sample",
                "smoke_harness.scenario_statuses",
            ),
            blockers=dirty_blockers,
            warnings=dirty_warnings,
            detail="Git status and the smoke harness prove unrelated dirty files are accounted for and preserved.",
        ),
        scorecard_dimension(
            runpack=runpack,
            dimension_id="stalled_bead_hygiene",
            title="Stalled-Bead hygiene",
            required_source_ids=("beads",),
            evidence_paths=(
                "beads.stale",
                "beads.stale_after_hours",
                "beads.active_count",
            ),
            blockers=stalled_blockers,
            warnings=stalled_warnings,
            detail="Beads evidence must not show stale active ownership before launching more swarm work.",
        ),
        scorecard_dimension(
            runpack=runpack,
            dimension_id="resource_governor_readiness",
            title="Resource-governor readiness",
            required_source_ids=("activity_digest", "cargo_admission"),
            evidence_paths=(
                "activity_digest.saturated",
                "activity_digest.evidence_pointers",
                "rch_admission.queue_forecast.recommended_action",
                "rch_admission.queue_forecast.slot_pressure",
            ),
            blockers=resource_blockers,
            warnings=resource_warnings,
            detail="Activity saturation and RCH queue posture decide whether the swarm should admit more work.",
        ),
        scorecard_dimension(
            runpack=runpack,
            dimension_id="bottleneck_attribution_coverage",
            title="Bottleneck attribution coverage",
            required_source_ids=(
                "doctor_swarm",
                "smoke_harness",
                "activity_digest",
                "cargo_admission",
            ),
            evidence_paths=(
                "bottleneck_attribution.status",
                "bottleneck_attribution.surface_coverage",
                "bottleneck_attribution.input_classification",
                "bottleneck_attribution.operator_notes",
            ),
            blockers=bottleneck_blockers,
            warnings=bottleneck_warnings,
            detail="Diagnostic bottleneck attribution must classify source freshness without promoting evidence to release claims.",
        ),
        scorecard_dimension(
            runpack=runpack,
            dimension_id="test_coverage",
            title="Test coverage",
            required_source_ids=("smoke_harness",),
            evidence_paths=(
                "smoke_harness.harness_status",
                "smoke_harness.scenario_statuses",
                "smoke_harness.artifact_manifest",
            ),
            blockers=coverage_blockers,
            warnings=coverage_warnings,
            detail="The smoke harness must pass and retain artifact-manifest evidence for the operator workflow.",
        ),
    ]
    total_score = sum(int_value(dimension["score"]) for dimension in dimensions)
    max_score = SCORECARD_MAX_PER_DIMENSION * len(dimensions)
    status_counts = Counter(str(dimension["status"]) for dimension in dimensions)
    return {
        "schema": SAFETY_SCORECARD_SCHEMA,
        "overall_status": "ready" if status_counts.get("green") == len(dimensions) else "degraded",
        "total_score": total_score,
        "max_score": max_score,
        "status_counts": dict(sorted(status_counts.items())),
        "green_requires_all_required_evidence": True,
        "dimensions": dimensions,
    }


def derive_status(runpack: dict[str, Any]) -> str:
    source_statuses = [item["status"] for item in runpack["source_statuses"]]
    if any(status == "ok" for status in source_statuses):
        status = "ready"
    else:
        status = "degraded"
    if any(status in {"missing", "not_provided"} for status in source_statuses):
        status = "degraded"
    doctor = runpack["doctor_swarm"]
    if doctor.get("overall") == "fail" or doctor.get("severity_counts", {}).get("fail", 0):
        status = "degraded"
    if runpack["evidence_readiness"].get("overall_status") not in {None, "ready"}:
        status = "degraded"
    if runpack["rch_admission"].get("decision") in {"backoff", "degraded", "deny"}:
        status = "degraded"
    if runpack["smoke_harness"].get("harness_status") == "fail":
        status = "degraded"
    if runpack["bottleneck_attribution"].get("status") != "ready":
        status = "degraded"
    scorecard = runpack.get("swarm_scale_safety_scorecard")
    if isinstance(scorecard, dict) and scorecard.get("overall_status") != "ready":
        status = "degraded"
    return status


def build_runpack(args: argparse.Namespace) -> dict[str, Any]:
    generated_at = parse_utc(args.generated_at) if args.generated_at else parse_utc(utc_now_iso())
    sources = source_payloads(args)
    by_id = {source.id: source for source in sources}
    redaction = RedactionStats()
    for source in sources:
        redaction.redacted_count += source.redacted_count
        redaction.fields.update(source.redacted_fields)
    doctor_summary = summarize_doctor(by_id["doctor_swarm"], args.max_items)
    smoke_summary = summarize_smoke_harness(by_id["smoke_harness"], args.max_items)
    capture_summary = capture_summary_from_args(args)
    validation_summary, validation_redaction = summarize_validation_outputs(
        getattr(args, "validation_outputs", []) or [],
        args.max_items,
    )
    redaction.merge(validation_redaction)
    runpack = {
        "schema": RUNPACK_SCHEMA,
        "generated_at": generated_at.isoformat(),
        "status": "unknown",
        "purpose": "operator_handoff_not_release_performance_claim",
        "capture": capture_summary,
        "source_statuses": [source.to_status() for source in sources],
        "doctor_swarm": doctor_summary,
        "beads": summarize_beads(
            by_id["beads"],
            generated_at=generated_at,
            stale_after_hours=args.stale_after_hours,
            max_items=args.max_items,
        ),
        "agent_mail": {
            "doctor_findings": doctor_summary.get("agent_mail_findings", []),
            "build_slots": doctor_summary.get("agent_mail_build_slots"),
            "smoke_reservation_count": smoke_summary.get("reservation_count"),
        },
        "agent_mail_read_state": summarize_agent_mail_read_state(
            capture_summary,
            doctor_summary,
            args.max_items,
        ),
        "rch_admission": summarize_cargo_admission(by_id["cargo_admission"]),
        "evidence_readiness": summarize_claim_readiness(by_id["claim_readiness"], args.max_items),
        "git_state": summarize_git_status(by_id["git_status"], args.max_items),
        "activity_digest": summarize_activity_digest(by_id["activity_digest"], args.max_items),
        "smoke_harness": smoke_summary,
        "validation_outputs": validation_summary,
        "resume_commands": build_resume_commands(args),
        "redaction_summary": redaction.to_json(),
    }
    if "tail_latency" in by_id:
        runpack["tail_latency"] = summarize_tail_latency(
            by_id["tail_latency"],
            args.max_items,
        )
    runpack["bottleneck_attribution"] = build_bottleneck_attribution(
        runpack,
        by_id,
        generated_at=generated_at,
        stale_after_hours=args.stale_after_hours,
        max_items=args.max_items,
    )
    runpack["swarm_scale_safety_scorecard"] = build_swarm_scale_safety_scorecard(runpack)
    runpack["status"] = derive_status(runpack)
    runpack["operator_next_actions"] = operator_next_actions(runpack)
    return runpack


def operator_next_actions(runpack: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    missing = [
        item["id"]
        for item in runpack["source_statuses"]
        if item.get("status") in {"missing", "not_provided"}
    ]
    if missing:
        actions.append("Capture missing source artifacts: " + ", ".join(sorted(missing)))
    if runpack["doctor_swarm"].get("severity_counts", {}).get("fail", 0):
        actions.append("Resolve failing `pi doctor --only swarm --format json` findings")
    if runpack["beads"].get("stale"):
        actions.append("Review stale in-progress Beads before assigning more work")
    if runpack["rch_admission"].get("decision") in {"backoff", "degraded", "deny"}:
        actions.append("Treat cargo/RCH admission as blocked or degraded before heavy builds")
    forecast_action = runpack["rch_admission"].get("queue_forecast", {}).get("recommended_action")
    if forecast_action == "split":
        actions.append("Split heavy cargo validation based on RCH queue forecast pressure")
    elif forecast_action == "backoff":
        actions.append("Back off heavy cargo validation until the RCH queue forecast recovers")
    if runpack["activity_digest"].get("saturated"):
        actions.append("Use activity-digest saturation evidence to narrow or redirect the swarm")
    if runpack["git_state"].get("dirty"):
        actions.append("Account for dirty files before using the runpack as handoff evidence")
    if runpack.get("agent_mail_read_state", {}).get("status") in {"degraded", "not_available"}:
        actions.append("Treat Agent Mail read state as unavailable and fall back to Beads ownership evidence")
    if runpack.get("validation_outputs", {}).get("status") == "failed":
        actions.append("Inspect failed validation output before resuming or closing the active bead")
    bottleneck = runpack.get("bottleneck_attribution")
    if isinstance(bottleneck, dict) and bottleneck.get("status") != "ready":
        actions.append(
            "Review degraded bottleneck attribution dashboard before using it as current diagnostic evidence"
        )
    scorecard = runpack.get("swarm_scale_safety_scorecard")
    if isinstance(scorecard, dict) and scorecard.get("overall_status") != "ready":
        actions.append("Review degraded swarm-scale safety scorecard dimensions before release runpack signoff")
    if not actions:
        actions.append("Runpack sources are ready; proceed with the next unblocked Beads task")
    return actions


def render_markdown(runpack: dict[str, Any]) -> str:
    lines = [
        "# Swarm Operator Runpack",
        "",
        f"- Schema: `{runpack['schema']}`",
        f"- Status: `{runpack['status']}`",
        f"- Generated: `{runpack['generated_at']}`",
        f"- Purpose: `{runpack['purpose']}`",
        "",
        "## Sources",
    ]
    for source in runpack["source_statuses"]:
        lines.append(
            f"- `{source['id']}`: `{source['status']}`"
            + (f" ({source['path']})" if source.get("path") else "")
        )
    lines.extend(["", "## Next Actions"])
    lines.extend(f"- {action}" for action in runpack["operator_next_actions"])
    lines.extend(["", "## Summaries"])
    lines.append(f"- Doctor swarm overall: `{runpack['doctor_swarm'].get('overall')}`")
    lines.append(f"- Beads active/stale: `{runpack['beads'].get('active_count')}` active, `{len(runpack['beads'].get('stale') or [])}` stale")
    lines.append(f"- RCH admission: `{runpack['rch_admission'].get('decision')}`")
    lines.append(f"- RCH queue forecast: `{runpack['rch_admission'].get('queue_forecast', {}).get('recommended_action')}`")
    lines.append(f"- Evidence readiness: `{runpack['evidence_readiness'].get('overall_status')}`")
    lines.append(f"- Git dirty: `{runpack['git_state'].get('dirty')}`")
    lines.append(f"- Agent Mail read state: `{runpack['agent_mail_read_state'].get('status')}`")
    lines.append(f"- Validation outputs: `{runpack['validation_outputs'].get('status')}`")
    lines.append(f"- Activity saturated: `{runpack['activity_digest'].get('saturated')}`")
    lines.append(f"- Bottleneck attribution: `{runpack['bottleneck_attribution'].get('status')}`")
    if isinstance(runpack.get("autopilot_handoff"), dict):
        handoff = runpack["autopilot_handoff"]
        lines.append(
            f"- Autopilot handoff: `{handoff.get('status')}` "
            f"(`{handoff.get('plan', {}).get('selected_action')}`)"
        )
    git_state = runpack["git_state"]
    lines.extend(["", "## Git Context"])
    lines.append(f"- Branch: `{git_state.get('branch')}`")
    lines.append(f"- HEAD: `{git_state.get('head')}`")
    upstream = git_state.get("upstream") if isinstance(git_state.get("upstream"), dict) else {}
    lines.append(
        f"- Upstream: `{upstream.get('name')}` "
        f"(ahead `{upstream.get('ahead')}`, behind `{upstream.get('behind')}`)"
    )
    for commit in git_state.get("recent_commits") or []:
        lines.append(f"- Recent: `{commit}`")
    capture = runpack["capture"]
    lines.extend(["", "## Capture"])
    lines.append(f"- Mode: `{capture.get('mode')}`")
    lines.append(f"- Status: `{capture.get('status')}`")
    if capture.get("capture_dir"):
        lines.append(f"- Directory: `{capture.get('capture_dir')}`")
    for command in capture.get("commands", []):
        lines.append(
            f"- `{command.get('id')}`: `{command.get('status')}`"
            + (f" ({command.get('issue')})" if command.get("issue") else "")
        )
    validation = runpack["validation_outputs"]
    if validation.get("outputs"):
        lines.extend(["", "## Validation Outputs"])
        for output in validation.get("outputs", []):
            lines.append(
                f"- `{output.get('path')}`: `{output.get('inferred_status')}` "
                f"({output.get('size_bytes')} bytes)"
            )
    if isinstance(runpack.get("tail_latency"), dict):
        tail_latency = runpack["tail_latency"]
        lines.append(
            f"- Tail latency telemetry: `{tail_latency.get('telemetry_enabled')}` "
            f"({len(tail_latency.get('metrics') or [])} metrics)"
        )
    scorecard = runpack["swarm_scale_safety_scorecard"]
    lines.extend(["", "## Safety Scorecard"])
    lines.append(
        f"- Overall: `{scorecard.get('overall_status')}` "
        f"({scorecard.get('total_score')}/{scorecard.get('max_score')})"
    )
    for dimension in scorecard.get("dimensions", []):
        lines.append(
            f"- `{dimension['id']}`: `{dimension['status']}` "
            f"({dimension['score']}/{dimension['max_score']})"
        )
    bottleneck = runpack["bottleneck_attribution"]
    lines.extend(["", "## Bottleneck Attribution"])
    for surface_id, surface in bottleneck.get("surface_coverage", {}).items():
        lines.append(f"- `{surface_id}`: `{surface.get('status')}`")
    for item in bottleneck.get("bottlenecks", []):
        lines.append(
            f"- `{item.get('surface')}` from `{item.get('source')}`: "
            f"{item.get('signal')}"
        )
    handoff = runpack.get("autopilot_handoff")
    if isinstance(handoff, dict):
        lines.extend(["", "## Autopilot Handoff"])
        lines.append(f"- Schema: `{handoff.get('schema')}`")
        lines.append(f"- Status: `{handoff.get('status')}`")
        lines.append(f"- Purpose: `{handoff.get('purpose')}`")
        input_pack = handoff.get("input_pack") if isinstance(handoff.get("input_pack"), dict) else {}
        lines.append(
            f"- Input pack: `{input_pack.get('schema')}` / `{input_pack.get('status')}`"
            + (f" ({input_pack.get('artifact_path')})" if input_pack.get("artifact_path") else "")
        )
        plan = handoff.get("plan") if isinstance(handoff.get("plan"), dict) else {}
        lines.append(
            f"- Plan: `{plan.get('schema')}` / `{plan.get('status')}`"
            + (f" ({plan.get('artifact_path')})" if plan.get("artifact_path") else "")
        )
        lines.append(f"- Selected action: `{plan.get('selected_action')}`")
        for action in plan.get("actions") or []:
            lines.append(
                f"- Action `{action.get('rank')}`: `{action.get('action')}` "
                f"({action.get('severity')}, {action.get('confidence')})"
            )
        provenance = (
            handoff.get("source_provenance")
            if isinstance(handoff.get("source_provenance"), dict)
            else {}
        )
        for source in provenance.get("source_statuses") or []:
            lines.append(f"- Source `{source.get('id')}`: `{source.get('status')}`")
    lines.extend(["", "## Resume Commands"])
    for item in runpack.get("resume_commands", []):
        lines.append(f"- {item.get('purpose')}: `{item.get('command')}`")
    lines.append("")
    return "\n".join(lines)


def write_outputs(args: argparse.Namespace, runpack: dict[str, Any]) -> None:
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        if args.out_json.exists():
            raise RunpackError(f"refusing to overwrite existing JSON runpack: {args.out_json}")
        args.out_json.write_text(json_dumps(runpack, pretty=True), encoding="utf-8")
    if args.out_md:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        if args.out_md.exists():
            raise RunpackError(f"refusing to overwrite existing Markdown runpack: {args.out_md}")
        args.out_md.write_text(render_markdown(runpack), encoding="utf-8")


def write_autopilot_input_pack_output(
    args: argparse.Namespace,
    input_pack: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_autopilot_input_pack_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(
            f"refusing to overwrite existing autopilot input pack: {output_path}"
        )
    output_path.write_text(json_dumps(input_pack, pretty=True), encoding="utf-8")


def write_autopilot_plan_output(
    args: argparse.Namespace,
    plan: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_autopilot_plan_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(f"refusing to overwrite existing autopilot plan: {output_path}")
    output_path.write_text(json_dumps(plan, pretty=True), encoding="utf-8")


def artifact_path(value: Path | None) -> str | None:
    return str(value) if value is not None else None


def build_autopilot_handoff_summary(
    args: argparse.Namespace,
    input_pack: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    actions = [
        {
            "rank": action.get("rank"),
            "action": action.get("action"),
            "severity": action.get("severity"),
            "confidence": action.get("confidence"),
            "evidence_paths": action.get("evidence_paths") or [],
        }
        for action in plan.get("actions", [])
        if isinstance(action, dict)
    ]
    source_statuses = [
        {
            "id": source.get("id"),
            "status": source.get("status"),
            "schema": source.get("schema"),
            "path": source.get("path"),
            "sha256": source.get("sha256"),
        }
        for source in input_pack.get("source_statuses", [])
        if isinstance(source, dict)
    ]
    return {
        "schema": AUTOPILOT_HANDOFF_SCHEMA,
        "status": plan.get("status"),
        "purpose": "operator_handoff_dry_run_autopilot_summary_not_source_of_truth",
        "input_pack": {
            "schema": input_pack.get("schema"),
            "status": input_pack.get("status"),
            "purpose": input_pack.get("purpose"),
            "artifact_path": artifact_path(getattr(args, "out_autopilot_input_pack_json", None)),
            "degraded_reasons": input_pack.get("degraded_reasons") or [],
        },
        "plan": {
            "schema": plan.get("schema"),
            "status": plan.get("status"),
            "purpose": plan.get("purpose"),
            "artifact_path": artifact_path(getattr(args, "out_autopilot_plan_json", None)),
            "selected_action": actions[0]["action"] if actions else None,
            "actions": actions,
            "budget_drift_status": (plan.get("budget_drift") or {}).get("status")
            if isinstance(plan.get("budget_drift"), dict)
            else None,
            "degraded_reasons": plan.get("degraded_reasons") or [],
        },
        "source_provenance": {
            "source_statuses": source_statuses,
            "command_count": len(input_pack.get("command_provenance") or []),
            "capture_status": (input_pack.get("capture") or {}).get("status")
            if isinstance(input_pack.get("capture"), dict)
            else None,
        },
    }


def assert_autopilot_handoff_summary(runpack: dict[str, Any]) -> None:
    handoff = runpack.get("autopilot_handoff")
    if handoff is None:
        return
    assert isinstance(handoff, dict)
    assert handoff.get("schema") == AUTOPILOT_HANDOFF_SCHEMA
    assert handoff.get("purpose") == (
        "operator_handoff_dry_run_autopilot_summary_not_source_of_truth"
    )
    input_pack = handoff.get("input_pack")
    assert isinstance(input_pack, dict)
    assert input_pack.get("schema") == AUTOPILOT_INPUT_PACK_SCHEMA
    assert input_pack.get("purpose") == "dry_run_swarm_autopilot_input_not_source_of_truth"
    plan = handoff.get("plan")
    assert isinstance(plan, dict)
    assert plan.get("schema") == AUTOPILOT_PLAN_SCHEMA
    assert plan.get("purpose") == "dry_run_swarm_autopilot_plan_not_source_of_truth"
    actions = plan.get("actions")
    assert isinstance(actions, list) and actions
    assert plan.get("selected_action") == actions[0].get("action")
    provenance = handoff.get("source_provenance")
    assert isinstance(provenance, dict)
    source_statuses = provenance.get("source_statuses")
    assert isinstance(source_statuses, list) and source_statuses
    for source in source_statuses:
        assert isinstance(source, dict)
        assert source.get("id")
        assert source.get("status")
    assert isinstance(provenance.get("command_count"), int)


def write_json(path: Path, payload: Any) -> Path:
    path.write_text(json_dumps(payload, pretty=True), encoding="utf-8")
    return path


def get_dotted(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def assert_runpack_contract(runpack: dict[str, Any]) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    contract_path = repo_root / RUNPACK_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing runpack contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(f"runpack contract is malformed JSON: {contract_path}: {exc}") from exc
    assert contract.get("schema") == RUNPACK_CONTRACT_SCHEMA
    assert contract.get("runpack_schema") == RUNPACK_SCHEMA
    assert runpack.get("schema") == contract["runpack_schema"]
    assert runpack.get("purpose") == contract.get("purpose")
    assert runpack.get("status") in set(contract.get("allowed_statuses", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in runpack, f"missing top-level runpack key: {key}"
    source_ids = {
        item.get("id")
        for item in runpack.get("source_statuses", [])
        if isinstance(item, dict)
    }
    required_source_ids = set(contract.get("required_source_ids", []))
    optional_source_ids = set(contract.get("optional_source_ids", []))
    assert source_ids.issuperset(required_source_ids)
    unknown_source_ids = source_ids - required_source_ids - optional_source_ids
    assert not unknown_source_ids, f"unexpected source ids: {sorted(unknown_source_ids)}"
    for path in contract.get("required_summary_paths", []):
        get_dotted(runpack, path)
    for path in contract.get("optional_summary_paths", []):
        top_level_key = path.split(".", maxsplit=1)[0]
        if top_level_key in runpack:
            get_dotted(runpack, path)
    assert_autopilot_handoff_summary(runpack)
    scorecard = runpack.get("swarm_scale_safety_scorecard")
    assert isinstance(scorecard, dict)
    assert scorecard.get("schema") == contract.get("scorecard_schema")
    assert scorecard.get("overall_status") in set(contract.get("allowed_scorecard_statuses", []))
    dimensions = scorecard.get("dimensions")
    assert isinstance(dimensions, list) and dimensions
    dimension_ids = {
        dimension.get("id")
        for dimension in dimensions
        if isinstance(dimension, dict)
    }
    assert dimension_ids == set(contract.get("required_scorecard_dimensions", []))
    for dimension in dimensions:
        assert isinstance(dimension, dict)
        assert dimension.get("status") in set(contract.get("allowed_dimension_statuses", []))
        assert dimension.get("max_score") == SCORECARD_MAX_PER_DIMENSION
        assert isinstance(dimension.get("required_source_ids"), list) and dimension.get(
            "required_source_ids"
        )
        assert isinstance(dimension.get("evidence_paths"), list) and dimension.get("evidence_paths")
        assert isinstance(dimension.get("missing_evidence"), list)
        green_requires = dimension.get("green_requires")
        assert isinstance(green_requires, dict)
        all_required_evidence_present = not dimension["missing_evidence"]
        assert (
            green_requires.get("all_required_evidence_present")
            is all_required_evidence_present
        )
        if dimension.get("status") == "green":
            assert not dimension["missing_evidence"]
            assert green_requires.get("all_required_sources_ok") is True
            assert green_requires.get("all_required_evidence_present") is True
            assert green_requires.get("no_blockers") is True
    for field in contract.get("required_source_status_fields", []):
        for source in runpack.get("source_statuses", []):
            if isinstance(source, dict) and source.get("status") == "ok":
                assert source.get(field) not in {None, ""}, (
                    f"source {source.get('id')} missing required status field {field}"
                )
    redaction = runpack.get("redaction_summary")
    assert isinstance(redaction, dict)
    assert redaction.get("redacted_count", 0) >= contract.get("minimum_redacted_count", 0)
    fields = set(redaction.get("fields", []))
    assert fields.issuperset(set(contract.get("required_redacted_fields", [])))
    actions = runpack.get("operator_next_actions")
    assert isinstance(actions, list) and actions
    action_text = "\n".join(str(action) for action in actions)
    for fragment in contract.get("required_next_action_fragments", []):
        assert fragment in action_text, f"missing next-action fragment: {fragment}"


def assert_autopilot_input_pack_contract(input_pack: dict[str, Any]) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    contract_path = repo_root / AUTOPILOT_INPUT_PACK_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing autopilot input-pack contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"autopilot input-pack contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == AUTOPILOT_INPUT_PACK_CONTRACT_SCHEMA
    assert contract.get("input_pack_schema") == AUTOPILOT_INPUT_PACK_SCHEMA
    assert input_pack.get("schema") == contract["input_pack_schema"]
    assert input_pack.get("purpose") == contract.get("purpose")
    assert input_pack.get("status") in set(contract.get("allowed_statuses", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in input_pack, f"missing top-level input-pack key: {key}"
    source_ids = {
        item.get("id")
        for item in input_pack.get("source_statuses", [])
        if isinstance(item, dict)
    }
    required_source_ids = set(contract.get("required_source_ids", []))
    optional_source_ids = set(contract.get("optional_source_ids", []))
    assert source_ids.issuperset(required_source_ids)
    unknown_source_ids = source_ids - required_source_ids - optional_source_ids
    assert not unknown_source_ids, f"unexpected input-pack source ids: {sorted(unknown_source_ids)}"
    for path in contract.get("required_summary_paths", []):
        get_dotted(input_pack, path)
    budget_drift = get_dotted(input_pack, "normalized_inputs.budget_drift")
    assert isinstance(budget_drift, dict)
    assert budget_drift.get("schema") == BUDGET_DRIFT_SCHEMA
    assert budget_drift.get("status") in {"stable", "degraded", "deny_new_work"}
    for field in contract.get("required_source_status_fields", []):
        for source in input_pack.get("source_statuses", []):
            if isinstance(source, dict) and source.get("status") == "ok":
                assert source.get(field) not in {None, ""}, (
                    f"input-pack source {source.get('id')} missing required status field {field}"
                )
    redaction = input_pack.get("redaction_summary")
    assert isinstance(redaction, dict)
    assert redaction.get("redacted_count", 0) >= contract.get("minimum_redacted_count", 0)
    fields = set(redaction.get("fields", []))
    assert fields.issuperset(set(contract.get("required_redacted_fields", [])))
    classifications = input_pack.get("source_classification")
    assert isinstance(classifications, list) and classifications
    for item in classifications:
        assert isinstance(item, dict)
        assert item.get("classification") in set(contract.get("allowed_classifications", []))
    reasons = input_pack.get("degraded_reasons")
    assert isinstance(reasons, list)
    if input_pack.get("status") == "degraded":
        assert reasons, "degraded input pack must explain why it is degraded"


def assert_autopilot_plan_contract(plan: dict[str, Any]) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    contract_path = repo_root / AUTOPILOT_PLAN_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing autopilot plan contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"autopilot plan contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == AUTOPILOT_PLAN_CONTRACT_SCHEMA
    assert contract.get("plan_schema") == AUTOPILOT_PLAN_SCHEMA
    assert plan.get("schema") == contract["plan_schema"]
    assert plan.get("purpose") == contract.get("purpose")
    assert plan.get("status") in set(contract.get("allowed_statuses", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in plan, f"missing top-level autopilot plan key: {key}"
    action_fields = set(contract.get("required_action_fields", []))
    partition_fields = set(contract.get("required_partition_fields", []))
    failure_action_fields = set(contract.get("required_failure_action_fields", []))
    budget_drift_fields = set(contract.get("required_budget_drift_fields", []))
    allowed_actions = set(contract.get("allowed_actions", []))
    allowed_severities = set(contract.get("allowed_severities", []))
    allowed_confidence = set(contract.get("allowed_confidence", []))
    allowed_failure_categories = set(contract.get("allowed_failure_categories", []))
    allowed_budget_drift_statuses = set(contract.get("allowed_budget_drift_statuses", []))
    budget_drift = plan.get("budget_drift")
    assert isinstance(budget_drift, dict)
    missing_budget_drift = budget_drift_fields - set(budget_drift)
    assert not missing_budget_drift, (
        f"autopilot plan budget drift missing fields: {sorted(missing_budget_drift)}"
    )
    assert budget_drift.get("schema") == BUDGET_DRIFT_SCHEMA
    assert budget_drift.get("status") in allowed_budget_drift_statuses
    partitions = plan.get("work_partitions")
    assert isinstance(partitions, list)
    for partition in partitions:
        assert isinstance(partition, dict)
        missing = partition_fields - set(partition)
        assert not missing, f"autopilot plan work partition missing fields: {sorted(missing)}"
        assert partition.get("confidence") in allowed_confidence
        assert isinstance(partition.get("surface_ids"), list) and partition.get("surface_ids")
        suggested = partition.get("suggested_reservation")
        assert isinstance(suggested, list) and suggested
        assert all(isinstance(item, str) and item for item in suggested)
        assert isinstance(partition.get("avoid"), list)
        assert isinstance(partition.get("degraded_caveats"), list)
        evidence_paths = partition.get("evidence_paths")
        assert isinstance(evidence_paths, list) and evidence_paths
    failure_actions = plan.get("failure_actions")
    assert isinstance(failure_actions, list)
    for failure_action in failure_actions:
        assert isinstance(failure_action, dict)
        missing = failure_action_fields - set(failure_action)
        assert not missing, f"autopilot plan failure action missing fields: {sorted(missing)}"
        assert failure_action.get("category") in allowed_failure_categories
        assert failure_action.get("match_confidence") in allowed_confidence
        assert isinstance(failure_action.get("evidence_paths"), list) and failure_action.get(
            "evidence_paths"
        )
        safe_commands = failure_action.get("safe_commands")
        assert isinstance(safe_commands, list) and safe_commands
        for command in safe_commands:
            assert isinstance(command, dict)
            assert command.get("purpose")
            assert command.get("command")
        redaction_summary = failure_action.get("redaction_summary")
        assert isinstance(redaction_summary, dict)
    actions = plan.get("actions")
    assert isinstance(actions, list) and actions
    ranks = [action.get("rank") for action in actions if isinstance(action, dict)]
    assert ranks == sorted(ranks), "autopilot plan actions must be rank ordered"
    for action in actions:
        assert isinstance(action, dict)
        missing = action_fields - set(action)
        assert not missing, f"autopilot plan action missing fields: {sorted(missing)}"
        assert action.get("action") in allowed_actions
        assert action.get("severity") in allowed_severities
        assert action.get("confidence") in allowed_confidence
        assert isinstance(action.get("preconditions"), list) and action.get("preconditions")
        assert isinstance(action.get("evidence_paths"), list) and action.get("evidence_paths")
        assert isinstance(action.get("commands"), list)
        for command in action.get("commands", []):
            assert isinstance(command, dict)
            assert command.get("purpose")
            assert command.get("command")
    assert set(plan.get("forbidden_actions", [])).issuperset(
        set(contract.get("required_forbidden_actions", []))
    )
    guards = plan.get("planner_guards")
    assert isinstance(guards, dict)
    assert guards.get("dry_run_only") is True
    assert guards.get("commands_require_operator_execution") is True
    assert guards.get("dangerous_runnable_commands_blocked") is True
    assert_autopilot_plan_commands_are_safe(plan)


def canonicalize_for_golden(value: Any, workspace: Path) -> Any:
    workspace_text = str(workspace)
    if isinstance(value, dict):
        return {
            key: "[SHA256]"
            if key == "sha256" and isinstance(item, str)
            else canonicalize_for_golden(item, workspace)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [canonicalize_for_golden(item, workspace) for item in value]
    if isinstance(value, str):
        return value.replace(workspace_text, "[WORKSPACE]")
    return value


def assert_runpack_golden(runpack: dict[str, Any], workspace: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    golden_path = repo_root / GOLDEN_REPORT_DIRECTORY / COMPLETE_RUNPACK_GOLDEN
    actual_projection = canonicalize_for_golden(runpack, workspace)
    actual = json_dumps(actual_projection, pretty=True)
    if os.environ.get(UPDATE_GOLDEN_ENV) == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return
    try:
        expected = golden_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AssertionError(
            f"missing runpack golden {golden_path}; rerun with {UPDATE_GOLDEN_ENV}=1"
        ) from exc
    if actual != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                actual.splitlines(),
                fromfile=str(golden_path),
                tofile="actual swarm operator runpack projection",
                lineterm="",
            )
        )
        raise AssertionError(
            "swarm operator runpack projection changed; update the golden only "
            f"after reviewing the diff with `{UPDATE_GOLDEN_ENV}=1 "
            "python3 scripts/build_swarm_operator_runpack.py --self-test`\n"
            + diff
        )


def assert_autopilot_plan_golden(plan: dict[str, Any], workspace: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    golden_path = repo_root / GOLDEN_REPORT_DIRECTORY / AUTOPILOT_PLAN_GOLDEN
    actual_projection = canonicalize_for_golden(plan, workspace)
    actual = json_dumps(actual_projection, pretty=True)
    if os.environ.get(UPDATE_GOLDEN_ENV) == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return
    try:
        expected = golden_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AssertionError(
            f"missing autopilot plan golden {golden_path}; rerun with {UPDATE_GOLDEN_ENV}=1"
        ) from exc
    if actual != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                actual.splitlines(),
                fromfile=str(golden_path),
                tofile="actual autopilot plan projection",
                lineterm="",
            )
        )
        raise AssertionError(
            "autopilot plan projection changed; update the golden only after "
            f"reviewing the diff with `{UPDATE_GOLDEN_ENV}=1 "
            "python3 scripts/build_swarm_operator_runpack.py --self-test`\n"
            + diff
        )


def assert_no_dangerous_runnable_commands(commands: list[dict[str, Any]]) -> None:
    for command in commands:
        text = str(command.get("command") or "").lower()
        for fragment in AUTOPILOT_PLAN_DANGEROUS_COMMAND_FRAGMENTS:
            assert fragment not in text, (
                f"autopilot E2E attempted a dangerous command fragment: {fragment}"
            )


def append_autopilot_e2e_event(events_path: Path, event: dict[str, Any]) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json_dumps(event) + "\n")


def autopilot_e2e_event(
    *,
    scenario_id: str,
    phase: str,
    event: str,
    status: str,
    generated_at: str,
    correlation_id: str,
    command_provenance: list[dict[str, Any]] | None = None,
    selected_action: str | None = None,
    evidence_paths: list[str] | None = None,
    redaction_summary: dict[str, Any] | None = None,
    budget_state: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": AUTOPILOT_E2E_EVENT_SCHEMA,
        "generated_at": generated_at,
        "correlation_id": correlation_id,
        "scenario_id": scenario_id,
        "phase": phase,
        "event": event,
        "status": status,
        "command_provenance": command_provenance or [],
        "selected_action": selected_action,
        "evidence_paths": evidence_paths or [],
        "redaction_summary": redaction_summary or {"redacted_count": 0, "fields": []},
        "budget_state": budget_state or {},
        "details": details or {},
    }


def autopilot_e2e_preflight(generated_at: str) -> dict[str, Any]:
    return {
        "schema": HOST_PREFLIGHT_SCHEMA,
        "generated_at": generated_at,
        "status": "pass",
        "cpu": {
            "logical_cores": 16,
            "effective_cores": 8,
            "cgroup_quota": {"quota_cores": 8.0, "unlimited": False},
            "cpuset": {"cpu_count": 8},
        },
        "numa": {"node_count": 2, "nodes": [0, 1]},
        "memory": {
            "cgroup_limit_bytes": 34359738368,
            "effective_limit_bytes": 34359738368,
            "unlimited": False,
        },
        "tmpfs_headroom": {
            "expected_root": "/data/tmp/pi_agent_rust_cargo",
            "paths": [
                {
                    "env_name": "CARGO_TARGET_DIR",
                    "path": "/data/tmp/pi_agent_rust_cargo/e2e/target",
                    "ready": True,
                    "available_kb": 52428800,
                },
                {
                    "env_name": "TMPDIR",
                    "path": "/data/tmp/pi_agent_rust_cargo/e2e/tmp",
                    "ready": True,
                    "available_kb": 52428800,
                },
            ],
        },
        "recommended_budgets": {
            "agent_concurrency": 4,
            "tool_concurrency": 8,
            "extension_hostcall_lanes": 16,
            "rch_verification_fanout": 2,
            "max_queue_depth": 2,
            "max_rss_bytes": 17179869184,
            "plan_confidence": "high",
        },
        "critical_failures": [],
        "source_errors": [],
    }


def autopilot_e2e_doctor_payload(
    generated_at: str,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    return {
        "overall": "pass",
        "summary": {"pass": 2, "info": 0, "warn": 0, "fail": 0},
        "findings": [
            {
                "category": "swarm",
                "severity": "pass",
                "title": "Agent Mail probe fixture",
                "detail": "token=super-secret-value must be redacted",
                "remediation": None,
                "data": {"schema": "pi.doctor.agent_mail_build_slots.v1", "active": 0},
                "fixability": "not_fixable",
            },
            {
                "category": "swarm",
                "severity": "pass",
                "title": "Swarm resource preflight ready",
                "detail": "resource profile accepted",
                "remediation": None,
                "data": preflight,
                "fixability": "not_fixable",
            },
        ],
    }


def autopilot_e2e_cargo_payload(
    *,
    decision: str = "admit",
    queue_action: str = "proceed",
    slot_pressure: str = "available",
    queue_depth: int = 0,
) -> dict[str, Any]:
    return {
        "schema": "pi.cargo_headroom.admission.v1",
        "decision": decision,
        "reason": "autopilot_e2e_fixture",
        "requested_runner": "rch",
        "resolved_runner": "rch" if decision == "admit" else "none",
        "command_class": "heavy",
        "allow_local_fallback": False,
        "cargo_target_dir": "/data/tmp/pi_agent_rust_cargo/e2e/target",
        "tmpdir": "/data/tmp/pi_agent_rust_cargo/e2e/tmp",
        "rch_queue_forecast": {
            "schema": "pi.cargo_headroom.rch_queue_forecast.v1",
            "status": "ok",
            "recommended_action": queue_action,
            "reason": f"e2e_{queue_action}",
            "slot_pressure": slot_pressure,
            "queue_depth": queue_depth,
            "active_builds": queue_depth,
            "queued_builds": max(0, queue_depth - 2),
            "slots_available": 0 if slot_pressure == "saturated" else 8,
            "slots_total": 8,
            "workers_healthy": 8,
            "workers_total": 8,
            "estimated_wait_seconds": 240 if queue_action == "backoff" else 0,
        },
    }


def autopilot_e2e_agent_mail_status(
    generated_at: str,
    *,
    status: str = "ok",
    health_level: str = "green",
    issue: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": "pi.agent_mail.robot_status.v1",
        "generated_at": generated_at,
        "status": status,
        "health_level": health_level,
        "registration_token": "super-secret-registration-token",
    }
    if issue is not None:
        payload["issue"] = issue
    return payload


def autopilot_e2e_agent_mail_reservations(generated_at: str) -> dict[str, Any]:
    return {
        "schema": "pi.agent_mail.robot_reservations.v1",
        "generated_at": generated_at,
        "status": "ok",
        "reservations": [],
    }


def autopilot_e2e_clean_git_payload(generated_at: str) -> dict[str, Any]:
    return {
        "schema": GIT_CONTEXT_SCHEMA,
        "generated_at": generated_at,
        "branch": "main",
        "head": "autopilote2e",
        "upstream": {"name": "origin/main", "ahead": 0, "behind": 0, "status": "ok"},
        "porcelain_lines": [],
        "recent_commits": ["autopilote2e fixture"],
        "recent_remote_commits": ["autopilote2e origin/main fixture"],
    }


def autopilot_e2e_source_paths(
    scenario_dir: Path,
    *,
    generated_at: str,
    preflight: dict[str, Any],
    cargo_payload: dict[str, Any],
    beads_payload: Any,
    beads_ready_payload: Any,
    agent_mail_status_payload: dict[str, Any],
    agent_mail_reservations_payload: dict[str, Any],
    git_payload: dict[str, Any] | None,
    git_status_file: Path | None = None,
) -> dict[str, Path]:
    paths = {
        "doctor_json": write_json(
            scenario_dir / "doctor.json",
            autopilot_e2e_doctor_payload(generated_at, preflight),
        ),
        "host_preflight_json": write_json(scenario_dir / "host-preflight.json", preflight),
        "cargo_admission_json": write_json(scenario_dir / "cargo-admission.json", cargo_payload),
        "beads_json": write_json(scenario_dir / "beads.json", beads_payload),
        "beads_ready_json": write_json(scenario_dir / "beads-ready.json", beads_ready_payload),
        "agent_mail_status_json": write_json(
            scenario_dir / "agent-mail-status.json",
            agent_mail_status_payload,
        ),
        "agent_mail_reservations_json": write_json(
            scenario_dir / "agent-mail-reservations.json",
            agent_mail_reservations_payload,
        ),
    }
    if git_status_file is not None:
        paths["git_status_file"] = git_status_file
    else:
        paths["git_status_file"] = write_json(
            scenario_dir / "git-status.json",
            git_payload or autopilot_e2e_clean_git_payload(generated_at),
        )
    return paths


def autopilot_e2e_args(
    *,
    paths: dict[str, Path],
    commands: list[dict[str, Any]],
    scenario_dir: Path,
    generated_at: str,
    max_items: int,
    stale_after_hours: int,
) -> argparse.Namespace:
    return argparse.Namespace(
        doctor_json=paths["doctor_json"],
        claim_readiness_json=None,
        smoke_summary_json=None,
        activity_digest_json=None,
        cargo_admission_json=paths["cargo_admission_json"],
        beads_json=paths["beads_json"],
        beads_ready_json=paths["beads_ready_json"],
        agent_mail_status_json=paths["agent_mail_status_json"],
        agent_mail_reservations_json=paths["agent_mail_reservations_json"],
        git_status_file=paths["git_status_file"],
        tail_latency_json=None,
        flight_recorder_report_json=None,
        host_preflight_json=paths["host_preflight_json"],
        hostcall_swarm_profile_json=None,
        session_recovery_swarm_profile_json=None,
        rpc_swarm_e2e_json=None,
        rch_artifact_sync_json=None,
        validation_outputs=[],
        operator_runpack_json=None,
        out_json=None,
        out_md=None,
        out_autopilot_input_pack_json=None,
        out_autopilot_plan_json=None,
        print_autopilot_input_pack=False,
        print_autopilot_plan=False,
        generated_at=generated_at,
        stale_after_hours=stale_after_hours,
        max_items=max_items,
        capture_manifest={
            "schema": RUNPACK_CAPTURE_SCHEMA,
            "mode": "autopilot_e2e",
            "status": "degraded"
            if any(command.get("status") in {"failed", "timeout"} for command in commands)
            else "ok",
            "generated_at": generated_at,
            "capture_dir": str(scenario_dir),
            "project_root": str(scenario_dir),
            "generated_source_paths": {
                key: str(path) for key, path in paths.items()
            },
            "commands": commands,
        },
        capture_dir=scenario_dir,
    )


def capture_autopilot_e2e_command(
    commands: list[dict[str, Any]],
    command_id: str,
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> str:
    result, stdout = capture_command(
        command_id,
        command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    commands.append(result)
    if result.get("status") != "ok":
        raise RunpackError(
            f"autopilot E2E command {command_id} failed: {result.get('issue')}"
        )
    return stdout


def build_real_beads_sources(
    scenario_dir: Path,
    *,
    scenario_id: str,
    issues: list[dict[str, Any]],
    timeout_seconds: int,
) -> tuple[Any, Any, list[dict[str, Any]]]:
    if shutil.which("br") is None:
        raise RunpackError("autopilot E2E requires br on PATH")
    commands: list[dict[str, Any]] = []
    workspace = scenario_dir / "beads-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:beads_init",
        ["br", "init", "--prefix", "e2e", "--json"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    for index, issue in enumerate(issues, start=1):
        create_command = [
            "br",
            "create",
            "--title",
            str(issue["title"]),
            "--type",
            str(issue.get("type", "task")),
            "--priority",
            str(issue.get("priority", 2)),
            "--description",
            str(issue.get("description", issue["title"])),
            "--json",
        ]
        labels = issue.get("labels")
        if labels:
            create_command.extend(["--labels", ",".join(str(label) for label in labels)])
        capture_autopilot_e2e_command(
            commands,
            f"{scenario_id}:beads_create_{index}",
            create_command,
            cwd=workspace,
            timeout_seconds=timeout_seconds,
        )
    list_stdout = capture_autopilot_e2e_command(
        commands,
        "beads_list",
        ["br", "list", "--json"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    ready_stdout = capture_autopilot_e2e_command(
        commands,
        "beads_ready",
        ["br", "ready", "--json"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    beads_payload = json_payload_from_stdout(list_stdout)
    ready_payload = json_payload_from_stdout(ready_stdout)
    if beads_payload is None or ready_payload is None:
        raise RunpackError("autopilot E2E could not parse br JSON output")
    return beads_payload, ready_payload, commands


def build_real_dirty_git_source(
    scenario_dir: Path,
    *,
    scenario_id: str,
    generated_at: str,
    timeout_seconds: int,
) -> tuple[Path, list[dict[str, Any]]]:
    commands: list[dict[str, Any]] = []
    workspace = scenario_dir / "git-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:git_init",
        ["git", "init", "-b", "main"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:git_config_email",
        ["git", "config", "user.email", "autopilot-e2e@example.invalid"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:git_config_name",
        ["git", "config", "user.name", "Autopilot E2E"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    (workspace / "README.md").write_text("autopilot e2e fixture\n", encoding="utf-8")
    capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:git_add_initial",
        ["git", "add", "README.md"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:git_commit_initial",
        ["git", "commit", "-m", "init autopilot e2e fixture"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    (workspace / "README.md").write_text(
        "autopilot e2e fixture\nunrelated dirty line\n",
        encoding="utf-8",
    )
    git_context, git_commands = capture_git_context(
        workspace,
        scenario_dir,
        timeout_seconds,
    )
    git_context["generated_at"] = generated_at
    (scenario_dir / "git-status.json").write_text(
        json_dumps(git_context, pretty=True),
        encoding="utf-8",
    )
    commands.extend(git_commands)
    assert git_context["porcelain_lines"], "dirty git fixture must report a changed path"
    return scenario_dir / "git-status.json", commands


def autopilot_e2e_result_from_plan(
    *,
    scenario_id: str,
    scenario_dir: Path,
    generated_at: str,
    correlation_id: str,
    input_pack: dict[str, Any],
    plan: dict[str, Any],
    expected_actions: list[str],
    events_path: Path,
) -> dict[str, Any]:
    assert_autopilot_input_pack_contract(input_pack)
    assert_autopilot_plan_contract(plan)
    assert_autopilot_plan_commands_are_safe(plan)
    command_provenance = input_pack.get("command_provenance")
    assert isinstance(command_provenance, list) and command_provenance
    assert_no_dangerous_runnable_commands(command_provenance)
    action_names = [str(action.get("action")) for action in plan.get("actions", [])]
    for action in expected_actions:
        assert action in action_names, f"{scenario_id} missing action {action}: {action_names}"
    selected_action = action_names[0] if action_names else None
    first_action = plan["actions"][0]
    budget_state = plan.get("budget_drift") if isinstance(plan.get("budget_drift"), dict) else {}
    event = autopilot_e2e_event(
        scenario_id=scenario_id,
        phase="assert",
        event="scenario_result",
        status="pass",
        generated_at=generated_at,
        correlation_id=correlation_id,
        command_provenance=command_provenance,
        selected_action=selected_action,
        evidence_paths=list(first_action.get("evidence_paths") or []),
        redaction_summary=plan.get("redaction_summary"),
        budget_state={
            "status": budget_state.get("status"),
            "profile_status": budget_state.get("profile_status"),
            "recommended_adjustments": budget_state.get("recommended_adjustments"),
            "signals": budget_state.get("signals"),
        },
        details={
            "plan_status": plan.get("status"),
            "input_pack_status": input_pack.get("status"),
            "actions": action_names,
        },
    )
    append_autopilot_e2e_event(events_path, event)
    return {
        "scenario_id": scenario_id,
        "status": "pass",
        "selected_action": selected_action,
        "actions": action_names,
        "plan_status": plan.get("status"),
        "input_pack_status": input_pack.get("status"),
        "evidence_paths": event["evidence_paths"],
        "redaction_summary": plan.get("redaction_summary"),
        "budget_state": event["budget_state"],
        "command_count": len(command_provenance),
        "artifact_dir": str(scenario_dir),
    }


def build_autopilot_e2e_summary(
    *,
    output_dir: Path | None,
    events_path: Path | None,
    generated_at: str,
    max_items: int,
    stale_after_hours: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    workspace = (
        output_dir.resolve()
        if output_dir is not None
        else Path(tempfile.mkdtemp(prefix="pi_swarm_autopilot_e2e_")).resolve()
    )
    workspace.mkdir(parents=True, exist_ok=True)
    events_path = events_path or (workspace / "autopilot-e2e-events.jsonl")
    if events_path.exists():
        raise RunpackError(f"refusing to overwrite autopilot E2E events: {events_path}")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("", encoding="utf-8")
    correlation_id = f"autopilot-e2e-{generated_at.replace(':', '').replace('+', 'Z')}"
    preflight = autopilot_e2e_preflight(generated_at)
    clean_git = autopilot_e2e_clean_git_payload(generated_at)
    agent_mail_ok = autopilot_e2e_agent_mail_status(generated_at)
    reservations_ok = autopilot_e2e_agent_mail_reservations(generated_at)
    results: list[dict[str, Any]] = []

    def run_plan_scenario(
        scenario_id: str,
        *,
        beads_payload: Any,
        beads_ready_payload: Any,
        commands: list[dict[str, Any]],
        expected_actions: list[str],
        cargo_payload: dict[str, Any] | None = None,
        agent_mail_payload: dict[str, Any] | None = None,
        git_payload: dict[str, Any] | None = None,
        git_status_file: Path | None = None,
        current_preflight: dict[str, Any] | None = None,
    ) -> None:
        scenario_dir = workspace / scenario_id
        scenario_dir.mkdir(parents=True, exist_ok=True)
        append_autopilot_e2e_event(
            events_path,
            autopilot_e2e_event(
                scenario_id=scenario_id,
                phase="setup",
                event="scenario_start",
                status="running",
                generated_at=generated_at,
                correlation_id=correlation_id,
                details={"expected_actions": expected_actions},
            ),
        )
        paths = autopilot_e2e_source_paths(
            scenario_dir,
            generated_at=generated_at,
            preflight=current_preflight or preflight,
            cargo_payload=cargo_payload or autopilot_e2e_cargo_payload(),
            beads_payload=beads_payload,
            beads_ready_payload=beads_ready_payload,
            agent_mail_status_payload=agent_mail_payload or agent_mail_ok,
            agent_mail_reservations_payload=reservations_ok,
            git_payload=git_payload or clean_git,
            git_status_file=git_status_file,
        )
        args = autopilot_e2e_args(
            paths=paths,
            commands=commands,
            scenario_dir=scenario_dir,
            generated_at=generated_at,
            max_items=max_items,
            stale_after_hours=stale_after_hours,
        )
        input_pack = build_autopilot_input_pack(args)
        plan = build_autopilot_plan(input_pack, max_items=max_items)
        results.append(
            autopilot_e2e_result_from_plan(
                scenario_id=scenario_id,
                scenario_dir=scenario_dir,
                generated_at=generated_at,
                correlation_id=correlation_id,
                input_pack=input_pack,
                plan=plan,
                expected_actions=expected_actions,
                events_path=events_path,
            )
        )

    ready_beads, ready_queue, ready_commands = build_real_beads_sources(
        workspace / "healthy_ready_claim",
        scenario_id="healthy_ready_claim",
        issues=[
            {
                "title": "Add OpenAI provider streaming parity",
                "description": "provider streaming body",
                "priority": 2,
                "labels": ["provider", "openai"],
            }
        ],
        timeout_seconds=timeout_seconds,
    )
    run_plan_scenario(
        "healthy_ready_claim",
        beads_payload=ready_beads,
        beads_ready_payload=ready_queue,
        commands=ready_commands,
        expected_actions=["claim_ready_bead"],
    )

    empty_beads, empty_ready, empty_commands = build_real_beads_sources(
        workspace / "empty_ready_queue",
        scenario_id="empty_ready_queue",
        issues=[],
        timeout_seconds=timeout_seconds,
    )
    run_plan_scenario(
        "empty_ready_queue",
        beads_payload=empty_beads,
        beads_ready_payload=empty_ready,
        commands=empty_commands,
        expected_actions=["run_docs_only_work"],
    )

    degraded_mail_commands = list(ready_commands) + [
        {
            "id": "agent_mail_status",
            "command": "am robot status --format json",
            "cwd": str(workspace),
            "status": "failed",
            "exit_code": 2,
            "issue": "database schema missing required tables",
            "stdout_path": None,
            "stderr_snippet": "database schema missing required tables",
            "redaction_summary": {"redacted_count": 0, "fields": []},
        }
    ]
    run_plan_scenario(
        "degraded_agent_mail_soft_lock",
        beads_payload=ready_beads,
        beads_ready_payload=ready_queue,
        commands=degraded_mail_commands,
        expected_actions=["use_beads_soft_lock"],
        agent_mail_payload=autopilot_e2e_agent_mail_status(
            generated_at,
            status="error",
            health_level="red",
            issue="database schema missing required tables",
        ),
    )

    run_plan_scenario(
        "saturated_rch_queue",
        beads_payload=ready_beads,
        beads_ready_payload=ready_queue,
        commands=list(ready_commands)
        + [
            {
                "id": "rch_queue",
                "command": "rch queue --json",
                "cwd": str(workspace),
                "status": "ok",
                "exit_code": 0,
                "issue": None,
                "stdout_path": None,
                "stderr_snippet": "",
                "redaction_summary": {"redacted_count": 0, "fields": []},
            }
        ],
        expected_actions=["adjust_swarm_budget", "wait_for_rch"],
        cargo_payload=autopilot_e2e_cargo_payload(
            decision="backoff",
            queue_action="backoff",
            slot_pressure="saturated",
            queue_depth=6,
        ),
    )

    stale_beads_payload = {
        "issues": [
            {
                "id": "bd-stale-e2e",
                "title": "Stale autopilot fixture",
                "status": "in_progress",
                "assignee": "OldAgent",
                "priority": 2,
                "updated_at": "2026-05-08T00:00:00+00:00",
            }
        ]
    }
    run_plan_scenario(
        "stale_in_progress_bead",
        beads_payload=stale_beads_payload,
        beads_ready_payload=[],
        commands=[
            {
                "id": "beads_list",
                "command": "br list --status=in_progress --json",
                "cwd": str(workspace),
                "status": "ok",
                "exit_code": 0,
                "issue": None,
                "stdout_path": None,
                "stderr_snippet": "",
                "redaction_summary": {"redacted_count": 0, "fields": []},
            }
        ],
        expected_actions=["reopen_stale_bead_candidate"],
    )

    dirty_git_path, dirty_git_commands = build_real_dirty_git_source(
        workspace / "unrelated_dirty_worktree",
        scenario_id="unrelated_dirty_worktree",
        generated_at=generated_at,
        timeout_seconds=timeout_seconds,
    )
    run_plan_scenario(
        "unrelated_dirty_worktree",
        beads_payload=empty_beads,
        beads_ready_payload=empty_ready,
        commands=dirty_git_commands + empty_commands,
        expected_actions=["capture_handoff"],
        git_status_file=dirty_git_path,
    )

    malformed_dir = workspace / "malformed_source_fail_closed"
    malformed_dir.mkdir(parents=True, exist_ok=True)
    malformed_path = malformed_dir / "doctor-malformed.json"
    malformed_path.write_text("{not valid json", encoding="utf-8")
    malformed_paths = autopilot_e2e_source_paths(
        malformed_dir,
        generated_at=generated_at,
        preflight=preflight,
        cargo_payload=autopilot_e2e_cargo_payload(),
        beads_payload=ready_beads,
        beads_ready_payload=ready_queue,
        agent_mail_status_payload=agent_mail_ok,
        agent_mail_reservations_payload=reservations_ok,
        git_payload=clean_git,
    )
    malformed_paths["doctor_json"] = malformed_path
    try:
        build_autopilot_input_pack(
            autopilot_e2e_args(
                paths=malformed_paths,
                commands=ready_commands,
                scenario_dir=malformed_dir,
                generated_at=generated_at,
                max_items=max_items,
                stale_after_hours=stale_after_hours,
            )
        )
    except RunpackError as exc:
        event = autopilot_e2e_event(
            scenario_id="malformed_source_fail_closed",
            phase="assert",
            event="scenario_result",
            status="pass",
            generated_at=generated_at,
            correlation_id=correlation_id,
            command_provenance=command_provenance(
                {"commands": ready_commands},
                max_items,
            ),
            selected_action="fail_closed",
            evidence_paths=["source_statuses.doctor_swarm", "doctor_json"],
            redaction_summary={"redacted_count": 0, "fields": []},
            budget_state={"status": "not_evaluated"},
            details={"error": str(exc)},
        )
        append_autopilot_e2e_event(events_path, event)
        results.append(
            {
                "scenario_id": "malformed_source_fail_closed",
                "status": "pass",
                "selected_action": "fail_closed",
                "actions": ["fail_closed"],
                "plan_status": "blocked",
                "input_pack_status": "not_built",
                "evidence_paths": event["evidence_paths"],
                "redaction_summary": event["redaction_summary"],
                "budget_state": event["budget_state"],
                "command_count": len(ready_commands),
                "artifact_dir": str(malformed_dir),
            }
        )
    else:
        raise AssertionError("malformed autopilot E2E source should fail closed")

    summary = {
        "schema": AUTOPILOT_E2E_SCHEMA,
        "generated_at": generated_at,
        "correlation_id": correlation_id,
        "status": "pass" if all(item["status"] == "pass" for item in results) else "fail",
        "purpose": "no_mock_swarm_autopilot_e2e_operator_evidence_not_release_claim",
        "scenario_count": len(results),
        "required_scenarios": list(AUTOPILOT_E2E_REQUIRED_SCENARIOS),
        "scenarios": {item["scenario_id"]: item for item in results},
        "events_jsonl": str(events_path),
        "workspace": str(workspace),
        "guards": {
            "uses_real_temp_beads": True,
            "uses_real_temp_git": True,
            "fixture_captures_degraded_rch_and_agent_mail": True,
            "dangerous_commands_blocked": True,
            "heavy_rust_validation_requires_rch": True,
        },
    }
    assert_autopilot_e2e_summary(summary)
    return summary


def assert_autopilot_e2e_summary(summary: dict[str, Any]) -> None:
    assert summary.get("schema") == AUTOPILOT_E2E_SCHEMA
    assert summary.get("status") == "pass"
    scenarios = summary.get("scenarios")
    assert isinstance(scenarios, dict)
    missing = set(AUTOPILOT_E2E_REQUIRED_SCENARIOS) - set(scenarios)
    assert not missing, f"autopilot E2E missing scenarios: {sorted(missing)}"
    for scenario_id in AUTOPILOT_E2E_REQUIRED_SCENARIOS:
        scenario = scenarios[scenario_id]
        assert scenario["status"] == "pass", scenario
        assert scenario["selected_action"]
        assert isinstance(scenario["evidence_paths"], list) and scenario["evidence_paths"]
        assert isinstance(scenario["redaction_summary"], dict)
        assert isinstance(scenario["budget_state"], dict)
        assert scenario["command_count"] > 0
    events_path = Path(str(summary.get("events_jsonl")))
    assert events_path.exists(), f"missing autopilot E2E events JSONL: {events_path}"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result_events = [event for event in events if event.get("event") == "scenario_result"]
    assert len(result_events) == len(AUTOPILOT_E2E_REQUIRED_SCENARIOS)
    for event in result_events:
        assert event.get("schema") == AUTOPILOT_E2E_EVENT_SCHEMA
        assert event.get("scenario_id") in AUTOPILOT_E2E_REQUIRED_SCENARIOS
        assert event.get("status") == "pass"
        assert event.get("selected_action")
        assert isinstance(event.get("command_provenance"), list)
        assert isinstance(event.get("evidence_paths"), list) and event.get("evidence_paths")
        assert isinstance(event.get("redaction_summary"), dict)
        assert isinstance(event.get("budget_state"), dict)
        assert_no_dangerous_runnable_commands(event["command_provenance"])


def write_autopilot_e2e_output(
    args: argparse.Namespace,
    summary: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_autopilot_e2e_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(f"refusing to overwrite autopilot E2E summary: {output_path}")
    output_path.write_text(json_dumps(summary, pretty=True), encoding="utf-8")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def file_contains(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False


def load_beads_issue_map(path: Path) -> dict[str, dict[str, Any]]:
    issues: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise RunpackError(f"missing Beads issue export: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            issue = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunpackError(
                f"malformed Beads issue export at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(issue, dict):
            continue
        issue_id = issue.get("id")
        if isinstance(issue_id, str):
            issues[issue_id] = issue
    return issues


def git_value(command: list[str], cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def parse_quality_gate_result(raw: str) -> dict[str, Any]:
    head, separator, command = raw.partition(":")
    if not separator:
        raise RunpackError(
            "quality gate result must use NAME=STATUS:COMMAND format"
        )
    gate_id, gate_separator, status = head.partition("=")
    if not gate_separator:
        raise RunpackError(
            "quality gate result must use NAME=STATUS:COMMAND format"
        )
    gate_id = gate_id.strip()
    status = status.strip()
    command = command.strip()
    if not gate_id or not status or not command:
        raise RunpackError(
            "quality gate result must include non-empty name, status, and command"
        )
    return {"id": gate_id, "status": status, "command": command}


def parse_quality_gate_results(raw_results: list[str] | None) -> list[dict[str, Any]]:
    return [parse_quality_gate_result(raw) for raw in raw_results or []]


def gate_check(
    check_id: str,
    requirement: str,
    passed: bool,
    evidence: list[dict[str, Any]],
    *,
    issue: str | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "requirement": requirement,
        "status": "pass" if passed else "fail",
        "issue": issue,
        "evidence": evidence,
    }


def build_autopilot_decision_gate_summary(
    *,
    generated_at: str,
    quality_gate_results: list[dict[str, Any]],
    issue_export_path: Path | None = None,
    git_refs: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    root = repo_root()
    script_path = root / "scripts/build_swarm_operator_runpack.py"
    readme_path = root / "README.md"
    runbook_path = root / "docs/swarm-operations-runbook.md"
    runpack_contract_path = root / RUNPACK_CONTRACT_PATH
    input_contract_path = root / AUTOPILOT_INPUT_PACK_CONTRACT_PATH
    plan_contract_path = root / AUTOPILOT_PLAN_CONTRACT_PATH
    gate_contract_path = root / AUTOPILOT_DECISION_GATE_CONTRACT_PATH
    issues = load_beads_issue_map(issue_export_path or (root / ".beads/issues.jsonl"))
    checklist: list[dict[str, Any]] = []

    child_states = {
        issue_id: (issues.get(issue_id) or {}).get("status")
        for issue_id in AUTOPILOT_DECISION_GATE_CHILD_BEADS
    }
    child_close_reasons = {
        issue_id: (issues.get(issue_id) or {}).get("close_reason")
        for issue_id in AUTOPILOT_DECISION_GATE_CHILD_BEADS
    }
    missing_children = [
        issue_id
        for issue_id, status in child_states.items()
        if status != "closed"
    ]
    checklist.append(
        gate_check(
            "child_beads_closed",
            "All implementation child Beads are closed before final epic signoff.",
            not missing_children,
            [
                {
                    "path": ".beads/issues.jsonl",
                    "child_statuses": child_states,
                    "close_reasons": child_close_reasons,
                }
            ],
            issue=f"children not closed: {', '.join(missing_children)}"
            if missing_children
            else None,
        )
    )

    checklist.append(
        gate_check(
            "input_pack_contract",
            "Input pack schema, contract, CLI output, and docs are present.",
            all(
                (
                    file_contains(script_path, AUTOPILOT_INPUT_PACK_SCHEMA),
                    file_contains(script_path, "build_autopilot_input_pack"),
                    file_contains(script_path, "assert_autopilot_input_pack_contract"),
                    file_contains(input_contract_path, AUTOPILOT_INPUT_PACK_SCHEMA),
                    file_contains(runbook_path, AUTOPILOT_INPUT_PACK_SCHEMA),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": str(AUTOPILOT_INPUT_PACK_CONTRACT_PATH)},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "planner_contract",
            "Dry-run planner schema, contract, CLI output, golden projection, and docs are present.",
            all(
                (
                    file_contains(script_path, AUTOPILOT_PLAN_SCHEMA),
                    file_contains(script_path, "build_autopilot_plan"),
                    file_contains(script_path, "assert_autopilot_plan_contract"),
                    file_contains(script_path, AUTOPILOT_PLAN_GOLDEN),
                    file_contains(plan_contract_path, AUTOPILOT_PLAN_SCHEMA),
                    file_contains(runbook_path, AUTOPILOT_PLAN_SCHEMA),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": str(AUTOPILOT_PLAN_CONTRACT_PATH)},
                {"path": str(GOLDEN_REPORT_DIRECTORY / AUTOPILOT_PLAN_GOLDEN)},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "work_partitions",
            "Planner emits diagnostic work partition recommendations with contract and regression coverage.",
            all(
                (
                    file_contains(script_path, "build_work_partition_recommendations"),
                    file_contains(plan_contract_path, "required_partition_fields"),
                    file_contains(script_path, "independent_plan"),
                    file_contains(script_path, "overlapping_plan"),
                    file_contains(runbook_path, "work_partitions"),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": str(AUTOPILOT_PLAN_CONTRACT_PATH)},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "failure_actions",
            "Planner emits bounded failure-action recommendations for RCH, Cargo, Agent Mail, Beads, and unknown operational failures.",
            all(
                (
                    file_contains(script_path, "build_failure_action_recommendations"),
                    file_contains(plan_contract_path, "required_failure_action_fields"),
                    file_contains(plan_contract_path, "agent_mail"),
                    file_contains(plan_contract_path, "unknown"),
                    file_contains(runbook_path, "failure_actions"),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": str(AUTOPILOT_PLAN_CONTRACT_PATH)},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "budget_drift",
            "Live budget drift watcher is in the input pack and plan with hysteresis and action coverage.",
            all(
                (
                    file_contains(script_path, BUDGET_DRIFT_SCHEMA),
                    file_contains(script_path, "build_budget_drift_report"),
                    file_contains(script_path, "replay_budget_drift_hysteresis"),
                    file_contains(input_contract_path, "normalized_inputs.budget_drift"),
                    file_contains(plan_contract_path, "required_budget_drift_fields"),
                    file_contains(runbook_path, BUDGET_DRIFT_SCHEMA),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": str(AUTOPILOT_INPUT_PACK_CONTRACT_PATH)},
                {"path": str(AUTOPILOT_PLAN_CONTRACT_PATH)},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "e2e_logging",
            "No-mock E2E evidence covers required scenarios and emits summary plus JSONL events.",
            all(
                (
                    file_contains(script_path, AUTOPILOT_E2E_SCHEMA),
                    file_contains(script_path, AUTOPILOT_E2E_EVENT_SCHEMA),
                    all(
                        file_contains(script_path, scenario)
                        for scenario in AUTOPILOT_E2E_REQUIRED_SCENARIOS
                    ),
                    file_contains(script_path, "build_autopilot_e2e_summary"),
                    file_contains(script_path, "assert_no_dangerous_runnable_commands"),
                    file_contains(runbook_path, AUTOPILOT_E2E_SCHEMA),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {
                    "required_scenarios": list(AUTOPILOT_E2E_REQUIRED_SCENARIOS),
                },
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "runpack_handoff",
            "Runpack projects advisory autopilot input-pack and plan summaries without replacing source artifacts.",
            all(
                (
                    file_contains(script_path, AUTOPILOT_HANDOFF_SCHEMA),
                    file_contains(script_path, "build_autopilot_handoff_summary"),
                    file_contains(script_path, "assert_autopilot_handoff_summary"),
                    file_contains(runpack_contract_path, AUTOPILOT_HANDOFF_SCHEMA),
                    file_contains(readme_path, AUTOPILOT_HANDOFF_SCHEMA),
                    file_contains(runbook_path, AUTOPILOT_HANDOFF_SCHEMA),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": str(RUNPACK_CONTRACT_PATH)},
                {"path": "README.md"},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    forbidden_fragments = set(AUTOPILOT_PLAN_DANGEROUS_COMMAND_FRAGMENTS)
    checklist.append(
        gate_check(
            "safety_guards",
            "Autopilot remains dry-run and blocks destructive runnable commands.",
            all(
                (
                    "git reset --hard" in forbidden_fragments,
                    "git clean -fd" in forbidden_fragments,
                    "rm -rf" in forbidden_fragments,
                    file_contains(script_path, "dangerous_runnable_commands_blocked"),
                    file_contains(script_path, "commands_require_operator_execution"),
                    file_contains(script_path, "assert_autopilot_plan_commands_are_safe"),
                    file_contains(runbook_path, "not a new source of truth"),
                    file_contains(readme_path, "never mutates ownership"),
                )
            ),
            [
                {"forbidden_fragments": sorted(forbidden_fragments)},
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": "README.md"},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )

    if git_refs is None:
        head = git_value(["git", "rev-parse", "HEAD"], root)
        origin_main = git_value(["git", "rev-parse", "origin/main"], root)
        origin_master = git_value(["git", "rev-parse", "origin/master"], root)
    else:
        head = git_refs.get("head")
        origin_main = git_refs.get("origin_main")
        origin_master = git_refs.get("origin_master")
    pushed = bool(head and head == origin_main == origin_master)
    checklist.append(
        gate_check(
            "pushed_commits",
            "Current HEAD is pushed to both origin/main and legacy origin/master.",
            pushed,
            [
                {
                    "head": head,
                    "origin_main": origin_main,
                    "origin_master": origin_master,
                }
            ],
            issue=None if pushed else "HEAD is not synchronized with both remotes",
        )
    )

    quality_by_id = {item["id"]: item for item in quality_gate_results}
    missing_quality = [
        gate_id
        for gate_id in AUTOPILOT_DECISION_GATE_REQUIRED_QUALITY_GATES
        if quality_by_id.get(gate_id, {}).get("status") != "pass"
    ]
    cargo_check_command = str(
        quality_by_id.get("cargo_check_all_targets_rch", {}).get("command") or ""
    )
    cargo_clippy_command = str(
        quality_by_id.get("cargo_clippy_all_targets_rch", {}).get("command") or ""
    )
    rch_proven = (
        "rch exec --" in cargo_check_command
        and "cargo check --all-targets" in cargo_check_command
        and "rch exec --" in cargo_clippy_command
        and "cargo clippy --all-targets -- -D warnings" in cargo_clippy_command
    )
    checklist.append(
        gate_check(
            "quality_gates",
            "Required focused and broad quality gates passed, with heavy Cargo gates run through RCH.",
            not missing_quality and rch_proven,
            [
                {
                    "required_quality_gates": list(
                        AUTOPILOT_DECISION_GATE_REQUIRED_QUALITY_GATES
                    ),
                    "provided_quality_gates": quality_gate_results,
                    "heavy_cargo_uses_rch": rch_proven,
                }
            ],
            issue=(
                "missing or failing quality gates: " + ", ".join(missing_quality)
                if missing_quality
                else "heavy Cargo gates did not prove rch exec usage"
                if not rch_proven
                else None
            ),
        )
    )

    missing_checks = [
        item["id"]
        for item in checklist
        if item.get("status") != "pass"
    ]
    final_gate_issue = issues.get("bd-h3uv0.8") or {}
    parent_issue = issues.get("bd-h3uv0") or {}
    status = "pass" if not missing_checks else "fail"
    summary = {
        "schema": AUTOPILOT_DECISION_GATE_SCHEMA,
        "generated_at": generated_at,
        "status": status,
        "purpose": "prompt_to_artifact_autopilot_epic_close_gate_not_source_of_truth",
        "parent_epic": {
            "id": "bd-h3uv0",
            "status": parent_issue.get("status"),
            "title": parent_issue.get("title"),
        },
        "final_gate_bead": {
            "id": "bd-h3uv0.8",
            "status": final_gate_issue.get("status"),
            "assignee": final_gate_issue.get("assignee"),
        },
        "required_checks": list(AUTOPILOT_DECISION_GATE_REQUIRED_CHECKS),
        "checklist": checklist,
        "missing_checks": missing_checks,
        "follow_up_required": bool(missing_checks),
        "follow_up_beads": [
            {
                "title": f"[AUTOPILOT-GATE] Fix missing {check_id}",
                "type": "task",
                "priority": 2,
                "source_check": check_id,
            }
            for check_id in missing_checks
        ],
        "decision": (
            "close_final_gate_and_parent_epic"
            if status == "pass"
            else "file_follow_up_beads_before_closing_epic"
        ),
        "epic_can_close_after_this_commit": status == "pass",
    }
    assert_autopilot_decision_gate_contract(summary)
    return summary


def assert_autopilot_decision_gate_contract(summary: dict[str, Any]) -> None:
    root = repo_root()
    contract_path = root / AUTOPILOT_DECISION_GATE_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing autopilot decision gate contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"autopilot decision gate contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == AUTOPILOT_DECISION_GATE_CONTRACT_SCHEMA
    assert contract.get("decision_gate_schema") == AUTOPILOT_DECISION_GATE_SCHEMA
    assert summary.get("schema") == contract["decision_gate_schema"]
    assert summary.get("purpose") == contract.get("purpose")
    assert summary.get("status") in set(contract.get("allowed_statuses", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in summary, f"missing top-level decision-gate key: {key}"
    checks = summary.get("checklist")
    assert isinstance(checks, list) and checks
    check_by_id = {
        item.get("id"): item
        for item in checks
        if isinstance(item, dict)
    }
    missing_required = set(contract.get("required_check_ids", [])) - set(check_by_id)
    assert not missing_required, (
        f"decision gate missing required checks: {sorted(missing_required)}"
    )
    for check in checks:
        assert isinstance(check, dict)
        assert check.get("status") in set(contract.get("allowed_check_statuses", []))
        assert check.get("requirement")
        evidence = check.get("evidence")
        assert isinstance(evidence, list) and evidence
    quality = check_by_id.get("quality_gates", {})
    evidence = quality.get("evidence", [])
    assert isinstance(evidence, list) and evidence
    payload = evidence[0]
    assert isinstance(payload, dict)
    required_quality = set(contract.get("required_quality_gate_ids", []))
    provided = {
        item.get("id")
        for item in payload.get("provided_quality_gates", [])
        if isinstance(item, dict) and item.get("status") == "pass"
    }
    if summary.get("status") == "pass":
        assert set(summary.get("missing_checks", [])) == set()
        assert provided.issuperset(required_quality)
        assert payload.get("heavy_cargo_uses_rch") is True
        assert summary.get("epic_can_close_after_this_commit") is True


def write_autopilot_decision_gate_output(
    args: argparse.Namespace,
    summary: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_autopilot_final_gate_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(f"refusing to overwrite autopilot final gate: {output_path}")
    output_path.write_text(json_dumps(summary, pretty=True), encoding="utf-8")


def run_self_test() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="pi_swarm_runpack_"))
    generated_at = "2026-05-09T09:00:00+00:00"
    accepted_preflight = {
        "schema": HOST_PREFLIGHT_SCHEMA,
        "generated_at": generated_at,
        "status": "pass",
        "cpu": {
            "logical_cores": 16,
            "effective_cores": 8,
            "cgroup_quota": {"quota_cores": 8.0, "unlimited": False},
            "cpuset": {"cpu_count": 8},
        },
        "numa": {"node_count": 2, "nodes": [0, 1]},
        "memory": {
            "cgroup_limit_bytes": 34359738368,
            "effective_limit_bytes": 34359738368,
            "unlimited": False,
        },
        "tmpfs_headroom": {
            "expected_root": "/data/tmp/pi_agent_rust_cargo",
            "paths": [
                {
                    "env_name": "CARGO_TARGET_DIR",
                    "path": "/data/tmp/pi_agent_rust_cargo/test/target",
                    "ready": True,
                    "available_kb": 52428800,
                },
                {
                    "env_name": "TMPDIR",
                    "path": "/data/tmp/pi_agent_rust_cargo/test/tmp",
                    "ready": True,
                    "available_kb": 52428800,
                },
            ],
        },
        "recommended_budgets": {
            "agent_concurrency": 4,
            "tool_concurrency": 8,
            "extension_hostcall_lanes": 16,
            "rch_verification_fanout": 2,
            "max_queue_depth": 2,
            "max_rss_bytes": 17179869184,
            "plan_confidence": "high",
        },
        "critical_failures": [],
        "source_errors": [],
    }
    doctor_path = write_json(
        workspace / "doctor.json",
        {
            "overall": "warn",
            "summary": {"pass": 1, "info": 0, "warn": 1, "fail": 0},
            "findings": [
                {
                    "category": "swarm",
                    "severity": "warn",
                    "title": "Agent Mail reservations expire soon",
                    "detail": "token=super-secret-value should be redacted",
                    "remediation": "Renew active reservations before long-running verification",
                    "data": {"schema": "pi.doctor.agent_mail_build_slots.v1", "active": 1},
                    "fixability": "not_fixable",
                }
            ],
        },
    )
    claim_path = write_json(
        workspace / "claim.json",
        {
            "schema": "pi.swarm.claim_readiness_report.v1",
            "overall_status": "ready",
            "max_age_days": 14,
            "artifact_statuses": [
                {
                    "id": "activity_ledger_digest",
                    "category": "activity_ledger",
                    "status": "ready",
                    "release_blocking": True,
                    "issue_kinds": [],
                }
            ],
            "stale_claims": {"summary": {"stale_count": 0}},
        },
    )
    smoke_path = write_json(
        workspace / "smoke.json",
        {
            "schema": "pi.swarm.smoke_harness.v1",
            "status": "pass",
            "correlation_id": "selftest",
            "reservation_ids": [1],
            "failed_scenarios": [],
            "scenarios": {
                "reservation_conflict": {"status": "pass"},
                "dirty_worktree_preserved": {"status": "pass"},
            },
            "artifacts": {"summary_json": str(workspace / "smoke.json")},
            "artifact_manifest": [
                {
                    "id": "events_jsonl",
                    "path": str(workspace / "events.jsonl"),
                    "size_bytes": 128,
                    "sha256": "a" * 64,
                }
            ],
        },
    )
    activity_path = write_json(
        workspace / "activity.json",
        {
            "schema": "pi.swarm.activity_digest.v1",
            "saturation": {
                "saturated": True,
                "signals": ["high_chatter_low_throughput"],
                "reasons": ["7 coordination events and 1 throughput event"],
                "evidence_pointers": ["agent:MagentaOak"],
            },
            "recommendations": [{"mode": "testing-golden-artifacts"}],
        },
    )
    cargo_path = write_json(
        workspace / "cargo.json",
        {
            "schema": "pi.cargo_headroom.admission.v1",
            "decision": "backoff",
            "reason": "rch_queue_saturated",
            "requested_runner": "auto",
            "resolved_runner": "none",
            "command_class": "heavy",
            "allow_local_fallback": False,
            "cargo_target_dir": "/data/tmp/pi_agent_rust_cargo/test/target",
            "tmpdir": "/data/tmp/pi_agent_rust_cargo/test/tmp",
            "rch_queue_forecast": {
                "schema": "pi.cargo_headroom.rch_queue_forecast.v1",
                "status": "ok",
                "recommended_action": "backoff",
                "reason": "queue_saturated",
                "slot_pressure": "saturated",
                "queue_depth": 4,
                "active_builds": 8,
                "queued_builds": 4,
                "slots_available": 0,
                "slots_total": 8,
                "workers_healthy": 1,
                "workers_total": 8,
                "estimated_wait_seconds": 240,
            },
        },
    )
    beads_path = write_json(
        workspace / "beads.json",
        {
            "issues": [
                {
                    "id": "bd-stale",
                    "title": "Stale fixture",
                    "status": "in_progress",
                    "assignee": "GreenStone",
                    "updated_at": "2026-05-08T00:00:00+00:00",
                },
                {
                    "id": "bd-open",
                    "title": "Open fixture",
                    "status": "open",
                    "updated_at": generated_at,
                },
            ]
        },
    )
    agent_mail_status_path = write_json(
        workspace / "agent-mail-status.json",
        {
            "schema": "pi.agent_mail.robot_status.v1",
            "generated_at": generated_at,
            "status": "error",
            "health_level": "red",
            "registration_token": "super-secret-registration-token",
            "issue": "database schema missing required tables",
        },
    )
    agent_mail_reservations_path = write_json(
        workspace / "agent-mail-reservations.json",
        {
            "schema": "pi.agent_mail.robot_reservations.v1",
            "generated_at": generated_at,
            "status": "ok",
            "reservations": [
                {
                    "id": 7,
                    "agent": "GreenStone",
                    "path": "src/doctor.rs",
                    "exclusive": True,
                    "released_ts": None,
                }
            ],
        },
    )
    git_path = write_json(
        workspace / "git-status.json",
        {
            "schema": GIT_CONTEXT_SCHEMA,
            "generated_at": generated_at,
            "branch": "main",
            "head": "abc123fixture",
            "upstream": {
                "name": "origin/main",
                "ahead": 1,
                "behind": 0,
                "status": "ok",
            },
            "porcelain_lines": [" M src/doctor.rs", "?? scripts/new-tool.py"],
            "recent_commits": ["abc123fixture bd-fixture closeout"],
            "recent_remote_commits": ["def456fixture origin/main prior closeout"],
        },
    )
    tail_latency_path = write_json(
        workspace / "tail-latency.json",
        {
            "schema": TAIL_LATENCY_SCHEMA,
            "generated_at": generated_at,
            "purpose": "operator_observability_not_release_performance_claim",
            "telemetry_enabled": True,
            "sample_window": 512,
            "redaction_summary": {
                "redacted_count": 0,
                "fields": [],
                "policy": "timing_only_no_prompt_or_tool_payload_fields",
            },
            "metrics": [
                {
                    "id": "provider_streaming",
                    "label": "Provider streaming",
                    "snapshot": {
                        "count": 3,
                        "total_us": 600,
                        "max_us": 300,
                        "avg_us": 200,
                        "tail": {
                            "sample_window": 512,
                            "sample_count": 3,
                            "p95_us": 300,
                            "p99_us": 300,
                            "p999_us": 300,
                        },
                    },
                }
            ],
        },
    )
    flight_recorder_path = write_json(
        workspace / "flight-recorder-report.json",
        {
            "schema": FLIGHT_RECORDER_REPORT_SCHEMA,
            "generated_at": generated_at,
            "dominant_latency_components": [
                {"component": "provider_streaming", "count": 3, "total_us": 900},
                {"component": "tool_execution", "count": 2, "total_us": 250},
            ],
            "component_counts": {"provider": 3, "tool": 2, "session": 2},
            "coordination_failures": [],
        },
    )
    host_preflight_path = write_json(
        workspace / "host-preflight.json",
        accepted_preflight,
    )
    hostcall_profile_path = write_json(
        workspace / "hostcall-profile.json",
        {
            "schema": HOSTCALL_SWARM_PROFILE_SCHEMA,
            "generated_at": generated_at,
            "agents": 4,
            "hostcalls_per_agent": 32,
            "profiles": [
                {
                    "mode": "compat",
                    "accepted_requests": 128,
                    "completed_requests": 128,
                    "p99_tail_latency_steps": 4,
                    "max_tail_latency_steps": 6,
                }
            ],
        },
    )
    session_profile_path = write_json(
        workspace / "session-recovery-profile.json",
        {
            "schema": SESSION_RECOVERY_SWARM_PROFILE_SCHEMA,
            "generated_at": generated_at,
            "counts": {
                "base_entries": 200,
                "tail_entries_appended": 32,
                "recovered_entries_after_truncation": 200,
            },
            "timings_us": {"recover": 800, "index": 1500, "save": 700},
        },
    )
    rpc_swarm_path = write_json(
        workspace / "rpc-swarm-e2e.json",
        {
            "schema": RPC_SWARM_E2E_SCHEMA,
            "generated_at": generated_at,
            "status": "pass",
            "sessions": 3,
            "command_ids": ["cmd-a", "cmd-b", "cmd-c"],
            "filesystem_state": "preserved",
            "session_index": "updated",
        },
    )
    rch_artifact_sync_path = write_json(
        workspace / "rch-artifact-sync.json",
        {
            "schema": RCH_ARTIFACT_SYNC_SCHEMA,
            "generated_at": generated_at,
            "status": "pass",
            "required_paths": [
                {"path": "tests/perf/reports/bench_schema_registry.json", "included": True}
            ],
            "violations": [],
        },
    )
    validation_path = workspace / "validation.log"
    validation_path.write_text(
        "cargo clippy failed\nerror: token=super-secret-value should be redacted\n",
        encoding="utf-8",
    )

    args = argparse.Namespace(
        doctor_json=doctor_path,
        claim_readiness_json=claim_path,
        smoke_summary_json=smoke_path,
        activity_digest_json=activity_path,
        cargo_admission_json=cargo_path,
        beads_json=beads_path,
        beads_ready_json=None,
        agent_mail_status_json=agent_mail_status_path,
        agent_mail_reservations_json=agent_mail_reservations_path,
        git_status_file=git_path,
        tail_latency_json=tail_latency_path,
        flight_recorder_report_json=flight_recorder_path,
        host_preflight_json=host_preflight_path,
        hostcall_swarm_profile_json=hostcall_profile_path,
        session_recovery_swarm_profile_json=session_profile_path,
        rpc_swarm_e2e_json=rpc_swarm_path,
        rch_artifact_sync_json=rch_artifact_sync_path,
        validation_outputs=[validation_path],
        capture_manifest={
            "schema": RUNPACK_CAPTURE_SCHEMA,
            "mode": "current",
            "status": "degraded",
            "generated_at": generated_at,
            "capture_dir": str(workspace / "capture"),
            "project_root": str(workspace),
            "generated_source_paths": {
                "git_status": str(git_path),
                "beads": str(beads_path),
                "cargo_admission": str(cargo_path),
                "agent_mail_status": str(agent_mail_status_path),
                "agent_mail_reservations": str(agent_mail_reservations_path),
            },
            "commands": [
                {
                    "id": "agent_mail_status",
                    "command": "am robot status --format json",
                    "status": "failed",
                    "exit_code": 2,
                    "issue": "database schema missing required tables",
                },
                {
                    "id": "agent_mail_reservations",
                    "command": "am robot reservations --format json",
                    "status": "ok",
                    "exit_code": 0,
                    "issue": None,
                },
                {
                    "id": "rch_queue",
                    "command": "rch queue --json",
                    "status": "ok",
                    "exit_code": 0,
                    "issue": None,
                },
            ],
        },
        capture_dir=workspace / "capture",
        out_json=workspace / "runpack.json",
        out_md=workspace / "runpack.md",
        operator_runpack_json=None,
        out_autopilot_input_pack_json=None,
        out_autopilot_plan_json=None,
        print_autopilot_input_pack=False,
        print_autopilot_plan=False,
        generated_at=generated_at,
        stale_after_hours=24,
        max_items=4,
    )
    try:
        runpack = build_runpack(args)
        write_outputs(args, runpack)
        assert runpack["schema"] == RUNPACK_SCHEMA
        assert runpack["status"] == "degraded"
        assert runpack["agent_mail"]["build_slots"]["data"]["active"] == 1
        assert runpack["beads"]["stale"][0]["id"] == "bd-stale"
        assert runpack["rch_admission"]["queue_forecast"]["recommended_action"] == "backoff"
        assert runpack["activity_digest"]["saturated"] is True
        assert runpack["git_state"]["dirty"] is True
        assert runpack["git_state"]["branch"] == "main"
        assert runpack["git_state"]["upstream"]["ahead"] == 1
        assert runpack["agent_mail_read_state"]["status"] == "degraded"
        assert runpack["validation_outputs"]["status"] == "failed"
        assert runpack["validation_outputs"]["outputs"][0]["inferred_status"] == "failed"
        assert any(
            item["purpose"] == "Regenerate this handoff bundle"
            for item in runpack["resume_commands"]
        )
        dashboard = runpack["bottleneck_attribution"]
        assert dashboard["schema"] == BOTTLENECK_ATTRIBUTION_SCHEMA
        assert dashboard["purpose"] == "operator_diagnostic_not_release_performance_claim"
        assert dashboard["surface_coverage"]["provider_streaming"]["status"] == "covered"
        assert dashboard["surface_coverage"]["local_tools"]["status"] == "covered"
        assert dashboard["surface_coverage"]["extension_hostcalls"]["status"] == "covered"
        assert dashboard["surface_coverage"]["persistence"]["status"] == "covered"
        assert dashboard["surface_coverage"]["rch_sync_retrieval"]["status"] == "covered"
        assert dashboard["surface_coverage"]["queue_pressure"]["status"] == "covered"
        assert dashboard["surface_coverage"]["cgroup_numa_context"]["status"] == "covered"
        assert any(
            item["id"] == "rpc_swarm_e2e" and item["classification"] == "fresh"
            for item in dashboard["input_classification"]
        )
        assert any(
            item["id"] == "session_recovery_swarm_profile"
            and item["classification"] == "fresh"
            for item in dashboard["input_classification"]
        )
        scorecard = runpack["swarm_scale_safety_scorecard"]
        assert scorecard["schema"] == SAFETY_SCORECARD_SCHEMA
        assert scorecard["overall_status"] == "degraded"
        scorecard_dimensions = {
            dimension["id"]: dimension for dimension in scorecard["dimensions"]
        }
        assert set(scorecard_dimensions) == {
            "coordination_health",
            "cargo_rch_posture",
            "perf_evidence_freshness",
            "dirty_worktree_tolerance",
            "stalled_bead_hygiene",
            "resource_governor_readiness",
            "bottleneck_attribution_coverage",
            "test_coverage",
        }
        assert scorecard_dimensions["cargo_rch_posture"]["status"] == "red"
        assert scorecard_dimensions["test_coverage"]["status"] == "green"
        for dimension in scorecard_dimensions.values():
            assert dimension["evidence_paths"]
            if dimension["status"] == "green":
                assert dimension["missing_evidence"] == []
        assert runpack["tail_latency"]["schema"] == TAIL_LATENCY_SCHEMA
        assert runpack["tail_latency"]["redaction_summary"]["policy"] == (
            "timing_only_no_prompt_or_tool_payload_fields"
        )
        assert runpack["tail_latency"]["metrics"][0]["p999_us"] == 300
        assert runpack["smoke_harness"]["artifact_manifest"][0]["sha256"] == "a" * 64
        for source in runpack["source_statuses"]:
            assert source["size_bytes"] is not None
            assert len(source["sha256"]) == 64
        assert runpack["redaction_summary"]["redacted_count"] >= 1
        assert args.out_json.exists() and args.out_md.exists()
        markdown = args.out_md.read_text(encoding="utf-8")
        assert "Tail latency telemetry" in markdown
        assert "Bottleneck Attribution" in markdown
        assert "Resume Commands" in markdown
        assert "Git Context" in markdown
        assert_runpack_contract(runpack)
        assert_runpack_golden(runpack, workspace)
        autopilot_args = argparse.Namespace(
            **{
                **vars(args),
                "operator_runpack_json": args.out_json,
                "out_autopilot_input_pack_json": workspace / "autopilot-input-pack.json",
            }
        )
        input_pack = build_autopilot_input_pack(autopilot_args)
        write_autopilot_input_pack_output(autopilot_args, input_pack)
        assert input_pack["schema"] == AUTOPILOT_INPUT_PACK_SCHEMA
        assert input_pack["status"] == "degraded"
        assert input_pack["normalized_inputs"]["agent_mail"]["status"] == "degraded"
        assert input_pack["normalized_inputs"]["agent_mail"]["fallback_action"] == "use_beads_soft_lock"
        assert input_pack["normalized_inputs"]["budget_drift"]["status"] == "deny_new_work"
        assert input_pack["normalized_inputs"]["budget_drift"]["schema"] == BUDGET_DRIFT_SCHEMA
        assert input_pack["normalized_inputs"]["operator_runpack"]["status"] == "ok"
        assert input_pack["normalized_inputs"]["beads_ready"]["status"] == "not_provided"
        assert any(
            item.get("id") == "operator_runpack" and item.get("status") == "ok"
            for item in input_pack["source_statuses"]
        )
        assert any("Agent Mail status" in reason for reason in input_pack["degraded_reasons"])
        assert input_pack["redaction_summary"]["redacted_count"] >= 1
        assert autopilot_args.out_autopilot_input_pack_json.exists()
        assert_autopilot_input_pack_contract(input_pack)
        plan = build_autopilot_plan(input_pack, max_items=args.max_items)
        assert plan["schema"] == AUTOPILOT_PLAN_SCHEMA
        assert plan["status"] == "degraded"
        assert plan["budget_drift"]["status"] == "deny_new_work"
        plan_actions = [item["action"] for item in plan["actions"]]
        assert plan_actions == [
            "adjust_swarm_budget",
            "wait_for_rch",
            "use_beads_soft_lock",
            "reopen_stale_bead_candidate",
        ]
        assert plan["work_partitions"] == []
        assert_autopilot_plan_contract(plan)
        assert_autopilot_plan_golden(plan, workspace)
        plan_output_args = argparse.Namespace(
            **{
                **vars(autopilot_args),
                "out_autopilot_plan_json": workspace / "autopilot-plan.json",
            }
        )
        write_autopilot_plan_output(plan_output_args, plan)
        assert plan_output_args.out_autopilot_plan_json.exists()
        handoff_runpack = {
            **runpack,
            "autopilot_handoff": build_autopilot_handoff_summary(
                plan_output_args,
                input_pack,
                plan,
            ),
        }
        assert_autopilot_handoff_summary(handoff_runpack)
        assert_runpack_contract(handoff_runpack)
        handoff_markdown = render_markdown(handoff_runpack)
        assert "Autopilot Handoff" in handoff_markdown
        assert "pi.swarm.autopilot_input_pack.v1" in handoff_markdown
        assert "pi.swarm.autopilot_plan.v1" in handoff_markdown
        assert "adjust_swarm_budget" in handoff_markdown
        missing_agent_mail_args = argparse.Namespace(
            **{
                **vars(autopilot_args),
                "agent_mail_status_json": None,
                "agent_mail_reservations_json": None,
                "out_autopilot_input_pack_json": None,
            }
        )
        missing_input_pack = build_autopilot_input_pack(missing_agent_mail_args)
        assert missing_input_pack["status"] == "degraded"
        assert any(
            "agent_mail_status" in reason for reason in missing_input_pack["degraded_reasons"]
        )
        missing_plan = build_autopilot_plan(missing_input_pack, max_items=args.max_items)
        assert missing_plan["status"] == "blocked"
        assert missing_plan["actions"][0]["action"] == "stop_and_surface_blocker"
        clean_git_path = write_json(
            workspace / "git-status-clean.json",
            {
                "schema": GIT_CONTEXT_SCHEMA,
                "generated_at": generated_at,
                "branch": "main",
                "head": "cleanfixture",
                "upstream": {"name": "origin/main", "ahead": 0, "behind": 0, "status": "ok"},
                "porcelain_lines": [],
                "recent_commits": [],
                "recent_remote_commits": [],
            },
        )
        empty_beads_path = write_json(workspace / "beads-empty.json", {"issues": []})
        cargo_admit_path = write_json(
            workspace / "cargo-admit.json",
            {
                "schema": "pi.cargo_headroom.admission.v1",
                "decision": "admit",
                "reason": "healthy_fixture",
                "requested_runner": "rch",
                "resolved_runner": "rch",
                "command_class": "heavy",
                "allow_local_fallback": False,
                "cargo_target_dir": "/data/tmp/pi_agent_rust_cargo/test/target",
                "tmpdir": "/data/tmp/pi_agent_rust_cargo/test/tmp",
                "rch_queue_forecast": {
                    "schema": "pi.cargo_headroom.rch_queue_forecast.v1",
                    "status": "ok",
                    "recommended_action": "proceed",
                    "slot_pressure": "available",
                    "queue_depth": 0,
                    "active_builds": 0,
                    "queued_builds": 0,
                    "slots_available": 8,
                    "slots_total": 8,
                    "workers_healthy": 8,
                    "workers_total": 8,
                    "estimated_wait_seconds": 0,
                },
            },
        )
        agent_mail_ok_path = write_json(
            workspace / "agent-mail-status-ok.json",
            {
                "schema": "pi.agent_mail.robot_status.v1",
                "generated_at": generated_at,
                "status": "ok",
                "health_level": "green",
                "registration_token": "super-secret-registration-token",
            },
        )
        agent_mail_reservations_empty_path = write_json(
            workspace / "agent-mail-reservations-empty.json",
            {
                "schema": "pi.agent_mail.robot_reservations.v1",
                "generated_at": generated_at,
                "status": "ok",
                "reservations": [],
            },
        )
        ready_beads_path = write_json(
            workspace / "beads-ready.json",
            [
                {
                    "id": "bd-ready",
                    "title": "Ready fixture",
                    "status": "open",
                    "priority": 1,
                    "updated_at": generated_at,
                    "labels": ["autopilot"],
                }
            ],
        )
        open_beads_path = write_json(
            workspace / "beads-open.json",
            {
                "issues": [
                    {
                        "id": "bd-ready",
                        "title": "Ready fixture",
                        "status": "open",
                        "priority": 1,
                        "updated_at": generated_at,
                    }
                ]
            },
        )
        healthy_args = argparse.Namespace(
            **{
                **vars(autopilot_args),
                "cargo_admission_json": cargo_admit_path,
                "beads_json": open_beads_path,
                "beads_ready_json": ready_beads_path,
                "agent_mail_status_json": agent_mail_ok_path,
                "agent_mail_reservations_json": agent_mail_reservations_empty_path,
                "git_status_file": clean_git_path,
                "operator_runpack_json": None,
                "capture_manifest": {
                    "schema": RUNPACK_CAPTURE_SCHEMA,
                    "mode": "current",
                    "status": "ok",
                    "generated_at": generated_at,
                    "capture_dir": str(workspace / "capture-healthy"),
                    "project_root": str(workspace),
                    "generated_source_paths": {},
                    "commands": [
                        {
                            "id": "beads_ready",
                            "command": "br ready --json",
                            "status": "ok",
                            "exit_code": 0,
                            "issue": None,
                        },
                        {
                            "id": "agent_mail_status",
                            "command": "am robot status --format json",
                            "status": "ok",
                            "exit_code": 0,
                            "issue": None,
                        },
                    ],
                },
            }
        )
        healthy_input_pack = build_autopilot_input_pack(healthy_args)
        healthy_plan = build_autopilot_plan(healthy_input_pack, max_items=args.max_items)
        assert healthy_input_pack["status"] == "ready"
        assert healthy_plan["status"] == "ready"
        assert healthy_input_pack["normalized_inputs"]["budget_drift"]["status"] == "stable"
        assert healthy_plan["budget_drift"]["status"] == "stable"
        assert [item["action"] for item in healthy_plan["actions"]] == ["claim_ready_bead"]
        healthy_partition = healthy_plan["work_partitions"][0]
        assert healthy_partition["issue_id"] == "bd-ready"
        assert "autopilot_runpack" in healthy_partition["surface_ids"]
        assert "scripts/build_swarm_operator_runpack.py" in healthy_partition["suggested_reservation"]
        assert healthy_partition["avoid"] == []
        assert healthy_partition["confidence"] == "high"
        assert healthy_partition["degraded_caveats"] == []
        assert_autopilot_plan_contract(healthy_plan)

        def clone_json(value: Any) -> Any:
            return json.loads(json_dumps(value))

        def doctor_with_preflight(name: str, preflight: dict[str, Any]) -> Path:
            payload = clone_json(json.loads(doctor_path.read_text(encoding="utf-8")))
            payload["findings"].append(
                {
                    "category": "swarm",
                    "severity": "pass",
                    "title": "Swarm resource preflight ready",
                    "detail": "resource profile accepted",
                    "remediation": None,
                    "data": preflight,
                    "fixability": "not_fixable",
                }
            )
            return write_json(workspace / f"{name}-doctor.json", payload)

        def build_budget_drift_fixture(
            name: str,
            *,
            current_preflight: dict[str, Any],
            cargo_payload: dict[str, Any] | None = None,
            beads_payload: dict[str, Any] | None = None,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            fixture_args = argparse.Namespace(
                **{
                    **vars(healthy_args),
                    "doctor_json": doctor_with_preflight(name, current_preflight),
                    "cargo_admission_json": write_json(
                        workspace / f"{name}-cargo.json",
                        cargo_payload
                        or json.loads(cargo_admit_path.read_text(encoding="utf-8")),
                    ),
                    "beads_json": write_json(
                        workspace / f"{name}-beads.json",
                        beads_payload
                        or json.loads(open_beads_path.read_text(encoding="utf-8")),
                    ),
                }
            )
            fixture_input_pack = build_autopilot_input_pack(fixture_args)
            fixture_plan = build_autopilot_plan(fixture_input_pack, max_items=args.max_items)
            assert_autopilot_input_pack_contract(fixture_input_pack)
            assert_autopilot_plan_contract(fixture_plan)
            return fixture_input_pack, fixture_plan

        cpu_reduced_preflight = clone_json(accepted_preflight)
        cpu_reduced_preflight["cpu"]["effective_cores"] = 4
        cpu_reduced_input, cpu_reduced_plan = build_budget_drift_fixture(
            "budget-drift-cpu-reduced",
            current_preflight=cpu_reduced_preflight,
        )
        assert cpu_reduced_input["normalized_inputs"]["budget_drift"]["status"] == "degraded"
        assert any(
            signal["id"] == "cpu_quota_reduced"
            for signal in cpu_reduced_input["normalized_inputs"]["budget_drift"]["signals"]
        )
        assert "adjust_swarm_budget" in [
            action["action"] for action in cpu_reduced_plan["actions"]
        ]

        memory_reduced_preflight = clone_json(accepted_preflight)
        memory_reduced_preflight["memory"]["effective_limit_bytes"] = 8 * 1024 * 1024 * 1024
        memory_reduced_input, _ = build_budget_drift_fixture(
            "budget-drift-memory-reduced",
            current_preflight=memory_reduced_preflight,
        )
        assert memory_reduced_input["normalized_inputs"]["budget_drift"]["status"] == "deny_new_work"
        assert any(
            signal["id"] == "memory_headroom_reduced"
            for signal in memory_reduced_input["normalized_inputs"]["budget_drift"]["signals"]
        )

        tmpdir_drift_cargo = json.loads(cargo_admit_path.read_text(encoding="utf-8"))
        tmpdir_drift_cargo["tmpdir"] = "/tmp/pi-agent-drift"
        tmpdir_drift_input, _ = build_budget_drift_fixture(
            "budget-drift-tmpdir",
            current_preflight=accepted_preflight,
            cargo_payload=tmpdir_drift_cargo,
        )
        assert tmpdir_drift_input["normalized_inputs"]["budget_drift"]["status"] == "degraded"
        assert any(
            signal["id"] == "tmpdir_path_drift"
            for signal in tmpdir_drift_input["normalized_inputs"]["budget_drift"]["signals"]
        )

        queue_saturated_cargo = json.loads(cargo_admit_path.read_text(encoding="utf-8"))
        queue_saturated_cargo["rch_queue_forecast"]["queue_depth"] = 8
        queue_saturated_cargo["rch_queue_forecast"]["recommended_action"] = "backoff"
        queue_saturated_cargo["rch_queue_forecast"]["slot_pressure"] = "saturated"
        queue_saturated_input, _ = build_budget_drift_fixture(
            "budget-drift-rch-queue",
            current_preflight=accepted_preflight,
            cargo_payload=queue_saturated_cargo,
        )
        assert queue_saturated_input["normalized_inputs"]["budget_drift"]["status"] == "deny_new_work"
        assert any(
            signal["id"] == "rch_queue_saturated"
            for signal in queue_saturated_input["normalized_inputs"]["budget_drift"]["signals"]
        )

        recovered_input, _ = build_budget_drift_fixture(
            "budget-drift-recovered",
            current_preflight=accepted_preflight,
        )
        assert recovered_input["normalized_inputs"]["budget_drift"]["status"] == "stable"
        replay = replay_budget_drift_hysteresis(
            [
                cpu_reduced_input["normalized_inputs"]["budget_drift"],
                recovered_input["normalized_inputs"]["budget_drift"],
                recovered_input["normalized_inputs"]["budget_drift"],
            ]
        )
        assert replay["effective_statuses"] == ["degraded", "degraded", "stable"]
        assert replay["hysteresis_applied"] is True

        independent_ready_path = write_json(
            workspace / "beads-ready-independent.json",
            [
                {
                    "id": "bd-provider",
                    "title": "Add OpenAI provider streaming parity",
                    "description": "Provider body for streaming fixture coverage",
                    "status": "open",
                    "priority": 1,
                    "updated_at": generated_at,
                    "labels": ["provider", "openai"],
                },
                {
                    "id": "bd-tools",
                    "title": "Harden read tool conformance",
                    "description": "Read tool body for conformance fixtures",
                    "status": "open",
                    "priority": 2,
                    "updated_at": generated_at,
                    "labels": ["tools", "conformance"],
                },
            ],
        )
        independent_args = argparse.Namespace(
            **{
                **vars(healthy_args),
                "beads_ready_json": independent_ready_path,
            }
        )
        independent_input_pack = build_autopilot_input_pack(independent_args)
        independent_plan = build_autopilot_plan(independent_input_pack, max_items=args.max_items)
        independent_partitions = {
            item["issue_id"]: item for item in independent_plan["work_partitions"]
        }
        assert "src/providers/**/*.rs" in independent_partitions["bd-provider"][
            "suggested_reservation"
        ]
        assert "src/tools.rs" in independent_partitions["bd-tools"]["suggested_reservation"]
        assert set(independent_partitions["bd-provider"]["suggested_reservation"]).isdisjoint(
            independent_partitions["bd-tools"]["suggested_reservation"]
        )
        assert independent_partitions["bd-provider"]["avoid"] == []
        assert independent_partitions["bd-tools"]["avoid"] == []
        assert_autopilot_plan_contract(independent_plan)

        agent_mail_reservations_provider_path = write_json(
            workspace / "agent-mail-reservations-provider.json",
            {
                "schema": "pi.agent_mail.robot_reservations.v1",
                "generated_at": generated_at,
                "status": "ok",
                "reservations": [
                    {
                        "id": 11,
                        "agent": "BlueLake",
                        "path": "src/providers/**/*.rs",
                        "exclusive": True,
                        "reason": "bd-provider-owner",
                        "released_ts": None,
                    }
                ],
            },
        )
        overlapping_args = argparse.Namespace(
            **{
                **vars(independent_args),
                "agent_mail_reservations_json": agent_mail_reservations_provider_path,
            }
        )
        overlapping_plan = build_autopilot_plan(
            build_autopilot_input_pack(overlapping_args),
            max_items=args.max_items,
        )
        overlapping_provider = {
            item["issue_id"]: item for item in overlapping_plan["work_partitions"]
        }["bd-provider"]
        assert overlapping_provider["confidence"] == "medium"
        assert any(
            item["source"] == "agent_mail" and item["path"] == "src/providers/**/*.rs"
            for item in overlapping_provider["avoid"]
        )
        assert overlapping_provider["alternate_surfaces"]
        assert_autopilot_plan_contract(overlapping_plan)

        stale_provider_beads_path = write_json(
            workspace / "beads-stale-provider.json",
            {
                "issues": [
                    {
                        "id": "bd-stale-provider",
                        "title": "Provider streaming stale owner",
                        "status": "in_progress",
                        "assignee": "StaleOwner",
                        "updated_at": "2026-05-08T00:00:00+00:00",
                    }
                ]
            },
        )
        stale_provider_args = argparse.Namespace(
            **{
                **vars(independent_args),
                "beads_json": stale_provider_beads_path,
            }
        )
        stale_provider_plan = build_autopilot_plan(
            build_autopilot_input_pack(stale_provider_args),
            max_items=args.max_items,
        )
        stale_provider_partition = {
            item["issue_id"]: item for item in stale_provider_plan["work_partitions"]
        }["bd-provider"]
        assert stale_provider_partition["confidence"] == "medium"
        assert any(
            item["source"] == "beads" and item["holder"] == "StaleOwner"
            for item in stale_provider_partition["avoid"]
        )
        assert_autopilot_plan_contract(stale_provider_plan)

        mail_unavailable_args = argparse.Namespace(
            **{
                **vars(independent_args),
                "agent_mail_status_json": None,
                "agent_mail_reservations_json": None,
            }
        )
        mail_unavailable_plan = build_autopilot_plan(
            build_autopilot_input_pack(mail_unavailable_args),
            max_items=args.max_items,
        )
        mail_unavailable_provider = {
            item["issue_id"]: item for item in mail_unavailable_plan["work_partitions"]
        }["bd-provider"]
        assert mail_unavailable_plan["status"] == "blocked"
        assert mail_unavailable_provider["confidence"] == "medium"
        assert any(
            "Agent Mail reservation evidence" in caveat
            for caveat in mail_unavailable_provider["degraded_caveats"]
        )
        assert_autopilot_plan_contract(mail_unavailable_plan)

        def build_failure_fixture_plan(
            name: str,
            *,
            cargo_payload: dict[str, Any] | None = None,
            agent_mail_status_payload: dict[str, Any] | None = None,
            beads_payload: dict[str, Any] | None = None,
            commands: list[dict[str, Any]] | None = None,
        ) -> dict[str, Any]:
            fixture_args = argparse.Namespace(
                **{
                    **vars(healthy_args),
                    "cargo_admission_json": write_json(
                        workspace / f"{name}-cargo.json",
                        cargo_payload
                        or json.loads(cargo_admit_path.read_text(encoding="utf-8")),
                    ),
                    "agent_mail_status_json": write_json(
                        workspace / f"{name}-agent-mail-status.json",
                        agent_mail_status_payload
                        or json.loads(agent_mail_ok_path.read_text(encoding="utf-8")),
                    ),
                    "beads_json": write_json(
                        workspace / f"{name}-beads.json",
                        beads_payload
                        or json.loads(open_beads_path.read_text(encoding="utf-8")),
                    ),
                    "capture_manifest": {
                        "schema": RUNPACK_CAPTURE_SCHEMA,
                        "mode": "current",
                        "status": "degraded" if commands else "ok",
                        "generated_at": generated_at,
                        "capture_dir": str(workspace / f"capture-{name}"),
                        "project_root": str(workspace),
                        "generated_source_paths": {},
                        "commands": commands or [],
                    },
                }
            )
            return build_autopilot_plan(
                build_autopilot_input_pack(fixture_args),
                max_items=args.max_items,
            )

        def require_failure_action(plan: dict[str, Any], action_id: str) -> dict[str, Any]:
            actions_by_id = {
                item["id"]: item
                for item in plan["failure_actions"]
                if isinstance(item, dict)
            }
            assert action_id in actions_by_id, sorted(actions_by_id)
            action = actions_by_id[action_id]
            assert action["safe_commands"]
            assert action["escalation"]
            assert action["raw_excerpt"]
            assert_autopilot_plan_contract(plan)
            return action

        rch_retrieval_plan = build_failure_fixture_plan(
            "failure-rch-retrieval",
            commands=[
                {
                    "id": "rch_artifact_sync",
                    "command": "rch exec -- cargo check --all-targets",
                    "status": "failed",
                    "exit_code": 1,
                    "issue": "RCH-E211 artifact retrieval failed: No space left on device while retrieving artifacts",
                }
            ],
        )
        require_failure_action(
            rch_retrieval_plan,
            "FAIL-RCH-ARTIFACT-RETRIEVAL-DISK",
        )

        local_target_plan = build_failure_fixture_plan(
            "failure-local-target-disk",
            cargo_payload={
                **json.loads(cargo_admit_path.read_text(encoding="utf-8")),
                "decision": "degraded",
                "reason": "cargo target/debug failed: No space left on device; set CARGO_TARGET_DIR and TMPDIR",
            },
        )
        require_failure_action(local_target_plan, "FAIL-CARGO-LOCAL-TARGET-DISK")

        remote_compile_plan = build_failure_fixture_plan(
            "failure-rch-remote-compile",
            commands=[
                {
                    "id": "cargo_check",
                    "command": "rch exec -- cargo check --all-targets",
                    "status": "failed",
                    "exit_code": 101,
                    "issue": "[RCH] remote vmi123 worker cargo check failed: error[E0308]: mismatched types",
                }
            ],
        )
        require_failure_action(remote_compile_plan, "FAIL-RCH-REMOTE-COMPILE")

        unknown_rch_plan = build_failure_fixture_plan(
            "failure-rch-unknown",
            commands=[
                {
                    "id": "rch_unknown",
                    "command": "rch exec -- cargo test",
                    "status": "failed",
                    "exit_code": 1,
                    "issue": "[RCH] remote worker failed [RCH-E999] token=super-secret-value new failure mode",
                }
            ],
        )
        unknown_rch_action = require_failure_action(unknown_rch_plan, "FAIL-RCH-UNKNOWN")
        assert "[REDACTED]" in unknown_rch_action["raw_excerpt"]

        agent_mail_schema_action = require_failure_action(
            plan,
            "FAIL-AGENT-MAIL-SCHEMA",
        )
        assert "Agent Mail" in agent_mail_schema_action["title"]

        agent_mail_readonly_plan = build_failure_fixture_plan(
            "failure-agent-mail-readonly",
            agent_mail_status_payload={
                "schema": "pi.agent_mail.robot_status.v1",
                "generated_at": generated_at,
                "status": "degraded_read_only",
                "health_level": "yellow",
                "issue": "archive writes disabled; Agent Mail reservation store is read-only",
            },
        )
        require_failure_action(
            agent_mail_readonly_plan,
            "FAIL-AGENT-MAIL-DEGRADED-READONLY",
        )

        beads_drift_plan = build_failure_fixture_plan(
            "failure-beads-jsonl-drift",
            commands=[
                {
                    "id": "beads_list",
                    "command": "br list --json",
                    "status": "failed",
                    "exit_code": 1,
                    "issue": "beads DB warning: JSONL drift detected; run br doctor",
                }
            ],
        )
        require_failure_action(beads_drift_plan, "FAIL-BEADS-JSONL-DRIFT")

        unknown_plan = build_failure_fixture_plan(
            "failure-unknown-operational",
            commands=[
                {
                    "id": "opaque_tool",
                    "command": "opaque tool",
                    "status": "failed",
                    "exit_code": 42,
                    "issue": "opaque operational failure token=super-secret-value with no known signature",
                }
            ],
        )
        unknown_action = require_failure_action(unknown_plan, "FAIL-UNKNOWN-OPERATIONAL")
        assert unknown_action["match_confidence"] == "low"
        assert "[REDACTED]" in unknown_action["raw_excerpt"]

        if shutil.which("br") is not None:
            real_beads_workspace = workspace / "real-beads-workspace"
            real_beads_workspace.mkdir()

            def run_real_br(*command: str) -> str:
                completed = subprocess.run(
                    ["br", *command],
                    cwd=real_beads_workspace,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if completed.returncode != 0:
                    raise AssertionError(
                        f"br {' '.join(command)} failed: {completed.stderr}"
                    )
                return completed.stdout

            run_real_br("init", "--prefix", "smoke", "--json")
            run_real_br(
                "create",
                "--title",
                "Add OpenAI provider fixture",
                "--type",
                "task",
                "--priority",
                "2",
                "--labels",
                "provider,openai",
                "--description",
                "provider streaming body",
                "--json",
            )
            run_real_br(
                "create",
                "--title",
                "Harden read tool conformance",
                "--type",
                "task",
                "--priority",
                "2",
                "--labels",
                "tools,conformance",
                "--description",
                "read tool body",
                "--json",
            )
            real_beads_path = write_json(
                workspace / "beads-real-smoke.json",
                json.loads(run_real_br("list", "--json")),
            )
            real_ready_path = write_json(
                workspace / "beads-ready-real-smoke.json",
                json.loads(run_real_br("ready", "--json")),
            )
            real_smoke_args = argparse.Namespace(
                **{
                    **vars(healthy_args),
                    "beads_json": real_beads_path,
                    "beads_ready_json": real_ready_path,
                }
            )
            real_smoke_plan = build_autopilot_plan(
                build_autopilot_input_pack(real_smoke_args),
                max_items=args.max_items,
            )
            real_smoke_reservations = [
                tuple(item["suggested_reservation"])
                for item in real_smoke_plan["work_partitions"]
            ]
            assert len(real_smoke_reservations) >= 2
            assert any("src/providers/**/*.rs" in item for item in real_smoke_reservations)
            assert any("src/tools.rs" in item for item in real_smoke_reservations)
            assert_autopilot_plan_contract(real_smoke_plan)
        empty_ready_path = write_json(workspace / "beads-ready-empty.json", [])
        empty_plan_args = argparse.Namespace(
            **{
                **vars(healthy_args),
                "beads_json": empty_beads_path,
                "beads_ready_json": empty_ready_path,
            }
        )
        empty_input_pack = build_autopilot_input_pack(empty_plan_args)
        empty_plan = build_autopilot_plan(empty_input_pack, max_items=args.max_items)
        assert empty_input_pack["status"] == "ready"
        assert empty_plan["actions"][0]["action"] == "run_docs_only_work"
        stale_git_path = write_json(
            workspace / "git-status-stale.json",
            {
                "schema": GIT_CONTEXT_SCHEMA,
                "generated_at": "2026-05-07T09:00:00+00:00",
                "branch": "main",
                "head": "stalefixture",
                "upstream": {"name": "origin/main", "ahead": 0, "behind": 0, "status": "ok"},
                "porcelain_lines": [],
                "recent_commits": [],
                "recent_remote_commits": [],
            },
        )
        stale_input_pack = build_autopilot_input_pack(
            argparse.Namespace(**{**vars(autopilot_args), "git_status_file": stale_git_path})
        )
        assert stale_input_pack["status"] == "degraded"
        assert any(
            item.get("id") == "git_status" and item.get("classification") == "stale"
            for item in stale_input_pack["source_classification"]
        )
        malformed = workspace / "malformed.json"
        malformed.write_text("{not valid json", encoding="utf-8")
        bad_args = argparse.Namespace(**{**vars(args), "doctor_json": malformed})
        try:
            build_runpack(bad_args)
        except RunpackError as exc:
            assert "malformed JSON" in str(exc)
        else:
            raise AssertionError("malformed provided source should fail closed")
        try:
            build_autopilot_input_pack(
                argparse.Namespace(**{**vars(autopilot_args), "doctor_json": malformed})
            )
        except RunpackError as exc:
            assert "malformed JSON" in str(exc)
        else:
            raise AssertionError("malformed autopilot source should fail closed")
        autopilot_e2e = build_autopilot_e2e_summary(
            output_dir=workspace / "autopilot-e2e",
            events_path=workspace / "autopilot-e2e" / "events.jsonl",
            generated_at=generated_at,
            max_items=args.max_items,
            stale_after_hours=args.stale_after_hours,
            timeout_seconds=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        )
        assert autopilot_e2e["schema"] == AUTOPILOT_E2E_SCHEMA
        assert autopilot_e2e["status"] == "pass"
        assert set(autopilot_e2e["scenarios"]) == set(AUTOPILOT_E2E_REQUIRED_SCENARIOS)
        assert autopilot_e2e["scenarios"]["healthy_ready_claim"][
            "selected_action"
        ] == "claim_ready_bead"
        assert autopilot_e2e["scenarios"]["empty_ready_queue"][
            "selected_action"
        ] == "run_docs_only_work"
        assert autopilot_e2e["scenarios"]["degraded_agent_mail_soft_lock"][
            "selected_action"
        ] == "use_beads_soft_lock"
        assert "wait_for_rch" in autopilot_e2e["scenarios"]["saturated_rch_queue"][
            "actions"
        ]
        assert autopilot_e2e["scenarios"]["stale_in_progress_bead"][
            "selected_action"
        ] == "reopen_stale_bead_candidate"
        assert autopilot_e2e["scenarios"]["unrelated_dirty_worktree"][
            "selected_action"
        ] == "capture_handoff"
        assert autopilot_e2e["scenarios"]["malformed_source_fail_closed"][
            "selected_action"
        ] == "fail_closed"
        final_gate_issues = [
            {
                "id": "bd-h3uv0",
                "title": "Swarm autopilot and evidence-driven launch control",
                "status": "open",
            },
            {
                "id": "bd-h3uv0.8",
                "title": "Finalize autopilot decision gate",
                "status": "in_progress",
                "assignee": "GoldenGlacier",
            },
        ]
        final_gate_issues.extend(
            {
                "id": issue_id,
                "status": "closed",
                "close_reason": f"{issue_id} self-test evidence closed",
            }
            for issue_id in AUTOPILOT_DECISION_GATE_CHILD_BEADS
        )
        final_gate_issues_path = workspace / "final-gate-issues.jsonl"
        final_gate_issues_path.write_text(
            "\n".join(json_dumps(issue) for issue in final_gate_issues) + "\n",
            encoding="utf-8",
        )
        final_gate_quality = [
            {
                "id": "py_compile",
                "status": "pass",
                "command": "python3 -m py_compile scripts/build_swarm_operator_runpack.py",
            },
            {
                "id": "runpack_self_test",
                "status": "pass",
                "command": "python3 scripts/build_swarm_operator_runpack.py --self-test",
            },
            {
                "id": "autopilot_e2e",
                "status": "pass",
                "command": (
                    "python3 scripts/build_swarm_operator_runpack.py "
                    "--run-autopilot-e2e"
                ),
            },
            {
                "id": "json_contracts",
                "status": "pass",
                "command": (
                    "python3 -m json.tool "
                    "docs/contracts/swarm-autopilot-decision-gate-contract.json"
                ),
            },
            {
                "id": "cargo_fmt",
                "status": "pass",
                "command": "cargo fmt --check",
            },
            {
                "id": "cargo_check_all_targets_rch",
                "status": "pass",
                "command": (
                    "CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/"
                    "goldenglacier_bd_h3uv0_8/target "
                    "TMPDIR=/data/tmp/pi_agent_rust_cargo/"
                    "goldenglacier_bd_h3uv0_8/tmp "
                    "rch exec -- cargo check --all-targets"
                ),
            },
            {
                "id": "cargo_clippy_all_targets_rch",
                "status": "pass",
                "command": (
                    "CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/"
                    "goldenglacier_bd_h3uv0_8/target "
                    "TMPDIR=/data/tmp/pi_agent_rust_cargo/"
                    "goldenglacier_bd_h3uv0_8/tmp "
                    "rch exec -- cargo clippy --all-targets -- -D warnings"
                ),
            },
            {
                "id": "staged_ubs",
                "status": "pass",
                "command": "timeout 60s ubs --staged --only=rust .",
            },
            {
                "id": "beads_ledger_reconcile",
                "status": "pass",
                "command": "./scripts/reconcile_beads_ledger.sh",
            },
        ]
        final_gate = build_autopilot_decision_gate_summary(
            generated_at=generated_at,
            quality_gate_results=final_gate_quality,
            issue_export_path=final_gate_issues_path,
            git_refs={
                "head": "finalgatefixture",
                "origin_main": "finalgatefixture",
                "origin_master": "finalgatefixture",
            },
        )
        assert final_gate["schema"] == AUTOPILOT_DECISION_GATE_SCHEMA
        assert final_gate["status"] == "pass"
        assert final_gate["decision"] == "close_final_gate_and_parent_epic"
        assert final_gate["follow_up_required"] is False
        assert not final_gate["follow_up_beads"]
        no_tail_args = argparse.Namespace(**{**vars(args), "tail_latency_json": None})
        no_tail_runpack = build_runpack(no_tail_args)
        assert "tail_latency" not in no_tail_runpack
        no_tail_dashboard = no_tail_runpack["bottleneck_attribution"]
        assert "tail_latency" in no_tail_dashboard["missing_optional_diagnostics"]
        assert_runpack_contract(no_tail_runpack)
        no_optional_args = argparse.Namespace(
            **{
                **vars(args),
                "tail_latency_json": None,
                "flight_recorder_report_json": None,
                "host_preflight_json": None,
                "hostcall_swarm_profile_json": None,
                "session_recovery_swarm_profile_json": None,
                "rpc_swarm_e2e_json": None,
                "rch_artifact_sync_json": None,
            }
        )
        no_optional_runpack = build_runpack(no_optional_args)
        assert no_optional_runpack["bottleneck_attribution"]["surface_coverage"][
            "provider_streaming"
        ]["status"] == "optional_diagnostic_missing"
        assert (
            "flight_recorder"
            in no_optional_runpack["bottleneck_attribution"]["missing_optional_diagnostics"]
        )
        assert_runpack_contract(no_optional_runpack)
        clean_git_path = write_json(
            workspace / "git-status-clean.json",
            {
                "schema": GIT_CONTEXT_SCHEMA,
                "generated_at": generated_at,
                "branch": "main",
                "head": "cleanfixture",
                "upstream": {"name": "origin/main", "ahead": 0, "behind": 0, "status": "ok"},
                "porcelain_lines": [],
                "recent_commits": [],
                "recent_remote_commits": [],
            },
        )
        empty_beads_path = write_json(workspace / "beads-empty.json", {"issues": []})
        clean_args = argparse.Namespace(
            **{
                **vars(args),
                "git_status_file": clean_git_path,
                "beads_json": empty_beads_path,
                "validation_outputs": [],
                "capture_manifest": {
                    "schema": RUNPACK_CAPTURE_SCHEMA,
                    "mode": "current",
                    "status": "degraded",
                    "generated_at": generated_at,
                    "capture_dir": str(workspace / "capture-clean"),
                    "project_root": str(workspace),
                    "generated_source_paths": {},
                    "commands": [
                        {
                            "id": "agent_mail_status",
                            "command": "am robot status --format json",
                            "status": "failed",
                            "exit_code": 2,
                            "issue": "corrupt mailbox database",
                        }
                    ],
                },
            }
        )
        clean_runpack = build_runpack(clean_args)
        assert clean_runpack["git_state"]["dirty"] is False
        assert clean_runpack["beads"]["active_count"] == 0
        assert clean_runpack["agent_mail_read_state"]["status"] == "degraded"
        assert clean_runpack["validation_outputs"]["status"] == "not_provided"
        text_git_path = workspace / "git-status-text.txt"
        text_git_path.write_text(" M src/main.rs\n", encoding="utf-8")
        text_git_runpack = build_runpack(
            argparse.Namespace(**{**vars(args), "git_status_file": text_git_path})
        )
        assert text_git_runpack["git_state"]["dirty"] is True
        stale_rpc_path = write_json(
            workspace / "stale-rpc-swarm-e2e.json",
            {
                "schema": RPC_SWARM_E2E_SCHEMA,
                "generated_at": "2026-05-07T09:00:00+00:00",
                "status": "pass",
            },
        )
        stale_args = argparse.Namespace(**{**vars(args), "rpc_swarm_e2e_json": stale_rpc_path})
        stale_runpack = build_runpack(stale_args)
        assert stale_runpack["bottleneck_attribution"]["status"] == "degraded"
        assert (
            "rpc_swarm_e2e"
            in stale_runpack["bottleneck_attribution"]["historical_snapshots"]
        )
        bad_rpc_schema_path = write_json(
            workspace / "bad-rpc-swarm-e2e.json",
            {"schema": "pi.rpc.concurrent_swarm_e2e.v0", "generated_at": generated_at},
        )
        bad_rpc_schema_args = argparse.Namespace(
            **{**vars(args), "rpc_swarm_e2e_json": bad_rpc_schema_path}
        )
        try:
            build_runpack(bad_rpc_schema_args)
        except RunpackError as exc:
            assert "rpc_swarm_e2e source schema mismatch" in str(exc)
        else:
            raise AssertionError("schema-mismatched optional diagnostic should fail closed")
    except (AssertionError, RunpackError) as exc:
        print(f"SELF-TEST FAIL: {type(exc).__name__}: {exc}")
        traceback.print_exception(exc, file=sys.stdout)
        return 2
    print("SELF-TEST PASS")
    print(json_dumps({"workspace": str(workspace), "runpack": runpack}, pretty=True))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--doctor-json",
        type=Path,
        help="JSON from `pi doctor --only swarm --format json`",
    )
    parser.add_argument(
        "--claim-readiness-json",
        type=Path,
        help="JSON from report_swarm_claim_readiness.py",
    )
    parser.add_argument(
        "--smoke-summary-json",
        type=Path,
        help="summary.json from run_swarm_smoke_harness.py",
    )
    parser.add_argument("--activity-digest-json", type=Path, help="pi.swarm.activity_digest.v1 JSON")
    parser.add_argument(
        "--cargo-admission-json",
        type=Path,
        help="JSON or JSONL from cargo_headroom.sh --admit-only",
    )
    parser.add_argument(
        "--beads-json",
        type=Path,
        help="JSON from `br list --json` or `br list --status=in_progress --json`",
    )
    parser.add_argument(
        "--beads-ready-json",
        type=Path,
        help="JSON from `br ready --json` for ready-queue planner recommendations",
    )
    parser.add_argument(
        "--agent-mail-status-json",
        type=Path,
        help="optional Agent Mail robot status JSON for the autopilot input pack",
    )
    parser.add_argument(
        "--agent-mail-reservations-json",
        type=Path,
        help="optional Agent Mail robot reservations JSON for the autopilot input pack",
    )
    parser.add_argument(
        "--git-status-file",
        type=Path,
        help="captured `git status --porcelain` output",
    )
    parser.add_argument(
        "--tail-latency-json",
        type=Path,
        help="pi.operator_tail_latency.v1 JSON from PI_PERF_TELEMETRY",
    )
    parser.add_argument(
        "--flight-recorder-report-json",
        type=Path,
        help="pi.swarm.flight_recorder.report.v1 JSON",
    )
    parser.add_argument(
        "--host-preflight-json",
        type=Path,
        help="pi.doctor.swarm_resource_preflight.v1 JSON",
    )
    parser.add_argument(
        "--hostcall-swarm-profile-json",
        type=Path,
        help="pi.ext.hostcall_admission_swarm_profile.v1 JSON",
    )
    parser.add_argument(
        "--session-recovery-swarm-profile-json",
        type=Path,
        help="pi.session_store_v2.recovery_swarm_profile.v1 JSON",
    )
    parser.add_argument(
        "--rpc-swarm-e2e-json",
        type=Path,
        help="pi.rpc.concurrent_swarm_e2e.v1 JSON",
    )
    parser.add_argument(
        "--rch-artifact-sync-json",
        type=Path,
        help="pi.rch.artifact_sync_preflight.v1 JSON",
    )
    parser.add_argument(
        "--validation-output",
        dest="validation_outputs",
        action="append",
        type=Path,
        default=[],
        help="captured validation log/output to summarize in the handoff bundle",
    )
    parser.add_argument(
        "--operator-runpack-json",
        type=Path,
        help="optional pi.swarm.operator_runpack.v1 JSON to summarize in the autopilot input pack",
    )
    parser.add_argument(
        "--capture-current",
        action="store_true",
        help="capture current git, Beads, Agent Mail, RCH, and safe evidence sources before building",
    )
    parser.add_argument(
        "--capture-dir",
        type=Path,
        help="directory for --capture-current source artifacts; files must not already exist",
    )
    parser.add_argument(
        "--capture-timeout-seconds",
        type=int,
        default=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        help="per-command timeout for --capture-current probes",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path("."),
        help="repository root for --capture-current commands",
    )
    parser.add_argument(
        "--agent-name",
        help="Agent Mail agent name for --capture-current robot reads",
    )
    parser.add_argument("--out-json", type=Path, help="write runpack JSON; refuses to overwrite")
    parser.add_argument("--out-md", type=Path, help="write runpack Markdown; refuses to overwrite")
    parser.add_argument(
        "--out-autopilot-input-pack-json",
        type=Path,
        help="write pi.swarm.autopilot_input_pack.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--out-autopilot-plan-json",
        type=Path,
        help="write pi.swarm.autopilot_plan.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--run-autopilot-e2e",
        action="store_true",
        help="run no-mock swarm autopilot E2E scenarios with JSONL evidence",
    )
    parser.add_argument(
        "--out-autopilot-e2e-json",
        type=Path,
        help="write pi.swarm.autopilot_e2e.v1 summary JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--out-autopilot-e2e-events-jsonl",
        type=Path,
        help="write pi.swarm.autopilot_e2e.event.v1 JSONL; refuses to overwrite",
    )
    parser.add_argument(
        "--print-autopilot-e2e",
        action="store_true",
        help="print the no-mock autopilot E2E summary JSON",
    )
    parser.add_argument(
        "--run-autopilot-final-gate",
        action="store_true",
        help="build the final prompt-to-artifact autopilot decision gate",
    )
    parser.add_argument(
        "--out-autopilot-final-gate-json",
        type=Path,
        help="write pi.swarm.autopilot_decision_gate.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--print-autopilot-final-gate",
        action="store_true",
        help="print the final autopilot decision gate JSON",
    )
    parser.add_argument(
        "--quality-gate-result",
        dest="quality_gate_results",
        action="append",
        default=[],
        help="final-gate evidence in NAME=STATUS:COMMAND format",
    )
    parser.add_argument("--generated-at", help="override generated timestamp for deterministic tests")
    parser.add_argument("--stale-after-hours", type=int, default=DEFAULT_STALE_AFTER_HOURS)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    parser.add_argument("--json", action="store_true", help="print the runpack JSON")
    parser.add_argument(
        "--print-autopilot-input-pack",
        action="store_true",
        help="print the autopilot input pack JSON",
    )
    parser.add_argument(
        "--print-autopilot-plan",
        action="store_true",
        help="print the dry-run autopilot plan JSON",
    )
    parser.add_argument("--self-test", action="store_true", help="run fixture-backed self-test")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        return run_self_test()
    if args.stale_after_hours < 0:
        print("ERROR: --stale-after-hours must be non-negative", file=sys.stderr)
        return 2
    if args.max_items < 0:
        print("ERROR: --max-items must be non-negative", file=sys.stderr)
        return 2
    if args.capture_timeout_seconds <= 0:
        print("ERROR: --capture-timeout-seconds must be positive", file=sys.stderr)
        return 2
    final_gate_options_used = (
        args.out_autopilot_final_gate_json
        or args.print_autopilot_final_gate
        or args.quality_gate_results
    )
    if final_gate_options_used and not args.run_autopilot_final_gate:
        print("ERROR: final-gate options require --run-autopilot-final-gate", file=sys.stderr)
        return 2
    try:
        if args.run_autopilot_final_gate:
            summary = build_autopilot_decision_gate_summary(
                generated_at=args.generated_at or utc_now_iso(),
                quality_gate_results=parse_quality_gate_results(args.quality_gate_results),
            )
            write_autopilot_decision_gate_output(args, summary)
            if (
                args.print_autopilot_final_gate
                or args.out_autopilot_final_gate_json is None
            ):
                print(json_dumps(summary, pretty=True))
            return 0
        if args.run_autopilot_e2e:
            summary = build_autopilot_e2e_summary(
                output_dir=args.capture_dir,
                events_path=args.out_autopilot_e2e_events_jsonl,
                generated_at=args.generated_at or utc_now_iso(),
                max_items=args.max_items,
                stale_after_hours=args.stale_after_hours,
                timeout_seconds=args.capture_timeout_seconds,
            )
            write_autopilot_e2e_output(args, summary)
            if args.print_autopilot_e2e or args.out_autopilot_e2e_json is None:
                print(json_dumps(summary, pretty=True))
            return 0
        capture_current_sources(args)
        runpack = build_runpack(args)
        input_pack: dict[str, Any] | None = None
        plan: dict[str, Any] | None = None
        if (
            args.out_autopilot_input_pack_json
            or args.print_autopilot_input_pack
            or args.out_autopilot_plan_json
            or args.print_autopilot_plan
        ):
            input_pack = build_autopilot_input_pack(args)
            if args.out_autopilot_plan_json or args.print_autopilot_plan:
                plan = build_autopilot_plan(input_pack, max_items=args.max_items)
                runpack["autopilot_handoff"] = build_autopilot_handoff_summary(
                    args,
                    input_pack,
                    plan,
                )
        write_outputs(args, runpack)
        if input_pack is not None:
            write_autopilot_input_pack_output(args, input_pack)
            if args.print_autopilot_input_pack:
                print(json_dumps(input_pack, pretty=True))
        if plan is not None:
            write_autopilot_plan_output(args, plan)
            if args.print_autopilot_plan:
                print(json_dumps(plan, pretty=True))
    except (RunpackError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json or (
        not args.out_json
        and not args.out_md
        and not args.out_autopilot_input_pack_json
        and not args.out_autopilot_plan_json
        and not args.print_autopilot_input_pack
        and not args.print_autopilot_plan
    ):
        print(json_dumps(runpack, pretty=True))
    return 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
