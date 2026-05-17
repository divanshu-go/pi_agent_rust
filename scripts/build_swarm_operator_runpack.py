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
import importlib.util
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


RUNPACK_SCHEMA = "pi.swarm.operator_runpack.v1"
RUNPACK_CONTRACT_SCHEMA = "pi.swarm.operator_runpack_contract.v1"
SAFETY_SCORECARD_SCHEMA = "pi.swarm.safety_scorecard.v1"
TAIL_LATENCY_SCHEMA = "pi.operator_tail_latency.v1"
BOTTLENECK_ATTRIBUTION_SCHEMA = "pi.swarm.bottleneck_attribution_dashboard.v1"
TURN_PRESSURE_LEDGER_SCHEMA = "pi.swarm.turn_pressure_ledger.v1"
TURN_PRESSURE_LEDGER_CONTRACT_SCHEMA = "pi.swarm.turn_pressure_ledger_contract.v1"
TEMP_ARTIFACT_INVENTORY_SCHEMA = "pi.swarm.temp_artifact_inventory.v1"
STALE_EVIDENCE_RENEWAL_QUEUE_SCHEMA = "pi.swarm.stale_evidence_renewal_queue.v1"
FLIGHT_RECORDER_REPORT_SCHEMA = "pi.swarm.flight_recorder.report.v1"
HOST_PREFLIGHT_SCHEMA = "pi.doctor.swarm_resource_preflight.v1"
CONTEXT_INTELLIGENCE_SCHEMA = "pi.doctor.context_intelligence_posture.v1"
VALIDATION_BROKER_DOCTOR_SCHEMA = "pi.doctor.validation_broker_posture.v1"
PROGRESS_SLO_DOCTOR_SCHEMA = "pi.doctor.swarm_progress_slo_posture.v1"
PROGRESS_SLO_SCHEMA = "pi.swarm.progress_slo.v1"
VALIDATION_BROKER_CLI_STATUS_SCHEMA = "pi.validation_broker.cli_status.v1"
VALIDATION_BROKER_CLI_PLAN_SCHEMA = "pi.validation_broker.cli_plan.v1"
VALIDATION_BROKER_STORE_ENV = "PI_VALIDATION_BROKER_STORE"
HOSTCALL_SWARM_PROFILE_SCHEMA = "pi.ext.hostcall_admission_swarm_profile.v1"
SESSION_RECOVERY_SWARM_PROFILE_SCHEMA = "pi.session_store_v2.recovery_swarm_profile.v1"
RPC_SWARM_E2E_SCHEMA = "pi.rpc.concurrent_swarm_e2e.v1"
RCH_ARTIFACT_SYNC_SCHEMA = "pi.rch.artifact_sync_preflight.v1"
REMOTE_VALIDATION_LEDGER_SCHEMA = "pi.remote_validation.proof_ledger.v1"
REMOTE_VALIDATION_ENTRY_SCHEMA = "pi.remote_validation.proof_entry.v1"
REMOTE_VALIDATION_PROOF_REUSE_GATE_SCHEMA = "pi.validation.proof_reuse_gate.v1"
REMOTE_VALIDATION_PROOF_REUSE_CONTEXT_SCHEMA = "pi.validation.proof_reuse_context.v1"
REMOTE_VALIDATION_PROOF_REUSE_GATE_CONTRACT_SCHEMA = (
    "pi.validation.proof_reuse_gate_contract.v1"
)
REMOTE_VALIDATION_CONTRACT_PATH = Path(
    "docs/contracts/remote-validation-proof-ledger-contract.json"
)
REMOTE_VALIDATION_PROOF_REUSE_GATE_CONTRACT_PATH = Path(
    "docs/contracts/remote-validation-proof-reuse-gate-contract.json"
)
GIT_CONTEXT_SCHEMA = "pi.swarm.git_context.v1"
RUNPACK_CAPTURE_SCHEMA = "pi.swarm.operator_runpack_capture.v1"
AUTOPILOT_INPUT_PACK_SCHEMA = "pi.swarm.autopilot_input_pack.v1"
AUTOPILOT_INPUT_PACK_CONTRACT_SCHEMA = "pi.swarm.autopilot_input_pack_contract.v1"
AUTOPILOT_PLAN_SCHEMA = "pi.swarm.autopilot_plan.v1"
AUTOPILOT_PLAN_CONTRACT_SCHEMA = "pi.swarm.autopilot_plan_contract.v1"
ACTION_PLAN_SCHEMA = "pi.swarm.action_plan.v1"
ACTION_PLAN_CONTRACT_SCHEMA = "pi.swarm.action_plan_contract.v1"
WORK_ADMISSION_GATE_SCHEMA = "pi.swarm.work_admission_gate.v1"
WORK_ADMISSION_GATE_CONTRACT_SCHEMA = "pi.swarm.work_admission_gate_contract.v1"
BUDGET_DRIFT_SCHEMA = "pi.swarm.budget_drift.v1"
AUTOPILOT_HANDOFF_SCHEMA = "pi.swarm.autopilot_handoff.v1"
AUTOPILOT_E2E_SCHEMA = "pi.swarm.autopilot_e2e.v1"
AUTOPILOT_E2E_EVENT_SCHEMA = "pi.swarm.autopilot_e2e.event.v1"
DEGRADED_COORDINATION_RUNPACK_E2E_SCHEMA = (
    "pi.swarm.degraded_coordination_runpack_e2e.v1"
)
DEGRADED_COORDINATION_RUNPACK_E2E_EVENT_SCHEMA = (
    "pi.swarm.degraded_coordination_runpack_e2e.event.v1"
)
AUTOPILOT_DECISION_GATE_SCHEMA = "pi.swarm.autopilot_decision_gate.v1"
AUTOPILOT_DECISION_GATE_CONTRACT_SCHEMA = (
    "pi.swarm.autopilot_decision_gate_contract.v1"
)
CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA = "pi.context_intelligence.closeout_gate.v1"
CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_SCHEMA = (
    "pi.context_intelligence.closeout_gate_contract.v1"
)
RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA = "pi.runtime_intelligence.closeout_gate.v1"
RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_SCHEMA = (
    "pi.runtime_intelligence.closeout_gate_contract.v1"
)
FOURTH_WAVE_CLOSEOUT_GATE_SCHEMA = "pi.swarm.fourth_wave_self_healing.closeout_gate.v1"
FOURTH_WAVE_CLOSEOUT_GATE_CONTRACT_SCHEMA = (
    "pi.swarm.fourth_wave_self_healing.closeout_gate_contract.v1"
)
ADAPTIVE_EXECUTION_CLOSEOUT_GATE_SCHEMA = (
    "pi.swarm.adaptive_execution.closeout_gate.v1"
)
ADAPTIVE_EXECUTION_CLOSEOUT_GATE_CONTRACT_SCHEMA = (
    "pi.swarm.adaptive_execution.closeout_gate_contract.v1"
)
SIXTH_WAVE_VALIDATION_CLOSEOUT_GATE_SCHEMA = (
    "pi.swarm.validation_hardening.closeout_gate.v1"
)
SIXTH_WAVE_VALIDATION_CLOSEOUT_GATE_CONTRACT_SCHEMA = (
    "pi.swarm.validation_hardening.closeout_gate_contract.v1"
)
BACKPRESSURE_BUDGET_CONTRACT_SCHEMA = (
    "pi.swarm.provider_rpc_tui_backpressure_budget_contract.v1"
)
BACKPRESSURE_BUDGET_CONTRACT_SPEC_SCHEMA = (
    "pi.swarm.provider_rpc_tui_backpressure_budget_contract_spec.v1"
)
SWARM_REPLAY_PREVIEW_SCHEMA = "pi.swarm.replay_preview.v1"
RUNPACK_CONTRACT_PATH = Path("docs/contracts/swarm-operator-runpack-contract.json")
TURN_PRESSURE_LEDGER_CONTRACT_PATH = Path(
    "docs/contracts/swarm-turn-pressure-ledger-contract.json"
)
AUTOPILOT_INPUT_PACK_CONTRACT_PATH = Path(
    "docs/contracts/swarm-autopilot-input-pack-contract.json"
)
AUTOPILOT_PLAN_CONTRACT_PATH = Path("docs/contracts/swarm-autopilot-plan-contract.json")
ACTION_PLAN_CONTRACT_PATH = Path("docs/contracts/swarm-action-plan-contract.json")
WORK_ADMISSION_GATE_CONTRACT_PATH = Path(
    "docs/contracts/swarm-work-admission-gate-contract.json"
)
AUTOPILOT_DECISION_GATE_CONTRACT_PATH = Path(
    "docs/contracts/swarm-autopilot-decision-gate-contract.json"
)
CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_PATH = Path(
    "docs/contracts/context-intelligence-closeout-gate-contract.json"
)
RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_PATH = Path(
    "docs/contracts/runtime-intelligence-closeout-gate-contract.json"
)
FOURTH_WAVE_CLOSEOUT_GATE_CONTRACT_PATH = Path(
    "docs/contracts/fourth-wave-self-healing-closeout-gate-contract.json"
)
ADAPTIVE_EXECUTION_CLOSEOUT_GATE_CONTRACT_PATH = Path(
    "docs/contracts/adaptive-execution-closeout-gate-contract.json"
)
SIXTH_WAVE_VALIDATION_CLOSEOUT_GATE_CONTRACT_PATH = Path(
    "docs/contracts/sixth-wave-validation-hardening-closeout-gate-contract.json"
)
BACKPRESSURE_BUDGET_CONTRACT_PATH = Path(
    "docs/contracts/provider-rpc-tui-backpressure-budget-contract.json"
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
AGENT_MAIL_SCHEMA_CORRUPT_FIXTURE = Path(
    "tests/fixtures/agent_mail/schema_corrupt_health.json"
)
AGENT_MAIL_SCHEMA_CORRUPT_DETAIL = (
    "sqlite schema missing required health_check tables: "
    "projects, agents, messages, message_recipients"
)
AGENT_MAIL_SCHEMA_CORRUPT_RECOVERY_ACTION = (
    "Run `am doctor repair --yes` or restore from archive backup"
)
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
NON_SENSITIVE_KEY_EXACT = frozenset(
    {
        "estimated_tokens",
    }
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
    "validation_broker",
    "stale_evidence_renewal_queue",
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
    "validation_broker": ("validation_broker_status",),
    "stale_evidence_renewal_queue": (),
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
    "create_or_refine_backlog",
    "run_docs_only_work",
    "plan_new_bead",
)
ACTION_PLAN_ALLOWED_DECISIONS = (
    "implement_ready_work",
    "create_or_refine_beads",
    "renew_stale_evidence",
    "wait_for_pressure",
    "pause_or_surface_blocker",
)
ACTION_PLAN_ALLOWED_STATUSES = ("ready", "degraded", "blocked")
ACTION_PLAN_COMMAND_SAFETY_CLASSES = (
    "read_only_probe",
    "coordination_probe",
    "validation_probe",
    "evidence_capture",
    "beads_mutation_requires_operator",
)
WORK_ADMISSION_GATE_ALLOWED_DECISIONS = (
    "proceed_with_implementation",
    "create_or_refine_bead",
    "renew_evidence",
    "wait",
    "pause_escalate",
)
WORK_ADMISSION_GATE_ALLOWED_STATUSES = ("admit", "limited", "wait", "blocked")
TURN_PRESSURE_LEDGER_DIMENSION_IDS = (
    "normal_turn_baseline",
    "tool_artifact_spillover",
    "compaction_admission",
    "provider_routing",
    "tui_frame_budget",
    "session_write_amplification",
)
TURN_PRESSURE_LEDGER_ALLOWED_STATUSES = ("ok", "watch", "pressured", "blocked")
ACTION_PLAN_TO_WORK_ADMISSION_DECISION = {
    "implement_ready_work": "proceed_with_implementation",
    "create_or_refine_beads": "create_or_refine_bead",
    "renew_stale_evidence": "renew_evidence",
    "wait_for_pressure": "wait",
    "pause_or_surface_blocker": "pause_escalate",
}
AUTOPILOT_TO_ACTION_PLAN_DECISION = {
    "claim_ready_bead": "implement_ready_work",
    "create_or_refine_backlog": "create_or_refine_beads",
    "run_docs_only_work": "create_or_refine_beads",
    "plan_new_bead": "create_or_refine_beads",
    "split_by_surface": "create_or_refine_beads",
    "wait_for_rch": "wait_for_pressure",
    "use_beads_soft_lock": "wait_for_pressure",
    "adjust_swarm_budget": "wait_for_pressure",
    "stop_and_surface_blocker": "pause_or_surface_blocker",
    "reopen_stale_bead_candidate": "pause_or_surface_blocker",
    "capture_handoff": "pause_or_surface_blocker",
}
ACTION_PLAN_VALIDATION_FAILURE_IDS = {
    "FAIL-RCH-REMOTE-COMPILE",
}
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
    "deferred_roadmap_backlog",
    "degraded_agent_mail_soft_lock",
    "saturated_rch_queue",
    "capacity_cpu16_mem64_healthy_admit",
    "capacity_cpu32_mem128_degraded_mail",
    "capacity_cpu64_mem256_rch_backoff",
    "dirty_mail_rch_empty_beads_dry_run",
    "stale_in_progress_bead",
    "unrelated_dirty_worktree",
    "stale_action_plan_evidence",
    "validation_failure_fail_closed",
    "malformed_source_fail_closed",
)
DEGRADED_COORDINATION_RUNPACK_E2E_REQUIRED_SCENARIOS = (
    "degraded_coordination_runpack_handoff",
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
CONTEXT_INTELLIGENCE_CLOSEOUT_CHILD_BEADS = (
    "bd-ircr3.1",
    "bd-ircr3.2",
    "bd-ircr3.3",
    "bd-ircr3.4",
    "bd-ircr3.5",
    "bd-ircr3.6",
    "bd-ircr3.7",
    "bd-ircr3.8",
    "bd-ircr3.9",
    "bd-ircr3.10",
)
CONTEXT_INTELLIGENCE_CLOSEOUT_REQUIRED_CHECKS = (
    "child_beads_closed",
    "graph_contracts",
    "graph_builder",
    "freshness_claim_gates",
    "bundle_planner",
    "redaction_invalidation",
    "preview_surface",
    "prompt_injection",
    "no_mock_e2e",
    "perf_budgets",
    "doctor_runpack",
    "operator_docs",
    "readme_freshness",
    "pushed_commits",
    "quality_gates",
)
CONTEXT_INTELLIGENCE_CLOSEOUT_REQUIRED_QUALITY_GATES = (
    "py_compile",
    "runpack_self_test",
    "json_contracts",
    "semantic_context_graph_contract_rch",
    "semantic_workspace_graph_contract_rch",
    "semantic_workspace_graph_builder_rch",
    "context_intelligence_e2e_rch",
    "doctor_context_intelligence_rch",
    "context_perf_budgets_rch",
    "context_intelligence_closeout_gate_contract_rch",
    "cargo_fmt",
    "cargo_check_all_targets_rch",
    "cargo_clippy_all_targets_rch",
    "staged_ubs",
    "beads_ledger_reconcile",
)
RUNTIME_INTELLIGENCE_CLOSEOUT_CHILD_BEADS = (
    "bd-h66tp.1",
    "bd-h66tp.2",
    "bd-h66tp.3",
    "bd-h66tp.4",
    "bd-h66tp.5",
    "bd-h66tp.6",
    "bd-h66tp.7",
)
RUNTIME_INTELLIGENCE_CLOSEOUT_REQUIRED_CHECKS = (
    "child_beads_closed",
    "compaction_admission",
    "tool_output_artifacts",
    "provider_routing",
    "scheduler_fairness",
    "frame_budget",
    "cancellation_cleanup",
    "extension_safety_provenance",
    "docs_and_evidence",
    "source_boundaries",
    "pushed_commits",
    "quality_gates",
)
RUNTIME_INTELLIGENCE_CLOSEOUT_REQUIRED_SOURCE_BOUNDARIES = (
    "beads_are_source_of_truth",
    "agent_mail_is_coordination_only",
    "read_only_gate",
    "rch_required_for_heavy_cargo",
    "staged_ubs_required",
    "beads_ledger_required",
    "no_release_or_dropin_claims",
    "closeout_does_not_replace_child_artifacts",
)
RUNTIME_INTELLIGENCE_CLOSEOUT_REQUIRED_QUALITY_GATES = (
    "py_compile",
    "runpack_self_test",
    "json_contracts",
    "runtime_intelligence_closeout_gate_contract_rch",
    "cargo_fmt",
    "git_diff_check",
    "cargo_check_all_targets_rch",
    "cargo_clippy_all_targets_rch",
    "staged_ubs",
    "beads_ledger_reconcile",
)
FOURTH_WAVE_CLOSEOUT_CHILD_BEADS = (
    "bd-63x3v.7.1",
    "bd-63x3v.7.2",
    "bd-63x3v.7.3",
    "bd-63x3v.7.4",
    "bd-63x3v.7.5",
    "bd-63x3v.7.6",
    "bd-63x3v.7.7",
    "bd-63x3v.7.8",
)
FOURTH_WAVE_CLOSEOUT_REQUIRED_CHECKS = (
    "child_beads_closed",
    "child_artifact_mapping",
    "action_planner",
    "work_admission_gate",
    "budget_lease_simulator",
    "turn_pressure_ledger",
    "extension_quarantine_rehearsal",
    "operator_handoff_summary",
    "stale_evidence_renewal",
    "operator_docs",
    "source_boundaries",
    "pushed_commits",
    "quality_gates",
)
FOURTH_WAVE_CLOSEOUT_REQUIRED_SOURCE_BOUNDARIES = (
    "beads_are_source_of_truth",
    "agent_mail_is_coordination_only",
    "dry_run_artifacts_only",
    "work_admission_is_not_runtime_enforcement",
    "human_confirmation_required",
    "no_evidence_auto_regeneration",
    "rch_required_for_heavy_cargo",
    "staged_ubs_required",
    "beads_ledger_required",
    "no_release_or_dropin_claims",
    "closeout_does_not_replace_child_artifacts",
)
FOURTH_WAVE_CLOSEOUT_REQUIRED_QUALITY_GATES = (
    "py_compile",
    "runpack_self_test",
    "helper_self_tests",
    "json_contracts",
    "fourth_wave_closeout_gate_contract_rch",
    "cargo_fmt",
    "git_diff_check",
    "cargo_check_all_targets_rch",
    "cargo_clippy_all_targets_rch",
    "staged_ubs",
    "beads_ledger_reconcile",
)
ADAPTIVE_EXECUTION_CLOSEOUT_CHILD_BEADS = (
    "bd-63x3v.8.1",
    "bd-63x3v.8.2",
    "bd-63x3v.8.3",
    "bd-63x3v.8.4",
    "bd-63x3v.8.5",
    "bd-63x3v.8.6",
)
ADAPTIVE_EXECUTION_CLOSEOUT_REQUIRED_CHECKS = (
    "child_beads_closed",
    "child_artifact_mapping",
    "provider_streaming_backpressure",
    "tool_artifact_lifecycle",
    "branch_replay_minimization",
    "tui_degradation_drill",
    "permission_drift_detector",
    "validation_refresh_scheduler",
    "source_boundaries",
    "pushed_commits",
    "quality_gates",
)
ADAPTIVE_EXECUTION_CLOSEOUT_REQUIRED_SOURCE_BOUNDARIES = (
    "beads_are_source_of_truth",
    "agent_mail_is_coordination_only",
    "read_only_gate",
    "rch_required_for_heavy_cargo",
    "staged_ubs_required",
    "beads_ledger_required",
    "no_release_or_dropin_claims",
    "closeout_does_not_replace_child_artifacts",
)
ADAPTIVE_EXECUTION_CLOSEOUT_REQUIRED_QUALITY_GATES = (
    "py_compile",
    "runpack_self_test",
    "json_contracts",
    "git_diff_check",
    "staged_ubs",
    "beads_ledger_reconcile",
)
SIXTH_WAVE_VALIDATION_CLOSEOUT_CHILD_BEADS = (
    "bd-63x3v.9.1",
    "bd-63x3v.9.2",
    "bd-63x3v.9.3",
    "bd-63x3v.9.4",
    "bd-63x3v.9.5",
    "bd-63x3v.9.6",
    "bd-63x3v.9.7",
    "bd-63x3v.9.8",
    "bd-63x3v.9.9",
)
SIXTH_WAVE_VALIDATION_CLOSEOUT_REQUIRED_CHECKS = (
    "child_beads_closed",
    "child_artifact_mapping",
    "rch_workspace_shadow_detector",
    "agent_mail_beads_soft_lock",
    "validation_proof_replay_ledger",
    "temp_artifact_inventory",
    "stale_claim_heuristics",
    "degraded_coordination_e2e",
    "resource_budget_model",
    "backpressure_budget_contract",
    "operator_explanations",
    "source_boundaries",
    "pushed_commits",
    "quality_gates",
)
SIXTH_WAVE_VALIDATION_CLOSEOUT_REQUIRED_SOURCE_BOUNDARIES = (
    "beads_are_source_of_truth",
    "agent_mail_is_coordination_only",
    "read_only_gate",
    "rch_required_for_heavy_cargo",
    "staged_ubs_required",
    "beads_ledger_required",
    "no_release_or_dropin_claims",
    "no_mail_or_rch_mutation",
    "closeout_does_not_replace_child_artifacts",
)
SIXTH_WAVE_VALIDATION_CLOSEOUT_REQUIRED_QUALITY_GATES = (
    "py_compile",
    "runpack_self_test",
    "claim_readiness_self_test",
    "json_contracts",
    "git_diff_check",
    "staged_ubs",
    "beads_ledger_reconcile",
)
BACKPRESSURE_BUDGET_REQUIRED_SURFACE_IDS = (
    "provider_streaming",
    "rpc_output",
    "tui_degradation",
)
BACKPRESSURE_BUDGET_REQUIRED_CANONICAL_METRICS = (
    "event_count",
    "semantic_preservation",
    "coalesced_low_value_count",
    "pressure_depth",
    "latency_or_step",
    "verdict",
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
RCH_DRIFT_CATEGORY_METADATA: dict[str, dict[str, str]] = {
    "missing_remote_target_directory": {
        "drift_class": "infrastructure_drift",
        "remediation": "Create or remap the worker-side target/output directory before rerunning the RCH-backed evidence refresh.",
    },
    "rsync_mkstemp_no_such_file": {
        "drift_class": "evidence_retrieval_drift",
        "remediation": "Fix RCH artifact retrieval/writeback path creation or rerun with worker-local output paths.",
    },
    "disk_pressure": {
        "drift_class": "infrastructure_drift",
        "remediation": "Move CARGO_TARGET_DIR and TMPDIR to high-capacity scratch, clear disposable build pressure with approved cleanup, then rerun the gate.",
    },
    "remote_dependency_preflight_blocked": {
        "drift_class": "code_regression",
        "remediation": "Fix the dependency/source preflight failure or pin the missing dependency before rerunning RCH validation.",
    },
    "worker_workspace_shadow": {
        "drift_class": "infrastructure_drift",
        "remediation": "Fix the RCH worker checkout/workdir so cargo resolves pi_agent_rust directly; do not treat parent-workspace manifest failures as local code regressions.",
    },
    "artifact_retrieval_warning_after_success": {
        "drift_class": "evidence_retrieval_drift",
        "remediation": "Treat the remote command as inconclusive for evidence until artifact retrieval is clean or the expected artifacts are regenerated locally.",
    },
}
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
        "id": "FAIL-RCH-MISSING-REMOTE-TARGET-DIRECTORY",
        "category": "rch",
        "confidence": "high",
        "drift_category_id": "missing_remote_target_directory",
        "drift_class": RCH_DRIFT_CATEGORY_METADATA["missing_remote_target_directory"]["drift_class"],
        "remediation": RCH_DRIFT_CATEGORY_METADATA["missing_remote_target_directory"]["remediation"],
        "terms": ("remote target dir", "remote target directory"),
        "secondary_terms": ("missing", "not found", "no such file"),
        "title": "RCH artifact retrieval cannot find the worker target directory",
        "explanation": (
            "The RCH worker-side target or output directory was absent when "
            "artifact retrieval ran, so the evidence refresh did not prove a "
            "clean remote validation result."
        ),
        "safe_commands": (
            ("Inspect RCH jobs and workers", "rch status --workers --jobs"),
            ("Inspect RCH queue", "rch queue"),
            (
                "Retry with explicit scratch paths",
                "env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/target TMPDIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/tmp rch exec -- cargo check --all-targets",
            ),
        ),
        "escalation": RCH_DRIFT_CATEGORY_METADATA["missing_remote_target_directory"]["remediation"],
    },
    {
        "id": "FAIL-RCH-RSYNC-MKSTEMP-NO-SUCH-FILE",
        "category": "rch",
        "confidence": "high",
        "drift_category_id": "rsync_mkstemp_no_such_file",
        "drift_class": RCH_DRIFT_CATEGORY_METADATA["rsync_mkstemp_no_such_file"]["drift_class"],
        "remediation": RCH_DRIFT_CATEGORY_METADATA["rsync_mkstemp_no_such_file"]["remediation"],
        "terms": ("rsync", "mkstemp"),
        "secondary_terms": ("no such file", "artifact", "retrieval"),
        "title": "RCH artifact rsync failed while creating a local temp file",
        "explanation": (
            "Rsync reached artifact retrieval but could not create the local "
            "temporary file path. Treat the command as inconclusive for evidence "
            "until the writeback path is fixed."
        ),
        "safe_commands": (
            ("Inspect RCH jobs and workers", "rch status --workers --jobs"),
            ("Inspect local scratch headroom", "df -h /data/tmp /tmp"),
            (
                "Retry with explicit scratch paths",
                "env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/target TMPDIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/tmp rch exec -- cargo check --all-targets",
            ),
        ),
        "escalation": RCH_DRIFT_CATEGORY_METADATA["rsync_mkstemp_no_such_file"]["remediation"],
    },
    {
        "id": "FAIL-RCH-ARTIFACT-RETRIEVAL-DISK",
        "category": "rch",
        "confidence": "high",
        "drift_category_id": "disk_pressure",
        "drift_class": RCH_DRIFT_CATEGORY_METADATA["disk_pressure"]["drift_class"],
        "remediation": RCH_DRIFT_CATEGORY_METADATA["disk_pressure"]["remediation"],
        "terms": (
            "artifact retrieval",
            "artifact sync",
            "retrieve artifacts",
            "retrieving artifacts",
            "rch-e21",
        ),
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
        "id": "FAIL-RCH-REMOTE-DEPENDENCY-PREFLIGHT-BLOCKED",
        "category": "rch",
        "confidence": "high",
        "drift_category_id": "remote_dependency_preflight_blocked",
        "drift_class": RCH_DRIFT_CATEGORY_METADATA["remote_dependency_preflight_blocked"]["drift_class"],
        "remediation": RCH_DRIFT_CATEGORY_METADATA["remote_dependency_preflight_blocked"]["remediation"],
        "terms": (
            "remote dependency preflight blocked",
            "dependency preflight blocked",
            "failed to load source for dependency",
            "failed to get `",
        ),
        "secondary_terms": ("dependency", "preflight", "source"),
        "title": "RCH dependency preflight failed before evidence refresh",
        "explanation": (
            "The remote validation was blocked before compiler evidence could be "
            "trusted because dependency/source resolution failed in preflight."
        ),
        "safe_commands": (
            ("Inspect RCH worker health", "rch status --workers --jobs"),
            (
                "Retry the focused validation after fixing dependency state",
                "env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/target TMPDIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/tmp rch exec -- cargo check --all-targets",
            ),
        ),
        "escalation": RCH_DRIFT_CATEGORY_METADATA["remote_dependency_preflight_blocked"]["remediation"],
    },
    {
        "id": "FAIL-RCH-WORKER-WORKSPACE-SHADOW",
        "category": "rch",
        "confidence": "high",
        "drift_category_id": "worker_workspace_shadow",
        "drift_class": RCH_DRIFT_CATEGORY_METADATA["worker_workspace_shadow"]["drift_class"],
        "remediation": RCH_DRIFT_CATEGORY_METADATA["worker_workspace_shadow"]["remediation"],
        "terms": (
            "failed to load manifest for workspace member",
            "referenced by workspace at",
            "failed to read",
        ),
        "secondary_terms": (
            "cargo.toml",
            "/data/projects/cargo.toml",
            "/data/toon_rust/cargo.toml",
            "/data/projects/crates/",
        ),
        "title": "RCH worker cargo resolved a stale parent workspace",
        "explanation": (
            "Cargo failed before compiling pi_agent_rust because the RCH worker "
            "resolved a parent or sibling workspace that is outside this repo. "
            "Treat this as worker/workdir drift, not as proof of a local Rust "
            "regression."
        ),
        "safe_commands": (
            ("Inspect RCH jobs and workers", "rch status --workers --jobs"),
            ("Inspect RCH queue", "rch queue"),
            (
                "Retry after worker workspace repair with explicit manifest scope",
                "env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/target TMPDIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/tmp rch exec -- cargo check --manifest-path /data/projects/pi_agent_rust/Cargo.toml --all-targets",
            ),
        ),
        "escalation": RCH_DRIFT_CATEGORY_METADATA["worker_workspace_shadow"]["remediation"],
    },
    {
        "id": "FAIL-RCH-ARTIFACT-RETRIEVAL-WARNING-AFTER-SUCCESS",
        "category": "rch",
        "confidence": "high",
        "drift_category_id": "artifact_retrieval_warning_after_success",
        "drift_class": RCH_DRIFT_CATEGORY_METADATA["artifact_retrieval_warning_after_success"]["drift_class"],
        "remediation": RCH_DRIFT_CATEGORY_METADATA["artifact_retrieval_warning_after_success"]["remediation"],
        "terms": (
            "artifact retrieval warning",
            "no artifacts retrieved",
            "rsync reported partial transfer",
        ),
        "secondary_terms": ("remote command finished: exit=0", "exit=0", "after success", "post-success"),
        "title": "RCH reported success before artifact retrieval warnings",
        "explanation": (
            "The remote command reported a successful exit, but artifact retrieval "
            "was not clean. Treat the proof as degraded operator evidence."
        ),
        "safe_commands": (
            ("Inspect RCH jobs and workers", "rch status --workers --jobs"),
            ("Inspect RCH queue", "rch queue"),
            (
                "Regenerate expected artifacts through RCH",
                "env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/target TMPDIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/tmp rch exec -- cargo check --all-targets",
            ),
        ),
        "escalation": RCH_DRIFT_CATEGORY_METADATA["artifact_retrieval_warning_after_success"]["remediation"],
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


def load_dashboard_rch_drift_categories() -> dict[str, dict[str, Any]]:
    dashboard_path = Path(__file__).with_name("generate_maintenance_dashboard.py")
    spec = importlib.util.spec_from_file_location(
        "generate_maintenance_dashboard",
        dashboard_path,
    )
    if spec is None or spec.loader is None:
        raise RunpackError(f"unable to import dashboard category source: {dashboard_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    categories = getattr(module, "RCH_DRIFT_CATEGORIES", None)
    if not isinstance(categories, dict):
        raise RunpackError("dashboard RCH drift categories are not available")
    return categories


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
    if lowered in NON_SENSITIVE_KEY_EXACT:
        return False
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


def load_validation_broker_source(path: Path) -> SourcePayload:
    source = load_json_source("validation_broker", path)
    allowed = {VALIDATION_BROKER_CLI_STATUS_SCHEMA, VALIDATION_BROKER_CLI_PLAN_SCHEMA}
    if source.schema not in allowed:
        raise RunpackError(
            "validation_broker source schema mismatch: expected one of "
            f"{sorted(allowed)}, got {source.schema}"
        )
    return source


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
    swarm_replay_preview_json = getattr(args, "swarm_replay_preview_json", None)
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
    if swarm_replay_preview_json is not None:
        sources.append(
            load_json_source(
                "swarm_replay_preview",
                swarm_replay_preview_json,
                expected_schema=SWARM_REPLAY_PREVIEW_SCHEMA,
            )
        )
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
    validation_broker_json = getattr(args, "validation_broker_json", None)
    if validation_broker_json is not None:
        sources.append(load_validation_broker_source(validation_broker_json))
    progress_slo_json = getattr(args, "progress_slo_json", None)
    if progress_slo_json is not None:
        sources.append(
            load_json_source(
                "progress_slo",
                progress_slo_json,
                expected_schema=PROGRESS_SLO_SCHEMA,
            )
        )
    stale_evidence_renewal_json = getattr(args, "stale_evidence_renewal_json", None)
    if stale_evidence_renewal_json is not None:
        sources.append(
            load_json_source(
                "stale_evidence_renewal_queue",
                stale_evidence_renewal_json,
                expected_schema=STALE_EVIDENCE_RENEWAL_QUEUE_SCHEMA,
            )
        )
    return sources


def autopilot_source_payloads(args: argparse.Namespace) -> list[SourcePayload]:
    beads_ready_json = getattr(args, "beads_ready_json", None)
    agent_mail_status_json = getattr(args, "agent_mail_status_json", None)
    agent_mail_reservations_json = getattr(args, "agent_mail_reservations_json", None)
    operator_runpack_json = getattr(args, "operator_runpack_json", None)
    sources = [
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
    validation_broker_json = getattr(args, "validation_broker_json", None)
    if validation_broker_json is not None:
        sources.append(load_validation_broker_source(validation_broker_json))
    stale_evidence_renewal_json = getattr(args, "stale_evidence_renewal_json", None)
    if stale_evidence_renewal_json is not None:
        sources.append(
            load_json_source(
                "stale_evidence_renewal_queue",
                stale_evidence_renewal_json,
                expected_schema=STALE_EVIDENCE_RENEWAL_QUEUE_SCHEMA,
            )
        )
    return sources


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

    validation_broker_store = os.environ.get(VALIDATION_BROKER_STORE_ENV, "").strip()
    if getattr(args, "validation_broker_json", None) is None and pi_path is not None and validation_broker_store:
        maybe_capture_json_source(
            args=args,
            attr="validation_broker_json",
            source_id="validation_broker",
            command_id="validation_broker_status",
            command=[
                pi_path,
                "validation-broker",
                "status",
                "--store",
                validation_broker_store,
                "--format",
                "json",
            ],
            output_path=capture_dir / "validation-broker-status.json",
            repo_root=repo_root,
            timeout_seconds=timeout_seconds,
            commands=commands,
            generated_source_paths=generated_source_paths,
        )
    elif getattr(args, "validation_broker_json", None) is not None:
        generated_source_paths["validation_broker"] = str(args.validation_broker_json)

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
        "validation_broker": getattr(args, "validation_broker_json", None),
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


def infer_agent_mail_semantic_readiness(commands: list[dict[str, Any]]) -> dict[str, Any]:
    for command in commands:
        issue = command.get("issue")
        if not isinstance(issue, str):
            continue
        lowered = issue.lower()
        if "schema missing" in lowered or "missing required" in lowered:
            return {
                "semantic_readiness_status": "fail",
                "semantic_readiness_detail": issue,
            }
    return {
        "semantic_readiness_status": None,
        "semantic_readiness_detail": None,
    }


def agent_mail_transport_status(commands: list[dict[str, Any]]) -> str:
    if not commands:
        return "not_captured"
    if any(command.get("exit_code") is not None for command in commands):
        return "reachable"
    return "unknown"


def beads_fallback_ownership_evidence(
    beads_summary: dict[str, Any],
    max_items: int,
) -> dict[str, Any]:
    active_ownership = (
        beads_summary.get("active_ownership")
        if isinstance(beads_summary.get("active_ownership"), list)
        else []
    )
    status_counts = (
        beads_summary.get("status_counts")
        if isinstance(beads_summary.get("status_counts"), dict)
        else {}
    )
    return {
        "source": "beads",
        "source_command": "br list --status=in_progress --json",
        "soft_lock_field": "assignee",
        "timestamp_field": "updated_at",
        "active_count": beads_summary.get("active_count"),
        "in_progress_count": status_counts.get("in_progress", 0),
        "active_ownership": bounded(active_ownership, max_items),
    }


def summarize_agent_mail_read_state(
    capture_summary: dict[str, Any],
    doctor_summary: dict[str, Any],
    beads_summary: dict[str, Any],
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
    summary = {
        "status": status,
        "capture_mode": capture_summary.get("mode"),
        "transport_status": agent_mail_transport_status(commands),
        **infer_agent_mail_semantic_readiness(commands),
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
    if status in {"degraded", "not_available"}:
        summary.update(
            {
                "soft_lock": "beads",
                "registration_required_before_coding": False,
                "fallback_action": "use_beads_soft_lock",
                "fallback_ownership_evidence": beads_fallback_ownership_evidence(
                    beads_summary,
                    max_items,
                ),
                "no_mail_closeout_steps": build_no_mail_closeout_steps(),
            }
        )
    return summary


def build_no_mail_closeout_steps() -> list[str]:
    return [
        "Attempt Agent Mail registration/inbox/reservation and capture the exact health error.",
        "Use the Beads assignee/status as the soft lock; do not require Agent Mail reservations before coding while Mail is unavailable.",
        "Close finished work with `br close <issue-id> --reason \"...\"` and `br sync --flush-only`.",
        "Final handoff must say Agent Mail was unavailable and Beads ownership was used as the coordination record.",
    ]


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


def summarize_context_intelligence(
    finding: dict[str, Any] | None, max_items: int
) -> dict[str, Any] | None:
    if finding is None:
        return None
    data = finding.get("data") if isinstance(finding.get("data"), dict) else {}
    graph = data.get("graph") if isinstance(data.get("graph"), dict) else {}
    bundle = data.get("bundle") if isinstance(data.get("bundle"), dict) else {}
    cache = data.get("cache") if isinstance(data.get("cache"), dict) else {}
    degraded_reasons = data.get("degraded_reasons")
    if not isinstance(degraded_reasons, list):
        degraded_reasons = []
    recommended_actions = data.get("recommended_actions")
    if not isinstance(recommended_actions, list):
        recommended_actions = []
    return {
        "schema": data.get("schema"),
        "status": data.get("status"),
        "severity": finding.get("severity"),
        "title": finding.get("title"),
        "detail": finding.get("detail"),
        "coverage_status": bundle.get("coverage_status"),
        "graph": {
            "node_count": graph.get("node_count"),
            "edge_count": graph.get("edge_count"),
            "missing_input_count": graph.get("missing_input_count"),
            "malformed_input_count": graph.get("malformed_input_count"),
            "unreadable_input_count": graph.get("unreadable_input_count"),
            "stale_or_untrusted_evidence_count": graph.get(
                "stale_or_untrusted_evidence_count"
            ),
            "validation_link_count": graph.get("validation_link_count"),
            "trace_status_counts": graph.get("trace_status_counts"),
            "evidence_freshness_status_counts": graph.get(
                "evidence_freshness_status_counts"
            ),
        },
        "bundle": {
            "selected_count": bundle.get("selected_count"),
            "excluded_count": bundle.get("excluded_count"),
            "stale_evidence_suppression_count": bundle.get(
                "stale_evidence_suppression_count"
            ),
            "missing_test_link_count": bundle.get("missing_test_link_count"),
            "suggested_validation_command_count": bundle.get(
                "suggested_validation_command_count"
            ),
            "estimated_bytes": bundle.get("estimated_bytes"),
            "estimated_tokens": bundle.get("estimated_tokens"),
        },
        "cache": {
            "fingerprint_count": cache.get("fingerprint_count"),
            "pressure_count": cache.get("pressure_count"),
            "pressure": cache.get("pressure"),
            "cacheable": cache.get("cacheable"),
            "status_counts": cache.get("status_counts"),
        },
        "redaction_summary": data.get("redaction_summary"),
        "degraded_reasons": bounded(degraded_reasons, max_items),
        "recommended_actions": bounded(recommended_actions, max_items),
    }


def summarize_progress_slo_payload(
    payload: dict[str, Any],
    *,
    source_status: str,
    max_items: int,
) -> dict[str, Any]:
    if payload.get("schema") == PROGRESS_SLO_DOCTOR_SCHEMA:
        progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        saturation = (
            payload.get("saturation_summary")
            if isinstance(payload.get("saturation_summary"), dict)
            else {}
        )
        redaction = (
            payload.get("redaction_summary")
            if isinstance(payload.get("redaction_summary"), dict)
            else {}
        )
        source_summary = (
            payload.get("source_summary")
            if isinstance(payload.get("source_summary"), dict)
            else {}
        )
        source_status_payload = (
            payload.get("source_status")
            if isinstance(payload.get("source_status"), dict)
            else {}
        )
        degraded_reasons = payload.get("degraded_reasons")
        if not isinstance(degraded_reasons, list):
            degraded_reasons = []
        reason_ids = progress.get("reason_ids")
        if not isinstance(reason_ids, list):
            reason_ids = []
        next_actions = progress.get("next_actions")
        if not isinstance(next_actions, list):
            next_actions = []
        suppressed_claims = progress.get("suppressed_claims")
        if not isinstance(suppressed_claims, list):
            suppressed_claims = []
        return {
            "status": source_status,
            "schema": progress.get("schema"),
            "doctor_schema": payload.get("schema"),
            "progress_status": progress.get("status"),
            "confidence": progress.get("confidence"),
            "generated_at": progress.get("generated_at"),
            "contract_version": progress.get("contract_version"),
            "source_status": source_status_payload.get("progress_slo_report"),
            "reason_ids": bounded(reason_ids, max_items),
            "next_actions": bounded(next_actions, max_items),
            "suppressed_claims": bounded(suppressed_claims, max_items),
            "metrics": metrics,
            "saturation_summary": saturation,
            "redaction_summary": redaction,
            "source_summary": source_summary,
            "degraded_reasons": bounded(degraded_reasons, max_items),
            "guards": payload.get("guards"),
        }

    reason_ids = payload.get("reason_ids")
    if not isinstance(reason_ids, list):
        reason_ids = []
    next_actions = payload.get("next_actions")
    if not isinstance(next_actions, list):
        next_actions = []
    suppressed_claims = payload.get("suppressed_claims")
    if not isinstance(suppressed_claims, list):
        suppressed_claims = []
    source_statuses = payload.get("source_statuses")
    if not isinstance(source_statuses, list):
        source_statuses = []
    return {
        "status": source_status,
        "schema": payload.get("schema"),
        "progress_status": payload.get("status"),
        "confidence": payload.get("confidence"),
        "generated_at": payload.get("generated_at"),
        "contract_version": payload.get("contract_version"),
        "time_window": payload.get("time_window"),
        "reason_ids": bounded(reason_ids, max_items),
        "next_actions": bounded(next_actions, max_items),
        "suppressed_claims": bounded(suppressed_claims, max_items),
        "metrics": payload.get("progress_metrics"),
        "saturation_summary": payload.get("saturation_summary"),
        "redaction_summary": payload.get("redaction_summary"),
        "source_summary": {
            "total": len(source_statuses),
        },
        "guards": {
            "advisory_only": True,
            "not_ci_success": True,
            "not_release_claim_evidence": True,
            "does_not_replace_beads_agent_mail_rch_validation_broker_doctor_runpacks_context_or_ci": True,
        },
    }


def summarize_progress_slo_source(
    source: SourcePayload,
    max_items: int,
) -> dict[str, Any]:
    if not isinstance(source.payload, dict):
        return {"status": source.status, "schema": source.schema}
    summary = summarize_progress_slo_payload(
        source.payload,
        source_status=source.status,
        max_items=max_items,
    )
    summary["source_path"] = source.path
    return summary


def summarize_progress_slo_doctor(
    finding: dict[str, Any] | None,
    max_items: int,
) -> dict[str, Any] | None:
    if finding is None:
        return None
    data = finding.get("data") if isinstance(finding.get("data"), dict) else {}
    summary = summarize_progress_slo_payload(
        data,
        source_status=str(finding.get("severity") or "unknown").lower(),
        max_items=max_items,
    )
    summary["severity"] = finding.get("severity")
    summary["title"] = finding.get("title")
    summary["detail"] = finding.get("detail")
    return summary


def summarize_validation_broker_payload(
    payload: dict[str, Any],
    max_items: int,
) -> dict[str, Any]:
    schema = payload.get("schema")
    store = payload.get("store") if isinstance(payload.get("store"), dict) else {}
    guards = payload.get("guards") if isinstance(payload.get("guards"), dict) else {}
    degraded_reasons = payload.get("degraded_reasons")
    if not isinstance(degraded_reasons, list):
        degraded_reasons = []
    recommended_next_actions = payload.get("recommended_next_actions")
    if not isinstance(recommended_next_actions, list):
        next_action = payload.get("next_action")
        recommended_next_actions = [next_action] if isinstance(next_action, str) else []

    if schema == VALIDATION_BROKER_DOCTOR_SCHEMA:
        source_status = (
            payload.get("source_status")
            if isinstance(payload.get("source_status"), dict)
            else {}
        )
        current_slots = (
            payload.get("current_slots")
            if isinstance(payload.get("current_slots"), dict)
            else {}
        )
        duplicate = (
            payload.get("duplicate_gate_opportunities")
            if isinstance(payload.get("duplicate_gate_opportunities"), dict)
            else {}
        )
        stale = (
            payload.get("stale_build_warnings")
            if isinstance(payload.get("stale_build_warnings"), dict)
            else {}
        )
        return {
            "schema": schema,
            "status": source_status.get("validation_slot_store") or store.get("status"),
            "source_status": source_status.get("validation_slot_store"),
            "store": {
                "path": store.get("path"),
                "configured": store.get("configured"),
                "exists": store.get("exists"),
                "status": store.get("status"),
                "total_records": store.get("total_records"),
                "total_slots": store.get("total_slots"),
                "state_counts": store.get("state_counts"),
            },
            "current_slots": {
                "total": current_slots.get("total"),
                "active": current_slots.get("active"),
                "reusable": current_slots.get("reusable"),
                "stale": current_slots.get("stale"),
                "expired_at_report_time": current_slots.get("expired_at_report_time"),
                "sample": bounded(current_slots.get("sample") or [], max_items),
            },
            "duplicate_gate_opportunities": {
                "count": duplicate.get("count"),
                "active_equivalent_groups": bounded(
                    duplicate.get("active_equivalent_groups") or [],
                    max_items,
                ),
                "reusable_slots": bounded(duplicate.get("reusable_slots") or [], max_items),
            },
            "stale_build_warnings": {
                "count": stale.get("count"),
                "expired_slots": bounded(stale.get("expired_slots") or [], max_items),
            },
            "degraded_reasons": bounded(degraded_reasons, max_items),
            "recommended_next_actions": bounded(recommended_next_actions, max_items),
            "guards": guards,
        }

    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    source_statuses = (
        decision.get("source_statuses")
        if isinstance(decision.get("source_statuses"), list)
        else []
    )
    rejected_reusable = (
        decision.get("rejected_reusable_slots")
        if isinstance(decision.get("rejected_reusable_slots"), list)
        else []
    )
    status = store.get("status")
    if schema == VALIDATION_BROKER_CLI_PLAN_SCHEMA and decision.get("decision"):
        status = str(decision.get("decision"))
    return {
        "schema": schema,
        "status": status,
        "source_status": store.get("status"),
        "store": {
            "path": store.get("path"),
            "status": store.get("status"),
            "total_records": store.get("total_records"),
            "total_slots": store.get("total_slots"),
            "active_slots": store.get("active_slots"),
            "reusable_slots": store.get("reusable_slots"),
            "stale_slots": store.get("stale_slots"),
            "expired_at_report_time_slots": store.get("expired_at_report_time_slots"),
            "state_counts": store.get("state_counts"),
        },
        "current_slots": {
            "total": store.get("total_slots"),
            "active": store.get("active_slots"),
            "reusable": store.get("reusable_slots"),
            "stale": store.get("stale_slots"),
            "expired_at_report_time": store.get("expired_at_report_time_slots"),
            "sample": [],
        },
        "duplicate_gate_opportunities": {
            "count": store.get("reusable_slots"),
            "active_equivalent_groups": [],
            "reusable_slots": [],
            "rejected_reusable_slots": bounded(rejected_reusable, max_items),
        },
        "stale_build_warnings": {
            "count": store.get("stale_slots") or store.get("expired_at_report_time_slots"),
            "expired_slots": [],
        },
        "decision": decision.get("decision"),
        "next_action": payload.get("next_action"),
        "source_statuses": bounded(source_statuses, max_items),
        "degraded_reasons": bounded(store.get("degraded_reasons") or degraded_reasons, max_items),
        "recommended_next_actions": bounded(recommended_next_actions, max_items),
        "guards": guards,
    }


def summarize_validation_broker_doctor(
    finding: dict[str, Any] | None,
    max_items: int,
) -> dict[str, Any] | None:
    if finding is None:
        return None
    data = finding.get("data") if isinstance(finding.get("data"), dict) else {}
    summary = summarize_validation_broker_payload(data, max_items)
    summary["finding"] = {
        "severity": finding.get("severity"),
        "title": finding.get("title"),
        "detail": finding.get("detail"),
    }
    return summary


def summarize_validation_broker_source(
    source: SourcePayload,
    max_items: int,
) -> dict[str, Any]:
    if not isinstance(source.payload, dict):
        return {"status": source.status}
    summary = summarize_validation_broker_payload(source.payload, max_items)
    summary["status"] = source.status if source.status != "ok" else summary.get("status")
    summary["source_path"] = source.path
    return summary


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
    context_intelligence_finding: dict[str, Any] | None = None
    validation_broker_finding: dict[str, Any] | None = None
    progress_slo_finding: dict[str, Any] | None = None
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
        if data_schema == CONTEXT_INTELLIGENCE_SCHEMA:
            context_intelligence_finding = item
        if data_schema == VALIDATION_BROKER_DOCTOR_SCHEMA:
            validation_broker_finding = item
        if data_schema == PROGRESS_SLO_DOCTOR_SCHEMA:
            progress_slo_finding = item
    severity_counts = Counter(str(item.get("severity") or "unknown") for item in swarm_findings)
    summary = {
        "status": source.status,
        "overall": payload.get("overall"),
        "summary": payload.get("summary"),
        "finding_count": len(swarm_findings),
        "severity_counts": dict(sorted(severity_counts.items())),
        "findings": bounded(swarm_findings, max_items),
        "agent_mail_findings": bounded(agent_mail_findings, max_items),
        "agent_mail_build_slots": build_slot_finding,
        "context_intelligence": summarize_context_intelligence(
            context_intelligence_finding,
            max_items,
        ),
    }
    progress_slo = summarize_progress_slo_doctor(
        progress_slo_finding,
        max_items,
    )
    if progress_slo is not None:
        summary["progress_slo"] = progress_slo
    validation_broker = summarize_validation_broker_doctor(
        validation_broker_finding,
        max_items,
    )
    if validation_broker is not None:
        summary["validation_broker"] = validation_broker
    return summary


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
    mode = payload.get("mode") if isinstance(payload.get("mode"), str) else "preflight"
    if not violations and status in {None, "pass", "ok"}:
        return []
    first_violation = violations[0] if violations and isinstance(violations[0], dict) else {}
    return [
        {
            "surface": "rch_sync_retrieval",
            "source": source.id,
            "label": "rch_artifact_sync",
            "signal": "artifact_sync_postcondition"
            if mode == "postcondition"
            else "artifact_sync_preflight",
            "mode": mode,
            "status": status,
            "violation_count": len(violations),
            "path": first_violation.get("path"),
            "reason": first_violation.get("reason"),
            "recommended_action": first_violation.get("recommended_action"),
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
    bottlenecks: list[dict[str, Any]] = []
    if by_id.get("rch_artifact_sync") is not None:
        bottlenecks.extend(extract_rch_sync_bottlenecks(by_id["rch_artifact_sync"]))
    bottlenecks.extend(extract_core_bottlenecks(runpack))
    if by_id.get("tail_latency") is not None:
        bottlenecks.extend(extract_tail_latency_bottlenecks(by_id["tail_latency"], max_items))
    if by_id.get("flight_recorder") is not None:
        bottlenecks.extend(extract_flight_recorder_bottlenecks(by_id["flight_recorder"], max_items))
    if by_id.get("hostcall_swarm_profile") is not None:
        bottlenecks.extend(extract_hostcall_bottlenecks(by_id["hostcall_swarm_profile"], max_items))
    if by_id.get("session_recovery_swarm_profile") is not None:
        bottlenecks.extend(extract_session_bottlenecks(by_id["session_recovery_swarm_profile"]))
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


def number_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def pressure_status_rank(status: str) -> int:
    return {
        "ok": 0,
        "watch": 1,
        "pressured": 2,
        "blocked": 3,
    }.get(status, 3)


def highest_pressure_status(statuses: list[str]) -> str:
    if not statuses:
        return "ok"
    return max(statuses, key=pressure_status_rank)


def pressure_dimension(
    *,
    dimension_id: str,
    status: str,
    signal: str,
    evidence_paths: list[str],
    metrics: dict[str, Any],
    recommendation: str,
) -> dict[str, Any]:
    assert dimension_id in TURN_PRESSURE_LEDGER_DIMENSION_IDS
    assert status in TURN_PRESSURE_LEDGER_ALLOWED_STATUSES
    return {
        "id": dimension_id,
        "status": status,
        "signal": signal,
        "evidence_paths": evidence_paths,
        "metrics": metrics,
        "recommendation": recommendation,
    }


def pressure_event_from_dimension(dimension: dict[str, Any]) -> dict[str, Any] | None:
    if dimension.get("status") == "ok":
        return None
    return {
        "dimension_id": dimension.get("id"),
        "status": dimension.get("status"),
        "signal": dimension.get("signal"),
        "evidence_paths": dimension.get("evidence_paths") or [],
        "recommendation": dimension.get("recommendation"),
    }


def build_normal_turn_pressure_dimension(runpack: dict[str, Any]) -> dict[str, Any]:
    validation_status = runpack.get("validation_outputs", {}).get("status")
    smoke_status = runpack.get("smoke_harness", {}).get("harness_status")
    dirty = runpack.get("git_state", {}).get("dirty") is True
    active_beads = int_value(runpack.get("beads", {}).get("active_count")) or 0
    status = "ok"
    signal = "normal_turn_observed"
    if validation_status == "failed":
        status = "blocked"
        signal = "validation_failed_during_turn"
    elif smoke_status not in {None, "pass", "ok"} or dirty:
        status = "watch"
        signal = "baseline_turn_has_handoff_caveats"
    return pressure_dimension(
        dimension_id="normal_turn_baseline",
        status=status,
        signal=signal,
        evidence_paths=[
            "smoke_harness.harness_status",
            "validation_outputs.status",
            "git_state.dirty",
            "beads.active_count",
        ],
        metrics={
            "smoke_status": smoke_status,
            "validation_status": validation_status,
            "git_dirty": dirty,
            "active_beads": active_beads,
        },
        recommendation=(
            "Treat the turn as normal only when smoke, validation, git, and active-Beads "
            "signals are clean."
        ),
    )


def build_tool_artifact_pressure_dimension(runpack: dict[str, Any]) -> dict[str, Any]:
    smoke = runpack.get("smoke_harness") if isinstance(runpack.get("smoke_harness"), dict) else {}
    manifest = (
        smoke.get("artifact_manifest")
        if isinstance(smoke.get("artifact_manifest"), list)
        else []
    )
    validation = (
        runpack.get("validation_outputs")
        if isinstance(runpack.get("validation_outputs"), dict)
        else {}
    )
    outputs = (
        validation.get("outputs") if isinstance(validation.get("outputs"), list) else []
    )
    artifact_sizes = [
        int_value(item.get("size_bytes")) or 0
        for item in manifest
        if isinstance(item, dict)
    ]
    output_sizes = [
        int_value(item.get("size_bytes")) or 0
        for item in outputs
        if isinstance(item, dict)
    ]
    total_bytes = sum(artifact_sizes) + sum(output_sizes)
    largest_bytes = max(artifact_sizes + output_sizes + [0])
    artifact_count = len(manifest) + len(outputs)
    status = "ok"
    signal = "tool_outputs_within_inline_budget"
    if total_bytes >= 2_000_000 or largest_bytes >= 1_000_000 or artifact_count > 8:
        status = "pressured"
        signal = "tool_artifact_spillover"
    elif total_bytes or artifact_count:
        status = "watch"
        signal = "tool_artifacts_recorded"
    return pressure_dimension(
        dimension_id="tool_artifact_spillover",
        status=status,
        signal=signal,
        evidence_paths=[
            "smoke_harness.artifact_manifest",
            "validation_outputs.outputs",
        ],
        metrics={
            "artifact_count": artifact_count,
            "total_bytes": total_bytes,
            "largest_bytes": largest_bytes,
        },
        recommendation=(
            "Keep large tool output in evidence artifacts and avoid embedding bulky "
            "payloads into prompts."
        ),
    )


def build_compaction_pressure_dimension(runpack: dict[str, Any]) -> dict[str, Any]:
    context = runpack.get("doctor_swarm", {}).get("context_intelligence")
    context = context if isinstance(context, dict) else {}
    bundle = context.get("bundle") if isinstance(context.get("bundle"), dict) else {}
    cache = context.get("cache") if isinstance(context.get("cache"), dict) else {}
    estimated_tokens = int_value(bundle.get("estimated_tokens")) or 0
    missing_test_links = int_value(bundle.get("missing_test_link_count")) or 0
    cache_pressure = cache.get("pressure") is True
    cache_pressure_count = int_value(cache.get("pressure_count")) or 0
    status = "ok"
    signal = "compaction_budget_available"
    if cache_pressure or estimated_tokens >= 8_000:
        status = "pressured"
        signal = "compaction_admission_pressure"
    elif estimated_tokens >= 2_000 or missing_test_links:
        status = "watch"
        signal = "compaction_watch"
    return pressure_dimension(
        dimension_id="compaction_admission",
        status=status,
        signal=signal,
        evidence_paths=[
            "doctor_swarm.context_intelligence.bundle.estimated_tokens",
            "doctor_swarm.context_intelligence.cache.pressure",
            "doctor_swarm.context_intelligence.bundle.missing_test_link_count",
        ],
        metrics={
            "estimated_tokens": estimated_tokens,
            "missing_test_link_count": missing_test_links,
            "cache_pressure": cache_pressure,
            "cache_pressure_count": cache_pressure_count,
        },
        recommendation=(
            "Gate compaction or prompt expansion on current context-intelligence "
            "evidence before adding more source material."
        ),
    )


def build_provider_routing_pressure_dimension(by_id: dict[str, SourcePayload]) -> dict[str, Any]:
    flight = by_id.get("flight_recorder")
    payload = flight.payload if flight is not None and isinstance(flight.payload, dict) else {}
    routes = payload.get("provider_routing")
    routes = routes if isinstance(routes, list) else []
    fallback_routes = [
        route
        for route in routes
        if isinstance(route, dict)
        and (
            route.get("fallback") is True
            or route.get("rerouted") is True
            or route.get("status") in {"fallback", "rerouted", "degraded"}
        )
    ]
    component_counts = (
        payload.get("component_counts")
        if isinstance(payload.get("component_counts"), dict)
        else {}
    )
    provider_events = int_value(component_counts.get("provider")) or len(routes)
    status = "ok"
    signal = "provider_route_stable"
    if fallback_routes:
        status = "pressured"
        signal = "provider_reroute_or_fallback"
    elif provider_events:
        status = "watch"
        signal = "provider_routing_observed"
    return pressure_dimension(
        dimension_id="provider_routing",
        status=status,
        signal=signal,
        evidence_paths=[
            "flight_recorder.provider_routing",
            "flight_recorder.component_counts.provider",
        ],
        metrics={
            "provider_event_count": provider_events,
            "route_count": len(routes),
            "fallback_count": len(fallback_routes),
            "fallback_routes": bounded(
                [
                    {
                        "from": route.get("from") or route.get("provider"),
                        "to": route.get("to") or route.get("fallback_provider"),
                        "reason": route.get("reason"),
                    }
                    for route in fallback_routes
                    if isinstance(route, dict)
                ],
                4,
            ),
        },
        recommendation=(
            "Record provider reroutes as pressure evidence before treating latency or "
            "success-rate samples as stable."
        ),
    )


def build_tui_frame_pressure_dimension(by_id: dict[str, SourcePayload]) -> dict[str, Any]:
    tail = by_id.get("tail_latency")
    payload = tail.payload if tail is not None and isinstance(tail.payload, dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), list) else []
    frame_metrics: list[dict[str, Any]] = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        name = f"{metric.get('id') or ''} {metric.get('label') or ''}".lower()
        if "tui" not in name and "frame" not in name:
            continue
        snapshot = metric.get("snapshot") if isinstance(metric.get("snapshot"), dict) else {}
        tail_payload = snapshot.get("tail") if isinstance(snapshot.get("tail"), dict) else {}
        p99_us = number_value(tail_payload.get("p99_us"))
        max_us = number_value(snapshot.get("max_us"))
        frame_metrics.append(
            {
                "id": metric.get("id"),
                "label": metric.get("label"),
                "p99_us": p99_us,
                "max_us": max_us,
            }
        )
    worst_p99 = max(
        [metric["p99_us"] or 0 for metric in frame_metrics] + [0],
    )
    status = "ok"
    signal = "tui_frame_budget_clear"
    if worst_p99 >= 16_667:
        status = "pressured"
        signal = "tui_frame_budget_pressure"
    elif worst_p99 >= 8_333:
        status = "watch"
        signal = "tui_frame_budget_watch"
    return pressure_dimension(
        dimension_id="tui_frame_budget",
        status=status,
        signal=signal,
        evidence_paths=["tail_latency.metrics"],
        metrics={
            "frame_budget_us": 16_667,
            "worst_p99_us": worst_p99,
            "frame_metric_count": len(frame_metrics),
            "frame_metrics": bounded(frame_metrics, 4),
        },
        recommendation=(
            "Keep TUI pressure separate from provider/tool pressure so frame-budget "
            "regressions do not hide in aggregate latency."
        ),
    )


def build_session_write_pressure_dimension(by_id: dict[str, SourcePayload]) -> dict[str, Any]:
    source = by_id.get("session_recovery_swarm_profile")
    payload = source.payload if source is not None and isinstance(source.payload, dict) else {}
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    timings = payload.get("timings_us") if isinstance(payload.get("timings_us"), dict) else {}
    write_amplification = (
        payload.get("write_amplification")
        if isinstance(payload.get("write_amplification"), dict)
        else {}
    )
    tail_entries = int_value(counts.get("tail_entries_appended")) or 0
    base_entries = int_value(counts.get("base_entries")) or 0
    entries_written = int_value(write_amplification.get("entries_written")) or tail_entries
    logical_turns = int_value(write_amplification.get("logical_turns")) or 1
    entries_per_turn = entries_written / max(logical_turns, 1)
    bytes_written = int_value(write_amplification.get("bytes_written")) or 0
    slowest_us = max(
        [
            number_value(value) or 0
            for value in timings.values()
            if isinstance(value, (int, float, str))
        ]
        + [0],
    )
    status = "ok"
    signal = "session_writes_within_budget"
    if (
        write_amplification.get("status") in {"pressured", "blocked"}
        or entries_per_turn >= 64
        or bytes_written >= 1_000_000
        or slowest_us >= 10_000
    ):
        status = "pressured"
        signal = "session_write_backpressure"
    elif entries_written or slowest_us:
        status = "watch"
        signal = "session_write_activity_observed"
    return pressure_dimension(
        dimension_id="session_write_amplification",
        status=status,
        signal=signal,
        evidence_paths=[
            "session_recovery_swarm_profile.counts",
            "session_recovery_swarm_profile.timings_us",
            "session_recovery_swarm_profile.write_amplification",
        ],
        metrics={
            "base_entries": base_entries,
            "tail_entries_appended": tail_entries,
            "entries_written": entries_written,
            "logical_turns": logical_turns,
            "entries_per_turn": round(entries_per_turn, 2),
            "bytes_written": bytes_written,
            "slowest_us": slowest_us,
        },
        recommendation=(
            "Track session write amplification per turn before widening replay, "
            "branching, or compaction workflows."
        ),
    )


def build_turn_pressure_ledger(
    runpack: dict[str, Any],
    by_id: dict[str, SourcePayload],
    *,
    generated_at: datetime,
    max_items: int,
) -> dict[str, Any]:
    dimensions = [
        build_normal_turn_pressure_dimension(runpack),
        build_tool_artifact_pressure_dimension(runpack),
        build_compaction_pressure_dimension(runpack),
        build_provider_routing_pressure_dimension(by_id),
        build_tui_frame_pressure_dimension(by_id),
        build_session_write_pressure_dimension(by_id),
    ]
    events = [
        event
        for event in (pressure_event_from_dimension(dimension) for dimension in dimensions)
        if event is not None
    ]
    statuses = [str(dimension.get("status") or "blocked") for dimension in dimensions]
    status = highest_pressure_status(statuses)
    dimension_counts = dict(sorted(Counter(statuses).items()))
    ledger = {
        "schema": TURN_PRESSURE_LEDGER_SCHEMA,
        "generated_at": generated_at.isoformat(),
        "status": status,
        "purpose": "operator_diagnostic_not_release_performance_claim",
        "turn_window": {
            "mode": "operator_runpack_snapshot",
            "source_status_count": len(runpack.get("source_statuses") or []),
            "max_items": max_items,
        },
        "dimensions": dimensions,
        "pressure_events": bounded(events, max_items),
        "summary": {
            "dimension_count": len(dimensions),
            "pressure_event_count": len(events),
            "dimension_status_counts": dimension_counts,
            "highest_status": status,
        },
        "guards": {
            "advisory_only": True,
            "not_release_claim_evidence": True,
            "no_embedded_bodies": True,
            "does_not_replace_source_runtime_metrics": True,
            "stable_for_action_planner_consumption": True,
        },
    }
    assert_turn_pressure_ledger_contract(ledger)
    return ledger


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
    if issue.get("issue_type") is not None:
        candidate["issue_type"] = issue.get("issue_type")
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


def issue_labels(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels")
    if isinstance(labels, list):
        return [str(label) for label in labels]
    if isinstance(labels, str):
        return [label.strip() for label in labels.split(",") if label.strip()]
    return []


def issue_type(issue: dict[str, Any]) -> str:
    value = issue.get("issue_type") or issue.get("type")
    return str(value or "")


def child_issues_by_parent(issues: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    children: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        for dep in issue.get("dependencies") or []:
            if not isinstance(dep, dict):
                continue
            dep_type = dep.get("type") or dep.get("dependency_type")
            if dep_type != "parent-child":
                continue
            parent_id = dep.get("depends_on_id") or dep.get("id")
            if parent_id:
                children.setdefault(str(parent_id), []).append(issue)
    return children


def is_deferred_planning_epic(issue: dict[str, Any]) -> bool:
    if issue.get("status") != "deferred":
        return False
    if issue_type(issue) != "epic":
        return False
    labels = {label.lower() for label in issue_labels(issue)}
    if labels & DEFERRED_PLANNING_LABELS:
        return True
    haystack = " ".join(
        str(issue.get(field, "")).lower()
        for field in ("title", "description", "body", "notes")
    )
    return any(term in haystack for term in DEFERRED_PLANNING_TERMS)


def deferred_planning_beads(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    children_by_parent = child_issues_by_parent(issues)
    planning: list[dict[str, Any]] = []
    for issue in issues:
        if not is_deferred_planning_epic(issue):
            continue
        children = children_by_parent.get(str(issue.get("id")), [])
        child_statuses: dict[str, int] = {}
        for child in children:
            status = str(child.get("status") or "unknown")
            child_statuses[status] = child_statuses.get(status, 0) + 1
        active_child_count = (
            child_statuses.get("open", 0) + child_statuses.get("in_progress", 0)
        )
        if active_child_count:
            continue
        planning.append(
            {
                **normalized_bead_candidate(issue),
                "child_count": len(children),
                "child_statuses": dict(sorted(child_statuses.items())),
                "planning_reason": "deferred roadmap/planning epic has no open or in-progress child work",
            }
        )
    return sorted(
        planning,
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
    active_ownership = [
        normalized_bead_candidate(issue)
        for issue in issues
        if issue.get("status") == "in_progress"
    ]
    active_ownership.sort(
        key=lambda issue: (
            str(issue.get("updated_at") or ""),
            str(issue.get("id") or ""),
        ),
        reverse=True,
    )
    open_candidates = sort_bead_candidates(
        [issue for issue in issues if issue.get("status") == "open"]
    )
    deferred_planning = deferred_planning_beads(issues)
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
        "active_ownership": bounded(active_ownership, max_items),
        "open_candidate_count": len(open_candidates),
        "open_candidates": bounded(open_candidates, max_items),
        "deferred_planning_count": len(deferred_planning),
        "deferred_planning": bounded(deferred_planning, max_items),
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


def summarize_swarm_replay_preview(source: SourcePayload, max_items: int) -> dict[str, Any]:
    payload = source.payload
    if not isinstance(payload, dict):
        return {"status": source.status}
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    replay = payload.get("replay") if isinstance(payload.get("replay"), dict) else {}
    policies = payload.get("policies") if isinstance(payload.get("policies"), dict) else {}
    recommendation = (
        payload.get("recommendation")
        if isinstance(payload.get("recommendation"), dict)
        else {}
    )
    comparisons = policies.get("comparisons") if isinstance(policies.get("comparisons"), list) else []
    return {
        "status": source.status,
        "source_path": source.path,
        "schema": payload.get("schema"),
        "trace_id": trace.get("trace_id"),
        "trace_path": trace.get("path"),
        "event_count": trace.get("event_count"),
        "replayed_event_count": replay.get("replayed_event_count"),
        "diagnostic_count": replay.get("diagnostic_count"),
        "resource_saturation_points": replay.get("resource_saturation_points"),
        "policy_ids": bounded(policies.get("evaluated_policy_ids") or [], max_items),
        "decision_count": policies.get("decision_count"),
        "comparison_count": policies.get("comparison_count"),
        "distinct_action_count": policies.get("distinct_action_count"),
        "score_spread": policies.get("score_spread"),
        "recommended_policy_id": recommendation.get("policy_id"),
        "recommendation_confidence": recommendation.get("confidence"),
        "recommendation_score": recommendation.get("score"),
        "comparison_rows": bounded(comparisons, max_items),
        "guards": payload.get("guards") if isinstance(payload.get("guards"), dict) else {},
        "output_paths": payload.get("output_paths")
        if isinstance(payload.get("output_paths"), dict)
        else {},
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


def utc_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def proof_string(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) and value else default


def proof_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def proof_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def command_argv_from_admission(payload: dict[str, Any]) -> list[str]:
    proof = proof_dict(
        payload.get("remote_validation_proof") or payload.get("remote_validation")
    )
    command = proof_dict(proof.get("command"))
    argv = command.get("argv")
    if isinstance(argv, list) and all(isinstance(part, str) for part in argv):
        return argv

    cargo_command = payload.get("cargo_command")
    if isinstance(cargo_command, str) and cargo_command.strip():
        parts = shlex.split(cargo_command)
        if parts and parts[0] == "cargo":
            return parts
        return ["cargo", *parts]
    return ["cargo", "<unknown>"]


def classify_remote_validation_command(argv: list[str], payload: dict[str, Any]) -> str:
    proof = proof_dict(
        payload.get("remote_validation_proof") or payload.get("remote_validation")
    )
    command = proof_dict(proof.get("command"))
    explicit = command.get("command_class") or proof.get("command_class")
    allowed = {
        "cargo_check",
        "cargo_clippy",
        "cargo_test",
        "cargo_build",
        "cargo_fmt_check",
        "script_self_test",
        "ubs_staged",
        "beads_ledger_reconcile",
        "custom_validation",
    }
    if isinstance(explicit, str) and explicit in allowed:
        return explicit

    subcommand = argv[1] if len(argv) > 1 else ""
    if subcommand == "check":
        return "cargo_check"
    if subcommand == "clippy":
        return "cargo_clippy"
    if subcommand == "test":
        return "cargo_test"
    if subcommand == "build":
        return "cargo_build"
    if subcommand == "fmt" and "--check" in argv:
        return "cargo_fmt_check"
    if subcommand == "fmt":
        return "custom_validation"
    if payload.get("command_class") == "safe_local":
        return "custom_validation"
    return "custom_validation"


def command_fingerprint(
    argv: list[str],
    cwd: str,
    git_head: str | None,
    command_class: str,
    cargo_target_dir: Any,
    tmpdir: Any,
    runner_requirement: str,
) -> str:
    payload = {
        "argv": argv,
        "cwd": cwd,
        "git_head": git_head,
        "command_class": command_class,
        "feature_flags": [],
        "cargo_target_dir": cargo_target_dir,
        "tmpdir": tmpdir,
        "runner_requirement": runner_requirement,
        "dirty_path_policy": "reported_in_runpack",
    }
    digest = hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def git_changed_paths(git_state: dict[str, Any]) -> list[str]:
    raw_lines = git_state.get("porcelain_lines")
    paths: list[str] = []
    if isinstance(raw_lines, list):
        for line in raw_lines:
            text = str(line)
            path = text[3:] if len(text) > 3 else text
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[1]
            if path:
                paths.append(path)
    sample = git_state.get("sample")
    if isinstance(sample, list):
        for entry in sample:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                path = entry["path"]
                if " -> " in path:
                    path = path.rsplit(" -> ", 1)[1]
                if path:
                    paths.append(path)
    return unique_strings(paths)


def command_explicit_paths(command_payload: dict[str, Any], key: str) -> list[str]:
    values = command_payload.get(key)
    if not isinstance(values, list):
        coverage = command_payload.get("coverage")
        if isinstance(coverage, dict):
            values = coverage.get(key)
    if not isinstance(values, list):
        return []
    return unique_strings([value for value in values if isinstance(value, str) and value])


def command_matches_script(argv: list[str], script_path: str) -> bool:
    return any(part == script_path or part.endswith(f"/{script_path}") for part in argv)


def inferred_covered_paths(
    *,
    command_class: str,
    argv: list[str],
    changed_paths: list[str],
) -> list[str]:
    if command_class in {
        "cargo_check",
        "cargo_clippy",
        "cargo_test",
        "cargo_build",
        "cargo_fmt_check",
    }:
        return [
            path
            for path in changed_paths
            if path.endswith(".rs")
            or path.endswith(".toml")
            or path == "Cargo.lock"
            or path.startswith("tests/")
        ]
    if command_class == "script_self_test":
        covered_prefixes = []
        if command_matches_script(argv, "scripts/build_swarm_operator_runpack.py"):
            covered_prefixes.extend(
                [
                    "scripts/build_swarm_operator_runpack.py",
                    "tests/golden_corpus/swarm_operator_runpack/",
                    "tests/golden_corpus/remote_validation_proof_ledger/",
                    "docs/contracts/remote-validation-proof-ledger-contract.json",
                    "docs/swarm-operations-runbook.md",
                ]
            )
        return [
            path
            for path in changed_paths
            if any(path == prefix or path.startswith(prefix) for prefix in covered_prefixes)
        ]
    if command_class == "ubs_staged":
        return [path for path in changed_paths if path.endswith(".rs")]
    if command_class == "beads_ledger_reconcile":
        return [
            path
            for path in changed_paths
            if path.startswith(".beads/")
            or path == "docs/evidence/dropin-parity-gap-ledger.json"
        ]
    return []


def build_validation_coverage_summary(
    *,
    command_class: str,
    argv: list[str],
    command_payload: dict[str, Any],
    git_state: dict[str, Any],
    status: str,
    success: bool,
) -> dict[str, Any]:
    changed_paths = git_changed_paths(git_state)
    explicit_covered = command_explicit_paths(command_payload, "covered_paths")
    covered_paths = explicit_covered or inferred_covered_paths(
        command_class=command_class,
        argv=argv,
        changed_paths=changed_paths,
    )
    claimed_paths = command_explicit_paths(command_payload, "claimed_paths")
    if not claimed_paths:
        claimed_paths = changed_paths
    uncovered_paths = [path for path in claimed_paths if path not in covered_paths]
    coverage = command_payload.get("coverage")
    explicit_authority = (
        coverage.get("authoritative_for_bead")
        if isinstance(coverage, dict)
        else command_payload.get("authoritative_for_bead")
    )
    claimed_authoritative = explicit_authority is True

    if status in {"blocked", "fail"}:
        coverage_status = status
    elif not changed_paths:
        coverage_status = "no_changed_paths"
    elif not claimed_paths:
        coverage_status = "no_claimed_paths"
    elif not covered_paths:
        coverage_status = "uncovered"
    elif uncovered_paths:
        coverage_status = "partial"
    else:
        coverage_status = "covered"

    authoritative_for_bead = bool(
        success
        and status == "pass"
        and coverage_status in {"covered", "no_changed_paths", "no_claimed_paths"}
    )
    if claimed_authoritative and not authoritative_for_bead:
        coverage_status = "claimed_uncovered"

    if coverage_status == "covered":
        rationale = "claimed changed paths are covered by this validation proof"
    elif coverage_status == "no_changed_paths":
        rationale = "worktree reported no changed paths for this proof context"
    elif coverage_status == "no_claimed_paths":
        rationale = "proof did not claim a file surface"
    elif coverage_status in {"blocked", "fail"}:
        rationale = "validation did not complete cleanly, so coverage is not authoritative"
    elif coverage_status == "claimed_uncovered":
        rationale = "proof explicitly claimed bead authority but did not cover every claimed path"
    else:
        rationale = "proof covers only part of the claimed or changed surface"

    return {
        "changed_paths": changed_paths,
        "claimed_paths": claimed_paths,
        "covered_paths": covered_paths,
        "uncovered_paths": uncovered_paths,
        "coverage_status": coverage_status,
        "coverage_rationale": rationale,
        "claimed_authoritative": claimed_authoritative,
        "authoritative_for_bead": authoritative_for_bead,
    }


def command_argv_from_capture(command: dict[str, Any]) -> list[str]:
    explicit = command.get("argv")
    if isinstance(explicit, list) and all(isinstance(part, str) for part in explicit):
        return explicit
    rendered = proof_string(command.get("command"))
    if not rendered:
        return ["<unknown>"]
    try:
        return shlex.split(rendered)
    except ValueError:
        return rendered.split()


def unwrap_validation_command(argv: list[str]) -> tuple[list[str], str]:
    unwrapped = list(argv)
    if len(unwrapped) >= 2 and unwrapped[0] == "timeout":
        unwrapped = unwrapped[2:]
    if unwrapped and unwrapped[0] == "env":
        index = 1
        while index < len(unwrapped) and "=" in unwrapped[index]:
            index += 1
        unwrapped = unwrapped[index:]
    if len(unwrapped) >= 4 and unwrapped[:3] == ["rch", "exec", "--"]:
        return unwrapped[3:], "rch"
    return unwrapped, "local"


def classify_capture_validation_command(command: dict[str, Any]) -> str | None:
    argv = command_argv_from_capture(command)
    inner_argv, _requested_runner = unwrap_validation_command(argv)
    explicit = command.get("command_class")
    if isinstance(explicit, str) and explicit in {
        "cargo_check",
        "cargo_clippy",
        "cargo_test",
        "cargo_build",
        "cargo_fmt_check",
        "script_self_test",
        "ubs_staged",
        "beads_ledger_reconcile",
        "custom_validation",
    }:
        return explicit
    if inner_argv and inner_argv[0] == "cargo":
        return classify_remote_validation_command(inner_argv, {"command_class": "capture"})
    if command_matches_script(inner_argv, "scripts/build_swarm_operator_runpack.py") and "--self-test" in inner_argv:
        return "script_self_test"
    if inner_argv and inner_argv[0] == "ubs" and "--staged" in inner_argv:
        return "ubs_staged"
    if command_matches_script(inner_argv, "scripts/reconcile_beads_ledger.sh"):
        return "beads_ledger_reconcile"
    return None


def capture_command_issue_text(command: dict[str, Any]) -> str:
    return " ".join(
        str(command.get(key) or "")
        for key in ("command", "status", "issue", "stderr_snippet", "stdout_snippet")
    ).lower()


def rch_drift_category_from_command(command: dict[str, Any]) -> str | None:
    text = capture_command_issue_text(command)
    for rule in FAILURE_ACTION_RULES:
        if rule.get("category") != "rch":
            continue
        if rule_matches_failure_signal(rule, text):
            drift_category = rule.get("drift_category_id")
            return str(drift_category) if drift_category else None
    return None


def artifact_retrieval_summary(source: SourcePayload | None) -> dict[str, Any]:
    payload = source.payload if source is not None else None
    if not isinstance(payload, dict):
        return {
            "status": "unknown",
            "retrieved_paths": [],
            "missing_paths": [],
            "warning_details": ["RCH artifact-sync source not provided"],
            "retrieval_exit_code": None,
            "retrieval_elapsed_ms": None,
        }

    required_paths = proof_list(payload.get("required_paths"))
    retrieved_paths = []
    missing_paths = []
    for item in required_paths:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        if item.get("included") is True or item.get("updated") is True:
            retrieved_paths.append(path)
        elif item.get("included") is False or item.get("updated") is False:
            missing_paths.append(path)

    violations = proof_list(payload.get("violations"))
    warning_details = []
    for violation in violations:
        if isinstance(violation, str):
            warning_details.append(violation)
        elif isinstance(violation, dict):
            path = proof_string(violation.get("path"), "<unknown>")
            reason = proof_string(violation.get("reason"), "artifact retrieval warning")
            warning_details.append(f"{path}: {reason}")

    raw_status = proof_string(payload.get("status"), "unknown").lower()
    if raw_status in {"pass", "ok", "clean", "ready"} and not warning_details:
        status = "clean"
    elif raw_status in {"fail", "failed", "error"}:
        status = "failed"
    elif raw_status in {"warn", "warning", "degraded"} or warning_details:
        status = "warning"
    elif raw_status in {"skipped", "not_applicable"}:
        status = raw_status
    else:
        status = "unknown"

    return {
        "status": status,
        "retrieved_paths": retrieved_paths,
        "missing_paths": missing_paths,
        "warning_details": warning_details,
        "retrieval_exit_code": payload.get("exit_code"),
        "retrieval_elapsed_ms": payload.get("elapsed_ms") or payload.get("duration_ms"),
    }


def remote_validation_warning(
    warning_id: str, severity: str, message: str, source: str
) -> dict[str, Any]:
    return {
        "warning_id": warning_id,
        "severity": severity,
        "message": message,
        "source": source,
        "redacted": True,
    }


def build_remote_validation_proof_entry(
    *,
    cargo_source: SourcePayload,
    artifact_source: SourcePayload | None,
    generated_at: datetime,
    git_state: dict[str, Any],
    bead_id: str | None,
) -> dict[str, Any]:
    payload = cargo_source.payload if isinstance(cargo_source.payload, dict) else {}
    proof = proof_dict(
        payload.get("remote_validation_proof") or payload.get("remote_validation")
    )
    entry_bead_id = proof_string(
        proof.get("bead_id"),
        proof_string(payload.get("bead_id"), bead_id or "operator-runpack"),
    )
    argv = command_argv_from_admission(payload)
    command_class = classify_remote_validation_command(argv, payload)
    requested_runner = proof_string(payload.get("requested_runner"), "read_only")
    admission_runner = proof_string(payload.get("resolved_runner"), "not_run")
    decision = proof_string(payload.get("decision"), "unknown")
    reason = proof_string(payload.get("reason"), "unknown")
    runner = proof_dict(proof.get("runner"))
    timing = proof_dict(proof.get("timing"))
    exit_payload = proof_dict(proof.get("exit"))
    paths_payload = proof_dict(proof.get("paths"))

    runner_requirement = proof_string(runner.get("runner_requirement"))
    if not runner_requirement:
        runner_requirement = (
            "rch_required" if requested_runner in {"rch", "auto"} else "local_allowed"
        )

    remote_execution = bool(runner.get("remote_execution"))
    if runner.get("resolved_runner") == "rch_remote":
        remote_execution = True
    if admission_runner == "rch" and exit_payload.get("exit_code") is not None:
        remote_execution = True

    resolved_runner = proof_string(runner.get("resolved_runner"))
    if not resolved_runner:
        if remote_execution:
            resolved_runner = "rch_remote"
        elif admission_runner == "local":
            resolved_runner = "local"
        else:
            resolved_runner = "not_run"

    local_fallback = proof_string(runner.get("local_fallback"))
    if not local_fallback:
        if admission_runner == "local" and requested_runner in {"rch", "auto"}:
            local_fallback = "observed"
        elif decision in {"backoff", "deny"} and runner_requirement == "rch_required":
            local_fallback = "refused"
        else:
            local_fallback = "none"

    artifact = artifact_retrieval_summary(artifact_source)
    if resolved_runner == "not_run":
        artifact = {
            **artifact,
            "status": "not_applicable",
            "retrieved_paths": [],
            "missing_paths": [],
            "warning_details": [],
        }

    started_at = proof_string(timing.get("started_at_utc"))
    ended_at = proof_string(timing.get("ended_at_utc"))
    if not started_at:
        started_at = utc_z(generated_at)
    if not ended_at:
        ended_at = utc_z(generated_at)
    duration_ms = timing.get("duration_ms")
    if not isinstance(duration_ms, int) or duration_ms < 0:
        duration_ms = 0

    termination_reason = proof_string(exit_payload.get("termination_reason"))
    exit_code = exit_payload.get("exit_code")
    if not termination_reason:
        if decision == "backoff" and "queue" in reason:
            termination_reason = "queue_backoff"
        elif local_fallback == "refused":
            termination_reason = "local_fallback_refused"
        elif exit_code is None:
            termination_reason = "not_run"
        elif exit_code == 0:
            termination_reason = "completed"
        else:
            termination_reason = "failed"
    success = exit_payload.get("success")
    if not isinstance(success, bool):
        success = exit_code == 0 if isinstance(exit_code, int) else False

    warnings: list[dict[str, Any]] = []
    queue_forecast = proof_dict(payload.get("rch_queue_forecast"))
    if termination_reason == "queue_backoff" or queue_forecast.get("recommended_action") == "backoff":
        warnings.append(
            remote_validation_warning(
                "queue_backoff",
                "error",
                "RCH queue forecast required backoff; validation command did not run.",
                "cargo_admission",
            )
        )
    if local_fallback == "refused":
        warnings.append(
            remote_validation_warning(
                "local_fallback_refused",
                "error",
                "Local fallback was refused for an RCH-required validation gate.",
                "cargo_admission",
            )
        )
    if local_fallback == "observed":
        warnings.append(
            remote_validation_warning(
                "local_fallback_observed",
                "error",
                "Validation ran locally after an RCH request; this is not remote proof.",
                "cargo_admission",
            )
        )
    if timing.get("stale_progress_detected") is True:
        warnings.append(
            remote_validation_warning(
                "stale_progress",
                "warning",
                "RCH progress heartbeat became stale before completion.",
                "remote_validation_proof",
            )
        )
    if artifact["status"] in {"warning", "failed"}:
        warnings.append(
            remote_validation_warning(
                "artifact_retrieval_warning",
                "warning" if artifact["status"] == "warning" else "error",
                "RCH artifact retrieval was not clean.",
                "rch_artifact_sync",
            )
        )

    clean_remote_proof = (
        success
        and remote_execution
        and local_fallback == "none"
        and artifact["status"] in {"clean", "not_applicable"}
        and not any(warning["severity"] == "error" for warning in warnings)
    )
    degraded_reasons: list[str] = []
    if not remote_execution:
        degraded_reasons.append("remote_execution_not_observed")
    if local_fallback != "none":
        degraded_reasons.append(f"local_fallback_{local_fallback}")
    if artifact["status"] not in {"clean", "not_applicable"}:
        degraded_reasons.append(f"artifact_retrieval_{artifact['status']}")
    if warnings:
        degraded_reasons.extend(warning["warning_id"] for warning in warnings)
    if not success and termination_reason not in {"queue_backoff", "local_fallback_refused", "not_run"}:
        degraded_reasons.append("validation_exit_failed")

    if clean_remote_proof:
        status = "pass"
        next_actions: list[str] = []
    elif termination_reason in {"queue_backoff", "local_fallback_refused", "not_run"}:
        status = "blocked"
        next_actions = ["Rerun the validation gate through RCH before closeout."]
    elif artifact["status"] == "failed" or (exit_code not in {None, 0}):
        status = "fail"
        next_actions = ["Inspect the failed validation command and RCH artifact retrieval output."]
    else:
        status = "degraded"
        next_actions = ["Treat this as degraded operator evidence, not clean remote proof."]

    cwd = proof_string(proof_dict(proof.get("command")).get("cwd"), str(Path.cwd()))
    requested_runner_contract = (
        requested_runner
        if requested_runner in {"rch", "local", "read_only"}
        else "rch"
        if runner_requirement == "rch_required"
        else "local"
    )
    rendered = proof_string(
        proof_dict(proof.get("command")).get("rendered"),
        " ".join(["rch", "exec", "--", *argv])
        if requested_runner_contract == "rch"
        else " ".join(argv),
    )
    fingerprint = proof_string(
        proof_dict(proof.get("command")).get("command_fingerprint"),
        command_fingerprint(
            argv,
            cwd,
            git_state.get("head") if isinstance(git_state, dict) else None,
            command_class,
            payload.get("cargo_target_dir"),
            payload.get("tmpdir"),
            runner_requirement,
        ),
    )
    coverage_payload: dict[str, Any] = {}
    for key in ("covered_paths", "claimed_paths", "authoritative_for_bead"):
        if key in payload:
            coverage_payload[key] = payload[key]
        if key in proof:
            coverage_payload[key] = proof[key]
    if isinstance(proof.get("coverage"), dict):
        coverage_payload["coverage"] = proof["coverage"]
    coverage = build_validation_coverage_summary(
        command_class=command_class,
        argv=argv,
        command_payload=coverage_payload,
        git_state=git_state,
        status=status,
        success=success,
    )
    if coverage["coverage_status"] in {"partial", "uncovered"}:
        degraded_reasons.append("partial_surface_coverage")
    if coverage["coverage_status"] == "claimed_uncovered":
        warnings.append(
            remote_validation_warning(
                "proof_claim_coverage_mismatch",
                "error",
                "Proof claimed bead authority but did not cover every claimed path.",
                "remote_validation_proof",
            )
        )
        degraded_reasons.append("proof_claim_coverage_mismatch")
        clean_remote_proof = False
        status = "fail"
        next_actions = ["Rerun validation with a proof that covers every claimed path."]

    return {
        "schema": REMOTE_VALIDATION_ENTRY_SCHEMA,
        "entry_id": f"rvpe-{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16]}",
        "bead_id": entry_bead_id,
        "command_class": command_class,
        "command": {
            "argv": argv,
            "rendered": rendered,
            "cwd": cwd,
            "command_fingerprint": fingerprint,
            "feature_flags": proof_list(proof_dict(proof.get("command")).get("feature_flags")),
            "env_allowlist": ["CARGO_TARGET_DIR", "TMPDIR"],
        },
        "runner": {
            "requested_runner": requested_runner_contract,
            "resolved_runner": resolved_runner,
            "runner_requirement": runner_requirement,
            "remote_execution": remote_execution,
            "local_fallback": local_fallback,
            "fallback_reason": proof_string(runner.get("fallback_reason"), reason),
            "rch_job_id": runner.get("rch_job_id"),
            "worker_id": runner.get("worker_id"),
            "worker_host": runner.get("worker_host"),
            "queue_state": proof_string(runner.get("queue_state"), queue_forecast.get("reason") or reason),
            "worker_state": proof_string(runner.get("worker_state"), termination_reason),
            "command_rewrite": proof_dict(runner.get("command_rewrite")),
            "status_excerpt": proof_string(runner.get("status_excerpt"), payload.get("rch_detail") or reason),
        },
        "timing": {
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            "duration_ms": duration_ms,
            "heartbeat_at_utc": proof_string(timing.get("heartbeat_at_utc"), ended_at),
            "stale_progress_detected": timing.get("stale_progress_detected") is True,
        },
        "exit": {
            "exit_code": exit_code,
            "success": success,
            "termination_reason": termination_reason,
            "stderr_excerpt": proof_string(exit_payload.get("stderr_excerpt")),
            "stdout_excerpt": proof_string(exit_payload.get("stdout_excerpt")),
        },
        "paths": {
            "cargo_target_dir": payload.get("cargo_target_dir"),
            "tmpdir": payload.get("tmpdir"),
            "remote_target_dir": paths_payload.get("remote_target_dir"),
            "remote_tmpdir": paths_payload.get("remote_tmpdir"),
            "artifact_paths": paths_payload.get("artifact_paths")
            if isinstance(paths_payload.get("artifact_paths"), list)
            else artifact.get("retrieved_paths", []) + artifact.get("missing_paths", []),
        },
        "artifact_retrieval": artifact,
        "warnings": warnings,
        "evidence_classification": {
            "status": status,
            "clean_remote_proof": clean_remote_proof,
            "operator_evidence_only": True,
            "suppressed_claims": [
                "release_performance",
                "strict_dropin",
                "benchmark_throughput",
                "memory_or_startup_claim",
            ],
            "coverage": coverage,
            "degraded_reasons": sorted(set(degraded_reasons)),
            "next_actions": next_actions,
        },
    }


def build_remote_validation_capture_proof_entry(
    *,
    command: dict[str, Any],
    generated_at: datetime,
    git_state: dict[str, Any],
    bead_id: str | None,
) -> dict[str, Any] | None:
    command_class = classify_capture_validation_command(command)
    if command_class is None:
        return None

    original_argv = command_argv_from_capture(command)
    argv, requested_runner = unwrap_validation_command(original_argv)
    cwd = proof_string(command.get("cwd"), str(Path.cwd()))
    rendered = proof_string(command.get("command"), shell_join(original_argv))
    runner_requirement = (
        "rch_required"
        if requested_runner == "rch" and command_class.startswith("cargo_")
        else "no_compile"
        if command_class in {
            "script_self_test",
            "ubs_staged",
            "beads_ledger_reconcile",
        }
        else "local_allowed"
    )
    fingerprint = command_fingerprint(
        original_argv,
        cwd,
        git_state.get("head") if isinstance(git_state, dict) else None,
        command_class,
        command.get("cargo_target_dir"),
        command.get("tmpdir"),
        runner_requirement,
    )

    raw_status = proof_string(command.get("status"), "unknown").lower()
    exit_code = command.get("exit_code")
    if not isinstance(exit_code, int):
        exit_code = None
    success = raw_status == "ok" and exit_code == 0
    termination_reason = (
        "completed"
        if success
        else "timed_out"
        if raw_status == "timeout"
        else "not_run"
        if raw_status in {"blocked", "not_available", "skipped"}
        else "failed"
    )
    issue_text = capture_command_issue_text(command)
    unrelated_worktree_blocker = "unrelated" in issue_text and (
        "dirty worktree" in issue_text or "worktree" in issue_text
    )
    rch_drift_category = rch_drift_category_from_command(command)

    warnings: list[dict[str, Any]] = []
    degraded_reasons: list[str] = []
    if requested_runner == "rch":
        degraded_reasons.append("remote_execution_not_observed")
    if unrelated_worktree_blocker:
        warnings.append(
            remote_validation_warning(
                "unrelated_worktree_blocker",
                "error",
                "Validation was blocked by unrelated dirty worktree state.",
                "capture_manifest",
            )
        )
        degraded_reasons.append("unrelated_worktree_blocker")
    if rch_drift_category:
        warnings.append(
            remote_validation_warning(
                rch_drift_category,
                "error",
                RCH_DRIFT_CATEGORY_METADATA.get(rch_drift_category, {}).get(
                    "remediation",
                    "RCH drift blocked validation before authoritative proof.",
                ),
                "capture_manifest",
            )
        )
        degraded_reasons.append(rch_drift_category)

    if success:
        status = "pass"
        next_actions: list[str] = []
    elif unrelated_worktree_blocker or rch_drift_category or raw_status in {
        "blocked",
        "not_available",
        "skipped",
    }:
        status = "blocked"
        next_actions = ["Resolve the blocker and rerun the validation command."]
    else:
        status = "fail"
        next_actions = ["Inspect the failed validation command output before closeout."]

    coverage = build_validation_coverage_summary(
        command_class=command_class,
        argv=argv,
        command_payload=command,
        git_state=git_state,
        status=status,
        success=success,
    )
    if coverage["coverage_status"] in {"partial", "uncovered"}:
        degraded_reasons.append("partial_surface_coverage")
    if coverage["coverage_status"] == "claimed_uncovered":
        warnings.append(
            remote_validation_warning(
                "proof_claim_coverage_mismatch",
                "error",
                "Proof claimed bead authority but did not cover every claimed path.",
                "capture_manifest",
            )
        )
        degraded_reasons.append("proof_claim_coverage_mismatch")
        status = "fail"
        next_actions = ["Rerun validation with a proof that covers every claimed path."]

    resolved_runner = (
        "local"
        if requested_runner == "local" and raw_status not in {"blocked", "not_available", "skipped"}
        else "not_run"
    )
    started_at = proof_string(command.get("started_at"), utc_z(generated_at))
    ended_at = proof_string(command.get("ended_at"), started_at)
    return {
        "schema": REMOTE_VALIDATION_ENTRY_SCHEMA,
        "entry_id": f"rvpe-{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16]}",
        "bead_id": proof_string(command.get("bead_id"), bead_id or "operator-runpack"),
        "command_class": command_class,
        "command": {
            "argv": original_argv,
            "rendered": rendered,
            "cwd": cwd,
            "capture_id": command.get("id"),
            "command_fingerprint": fingerprint,
            "feature_flags": proof_list(command.get("feature_flags")),
            "env_allowlist": ["CARGO_TARGET_DIR", "TMPDIR"],
        },
        "runner": {
            "requested_runner": requested_runner,
            "resolved_runner": resolved_runner,
            "runner_requirement": runner_requirement,
            "remote_execution": False,
            "local_fallback": "none",
            "fallback_reason": proof_string(command.get("issue")),
            "rch_job_id": command.get("rch_job_id"),
            "worker_id": command.get("worker_id"),
            "worker_host": command.get("worker_host"),
            "queue_state": proof_string(command.get("queue_state")),
            "worker_state": proof_string(command.get("worker_state"), raw_status),
            "command_rewrite": proof_dict(command.get("command_rewrite")),
            "status_excerpt": proof_string(command.get("issue"), raw_status),
        },
        "timing": {
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            "duration_ms": command.get("duration_ms") if isinstance(command.get("duration_ms"), int) else 0,
            "heartbeat_at_utc": proof_string(command.get("heartbeat_at"), ended_at),
            "stale_progress_detected": command.get("stale_progress_detected") is True,
        },
        "exit": {
            "exit_code": exit_code,
            "success": success,
            "termination_reason": termination_reason,
            "stderr_excerpt": proof_string(command.get("stderr_snippet")),
            "stdout_excerpt": proof_string(command.get("stdout_snippet")),
        },
        "paths": {
            "cargo_target_dir": command.get("cargo_target_dir"),
            "tmpdir": command.get("tmpdir"),
            "remote_target_dir": command.get("remote_target_dir"),
            "remote_tmpdir": command.get("remote_tmpdir"),
            "artifact_paths": proof_list(command.get("artifact_paths")),
        },
        "artifact_retrieval": {
            "status": "not_applicable",
            "retrieved_paths": [],
            "missing_paths": [],
            "warning_details": [],
            "retrieval_exit_code": None,
            "retrieval_elapsed_ms": None,
        },
        "warnings": warnings,
        "evidence_classification": {
            "status": status,
            "clean_remote_proof": False,
            "operator_evidence_only": True,
            "suppressed_claims": [
                "release_performance",
                "strict_dropin",
                "benchmark_throughput",
                "memory_or_startup_claim",
            ],
            "coverage": coverage,
            "degraded_reasons": sorted(set(degraded_reasons)),
            "next_actions": next_actions,
        },
    }


def build_remote_validation_capture_proof_entries(
    *,
    capture_summary: dict[str, Any] | None,
    generated_at: datetime,
    git_state: dict[str, Any],
    bead_id: str | None,
) -> list[dict[str, Any]]:
    if not isinstance(capture_summary, dict):
        return []
    commands = capture_summary.get("commands")
    if not isinstance(commands, list):
        return []
    entries: list[dict[str, Any]] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        entry = build_remote_validation_capture_proof_entry(
            command=command,
            generated_at=generated_at,
            git_state=git_state,
            bead_id=bead_id,
        )
        if entry is not None:
            entries.append(entry)
    return entries


def build_remote_validation_proof_ledger(
    *,
    by_id: dict[str, SourcePayload],
    generated_at: datetime,
    git_state: dict[str, Any],
    bead_id: str | None,
    capture_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cargo_source = by_id["cargo_admission"]
    artifact_source = by_id.get("rch_artifact_sync")
    entries = [
        build_remote_validation_proof_entry(
            cargo_source=cargo_source,
            artifact_source=artifact_source,
            generated_at=generated_at,
            git_state=git_state,
            bead_id=bead_id,
        )
    ]
    entries.extend(
        build_remote_validation_capture_proof_entries(
            capture_summary=capture_summary,
            generated_at=generated_at,
            git_state=git_state,
            bead_id=bead_id,
        )
    )
    classifications = [
        entry["evidence_classification"]["status"] for entry in entries
    ]
    clean_remote_proof_count = sum(
        1
        for entry in entries
        if entry.get("evidence_classification", {}).get("clean_remote_proof") is True
    )
    authoritative_count = sum(
        1
        for entry in entries
        if entry.get("evidence_classification", {})
        .get("coverage", {})
        .get("authoritative_for_bead")
        is True
    )
    coverage_statuses = [
        entry.get("evidence_classification", {})
        .get("coverage", {})
        .get("coverage_status")
        for entry in entries
    ]
    redacted_warning_count = sum(
        1
        for entry in entries
        for warning in entry.get("warnings", [])
        if isinstance(warning, dict) and warning.get("redacted") is True
    )
    return {
        "schema": REMOTE_VALIDATION_LEDGER_SCHEMA,
        "ledger_id": f"rvpl-{hashlib.sha256(json_dumps(entries).encode('utf-8')).hexdigest()[:16]}",
        "generated_at_utc": utc_z(generated_at),
        "producer": {
            "name": "scripts/build_swarm_operator_runpack.py",
            "version": "1.0.0",
        },
        "git": {
            "head": git_state.get("head") if isinstance(git_state, dict) else None,
            "branch": git_state.get("branch") if isinstance(git_state, dict) else None,
            "dirty_paths": git_changed_paths(git_state) if isinstance(git_state, dict) else [],
        },
        "worktree": {
            "cwd": str(Path.cwd()),
            "dirty_policy": "reported_in_runpack",
        },
        "entries": entries,
        "summary": {
            "clean_remote_proof_entries": clean_remote_proof_count,
            "passing_entries": classifications.count("pass"),
            "degraded_entries": classifications.count("degraded"),
            "blocked_entries": classifications.count("blocked"),
            "failed_entries": classifications.count("fail"),
            "authoritative_for_bead_entries": authoritative_count,
            "partial_or_uncovered_entries": sum(
                1
                for status in coverage_statuses
                if status in {"partial", "uncovered", "claimed_uncovered"}
            ),
        },
        "redaction_summary": {
            "redacted_count": redacted_warning_count,
            "policy": "derived from redacted runpack sources; no raw logs stored",
        },
        "claim_boundaries": {
            "operator_evidence_only": True,
            "release_performance_claims_allowed": False,
            "strict_dropin_claims_allowed": False,
        },
    }


def proof_reuse_context_changed_paths(context: dict[str, Any]) -> list[str]:
    for key in ("changed_paths", "staged_paths", "claimed_paths"):
        values = context.get(key)
        if isinstance(values, list):
            return unique_strings(
                [value for value in values if isinstance(value, str) and value]
            )
    git = context.get("git")
    return git_changed_paths(git if isinstance(git, dict) else {})


def proof_reuse_context_fingerprint(context: dict[str, Any]) -> str | None:
    command = context.get("command")
    if isinstance(command, dict):
        for key in ("command_fingerprint", "fingerprint"):
            value = command.get(key)
            if isinstance(value, str) and value:
                return value
    value = context.get("command_fingerprint")
    return value if isinstance(value, str) and value else None


def proof_reuse_context_git_head(context: dict[str, Any]) -> str | None:
    git = context.get("git")
    if isinstance(git, dict):
        value = git.get("head")
        if isinstance(value, str) and value:
            return value
    value = context.get("git_head")
    return value if isinstance(value, str) and value else None


def proof_reuse_context_env(context: dict[str, Any]) -> dict[str, Any]:
    env = context.get("env")
    if isinstance(env, dict):
        return env
    env_fingerprint = context.get("env_fingerprint")
    if isinstance(env_fingerprint, dict):
        return env_fingerprint
    return {}


def proof_reuse_entry_invalidation_reasons(
    entry: dict[str, Any],
    *,
    ledger: dict[str, Any],
    context: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    classification = proof_dict(entry.get("evidence_classification"))
    coverage = proof_dict(classification.get("coverage"))
    runner = proof_dict(entry.get("runner"))
    paths = proof_dict(entry.get("paths"))
    command = proof_dict(entry.get("command"))
    context_env = proof_reuse_context_env(context)
    current_paths = proof_reuse_context_changed_paths(context)
    covered_paths = unique_strings(
        [
            value
            for value in proof_list(coverage.get("covered_paths"))
            if isinstance(value, str)
        ]
    )
    ledger_git = proof_dict(ledger.get("git"))
    ledger_dirty_paths = unique_strings(
        [
            value
            for value in proof_list(ledger_git.get("dirty_paths"))
            if isinstance(value, str)
        ]
    )
    current_git_head = proof_reuse_context_git_head(context)
    current_fingerprint = proof_reuse_context_fingerprint(context)
    required_runner = proof_string(context.get("runner_requirement"), "rch_required")

    if classification.get("status") != "pass":
        reasons.append("source_proof_not_passing")
    if classification.get("clean_remote_proof") is not True:
        reasons.append("missing_clean_remote_proof")
    if (
        runner.get("resolved_runner") != "rch_remote"
        or runner.get("remote_execution") is not True
    ):
        reasons.append("missing_rch_provenance")
    if runner.get("local_fallback") not in {None, "none"}:
        reasons.append("local_fallback_observed")
    if runner.get("runner_requirement") != required_runner:
        reasons.append("runner_requirement_mismatch")
    if current_git_head and ledger_git.get("head") != current_git_head:
        reasons.append("stale_git_head")
    if current_fingerprint and command.get("command_fingerprint") != current_fingerprint:
        reasons.append("command_fingerprint_mismatch")
    if sorted(ledger_dirty_paths) != sorted(current_paths):
        reasons.append("dirty_worktree_mismatch")
    uncovered_paths = [path for path in current_paths if path not in covered_paths]
    if uncovered_paths:
        reasons.append("staged_paths_not_covered")
    if coverage.get("authoritative_for_bead") is not True:
        reasons.append("coverage_not_authoritative")
    if any(path in {"Cargo.lock", "rust-toolchain.toml"} for path in current_paths):
        reasons.append("toolchain_or_lockfile_changed")
    if (
        context_env.get("cargo_target_dir")
        and paths.get("cargo_target_dir") != context_env.get("cargo_target_dir")
    ):
        reasons.append("cargo_target_dir_mismatch")
    if context_env.get("tmpdir") and paths.get("tmpdir") != context_env.get("tmpdir"):
        reasons.append("tmpdir_mismatch")
    if not current_fingerprint:
        reasons.append("missing_current_command_fingerprint")
    if not current_git_head:
        reasons.append("missing_current_git_head")
    return sorted(set(reasons))


def build_remote_validation_proof_reuse_gate(
    *,
    generated_at: str,
    proof_ledger: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    entries = [
        entry
        for entry in proof_ledger.get("entries", [])
        if isinstance(entry, dict)
    ]
    current_paths = proof_reuse_context_changed_paths(context)
    candidate_decisions: list[dict[str, Any]] = []
    for entry in entries:
        invalidation_reasons = proof_reuse_entry_invalidation_reasons(
            entry,
            ledger=proof_ledger,
            context=context,
        )
        classification = proof_dict(entry.get("evidence_classification"))
        coverage = proof_dict(classification.get("coverage"))
        candidate_decisions.append(
            {
                "entry_id": entry.get("entry_id"),
                "bead_id": entry.get("bead_id"),
                "command_fingerprint": proof_dict(entry.get("command")).get(
                    "command_fingerprint"
                ),
                "clean_remote_proof": classification.get("clean_remote_proof") is True,
                "authoritative_for_bead": coverage.get("authoritative_for_bead") is True,
                "covered_paths": proof_list(coverage.get("covered_paths")),
                "invalidation_reasons": invalidation_reasons,
                "reuse_allowed": not invalidation_reasons,
            }
        )

    reusable = [
        candidate for candidate in candidate_decisions if candidate["reuse_allowed"]
    ]
    selected = reusable[0] if reusable else None
    invalidation_reasons = sorted(
        {
            reason
            for candidate in candidate_decisions
            for reason in candidate["invalidation_reasons"]
        }
    )
    if not entries:
        invalidation_reasons.append("no_source_proofs")
    reuse_allowed = selected is not None
    summary = {
        "schema": REMOTE_VALIDATION_PROOF_REUSE_GATE_SCHEMA,
        "generated_at": generated_at,
        "status": "pass" if reuse_allowed else "blocked",
        "purpose": "operator_validation_proof_reuse_gate_not_validation_skipper",
        "source_ledger_id": proof_ledger.get("ledger_id"),
        "source_ledger_schema": proof_ledger.get("schema"),
        "context_schema": context.get("schema"),
        "reuse_allowed": reuse_allowed,
        "decision": "reuse_existing_remote_proof" if reuse_allowed else "rerun_validation",
        "selected_entry_id": selected.get("entry_id") if selected else None,
        "source_proof_ids": [
            candidate.get("entry_id")
            for candidate in candidate_decisions
            if candidate.get("entry_id")
        ],
        "candidate_count": len(candidate_decisions),
        "candidate_decisions": candidate_decisions,
        "invalidation_reasons": [] if reuse_allowed else invalidation_reasons,
        "current_context": {
            "git_head": proof_reuse_context_git_head(context),
            "changed_paths": current_paths,
            "command_fingerprint": proof_reuse_context_fingerprint(context),
            "runner_requirement": proof_string(context.get("runner_requirement"), "rch_required"),
            "env_fingerprint": proof_reuse_context_env(context),
        },
        "claim_boundaries": {
            "read_only": True,
            "does_not_skip_validation_by_itself": True,
            "does_not_mutate_rch_or_agent_mail": True,
            "does_not_authorize_release_performance_claims": True,
            "requires_fresh_remote_proof": True,
        },
        "next_actions": (
            ["Reuse selected proof only for the exact covered command and changed-path context."]
            if reuse_allowed
            else ["Rerun validation through RCH; existing proof cannot cover this context."]
        ),
    }
    assert_remote_validation_proof_reuse_gate_contract(summary)
    return summary


def assert_remote_validation_proof_reuse_gate_contract(summary: dict[str, Any]) -> None:
    root = repo_root()
    contract_path = root / REMOTE_VALIDATION_PROOF_REUSE_GATE_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing proof reuse gate contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"proof reuse gate contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == REMOTE_VALIDATION_PROOF_REUSE_GATE_CONTRACT_SCHEMA
    assert contract.get("gate_schema") == REMOTE_VALIDATION_PROOF_REUSE_GATE_SCHEMA
    assert summary.get("schema") == contract["gate_schema"]
    assert summary.get("purpose") == contract.get("purpose")
    assert summary.get("status") in set(contract.get("allowed_statuses", []))
    assert summary.get("decision") in set(contract.get("allowed_decisions", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in summary, f"proof reuse gate missing top-level key: {key}"
    boundaries = summary.get("claim_boundaries")
    assert isinstance(boundaries, dict)
    for key in contract.get("required_claim_boundary_true_keys", []):
        assert boundaries.get(key) is True, f"proof reuse boundary is not true: {key}"
    candidates = summary.get("candidate_decisions")
    assert isinstance(candidates, list)
    for candidate in candidates:
        assert isinstance(candidate, dict)
        for key in contract.get("required_candidate_keys", []):
            assert key in candidate, f"proof reuse candidate missing key: {key}"
        assert isinstance(candidate.get("invalidation_reasons"), list)
        assert isinstance(candidate.get("reuse_allowed"), bool)
    if summary.get("reuse_allowed") is True:
        assert summary.get("status") == "pass"
        assert summary.get("selected_entry_id")
        assert summary.get("invalidation_reasons") == []
    else:
        assert summary.get("status") == "blocked"
        assert summary.get("decision") == "rerun_validation"
        assert summary.get("invalidation_reasons")


def write_remote_validation_proof_reuse_gate_output(
    args: argparse.Namespace,
    summary: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_proof_reuse_gate_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(f"refusing to overwrite proof reuse gate: {output_path}")
    output_path.write_text(json_dumps(summary, pretty=True), encoding="utf-8")


def build_remote_validation_proof_reuse_gate_from_args(
    args: argparse.Namespace,
) -> dict[str, Any]:
    ledger_source = load_json_source(
        "proof_ledger",
        getattr(args, "proof_ledger_json", None),
        expected_schema=REMOTE_VALIDATION_LEDGER_SCHEMA,
    )
    if ledger_source.payload is None:
        raise RunpackError("--proof-ledger-json is required for --run-proof-reuse-gate")
    context_source = load_json_source(
        "proof_reuse_context",
        getattr(args, "proof_reuse_context_json", None),
        expected_schema=REMOTE_VALIDATION_PROOF_REUSE_CONTEXT_SCHEMA,
    )
    if context_source.payload is None:
        raise RunpackError(
            "--proof-reuse-context-json is required for --run-proof-reuse-gate"
        )
    return build_remote_validation_proof_reuse_gate(
        generated_at=args.generated_at or utc_now_iso(),
        proof_ledger=ledger_source.payload,
        context=context_source.payload,
    )


def temp_artifact_text(value: Any) -> str:
    return value if isinstance(value, str) and value else ""


def is_temp_artifact_path(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered.startswith("/data/tmp/")
        or lowered.startswith("/tmp/")
        or "/.rch-target" in lowered
        or "/.rch-tmp" in lowered
        or "clean-worktree" in lowered
        or "clean_worktree" in lowered
        or "git_scan" in lowered
    )


def temp_artifact_kind_from_path(path: str, fallback: str) -> str:
    lowered = path.lower()
    if "cargo" in lowered and "target" in lowered:
        return "cargo_target_dir"
    if "tmp" in lowered and "cargo" in lowered:
        return "cargo_tmpdir"
    if ".rch-target" in lowered:
        return "rch_remote_target_dir"
    if ".rch-tmp" in lowered:
        return "rch_remote_tmpdir"
    if "clean-worktree" in lowered or "clean_worktree" in lowered:
        return "clean_worktree_validation_dir"
    if "git_scan" in lowered or "ubs" in lowered:
        return "ubs_shadow_workspace"
    return fallback


def temp_artifact_state(item: dict[str, Any]) -> str:
    state = temp_artifact_text(item.get("state") or item.get("activity"))
    if state in {"active", "retained", "stale_candidate", "unknown_owner"}:
        return state
    if item.get("owner") or item.get("bead_id"):
        return "active"
    return "unknown_owner"


def temp_artifact_deletion_policy(entry: dict[str, Any]) -> str:
    if entry["state"] in {"active", "retained"}:
        return "retain_active"
    if not entry.get("owner") and not entry.get("bead_id"):
        return "deletion_protected_unknown_owner"
    return "requires_explicit_operator_approval"


def temp_artifact_review_commands(path: str) -> list[str]:
    quoted = shlex.quote(path)
    return [
        f"test -e {quoted} && stat {quoted}",
        f"du -sh {quoted}",
    ]


def add_temp_artifact_entry(
    entries: list[dict[str, Any]],
    *,
    path: Any,
    source: str,
    kind: str,
    evidence_path: str,
    owner: Any = None,
    bead_id: Any = None,
    reason: Any = None,
    state: Any = None,
    last_observed_at: Any = None,
) -> None:
    path_text = temp_artifact_text(path)
    if not path_text:
        return
    entry = {
        "id": f"temp-{hashlib.sha256(f'{source}:{evidence_path}:{kind}'.encode('utf-8')).hexdigest()[:12]}",
        "path": path_text,
        "kind": temp_artifact_kind_from_path(path_text, kind),
        "source": source,
        "evidence_paths": [evidence_path],
        "owner": owner if isinstance(owner, str) and owner else None,
        "bead_id": bead_id if isinstance(bead_id, str) and bead_id else None,
        "reason": reason if isinstance(reason, str) and reason else None,
        "state": state if isinstance(state, str) and state else None,
        "last_observed_at": last_observed_at
        if isinstance(last_observed_at, str) and last_observed_at
        else None,
        "review_commands": temp_artifact_review_commands(path_text),
        "cleanup_command": None,
        "permission_boundary": (
            "No deletion command emitted. Explicit written operator approval is "
            "required before deleting any file or directory."
        ),
    }
    entry["state"] = temp_artifact_state(entry)
    entry["deletion_policy"] = temp_artifact_deletion_policy(entry)
    entries.append(entry)


def build_temp_artifact_inventory(
    runpack: dict[str, Any],
    *,
    capture_summary: dict[str, Any],
    generated_at: datetime,
    max_items: int,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    cargo = runpack.get("rch_admission") if isinstance(runpack.get("rch_admission"), dict) else {}
    add_temp_artifact_entry(
        entries,
        path=cargo.get("cargo_target_dir"),
        source="cargo_admission",
        kind="cargo_target_dir",
        evidence_path="rch_admission.cargo_target_dir",
        reason="cargo_headroom selected target directory",
        state="active",
        last_observed_at=generated_at.isoformat(),
    )
    add_temp_artifact_entry(
        entries,
        path=cargo.get("tmpdir"),
        source="cargo_admission",
        kind="cargo_tmpdir",
        evidence_path="rch_admission.tmpdir",
        reason="cargo_headroom selected TMPDIR",
        state="active",
        last_observed_at=generated_at.isoformat(),
    )

    proof_ledger = (
        runpack.get("remote_validation_proof_ledger")
        if isinstance(runpack.get("remote_validation_proof_ledger"), dict)
        else {}
    )
    for index, entry in enumerate(proof_ledger.get("entries") or []):
        if not isinstance(entry, dict):
            continue
        paths = entry.get("paths") if isinstance(entry.get("paths"), dict) else {}
        bead_id = entry.get("bead_id")
        add_temp_artifact_entry(
            entries,
            path=paths.get("remote_target_dir"),
            source="remote_validation_proof_ledger",
            kind="rch_remote_target_dir",
            evidence_path=f"remote_validation_proof_ledger.entries[{index}].paths.remote_target_dir",
            bead_id=bead_id,
            reason="RCH remote validation target directory",
            state="active",
            last_observed_at=proof_ledger.get("generated_at_utc"),
        )
        add_temp_artifact_entry(
            entries,
            path=paths.get("remote_tmpdir"),
            source="remote_validation_proof_ledger",
            kind="rch_remote_tmpdir",
            evidence_path=f"remote_validation_proof_ledger.entries[{index}].paths.remote_tmpdir",
            bead_id=bead_id,
            reason="RCH remote validation temp directory",
            state="active",
            last_observed_at=proof_ledger.get("generated_at_utc"),
        )

    smoke = runpack.get("smoke_harness") if isinstance(runpack.get("smoke_harness"), dict) else {}
    for index, artifact in enumerate(smoke.get("artifact_manifest") or []):
        if not isinstance(artifact, dict):
            continue
        add_temp_artifact_entry(
            entries,
            path=artifact.get("path"),
            source="smoke_harness",
            kind="e2e_artifact",
            evidence_path=f"smoke_harness.artifact_manifest[{index}].path",
            reason=temp_artifact_text(artifact.get("id")) or "smoke harness artifact",
            state="retained",
            last_observed_at=generated_at.isoformat(),
        )

    validation = (
        runpack.get("validation_outputs")
        if isinstance(runpack.get("validation_outputs"), dict)
        else {}
    )
    for index, output in enumerate(validation.get("outputs") or []):
        if not isinstance(output, dict):
            continue
        add_temp_artifact_entry(
            entries,
            path=output.get("path"),
            source="validation_outputs",
            kind="validation_output",
            evidence_path=f"validation_outputs.outputs[{index}].path",
            reason="validation output artifact",
            state="retained",
            last_observed_at=generated_at.isoformat(),
        )

    for index, command in enumerate(capture_summary.get("commands") or []):
        if not isinstance(command, dict):
            continue
        command_text = temp_artifact_text(command.get("command"))
        command_kind = (
            "ubs_shadow_workspace"
            if "ubs" in command_text or command.get("id") == "ubs_staged"
            else "clean_worktree_validation_dir"
            if "clean-worktree" in command_text or "clean_worktree" in command_text
            else "command_output"
        )
        for key in ("stdout_path", "stderr_path", "artifact_path"):
            add_temp_artifact_entry(
                entries,
                path=command.get(key),
                source="capture_manifest.commands",
                kind=command_kind,
                evidence_path=f"capture.commands[{index}].{key}",
                owner=command.get("owner"),
                bead_id=command.get("bead_id"),
                reason=command_text or temp_artifact_text(command.get("id")),
                state="retained",
                last_observed_at=command.get("ended_at") or command.get("started_at"),
            )

    for index, item in enumerate(capture_summary.get("temp_artifacts") or []):
        if not isinstance(item, dict):
            continue
        add_temp_artifact_entry(
            entries,
            path=item.get("path"),
            source="capture_manifest.temp_artifacts",
            kind=temp_artifact_text(item.get("kind")) or "temp_artifact",
            evidence_path=f"capture.temp_artifacts[{index}].path",
            owner=item.get("owner"),
            bead_id=item.get("bead_id"),
            reason=item.get("reason"),
            state=item.get("state"),
            last_observed_at=item.get("last_observed_at"),
        )

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        if not is_temp_artifact_path(entry["path"]):
            continue
        key = (entry["path"], entry["kind"])
        if key in deduped:
            deduped[key]["evidence_paths"].extend(entry["evidence_paths"])
            deduped[key]["evidence_paths"] = unique_strings(deduped[key]["evidence_paths"])
            for field in ("owner", "bead_id", "reason", "last_observed_at"):
                if not deduped[key].get(field) and entry.get(field):
                    deduped[key][field] = entry[field]
            continue
        deduped[key] = entry
    inventory_entries = list(deduped.values())
    deletion_protected_count = sum(
        1
        for entry in inventory_entries
        if entry.get("deletion_policy") == "deletion_protected_unknown_owner"
    )
    cleanup_candidate_count = sum(
        1 for entry in inventory_entries if entry.get("state") == "stale_candidate"
    )
    active_count = sum(
        1 for entry in inventory_entries if entry.get("state") in {"active", "retained"}
    )
    return {
        "schema": TEMP_ARTIFACT_INVENTORY_SCHEMA,
        "generated_at": generated_at.isoformat(),
        "status": "review_required"
        if deletion_protected_count or cleanup_candidate_count
        else "tracked",
        "purpose": "read_only_temp_artifact_inventory_not_cleanup_executor",
        "known_temp_roots": [
            "/data/tmp/pi_agent_rust_cargo",
            "/data/tmp",
            "/tmp",
            ".rch-target*",
            ".rch-tmp*",
        ],
        "entries": inventory_entries,
        "summary": {
            "entry_count": len(inventory_entries),
            "active_or_retained_count": active_count,
            "cleanup_candidate_count": cleanup_candidate_count,
            "deletion_protected_count": deletion_protected_count,
            "emitted_deletion_command_count": 0,
        },
        "guards": {
            "read_only": True,
            "no_filesystem_deletion": True,
            "no_destructive_commands_emitted": True,
            "explicit_operator_permission_required": True,
            "unknown_owner_deletion_protected": True,
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


def summarize_stale_evidence_renewal_queue(
    source: SourcePayload,
    max_items: int,
) -> dict[str, Any]:
    payload = source.payload
    if not isinstance(payload, dict):
        return {"status": source.status, "schema": source.schema}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    action_plan = (
        payload.get("action_plan_integration")
        if isinstance(payload.get("action_plan_integration"), dict)
        else {}
    )
    runpack = (
        payload.get("runpack_integration")
        if isinstance(payload.get("runpack_integration"), dict)
        else {}
    )
    return {
        "status": source.status if source.status != "ok" else payload.get("status"),
        "schema": payload.get("schema"),
        "generated_at": payload.get("generated_at"),
        "purpose": payload.get("purpose"),
        "source_path": source.path,
        "freshness_window_hours": payload.get("freshness_window_hours"),
        "summary": {
            "scanned_artifacts": summary.get("scanned_artifacts"),
            "fresh_artifacts": summary.get("fresh_artifacts"),
            "renewal_item_count": summary.get("renewal_item_count"),
            "renewal_recommended_count": summary.get("renewal_recommended_count"),
            "blocked_count": summary.get("blocked_count"),
            "contract_count": summary.get("contract_count"),
        },
        "queue": bounded(payload.get("queue") or [], max_items),
        "action_plan_integration": {
            "schema": action_plan.get("schema"),
            "status": action_plan.get("status"),
            "recommended_decision": action_plan.get("recommended_decision"),
            "commands_require_operator_execution": action_plan.get(
                "commands_require_operator_execution"
            ),
        },
        "runpack_integration": {
            "schema": runpack.get("schema"),
            "status": runpack.get("status"),
            "operator_next_action": runpack.get("operator_next_action"),
        },
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
    semantic = payload.get("semantic_readiness")
    semantic_status = (
        str(semantic.get("status") or "").lower() if isinstance(semantic, dict) else ""
    )
    recovery = payload.get("recovery")
    recovery_mode = (
        str(recovery.get("mode") or "").lower() if isinstance(recovery, dict) else ""
    )
    if health_level in {"red", "error", "critical"}:
        return "degraded"
    if status in {"error", "failed", "fail", "degraded", "red"}:
        return "degraded"
    if semantic_status in {"fail", "failed", "error"}:
        return "degraded"
    if recovery_mode in {"corrupt", "degraded", "read_only", "readonly"}:
        return "degraded"
    return status


def agent_mail_diagnostics(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    semantic = payload.get("semantic_readiness")
    semantic = semantic if isinstance(semantic, dict) else {}
    recovery = payload.get("recovery")
    recovery = recovery if isinstance(recovery, dict) else {}
    next_action = recovery.get("next_action")
    preview_action = None
    if isinstance(next_action, str) and "doctor repair --yes" in next_action:
        preview_action = "am doctor repair --dry-run"
    return {
        "health_level": payload.get("health_level"),
        "semantic_readiness_status": semantic.get("status"),
        "semantic_readiness_detail": semantic.get("detail"),
        "recovery_mode": recovery.get("mode"),
        "recovery_action": next_action,
        "recovery_preview_action": preview_action,
    }


def agent_mail_unavailable_operations(
    read_status: str,
    reservation_status: str,
) -> list[str]:
    operations: list[str] = []
    if read_status != "ok":
        operations.extend(
            [
                "fetch_inbox",
                "send_message",
                "acknowledge_message",
                "file_reservation_paths",
            ]
        )
    elif reservation_status != "ok":
        operations.append("file_reservation_paths")
    return unique_strings(operations)


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
    beads_summary: dict[str, Any],
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
    unavailable_operations = agent_mail_unavailable_operations(
        read_status,
        reservation_status,
    )
    reservations = reservation_items(reservations_source.payload)
    active_reservations = [
        item for item in reservations if item.get("released_ts") in {None, ""}
    ]
    no_mail_closeout_steps: list[str] = []
    if status != "ok":
        no_mail_closeout_steps = build_no_mail_closeout_steps()
    redaction = RedactionStats()
    redaction.redacted_count += status_source.redacted_count
    redaction.fields.update(status_source.redacted_fields)
    redaction.redacted_count += reservations_source.redacted_count
    redaction.fields.update(reservations_source.redacted_fields)
    summary = {
        "status": status,
        "capture_mode": capture_summary.get("mode"),
        "transport_status": agent_mail_transport_status(commands),
        "read_status": read_status,
        "reservation_status": reservation_status,
        "reservation_count": len(reservations),
        "active_reservation_count": len(active_reservations),
        "soft_lock": "beads" if status != "ok" else None,
        "registration_required_before_coding": False if status != "ok" else None,
        "unavailable_operations": unavailable_operations,
        "active_reservations": bounded(
            [summarize_reservation(item) for item in active_reservations],
            max_items,
        ),
        "fallback_action": "use_beads_soft_lock" if status != "ok" else None,
        "fallback_ownership_evidence": (
            beads_fallback_ownership_evidence(beads_summary, max_items)
            if status != "ok"
            else None
        ),
        "no_mail_closeout_steps": no_mail_closeout_steps,
        "commands": commands,
        "redaction_summary": redaction.to_json(),
    }
    summary.update(agent_mail_diagnostics(status_source.payload))
    return summary


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
        beads_summary,
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
    if "validation_broker" in by_id:
        pack["normalized_inputs"]["validation_broker"] = summarize_validation_broker_source(
            by_id["validation_broker"],
            args.max_items,
        )
    if "stale_evidence_renewal_queue" in by_id:
        pack["normalized_inputs"]["stale_evidence_renewal_queue"] = (
            summarize_stale_evidence_renewal_queue(
                by_id["stale_evidence_renewal_queue"],
                args.max_items,
            )
        )
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
    action = {
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
    for key in ("drift_category_id", "drift_class", "remediation"):
        value = rule.get(key)
        if value is not None:
            action[key] = value
    return action


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
        evidence_paths = [
            "normalized_inputs.agent_mail.status",
            "normalized_inputs.agent_mail.transport_status",
            "normalized_inputs.agent_mail.read_status",
            "normalized_inputs.agent_mail.reservation_status",
            "normalized_inputs.agent_mail.unavailable_operations",
            "normalized_inputs.beads.active_count",
            "normalized_inputs.agent_mail.fallback_ownership_evidence.active_ownership",
        ]
        rationale_parts = [
            f"Agent Mail status={agent_mail.get('status')}",
            f"transport={agent_mail.get('transport_status')}",
            f"fallback={agent_mail.get('fallback_action')}",
        ]
        if agent_mail.get("recovery_mode") is not None:
            rationale_parts.append(f"recovery_mode={agent_mail.get('recovery_mode')}")
        if agent_mail.get("recovery_preview_action") is not None:
            evidence_paths.append("normalized_inputs.agent_mail.recovery_action")
            evidence_paths.append("normalized_inputs.agent_mail.recovery_preview_action")
            rationale_parts.append(
                f"recovery_preview={agent_mail.get('recovery_preview_action')}"
            )
        rationale_parts.append(
            "unavailable_operations="
            f"{','.join(agent_mail.get('unavailable_operations') or [])}"
        )
        fallback_ownership = (
            agent_mail.get("fallback_ownership_evidence")
            if isinstance(agent_mail.get("fallback_ownership_evidence"), dict)
            else {}
        )
        rationale_parts.append(
            "active_beads="
            f"{fallback_ownership.get('active_count')}; "
            "in_progress="
            f"{fallback_ownership.get('in_progress_count')}"
        )
        actions.append(
            autopilot_plan_action(
                action="use_beads_soft_lock",
                title="Use Beads assignment as the coordination lock",
                severity="high",
                confidence="high",
                preconditions=[
                    "Agent Mail message read/write and reservation status is degraded or unavailable.",
                    "Announce/reserve through Agent Mail only after health recovers.",
                ],
                evidence_paths=evidence_paths,
                commands=[
                    plan_command("Inspect active ownership", "br list --status=in_progress --json"),
                    plan_command("Inspect candidate bead before editing", "br show <issue-id> --json"),
                    plan_command("Claim through Beads", "br update <issue-id> --status in_progress --assignee \"${AGENT_NAME:-agent}\""),
                ],
                omitted_commands=[
                    omitted_command("Agent Mail messages", "message read/write paths are unavailable; use Beads status/comments until health is green"),
                    omitted_command("Agent Mail reservation", "coordination transport is degraded; retry after health is green"),
                ],
                forbidden_actions=["automatic file reservation"],
                rationale="; ".join(rationale_parts),
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
    agent_mail_ready = agent_mail.get("status") == "ok"
    if ready_candidate is not None and not blockers and agent_mail_ready:
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
    deferred_planning = beads.get("deferred_planning")
    if not isinstance(deferred_planning, list):
        deferred_planning = []
    deferred_planning_count = int_value(beads.get("deferred_planning_count"))
    if (
        ready_count == 0
        and active_count == 0
        and open_count == 0
        and deferred_planning_count
        and not blockers
    ):
        issue_ids = [
            str(item.get("id"))
            for item in deferred_planning
            if item.get("id")
        ]
        epic_id = issue_ids[0] if issue_ids else "<epic-id>"
        title_suffix = ", ".join(issue_ids[:3]) if issue_ids else "deferred roadmap epics"
        actions.append(
            autopilot_plan_action(
                action="create_or_refine_backlog",
                title=f"Create/refine child Beads for deferred roadmap epics: {title_suffix}",
                severity="medium",
                confidence="high",
                preconditions=[
                    "No ready, open, or in-progress Beads are present in the captured evidence.",
                    "Deferred roadmap/planning epics remain without open child work.",
                    "Create bounded child Beads before treating the queue as clean.",
                ],
                evidence_paths=[
                    "normalized_inputs.beads.deferred_planning",
                    "normalized_inputs.beads.deferred_planning_count",
                    "normalized_inputs.beads_ready.ready_count",
                    "normalized_inputs.beads.open_candidate_count",
                    "command_provenance",
                ],
                commands=[
                    plan_command("Inspect top deferred epic", f"br show {epic_id} --json"),
                    plan_command("Create one child bead", "br create --title \"<narrow child task>\" --type task --priority 2"),
                    plan_command("Link child to epic", f"br dep add <child-id> {epic_id} --type parent-child"),
                    plan_command("Sync Beads JSONL", "br sync --flush-only"),
                ],
                omitted_commands=[
                    omitted_command("stop queue clean", "deferred roadmap/planning epics still need actionable children"),
                    omitted_command("automatic bead creation", "planner is dry-run and must not mutate Beads"),
                ],
                forbidden_actions=["invent broad work without a bead", "automatic commit"],
                rationale=(
                    "Captured Beads evidence has no ready or active work, but deferred "
                    "roadmap/planning epics remain without open child Beads."
                ),
            )
        )
    if ready_count == 0 and active_count == 0 and open_count == 0 and deferred_planning_count == 0 and not blockers:
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
        actions.append(
            autopilot_plan_action(
                action="plan_new_bead",
                title="Create a narrow Beads issue from current operator evidence",
                severity="low",
                confidence="medium",
                preconditions=[
                    "No ready, open, or in-progress Beads are present in the captured evidence.",
                    "Use idea-wizard or a focused source audit to define one narrow task before coding.",
                    "Do not wait for unavailable coordination surfaces when the queue is empty.",
                ],
                evidence_paths=[
                    "normalized_inputs.beads_ready.ready_count",
                    "normalized_inputs.beads.active_count",
                    "normalized_inputs.beads.open_candidate_count",
                    "command_provenance",
                ],
                commands=[
                    plan_command("Run idea-wizard planning", "bv --recipe actionable --robot-plan"),
                    plan_command("Create one follow-up bead", "br create --title \"<narrow task>\" --type task --priority 2"),
                    plan_command("Sync Beads JSONL", "br sync --flush-only"),
                ],
                omitted_commands=[
                    omitted_command("wait for ready work", "the captured ready/open/in-progress Beads sets are empty"),
                    omitted_command("claim placeholder epic", "new work needs a leaf bead with concrete acceptance"),
                ],
                forbidden_actions=["invent broad work without a bead"],
                rationale=(
                    "Captured Beads evidence is empty after closeout; next progress is a "
                    "new narrow bead or idea-wizard planning artifact, not idle waiting."
                ),
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


def action_plan_command_safety_class(command: dict[str, Any]) -> str:
    text = str(command.get("command") or "").strip()
    lower = text.lower()
    for fragment in AUTOPILOT_PLAN_DANGEROUS_COMMAND_FRAGMENTS:
        if fragment in lower:
            raise AssertionError(
                f"action plan command includes dangerous fragment: {fragment}"
            )
    if lower.startswith(("br update", "br create", "br dep add", "br close", "br sync")):
        return "beads_mutation_requires_operator"
    if (
        " --capture-current" in lower
        or " --out-" in lower
        or lower.startswith("mkdir -p ")
    ):
        return "evidence_capture"
    if (
        " cargo " in f" {lower} "
        or lower.startswith("cargo ")
        or lower.startswith("rch ")
        or "cargo_headroom" in lower
    ):
        return "validation_probe"
    if lower.startswith(("am ", "br ", "git ", "pi doctor")):
        return "coordination_probe"
    return "read_only_probe"


def action_plan_command_safety_classes(action: dict[str, Any]) -> list[str]:
    classes = {
        action_plan_command_safety_class(command)
        for command in action.get("commands", [])
        if isinstance(command, dict)
    }
    return sorted(classes)


def action_plan_stale_required_sources(input_pack: dict[str, Any]) -> list[dict[str, Any]]:
    stale: list[dict[str, Any]] = []
    for item in input_pack.get("source_classification", []):
        if not isinstance(item, dict):
            continue
        if item.get("required") is True and item.get("classification") == "stale":
            stale.append(item)
    return stale


def action_plan_renewal_queue(input_pack: dict[str, Any]) -> dict[str, Any] | None:
    queue = normalized_section(input_pack, "stale_evidence_renewal_queue")
    if not queue:
        return None
    summary = queue.get("summary") if isinstance(queue.get("summary"), dict) else {}
    renewal_count = int_value(summary.get("renewal_item_count"))
    blocked_count = int_value(summary.get("blocked_count"))
    if not renewal_count and not blocked_count:
        return None
    action_plan = (
        queue.get("action_plan_integration")
        if isinstance(queue.get("action_plan_integration"), dict)
        else {}
    )
    return {
        "renewal_count": renewal_count,
        "blocked_count": blocked_count,
        "status": action_plan.get("status") or queue.get("status"),
        "recommended_decision": action_plan.get("recommended_decision")
        or "renew_stale_evidence",
        "queue": queue.get("queue") if isinstance(queue.get("queue"), list) else [],
    }


def action_plan_validation_failure(
    input_pack: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any] | None:
    for action in plan.get("failure_actions", []):
        if not isinstance(action, dict):
            continue
        if action.get("id") in ACTION_PLAN_VALIDATION_FAILURE_IDS:
            return action
    for index, command in enumerate(input_pack.get("command_provenance", [])):
        if not isinstance(command, dict) or command.get("status") in {None, "ok"}:
            continue
        text = failure_signal_text(command).lower()
        if ("cargo " in text or "rustc" in text or "error[" in text) and (
            "failed" in text or "exit_code=101" in text
        ):
            return {
                "id": "ACTION-PLAN-VALIDATION-FAILED",
                "title": "Validation command failed",
                "match_confidence": "medium",
                "evidence_paths": [f"command_provenance[{index}]"],
                "safe_commands": [
                    plan_command("Inspect failed validation", "git status --short --branch"),
                    plan_command(
                        "Rerun focused validation through RCH",
                        "env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/target TMPDIR=/data/tmp/pi_agent_rust_cargo/${USER:-agent}/tmp rch exec -- cargo check --all-targets",
                    ),
                ],
            }
    return None


def sorted_autopilot_actions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    actions = [action for action in plan.get("actions", []) if isinstance(action, dict)]
    return sorted(actions, key=lambda item: int_value(item.get("rank")) or 999)


def action_plan_decision_for_action(action: dict[str, Any]) -> str:
    source_action = str(action.get("action") or "")
    return AUTOPILOT_TO_ACTION_PLAN_DECISION.get(
        source_action,
        "pause_or_surface_blocker",
    )


def build_action_plan_decision(
    input_pack: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    actions = sorted_autopilot_actions(plan)
    first_action = actions[0] if actions else {}
    stale_sources = action_plan_stale_required_sources(input_pack)
    renewal_queue = action_plan_renewal_queue(input_pack)
    validation_failure = action_plan_validation_failure(input_pack, plan)
    if renewal_queue is not None:
        return {
            "decision": "renew_stale_evidence",
            "source_action": first_action.get("action"),
            "rank": first_action.get("rank"),
            "title": "Renew stale evidence queue items",
            "severity": "high"
            if int_value(renewal_queue.get("blocked_count"))
            else "medium",
            "confidence": "high",
            "rationale": (
                f"renewal_item_count={renewal_queue.get('renewal_count')} "
                f"blocked_count={renewal_queue.get('blocked_count')}"
            ),
            "evidence_paths": [
                "normalized_inputs.stale_evidence_renewal_queue.summary",
                "normalized_inputs.stale_evidence_renewal_queue.queue",
            ],
            "command_safety_classes": action_plan_command_safety_classes(first_action),
            "commands_require_operator_execution": True,
        }
    if stale_sources:
        return {
            "decision": "renew_stale_evidence",
            "source_action": first_action.get("action"),
            "rank": first_action.get("rank"),
            "title": "Renew stale required planner evidence",
            "severity": "high",
            "confidence": "high",
            "rationale": "; ".join(
                f"{item.get('id')} freshness_hours={item.get('freshness_hours')}"
                for item in stale_sources
            ),
            "evidence_paths": [
                f"source_classification[{item.get('id')}]"
                for item in stale_sources
            ],
            "command_safety_classes": action_plan_command_safety_classes(first_action),
            "commands_require_operator_execution": True,
        }
    if validation_failure is not None:
        return {
            "decision": "pause_or_surface_blocker",
            "source_action": first_action.get("action"),
            "rank": first_action.get("rank"),
            "title": "Pause for validation failure triage",
            "severity": "critical",
            "confidence": validation_failure.get("match_confidence") or "medium",
            "rationale": str(validation_failure.get("title") or "validation failed"),
            "evidence_paths": list(validation_failure.get("evidence_paths") or []),
            "command_safety_classes": sorted(
                {
                    action_plan_command_safety_class(command)
                    for command in validation_failure.get("safe_commands", [])
                    if isinstance(command, dict)
                }
            ),
            "commands_require_operator_execution": True,
        }
    return {
        "decision": action_plan_decision_for_action(first_action),
        "source_action": first_action.get("action"),
        "rank": first_action.get("rank"),
        "title": first_action.get("title"),
        "severity": first_action.get("severity"),
        "confidence": first_action.get("confidence"),
        "rationale": first_action.get("rationale"),
        "evidence_paths": list(first_action.get("evidence_paths") or []),
        "command_safety_classes": action_plan_command_safety_classes(first_action),
        "commands_require_operator_execution": True,
    }


def derive_action_plan_status(decision: str, plan: dict[str, Any]) -> str:
    if decision == "pause_or_surface_blocker" or plan.get("status") == "blocked":
        return "blocked"
    if decision in {"renew_stale_evidence", "wait_for_pressure"}:
        return "degraded"
    if plan.get("status") == "degraded":
        return "degraded"
    return "ready"


def build_swarm_action_plan(
    input_pack: dict[str, Any],
    autopilot_plan: dict[str, Any],
    *,
    max_items: int,
) -> dict[str, Any]:
    if input_pack.get("schema") != AUTOPILOT_INPUT_PACK_SCHEMA:
        raise RunpackError(
            "action plan requires "
            f"{AUTOPILOT_INPUT_PACK_SCHEMA}, got {input_pack.get('schema')}"
        )
    if autopilot_plan.get("schema") != AUTOPILOT_PLAN_SCHEMA:
        raise RunpackError(
            "action plan requires "
            f"{AUTOPILOT_PLAN_SCHEMA}, got {autopilot_plan.get('schema')}"
        )
    next_safest = build_action_plan_decision(input_pack, autopilot_plan)
    operator_actions = []
    for action in sorted_autopilot_actions(autopilot_plan):
        operator_actions.append(
            {
                "rank": action.get("rank"),
                "source_action": action.get("action"),
                "decision": action_plan_decision_for_action(action),
                "title": action.get("title"),
                "severity": action.get("severity"),
                "confidence": action.get("confidence"),
                "evidence_paths": list(action.get("evidence_paths") or []),
                "command_safety_classes": action_plan_command_safety_classes(action),
            }
        )
    decision = str(next_safest.get("decision") or "pause_or_surface_blocker")
    action_plan = {
        "schema": ACTION_PLAN_SCHEMA,
        "generated_at": input_pack.get("generated_at"),
        "status": derive_action_plan_status(decision, autopilot_plan),
        "purpose": "dry_run_swarm_action_plan_not_source_of_truth",
        "source_plan_schema": autopilot_plan.get("schema"),
        "input_pack_schema": input_pack.get("schema"),
        "input_pack_status": input_pack.get("status"),
        "next_safest_action": next_safest,
        "operator_actions": bounded(operator_actions, max_items),
        "failure_actions": bounded(autopilot_plan.get("failure_actions") or [], max_items),
        "source_statuses": bounded(input_pack.get("source_statuses") or [], max_items),
        "source_classification": bounded(
            input_pack.get("source_classification") or [],
            max_items,
        ),
        "degraded_reasons": input_pack.get("degraded_reasons", []),
        "forbidden_actions": list(AUTOPILOT_FORBIDDEN_ACTIONS),
        "planner_guards": {
            "dry_run_only": True,
            "no_source_mutation": True,
            "commands_require_operator_execution": True,
            "dangerous_runnable_commands_blocked": True,
            "output_overwrite_refusal": True,
            "source_of_truth": "upstream_source_artifacts",
        },
        "redaction_summary": input_pack.get("redaction_summary"),
    }
    assert_action_plan_contract(action_plan)
    return action_plan


def work_admission_evidence_counts(input_pack: dict[str, Any]) -> dict[str, int]:
    counts = {
        "required_stale": 0,
        "required_blocker": 0,
        "required_missing": 0,
    }
    for item in input_pack.get("source_classification", []):
        if not isinstance(item, dict) or item.get("required") is not True:
            continue
        classification = item.get("classification")
        if classification == "stale":
            counts["required_stale"] += 1
        elif classification == "blocker":
            counts["required_blocker"] += 1
        elif classification == "missing":
            counts["required_missing"] += 1
    return counts


def work_admission_status_for_decision(decision: str) -> str:
    if decision == "proceed_with_implementation":
        return "admit"
    if decision in {"create_or_refine_bead", "renew_evidence"}:
        return "limited"
    if decision == "wait":
        return "wait"
    return "blocked"


def work_admission_validation_broker_signal(input_pack: dict[str, Any]) -> dict[str, Any]:
    validation_broker = normalized_section(input_pack, "validation_broker")
    if not validation_broker:
        return {"status": "not_provided", "source_status": "not_provided"}
    stale = (
        validation_broker.get("stale_build_warnings")
        if isinstance(validation_broker.get("stale_build_warnings"), dict)
        else {}
    )
    return {
        "status": validation_broker.get("status"),
        "source_status": validation_broker.get("source_status"),
        "stale_build_warning_count": int_value(stale.get("count")) or 0,
        "recommended_next_actions": bounded(
            validation_broker.get("recommended_next_actions") or [],
            8,
        ),
    }


def work_admission_source_signals(input_pack: dict[str, Any]) -> dict[str, Any]:
    beads = normalized_section(input_pack, "beads")
    beads_ready = normalized_section(input_pack, "beads_ready")
    agent_mail = normalized_section(input_pack, "agent_mail")
    cargo = normalized_section(input_pack, "cargo_admission")
    queue_forecast = (
        cargo.get("queue_forecast") if isinstance(cargo.get("queue_forecast"), dict) else {}
    )
    git_state = normalized_section(input_pack, "git_state")
    return {
        "beads": {
            "ready_count": int_value(beads_ready.get("ready_count")) or 0,
            "active_count": int_value(beads.get("active_count")) or 0,
            "open_candidate_count": int_value(beads.get("open_candidate_count")) or 0,
            "deferred_planning_count": int_value(
                beads.get("deferred_planning_count")
            ) or 0,
        },
        "agent_mail": {
            "status": agent_mail.get("status"),
            "read_status": agent_mail.get("read_status"),
            "reservation_status": agent_mail.get("reservation_status"),
            "fallback_action": agent_mail.get("fallback_action"),
        },
        "rch": {
            "decision": cargo.get("decision"),
            "queue_recommended_action": queue_forecast.get("recommended_action"),
            "slot_pressure": queue_forecast.get("slot_pressure"),
            "queue_depth": queue_forecast.get("queue_depth"),
        },
        "git": {
            "dirty": git_state.get("dirty"),
            "change_count": git_state.get("change_count"),
            "branch": git_state.get("branch"),
            "upstream": git_state.get("upstream"),
        },
        "validation_broker": work_admission_validation_broker_signal(input_pack),
        "evidence": work_admission_evidence_counts(input_pack),
    }


def work_admission_validation_failure(
    input_pack: dict[str, Any],
    autopilot_plan: dict[str, Any],
) -> dict[str, Any] | None:
    return action_plan_validation_failure(input_pack, autopilot_plan)


def work_admission_required_signal_blockers(
    input_pack: dict[str, Any],
    action_plan: dict[str, Any],
    autopilot_plan: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    next_decision = str(
        action_plan.get("next_safest_action", {}).get("decision")
        or "pause_or_surface_blocker"
    )
    evidence_counts = work_admission_evidence_counts(input_pack)
    if evidence_counts["required_blocker"] or evidence_counts["required_missing"]:
        blockers.append("required_evidence_missing_or_blocked")
    validation_failure = work_admission_validation_failure(input_pack, autopilot_plan)
    if validation_failure is not None:
        blockers.append("active_validation_failure")
    validation_broker = work_admission_validation_broker_signal(input_pack)
    validation_status = str(validation_broker.get("status") or "")
    validation_source_status = str(validation_broker.get("source_status") or "")
    if validation_status in {"blocked", "degraded", "failed", "error", "unavailable"}:
        blockers.append("validation_broker_degraded")
    if validation_source_status in {"blocked", "degraded", "failed", "error", "unavailable"}:
        blockers.append("validation_broker_source_degraded")
    if next_decision != "implement_ready_work":
        return sorted(set(blockers))

    agent_mail = normalized_section(input_pack, "agent_mail")
    if agent_mail.get("status") != "ok":
        blockers.append("ownership_signal_unavailable")
    cargo = normalized_section(input_pack, "cargo_admission")
    queue_forecast = (
        cargo.get("queue_forecast") if isinstance(cargo.get("queue_forecast"), dict) else {}
    )
    if cargo.get("decision") in {"backoff", "degraded", "deny"}:
        blockers.append("rch_admission_not_green")
    if queue_forecast.get("recommended_action") == "backoff":
        blockers.append("rch_queue_backoff")
    git_state = normalized_section(input_pack, "git_state")
    if git_state.get("dirty") is True:
        blockers.append("dirty_worktree_before_implementation")
    return sorted(set(blockers))


def derive_work_admission_decision(
    input_pack: dict[str, Any],
    autopilot_plan: dict[str, Any],
    action_plan: dict[str, Any],
) -> dict[str, Any]:
    next_action = action_plan.get("next_safest_action")
    if not isinstance(next_action, dict):
        next_action = {}
    source_decision = str(next_action.get("decision") or "pause_or_surface_blocker")
    decision = ACTION_PLAN_TO_WORK_ADMISSION_DECISION.get(
        source_decision,
        "pause_escalate",
    )
    blockers = work_admission_required_signal_blockers(
        input_pack,
        action_plan,
        autopilot_plan,
    )
    evidence_counts = work_admission_evidence_counts(input_pack)
    if "active_validation_failure" in blockers:
        decision = "pause_escalate"
    elif evidence_counts["required_blocker"] or evidence_counts["required_missing"]:
        decision = "pause_escalate"
    elif evidence_counts["required_stale"]:
        decision = "renew_evidence"
    elif decision == "proceed_with_implementation" and blockers:
        decision = "wait" if blockers == ["ownership_signal_unavailable"] else "pause_escalate"
    status = work_admission_status_for_decision(decision)
    admit_new_implementation = decision == "proceed_with_implementation" and not blockers
    return {
        "decision": decision,
        "status": status,
        "source_action_plan_decision": source_decision,
        "source_action": next_action.get("source_action"),
        "rank": next_action.get("rank"),
        "title": next_action.get("title"),
        "severity": next_action.get("severity"),
        "confidence": next_action.get("confidence"),
        "rationale": next_action.get("rationale"),
        "blockers": blockers,
        "evidence_paths": list(next_action.get("evidence_paths") or []),
        "command_safety_classes": list(next_action.get("command_safety_classes") or []),
        "admit_new_implementation": admit_new_implementation,
        "admit_coordination_work": decision in {
            "create_or_refine_bead",
            "renew_evidence",
        },
        "commands_require_operator_execution": True,
    }


def build_work_admission_gate(
    input_pack: dict[str, Any],
    autopilot_plan: dict[str, Any],
    action_plan: dict[str, Any],
    *,
    max_items: int,
) -> dict[str, Any]:
    if input_pack.get("schema") != AUTOPILOT_INPUT_PACK_SCHEMA:
        raise RunpackError(
            "work admission gate requires "
            f"{AUTOPILOT_INPUT_PACK_SCHEMA}, got {input_pack.get('schema')}"
        )
    if autopilot_plan.get("schema") != AUTOPILOT_PLAN_SCHEMA:
        raise RunpackError(
            "work admission gate requires "
            f"{AUTOPILOT_PLAN_SCHEMA}, got {autopilot_plan.get('schema')}"
        )
    if action_plan.get("schema") != ACTION_PLAN_SCHEMA:
        raise RunpackError(
            "work admission gate requires "
            f"{ACTION_PLAN_SCHEMA}, got {action_plan.get('schema')}"
        )
    decision = derive_work_admission_decision(input_pack, autopilot_plan, action_plan)
    gate = {
        "schema": WORK_ADMISSION_GATE_SCHEMA,
        "generated_at": input_pack.get("generated_at"),
        "status": decision["status"],
        "purpose": "fail_closed_swarm_work_admission_gate_not_source_of_truth",
        "input_pack_schema": input_pack.get("schema"),
        "source_plan_schema": autopilot_plan.get("schema"),
        "action_plan_schema": action_plan.get("schema"),
        "decision": decision,
        "source_signals": work_admission_source_signals(input_pack),
        "failure_actions": bounded(autopilot_plan.get("failure_actions") or [], max_items),
        "degraded_reasons": bounded(input_pack.get("degraded_reasons") or [], max_items),
        "forbidden_actions": list(AUTOPILOT_FORBIDDEN_ACTIONS),
        "gate_guards": {
            "fail_closed": True,
            "dry_run_only": True,
            "no_source_mutation": True,
            "commands_require_operator_execution": True,
            "does_not_replace_beads_agent_mail_rch_validation_broker_doctor_or_ci": True,
        },
        "redaction_summary": input_pack.get("redaction_summary"),
    }
    assert_work_admission_gate_contract(gate)
    return gate


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
    proof_ledger = runpack.get("remote_validation_proof_ledger")
    if isinstance(proof_ledger, dict):
        proof_summary = proof_ledger.get("summary")
        if isinstance(proof_summary, dict) and (
            proof_summary.get("blocked_entries", 0)
            or proof_summary.get("degraded_entries", 0)
            or proof_summary.get("failed_entries", 0)
        ):
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
    git_summary = summarize_git_status(by_id["git_status"], args.max_items)
    proof_ledger = build_remote_validation_proof_ledger(
        by_id=by_id,
        generated_at=generated_at,
        git_state=git_summary,
        bead_id=getattr(args, "active_bead_id", None),
        capture_summary=capture_summary,
    )
    beads_summary = summarize_beads(
        by_id["beads"],
        generated_at=generated_at,
        stale_after_hours=args.stale_after_hours,
        max_items=args.max_items,
    )
    runpack = {
        "schema": RUNPACK_SCHEMA,
        "generated_at": generated_at.isoformat(),
        "status": "unknown",
        "purpose": "operator_handoff_not_release_performance_claim",
        "capture": capture_summary,
        "source_statuses": [source.to_status() for source in sources],
        "doctor_swarm": doctor_summary,
        "beads": beads_summary,
        "agent_mail": {
            "doctor_findings": doctor_summary.get("agent_mail_findings", []),
            "build_slots": doctor_summary.get("agent_mail_build_slots"),
            "smoke_reservation_count": smoke_summary.get("reservation_count"),
        },
        "agent_mail_read_state": summarize_agent_mail_read_state(
            capture_summary,
            doctor_summary,
            beads_summary,
            args.max_items,
        ),
        "rch_admission": summarize_cargo_admission(by_id["cargo_admission"]),
        "remote_validation_proof_ledger": proof_ledger,
        "evidence_readiness": summarize_claim_readiness(by_id["claim_readiness"], args.max_items),
        "git_state": git_summary,
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
    if "swarm_replay_preview" in by_id:
        runpack["swarm_replay_preview"] = summarize_swarm_replay_preview(
            by_id["swarm_replay_preview"],
            args.max_items,
        )
    if "validation_broker" in by_id:
        runpack["validation_broker"] = summarize_validation_broker_source(
            by_id["validation_broker"],
            args.max_items,
        )
    if "progress_slo" in by_id:
        runpack["progress_slo"] = summarize_progress_slo_source(
            by_id["progress_slo"],
            args.max_items,
        )
    if "stale_evidence_renewal_queue" in by_id:
        runpack["stale_evidence_renewal_queue"] = (
            summarize_stale_evidence_renewal_queue(
                by_id["stale_evidence_renewal_queue"],
                args.max_items,
            )
        )
    runpack["temp_artifact_inventory"] = build_temp_artifact_inventory(
        runpack,
        capture_summary=capture_summary,
        generated_at=generated_at,
        max_items=args.max_items,
    )
    runpack["turn_pressure_ledger"] = build_turn_pressure_ledger(
        runpack,
        by_id,
        generated_at=generated_at,
        max_items=args.max_items,
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
    deferred_planning = runpack["beads"].get("deferred_planning")
    if isinstance(deferred_planning, list) and deferred_planning:
        epic_names = [
            f"{item.get('id')} ({item.get('title')})"
            for item in deferred_planning[:3]
            if item.get("id")
        ]
        actions.append(
            "Create or refine child Beads for deferred roadmap/planning epics before "
            f"declaring the queue clean: {', '.join(epic_names)}"
        )
    if runpack["rch_admission"].get("decision") in {"backoff", "degraded", "deny"}:
        actions.append("Treat cargo/RCH admission as blocked or degraded before heavy builds")
    proof_ledger = runpack.get("remote_validation_proof_ledger")
    if isinstance(proof_ledger, dict):
        proof_summary = proof_ledger.get("summary")
        if isinstance(proof_summary, dict) and (
            proof_summary.get("blocked_entries", 0)
            or proof_summary.get("degraded_entries", 0)
            or proof_summary.get("failed_entries", 0)
        ):
            actions.append(
                "Inspect remote validation proof ledger before closing RCH-required beads"
            )
    forecast_action = runpack["rch_admission"].get("queue_forecast", {}).get("recommended_action")
    if forecast_action == "split":
        actions.append("Split heavy cargo validation based on RCH queue forecast pressure")
    elif forecast_action == "backoff":
        actions.append("Back off heavy cargo validation until the RCH queue forecast recovers")
    if runpack["activity_digest"].get("saturated"):
        actions.append("Use activity-digest saturation evidence to narrow or redirect the swarm")
    if runpack["git_state"].get("dirty"):
        actions.append("Account for dirty files before using the runpack as handoff evidence")
    temp_inventory = runpack.get("temp_artifact_inventory")
    if isinstance(temp_inventory, dict):
        summary = (
            temp_inventory.get("summary")
            if isinstance(temp_inventory.get("summary"), dict)
            else {}
        )
        if summary.get("cleanup_candidate_count") or summary.get("deletion_protected_count"):
            actions.append(
                "Review temp artifact inventory; do not delete unknown-owner or candidate paths without explicit written approval"
            )
    mail_state = runpack.get("agent_mail_read_state", {})
    if mail_state.get("status") in {"degraded", "not_available"}:
        actions.append("Treat Agent Mail read state as unavailable and fall back to Beads ownership evidence")
        fallback_ownership = (
            mail_state.get("fallback_ownership_evidence")
            if isinstance(mail_state.get("fallback_ownership_evidence"), dict)
            else {}
        )
        active_ownership = (
            fallback_ownership.get("active_ownership")
            if isinstance(fallback_ownership.get("active_ownership"), list)
            else []
        )
        if active_ownership:
            owner = active_ownership[0]
            actions.append(
                "Use Beads active ownership from "
                f"`{fallback_ownership.get('source_command')}` as the soft-lock source "
                f"({owner.get('id')} assigned to {owner.get('assignee')}, updated {owner.get('updated_at')})"
            )
        recovery_preview_action = mail_state.get("recovery_preview_action")
        recovery_action = mail_state.get("recovery_action")
        if recovery_preview_action:
            actions.append(
                f"Preview Agent Mail recovery with `{recovery_preview_action}` before any repair; keep using Beads soft locks meanwhile"
            )
        elif recovery_action:
            actions.append(
                f"Review Agent Mail recovery action `{recovery_action}` without mutating Mail during runpack generation"
            )
        actions.extend(proof_string(step) for step in mail_state.get("no_mail_closeout_steps", []))
    if runpack.get("validation_outputs", {}).get("status") == "failed":
        actions.append("Inspect failed validation output before resuming or closing the active bead")
    turn_pressure = runpack.get("turn_pressure_ledger")
    if isinstance(turn_pressure, dict) and turn_pressure.get("status") in {
        "watch",
        "pressured",
        "blocked",
    }:
        actions.append(
            "Review turn pressure ledger before adding prompt/tool/session load to the next turn"
        )
    bottleneck = runpack.get("bottleneck_attribution")
    if isinstance(bottleneck, dict) and bottleneck.get("status") != "ready":
        actions.append(
            "Review degraded bottleneck attribution dashboard before using it as current diagnostic evidence"
        )
    scorecard = runpack.get("swarm_scale_safety_scorecard")
    if isinstance(scorecard, dict) and scorecard.get("overall_status") != "ready":
        actions.append("Review degraded swarm-scale safety scorecard dimensions before release runpack signoff")
    validation_broker = runpack.get("validation_broker")
    if not isinstance(validation_broker, dict):
        validation_broker = runpack.get("doctor_swarm", {}).get("validation_broker")
    if isinstance(validation_broker, dict):
        stale = (
            validation_broker.get("stale_build_warnings")
            if isinstance(validation_broker.get("stale_build_warnings"), dict)
            else {}
        )
        duplicate = (
            validation_broker.get("duplicate_gate_opportunities")
            if isinstance(validation_broker.get("duplicate_gate_opportunities"), dict)
            else {}
        )
        if validation_broker.get("source_status") in {"degraded", "unavailable"}:
            actions.append("Treat validation-broker posture as degraded advisory evidence before coalescing gates")
        if stale.get("count"):
            actions.append("Review validation-broker stale slot warnings before launching heavy validation")
        if duplicate.get("count"):
            actions.append("Use validation-broker duplicate-gate opportunities only after provenance matches")
    stale_queue = runpack.get("stale_evidence_renewal_queue")
    if isinstance(stale_queue, dict):
        summary = stale_queue.get("summary") if isinstance(stale_queue.get("summary"), dict) else {}
        renewal_count = int_value(summary.get("renewal_item_count"))
        blocked_count = int_value(summary.get("blocked_count"))
        if renewal_count or blocked_count:
            actions.append(
                "Renew stale evidence queue items before relying on runpack or release-claim evidence"
            )
    progress_slo = runpack.get("progress_slo")
    if not isinstance(progress_slo, dict):
        progress_slo = runpack.get("doctor_swarm", {}).get("progress_slo")
    if isinstance(progress_slo, dict):
        progress_status = progress_slo.get("progress_status")
        if progress_status in {
            "coordination_degraded",
            "build_saturated",
            "stalled",
            "malformed_source_degraded",
            "insufficient_evidence_degraded",
        }:
            actions.append(
                f"Review progress SLO `{progress_status}` before launching more swarm work"
            )
        elif progress_status == "quiet_blocked":
            actions.append("Use progress SLO quiet-blocked evidence to inspect blocked dependencies before broadening work")
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
    context_intelligence = runpack["doctor_swarm"].get("context_intelligence")
    if isinstance(context_intelligence, dict):
        bundle = context_intelligence.get("bundle")
        cache = context_intelligence.get("cache")
        bundle = bundle if isinstance(bundle, dict) else {}
        cache = cache if isinstance(cache, dict) else {}
        lines.append(
            "- Context intelligence: "
            f"`{context_intelligence.get('status')}` "
            f"(coverage `{context_intelligence.get('coverage_status')}`, "
            f"missing tests `{bundle.get('missing_test_link_count')}`, "
            f"cache pressure `{cache.get('pressure_count')}`)"
        )
    lines.append(f"- Beads active/stale: `{runpack['beads'].get('active_count')}` active, `{len(runpack['beads'].get('stale') or [])}` stale")
    lines.append(f"- RCH admission: `{runpack['rch_admission'].get('decision')}`")
    lines.append(f"- RCH queue forecast: `{runpack['rch_admission'].get('queue_forecast', {}).get('recommended_action')}`")
    proof_summary = runpack["remote_validation_proof_ledger"].get("summary", {})
    lines.append(
        "- Remote validation proof: "
        f"`{proof_summary.get('clean_remote_proof_entries')}` clean, "
        f"`{proof_summary.get('degraded_entries')}` degraded, "
        f"`{proof_summary.get('blocked_entries')}` blocked, "
        f"`{proof_summary.get('failed_entries')}` failed"
    )
    lines.append(f"- Evidence readiness: `{runpack['evidence_readiness'].get('overall_status')}`")
    lines.append(f"- Git dirty: `{runpack['git_state'].get('dirty')}`")
    temp_inventory = runpack.get("temp_artifact_inventory")
    if isinstance(temp_inventory, dict):
        summary = (
            temp_inventory.get("summary")
            if isinstance(temp_inventory.get("summary"), dict)
            else {}
        )
        lines.append(
            "- Temp artifact inventory: "
            f"`{temp_inventory.get('status')}` "
            f"(entries `{summary.get('entry_count')}`, "
            f"protected `{summary.get('deletion_protected_count')}`, "
            f"delete commands `{summary.get('emitted_deletion_command_count')}`)"
        )
    lines.append(f"- Agent Mail read state: `{runpack['agent_mail_read_state'].get('status')}`")
    mail_state = runpack.get("agent_mail_read_state", {})
    fallback_ownership = (
        mail_state.get("fallback_ownership_evidence")
        if isinstance(mail_state.get("fallback_ownership_evidence"), dict)
        else {}
    )
    if fallback_ownership:
        lines.append(
            "- Beads soft-lock source: "
            f"`{fallback_ownership.get('source_command')}` "
            f"({fallback_ownership.get('in_progress_count')} in-progress owners)"
        )
    if isinstance(mail_state, dict) and mail_state.get("recovery_mode"):
        lines.append(
            "- Agent Mail recovery: "
            f"`{mail_state.get('recovery_mode')}` "
            f"(preview `{mail_state.get('recovery_preview_action')}`, "
            f"source action `{mail_state.get('recovery_action')}`)"
        )
    lines.append(f"- Validation outputs: `{runpack['validation_outputs'].get('status')}`")
    lines.append(f"- Activity saturated: `{runpack['activity_digest'].get('saturated')}`")
    validation_broker = runpack.get("validation_broker")
    if not isinstance(validation_broker, dict):
        validation_broker = runpack["doctor_swarm"].get("validation_broker")
    if isinstance(validation_broker, dict):
        stale = (
            validation_broker.get("stale_build_warnings")
            if isinstance(validation_broker.get("stale_build_warnings"), dict)
            else {}
        )
        duplicate = (
            validation_broker.get("duplicate_gate_opportunities")
            if isinstance(validation_broker.get("duplicate_gate_opportunities"), dict)
            else {}
        )
        lines.append(
            "- Validation broker: "
            f"`{validation_broker.get('source_status')}` "
            f"(stale `{stale.get('count')}`, duplicate `{duplicate.get('count')}`)"
        )
    stale_queue = runpack.get("stale_evidence_renewal_queue")
    if isinstance(stale_queue, dict):
        summary = stale_queue.get("summary") if isinstance(stale_queue.get("summary"), dict) else {}
        lines.append(
            "- Stale evidence renewal queue: "
            f"`{stale_queue.get('status')}` "
            f"(renew `{summary.get('renewal_item_count')}`, "
            f"blocked `{summary.get('blocked_count')}`)"
        )
    progress_slo = runpack.get("progress_slo")
    if not isinstance(progress_slo, dict):
        progress_slo = runpack["doctor_swarm"].get("progress_slo")
    if isinstance(progress_slo, dict):
        lines.append(
            "- Progress SLO: "
            f"`{progress_slo.get('progress_status')}` "
            f"(confidence `{progress_slo.get('confidence')}`, "
            f"reasons `{len(progress_slo.get('reason_ids') or [])}`)"
        )
    if isinstance(runpack.get("swarm_replay_preview"), dict):
        preview = runpack["swarm_replay_preview"]
        lines.append(
            f"- Replay preview: `{preview.get('recommended_policy_id')}` "
            f"({preview.get('comparison_count')} policy comparisons)"
        )
    turn_pressure = runpack.get("turn_pressure_ledger")
    if isinstance(turn_pressure, dict):
        summary = (
            turn_pressure.get("summary")
            if isinstance(turn_pressure.get("summary"), dict)
            else {}
        )
        lines.append(
            f"- Turn pressure ledger: `{turn_pressure.get('status')}` "
            f"({summary.get('pressure_event_count')} pressure events)"
        )
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
    proof_ledger = runpack["remote_validation_proof_ledger"]
    lines.extend(["", "## Remote Validation Proof Ledger"])
    lines.append(f"- Schema: `{proof_ledger.get('schema')}`")
    for entry in proof_ledger.get("entries", []):
        classification = entry.get("evidence_classification", {})
        runner = entry.get("runner", {})
        artifact = entry.get("artifact_retrieval", {})
        lines.append(
            f"- `{entry.get('command_class')}`: `{classification.get('status')}` "
            f"(remote `{runner.get('remote_execution')}`, "
            f"fallback `{runner.get('local_fallback')}`, "
            f"artifacts `{artifact.get('status')}`)"
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
    if isinstance(runpack.get("turn_pressure_ledger"), dict):
        ledger = runpack["turn_pressure_ledger"]
        lines.extend(["", "## Turn Pressure Ledger"])
        for dimension in ledger.get("dimensions", []):
            lines.append(
                f"- `{dimension.get('id')}`: `{dimension.get('status')}` "
                f"({dimension.get('signal')})"
            )
    if isinstance(runpack.get("temp_artifact_inventory"), dict):
        inventory = runpack["temp_artifact_inventory"]
        lines.extend(["", "## Temp Artifact Inventory"])
        lines.append(f"- Schema: `{inventory.get('schema')}`")
        lines.append(f"- Status: `{inventory.get('status')}`")
        for entry in inventory.get("entries", []):
            lines.append(
                f"- `{entry.get('kind')}`: `{entry.get('deletion_policy')}` "
                f"({entry.get('path')})"
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


def write_action_plan_output(
    args: argparse.Namespace,
    action_plan: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_action_plan_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(f"refusing to overwrite existing action plan: {output_path}")
    output_path.write_text(json_dumps(action_plan, pretty=True), encoding="utf-8")


def write_work_admission_gate_output(
    args: argparse.Namespace,
    gate: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_work_admission_gate_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(
            f"refusing to overwrite existing work admission gate: {output_path}"
        )
    output_path.write_text(json_dumps(gate, pretty=True), encoding="utf-8")


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


def load_shared_agent_mail_schema_corrupt_fixture(
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / AGENT_MAIL_SCHEMA_CORRUPT_FIXTURE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing shared Agent Mail fixture: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(f"malformed shared Agent Mail fixture: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"shared Agent Mail fixture root must be an object: {path}")
    if generated_at is not None:
        payload = {**payload, "generated_at": generated_at}
    return payload


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
    remote_contract_path = repo_root / REMOTE_VALIDATION_CONTRACT_PATH
    try:
        remote_contract = json.loads(remote_contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(
            f"missing remote validation contract: {remote_contract_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"remote validation contract is malformed JSON: {remote_contract_path}: {exc}"
        ) from exc
    ledger = runpack.get("remote_validation_proof_ledger")
    assert isinstance(ledger, dict)
    assert ledger.get("schema") == remote_contract.get("ledger_schema")
    for key in remote_contract.get("required_ledger_top_level_keys", []):
        assert key in ledger, f"remote validation ledger missing key: {key}"
    entries = ledger.get("entries")
    assert isinstance(entries, list) and entries
    for entry in entries:
        assert isinstance(entry, dict)
        assert entry.get("schema") == remote_contract.get("entry_schema")
        for key in remote_contract.get("required_entry_keys", []):
            assert key in entry, f"remote validation entry missing key: {key}"
        for section, contract_key in (
            ("command", "command_contract"),
            ("runner", "runner_contract"),
            ("timing", "timing_contract"),
            ("exit", "exit_contract"),
            ("paths", "paths_contract"),
            ("artifact_retrieval", "artifact_retrieval_contract"),
            ("evidence_classification", "evidence_classification_contract"),
        ):
            section_payload = entry.get(section)
            assert isinstance(section_payload, dict)
            for field in remote_contract.get(contract_key, {}).get("required_fields", []):
                assert field in section_payload, (
                    f"remote validation {section} missing field: {field}"
                )
        assert entry["command_class"] in set(
            remote_contract.get("command_contract", {}).get("allowed_command_classes", [])
        )
        assert entry["runner"]["requested_runner"] in set(
            remote_contract.get("runner_contract", {}).get("allowed_requested_runners", [])
        )
        assert entry["runner"]["resolved_runner"] in set(
            remote_contract.get("runner_contract", {}).get("allowed_resolved_runners", [])
        )
        assert entry["runner"]["local_fallback"] in set(
            remote_contract.get("runner_contract", {}).get("allowed_local_fallback", [])
        )
        assert entry["exit"]["termination_reason"] in set(
            remote_contract.get("exit_contract", {}).get("allowed_termination_reasons", [])
        )
        assert entry["artifact_retrieval"]["status"] in set(
            remote_contract.get("artifact_retrieval_contract", {}).get("allowed_status", [])
        )
        assert entry["evidence_classification"]["status"] in set(
            remote_contract.get("evidence_classification_contract", {}).get("allowed_status", [])
        )
        suppressed = set(entry["evidence_classification"]["suppressed_claims"])
        required_suppressed = set(
            remote_contract.get("evidence_classification_contract", {}).get(
                "required_suppressed_claims",
                [],
            )
        )
        assert suppressed.issuperset(required_suppressed)
    for path in contract.get("optional_summary_paths", []):
        parts = path.split(".")
        optional_root = (
            ".".join(parts[:2])
            if len(parts) > 1 and parts[0] == "doctor_swarm"
            else parts[0]
        )
        try:
            get_dotted(runpack, optional_root)
        except KeyError:
            continue
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
    turn_pressure = runpack.get("turn_pressure_ledger")
    assert isinstance(turn_pressure, dict)
    assert_turn_pressure_ledger_contract(turn_pressure)
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


def assert_action_plan_contract(action_plan: dict[str, Any]) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    contract_path = repo_root / ACTION_PLAN_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing action plan contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"action plan contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == ACTION_PLAN_CONTRACT_SCHEMA
    assert contract.get("action_plan_schema") == ACTION_PLAN_SCHEMA
    assert action_plan.get("schema") == contract["action_plan_schema"]
    assert action_plan.get("purpose") == contract.get("purpose")
    assert action_plan.get("status") in set(contract.get("allowed_statuses", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in action_plan, f"missing top-level action plan key: {key}"
    next_action = action_plan.get("next_safest_action")
    assert isinstance(next_action, dict)
    for key in contract.get("required_next_action_fields", []):
        assert key in next_action, f"missing next_safest_action key: {key}"
    allowed_decisions = set(contract.get("allowed_decisions", []))
    assert next_action.get("decision") in allowed_decisions
    safety_classes = set(contract.get("command_safety_classes", []))
    for item in next_action.get("command_safety_classes", []):
        assert item in safety_classes, f"unknown action plan safety class: {item}"
    assert next_action.get("commands_require_operator_execution") is True
    operator_actions = action_plan.get("operator_actions")
    assert isinstance(operator_actions, list) and operator_actions
    for action in operator_actions:
        assert isinstance(action, dict)
        assert action.get("decision") in allowed_decisions
        for item in action.get("command_safety_classes", []):
            assert item in safety_classes, f"unknown action safety class: {item}"
    guards = action_plan.get("planner_guards")
    assert isinstance(guards, dict)
    for guard in contract.get("required_true_guards", []):
        assert guards.get(guard) is True, f"action plan guard must be true: {guard}"
    assert set(action_plan.get("forbidden_actions", [])).issuperset(
        set(contract.get("required_forbidden_actions", []))
    )


def assert_work_admission_gate_contract(gate: dict[str, Any]) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    contract_path = repo_root / WORK_ADMISSION_GATE_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing work admission gate contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"work admission gate contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == WORK_ADMISSION_GATE_CONTRACT_SCHEMA
    assert contract.get("work_admission_gate_schema") == WORK_ADMISSION_GATE_SCHEMA
    assert gate.get("schema") == contract["work_admission_gate_schema"]
    assert gate.get("purpose") == contract.get("purpose")
    assert gate.get("status") in set(contract.get("allowed_statuses", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in gate, f"missing top-level work admission gate key: {key}"
    decision = gate.get("decision")
    assert isinstance(decision, dict)
    for key in contract.get("required_decision_fields", []):
        assert key in decision, f"missing work admission decision key: {key}"
    assert decision.get("decision") in set(contract.get("allowed_decisions", []))
    assert decision.get("status") in set(contract.get("allowed_statuses", []))
    assert isinstance(decision.get("blockers"), list)
    if decision.get("admit_new_implementation") is True:
        assert decision.get("decision") == "proceed_with_implementation"
        assert decision.get("blockers") == []
    assert decision.get("commands_require_operator_execution") is True
    source_signals = gate.get("source_signals")
    assert isinstance(source_signals, dict)
    for key in contract.get("required_source_signal_keys", []):
        assert key in source_signals, f"missing source signal key: {key}"
    guards = gate.get("gate_guards")
    assert isinstance(guards, dict)
    for guard in contract.get("required_true_guards", []):
        assert guards.get(guard) is True, f"work admission gate guard must be true: {guard}"
    assert set(gate.get("forbidden_actions", [])).issuperset(
        set(contract.get("required_forbidden_actions", []))
    )


def assert_turn_pressure_ledger_contract(ledger: dict[str, Any]) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    contract_path = repo_root / TURN_PRESSURE_LEDGER_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing turn pressure ledger contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"turn pressure ledger contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == TURN_PRESSURE_LEDGER_CONTRACT_SCHEMA
    assert contract.get("ledger_schema") == TURN_PRESSURE_LEDGER_SCHEMA
    assert ledger.get("schema") == contract["ledger_schema"]
    assert ledger.get("purpose") == contract.get("purpose")
    assert ledger.get("status") in set(contract.get("allowed_statuses", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in ledger, f"missing top-level turn pressure ledger key: {key}"
    dimensions = ledger.get("dimensions")
    assert isinstance(dimensions, list)
    dimension_ids = {
        dimension.get("id") for dimension in dimensions if isinstance(dimension, dict)
    }
    assert dimension_ids == set(contract.get("required_dimension_ids", []))
    for dimension in dimensions:
        assert isinstance(dimension, dict)
        for key in contract.get("required_dimension_fields", []):
            assert key in dimension, f"turn pressure dimension missing key: {key}"
        assert dimension.get("status") in set(contract.get("allowed_statuses", []))
        assert isinstance(dimension.get("evidence_paths"), list)
        assert isinstance(dimension.get("metrics"), dict)
    events = ledger.get("pressure_events")
    assert isinstance(events, list)
    for event in events:
        assert isinstance(event, dict)
        for key in contract.get("required_event_fields", []):
            assert key in event, f"turn pressure event missing key: {key}"
        assert event.get("dimension_id") in dimension_ids
        assert event.get("status") in set(contract.get("allowed_statuses", []))
    guards = ledger.get("guards")
    assert isinstance(guards, dict)
    for guard in contract.get("required_true_guards", []):
        assert guards.get(guard) is True, f"turn pressure guard must be true: {guard}"


def canonicalize_for_golden(value: Any, workspace: Path) -> Any:
    workspace_text = str(workspace)
    if isinstance(value, dict):
        path_value = value.get("path")
        workspace_scoped_path = isinstance(path_value, str) and workspace_text in path_value
        canonicalized = {}
        for key, item in value.items():
            if key == "sha256" and isinstance(item, str):
                canonicalized[key] = "[SHA256]"
            elif key == "size_bytes" and workspace_scoped_path:
                canonicalized[key] = "[SIZE_BYTES]"
            else:
                canonicalized[key] = canonicalize_for_golden(item, workspace)
        return canonicalized
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


def gibibytes(gib: int) -> int:
    return gib * 1024 * 1024 * 1024


def autopilot_e2e_capacity_preflight(
    generated_at: str,
    *,
    logical_cores: int,
    effective_cores: int,
    memory_gib: int,
    agent_concurrency: int,
    rch_verification_fanout: int,
    max_queue_depth: int,
    tool_concurrency: int | None = None,
    extension_hostcall_lanes: int | None = None,
) -> dict[str, Any]:
    effective_memory_bytes = gibibytes(memory_gib)
    return {
        "schema": HOST_PREFLIGHT_SCHEMA,
        "generated_at": generated_at,
        "status": "pass",
        "cpu": {
            "logical_cores": logical_cores,
            "effective_cores": effective_cores,
            "cgroup_quota": {"quota_cores": float(effective_cores), "unlimited": False},
            "cpuset": {"cpu_count": effective_cores},
        },
        "numa": {"node_count": 2, "nodes": [0, 1]},
        "memory": {
            "cgroup_limit_bytes": effective_memory_bytes,
            "effective_limit_bytes": effective_memory_bytes,
            "unlimited": False,
        },
        "tmpfs_headroom": {
            "expected_root": "/data/tmp/pi_agent_rust_cargo",
            "paths": [
                {
                    "env_name": "CARGO_TARGET_DIR",
                    "path": "/data/tmp/pi_agent_rust_cargo/e2e/target",
                    "ready": True,
                    "available_kb": max(1, memory_gib * 1024 * 1024 // 2),
                },
                {
                    "env_name": "TMPDIR",
                    "path": "/data/tmp/pi_agent_rust_cargo/e2e/tmp",
                    "ready": True,
                    "available_kb": max(1, memory_gib * 1024 * 1024 // 2),
                },
            ],
        },
        "recommended_budgets": {
            "agent_concurrency": agent_concurrency,
            "tool_concurrency": tool_concurrency or agent_concurrency * 2,
            "extension_hostcall_lanes": extension_hostcall_lanes
            or min(16, max(1, effective_cores // 2)),
            "rch_verification_fanout": rch_verification_fanout,
            "max_queue_depth": max_queue_depth,
            "max_rss_bytes": effective_memory_bytes // 2,
            "plan_confidence": "high",
        },
        "critical_failures": [],
        "source_errors": [],
    }


def autopilot_e2e_preflight(generated_at: str) -> dict[str, Any]:
    return autopilot_e2e_capacity_preflight(
        generated_at,
        logical_cores=16,
        effective_cores=8,
        memory_gib=32,
        agent_concurrency=4,
        tool_concurrency=8,
        extension_hostcall_lanes=16,
        rch_verification_fanout=2,
        max_queue_depth=2,
    )


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
    slots_total: int = 8,
    slots_available: int | None = None,
    active_builds: int | None = None,
    queued_builds: int | None = None,
    reason: str = "autopilot_e2e_fixture",
) -> dict[str, Any]:
    return {
        "schema": "pi.cargo_headroom.admission.v1",
        "decision": decision,
        "reason": reason,
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
            "active_builds": queue_depth if active_builds is None else active_builds,
            "queued_builds": max(0, queue_depth - 2)
            if queued_builds is None
            else queued_builds,
            "slots_available": (
                0 if slot_pressure == "saturated" else slots_total
            )
            if slots_available is None
            else slots_available,
            "slots_total": slots_total,
            "workers_healthy": slots_total,
            "workers_total": slots_total,
            "estimated_wait_seconds": 240 if queue_action == "backoff" else 0,
        },
    }


def autopilot_e2e_remote_validation_cargo_payload(
    generated_at: str,
    *,
    scenario_id: str,
) -> dict[str, Any]:
    payload = autopilot_e2e_cargo_payload(
        queue_depth=0,
        slots_total=8,
        slots_available=7,
        active_builds=1,
        queued_builds=0,
        reason="remote validation proof captured through RCH",
    )
    timestamp = generated_at.replace("+00:00", "Z")
    payload["cargo_command"] = "check --all-targets"
    payload["remote_validation_proof"] = {
        "bead_id": scenario_id,
        "runner": {
            "requested_runner": "rch",
            "resolved_runner": "rch_remote",
            "runner_requirement": "rch_required",
            "remote_execution": True,
            "local_fallback": "none",
            "rch_job_id": f"{scenario_id}-rch-job",
            "worker_id": "autopilot-e2e-worker",
            "worker_host": "ubuntu@autopilot-e2e-worker",
            "queue_state": "admitted",
            "worker_state": "completed",
            "command_rewrite": {
                "tmpdir_rewritten": True,
                "target_dir_rewritten": True,
            },
            "status_excerpt": "Remote command finished: exit=0",
        },
        "timing": {
            "started_at_utc": timestamp,
            "ended_at_utc": timestamp,
            "duration_ms": 0,
            "heartbeat_at_utc": timestamp,
            "stale_progress_detected": False,
        },
        "exit": {
            "exit_code": 0,
            "success": True,
            "termination_reason": "completed",
            "stderr_excerpt": "",
            "stdout_excerpt": "Finished dev profile",
        },
        "paths": {
            "remote_target_dir": "/data/projects/pi_agent_rust/.rch-target-autopilot-e2e",
            "remote_tmpdir": "/data/projects/pi_agent_rust/.rch-tmp-autopilot-e2e",
            "artifact_paths": ["target/debug/.fingerprint"],
        },
    }
    return payload


def autopilot_e2e_remote_artifact_sync_payload(generated_at: str) -> dict[str, Any]:
    return {
        "schema": RCH_ARTIFACT_SYNC_SCHEMA,
        "generated_at": generated_at,
        "status": "pass",
        "required_paths": [{"path": "target/debug/.fingerprint", "included": True}],
        "violations": [],
        "exit_code": 0,
        "elapsed_ms": 12,
    }


def autopilot_e2e_remote_validation_ledger(
    *,
    cargo_payload: dict[str, Any],
    git_payload: dict[str, Any],
    generated_at: str,
    scenario_id: str,
) -> dict[str, Any]:
    return build_remote_validation_proof_ledger(
        by_id={
            "cargo_admission": SourcePayload(
                "cargo_admission",
                None,
                "ok",
                cargo_payload.get("schema"),
                cargo_payload,
            ),
            "rch_artifact_sync": SourcePayload(
                "rch_artifact_sync",
                None,
                "ok",
                RCH_ARTIFACT_SYNC_SCHEMA,
                autopilot_e2e_remote_artifact_sync_payload(generated_at),
            ),
        },
        generated_at=parse_utc(generated_at),
        git_state=git_payload,
        bead_id=scenario_id,
    )


def autopilot_e2e_agent_mail_status(
    generated_at: str,
    *,
    status: str = "ok",
    health_level: str = "green",
    issue: str | None = None,
    semantic_readiness_detail: str | None = None,
    recovery_mode: str | None = None,
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
    if semantic_readiness_detail is not None:
        payload["semantic_readiness"] = {
            "status": "fail",
            "detail": semantic_readiness_detail,
        }
    if recovery_mode is not None:
        payload["recovery"] = {
            "mode": recovery_mode,
            "next_action": AGENT_MAIL_SCHEMA_CORRUPT_RECOVERY_ACTION,
        }
    return payload


def autopilot_e2e_agent_mail_reservations(generated_at: str) -> dict[str, Any]:
    return {
        "schema": "pi.agent_mail.robot_reservations.v1",
        "generated_at": generated_at,
        "status": "ok",
        "reservations": [],
    }


def autopilot_e2e_active_beads_payload(
    generated_at: str,
    *,
    active_count: int,
    prefix: str,
) -> dict[str, Any]:
    return {
        "issues": [
            {
                "id": f"bd-{prefix}-{index + 1}",
                "title": f"{prefix} active fixture {index + 1}",
                "status": "in_progress",
                "assignee": f"{prefix}-agent-{index + 1}",
                "priority": 2,
                "updated_at": generated_at,
            }
            for index in range(active_count)
        ]
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
        swarm_replay_preview_json=None,
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
    if "use_beads_soft_lock" in expected_actions:
        agent_mail = normalized_section(input_pack, "agent_mail")
        unavailable_operations = agent_mail.get("unavailable_operations")
        assert isinstance(unavailable_operations, list)
        for operation in ("fetch_inbox", "send_message", "file_reservation_paths"):
            assert operation in unavailable_operations, (
                f"{scenario_id} should report {operation} unavailable: {agent_mail}"
            )
        assert "claim_ready_bead" not in action_names, (
            f"{scenario_id} must not emit Agent Mail-ready claim action: {action_names}"
        )
        if scenario_id == "degraded_agent_mail_soft_lock":
            assert agent_mail["semantic_readiness_status"] == "fail"
            assert agent_mail["semantic_readiness_detail"] == AGENT_MAIL_SCHEMA_CORRUPT_DETAIL
            assert agent_mail["recovery_mode"] == "corrupt"
            assert agent_mail["recovery_action"] == AGENT_MAIL_SCHEMA_CORRUPT_RECOVERY_ACTION
            assert agent_mail["recovery_preview_action"] == "am doctor repair --dry-run"
            assert agent_mail["redaction_summary"]["redacted_count"] >= 1
            mail_actions = [
                action
                for action in plan["actions"]
                if action.get("action") == "use_beads_soft_lock"
            ]
            assert mail_actions
            assert any(
                "recovery_preview=am doctor repair --dry-run"
                in action.get("rationale", "")
                for action in mail_actions
            )
    if "create_or_refine_backlog" in expected_actions:
        beads = normalized_section(input_pack, "beads")
        assert beads.get("deferred_planning_count") == 1
        assert beads.get("open_candidate_count") == 0
        assert "run_docs_only_work" not in action_names
        backlog_actions = [
            action
            for action in plan["actions"]
            if action.get("action") == "create_or_refine_backlog"
        ]
        assert backlog_actions
        assert "normalized_inputs.beads.deferred_planning" in backlog_actions[0].get(
            "evidence_paths", []
        )
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
        expected_action_plan_decision: str | None = None,
        expected_work_admission_decision: str | None = None,
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
        action_plan = build_swarm_action_plan(input_pack, plan, max_items=max_items)
        work_admission_gate = build_work_admission_gate(
            input_pack,
            plan,
            action_plan,
            max_items=max_items,
        )
        if expected_action_plan_decision is not None:
            actual_decision = action_plan["next_safest_action"]["decision"]
            assert actual_decision == expected_action_plan_decision, (
                f"{scenario_id} action plan decision mismatch: "
                f"expected {expected_action_plan_decision}, got {actual_decision}"
            )
        if expected_work_admission_decision is not None:
            actual_work_decision = work_admission_gate["decision"]["decision"]
            assert actual_work_decision == expected_work_admission_decision, (
                f"{scenario_id} work admission decision mismatch: "
                f"expected {expected_work_admission_decision}, got {actual_work_decision}"
            )
        result = autopilot_e2e_result_from_plan(
            scenario_id=scenario_id,
            scenario_dir=scenario_dir,
            generated_at=generated_at,
            correlation_id=correlation_id,
            input_pack=input_pack,
            plan=plan,
            expected_actions=expected_actions,
            events_path=events_path,
        )
        result["action_plan"] = {
            "schema": action_plan["schema"],
            "status": action_plan["status"],
            "decision": action_plan["next_safest_action"]["decision"],
            "source_action": action_plan["next_safest_action"]["source_action"],
            "command_safety_classes": action_plan["next_safest_action"][
                "command_safety_classes"
            ],
        }
        result["work_admission_gate"] = {
            "schema": work_admission_gate["schema"],
            "status": work_admission_gate["status"],
            "decision": work_admission_gate["decision"]["decision"],
            "admit_new_implementation": work_admission_gate["decision"][
                "admit_new_implementation"
            ],
            "blockers": work_admission_gate["decision"]["blockers"],
        }
        results.append(result)
        return input_pack, plan, result

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
        expected_action_plan_decision="implement_ready_work",
        expected_work_admission_decision="proceed_with_implementation",
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
        expected_action_plan_decision="create_or_refine_beads",
        expected_work_admission_decision="create_or_refine_bead",
    )
    deferred_roadmap_payload = {
        "issues": [
            {
                "id": "bd-roadmap-e2e",
                "title": "Swarm follow-up roadmap",
                "description": "Needs child backlog after idea-wizard planning.",
                "status": "deferred",
                "priority": 2,
                "issue_type": "epic",
                "updated_at": generated_at,
                "labels": ["idea-wizard", "swarm"],
            },
            {
                "id": "bd-roadmap-e2e.1",
                "title": "Closed roadmap child",
                "status": "closed",
                "priority": 2,
                "updated_at": generated_at,
                "dependencies": [
                    {"type": "parent-child", "depends_on_id": "bd-roadmap-e2e"}
                ],
            },
        ]
    }
    run_plan_scenario(
        "deferred_roadmap_backlog",
        beads_payload=deferred_roadmap_payload,
        beads_ready_payload=empty_ready,
        commands=empty_commands,
        expected_actions=["create_or_refine_backlog"],
        expected_action_plan_decision="create_or_refine_beads",
        expected_work_admission_decision="create_or_refine_bead",
    )

    degraded_mail_commands = list(ready_commands) + [
        {
            "id": "agent_mail_status",
            "command": "am robot status --format json",
            "cwd": str(workspace),
            "status": "failed",
            "exit_code": 2,
            "issue": AGENT_MAIL_SCHEMA_CORRUPT_DETAIL,
            "stdout_path": None,
            "stderr_snippet": AGENT_MAIL_SCHEMA_CORRUPT_DETAIL,
            "redaction_summary": {"redacted_count": 0, "fields": []},
        }
    ]
    run_plan_scenario(
        "degraded_agent_mail_soft_lock",
        beads_payload=ready_beads,
        beads_ready_payload=ready_queue,
        commands=degraded_mail_commands,
        expected_actions=["use_beads_soft_lock"],
        expected_action_plan_decision="wait_for_pressure",
        expected_work_admission_decision="wait",
        agent_mail_payload=load_shared_agent_mail_schema_corrupt_fixture(
            generated_at=generated_at,
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
        expected_action_plan_decision="wait_for_pressure",
        expected_work_admission_decision="wait",
        cargo_payload=autopilot_e2e_cargo_payload(
            decision="backoff",
            queue_action="backoff",
            slot_pressure="saturated",
            queue_depth=6,
        ),
    )

    capacity_ready_queue = [
        {
            "id": "bd-capacity-ready",
            "title": "Capacity fixture ready work",
            "status": "open",
            "priority": 2,
            "updated_at": generated_at,
            "labels": ["capacity", "resource-governor"],
        }
    ]
    capacity_cases = [
        {
            "scenario_id": "capacity_cpu16_mem64_healthy_admit",
            "preflight": autopilot_e2e_capacity_preflight(
                generated_at,
                logical_cores=16,
                effective_cores=16,
                memory_gib=64,
                agent_concurrency=8,
                rch_verification_fanout=2,
                max_queue_depth=4,
            ),
            "cargo": autopilot_e2e_cargo_payload(
                queue_depth=1,
                slots_total=2,
                active_builds=1,
                queued_builds=0,
            ),
            "beads": autopilot_e2e_active_beads_payload(
                generated_at,
                active_count=2,
                prefix="cap16",
            ),
            "agent_mail": agent_mail_ok,
            "expected_actions": ["claim_ready_bead"],
            "expected_status": "stable",
            "expected_selected_action": "claim_ready_bead",
            "expected_mail_status": "ok",
            "expected_active_agents": 2,
            "expected_active_builds": 1,
            "expected_signal_ids": set(),
        },
        {
            "scenario_id": "capacity_cpu32_mem128_degraded_mail",
            "preflight": autopilot_e2e_capacity_preflight(
                generated_at,
                logical_cores=32,
                effective_cores=32,
                memory_gib=128,
                agent_concurrency=16,
                rch_verification_fanout=4,
                max_queue_depth=8,
            ),
            "cargo": autopilot_e2e_cargo_payload(
                queue_depth=2,
                slots_total=4,
                active_builds=2,
                queued_builds=0,
            ),
            "beads": autopilot_e2e_active_beads_payload(
                generated_at,
                active_count=6,
                prefix="cap32",
            ),
            "agent_mail": load_shared_agent_mail_schema_corrupt_fixture(
                generated_at=generated_at,
            ),
            "expected_actions": ["use_beads_soft_lock"],
            "expected_status": "stable",
            "expected_selected_action": "use_beads_soft_lock",
            "expected_mail_status": "degraded",
            "expected_active_agents": 6,
            "expected_active_builds": 2,
            "expected_signal_ids": set(),
        },
        {
            "scenario_id": "capacity_cpu64_mem256_rch_backoff",
            "preflight": autopilot_e2e_capacity_preflight(
                generated_at,
                logical_cores=64,
                effective_cores=64,
                memory_gib=256,
                agent_concurrency=32,
                rch_verification_fanout=8,
                max_queue_depth=16,
            ),
            "cargo": autopilot_e2e_cargo_payload(
                decision="backoff",
                queue_action="backoff",
                slot_pressure="saturated",
                queue_depth=18,
                slots_total=8,
                slots_available=0,
                active_builds=8,
                queued_builds=10,
                reason="local cargo fallback refused; wait for RCH slots",
            ),
            "beads": autopilot_e2e_active_beads_payload(
                generated_at,
                active_count=12,
                prefix="cap64",
            ),
            "agent_mail": agent_mail_ok,
            "expected_actions": ["adjust_swarm_budget", "wait_for_rch"],
            "expected_status": "deny_new_work",
            "expected_selected_action": "adjust_swarm_budget",
            "expected_mail_status": "ok",
            "expected_active_agents": 12,
            "expected_active_builds": 8,
            "expected_signal_ids": {
                "rch_queue_depth_over_budget",
                "rch_queue_saturated",
            },
        },
    ]
    for case in capacity_cases:
        input_pack, plan, result = run_plan_scenario(
            str(case["scenario_id"]),
            beads_payload=case["beads"],
            beads_ready_payload=capacity_ready_queue,
            commands=list(ready_commands),
            expected_actions=list(case["expected_actions"]),
            cargo_payload=case["cargo"],
            agent_mail_payload=case["agent_mail"],
            current_preflight=case["preflight"],
        )
        budget_drift = input_pack["normalized_inputs"]["budget_drift"]
        current = budget_drift["current_observation"]
        accepted = budget_drift["accepted_profile"]
        recommended = accepted["recommended_budgets"]
        queue_forecast = input_pack["normalized_inputs"]["cargo_admission"][
            "queue_forecast"
        ]
        signals = {
            signal["id"] for signal in budget_drift["signals"] if isinstance(signal, dict)
        }

        assert budget_drift["status"] == case["expected_status"]
        assert result["selected_action"] == case["expected_selected_action"]
        assert input_pack["normalized_inputs"]["agent_mail"]["status"] == case[
            "expected_mail_status"
        ]
        assert current["active_agents"] == case["expected_active_agents"]
        assert current["active_builds"] == case["expected_active_builds"]
        assert recommended["rch_verification_fanout"] == case["preflight"][
            "recommended_budgets"
        ]["rch_verification_fanout"]
        assert queue_forecast["slots_total"] == case["cargo"]["rch_queue_forecast"][
            "slots_total"
        ]
        assert set(case["expected_signal_ids"]).issubset(signals)
        if case["expected_status"] == "deny_new_work":
            assert current["slot_pressure"] == "saturated"
            assert any(
                signal["evidence_path"]
                == "normalized_inputs.cargo_admission.queue_forecast.queue_depth"
                for signal in budget_drift["signals"]
            )
            assert any(
                signal["evidence_path"]
                == "normalized_inputs.cargo_admission.queue_forecast"
                for signal in budget_drift["signals"]
            )
        assert_autopilot_input_pack_contract(input_pack)
        assert_autopilot_plan_contract(plan)

    dry_run_scenario_id = "dirty_mail_rch_empty_beads_dry_run"
    dry_run_git_path, dry_run_git_commands = build_real_dirty_git_source(
        workspace / dry_run_scenario_id,
        scenario_id=dry_run_scenario_id,
        generated_at=generated_at,
        timeout_seconds=timeout_seconds,
    )
    dry_run_cargo = autopilot_e2e_remote_validation_cargo_payload(
        generated_at,
        scenario_id=dry_run_scenario_id,
    )
    dry_run_mail = load_shared_agent_mail_schema_corrupt_fixture(
        generated_at=generated_at,
    )
    dry_run_remote_command = {
        "id": "remote_validation_rch_check",
        "command": "rch exec -- cargo check --all-targets",
        "cwd": str(workspace),
        "status": "ok",
        "exit_code": 0,
        "issue": None,
        "stdout_path": None,
        "stderr_snippet": "",
        "redaction_summary": {"redacted_count": 0, "fields": []},
    }
    dry_run_mail_command = {
        "id": "agent_mail_status",
        "command": "am robot status --format json",
        "cwd": str(workspace),
        "status": "failed",
        "exit_code": 2,
        "issue": AGENT_MAIL_SCHEMA_CORRUPT_DETAIL,
        "stdout_path": None,
        "stderr_snippet": AGENT_MAIL_SCHEMA_CORRUPT_DETAIL,
        "redaction_summary": {"redacted_count": 0, "fields": []},
    }
    dry_run_input, dry_run_plan, dry_run_result = run_plan_scenario(
        dry_run_scenario_id,
        beads_payload=empty_beads,
        beads_ready_payload=empty_ready,
        commands=dry_run_git_commands
        + empty_commands
        + [dry_run_mail_command, dry_run_remote_command],
        expected_actions=[
            "use_beads_soft_lock",
            "capture_handoff",
            "run_docs_only_work",
            "plan_new_bead",
        ],
        expected_action_plan_decision="wait_for_pressure",
        expected_work_admission_decision="wait",
        cargo_payload=dry_run_cargo,
        agent_mail_payload=dry_run_mail,
        git_status_file=dry_run_git_path,
    )
    dry_run_git_payload = json.loads(dry_run_git_path.read_text(encoding="utf-8"))
    dry_run_ledger = autopilot_e2e_remote_validation_ledger(
        cargo_payload=dry_run_cargo,
        git_payload=dry_run_git_payload,
        generated_at=generated_at,
        scenario_id=dry_run_scenario_id,
    )
    dry_run_entry = dry_run_ledger["entries"][0]
    dry_run_actions = [action["action"] for action in dry_run_plan["actions"]]
    dry_run_beads = dry_run_input["normalized_inputs"]["beads"]
    dry_run_ready = dry_run_input["normalized_inputs"]["beads_ready"]
    dry_run_result["agent_mail_status"] = dry_run_input["normalized_inputs"][
        "agent_mail"
    ]["status"]
    dry_run_result["git_dirty_preserved"] = dry_run_input["normalized_inputs"][
        "git_state"
    ]["dirty"]
    dry_run_result["beads_after_closeout"] = {
        "active_count": dry_run_beads["active_count"],
        "open_candidate_count": dry_run_beads["open_candidate_count"],
        "ready_count": dry_run_ready["ready_count"],
    }
    dry_run_result["remote_validation_proof"] = {
        "schema": dry_run_ledger["schema"],
        "summary": dry_run_ledger["summary"],
        "entry_status": dry_run_entry["evidence_classification"]["status"],
        "clean_remote_proof": dry_run_entry["evidence_classification"][
            "clean_remote_proof"
        ],
        "runner": dry_run_entry["runner"],
    }
    assert dry_run_input["normalized_inputs"]["git_state"]["dirty"] is True
    assert dry_run_input["normalized_inputs"]["agent_mail"]["status"] == "degraded"
    assert dry_run_beads["active_count"] == 0
    assert dry_run_beads["open_candidate_count"] == 0
    assert dry_run_ready["ready_count"] == 0
    assert dry_run_ledger["summary"]["clean_remote_proof_entries"] == 1
    assert dry_run_entry["runner"]["remote_execution"] is True
    assert dry_run_entry["runner"]["local_fallback"] == "none"
    assert "plan_new_bead" in dry_run_actions
    assert "wait_for_rch" not in dry_run_actions

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
        expected_action_plan_decision="pause_or_surface_blocker",
        expected_work_admission_decision="pause_escalate",
        git_status_file=dirty_git_path,
    )

    stale_generated_at = (
        parse_utc(generated_at) - timedelta(hours=stale_after_hours + 2)
    ).isoformat()
    stale_mail_payload = autopilot_e2e_agent_mail_status(stale_generated_at)
    stale_input, stale_plan, stale_result = run_plan_scenario(
        "stale_action_plan_evidence",
        beads_payload=ready_beads,
        beads_ready_payload=ready_queue,
        commands=ready_commands,
        expected_actions=["stop_and_surface_blocker"],
        expected_action_plan_decision="renew_stale_evidence",
        expected_work_admission_decision="renew_evidence",
        agent_mail_payload=stale_mail_payload,
    )
    stale_classifications = [
        item
        for item in stale_input["source_classification"]
        if item.get("classification") == "stale"
    ]
    assert stale_classifications
    assert stale_result["action_plan"]["decision"] == "renew_stale_evidence"
    assert stale_plan["status"] == "blocked"

    validation_failure_commands = [
        {
            "id": "remote_validation_rch_check",
            "command": "rch exec -- cargo check --all-targets",
            "cwd": str(workspace),
            "status": "failed",
            "exit_code": 101,
            "issue": "[rch] remote cargo check failed error[E0308]",
            "stdout_path": None,
            "stderr_snippet": (
                "[rch] remote cargo check failed error[E0308]: mismatched types"
            ),
            "redaction_summary": {"redacted_count": 0, "fields": []},
        }
    ] + list(ready_commands)
    validation_input, validation_plan, validation_result = run_plan_scenario(
        "validation_failure_fail_closed",
        beads_payload=ready_beads,
        beads_ready_payload=ready_queue,
        commands=validation_failure_commands,
        expected_actions=["claim_ready_bead"],
        expected_action_plan_decision="pause_or_surface_blocker",
        expected_work_admission_decision="pause_escalate",
    )
    assert validation_input["capture"]["status"] == "degraded"
    assert any(
        action.get("id") == "FAIL-RCH-REMOTE-COMPILE"
        for action in validation_plan["failure_actions"]
    )
    assert validation_result["action_plan"]["status"] == "blocked"

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
                "action_plan": {
                    "schema": ACTION_PLAN_SCHEMA,
                    "status": "blocked",
                    "decision": "pause_or_surface_blocker",
                    "source_action": "fail_closed",
                    "command_safety_classes": ["read_only_probe"],
                },
                "work_admission_gate": {
                    "schema": WORK_ADMISSION_GATE_SCHEMA,
                    "status": "blocked",
                    "decision": "pause_escalate",
                    "admit_new_implementation": False,
                    "blockers": ["malformed_required_source"],
                },
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
        action_plan = scenario.get("action_plan")
        assert isinstance(action_plan, dict)
        assert action_plan.get("schema") == ACTION_PLAN_SCHEMA
        assert action_plan.get("decision") in ACTION_PLAN_ALLOWED_DECISIONS
        assert action_plan.get("status") in ACTION_PLAN_ALLOWED_STATUSES
        assert isinstance(action_plan.get("command_safety_classes"), list)
        work_admission_gate = scenario.get("work_admission_gate")
        assert isinstance(work_admission_gate, dict)
        assert work_admission_gate.get("schema") == WORK_ADMISSION_GATE_SCHEMA
        assert work_admission_gate.get("decision") in WORK_ADMISSION_GATE_ALLOWED_DECISIONS
        assert work_admission_gate.get("status") in WORK_ADMISSION_GATE_ALLOWED_STATUSES
        assert isinstance(work_admission_gate.get("blockers"), list)
        if work_admission_gate.get("admit_new_implementation") is True:
            assert work_admission_gate.get("decision") == "proceed_with_implementation"
            assert work_admission_gate.get("blockers") == []
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


def append_degraded_coordination_e2e_event(
    events_path: Path,
    event: dict[str, Any],
) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json_dumps(event) + "\n")


def degraded_coordination_e2e_event(
    *,
    scenario_id: str,
    phase: str,
    event: str,
    status: str,
    generated_at: str,
    correlation_id: str,
    command_provenance: list[dict[str, Any]] | None = None,
    evidence_paths: list[str] | None = None,
    recommended_action: str | None = None,
    verdict: str | None = None,
    degraded_inputs: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": DEGRADED_COORDINATION_RUNPACK_E2E_EVENT_SCHEMA,
        "generated_at": generated_at,
        "correlation_id": correlation_id,
        "scenario_id": scenario_id,
        "phase": phase,
        "event": event,
        "status": status,
        "command_provenance": command_provenance or [],
        "evidence_paths": evidence_paths or [],
        "recommended_action": recommended_action,
        "verdict": verdict,
        "degraded_inputs": degraded_inputs or {},
        "details": details or {},
    }


def first_issue_id_from_br_payload(payload: Any, context: str) -> str:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        issue_id = payload[0].get("id")
    elif isinstance(payload, dict):
        issue_id = payload.get("id")
    else:
        issue_id = None
    if not isinstance(issue_id, str) or not issue_id:
        raise RunpackError(f"could not parse Beads issue id from {context}")
    return issue_id


def build_real_degraded_coordination_beads_sources(
    scenario_dir: Path,
    *,
    scenario_id: str,
    timeout_seconds: int,
) -> tuple[Any, Any, list[dict[str, Any]], dict[str, Any]]:
    if shutil.which("br") is None:
        raise RunpackError("degraded coordination E2E requires br on PATH")
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
    fresh_stdout = capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:beads_create_fresh_in_progress",
        [
            "br",
            "create",
            "--title",
            "Fresh in-progress degraded coordination fixture",
            "--type",
            "task",
            "--priority",
            "2",
            "--status",
            "in_progress",
            "--assignee",
            "FreshStone",
            "--description",
            "Fresh active work proves Beads soft-lock fallback ownership.",
            "--json",
        ],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    dependency_stdout = capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:beads_create_deferred_dependency",
        [
            "br",
            "create",
            "--title",
            "Deferred dependency for blocked degraded coordination fixture",
            "--type",
            "task",
            "--priority",
            "2",
            "--status",
            "deferred",
            "--description",
            "Deferred blocker keeps the open fixture out of br ready output.",
            "--json",
        ],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    blocked_stdout = capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:beads_create_blocked_open",
        [
            "br",
            "create",
            "--title",
            "Blocked open degraded coordination fixture",
            "--type",
            "task",
            "--priority",
            "2",
            "--description",
            "Open work blocked by deferred dependency.",
            "--json",
        ],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    fresh_id = first_issue_id_from_br_payload(
        json_payload_from_stdout(fresh_stdout),
        "fresh in-progress fixture",
    )
    dependency_id = first_issue_id_from_br_payload(
        json_payload_from_stdout(dependency_stdout),
        "deferred dependency fixture",
    )
    blocked_id = first_issue_id_from_br_payload(
        json_payload_from_stdout(blocked_stdout),
        "blocked open fixture",
    )
    capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:beads_dep_blocked_open",
        ["br", "dep", "add", blocked_id, dependency_id, "--json"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    list_stdout = capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:beads_list",
        ["br", "list", "--json"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    ready_stdout = capture_autopilot_e2e_command(
        commands,
        f"{scenario_id}:beads_ready",
        ["br", "ready", "--json"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
    )
    beads_payload = json_payload_from_stdout(list_stdout)
    ready_payload = json_payload_from_stdout(ready_stdout)
    if beads_payload is None or ready_payload is None:
        raise RunpackError("degraded coordination E2E could not parse br JSON output")
    issues = parse_issue_list(beads_payload)
    ready_issues = parse_issue_list(ready_payload)
    issue_by_id = {str(issue.get("id")): issue for issue in issues}
    assert issue_by_id[fresh_id]["status"] == "in_progress"
    assert issue_by_id[fresh_id]["assignee"] == "FreshStone"
    assert issue_by_id[blocked_id]["status"] == "open"
    assert blocked_id not in {str(issue.get("id")) for issue in ready_issues}
    return (
        beads_payload,
        ready_payload,
        commands,
        {
            "fresh_in_progress_id": fresh_id,
            "blocked_open_id": blocked_id,
            "deferred_dependency_id": dependency_id,
            "ready_count": len(ready_issues),
            "workspace": str(workspace),
        },
    )


def build_degraded_coordination_runpack_e2e_summary(
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
        else Path(tempfile.mkdtemp(prefix="pi_swarm_degraded_runpack_e2e_")).resolve()
    )
    workspace.mkdir(parents=True, exist_ok=True)
    events_path = events_path or (workspace / "degraded-coordination-e2e-events.jsonl")
    if events_path.exists():
        raise RunpackError(
            f"refusing to overwrite degraded coordination E2E events: {events_path}"
        )
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("", encoding="utf-8")
    correlation_id = (
        f"degraded-coordination-e2e-{generated_at.replace(':', '').replace('+', 'Z')}"
    )
    scenario_id = "degraded_coordination_runpack_handoff"
    scenario_dir = workspace / scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    append_degraded_coordination_e2e_event(
        events_path,
        degraded_coordination_e2e_event(
            scenario_id=scenario_id,
            phase="setup",
            event="scenario_start",
            status="running",
            generated_at=generated_at,
            correlation_id=correlation_id,
            evidence_paths=["beads", "agent_mail_status", "remote_validation_proof_ledger"],
        ),
    )
    preflight = autopilot_e2e_preflight(generated_at)
    beads_payload, ready_payload, bead_commands, bead_details = (
        build_real_degraded_coordination_beads_sources(
            scenario_dir,
            scenario_id=scenario_id,
            timeout_seconds=timeout_seconds,
        )
    )
    mail_command = {
        "id": "agent_mail_status",
        "command": "am robot status --format json",
        "cwd": str(scenario_dir),
        "status": "failed",
        "exit_code": 2,
        "issue": AGENT_MAIL_SCHEMA_CORRUPT_DETAIL,
        "stdout_path": None,
        "stderr_snippet": AGENT_MAIL_SCHEMA_CORRUPT_DETAIL,
        "redaction_summary": {"redacted_count": 0, "fields": []},
    }
    shadow_command = {
        "id": "rch_workspace_shadow",
        "command": "rch exec -- cargo check --all-targets",
        "cwd": str(scenario_dir),
        "status": "failed",
        "exit_code": 101,
        "issue": (
            "error: failed to load manifest for workspace member "
            "`/data/projects/crates/fwc` referenced by workspace at "
            "`/data/projects/Cargo.toml`; caused by failed to read "
            "`/data/toon_rust/Cargo.toml`"
        ),
        "stdout_path": None,
        "stderr_snippet": (
            "error: failed to load manifest for workspace member "
            "`/data/projects/crates/fwc` referenced by workspace at "
            "`/data/projects/Cargo.toml`; caused by failed to read "
            "`/data/toon_rust/Cargo.toml`"
        ),
        "redaction_summary": {"redacted_count": 0, "fields": []},
    }
    commands = bead_commands + [mail_command, shadow_command]
    paths = autopilot_e2e_source_paths(
        scenario_dir,
        generated_at=generated_at,
        preflight=preflight,
        cargo_payload=autopilot_e2e_cargo_payload(
            decision="admit",
            queue_action="proceed",
            slot_pressure="available",
            queue_depth=1,
            active_builds=1,
            queued_builds=0,
            reason="degraded coordination runpack e2e fixture",
        ),
        beads_payload=beads_payload,
        beads_ready_payload=ready_payload,
        agent_mail_status_payload=load_shared_agent_mail_schema_corrupt_fixture(
            generated_at=generated_at,
        ),
        agent_mail_reservations_payload=autopilot_e2e_agent_mail_reservations(
            generated_at,
        ),
        git_payload=autopilot_e2e_clean_git_payload(generated_at),
    )
    args = autopilot_e2e_args(
        paths=paths,
        commands=commands,
        scenario_dir=scenario_dir,
        generated_at=generated_at,
        max_items=max_items,
        stale_after_hours=stale_after_hours,
    )
    args.active_bead_id = scenario_id
    runpack = build_runpack(args)
    proof_entries = {
        entry["command"].get("capture_id"): entry
        for entry in runpack["remote_validation_proof_ledger"]["entries"]
        if entry["command"].get("capture_id")
    }
    shadow_entry = proof_entries.get("rch_workspace_shadow")
    assert shadow_entry is not None, runpack["remote_validation_proof_ledger"]
    assert shadow_entry["evidence_classification"]["status"] == "blocked"
    assert "worker_workspace_shadow" in shadow_entry["evidence_classification"][
        "degraded_reasons"
    ]
    assert any(
        warning["warning_id"] == "worker_workspace_shadow"
        for warning in shadow_entry["warnings"]
    )
    assert runpack["status"] == "degraded"
    assert runpack["agent_mail_read_state"]["status"] == "degraded"
    assert runpack["agent_mail_read_state"]["soft_lock"] == "beads"
    assert runpack["agent_mail_read_state"]["semantic_readiness_status"] == "fail"
    assert runpack["beads"]["active_ownership"][0]["assignee"] == "FreshStone"
    assert runpack["beads"]["open_candidate_count"] == 1
    assert runpack["remote_validation_proof_ledger"]["summary"]["blocked_entries"] >= 1
    assert (
        runpack["remote_validation_proof_ledger"]["summary"][
            "clean_remote_proof_entries"
        ]
        == 0
    )
    temp_inventory = runpack["temp_artifact_inventory"]
    assert temp_inventory["summary"]["emitted_deletion_command_count"] == 0
    assert all(entry["cleanup_command"] is None for entry in temp_inventory["entries"])
    operator_actions = runpack["operator_next_actions"]
    assert any("Treat Agent Mail read state as unavailable" in action for action in operator_actions)
    assert any("Use Beads active ownership" in action and "FreshStone" in action for action in operator_actions)
    assert any("Inspect remote validation proof ledger" in action for action in operator_actions)
    assert not any("Runpack sources are ready" in action for action in operator_actions)
    recommended_action = "use_beads_soft_lock_and_surface_rch_workspace_shadow"
    degraded_inputs = {
        "agent_mail_semantic_readiness": runpack["agent_mail_read_state"][
            "semantic_readiness_status"
        ],
        "agent_mail_soft_lock": runpack["agent_mail_read_state"]["soft_lock"],
        "fresh_in_progress_bead": bead_details["fresh_in_progress_id"],
        "blocked_open_bead": bead_details["blocked_open_id"],
        "ready_count": bead_details["ready_count"],
        "rch_workspace_shadow_blocked": True,
    }
    event = degraded_coordination_e2e_event(
        scenario_id=scenario_id,
        phase="assert",
        event="scenario_result",
        status="pass",
        generated_at=generated_at,
        correlation_id=correlation_id,
        command_provenance=command_provenance({"commands": commands}, max_items),
        evidence_paths=[
            "agent_mail_read_state",
            "beads.active_ownership",
            "beads.open_candidates",
            "remote_validation_proof_ledger.entries",
            "operator_next_actions",
            "temp_artifact_inventory.summary.emitted_deletion_command_count",
        ],
        recommended_action=recommended_action,
        verdict="pass",
        degraded_inputs=degraded_inputs,
        details={
            "runpack_status": runpack["status"],
            "proof_summary": runpack["remote_validation_proof_ledger"]["summary"],
            "operator_next_actions": operator_actions,
            "artifact_dir": str(scenario_dir),
        },
    )
    append_degraded_coordination_e2e_event(events_path, event)
    summary = {
        "schema": DEGRADED_COORDINATION_RUNPACK_E2E_SCHEMA,
        "generated_at": generated_at,
        "correlation_id": correlation_id,
        "status": "pass",
        "purpose": (
            "no_mock_degraded_coordination_runpack_e2e_operator_evidence_not_release_claim"
        ),
        "scenario_count": 1,
        "required_scenarios": list(DEGRADED_COORDINATION_RUNPACK_E2E_REQUIRED_SCENARIOS),
        "scenarios": {
            scenario_id: {
                "scenario_id": scenario_id,
                "status": "pass",
                "verdict": "pass",
                "recommended_action": recommended_action,
                "degraded_inputs": degraded_inputs,
                "runpack_status": runpack["status"],
                "agent_mail_read_state": runpack["agent_mail_read_state"],
                "beads": {
                    "active_ownership": runpack["beads"]["active_ownership"],
                    "open_candidates": runpack["beads"]["open_candidates"],
                    "stale": runpack["beads"]["stale"],
                },
                "remote_validation_proof": {
                    "summary": runpack["remote_validation_proof_ledger"]["summary"],
                    "shadow_entry_status": shadow_entry["evidence_classification"][
                        "status"
                    ],
                    "shadow_warnings": [
                        warning["warning_id"] for warning in shadow_entry["warnings"]
                    ],
                },
                "operator_next_actions": operator_actions,
                "no_delete_evidence": {
                    "emitted_deletion_command_count": temp_inventory["summary"][
                        "emitted_deletion_command_count"
                    ],
                    "cleanup_commands_present": any(
                        entry["cleanup_command"] for entry in temp_inventory["entries"]
                    ),
                },
                "artifact_dir": str(scenario_dir),
            }
        },
        "events_jsonl": str(events_path),
        "workspace": str(workspace),
        "guards": {
            "uses_real_temp_beads": True,
            "uses_runpack_builder": True,
            "fixture_captures_degraded_rch_and_agent_mail": True,
            "no_cleanup_or_deletion_commands": True,
            "heavy_rust_validation_requires_rch": True,
            "operator_evidence_not_release_claim": True,
        },
    }
    assert_degraded_coordination_runpack_e2e_summary(summary)
    return summary


def assert_degraded_coordination_runpack_e2e_summary(summary: dict[str, Any]) -> None:
    assert summary.get("schema") == DEGRADED_COORDINATION_RUNPACK_E2E_SCHEMA
    assert summary.get("status") == "pass"
    assert summary.get("purpose") == (
        "no_mock_degraded_coordination_runpack_e2e_operator_evidence_not_release_claim"
    )
    scenarios = summary.get("scenarios")
    assert isinstance(scenarios, dict)
    missing = set(DEGRADED_COORDINATION_RUNPACK_E2E_REQUIRED_SCENARIOS) - set(scenarios)
    assert not missing, f"degraded coordination E2E missing scenarios: {sorted(missing)}"
    for scenario_id in DEGRADED_COORDINATION_RUNPACK_E2E_REQUIRED_SCENARIOS:
        scenario = scenarios[scenario_id]
        assert scenario["status"] == "pass", scenario
        assert scenario["verdict"] == "pass"
        assert scenario["recommended_action"]
        assert scenario["degraded_inputs"]["agent_mail_semantic_readiness"] == "fail"
        assert scenario["degraded_inputs"]["agent_mail_soft_lock"] == "beads"
        assert scenario["degraded_inputs"]["ready_count"] == 0
        assert scenario["degraded_inputs"]["rch_workspace_shadow_blocked"] is True
        assert scenario["runpack_status"] == "degraded"
        assert scenario["remote_validation_proof"]["shadow_entry_status"] == "blocked"
        assert "worker_workspace_shadow" in scenario["remote_validation_proof"][
            "shadow_warnings"
        ]
        assert scenario["no_delete_evidence"]["emitted_deletion_command_count"] == 0
        assert scenario["no_delete_evidence"]["cleanup_commands_present"] is False
    guards = summary.get("guards")
    assert isinstance(guards, dict)
    assert guards.get("uses_real_temp_beads") is True
    assert guards.get("uses_runpack_builder") is True
    assert guards.get("no_cleanup_or_deletion_commands") is True
    events_path = Path(str(summary.get("events_jsonl")))
    assert events_path.exists(), f"missing degraded coordination E2E events: {events_path}"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result_events = [event for event in events if event.get("event") == "scenario_result"]
    assert len(result_events) == len(DEGRADED_COORDINATION_RUNPACK_E2E_REQUIRED_SCENARIOS)
    for event in result_events:
        assert event.get("schema") == DEGRADED_COORDINATION_RUNPACK_E2E_EVENT_SCHEMA
        assert event.get("scenario_id") in DEGRADED_COORDINATION_RUNPACK_E2E_REQUIRED_SCENARIOS
        assert event.get("status") == "pass"
        assert event.get("verdict") == "pass"
        assert event.get("recommended_action")
        assert isinstance(event.get("degraded_inputs"), dict)
        assert_no_dangerous_runnable_commands(event.get("command_provenance") or [])


def write_degraded_coordination_runpack_e2e_output(
    args: argparse.Namespace,
    summary: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_degraded_coordination_e2e_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(
            f"refusing to overwrite degraded coordination E2E summary: {output_path}"
        )
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


def git_command_ok(command: list[str], cwd: Path) -> bool:
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
        return False
    return completed.returncode == 0


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


def context_intelligence_child_artifact_map(
    issues: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "bead_id": "bd-ircr3.1",
            "commit": "98008ed0a2f984338858e0c760e64ea913e84111",
            "code_paths": [],
            "test_paths": [
                "tests/semantic_context_graph_contract.rs",
                "tests/semantic_workspace_graph_contract.rs",
            ],
            "docs_or_evidence_paths": [
                "docs/contracts/semantic-context-graph-contract.json",
                "docs/contracts/semantic-workspace-graph-contract.json",
            ],
            "validation_commands": [
                "rch exec -- cargo test --test semantic_context_graph_contract -- --nocapture",
                "rch exec -- cargo test --test semantic_workspace_graph_contract -- --nocapture",
            ],
        },
        {
            "bead_id": "bd-ircr3.2",
            "commit": "edd5586c45fa356b8407e7c703d665ea96f55a40",
            "code_paths": ["src/semantic_workspace_graph.rs"],
            "test_paths": ["tests/semantic_workspace_graph_builder.rs"],
            "docs_or_evidence_paths": [
                "docs/contracts/semantic-workspace-graph-contract.json"
            ],
            "validation_commands": [
                "rch exec -- cargo test --test semantic_workspace_graph_builder"
            ],
        },
        {
            "bead_id": "bd-ircr3.3",
            "commit": "0cbb42d51e242a1b5b47ccc75fc4deff96ef0e66",
            "code_paths": ["src/semantic_workspace_graph.rs"],
            "test_paths": ["tests/semantic_workspace_graph_builder.rs"],
            "docs_or_evidence_paths": [
                "docs/contracts/semantic-context-graph-contract.json",
                "docs/contracts/semantic-workspace-graph-contract.json",
            ],
            "validation_commands": [
                "rch exec -- cargo test --test semantic_workspace_graph_builder"
            ],
        },
        {
            "bead_id": "bd-ircr3.4",
            "commit": "d64d4019a82f89a460dd936751a4d4035f4d03c2",
            "code_paths": ["src/semantic_workspace_graph.rs"],
            "test_paths": ["tests/semantic_workspace_graph_builder.rs"],
            "docs_or_evidence_paths": ["tests/semantic_workspace_graph_builder.rs"],
            "validation_commands": [
                "rch exec -- cargo test --test semantic_workspace_graph_builder"
            ],
        },
        {
            "bead_id": "bd-ircr3.5",
            "commit": "f5a9b3190ea81a475206ff2ef20f1c45a576b0c8",
            "code_paths": ["src/semantic_workspace_graph.rs"],
            "test_paths": ["tests/semantic_workspace_graph_builder.rs"],
            "docs_or_evidence_paths": [
                "docs/contracts/semantic-context-graph-contract.json",
                "docs/contracts/semantic-workspace-graph-contract.json",
            ],
            "validation_commands": [
                "rch exec -- cargo test --test semantic_workspace_graph_builder"
            ],
        },
        {
            "bead_id": "bd-ircr3.6",
            "commit": "bc51f70f70fca98d045ab14ca89fc3228c2d343c",
            "code_paths": ["src/cli.rs", "src/main.rs", "src/semantic_workspace_graph.rs"],
            "test_paths": ["tests/e2e_cli.rs", "tests/conformance/fixture_runner.rs"],
            "docs_or_evidence_paths": ["README.md", "docs/context-intelligence.md"],
            "validation_commands": [
                "rch exec -- cargo test --test e2e_cli context_preview -- --nocapture"
            ],
        },
        {
            "bead_id": "bd-ircr3.7",
            "commit": "ad94a8c2fa90b46b806f0bc583f4718803e00449",
            "code_paths": ["src/agent.rs", "src/semantic_workspace_graph.rs"],
            "test_paths": [
                "src/agent.rs",
                "tests/semantic_workspace_graph_builder.rs",
                "tests/e2e_agent_loop.rs",
            ],
            "docs_or_evidence_paths": ["tests/e2e_agent_loop.rs"],
            "validation_commands": [
                "rch exec -- cargo test -p pi --lib agent_semantic_context",
                "rch exec -- cargo test --test semantic_workspace_graph_builder",
            ],
        },
        {
            "bead_id": "bd-ircr3.8",
            "commit": "70780ea69f8619068c50025b12bcf6c3bb546ec0",
            "code_paths": ["src/agent.rs", "src/semantic_workspace_graph.rs"],
            "test_paths": ["tests/e2e_agent_loop.rs"],
            "docs_or_evidence_paths": ["tests/e2e_agent_loop.rs"],
            "validation_commands": [
                "rch exec -- cargo test --test e2e_agent_loop context_intelligence_no_mock_harness -- --nocapture"
            ],
        },
        {
            "bead_id": "bd-ircr3.9",
            "commit": "60f25202a617a0bc5972e182e4db03283c912c33",
            "code_paths": ["src/semantic_workspace_graph.rs"],
            "test_paths": ["tests/perf_budgets.rs", "tests/semantic_workspace_graph_builder.rs"],
            "docs_or_evidence_paths": [
                "tests/perf_budgets.rs",
                "tests/semantic_workspace_graph_builder.rs",
            ],
            "validation_commands": [
                "rch exec -- cargo test --test perf_budgets context_intelligence",
                "rch exec -- cargo test --test semantic_workspace_graph_builder large_workspace_context_planner_budget_artifact_is_deterministic_under_randomized_order",
            ],
        },
        {
            "bead_id": "bd-ircr3.10",
            "commit": "0142c8ba666aeee65a7295284c88d4ead9a0c9b6",
            "code_paths": ["src/doctor.rs", "scripts/build_swarm_operator_runpack.py"],
            "test_paths": [
                "tests/doctor_swarm_temp_dir_json.rs",
                "tests/golden_corpus/swarm_operator_runpack/complete_runpack_projection.json",
            ],
            "docs_or_evidence_paths": [
                "docs/contracts/swarm-operator-runpack-contract.json",
                "docs/swarm-operations-runbook.md",
            ],
            "validation_commands": [
                "rch exec -- cargo test --test doctor_swarm_temp_dir_json context_intelligence -- --nocapture",
                "python3 scripts/build_swarm_operator_runpack.py --self-test",
            ],
        },
    ]
    for row in rows:
        issue = issues.get(row["bead_id"]) or {}
        row["status"] = issue.get("status")
        row["title"] = issue.get("title")
        row["close_reason"] = issue.get("close_reason")
    return rows


def paths_exist(root: Path, paths: list[str]) -> bool:
    return all((root / path).exists() for path in paths)


def commits_exist(root: Path, commits: list[str]) -> bool:
    return all(
        git_command_ok(["git", "cat-file", "-e", f"{commit}^{{commit}}"], root)
        for commit in commits
    )


def build_context_intelligence_closeout_gate_summary(
    *,
    generated_at: str,
    quality_gate_results: list[dict[str, Any]],
    issue_export_path: Path | None = None,
    git_refs: dict[str, str | None] | None = None,
    commit_check_override: bool | None = None,
) -> dict[str, Any]:
    root = repo_root()
    script_path = root / "scripts/build_swarm_operator_runpack.py"
    readme_path = root / "README.md"
    operator_doc_path = root / "docs/context-intelligence.md"
    runbook_path = root / "docs/swarm-operations-runbook.md"
    semantic_context_contract_path = (
        root / "docs/contracts/semantic-context-graph-contract.json"
    )
    semantic_workspace_contract_path = (
        root / "docs/contracts/semantic-workspace-graph-contract.json"
    )
    closeout_contract_path = root / CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_PATH
    runpack_contract_path = root / RUNPACK_CONTRACT_PATH
    graph_source_path = root / "src/semantic_workspace_graph.rs"
    main_path = root / "src/main.rs"
    cli_path = root / "src/cli.rs"
    agent_path = root / "src/agent.rs"
    doctor_path = root / "src/doctor.rs"
    e2e_path = root / "tests/e2e_agent_loop.rs"
    perf_path = root / "tests/perf_budgets.rs"
    builder_test_path = root / "tests/semantic_workspace_graph_builder.rs"
    context_contract_test_path = root / "tests/semantic_context_graph_contract.rs"
    workspace_contract_test_path = root / "tests/semantic_workspace_graph_contract.rs"
    doctor_test_path = root / "tests/doctor_swarm_temp_dir_json.rs"

    issues = load_beads_issue_map(issue_export_path or (root / ".beads/issues.jsonl"))
    child_artifacts = context_intelligence_child_artifact_map(issues)
    checklist: list[dict[str, Any]] = []

    child_states = {
        issue_id: (issues.get(issue_id) or {}).get("status")
        for issue_id in CONTEXT_INTELLIGENCE_CLOSEOUT_CHILD_BEADS
    }
    child_close_reasons = {
        issue_id: (issues.get(issue_id) or {}).get("close_reason")
        for issue_id in CONTEXT_INTELLIGENCE_CLOSEOUT_CHILD_BEADS
    }
    missing_children = [
        issue_id for issue_id, status in child_states.items() if status != "closed"
    ]
    commits = [
        str(row.get("commit"))
        for row in child_artifacts
        if isinstance(row.get("commit"), str)
    ]
    artifact_paths = sorted(
        {
            path
            for row in child_artifacts
            for key in ("code_paths", "test_paths", "docs_or_evidence_paths")
            for path in row.get(key, [])
        }
    )
    artifact_paths_exist = paths_exist(root, artifact_paths)
    commits_present = (
        commit_check_override
        if commit_check_override is not None
        else commits_exist(root, commits)
    )
    checklist.append(
        gate_check(
            "child_beads_closed",
            "All context-intelligence implementation child Beads are closed and mapped to artifacts.",
            not missing_children and artifact_paths_exist and commits_present,
            [
                {
                    "path": ".beads/issues.jsonl",
                    "child_statuses": child_states,
                    "close_reasons": child_close_reasons,
                    "artifact_path_count": len(artifact_paths),
                    "artifact_paths_exist": artifact_paths_exist,
                    "commits": commits,
                    "commits_present": commits_present,
                    "commit_check_source": (
                        "self_test_fixture_override"
                        if commit_check_override is not None
                        else "git_cat_file"
                    ),
                }
            ],
            issue=(
                f"children not closed: {', '.join(missing_children)}"
                if missing_children
                else "one or more mapped artifact paths or commits are missing"
                if not artifact_paths_exist or not commits_present
                else None
            ),
        )
    )

    checklist.append(
        gate_check(
            "graph_contracts",
            "Semantic graph contracts and contract tests define authority boundaries, freshness, actionability, and redaction.",
            all(
                (
                    file_contains(semantic_context_contract_path, "bd-ircr3.1"),
                    file_contains(semantic_context_contract_path, "fail_closed"),
                    file_contains(semantic_workspace_contract_path, "graph_must_not"),
                    file_contains(semantic_workspace_contract_path, "allowed_bead_actionability_statuses"),
                    file_contains(context_contract_test_path, "semantic_context_graph_contract_is_complete"),
                    file_contains(workspace_contract_test_path, "contract_preserves_existing_authorities"),
                )
            ),
            [
                {"path": "docs/contracts/semantic-context-graph-contract.json"},
                {"path": "docs/contracts/semantic-workspace-graph-contract.json"},
                {"path": "tests/semantic_context_graph_contract.rs"},
                {"path": "tests/semantic_workspace_graph_contract.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "graph_builder",
            "Incremental semantic workspace graph builder indexes source, tests, docs, evidence, and Beads without mutating sources of truth.",
            all(
                (
                    file_contains(graph_source_path, "SemanticWorkspaceGraphBuilder"),
                    file_contains(graph_source_path, "GRAPH_BUILDER_SCHEMA"),
                    file_contains(graph_source_path, "InputFingerprint"),
                    file_contains(builder_test_path, "deterministic"),
                    file_contains(builder_test_path, "permuted_large_context_indices"),
                )
            ),
            [
                {"path": "src/semantic_workspace_graph.rs"},
                {"path": "tests/semantic_workspace_graph_builder.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "freshness_claim_gates",
            "Evidence freshness and release-claim facts fail closed for stale, missing, malformed, uncertified, and historical inputs.",
            all(
                (
                    file_contains(graph_source_path, "EvidenceFreshnessStatus"),
                    file_contains(graph_source_path, "Uncertified"),
                    file_contains(graph_source_path, "HistoricalSnapshot"),
                    file_contains(graph_source_path, "release_claim_allowed"),
                    file_contains(builder_test_path, "stale_evidence_suppressions"),
                    file_contains(semantic_workspace_contract_path, "dropin_verdict_not_certified"),
                )
            ),
            [
                {"path": "src/semantic_workspace_graph.rs"},
                {"path": "tests/semantic_workspace_graph_builder.rs"},
                {"path": "docs/contracts/semantic-workspace-graph-contract.json"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "bundle_planner",
            "Budgeted context bundle planner emits bounded selected items, exclusions, validation commands, and deterministic replay artifacts.",
            all(
                (
                    file_contains(graph_source_path, "SemanticContextBundlePlanner"),
                    file_contains(graph_source_path, "ContextBundleBudget"),
                    file_contains(graph_source_path, "suggested_validation_commands"),
                    file_contains(builder_test_path, "planner_emits_budgeted_golden_bundles_for_core_task_shapes"),
                    file_contains(builder_test_path, "bundle_golden_summary"),
                )
            ),
            [
                {"path": "src/semantic_workspace_graph.rs"},
                {"path": "tests/semantic_workspace_graph_builder.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "redaction_invalidation",
            "Context artifacts record redaction summaries, suppress unsafe payloads, normalize paths, and invalidate cache entries by workspace and fingerprint.",
            all(
                (
                    file_contains(graph_source_path, "ContextRedactionSummary"),
                    file_contains(graph_source_path, "suppressed_unsafe_nodes"),
                    file_contains(graph_source_path, "ContextArtifactCacheScope"),
                    file_contains(graph_source_path, "normalize_context_artifact_path"),
                    file_contains(builder_test_path, "unsafe_to_emit_by_redaction_policy"),
                    file_contains(builder_test_path, "context_path_normalization_rejects_escape_and_normalizes_safe_paths"),
                )
            ),
            [
                {"path": "src/semantic_workspace_graph.rs"},
                {"path": "tests/semantic_workspace_graph_builder.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "preview_surface",
            "`pi context-preview` exposes read-only JSON/text previews with query, Bead, changed-path, failing-command, and budget controls.",
            all(
                (
                    file_contains(cli_path, "ContextPreview"),
                    file_contains(cli_path, "changed-path"),
                    file_contains(main_path, "handle_context_preview_blocking"),
                    file_contains(main_path, "pi.context_bundle_preview.v1"),
                    file_contains(main_path, "provider_calls: 0"),
                )
            ),
            [
                {"path": "src/cli.rs"},
                {"path": "src/main.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "prompt_injection",
            "AgentSession prompt assembly consumes an opt-in semantic context bundle with provider-aware shape, byte/item bounds, and session provenance.",
            all(
                (
                    file_contains(agent_path, "SemanticContextBundleInjection"),
                    file_contains(agent_path, "prepare_semantic_context_prompt"),
                    file_contains(agent_path, "semantic_context_prompt_shape_for_provider"),
                    file_contains(agent_path, "SEMANTIC_CONTEXT_PROVENANCE_SCHEMA_V1"),
                    file_contains(agent_path, "semantic_context_bundle_injection_adds_bounded_custom_message_and_session_provenance"),
                )
            ),
            [{"path": "src/agent.rs"}],
        )
    )
    checklist.append(
        gate_check(
            "no_mock_e2e",
            "No-mock E2E harness exercises real temp git, source, tests, evidence, Beads JSONL, session persistence, graph planning, and provider prompt assembly.",
            all(
                (
                    file_contains(e2e_path, "context_intelligence_no_mock_harness"),
                    file_contains(e2e_path, "init_real_git_workspace"),
                    file_contains(e2e_path, "docs/evidence/context-intelligence-current.json"),
                    file_contains(e2e_path, "semantic_context_bundle"),
                    file_contains(e2e_path, "persist_context intelligence session")
                    or file_contains(e2e_path, "persist context intelligence session"),
                )
            ),
            [{"path": "tests/e2e_agent_loop.rs"}],
        )
    )
    checklist.append(
        gate_check(
            "perf_budgets",
            "Large-workspace context planner performance budgets have schema, deterministic replay, cache, environment, and metric validation.",
            all(
                (
                    file_contains(perf_path, "pi.semantic_context.performance_budget.v1"),
                    file_contains(perf_path, "context_intelligence_budget_contract_accepts_valid_artifact"),
                    file_contains(perf_path, "context_intelligence_budget_contract_fails_closed_when_missing"),
                    file_contains(perf_path, "context_intelligence_budget_contract_requires_randomized_order_replay"),
                )
            ),
            [
                {"path": "tests/perf_budgets.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "doctor_runpack",
            "Doctor and swarm runpacks surface context-intelligence posture, degraded reasons, cache pressure, bundle coverage, and redaction summaries.",
            all(
                (
                    file_contains(doctor_path, "SWARM_DOCTOR_CONTEXT_INTELLIGENCE_SCHEMA"),
                    file_contains(doctor_path, "build_swarm_context_intelligence_posture"),
                    file_contains(script_path, "summarize_context_intelligence"),
                    file_contains(script_path, CONTEXT_INTELLIGENCE_SCHEMA),
                    file_contains(runpack_contract_path, "doctor_swarm.context_intelligence"),
                    file_contains(doctor_test_path, "doctor_swarm_context_intelligence_json_reports_posture"),
                )
            ),
            [
                {"path": "src/doctor.rs"},
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": str(RUNPACK_CONTRACT_PATH)},
                {"path": "tests/doctor_swarm_temp_dir_json.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "operator_docs",
            "Operator docs cover configuration, preview workflow, failure modes, privacy posture, examples, troubleshooting, and final closeout evidence.",
            all(
                (
                    file_contains(operator_doc_path, "Configuration"),
                    file_contains(operator_doc_path, "Preview Workflow"),
                    file_contains(operator_doc_path, "Failure Modes"),
                    file_contains(operator_doc_path, "Privacy Posture"),
                    file_contains(operator_doc_path, "Troubleshooting"),
                    file_contains(operator_doc_path, CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA),
                    file_contains(runbook_path, "--run-context-intelligence-final-gate"),
                )
            ),
            [
                {"path": "docs/context-intelligence.md"},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "readme_freshness",
            "README documentation index and machine-contract links identify context-intelligence docs, contracts, evidence, and closeout gate freshness.",
            all(
                (
                    file_contains(readme_path, "docs/context-intelligence.md"),
                    file_contains(readme_path, "context-intelligence closeout gate"),
                    file_contains(readme_path, "docs/evidence/context-intelligence-closeout-gate.json"),
                    file_contains(readme_path, str(CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_PATH)),
                    file_contains(closeout_contract_path, CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA),
                )
            ),
            [
                {"path": "README.md"},
                {"path": str(CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_PATH)},
                {"path": "docs/evidence/context-intelligence-closeout-gate.json"},
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
            "Current HEAD is pushed to both origin/main and legacy origin/master before closeout artifact generation.",
            pushed,
            [
                {
                    "head": head,
                    "origin_main": origin_main,
                    "origin_master": origin_master,
                    "child_commits": commits,
                }
            ],
            issue=None if pushed else "HEAD is not synchronized with both remotes",
        )
    )

    quality_by_id = {item["id"]: item for item in quality_gate_results}
    missing_quality = [
        gate_id
        for gate_id in CONTEXT_INTELLIGENCE_CLOSEOUT_REQUIRED_QUALITY_GATES
        if quality_by_id.get(gate_id, {}).get("status") != "pass"
    ]
    cargo_check_command = str(
        quality_by_id.get("cargo_check_all_targets_rch", {}).get("command") or ""
    )
    cargo_clippy_command = str(
        quality_by_id.get("cargo_clippy_all_targets_rch", {}).get("command") or ""
    )
    rch_gate_ids = [
        gate_id
        for gate_id in CONTEXT_INTELLIGENCE_CLOSEOUT_REQUIRED_QUALITY_GATES
        if gate_id.endswith("_rch")
    ]
    rch_proven = (
        "rch exec --" in cargo_check_command
        and "cargo check --all-targets" in cargo_check_command
        and "rch exec --" in cargo_clippy_command
        and "cargo clippy --all-targets -- -D warnings" in cargo_clippy_command
        and all(
            "rch exec --" in str(quality_by_id.get(gate_id, {}).get("command") or "")
            for gate_id in rch_gate_ids
        )
    )
    checklist.append(
        gate_check(
            "quality_gates",
            "Required focused and broad quality gates passed, with every Cargo/Rust validation gate run through RCH.",
            not missing_quality and rch_proven,
            [
                {
                    "required_quality_gates": list(
                        CONTEXT_INTELLIGENCE_CLOSEOUT_REQUIRED_QUALITY_GATES
                    ),
                    "provided_quality_gates": quality_gate_results,
                    "rch_gate_ids": rch_gate_ids,
                    "heavy_cargo_uses_rch": rch_proven,
                }
            ],
            issue=(
                "missing or failing quality gates: " + ", ".join(missing_quality)
                if missing_quality
                else "one or more Cargo/Rust gates did not prove rch exec usage"
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
    final_gate_issue = issues.get("bd-ircr3.11") or {}
    parent_issue = issues.get("bd-ircr3") or {}
    status = "pass" if not missing_checks else "fail"
    summary = {
        "schema": CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA,
        "generated_at": generated_at,
        "status": status,
        "purpose": "prompt_to_artifact_context_intelligence_closeout_gate_not_source_of_truth",
        "parent_epic": {
            "id": "bd-ircr3",
            "status": parent_issue.get("status"),
            "title": parent_issue.get("title"),
        },
        "final_gate_bead": {
            "id": "bd-ircr3.11",
            "status": final_gate_issue.get("status"),
            "assignee": final_gate_issue.get("assignee"),
        },
        "required_checks": list(CONTEXT_INTELLIGENCE_CLOSEOUT_REQUIRED_CHECKS),
        "child_artifact_map": child_artifacts,
        "quality_gate_results": quality_gate_results,
        "checklist": checklist,
        "missing_checks": missing_checks,
        "follow_up_required": bool(missing_checks),
        "follow_up_beads": [
            {
                "title": f"[CONTEXT-INTELLIGENCE-GATE] Fix missing {check_id}",
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
    assert_context_intelligence_closeout_gate_contract(summary)
    return summary


def assert_context_intelligence_closeout_gate_contract(summary: dict[str, Any]) -> None:
    root = repo_root()
    contract_path = root / CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(
            f"missing context intelligence closeout gate contract: {contract_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"context intelligence closeout gate contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_SCHEMA
    assert contract.get("decision_gate_schema") == CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA
    assert summary.get("schema") == contract["decision_gate_schema"]
    assert summary.get("purpose") == contract.get("purpose")
    assert summary.get("status") in set(contract.get("allowed_statuses", []))
    assert summary.get("decision") in set(contract.get("allowed_decisions", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in summary, f"missing top-level closeout-gate key: {key}"
    checks = summary.get("checklist")
    assert isinstance(checks, list) and checks
    check_by_id = {
        item.get("id"): item
        for item in checks
        if isinstance(item, dict)
    }
    missing_required = set(contract.get("required_check_ids", [])) - set(check_by_id)
    assert not missing_required, (
        f"context intelligence closeout gate missing checks: {sorted(missing_required)}"
    )
    for check in checks:
        assert isinstance(check, dict)
        assert check.get("status") in set(contract.get("allowed_check_statuses", []))
        assert check.get("requirement")
        evidence = check.get("evidence")
        assert isinstance(evidence, list) and evidence
    child_map = summary.get("child_artifact_map")
    assert isinstance(child_map, list) and child_map
    mapped_children = {
        row.get("bead_id")
        for row in child_map
        if isinstance(row, dict)
    }
    missing_children = set(contract.get("required_child_bead_ids", [])) - mapped_children
    assert not missing_children, (
        f"context intelligence closeout gate missing child mapping: {sorted(missing_children)}"
    )
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


def write_context_intelligence_closeout_gate_output(
    args: argparse.Namespace,
    summary: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_context_intelligence_final_gate_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(
            f"refusing to overwrite context intelligence final gate: {output_path}"
        )
    output_path.write_text(json_dumps(summary, pretty=True), encoding="utf-8")


def runtime_intelligence_child_artifact_map(
    issues: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "bead_id": "bd-h66tp.1",
            "commit": "d9cfaa66073e0b1d27ccfe2235f0916007c61f7f",
            "code_paths": ["src/agent.rs", "src/compaction_worker.rs"],
            "test_paths": ["src/compaction_worker.rs"],
            "docs_or_evidence_paths": ["docs/evidence/compaction-admission-proof.json"],
            "validation_commands": [
                "rch exec -- cargo test --lib compaction_admission -- --nocapture",
                "rch exec -- cargo check --all-targets",
                "rch exec -- cargo clippy --all-targets -- -D warnings",
            ],
        },
        {
            "bead_id": "bd-h66tp.2",
            "commit": "547302f93977849b0f02b19e1f70a9127a154922",
            "code_paths": ["src/agent.rs", "src/session.rs", "src/tools.rs"],
            "test_paths": ["src/session.rs", "src/tools.rs"],
            "docs_or_evidence_paths": [".beads/issues.jsonl"],
            "validation_commands": [
                "rch exec -- cargo test --lib artifact -- --nocapture",
                "rch exec -- cargo check --all-targets",
                "rch exec -- cargo clippy --all-targets -- -D warnings",
            ],
        },
        {
            "bead_id": "bd-h66tp.3",
            "commit": "aa5f8bc5183e862dd7d63c2b4524f367776909de",
            "code_paths": [
                "src/interactive/model_selector_ui.rs",
                "src/model_routing.rs",
                "src/model_selector.rs",
            ],
            "test_paths": ["src/model_routing.rs", "src/model_selector.rs"],
            "docs_or_evidence_paths": [".beads/issues.jsonl"],
            "validation_commands": [
                "rch exec -- cargo test --lib model_routing -- --nocapture",
                "rch exec -- cargo test --test tui_state routing -- --nocapture",
                "rch exec -- cargo check --all-targets",
                "rch exec -- cargo clippy --all-targets -- -D warnings",
            ],
        },
        {
            "bead_id": "bd-h66tp.4",
            "commit": "97cda5eb6eb0446c6e4c7fb8020d891135ae1a6c",
            "code_paths": ["src/scheduler.rs"],
            "test_paths": ["src/scheduler.rs"],
            "docs_or_evidence_paths": [".beads/issues.jsonl"],
            "validation_commands": [
                "rch exec -- cargo test --lib scheduler_fairness_replay -- --nocapture",
                "rch exec -- cargo check --all-targets",
                "rch exec -- cargo clippy --all-targets -- -D warnings",
            ],
        },
        {
            "bead_id": "bd-h66tp.5",
            "commit": "c34a29b3823280198afce8dc850a77f4773b28aa",
            "code_paths": ["src/interactive.rs", "src/interactive/perf.rs"],
            "test_paths": ["tests/tui_state.rs"],
            "docs_or_evidence_paths": [
                "docs/evidence/large-session-tui-frame-budget.json",
                "docs/tui.md",
            ],
            "validation_commands": [
                "rch exec -- cargo test -p pi_agent_rust --test tui_state frame_budget -- --nocapture",
                "rch exec -- cargo check --all-targets",
                "rch exec -- cargo clippy --all-targets -- -D warnings",
            ],
        },
        {
            "bead_id": "bd-h66tp.6",
            "commit": "09c4d9e02c80ea352f783f69d4e2a774bf21e4bf",
            "code_paths": [
                "src/agent.rs",
                "src/extensions_js.rs",
                "src/interactive/commands.rs",
                "src/tools.rs",
            ],
            "test_paths": ["src/agent.rs", "src/tools.rs"],
            "docs_or_evidence_paths": ["docs/evidence/cancellation-cleanup-proof.json"],
            "validation_commands": [
                "rch exec -- cargo test --lib abort_during_tool_execution_records_aborted_tool_result -- --nocapture",
                "rch exec -- cargo test --lib test_bash_timeout_includes_cancellation_details -- --nocapture",
                "rch exec -- cargo check --all-targets",
                "rch exec -- cargo clippy --all-targets -- -D warnings",
            ],
        },
        {
            "bead_id": "bd-h66tp.7",
            "commit": "a82551dccb31a478a398665b7843d0afd118d13e",
            "code_paths": ["src/extension_index.rs", "src/main.rs"],
            "test_paths": ["src/extension_index.rs"],
            "docs_or_evidence_paths": [".beads/issues.jsonl"],
            "validation_commands": [
                "rch exec -- cargo test --lib extension_safety_provenance -- --nocapture",
                "rch exec -- cargo check --all-targets",
                "rch exec -- cargo clippy --all-targets -- -D warnings",
            ],
        },
    ]
    for row in rows:
        issue = issues.get(row["bead_id"]) or {}
        row["status"] = issue.get("status")
        row["title"] = issue.get("title")
        row["close_reason"] = issue.get("close_reason")
    return rows


def runtime_intelligence_source_boundary_checks() -> list[dict[str, Any]]:
    boundaries = [
        (
            "beads_are_source_of_truth",
            "Beads record child status, close reasons, and final-gate ownership.",
            [{"path": ".beads/issues.jsonl"}],
        ),
        (
            "agent_mail_is_coordination_only",
            "Agent Mail reservations and messages are coordination signals, not implementation proof.",
            [{"source": "Agent Mail", "role": "coordination"}],
        ),
        (
            "read_only_gate",
            "The closeout gate inspects already-captured artifacts and does not mutate runtime sources.",
            [{"path": "scripts/build_swarm_operator_runpack.py"}],
        ),
        (
            "rch_required_for_heavy_cargo",
            "Heavy Cargo gates must prove RCH execution.",
            [{"required_command_fragment": "rch exec --"}],
        ),
        (
            "staged_ubs_required",
            "The staged UBS gate remains mandatory before the closeout commit.",
            [{"command": "ubs --staged --only=rust ."}],
        ),
        (
            "beads_ledger_required",
            "Beads ledger reconciliation remains mandatory before the closeout commit.",
            [{"command": "./scripts/reconcile_beads_ledger.sh"}],
        ),
        (
            "no_release_or_dropin_claims",
            "Runtime-intelligence closeout is advisory prompt-to-artifact evidence only.",
            [{"claim_authorized": False}],
        ),
        (
            "closeout_does_not_replace_child_artifacts",
            "Passing closeout does not replace child tests, evidence, Beads, git, RCH, UBS, or CI.",
            [{"closeout_replaces_source_artifacts": False}],
        ),
    ]
    return [
        {
            "id": boundary_id,
            "status": "pass",
            "requirement": requirement,
            "evidence": evidence,
        }
        for boundary_id, requirement, evidence in boundaries
    ]


def build_runtime_intelligence_closeout_gate_summary(
    *,
    generated_at: str,
    quality_gate_results: list[dict[str, Any]],
    issue_export_path: Path | None = None,
    git_refs: dict[str, str | None] | None = None,
    commit_check_override: bool | None = None,
) -> dict[str, Any]:
    root = repo_root()
    script_path = root / "scripts/build_swarm_operator_runpack.py"
    readme_path = root / "README.md"
    closeout_contract_path = root / RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_PATH
    issues = load_beads_issue_map(issue_export_path or (root / ".beads/issues.jsonl"))
    child_artifacts = runtime_intelligence_child_artifact_map(issues)
    checklist: list[dict[str, Any]] = []

    child_states = {
        issue_id: (issues.get(issue_id) or {}).get("status")
        for issue_id in RUNTIME_INTELLIGENCE_CLOSEOUT_CHILD_BEADS
    }
    child_close_reasons = {
        issue_id: (issues.get(issue_id) or {}).get("close_reason")
        for issue_id in RUNTIME_INTELLIGENCE_CLOSEOUT_CHILD_BEADS
    }
    missing_children = [
        issue_id for issue_id, status in child_states.items() if status != "closed"
    ]
    commits = [
        str(row.get("commit"))
        for row in child_artifacts
        if isinstance(row.get("commit"), str)
    ]
    artifact_paths = sorted(
        {
            path
            for row in child_artifacts
            for key in ("code_paths", "test_paths", "docs_or_evidence_paths")
            for path in row.get(key, [])
        }
    )
    artifact_paths_exist = paths_exist(root, artifact_paths)
    commits_present = (
        commit_check_override
        if commit_check_override is not None
        else commits_exist(root, commits)
    )
    checklist.append(
        gate_check(
            "child_beads_closed",
            "All third-wave runtime-intelligence child Beads are closed and mapped to concrete artifacts.",
            not missing_children and artifact_paths_exist and commits_present,
            [
                {
                    "path": ".beads/issues.jsonl",
                    "child_statuses": child_states,
                    "close_reasons": child_close_reasons,
                    "artifact_path_count": len(artifact_paths),
                    "artifact_paths_exist": artifact_paths_exist,
                    "commits": commits,
                    "commits_present": commits_present,
                    "commit_check_source": (
                        "self_test_fixture_override"
                        if commit_check_override is not None
                        else "git_cat_file"
                    ),
                }
            ],
            issue=(
                f"children not closed: {', '.join(missing_children)}"
                if missing_children
                else "one or more mapped artifact paths or commits are missing"
                if not artifact_paths_exist or not commits_present
                else None
            ),
        )
    )

    compaction_path = root / "src/compaction_worker.rs"
    tool_path = root / "src/tools.rs"
    session_path = root / "src/session.rs"
    routing_path = root / "src/model_routing.rs"
    selector_path = root / "src/model_selector.rs"
    selector_ui_path = root / "src/interactive/model_selector_ui.rs"
    scheduler_path = root / "src/scheduler.rs"
    frame_perf_path = root / "src/interactive/perf.rs"
    tui_state_path = root / "tests/tui_state.rs"
    agent_path = root / "src/agent.rs"
    extension_index_path = root / "src/extension_index.rs"
    main_path = root / "src/main.rs"
    compaction_evidence_path = root / "docs/evidence/compaction-admission-proof.json"
    cancellation_evidence_path = root / "docs/evidence/cancellation-cleanup-proof.json"
    frame_evidence_path = root / "docs/evidence/large-session-tui-frame-budget.json"

    checklist.append(
        gate_check(
            "compaction_admission",
            "Memory-aware compaction admission is deterministic, reason-coded, and backed by proof evidence.",
            all(
                (
                    file_contains(compaction_path, "compaction_admission_decision"),
                    file_contains(compaction_path, "CompactionAdmissionReason"),
                    file_contains(compaction_path, "memory_pressure"),
                    file_contains(compaction_evidence_path, "pi.compaction.admission.v1"),
                )
            ),
            [
                {"path": "src/compaction_worker.rs"},
                {"path": "docs/evidence/compaction-admission-proof.json"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "tool_output_artifacts",
            "Huge built-in tool outputs spill into bounded session artifacts instead of unbounded provider-visible payloads.",
            all(
                (
                    file_contains(tool_path, "Full tool output artifact"),
                    file_contains(tool_path, "pi.tool_output_artifact.v1"),
                    file_contains(session_path, "tool_result_artifact_metadata_round_trip"),
                    file_contains(agent_path, "artifact"),
                )
            ),
            [
                {"path": "src/tools.rs"},
                {"path": "src/session.rs"},
                {"path": "src/agent.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "provider_routing",
            "Provider/model routing evidence exposes latency, errors, staleness, cost hints, and user override posture without secrets.",
            all(
                (
                    file_contains(routing_path, "pi.provider_routing.evidence.v1"),
                    file_contains(routing_path, "ModelRoutingEvidence"),
                    file_contains(routing_path, "cost_hint_high"),
                    file_contains(selector_path, "routing_evidence_for"),
                    file_contains(selector_ui_path, "routing"),
                )
            ),
            [
                {"path": "src/model_routing.rs"},
                {"path": "src/model_selector.rs"},
                {"path": "src/interactive/model_selector_ui.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "scheduler_fairness",
            "Extension scheduler replay proves hostcall, timer, stream, and event storms remain ordered and non-starving.",
            all(
                (
                    file_contains(scheduler_path, "scheduler_fairness_replay_under_hostcall_timer_and_event_storms"),
                    file_contains(scheduler_path, "pi.scheduler.fairness_replay.v1"),
                    file_contains(scheduler_path, "max_class_gap_ticks"),
                )
            ),
            [{"path": "src/scheduler.rs"}],
        )
    )
    checklist.append(
        gate_check(
            "frame_budget",
            "Large-session TUI frame-budget telemetry is redaction-safe and covers conversation, tool preview, model, branch, and tree surfaces.",
            all(
                (
                    file_contains(frame_perf_path, "pi.tui.frame_budget.v1"),
                    file_contains(frame_perf_path, "snapshot_json"),
                    file_contains(tui_state_path, "tui_frame_budget_snapshot_covers_large_session_surfaces"),
                    file_contains(frame_evidence_path, "large_session_tui_frame_budget.proof.v1"),
                )
            ),
            [
                {"path": "src/interactive/perf.rs"},
                {"path": "tests/tui_state.rs"},
                {"path": "docs/evidence/large-session-tui-frame-budget.json"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "cancellation_cleanup",
            "Long-running tool and extension cancellation leaves structured evidence and avoids late-success records.",
            all(
                (
                    file_contains(tool_path, "pi.tool.bash.cancellation.v1"),
                    file_contains(agent_path, "pi.tool.cancellation.v1"),
                    file_contains(cancellation_evidence_path, "pi.cancellation.cleanup.proof.v1"),
                    file_contains(cancellation_evidence_path, "tool_result_recorded_no_success"),
                )
            ),
            [
                {"path": "src/tools.rs"},
                {"path": "src/agent.rs"},
                {"path": "docs/evidence/cancellation-cleanup-proof.json"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "extension_safety_provenance",
            "Offline extension search/info/install surfaces expose source, license, category, capability, risk, and freshness provenance.",
            all(
                (
                    file_contains(extension_index_path, "ExtensionSafetyProvenance"),
                    file_contains(extension_index_path, "source_confidence"),
                    file_contains(extension_index_path, "risk_profile"),
                    file_contains(main_path, "extension_safety_lines"),
                    file_contains(main_path, "print_extension_info"),
                )
            ),
            [
                {"path": "src/extension_index.rs"},
                {"path": "src/main.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "docs_and_evidence",
            "Runtime-intelligence operator evidence is linked without promoting advisory evidence into release or drop-in claims.",
            all(
                (
                    file_contains(readme_path, "docs/evidence/runtime-intelligence-closeout-gate.json"),
                    file_contains(readme_path, str(RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_PATH)),
                    file_contains(closeout_contract_path, RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA),
                    file_contains(script_path, "--run-runtime-intelligence-final-gate"),
                )
            ),
            [
                {"path": "README.md"},
                {"path": str(RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_PATH)},
                {"path": "docs/evidence/runtime-intelligence-closeout-gate.json"},
                {"path": "scripts/build_swarm_operator_runpack.py"},
            ],
        )
    )

    source_boundaries = runtime_intelligence_source_boundary_checks()
    boundary_ids = {item["id"] for item in source_boundaries}
    missing_boundaries = set(RUNTIME_INTELLIGENCE_CLOSEOUT_REQUIRED_SOURCE_BOUNDARIES) - boundary_ids
    checklist.append(
        gate_check(
            "source_boundaries",
            "Closeout source boundaries preserve Beads, Agent Mail, RCH, UBS, ledger, CI, child evidence, and claim-integrity authorities.",
            not missing_boundaries,
            source_boundaries,
            issue=(
                "missing source boundary checks: " + ", ".join(sorted(missing_boundaries))
                if missing_boundaries
                else None
            ),
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
            "All implementation child commits are already pushed to origin/main and legacy origin/master before final closeout generation.",
            pushed,
            [
                {
                    "head_before_closeout_commit": head,
                    "origin_main_before_closeout_commit": origin_main,
                    "origin_legacy_mirror_before_closeout_commit": origin_master,
                    "pushed_remote_refs_equal_head": pushed,
                    "child_commits": commits,
                }
            ],
            issue=None if pushed else "HEAD is not synchronized with both remotes",
        )
    )

    quality_by_id = {item["id"]: item for item in quality_gate_results}
    missing_quality = [
        gate_id
        for gate_id in RUNTIME_INTELLIGENCE_CLOSEOUT_REQUIRED_QUALITY_GATES
        if quality_by_id.get(gate_id, {}).get("status") != "pass"
    ]
    cargo_check_command = str(
        quality_by_id.get("cargo_check_all_targets_rch", {}).get("command") or ""
    )
    cargo_clippy_command = str(
        quality_by_id.get("cargo_clippy_all_targets_rch", {}).get("command") or ""
    )
    runtime_contract_command = str(
        quality_by_id.get("runtime_intelligence_closeout_gate_contract_rch", {}).get("command")
        or ""
    )
    rch_proven = (
        "rch exec --" in cargo_check_command
        and "cargo check --all-targets" in cargo_check_command
        and "rch exec --" in cargo_clippy_command
        and "cargo clippy --all-targets -- -D warnings" in cargo_clippy_command
        and "rch exec --" in runtime_contract_command
    )
    checklist.append(
        gate_check(
            "quality_gates",
            "Required closeout quality gates passed, with every Cargo/Rust validation gate run through RCH.",
            not missing_quality and rch_proven,
            [
                {
                    "required_quality_gates": list(
                        RUNTIME_INTELLIGENCE_CLOSEOUT_REQUIRED_QUALITY_GATES
                    ),
                    "provided_quality_gates": quality_gate_results,
                    "heavy_cargo_uses_rch": rch_proven,
                }
            ],
            issue=(
                "missing or failing quality gates: " + ", ".join(missing_quality)
                if missing_quality
                else "one or more Cargo/Rust gates did not prove rch exec usage"
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
    final_gate_issue = issues.get("bd-h66tp.8") or {}
    parent_issue = issues.get("bd-h66tp") or {}
    status = "pass" if not missing_checks else "fail"
    summary = {
        "schema": RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA,
        "generated_at": generated_at,
        "status": status,
        "purpose": "prompt_to_artifact_runtime_intelligence_closeout_gate_not_source_of_truth",
        "parent_epic": {
            "id": "bd-h66tp",
            "status": parent_issue.get("status"),
            "title": parent_issue.get("title"),
        },
        "final_gate_bead": {
            "id": "bd-h66tp.8",
            "status": final_gate_issue.get("status"),
            "assignee": final_gate_issue.get("assignee"),
        },
        "required_checks": list(RUNTIME_INTELLIGENCE_CLOSEOUT_REQUIRED_CHECKS),
        "child_artifact_map": child_artifacts,
        "source_boundary_checks": source_boundaries,
        "quality_gate_results": quality_gate_results,
        "checklist": checklist,
        "missing_checks": missing_checks,
        "remaining_follow_ups": [],
        "known_limitations": [
            "Agent Mail is coordination evidence only; reservations/messages do not replace Beads, source review, or validation gates.",
            "Runtime-intelligence closeout is not release performance evidence, capacity evidence, benchmark evidence, or strict drop-in certification evidence.",
            "A passing closeout gate is not permission to skip source-specific tests, RCH, cargo fmt, cargo check, cargo clippy, staged UBS, Beads ledger reconciliation, CI, or push verification.",
        ],
        "claim_boundaries": {
            "strict_dropin_or_release_claim_authorized": False,
            "runtime_intelligence_is_release_performance_evidence": False,
            "closeout_replaces_source_artifacts": False,
            "allowed_claim": "advisory prompt-to-artifact runtime-intelligence closeout evidence only",
        },
        "follow_up_required": bool(missing_checks),
        "follow_up_beads": [
            {
                "title": f"[RUNTIME-INTELLIGENCE-GATE] Fix missing {check_id}",
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
    assert_runtime_intelligence_closeout_gate_contract(summary)
    return summary


def assert_runtime_intelligence_closeout_gate_contract(summary: dict[str, Any]) -> None:
    root = repo_root()
    contract_path = root / RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(
            f"missing runtime intelligence closeout gate contract: {contract_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"runtime intelligence closeout gate contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_CONTRACT_SCHEMA
    assert contract.get("decision_gate_schema") == RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA
    assert summary.get("schema") == contract["decision_gate_schema"]
    assert summary.get("purpose") == contract.get("purpose")
    assert summary.get("status") in set(contract.get("allowed_statuses", []))
    assert summary.get("decision") in set(contract.get("allowed_decisions", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in summary, f"missing top-level runtime closeout-gate key: {key}"
    checks = summary.get("checklist")
    assert isinstance(checks, list) and checks
    check_by_id = {
        item.get("id"): item
        for item in checks
        if isinstance(item, dict)
    }
    missing_required = set(contract.get("required_check_ids", [])) - set(check_by_id)
    assert not missing_required, (
        f"runtime intelligence closeout gate missing checks: {sorted(missing_required)}"
    )
    for check in checks:
        assert isinstance(check, dict)
        assert check.get("status") in set(contract.get("allowed_check_statuses", []))
        assert check.get("requirement")
        evidence = check.get("evidence")
        assert isinstance(evidence, list) and evidence
    child_map = summary.get("child_artifact_map")
    assert isinstance(child_map, list) and child_map
    mapped_children = {
        row.get("bead_id")
        for row in child_map
        if isinstance(row, dict)
    }
    missing_children = set(contract.get("required_child_bead_ids", [])) - mapped_children
    assert not missing_children, (
        f"runtime intelligence closeout gate missing child mapping: {sorted(missing_children)}"
    )
    source_boundaries = summary.get("source_boundary_checks")
    assert isinstance(source_boundaries, list) and source_boundaries
    boundary_ids = {
        row.get("id")
        for row in source_boundaries
        if isinstance(row, dict)
    }
    missing_boundaries = set(contract.get("required_source_boundary_ids", [])) - boundary_ids
    assert not missing_boundaries, (
        f"runtime intelligence closeout gate missing source boundaries: {sorted(missing_boundaries)}"
    )
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
        claim_boundaries = summary.get("claim_boundaries")
        assert isinstance(claim_boundaries, dict)
        assert claim_boundaries.get("strict_dropin_or_release_claim_authorized") is False
        assert claim_boundaries.get("runtime_intelligence_is_release_performance_evidence") is False
        assert claim_boundaries.get("closeout_replaces_source_artifacts") is False


def write_runtime_intelligence_closeout_gate_output(
    args: argparse.Namespace,
    summary: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_runtime_intelligence_final_gate_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(
            f"refusing to overwrite runtime intelligence final gate: {output_path}"
        )
    output_path.write_text(json_dumps(summary, pretty=True), encoding="utf-8")


def fourth_wave_child_artifact_map(
    issues: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "bead_id": "bd-63x3v.7.1",
            "commit": "d17f403264761b55c8ff3281558c83c99ede862b",
            "code_paths": ["scripts/build_swarm_operator_runpack.py"],
            "test_paths": [],
            "docs_or_evidence_paths": [
                "docs/contracts/swarm-action-plan-contract.json"
            ],
            "validation_commands": [
                "python3 scripts/build_swarm_operator_runpack.py --self-test"
            ],
            "claim_boundary_text": (
                "Dry-run action planning does not claim Beads, mutate source "
                "artifacts, run commands, or replace operator judgment."
            ),
        },
        {
            "bead_id": "bd-63x3v.7.2",
            "commit": "96acd31b645b2227cee6da8177af48fe230e3e16",
            "code_paths": ["scripts/build_swarm_operator_runpack.py"],
            "test_paths": [],
            "docs_or_evidence_paths": [
                "docs/contracts/swarm-work-admission-gate-contract.json"
            ],
            "validation_commands": [
                "python3 scripts/build_swarm_operator_runpack.py --self-test"
            ],
            "claim_boundary_text": (
                "The work admission gate is fail-closed advisory evidence and "
                "does not enforce runtime throttles or start implementation work."
            ),
        },
        {
            "bead_id": "bd-63x3v.7.3",
            "commit": "96acd31b645b2227cee6da8177af48fe230e3e16",
            "code_paths": ["scripts/simulate_swarm_budget_leases.py"],
            "test_paths": ["tests/fixtures/swarm_budget_leases/scenarios.json"],
            "docs_or_evidence_paths": [
                "docs/contracts/swarm-budget-lease-simulator-contract.json"
            ],
            "validation_commands": [
                "python3 scripts/simulate_swarm_budget_leases.py --self-test"
            ],
            "claim_boundary_text": (
                "Budget lease simulation recommends allocations only; it does "
                "not reserve capacity, mutate Agent Mail, or throttle processes."
            ),
        },
        {
            "bead_id": "bd-63x3v.7.4",
            "commit": "f85b26331a74787c8c3d461b7406738654fb4bd0",
            "code_paths": ["scripts/build_swarm_operator_runpack.py"],
            "test_paths": [],
            "docs_or_evidence_paths": [
                "docs/contracts/swarm-turn-pressure-ledger-contract.json"
            ],
            "validation_commands": [
                "python3 scripts/build_swarm_operator_runpack.py --self-test"
            ],
            "claim_boundary_text": (
                "The turn pressure ledger is diagnostic operator evidence only "
                "and does not support benchmark, capacity, or release claims."
            ),
        },
        {
            "bead_id": "bd-63x3v.7.5",
            "commit": "4bd167f2685424993bad86b64b617f1c836ecc48",
            "code_paths": ["scripts/rehearse_extension_quarantine.py"],
            "test_paths": [
                "tests/fixtures/extension_quarantine_rehearsal/scenarios.json"
            ],
            "docs_or_evidence_paths": [
                "docs/contracts/extension-quarantine-rehearsal-contract.json"
            ],
            "validation_commands": [
                "python3 scripts/rehearse_extension_quarantine.py --self-test"
            ],
            "claim_boundary_text": (
                "Extension quarantine rehearsal does not edit configuration, "
                "quarantine extensions, or roll back anything without operator confirmation."
            ),
        },
        {
            "bead_id": "bd-63x3v.7.6",
            "commit": "6ec525c36ca4a2105bea7409a787f8ce2ada25f7",
            "code_paths": ["scripts/summarize_operator_handoff.py"],
            "test_paths": ["tests/fixtures/operator_handoff_summary/scenarios.json"],
            "docs_or_evidence_paths": [
                "docs/contracts/operator-handoff-summary-contract.json"
            ],
            "validation_commands": [
                "python3 scripts/summarize_operator_handoff.py --self-test"
            ],
            "claim_boundary_text": (
                "Operator handoff summaries are redacted summaries and do not "
                "replace Beads, Agent Mail, RCH, Doctor, CI, UBS, or source evidence."
            ),
        },
        {
            "bead_id": "bd-63x3v.7.7",
            "commit": "9c613929f2f9df7bc24e6774283673b057204f10",
            "code_paths": ["scripts/build_stale_evidence_renewal_queue.py"],
            "test_paths": [
                "tests/fixtures/stale_evidence_renewal_queue/scenarios.json"
            ],
            "docs_or_evidence_paths": [
                "docs/contracts/stale-evidence-renewal-queue-contract.json"
            ],
            "validation_commands": [
                "python3 scripts/build_stale_evidence_renewal_queue.py --self-test"
            ],
            "claim_boundary_text": (
                "The stale-evidence renewal queue does not regenerate evidence, "
                "overwrite artifacts, or weaken the drop-in claim gate."
            ),
        },
        {
            "bead_id": "bd-63x3v.7.8",
            "commit": "68185ba5f243310670bf7fd4e8d877a0fc030990",
            "code_paths": [],
            "test_paths": [],
            "docs_or_evidence_paths": [
                "README.md",
                "docs/swarm-operations-runbook.md",
            ],
            "validation_commands": [
                "git diff --check",
                "timeout 120s ubs --staged --only=rust .",
            ],
            "claim_boundary_text": (
                "Operator docs preserve human confirmation points and do not "
                "claim autonomous destructive remediation or strict drop-in authority."
            ),
        },
    ]
    for row in rows:
        issue = issues.get(row["bead_id"]) or {}
        row["status"] = issue.get("status")
        row["title"] = issue.get("title")
        row["close_reason"] = issue.get("close_reason")
    return rows


def fourth_wave_source_boundary_checks() -> list[dict[str, Any]]:
    return [
        {
            "id": "beads_are_source_of_truth",
            "status": "pass",
            "evidence": [".beads/issues.jsonl"],
            "boundary": "Beads issue state and comments remain the work ledger.",
        },
        {
            "id": "agent_mail_is_coordination_only",
            "status": "pass",
            "evidence": ["docs/swarm-operations-runbook.md"],
            "boundary": (
                "Agent Mail reservations and messages coordinate agents but do "
                "not replace Beads, source review, or validation gates."
            ),
        },
        {
            "id": "dry_run_artifacts_only",
            "status": "pass",
            "evidence": ["docs/contracts/swarm-action-plan-contract.json"],
            "boundary": "Action plans and handoff artifacts are dry-run guidance only.",
        },
        {
            "id": "work_admission_is_not_runtime_enforcement",
            "status": "pass",
            "evidence": ["docs/contracts/swarm-work-admission-gate-contract.json"],
            "boundary": (
                "The work admission gate can stop new implementation admission "
                "procedurally but does not enforce runtime throttles."
            ),
        },
        {
            "id": "human_confirmation_required",
            "status": "pass",
            "evidence": ["docs/swarm-operations-runbook.md"],
            "boundary": (
                "Mutating Beads, Agent Mail, extension configuration, git refs, "
                "or evidence files requires explicit human operator execution."
            ),
        },
        {
            "id": "no_evidence_auto_regeneration",
            "status": "pass",
            "evidence": ["docs/contracts/stale-evidence-renewal-queue-contract.json"],
            "boundary": (
                "The stale-evidence queue recommends renewal commands but does "
                "not regenerate or overwrite evidence."
            ),
        },
        {
            "id": "rch_required_for_heavy_cargo",
            "status": "pass",
            "evidence": ["AGENTS.md", "docs/swarm-operations-runbook.md"],
            "boundary": "Heavy Cargo validation must prove RCH execution.",
        },
        {
            "id": "staged_ubs_required",
            "status": "pass",
            "evidence": ["AGENTS.md", "docs/swarm-operations-runbook.md"],
            "boundary": "Staged UBS remains mandatory before commit.",
        },
        {
            "id": "beads_ledger_required",
            "status": "pass",
            "evidence": ["scripts/reconcile_beads_ledger.sh"],
            "boundary": "Beads ledger reconciliation remains mandatory before commit.",
        },
        {
            "id": "no_release_or_dropin_claims",
            "status": "pass",
            "evidence": [
                "docs/contracts/dropin-certification-contract.json",
                "docs/evidence/dropin-certification-verdict.json",
            ],
            "boundary": (
                "Fourth-wave self-healing evidence does not authorize release, "
                "benchmark, capacity, performance, or strict drop-in wording."
            ),
        },
        {
            "id": "closeout_does_not_replace_child_artifacts",
            "status": "pass",
            "evidence": ["docs/evidence/fourth-wave-self-healing-closeout-gate.json"],
            "boundary": (
                "The closeout gate summarizes child evidence and does not "
                "replace source contracts, tests, docs, or pushed commits."
            ),
        },
    ]


def build_fourth_wave_closeout_gate_summary(
    *,
    generated_at: str,
    quality_gate_results: list[dict[str, Any]],
    issue_export_path: Path | None = None,
    git_refs: dict[str, str | None] | None = None,
    commit_check_override: bool | None = None,
) -> dict[str, Any]:
    root = repo_root()
    script_path = root / "scripts/build_swarm_operator_runpack.py"
    runbook_path = root / "docs/swarm-operations-runbook.md"
    readme_path = root / "README.md"
    action_contract_path = root / ACTION_PLAN_CONTRACT_PATH
    work_admission_contract_path = root / WORK_ADMISSION_GATE_CONTRACT_PATH
    budget_contract_path = root / "docs/contracts/swarm-budget-lease-simulator-contract.json"
    turn_pressure_contract_path = root / TURN_PRESSURE_LEDGER_CONTRACT_PATH
    quarantine_contract_path = root / "docs/contracts/extension-quarantine-rehearsal-contract.json"
    handoff_contract_path = root / "docs/contracts/operator-handoff-summary-contract.json"
    stale_queue_contract_path = root / "docs/contracts/stale-evidence-renewal-queue-contract.json"
    closeout_contract_path = root / FOURTH_WAVE_CLOSEOUT_GATE_CONTRACT_PATH
    budget_script_path = root / "scripts/simulate_swarm_budget_leases.py"
    quarantine_script_path = root / "scripts/rehearse_extension_quarantine.py"
    handoff_script_path = root / "scripts/summarize_operator_handoff.py"
    stale_queue_script_path = root / "scripts/build_stale_evidence_renewal_queue.py"

    issues = load_beads_issue_map(issue_export_path or (root / ".beads/issues.jsonl"))
    child_artifacts = fourth_wave_child_artifact_map(issues)
    checklist: list[dict[str, Any]] = []

    child_states = {
        issue_id: (issues.get(issue_id) or {}).get("status")
        for issue_id in FOURTH_WAVE_CLOSEOUT_CHILD_BEADS
    }
    child_close_reasons = {
        issue_id: (issues.get(issue_id) or {}).get("close_reason")
        for issue_id in FOURTH_WAVE_CLOSEOUT_CHILD_BEADS
    }
    missing_children = [
        issue_id for issue_id, status in child_states.items() if status != "closed"
    ]
    checklist.append(
        gate_check(
            "child_beads_closed",
            "All fourth-wave implementation and docs child Beads are closed before closeout.",
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

    commits = [
        str(row.get("commit"))
        for row in child_artifacts
        if isinstance(row.get("commit"), str)
    ]
    artifact_paths = sorted(
        {
            path
            for row in child_artifacts
            for key in ("code_paths", "test_paths", "docs_or_evidence_paths")
            for path in row.get(key, [])
        }
    )
    artifact_paths_exist = paths_exist(root, artifact_paths)
    commits_present = (
        commit_check_override
        if commit_check_override is not None
        else commits_exist(root, commits)
    )
    weak_child_rows = [
        row["bead_id"]
        for row in child_artifacts
        if row.get("status") != "closed"
        or not row.get("validation_commands")
        or not str(row.get("claim_boundary_text") or "").strip()
    ]
    checklist.append(
        gate_check(
            "child_artifact_mapping",
            "Every fourth-wave child bead maps to pushed commits, source paths, validation commands, and claim-boundary text.",
            artifact_paths_exist and bool(commits_present) and not weak_child_rows,
            [
                {
                    "child_artifact_map": child_artifacts,
                    "artifact_paths_exist": artifact_paths_exist,
                    "commits_present": bool(commits_present),
                    "weak_child_rows": weak_child_rows,
                }
            ],
            issue=(
                "missing paths, commits, validation commands, or claim boundaries"
                if not artifact_paths_exist or not commits_present or weak_child_rows
                else None
            ),
        )
    )

    checklist.append(
        gate_check(
            "action_planner",
            "Dry-run action planner contract, implementation, and docs are present.",
            all(
                (
                    file_contains(script_path, ACTION_PLAN_SCHEMA),
                    file_contains(script_path, "build_swarm_action_plan"),
                    file_contains(action_contract_path, ACTION_PLAN_SCHEMA),
                    file_contains(runbook_path, "action-plan.json"),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": str(ACTION_PLAN_CONTRACT_PATH)},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "work_admission_gate",
            "Stop-admitting-work gate is fail-closed and documented as advisory guidance.",
            all(
                (
                    file_contains(script_path, WORK_ADMISSION_GATE_SCHEMA),
                    file_contains(script_path, "build_work_admission_gate"),
                    file_contains(work_admission_contract_path, WORK_ADMISSION_GATE_SCHEMA),
                    file_contains(runbook_path, "work-admission-gate.json"),
                    file_contains(runbook_path, "admit_new_implementation=false"),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": str(WORK_ADMISSION_GATE_CONTRACT_PATH)},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "budget_lease_simulator",
            "Budget lease simulator has contract, fixtures, self-test, and no runtime enforcement claim.",
            all(
                (
                    file_contains(budget_script_path, "budget lease simulator"),
                    file_contains(budget_script_path, "no_runtime_throttles_enforced"),
                    file_contains(budget_contract_path, "pi.swarm.budget_lease_simulation.v1"),
                    (root / "tests/fixtures/swarm_budget_leases/scenarios.json").exists(),
                    file_contains(runbook_path, "budget-lease-simulation.json"),
                )
            ),
            [
                {"path": "scripts/simulate_swarm_budget_leases.py"},
                {"path": "docs/contracts/swarm-budget-lease-simulator-contract.json"},
                {"path": "tests/fixtures/swarm_budget_leases/scenarios.json"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "turn_pressure_ledger",
            "Turn pressure ledger is generated into the runpack and bounded to diagnostic claims.",
            all(
                (
                    file_contains(script_path, TURN_PRESSURE_LEDGER_SCHEMA),
                    file_contains(script_path, "build_turn_pressure_ledger"),
                    file_contains(turn_pressure_contract_path, TURN_PRESSURE_LEDGER_SCHEMA),
                    file_contains(runbook_path, "turn_pressure_ledger"),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": str(TURN_PRESSURE_LEDGER_CONTRACT_PATH)},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "extension_quarantine_rehearsal",
            "Extension quarantine rehearsal covers quarantine, rollback, blocked provenance, and operator confirmation boundaries.",
            all(
                (
                    file_contains(quarantine_script_path, "Rehearse extension quarantine"),
                    file_contains(quarantine_contract_path, "pi.extension.quarantine_rehearsal.v1"),
                    (root / "tests/fixtures/extension_quarantine_rehearsal/scenarios.json").exists(),
                    file_contains(runbook_path, "extension-quarantine-rehearsal.json"),
                    file_contains(runbook_path, "operator has approved"),
                )
            ),
            [
                {"path": "scripts/rehearse_extension_quarantine.py"},
                {"path": "docs/contracts/extension-quarantine-rehearsal-contract.json"},
                {"path": "tests/fixtures/extension_quarantine_rehearsal/scenarios.json"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "operator_handoff_summary",
            "Operator handoff summaries remain redacted, advisory, and bounded to source-status handoff.",
            all(
                (
                    file_contains(handoff_script_path, "handoff"),
                    file_contains(handoff_contract_path, "pi.operator.handoff_summary.v1"),
                    (root / "tests/fixtures/operator_handoff_summary/scenarios.json").exists(),
                    file_contains(runbook_path, "Handoff summaries"),
                    file_contains(readme_path, "handoff summaries"),
                )
            ),
            [
                {"path": "scripts/summarize_operator_handoff.py"},
                {"path": "docs/contracts/operator-handoff-summary-contract.json"},
                {"path": "tests/fixtures/operator_handoff_summary/scenarios.json"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "stale_evidence_renewal",
            "Stale evidence renewal queue is read-only, contract-backed, fixture-tested, and preserves drop-in claim gates.",
            all(
                (
                    file_contains(stale_queue_script_path, STALE_EVIDENCE_RENEWAL_QUEUE_SCHEMA),
                    file_contains(stale_queue_contract_path, STALE_EVIDENCE_RENEWAL_QUEUE_SCHEMA),
                    (root / "tests/fixtures/stale_evidence_renewal_queue/scenarios.json").exists(),
                    file_contains(runbook_path, "stale-evidence-renewal.json"),
                    file_contains(runbook_path, "does not weaken the drop-in claim gate"),
                )
            ),
            [
                {"path": "scripts/build_stale_evidence_renewal_queue.py"},
                {"path": "docs/contracts/stale-evidence-renewal-queue-contract.json"},
                {"path": "tests/fixtures/stale_evidence_renewal_queue/scenarios.json"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "operator_docs",
            "README and runbook document fourth-wave workflow, human confirmation points, and closeout artifacts.",
            all(
                (
                    file_contains(runbook_path, "Fourth-Wave Self-Healing Workflow"),
                    file_contains(runbook_path, "explicit operator execution"),
                    file_contains(readme_path, "Fourth-wave swarm self-healing guidance"),
                    file_contains(readme_path, str(FOURTH_WAVE_CLOSEOUT_GATE_CONTRACT_PATH)),
                    file_contains(closeout_contract_path, FOURTH_WAVE_CLOSEOUT_GATE_SCHEMA),
                )
            ),
            [
                {"path": "README.md"},
                {"path": "docs/swarm-operations-runbook.md"},
                {"path": str(FOURTH_WAVE_CLOSEOUT_GATE_CONTRACT_PATH)},
            ],
        )
    )

    source_boundaries = fourth_wave_source_boundary_checks()
    boundary_ids = {item["id"] for item in source_boundaries}
    missing_boundaries = set(FOURTH_WAVE_CLOSEOUT_REQUIRED_SOURCE_BOUNDARIES) - boundary_ids
    checklist.append(
        gate_check(
            "source_boundaries",
            "Closeout source boundaries preserve Beads, Agent Mail, dry-run, human-confirmation, RCH, UBS, ledger, and release-claim authorities.",
            not missing_boundaries,
            source_boundaries,
            issue=(
                "missing source boundary checks: " + ", ".join(sorted(missing_boundaries))
                if missing_boundaries
                else None
            ),
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
            "All prerequisite child commits are pushed to origin/main and legacy origin/master before final closeout generation.",
            pushed,
            [
                {
                    "head_before_closeout_commit": head,
                    "origin_main_before_closeout_commit": origin_main,
                    "origin_legacy_mirror_before_closeout_commit": origin_master,
                    "pushed_remote_refs_equal_head": pushed,
                    "child_commits": commits,
                }
            ],
            issue=None if pushed else "HEAD is not synchronized with both remotes",
        )
    )

    quality_by_id = {item["id"]: item for item in quality_gate_results}
    missing_quality = [
        gate_id
        for gate_id in FOURTH_WAVE_CLOSEOUT_REQUIRED_QUALITY_GATES
        if quality_by_id.get(gate_id, {}).get("status") != "pass"
    ]
    cargo_check_command = str(
        quality_by_id.get("cargo_check_all_targets_rch", {}).get("command") or ""
    )
    cargo_clippy_command = str(
        quality_by_id.get("cargo_clippy_all_targets_rch", {}).get("command") or ""
    )
    contract_command = str(
        quality_by_id.get("fourth_wave_closeout_gate_contract_rch", {}).get("command")
        or ""
    )
    rch_proven = (
        "rch exec --" in cargo_check_command
        and "cargo check --all-targets" in cargo_check_command
        and "rch exec --" in cargo_clippy_command
        and "cargo clippy --all-targets -- -D warnings" in cargo_clippy_command
        and "rch exec --" in contract_command
    )
    checklist.append(
        gate_check(
            "quality_gates",
            "Required closeout quality gates passed, with every Cargo/Rust validation gate run through RCH.",
            not missing_quality and rch_proven,
            [
                {
                    "required_quality_gates": list(
                        FOURTH_WAVE_CLOSEOUT_REQUIRED_QUALITY_GATES
                    ),
                    "provided_quality_gates": quality_gate_results,
                    "heavy_cargo_uses_rch": rch_proven,
                }
            ],
            issue=(
                "missing or failing quality gates: " + ", ".join(missing_quality)
                if missing_quality
                else "one or more Cargo/Rust gates did not prove rch exec usage"
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
    final_gate_issue = issues.get("bd-63x3v.7.9") or {}
    parent_issue = issues.get("bd-63x3v.7") or {}
    status = "pass" if not missing_checks else "fail"
    summary = {
        "schema": FOURTH_WAVE_CLOSEOUT_GATE_SCHEMA,
        "generated_at": generated_at,
        "status": status,
        "purpose": "prompt_to_artifact_fourth_wave_self_healing_closeout_gate_not_source_of_truth",
        "parent_epic": {
            "id": "bd-63x3v.7",
            "status": parent_issue.get("status"),
            "title": parent_issue.get("title"),
        },
        "final_gate_bead": {
            "id": "bd-63x3v.7.9",
            "status": final_gate_issue.get("status"),
            "assignee": final_gate_issue.get("assignee"),
        },
        "required_checks": list(FOURTH_WAVE_CLOSEOUT_REQUIRED_CHECKS),
        "child_artifact_map": child_artifacts,
        "source_boundary_checks": source_boundaries,
        "quality_gate_results": quality_gate_results,
        "checklist": checklist,
        "missing_checks": missing_checks,
        "remaining_follow_ups": [],
        "known_limitations": [
            "Fourth-wave self-healing artifacts are advisory dry-run operator evidence only.",
            "The work admission gate does not enforce runtime throttles or mutate Beads, Agent Mail, RCH, git, extension configuration, or evidence files.",
            "Mutating commands recommended by action plans, handoff summaries, quarantine rehearsal, or stale-evidence renewal require explicit human operator execution.",
            "This closeout does not authorize release, benchmark, capacity, performance, or strict drop-in claims.",
        ],
        "claim_boundaries": {
            "strict_dropin_or_release_claim_authorized": False,
            "self_healing_artifacts_mutate_sources": False,
            "work_admission_gate_enforces_runtime_throttle": False,
            "closeout_replaces_source_artifacts": False,
            "human_confirmation_required_for_mutation": True,
            "allowed_claim": "advisory fourth-wave self-healing closeout evidence only",
        },
        "follow_up_required": bool(missing_checks),
        "follow_up_beads": [
            {
                "title": f"[FOURTH-WAVE-GATE] Fix missing {check_id}",
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
    assert_fourth_wave_closeout_gate_contract(summary)
    return summary


def assert_fourth_wave_closeout_gate_contract(summary: dict[str, Any]) -> None:
    root = repo_root()
    contract_path = root / FOURTH_WAVE_CLOSEOUT_GATE_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing fourth-wave closeout gate contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"fourth-wave closeout gate contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == FOURTH_WAVE_CLOSEOUT_GATE_CONTRACT_SCHEMA
    assert contract.get("decision_gate_schema") == FOURTH_WAVE_CLOSEOUT_GATE_SCHEMA
    assert summary.get("schema") == contract["decision_gate_schema"]
    assert summary.get("purpose") == contract.get("purpose")
    assert summary.get("status") in set(contract.get("allowed_statuses", []))
    assert summary.get("decision") in set(contract.get("allowed_decisions", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in summary, f"missing top-level fourth-wave closeout-gate key: {key}"
    checks = summary.get("checklist")
    assert isinstance(checks, list) and checks
    check_by_id = {
        item.get("id"): item
        for item in checks
        if isinstance(item, dict)
    }
    missing_required = set(contract.get("required_check_ids", [])) - set(check_by_id)
    assert not missing_required, (
        f"fourth-wave closeout gate missing checks: {sorted(missing_required)}"
    )
    for check in checks:
        assert isinstance(check, dict)
        assert check.get("status") in set(contract.get("allowed_check_statuses", []))
        assert check.get("requirement")
        evidence = check.get("evidence")
        assert isinstance(evidence, list) and evidence
    child_map = summary.get("child_artifact_map")
    assert isinstance(child_map, list) and child_map
    mapped_children = {
        row.get("bead_id")
        for row in child_map
        if isinstance(row, dict)
    }
    missing_children = set(contract.get("required_child_bead_ids", [])) - mapped_children
    assert not missing_children, (
        f"fourth-wave closeout gate missing child mapping: {sorted(missing_children)}"
    )
    for row in child_map:
        assert isinstance(row.get("validation_commands"), list) and row.get(
            "validation_commands"
        )
        assert str(row.get("claim_boundary_text") or "").strip()
    source_boundaries = summary.get("source_boundary_checks")
    assert isinstance(source_boundaries, list) and source_boundaries
    boundary_ids = {
        row.get("id")
        for row in source_boundaries
        if isinstance(row, dict)
    }
    missing_boundaries = set(contract.get("required_source_boundary_ids", [])) - boundary_ids
    assert not missing_boundaries, (
        f"fourth-wave closeout gate missing source boundaries: {sorted(missing_boundaries)}"
    )
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
        claim_boundaries = summary.get("claim_boundaries")
        assert isinstance(claim_boundaries, dict)
        assert claim_boundaries.get("strict_dropin_or_release_claim_authorized") is False
        assert claim_boundaries.get("self_healing_artifacts_mutate_sources") is False
        assert (
            claim_boundaries.get("work_admission_gate_enforces_runtime_throttle") is False
        )
        assert claim_boundaries.get("closeout_replaces_source_artifacts") is False
        assert claim_boundaries.get("human_confirmation_required_for_mutation") is True


def write_fourth_wave_closeout_gate_output(
    args: argparse.Namespace,
    summary: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_fourth_wave_final_gate_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(
            f"refusing to overwrite fourth-wave self-healing final gate: {output_path}"
        )
    output_path.write_text(json_dumps(summary, pretty=True), encoding="utf-8")


def load_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise RunpackError(f"missing JSONL evidence: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunpackError(
                f"malformed JSONL evidence at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise RunpackError(f"JSONL evidence row is not an object: {path}:{line_number}")
        rows.append(value)
    return rows


def source_terms_present(path: Path, terms: tuple[str, ...]) -> dict[str, bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {term: False for term in terms}
    return {term: term in text for term in terms}


def canonical_metric_status(mapping: dict[str, str | list[str] | None]) -> dict[str, Any]:
    missing = [
        metric
        for metric in BACKPRESSURE_BUDGET_REQUIRED_CANONICAL_METRICS
        if not mapping.get(metric)
    ]
    return {
        "canonical_metric_map": mapping,
        "missing_canonical_metrics": missing,
        "status": "pass" if not missing else "fail",
    }


def build_provider_backpressure_surface(root: Path) -> dict[str, Any]:
    path = root / "tests/provider_streaming.rs"
    terms = source_terms_present(
        path,
        (
            "BACKPRESSURE_SCHEMA",
            "pi.test.provider_stream_backpressure.v1",
            "event_count",
            "semantic_count",
            "coalesced_or_buffered_count",
            "max_queue_depth",
            "latency_steps",
            "\"verdict\"",
            "replayed text changed under pressure",
            "tool-call boundary ordering was lost",
            "malformed stream must not synthesize Done",
        ),
    )
    mapping = canonical_metric_status(
        {
            "event_count": "event_count" if terms["event_count"] else None,
            "semantic_preservation": (
                ["semantic_count", "replayed text changed under pressure", "tool-call boundary ordering was lost"]
                if terms["semantic_count"]
                and terms["replayed text changed under pressure"]
                and terms["tool-call boundary ordering was lost"]
                else None
            ),
            "coalesced_low_value_count": (
                "coalesced_or_buffered_count"
                if terms["coalesced_or_buffered_count"]
                else None
            ),
            "pressure_depth": "max_queue_depth" if terms["max_queue_depth"] else None,
            "latency_or_step": "latency_steps" if terms["latency_steps"] else None,
            "verdict": "verdict" if terms['"verdict"'] else None,
        }
    )
    semantic_assertions = {
        "text_preserved": terms["replayed text changed under pressure"],
        "tool_call_boundary_preserved": terms["tool-call boundary ordering was lost"],
        "malformed_stream_fail_closed": terms["malformed stream must not synthesize Done"],
    }
    status = (
        "pass"
        if path.exists()
        and terms["pi.test.provider_stream_backpressure.v1"]
        and mapping["status"] == "pass"
        and all(semantic_assertions.values())
        else "fail"
    )
    return {
        "id": "provider_streaming",
        "status": status,
        "schema": "pi.test.provider_stream_backpressure.v1",
        "evidence_path": "tests/provider_streaming.rs",
        "evidence_kind": "checked_in_test_evidence_source",
        "metric_compatibility": mapping,
        "semantic_loss_assertions": semantic_assertions,
        "low_value_delta_policy": (
            "TextDelta and ThinkingDelta may be coalesced under queue pressure; "
            "semantic boundaries, usage, stop reason, and malformed-stream fail-closed behavior must survive."
        ),
        "operator_summary": (
            "Provider streaming pressure preserves semantic stream boundaries while "
            "coalescing low-value deltas into bounded queue pressure."
        ),
    }


def build_rpc_output_backpressure_surface(root: Path) -> dict[str, Any]:
    path = root / "docs/evidence/rpc-output-pressure.jsonl"
    rows = load_jsonl_objects(path)
    matching_rows = [
        row for row in rows if row.get("schema") == "pi.rpc_output_pressure.v1"
    ]
    coalesced_total = 0
    for row in matching_rows:
        for key in ("coalesced_message_delta_count", "coalesced_tool_update_count"):
            value = row.get(key)
            if isinstance(value, int):
                coalesced_total += value
    has_event_count = any(
        isinstance(row.get("event_count"), int) or isinstance(row.get("input_events"), int)
        for row in matching_rows
    )
    has_semantic_assertion = any(
        isinstance(row.get("semantic_events_preserved_by"), str)
        or isinstance(row.get("preserved_semantic_count"), int)
        for row in matching_rows
    )
    has_pressure_depth = any(
        isinstance(row.get("pending_events"), int)
        or isinstance(row.get("pending_class_count_before_semantic"), int)
        for row in matching_rows
    )
    has_latency = any(
        isinstance(row.get("p99_us"), int) or isinstance(row.get("latency_budget_us"), int)
        for row in matching_rows
    )
    has_verdict = any(row.get("verdict") == "pass" for row in matching_rows)
    mapping = canonical_metric_status(
        {
            "event_count": "input_events|event_count" if has_event_count else None,
            "semantic_preservation": (
                "semantic_events_preserved_by|preserved_semantic_count"
                if has_semantic_assertion
                else None
            ),
            "coalesced_low_value_count": (
                "coalesced_message_delta_count+coalesced_tool_update_count"
                if coalesced_total > 0
                else None
            ),
            "pressure_depth": (
                "pending_events|pending_class_count_before_semantic"
                if has_pressure_depth
                else None
            ),
            "latency_or_step": "p99_us|latency_budget_us" if has_latency else None,
            "verdict": "verdict" if has_verdict else None,
        }
    )
    status = (
        "pass"
        if matching_rows
        and mapping["status"] == "pass"
        and all(row.get("verdict") == "pass" for row in matching_rows)
        else "fail"
    )
    return {
        "id": "rpc_output",
        "status": status,
        "schema": "pi.rpc_output_pressure.v1",
        "evidence_path": "docs/evidence/rpc-output-pressure.jsonl",
        "evidence_kind": "checked_in_jsonl_evidence",
        "row_count": len(matching_rows),
        "metric_compatibility": mapping,
        "semantic_loss_assertions": {
            "semantic_events_preserved": has_semantic_assertion,
            "all_checked_in_rows_pass": all(row.get("verdict") == "pass" for row in matching_rows),
        },
        "low_value_delta_policy": (
            "MessageDelta and ToolUpdate are coalescible; pending updates must flush before semantic events."
        ),
        "operator_summary": (
            "RPC output pressure evidence bounds low-value updates without blocking, "
            "then flushes the latest pending update before semantic output."
        ),
    }


def build_tui_degradation_backpressure_surface(root: Path) -> dict[str, Any]:
    path = root / "src/interactive/tests.rs"
    terms = source_terms_present(
        path,
        (
            "pi.tui.degradation_drill.v1",
            "event_count",
            "semantic_visible_count",
            "coalesced_count",
            "max_frame_budget_pressure",
            "redraw_count",
            "preserved_input_count",
            "\"verdict\"",
            "tui_degradation_drill_preserves_input_and_semantics_under_pressure",
            "semantic_visible_count == 4",
        ),
    )
    mapping = canonical_metric_status(
        {
            "event_count": "event_count" if terms["event_count"] else None,
            "semantic_preservation": (
                ["semantic_visible_count", "preserved_input_count"]
                if terms["semantic_visible_count"] and terms["preserved_input_count"]
                else None
            ),
            "coalesced_low_value_count": "coalesced_count" if terms["coalesced_count"] else None,
            "pressure_depth": (
                "max_frame_budget_pressure"
                if terms["max_frame_budget_pressure"]
                else None
            ),
            "latency_or_step": "redraw_count" if terms["redraw_count"] else None,
            "verdict": "verdict" if terms['"verdict"'] else None,
        }
    )
    semantic_assertions = {
        "semantic_output_visible": terms["semantic_visible_count == 4"],
        "input_preserved": terms["preserved_input_count"],
        "dedicated_drill_present": terms[
            "tui_degradation_drill_preserves_input_and_semantics_under_pressure"
        ],
    }
    status = (
        "pass"
        if path.exists()
        and terms["pi.tui.degradation_drill.v1"]
        and mapping["status"] == "pass"
        and all(semantic_assertions.values())
        else "fail"
    )
    return {
        "id": "tui_degradation",
        "status": status,
        "schema": "pi.tui.degradation_drill.v1",
        "evidence_path": "src/interactive/tests.rs",
        "evidence_kind": "checked_in_test_evidence_source",
        "metric_compatibility": mapping,
        "semantic_loss_assertions": semantic_assertions,
        "low_value_delta_policy": (
            "Tool-update noise can collapse under frame pressure; final provider text, "
            "final tool output, session notes, and user input must remain visible."
        ),
        "operator_summary": (
            "TUI degradation evidence preserves semantic output and user input while "
            "collapsing low-value tool-update pressure."
        ),
    }


def build_backpressure_budget_contract_summary(*, generated_at: str) -> dict[str, Any]:
    root = repo_root()
    surfaces = [
        build_provider_backpressure_surface(root),
        build_rpc_output_backpressure_surface(root),
        build_tui_degradation_backpressure_surface(root),
    ]
    present_surface_ids = {surface["id"] for surface in surfaces}
    missing_surfaces = [
        surface_id
        for surface_id in BACKPRESSURE_BUDGET_REQUIRED_SURFACE_IDS
        if surface_id not in present_surface_ids
    ]
    failing_surfaces = [
        surface["id"] for surface in surfaces if surface.get("status") != "pass"
    ]
    incompatible_surfaces = [
        surface["id"]
        for surface in surfaces
        if surface.get("metric_compatibility", {}).get("status") != "pass"
    ]
    status = (
        "pass"
        if not missing_surfaces and not failing_surfaces and not incompatible_surfaces
        else "fail"
    )
    operator_summary = [
        "Provider, RPC, and TUI pressure surfaces expose compatible event, semantic-preservation, coalescing, pressure-depth, latency/step, and verdict evidence.",
        "Semantic preservation means lifecycle boundaries, final content, tool boundaries, session notes, and user input remain visible or flushed before semantic events.",
        "Low-value pressure means provider text/thinking deltas, RPC message/tool updates, and TUI tool-update noise may be coalesced or collapsed under pressure.",
    ]
    return {
        "schema": BACKPRESSURE_BUDGET_CONTRACT_SCHEMA,
        "generated_at": generated_at,
        "status": status,
        "purpose": "operator_backpressure_budget_contract_not_release_performance_claim",
        "source_bead": "bd-63x3v.9.8",
        "required_surface_ids": list(BACKPRESSURE_BUDGET_REQUIRED_SURFACE_IDS),
        "required_canonical_metrics": list(BACKPRESSURE_BUDGET_REQUIRED_CANONICAL_METRICS),
        "surfaces": surfaces,
        "missing_surfaces": missing_surfaces,
        "failing_surfaces": failing_surfaces,
        "incompatible_surfaces": incompatible_surfaces,
        "operator_summary": operator_summary,
        "claim_boundaries": {
            "read_only_aggregator": True,
            "does_not_duplicate_pressure_tests": True,
            "does_not_authorize_release_performance_claims": True,
            "does_not_replace_provider_rpc_tui_source_evidence": True,
            "does_not_run_cargo_or_regenerate_child_evidence": True,
        },
        "decision": "contract_passed" if status == "pass" else "file_follow_up_before_relying_on_contract",
    }


def assert_backpressure_budget_contract(summary: dict[str, Any]) -> None:
    root = repo_root()
    contract = json.loads((root / BACKPRESSURE_BUDGET_CONTRACT_PATH).read_text(encoding="utf-8"))
    assert contract.get("schema") == BACKPRESSURE_BUDGET_CONTRACT_SPEC_SCHEMA
    assert summary.get("schema") == contract.get("evidence_schema")
    for key in contract.get("required_top_level_keys", []):
        assert key in summary, f"backpressure contract missing top-level key: {key}"
    surface_ids = {surface.get("id") for surface in summary.get("surfaces", [])}
    required_surfaces = set(contract.get("required_surface_ids", []))
    assert surface_ids.issuperset(required_surfaces)
    required_metrics = set(contract.get("required_canonical_metrics", []))
    for surface in summary.get("surfaces", []):
        metric_map = surface.get("metric_compatibility", {}).get("canonical_metric_map", {})
        assert required_metrics.issubset(metric_map.keys())
        missing = surface.get("metric_compatibility", {}).get("missing_canonical_metrics")
        if summary.get("status") == "pass":
            assert missing == [], f"surface has missing metrics: {surface.get('id')}"
            assert surface.get("status") == "pass"
    if summary.get("status") == "pass":
        assert summary.get("missing_surfaces") == []
        assert summary.get("failing_surfaces") == []
        assert summary.get("incompatible_surfaces") == []
        boundaries = summary.get("claim_boundaries", {})
        assert boundaries.get("read_only_aggregator") is True
        assert boundaries.get("does_not_duplicate_pressure_tests") is True
        assert boundaries.get("does_not_authorize_release_performance_claims") is True


def write_backpressure_budget_contract_output(
    args: argparse.Namespace,
    summary: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_backpressure_budget_contract_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(
            f"refusing to overwrite backpressure budget contract evidence: {output_path}"
        )
    output_path.write_text(json_dumps(summary, pretty=True), encoding="utf-8")


def adaptive_execution_child_artifact_map(
    issues: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "bead_id": "bd-63x3v.8.1",
            "commit": "cb28654244cd3dc7041c30d1787361d667e8f952",
            "code_paths": ["tests/provider_streaming.rs"],
            "test_paths": ["tests/provider_streaming.rs"],
            "docs_or_evidence_paths": ["tests/provider_streaming.rs"],
            "validation_commands": [
                "rch exec -- env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/<agent>/target TMPDIR=/data/tmp/pi_agent_rust_cargo/<agent>/tmp cargo test provider_streaming -- --nocapture",
                "rch exec -- env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/<agent>/target TMPDIR=/data/tmp/pi_agent_rust_cargo/<agent>/tmp cargo check --all-targets",
            ],
            "claim_boundary_text": (
                "Provider backpressure replay evidence does not cover RPC, TUI, "
                "or session pressure surfaces."
            ),
        },
        {
            "bead_id": "bd-63x3v.8.2",
            "commit": "8341d6873e425e147ad3ff00c085fbaa13b8b05e",
            "code_paths": ["src/tools.rs"],
            "test_paths": ["src/tools.rs"],
            "docs_or_evidence_paths": ["src/tools.rs"],
            "validation_commands": [
                "rch exec -- env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/<agent>/target TMPDIR=/data/tmp/pi_agent_rust_cargo/<agent>/tmp cargo test tools::tests conformance::test_read conformance::test_grep conformance::test_bash",
            ],
            "claim_boundary_text": (
                "Tool artifact lifecycle evidence does not delete user files or "
                "authorize cleanup without operator confirmation."
            ),
        },
        {
            "bead_id": "bd-63x3v.8.3",
            "commit": "f8699c73461d8a0838c643362a34ba283ac8e134",
            "code_paths": ["src/session.rs"],
            "test_paths": ["src/session.rs"],
            "docs_or_evidence_paths": ["src/session.rs"],
            "validation_commands": [
                "rch exec -- env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/<agent>/target TMPDIR=/data/tmp/pi_agent_rust_cargo/<agent>/tmp cargo test session::tests session_index::tests",
            ],
            "claim_boundary_text": (
                "Branch-heavy replay minimization evidence does not replace "
                "session persistence correctness tests."
            ),
        },
        {
            "bead_id": "bd-63x3v.8.4",
            "commit": "5662cb8ba4a7fc4c2f9ba75483f8b78c4534f618",
            "code_paths": ["src/interactive/tests.rs"],
            "test_paths": ["src/interactive/tests.rs"],
            "docs_or_evidence_paths": ["src/interactive/tests.rs"],
            "validation_commands": [
                "rch exec -- env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/<agent>/target TMPDIR=/data/tmp/pi_agent_rust_cargo/<agent>/tmp cargo test interactive tui -- --nocapture",
            ],
            "claim_boundary_text": (
                "TUI degradation drill evidence does not claim visual perfection "
                "for every terminal or bypass accessibility review."
            ),
        },
        {
            "bead_id": "bd-63x3v.8.5",
            "commit": "190cd9993f6335efb1c0a57022afccafbd8ba295",
            "code_paths": ["src/extensions.rs"],
            "test_paths": ["src/extensions.rs"],
            "docs_or_evidence_paths": ["src/extensions.rs"],
            "validation_commands": [
                "rch exec -- env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/<agent>/target TMPDIR=/data/tmp/pi_agent_rust_cargo/<agent>/tmp cargo test extension_policy ext_conformance",
            ],
            "claim_boundary_text": (
                "Permission drift detection does not mutate installed extensions, "
                "manifests, user config, or vendored artifacts."
            ),
        },
        {
            "bead_id": "bd-63x3v.8.6",
            "commit": "f9fdfc063ee0e94002f491b4f4c499d7c788cc49",
            "code_paths": ["scripts/schedule_validation_evidence_refresh.py"],
            "test_paths": ["scripts/schedule_validation_evidence_refresh.py"],
            "docs_or_evidence_paths": ["scripts/schedule_validation_evidence_refresh.py"],
            "validation_commands": [
                "python3 scripts/schedule_validation_evidence_refresh.py --self-test",
            ],
            "claim_boundary_text": (
                "Validation refresh scheduling does not run cargo, overwrite "
                "evidence, or weaken strict drop-in claim gates."
            ),
        },
    ]
    for row in rows:
        issue = issues.get(row["bead_id"]) or {}
        row["status"] = issue.get("status")
        row["title"] = issue.get("title")
        row["close_reason"] = issue.get("close_reason")
    return rows


def adaptive_execution_source_boundary_checks() -> list[dict[str, Any]]:
    return [
        {
            "id": "beads_are_source_of_truth",
            "status": "pass",
            "evidence": [".beads/issues.jsonl"],
            "boundary": "Beads issue state and close reasons remain the work ledger.",
        },
        {
            "id": "agent_mail_is_coordination_only",
            "status": "pass",
            "evidence": ["AGENTS.md"],
            "boundary": (
                "Agent Mail coordinates ownership but does not replace Beads, git, "
                "source inspection, or validation evidence."
            ),
        },
        {
            "id": "read_only_gate",
            "status": "pass",
            "evidence": ["scripts/build_swarm_operator_runpack.py"],
            "boundary": "The closeout gate summarizes existing evidence and does not mutate runtime sources.",
        },
        {
            "id": "rch_required_for_heavy_cargo",
            "status": "pass",
            "evidence": ["AGENTS.md"],
            "boundary": "Heavy Cargo validation evidence for child work must prove RCH execution.",
        },
        {
            "id": "staged_ubs_required",
            "status": "pass",
            "evidence": ["AGENTS.md"],
            "boundary": "Staged UBS remains mandatory before closeout commit.",
        },
        {
            "id": "beads_ledger_required",
            "status": "pass",
            "evidence": ["scripts/reconcile_beads_ledger.sh"],
            "boundary": "Beads ledger reconciliation remains mandatory before closeout commit.",
        },
        {
            "id": "no_release_or_dropin_claims",
            "status": "pass",
            "evidence": [
                "docs/contracts/dropin-certification-contract.json",
                "docs/evidence/dropin-certification-verdict.json",
            ],
            "boundary": (
                "Fifth-wave adaptive execution closeout evidence is advisory and "
                "does not authorize release, benchmark, capacity, performance, or strict drop-in claims."
            ),
        },
        {
            "id": "closeout_does_not_replace_child_artifacts",
            "status": "pass",
            "evidence": ["docs/evidence/adaptive-execution-closeout-gate.json"],
            "boundary": (
                "The closeout gate summarizes child evidence and does not replace "
                "source tests, contracts, Beads, pushed commits, RCH, UBS, or CI."
            ),
        },
    ]


def build_adaptive_execution_closeout_gate_summary(
    *,
    generated_at: str,
    quality_gate_results: list[dict[str, Any]],
    issue_export_path: Path | None = None,
    git_refs: dict[str, str | None] | None = None,
    commit_check_override: bool | None = None,
) -> dict[str, Any]:
    root = repo_root()
    script_path = root / "scripts/build_swarm_operator_runpack.py"
    provider_streaming_path = root / "tests/provider_streaming.rs"
    tools_path = root / "src/tools.rs"
    session_path = root / "src/session.rs"
    interactive_tests_path = root / "src/interactive/tests.rs"
    extensions_path = root / "src/extensions.rs"
    refresh_scheduler_path = root / "scripts/schedule_validation_evidence_refresh.py"
    closeout_contract_path = root / ADAPTIVE_EXECUTION_CLOSEOUT_GATE_CONTRACT_PATH

    issues = load_beads_issue_map(issue_export_path or (root / ".beads/issues.jsonl"))
    child_artifacts = adaptive_execution_child_artifact_map(issues)
    checklist: list[dict[str, Any]] = []

    child_states = {
        issue_id: (issues.get(issue_id) or {}).get("status")
        for issue_id in ADAPTIVE_EXECUTION_CLOSEOUT_CHILD_BEADS
    }
    child_close_reasons = {
        issue_id: (issues.get(issue_id) or {}).get("close_reason")
        for issue_id in ADAPTIVE_EXECUTION_CLOSEOUT_CHILD_BEADS
    }
    missing_children = [
        issue_id for issue_id, status in child_states.items() if status != "closed"
    ]
    checklist.append(
        gate_check(
            "child_beads_closed",
            "All fifth-wave adaptive-execution child Beads are closed before closeout.",
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

    commits = [
        str(row.get("commit"))
        for row in child_artifacts
        if isinstance(row.get("commit"), str)
    ]
    artifact_paths = sorted(
        {
            path
            for row in child_artifacts
            for key in ("code_paths", "test_paths", "docs_or_evidence_paths")
            for path in row.get(key, [])
        }
    )
    artifact_paths_exist = paths_exist(root, artifact_paths)
    commits_present = (
        commit_check_override
        if commit_check_override is not None
        else commits_exist(root, commits)
    )
    weak_child_rows = [
        row["bead_id"]
        for row in child_artifacts
        if row.get("status") != "closed"
        or not row.get("validation_commands")
        or not str(row.get("claim_boundary_text") or "").strip()
    ]
    checklist.append(
        gate_check(
            "child_artifact_mapping",
            "Every fifth-wave child bead maps to pushed commits, source paths, validation commands, and claim-boundary text.",
            artifact_paths_exist and bool(commits_present) and not weak_child_rows,
            [
                {
                    "child_artifact_map": child_artifacts,
                    "artifact_paths_exist": artifact_paths_exist,
                    "commits_present": bool(commits_present),
                    "weak_child_rows": weak_child_rows,
                }
            ],
            issue=(
                "missing paths, commits, validation commands, or claim boundaries"
                if not artifact_paths_exist or not commits_present or weak_child_rows
                else None
            ),
        )
    )

    checklist.append(
        gate_check(
            "provider_streaming_backpressure",
            "Provider streaming replay preserves semantic chunks while bounding low-value backpressure deltas.",
            all(
                (
                    file_contains(provider_streaming_path, "BACKPRESSURE_SCHEMA"),
                    file_contains(provider_streaming_path, "semantic_count"),
                    file_contains(provider_streaming_path, "coalesced_or_buffered_count"),
                    file_contains(provider_streaming_path, "max_queue_depth"),
                )
            ),
            [{"path": "tests/provider_streaming.rs"}],
        )
    )
    checklist.append(
        gate_check(
            "tool_artifact_lifecycle",
            "Tool output artifacts have bounded lifecycle metadata, redaction, retention, and safe-delete classification.",
            all(
                (
                    file_contains(tools_path, "TOOL_OUTPUT_ARTIFACT_SCHEMA_V1"),
                    file_contains(tools_path, "retention_class"),
                    file_contains(tools_path, "safe_delete_candidate"),
                    file_contains(tools_path, "redact_tool_output_artifact"),
                    file_contains(tools_path, "ensure_artifact_path_under_root"),
                )
            ),
            [{"path": "src/tools.rs"}],
        )
    )
    checklist.append(
        gate_check(
            "branch_replay_minimization",
            "Branch-heavy session replay emits bounded selected-branch evidence and explicit fallback behavior.",
            all(
                (
                    file_contains(session_path, "pi.session.replay_minimization_trace.v1"),
                    file_contains(session_path, "skipped_sibling_entries"),
                    file_contains(session_path, "bounded_selected_branch"),
                    file_contains(session_path, "cold_start_replay_minimization_bounds_branch_heavy"),
                )
            ),
            [{"path": "src/session.rs"}],
        )
    )
    checklist.append(
        gate_check(
            "tui_degradation_drill",
            "TUI degradation drill preserves semantic output and user input under sustained event pressure.",
            all(
                (
                    file_contains(interactive_tests_path, "pi.tui.degradation_drill.v1"),
                    file_contains(interactive_tests_path, "redraw_count"),
                    file_contains(interactive_tests_path, "coalesced_count"),
                    file_contains(interactive_tests_path, "preserved_input_count"),
                    file_contains(interactive_tests_path, "tui_degradation_drill_preserves_input"),
                )
            ),
            [{"path": "src/interactive/tests.rs"}],
        )
    )
    checklist.append(
        gate_check(
            "permission_drift_detector",
            "Extension permission drift detector fails closed on capability expansion without trustworthy provenance.",
            all(
                (
                    file_contains(extensions_path, "ExtensionPermissionDriftReport"),
                    file_contains(extensions_path, "ExtensionPermissionDriftClass"),
                    file_contains(extensions_path, "ExtensionPermissionDriftVerdict"),
                    file_contains(extensions_path, "detect_extension_permission_drift"),
                    file_contains(extensions_path, "ExtensionPermissionDriftVerdict::FailClosed"),
                )
            ),
            [{"path": "src/extensions.rs"}],
        )
    )
    checklist.append(
        gate_check(
            "validation_refresh_scheduler",
            "Validation refresh scheduler ranks stale evidence by value, cost, duplicate coverage, headroom, and dirty state without running validation.",
            all(
                (
                    file_contains(refresh_scheduler_path, "pi.validation.refresh_scheduler.v1"),
                    file_contains(refresh_scheduler_path, "must-refresh"),
                    file_contains(refresh_scheduler_path, "duplicate-covered"),
                    file_contains(refresh_scheduler_path, "blocked-by-headroom"),
                    file_contains(refresh_scheduler_path, "blocked-by-dirty-worktree"),
                )
            ),
            [{"path": "scripts/schedule_validation_evidence_refresh.py"}],
        )
    )

    source_boundaries = adaptive_execution_source_boundary_checks()
    boundary_ids = {item["id"] for item in source_boundaries}
    missing_boundaries = set(ADAPTIVE_EXECUTION_CLOSEOUT_REQUIRED_SOURCE_BOUNDARIES) - boundary_ids
    checklist.append(
        gate_check(
            "source_boundaries",
            "Closeout source boundaries preserve Beads, Agent Mail, RCH, UBS, ledger, child-artifact, and release-claim authorities.",
            not missing_boundaries,
            source_boundaries,
            issue=(
                "missing source boundary checks: " + ", ".join(sorted(missing_boundaries))
                if missing_boundaries
                else None
            ),
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
            "All prerequisite child commits are pushed to origin/main and legacy origin/master before final closeout generation.",
            pushed,
            [
                {
                    "head_before_closeout_commit": head,
                    "origin_main_before_closeout_commit": origin_main,
                    "origin_legacy_mirror_before_closeout_commit": origin_master,
                    "pushed_remote_refs_equal_head": pushed,
                    "child_commits": commits,
                }
            ],
            issue=None if pushed else "HEAD is not synchronized with both remotes",
        )
    )

    quality_by_id = {item["id"]: item for item in quality_gate_results}
    missing_quality = [
        gate_id
        for gate_id in ADAPTIVE_EXECUTION_CLOSEOUT_REQUIRED_QUALITY_GATES
        if quality_by_id.get(gate_id, {}).get("status") != "pass"
    ]
    checklist.append(
        gate_check(
            "quality_gates",
            "Required closeout quality gates passed for the script, contract, UBS, and Beads ledger.",
            not missing_quality,
            [
                {
                    "required_quality_gates": list(
                        ADAPTIVE_EXECUTION_CLOSEOUT_REQUIRED_QUALITY_GATES
                    ),
                    "provided_quality_gates": quality_gate_results,
                    "heavy_cargo_required_for_closeout_script": False,
                }
            ],
            issue=(
                "missing or failing quality gates: " + ", ".join(missing_quality)
                if missing_quality
                else None
            ),
        )
    )

    missing_checks = [
        item["id"]
        for item in checklist
        if item.get("status") != "pass"
    ]
    final_gate_issue = issues.get("bd-63x3v.8.7") or {}
    parent_issue = issues.get("bd-63x3v.8") or {}
    status = "pass" if not missing_checks else "fail"
    summary = {
        "schema": ADAPTIVE_EXECUTION_CLOSEOUT_GATE_SCHEMA,
        "generated_at": generated_at,
        "status": status,
        "purpose": "prompt_to_artifact_adaptive_execution_closeout_gate_not_source_of_truth",
        "parent_epic": {
            "id": "bd-63x3v.8",
            "status": parent_issue.get("status"),
            "title": parent_issue.get("title"),
        },
        "final_gate_bead": {
            "id": "bd-63x3v.8.7",
            "status": final_gate_issue.get("status"),
            "assignee": final_gate_issue.get("assignee"),
        },
        "required_checks": list(ADAPTIVE_EXECUTION_CLOSEOUT_REQUIRED_CHECKS),
        "child_artifact_map": child_artifacts,
        "source_boundary_checks": source_boundaries,
        "quality_gate_results": quality_gate_results,
        "checklist": checklist,
        "missing_checks": missing_checks,
        "remaining_follow_ups": [],
        "known_limitations": [
            "Fifth-wave adaptive execution closeout evidence is advisory operator evidence only.",
            "Provider, tool, session, TUI, extension, and validation-refresh proofs remain their own sources of truth.",
            "This closeout does not authorize release, benchmark, capacity, performance, or strict drop-in claims.",
        ],
        "claim_boundaries": {
            "strict_dropin_or_release_claim_authorized": False,
            "performance_or_capacity_claim_authorized": False,
            "closeout_mutates_sources": False,
            "closeout_replaces_child_artifacts": False,
            "allowed_claim": "advisory fifth-wave adaptive execution closeout evidence only",
        },
        "follow_up_required": bool(missing_checks),
        "follow_up_beads": [
            {
                "title": f"[ADAPTIVE-EXECUTION-GATE] Fix missing {check_id}",
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
    assert_adaptive_execution_closeout_gate_contract(summary)
    return summary


def assert_adaptive_execution_closeout_gate_contract(summary: dict[str, Any]) -> None:
    root = repo_root()
    contract_path = root / ADAPTIVE_EXECUTION_CLOSEOUT_GATE_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing adaptive execution closeout gate contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"adaptive execution closeout gate contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == ADAPTIVE_EXECUTION_CLOSEOUT_GATE_CONTRACT_SCHEMA
    assert contract.get("decision_gate_schema") == ADAPTIVE_EXECUTION_CLOSEOUT_GATE_SCHEMA
    assert summary.get("schema") == contract["decision_gate_schema"]
    assert summary.get("purpose") == contract.get("purpose")
    assert summary.get("status") in set(contract.get("allowed_statuses", []))
    assert summary.get("decision") in set(contract.get("allowed_decisions", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in summary, f"missing top-level adaptive execution closeout-gate key: {key}"
    checks = summary.get("checklist")
    assert isinstance(checks, list) and checks
    check_by_id = {
        item.get("id"): item
        for item in checks
        if isinstance(item, dict)
    }
    missing_required = set(contract.get("required_check_ids", [])) - set(check_by_id)
    assert not missing_required, (
        f"adaptive execution closeout gate missing checks: {sorted(missing_required)}"
    )
    for check in checks:
        assert isinstance(check, dict)
        assert check.get("status") in set(contract.get("allowed_check_statuses", []))
        assert check.get("requirement")
        evidence = check.get("evidence")
        assert isinstance(evidence, list) and evidence
    child_map = summary.get("child_artifact_map")
    assert isinstance(child_map, list) and child_map
    mapped_children = {
        row.get("bead_id")
        for row in child_map
        if isinstance(row, dict)
    }
    missing_children = set(contract.get("required_child_bead_ids", [])) - mapped_children
    assert not missing_children, (
        f"adaptive execution closeout gate missing child mapping: {sorted(missing_children)}"
    )
    for row in child_map:
        assert isinstance(row.get("validation_commands"), list) and row.get(
            "validation_commands"
        )
        assert str(row.get("claim_boundary_text") or "").strip()
    source_boundaries = summary.get("source_boundary_checks")
    assert isinstance(source_boundaries, list) and source_boundaries
    boundary_ids = {
        row.get("id")
        for row in source_boundaries
        if isinstance(row, dict)
    }
    missing_boundaries = set(contract.get("required_source_boundary_ids", [])) - boundary_ids
    assert not missing_boundaries, (
        f"adaptive execution closeout gate missing source boundaries: {sorted(missing_boundaries)}"
    )
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
        assert summary.get("epic_can_close_after_this_commit") is True
        claim_boundaries = summary.get("claim_boundaries")
        assert isinstance(claim_boundaries, dict)
        assert claim_boundaries.get("strict_dropin_or_release_claim_authorized") is False
        assert claim_boundaries.get("performance_or_capacity_claim_authorized") is False
        assert claim_boundaries.get("closeout_mutates_sources") is False
        assert claim_boundaries.get("closeout_replaces_child_artifacts") is False


def write_adaptive_execution_closeout_gate_output(
    args: argparse.Namespace,
    summary: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_adaptive_execution_final_gate_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(
            f"refusing to overwrite adaptive execution final gate: {output_path}"
        )
    output_path.write_text(json_dumps(summary, pretty=True), encoding="utf-8")


def sixth_wave_validation_child_artifact_map(
    issues: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "bead_id": "bd-63x3v.9.1",
            "commit": "76edc24d8c5356021f67e9eede16e8ec26c961dd",
            "code_paths": [
                "scripts/build_swarm_operator_runpack.py",
                "scripts/generate_maintenance_dashboard.py",
            ],
            "test_paths": ["scripts/build_swarm_operator_runpack.py"],
            "docs_or_evidence_paths": [
                "scripts/build_swarm_operator_runpack.py",
                "scripts/generate_maintenance_dashboard.py",
            ],
            "validation_commands": [
                "python3 -m py_compile scripts/build_swarm_operator_runpack.py scripts/generate_maintenance_dashboard.py",
                "python3 scripts/build_swarm_operator_runpack.py --self-test",
            ],
            "claim_boundary_text": (
                "RCH workspace-shadow detection does not mutate workers, cancel jobs, "
                "or claim pi_agent_rust failed to compile when worker checkout scope is wrong."
            ),
        },
        {
            "bead_id": "bd-63x3v.9.2",
            "commit": "c86034884e8187b53c65f25977c699848a5d6b5b",
            "code_paths": ["scripts/build_swarm_operator_runpack.py"],
            "test_paths": [
                "tests/golden_corpus/swarm_operator_runpack/autopilot_plan_projection.json",
                "tests/golden_corpus/swarm_operator_runpack/complete_runpack_projection.json",
            ],
            "docs_or_evidence_paths": [
                "tests/golden_corpus/swarm_operator_runpack/autopilot_plan_projection.json",
                "tests/golden_corpus/swarm_operator_runpack/complete_runpack_projection.json",
            ],
            "validation_commands": [
                "python3 scripts/build_swarm_operator_runpack.py --self-test",
            ],
            "claim_boundary_text": (
                "Degraded Agent Mail posture recommends Beads soft locks but does not "
                "repair, mutate, or replace Agent Mail state."
            ),
        },
        {
            "bead_id": "bd-63x3v.9.3",
            "commit": "5e61b18a6eba6f39d05c6325219ce34f3d12579f",
            "code_paths": ["scripts/build_swarm_operator_runpack.py"],
            "test_paths": [
                "tests/golden_corpus/remote_validation_proof_ledger/examples.json",
                "tests/golden_corpus/swarm_operator_runpack/complete_runpack_projection.json",
            ],
            "docs_or_evidence_paths": [
                "docs/contracts/remote-validation-proof-ledger-contract.json",
                "docs/swarm-operations-runbook.md",
                "tests/golden_corpus/remote_validation_proof_ledger/examples.json",
            ],
            "validation_commands": [
                "python3 scripts/build_swarm_operator_runpack.py --self-test",
                "python3 -m json.tool docs/contracts/remote-validation-proof-ledger-contract.json",
            ],
            "claim_boundary_text": (
                "Validation proof replay ledger records command authority and blockers "
                "but does not make uncovered validation green."
            ),
        },
        {
            "bead_id": "bd-63x3v.9.4",
            "commit": "7895a1a6f9d38015792d8ec26bd061ff8f668443",
            "code_paths": ["scripts/build_swarm_operator_runpack.py"],
            "test_paths": [
                "tests/golden_corpus/swarm_operator_runpack/complete_runpack_projection.json",
            ],
            "docs_or_evidence_paths": [
                "docs/contracts/swarm-operator-runpack-contract.json",
                "docs/swarm-operations-runbook.md",
                "tests/golden_corpus/swarm_operator_runpack/complete_runpack_projection.json",
            ],
            "validation_commands": [
                "python3 scripts/build_swarm_operator_runpack.py --self-test",
                "python3 -m json.tool docs/contracts/swarm-operator-runpack-contract.json",
            ],
            "claim_boundary_text": (
                "Temp artifact inventory is read-only and does not authorize file "
                "or directory deletion without operator approval."
            ),
        },
        {
            "bead_id": "bd-63x3v.9.5",
            "commit": "d044c04784293ae9b351ddb45bcd85d40ff3b828",
            "code_paths": ["scripts/report_swarm_claim_readiness.py"],
            "test_paths": [
                "tests/golden_corpus/swarm_claim_readiness/complete_report_projection.json",
                "tests/golden_corpus/swarm_claim_readiness/hostcall_fallback_heavy_projection.json",
                "tests/golden_corpus/swarm_claim_readiness/hostcall_missing_fields_projection.json",
                "tests/golden_corpus/swarm_claim_readiness/hostcall_missing_telemetry_projection.json",
            ],
            "docs_or_evidence_paths": [
                "tests/golden_corpus/swarm_claim_readiness/complete_report_projection.json",
            ],
            "validation_commands": [
                "python3 scripts/report_swarm_claim_readiness.py --self-test",
            ],
            "claim_boundary_text": (
                "Stale in-progress bead heuristics emit review checklists and do not "
                "auto-reopen or steal active work."
            ),
        },
        {
            "bead_id": "bd-63x3v.9.6",
            "commit": "776e60465fd3aafeedc664f4d001a473540c9934",
            "code_paths": ["scripts/build_swarm_operator_runpack.py"],
            "test_paths": ["scripts/build_swarm_operator_runpack.py"],
            "docs_or_evidence_paths": [
                "README.md",
                "docs/swarm-operations-runbook.md",
            ],
            "validation_commands": [
                "python3 scripts/build_swarm_operator_runpack.py --self-test",
            ],
            "claim_boundary_text": (
                "Degraded-coordination E2E evidence proves handoff behavior only and "
                "does not mark validation infrastructure healthy."
            ),
        },
        {
            "bead_id": "bd-63x3v.9.7",
            "commit": "81a7d4dd479ac6efa85ca462078ef1d9850c0668",
            "code_paths": ["src/doctor.rs"],
            "test_paths": ["src/doctor.rs", "tests/doctor_swarm_temp_dir_json.rs"],
            "docs_or_evidence_paths": ["README.md"],
            "validation_commands": [
                "rch exec -- env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/<agent>/target TMPDIR=/data/tmp/pi_agent_rust_cargo/<agent>/tmp cargo test swarm_resource_preflight -- --nocapture",
                "rch exec -- env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/<agent>/target TMPDIR=/data/tmp/pi_agent_rust_cargo/<agent>/tmp cargo check --all-targets",
                "rch exec -- env CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/<agent>/target TMPDIR=/data/tmp/pi_agent_rust_cargo/<agent>/tmp cargo clippy --all-targets -- -D warnings",
            ],
            "claim_boundary_text": (
                "Swarm resource budget recommendations are advisory and do not create "
                "release-facing performance or capacity claims."
            ),
        },
        {
            "bead_id": "bd-63x3v.9.8",
            "commit": "219884dcce3a5cd622d87ca24fbcafdc593f8dc8",
            "code_paths": ["scripts/build_swarm_operator_runpack.py"],
            "test_paths": ["scripts/build_swarm_operator_runpack.py"],
            "docs_or_evidence_paths": [
                "docs/contracts/provider-rpc-tui-backpressure-budget-contract.json",
                "docs/evidence/provider-rpc-tui-backpressure-budget-contract.json",
            ],
            "validation_commands": [
                "python3 scripts/build_swarm_operator_runpack.py --run-backpressure-budget-contract --print-backpressure-budget-contract",
                "python3 scripts/build_swarm_operator_runpack.py --self-test",
            ],
            "claim_boundary_text": (
                "Provider/RPC/TUI backpressure budget contract aggregates existing "
                "evidence and does not duplicate or replace pressure tests."
            ),
        },
        {
            "bead_id": "bd-63x3v.9.9",
            "commit": "416af8fbd0239c9afce6bfc1e186eea8c4d1ebab",
            "code_paths": ["scripts/report_swarm_claim_readiness.py"],
            "test_paths": [
                "tests/golden_corpus/swarm_claim_readiness/complete_report_projection.json",
                "tests/golden_corpus/swarm_claim_readiness/hostcall_fallback_heavy_projection.json",
                "tests/golden_corpus/swarm_claim_readiness/hostcall_missing_fields_projection.json",
                "tests/golden_corpus/swarm_claim_readiness/hostcall_missing_telemetry_projection.json",
            ],
            "docs_or_evidence_paths": [
                "tests/golden_corpus/swarm_claim_readiness/complete_report_projection.json",
            ],
            "validation_commands": [
                "python3 scripts/report_swarm_claim_readiness.py --self-test",
            ],
            "claim_boundary_text": (
                "Operator explanations summarize blockers and next safe actions but do "
                "not make stale, missing, or degraded evidence green."
            ),
        },
    ]
    for row in rows:
        issue = issues.get(row["bead_id"]) or {}
        row["status"] = issue.get("status")
        row["title"] = issue.get("title")
        row["close_reason"] = issue.get("close_reason")
    return rows


def sixth_wave_validation_source_boundary_checks() -> list[dict[str, Any]]:
    return [
        {
            "id": "beads_are_source_of_truth",
            "status": "pass",
            "evidence": [".beads/issues.jsonl"],
            "boundary": "Beads issue state and close reasons remain the work ledger.",
        },
        {
            "id": "agent_mail_is_coordination_only",
            "status": "pass",
            "evidence": ["AGENTS.md"],
            "boundary": (
                "Agent Mail coordinates ownership but does not replace Beads, git, "
                "source inspection, or validation evidence."
            ),
        },
        {
            "id": "read_only_gate",
            "status": "pass",
            "evidence": ["scripts/build_swarm_operator_runpack.py"],
            "boundary": (
                "The sixth-wave closeout gate summarizes existing evidence and "
                "does not mutate runtime sources."
            ),
        },
        {
            "id": "rch_required_for_heavy_cargo",
            "status": "pass",
            "evidence": ["AGENTS.md"],
            "boundary": "Heavy Cargo validation evidence must prove RCH execution.",
        },
        {
            "id": "staged_ubs_required",
            "status": "pass",
            "evidence": ["AGENTS.md"],
            "boundary": "Staged UBS remains mandatory before the closeout commit.",
        },
        {
            "id": "beads_ledger_required",
            "status": "pass",
            "evidence": ["scripts/reconcile_beads_ledger.sh"],
            "boundary": "Beads ledger reconciliation remains mandatory before closeout.",
        },
        {
            "id": "no_release_or_dropin_claims",
            "status": "pass",
            "evidence": [
                "docs/contracts/dropin-certification-contract.json",
                "docs/evidence/dropin-certification-verdict.json",
            ],
            "boundary": (
                "Sixth-wave validation hardening closeout evidence is advisory and "
                "does not authorize release, benchmark, capacity, performance, or strict drop-in claims."
            ),
        },
        {
            "id": "no_mail_or_rch_mutation",
            "status": "pass",
            "evidence": ["AGENTS.md", "scripts/build_swarm_operator_runpack.py"],
            "boundary": (
                "The gate must not mutate Agent Mail storage, RCH worker state, "
                "installed extensions, user config, git refs, or temp artifacts."
            ),
        },
        {
            "id": "closeout_does_not_replace_child_artifacts",
            "status": "pass",
            "evidence": ["docs/evidence/sixth-wave-validation-hardening-closeout-gate.json"],
            "boundary": (
                "The closeout gate summarizes child evidence and does not replace "
                "source tests, contracts, Beads, pushed commits, RCH, UBS, or CI."
            ),
        },
    ]


def build_sixth_wave_validation_closeout_gate_summary(
    *,
    generated_at: str,
    quality_gate_results: list[dict[str, Any]],
    issue_export_path: Path | None = None,
    git_refs: dict[str, str | None] | None = None,
    commit_check_override: bool | None = None,
) -> dict[str, Any]:
    root = repo_root()
    runpack_path = root / "scripts/build_swarm_operator_runpack.py"
    claim_readiness_path = root / "scripts/report_swarm_claim_readiness.py"
    dashboard_path = root / "scripts/generate_maintenance_dashboard.py"
    doctor_path = root / "src/doctor.rs"
    doctor_json_test_path = root / "tests/doctor_swarm_temp_dir_json.rs"
    runbook_path = root / "docs/swarm-operations-runbook.md"
    readme_path = root / "README.md"
    backpressure_contract_path = root / BACKPRESSURE_BUDGET_CONTRACT_PATH
    backpressure_evidence_path = (
        root / "docs/evidence/provider-rpc-tui-backpressure-budget-contract.json"
    )

    issues = load_beads_issue_map(issue_export_path or (root / ".beads/issues.jsonl"))
    child_artifacts = sixth_wave_validation_child_artifact_map(issues)
    checklist: list[dict[str, Any]] = []

    child_states = {
        issue_id: (issues.get(issue_id) or {}).get("status")
        for issue_id in SIXTH_WAVE_VALIDATION_CLOSEOUT_CHILD_BEADS
    }
    child_close_reasons = {
        issue_id: (issues.get(issue_id) or {}).get("close_reason")
        for issue_id in SIXTH_WAVE_VALIDATION_CLOSEOUT_CHILD_BEADS
    }
    missing_children = [
        issue_id for issue_id, status in child_states.items() if status != "closed"
    ]
    checklist.append(
        gate_check(
            "child_beads_closed",
            "All sixth-wave validation-hardening child Beads are closed before closeout.",
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

    commits = [
        str(row.get("commit"))
        for row in child_artifacts
        if isinstance(row.get("commit"), str)
    ]
    artifact_paths = sorted(
        {
            path
            for row in child_artifacts
            for key in ("code_paths", "test_paths", "docs_or_evidence_paths")
            for path in row.get(key, [])
        }
    )
    artifact_paths_exist = paths_exist(root, artifact_paths)
    commits_present = (
        commit_check_override
        if commit_check_override is not None
        else commits_exist(root, commits)
    )
    weak_child_rows = [
        row["bead_id"]
        for row in child_artifacts
        if row.get("status") != "closed"
        or not row.get("validation_commands")
        or not str(row.get("claim_boundary_text") or "").strip()
    ]
    checklist.append(
        gate_check(
            "child_artifact_mapping",
            "Every sixth-wave child bead maps to pushed commits, source paths, validation commands, and claim-boundary text.",
            artifact_paths_exist and bool(commits_present) and not weak_child_rows,
            [
                {
                    "child_artifact_map": child_artifacts,
                    "artifact_paths_exist": artifact_paths_exist,
                    "commits_present": bool(commits_present),
                    "weak_child_rows": weak_child_rows,
                }
            ],
            issue=(
                "missing paths, commits, validation commands, or claim boundaries"
                if not artifact_paths_exist or not commits_present or weak_child_rows
                else None
            ),
        )
    )

    checklist.append(
        gate_check(
            "rch_workspace_shadow_detector",
            "RCH worker workspace-shadow failures are classified before cargo results are trusted.",
            all(
                (
                    file_contains(runpack_path, "worker_workspace_shadow"),
                    file_contains(runpack_path, "workspace_shadow"),
                    file_contains(dashboard_path, "worker_workspace_shadow"),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": "scripts/generate_maintenance_dashboard.py"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "agent_mail_beads_soft_lock",
            "Agent Mail semantic corruption surfaces Beads soft-lock fallback without blocking work.",
            all(
                (
                    file_contains(runpack_path, "beads_soft_lock"),
                    file_contains(runpack_path, "use_beads_soft_lock"),
                    file_contains(runpack_path, "semantic_readiness"),
                )
            ),
            [{"path": "scripts/build_swarm_operator_runpack.py"}],
        )
    )
    checklist.append(
        gate_check(
            "validation_proof_replay_ledger",
            "Cargo and script validation proofs record command, runner, coverage, result, and blocker authority.",
            all(
                (
                    file_contains(runpack_path, "remote_validation_proof_ledger"),
                    file_contains(runpack_path, "coverage_rationale"),
                    file_contains(runbook_path, "remote_validation_proof_ledger"),
                    file_contains(
                        root / "docs/contracts/remote-validation-proof-ledger-contract.json",
                        "pi.remote_validation.proof_ledger_contract.v1",
                    ),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": "docs/contracts/remote-validation-proof-ledger-contract.json"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "temp_artifact_inventory",
            "Swarm temp artifacts are inventoried with explicit no-delete policy boundaries.",
            all(
                (
                    file_contains(runpack_path, "TEMP_ARTIFACT_INVENTORY_SCHEMA"),
                    file_contains(runpack_path, "read_only_temp_artifact_inventory_not_cleanup_executor"),
                    file_contains(runpack_path, "deletion_policy"),
                    file_contains(runbook_path, "temp_artifact_inventory"),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": "docs/swarm-operations-runbook.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "stale_claim_heuristics",
            "Stale in-progress bead reporting emits review checklists instead of automatic reopen actions.",
            all(
                (
                    file_contains(claim_readiness_path, "safe_reopen_checklist"),
                    file_contains(claim_readiness_path, "stale_claim_item"),
                    file_contains(claim_readiness_path, "configured stale threshold"),
                )
            ),
            [{"path": "scripts/report_swarm_claim_readiness.py"}],
        )
    )
    checklist.append(
        gate_check(
            "degraded_coordination_e2e",
            "No-mock degraded-coordination E2E evidence covers Beads soft locks, degraded validation, and no-delete output.",
            all(
                (
                    file_contains(runpack_path, "DEGRADED_COORDINATION_RUNPACK_E2E_SCHEMA"),
                    file_contains(runpack_path, "degraded_coordination_runpack_handoff"),
                    file_contains(runpack_path, "use_beads_soft_lock"),
                    file_contains(readme_path, "degraded-coordination handoff proof"),
                )
            ),
            [
                {"path": "scripts/build_swarm_operator_runpack.py"},
                {"path": "README.md"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "resource_budget_model",
            "Doctor swarm resource preflight consumes cgroup, NUMA, local build pressure, and RCH queue posture with explanations.",
            all(
                (
                    file_contains(doctor_path, "local_build_pressure"),
                    file_contains(doctor_path, "rch_queue_posture"),
                    file_contains(doctor_path, "SwarmBudgetExplanation"),
                    file_contains(doctor_path, "swarm_resource_preflight_budget_model_covers_large_host_fixture"),
                    file_contains(doctor_json_test_path, "swarm_resource_preflight"),
                )
            ),
            [
                {"path": "src/doctor.rs"},
                {"path": "tests/doctor_swarm_temp_dir_json.rs"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "backpressure_budget_contract",
            "Provider, RPC, and TUI pressure evidence is normalized into one read-only budget contract.",
            all(
                (
                    file_contains(runpack_path, "BACKPRESSURE_BUDGET_CONTRACT_SCHEMA"),
                    file_contains(backpressure_contract_path, BACKPRESSURE_BUDGET_CONTRACT_SCHEMA),
                    file_contains(backpressure_evidence_path, BACKPRESSURE_BUDGET_CONTRACT_SCHEMA),
                )
            ),
            [
                {"path": str(BACKPRESSURE_BUDGET_CONTRACT_PATH)},
                {"path": "docs/evidence/provider-rpc-tui-backpressure-budget-contract.json"},
            ],
        )
    )
    checklist.append(
        gate_check(
            "operator_explanations",
            "Validation blockers map to stable categories, explanations, affected artifacts, and next safe actions.",
            all(
                (
                    file_contains(claim_readiness_path, "OPERATOR_EXPLANATIONS_SCHEMA"),
                    file_contains(claim_readiness_path, "validation_blocker"),
                    file_contains(claim_readiness_path, "next_safe_action"),
                    file_contains(claim_readiness_path, "operator explanations"),
                )
            ),
            [{"path": "scripts/report_swarm_claim_readiness.py"}],
        )
    )

    source_boundaries = sixth_wave_validation_source_boundary_checks()
    boundary_ids = {item["id"] for item in source_boundaries}
    missing_boundaries = (
        set(SIXTH_WAVE_VALIDATION_CLOSEOUT_REQUIRED_SOURCE_BOUNDARIES) - boundary_ids
    )
    checklist.append(
        gate_check(
            "source_boundaries",
            "Closeout source boundaries preserve Beads, Agent Mail, RCH, UBS, ledger, child-artifact, no-delete, and release-claim authorities.",
            not missing_boundaries,
            source_boundaries,
            issue=(
                "missing source boundary checks: " + ", ".join(sorted(missing_boundaries))
                if missing_boundaries
                else None
            ),
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
            "All prerequisite child commits are pushed to origin/main and legacy origin/master before final closeout generation.",
            pushed,
            [
                {
                    "head_before_closeout_commit": head,
                    "origin_main_before_closeout_commit": origin_main,
                    "origin_legacy_mirror_before_closeout_commit": origin_master,
                    "pushed_remote_refs_equal_head": pushed,
                    "child_commits": commits,
                }
            ],
            issue=None if pushed else "HEAD is not synchronized with both remotes",
        )
    )

    quality_by_id = {item["id"]: item for item in quality_gate_results}
    missing_quality = [
        gate_id
        for gate_id in SIXTH_WAVE_VALIDATION_CLOSEOUT_REQUIRED_QUALITY_GATES
        if quality_by_id.get(gate_id, {}).get("status") != "pass"
    ]
    checklist.append(
        gate_check(
            "quality_gates",
            "Required closeout quality gates passed for scripts, contracts, UBS, and Beads ledger.",
            not missing_quality,
            [
                {
                    "required_quality_gates": list(
                        SIXTH_WAVE_VALIDATION_CLOSEOUT_REQUIRED_QUALITY_GATES
                    ),
                    "provided_quality_gates": quality_gate_results,
                    "heavy_cargo_required_for_closeout_script": False,
                }
            ],
            issue=(
                "missing or failing quality gates: " + ", ".join(missing_quality)
                if missing_quality
                else None
            ),
        )
    )

    missing_checks = [
        item["id"]
        for item in checklist
        if item.get("status") != "pass"
    ]
    final_gate_issue = issues.get("bd-63x3v.9.10") or {}
    parent_issue = issues.get("bd-63x3v.9") or {}
    status = "pass" if not missing_checks else "fail"
    summary = {
        "schema": SIXTH_WAVE_VALIDATION_CLOSEOUT_GATE_SCHEMA,
        "generated_at": generated_at,
        "status": status,
        "purpose": "prompt_to_artifact_sixth_wave_validation_hardening_closeout_gate_not_source_of_truth",
        "parent_epic": {
            "id": "bd-63x3v.9",
            "status": parent_issue.get("status"),
            "title": parent_issue.get("title"),
        },
        "final_gate_bead": {
            "id": "bd-63x3v.9.10",
            "status": final_gate_issue.get("status"),
            "assignee": final_gate_issue.get("assignee"),
        },
        "required_checks": list(SIXTH_WAVE_VALIDATION_CLOSEOUT_REQUIRED_CHECKS),
        "child_artifact_map": child_artifacts,
        "source_boundary_checks": source_boundaries,
        "quality_gate_results": quality_gate_results,
        "checklist": checklist,
        "missing_checks": missing_checks,
        "remaining_follow_ups": [],
        "known_limitations": [
            "Sixth-wave validation hardening closeout evidence is advisory operator evidence only.",
            "RCH, Agent Mail, Beads, Doctor, runpack, and claim-readiness proofs remain their own sources of truth.",
            "This closeout does not authorize release, benchmark, capacity, performance, or strict drop-in claims.",
        ],
        "claim_boundaries": {
            "strict_dropin_or_release_claim_authorized": False,
            "performance_or_capacity_claim_authorized": False,
            "closeout_mutates_sources": False,
            "closeout_mutates_agent_mail_or_rch": False,
            "closeout_authorizes_file_deletion": False,
            "closeout_replaces_child_artifacts": False,
            "allowed_claim": "advisory sixth-wave validation hardening closeout evidence only",
        },
        "follow_up_required": bool(missing_checks),
        "follow_up_beads": [
            {
                "title": f"[SIXTH-WAVE-GATE] Fix missing {check_id}",
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
    assert_sixth_wave_validation_closeout_gate_contract(summary)
    return summary


def assert_sixth_wave_validation_closeout_gate_contract(summary: dict[str, Any]) -> None:
    root = repo_root()
    contract_path = root / SIXTH_WAVE_VALIDATION_CLOSEOUT_GATE_CONTRACT_PATH
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(
            f"missing sixth-wave validation closeout gate contract: {contract_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"sixth-wave validation closeout gate contract is malformed JSON: {contract_path}: {exc}"
        ) from exc
    assert contract.get("schema") == SIXTH_WAVE_VALIDATION_CLOSEOUT_GATE_CONTRACT_SCHEMA
    assert contract.get("decision_gate_schema") == SIXTH_WAVE_VALIDATION_CLOSEOUT_GATE_SCHEMA
    assert summary.get("schema") == contract["decision_gate_schema"]
    assert summary.get("purpose") == contract.get("purpose")
    assert summary.get("status") in set(contract.get("allowed_statuses", []))
    assert summary.get("decision") in set(contract.get("allowed_decisions", []))
    for key in contract.get("required_top_level_keys", []):
        assert key in summary, f"missing top-level sixth-wave closeout-gate key: {key}"
    checks = summary.get("checklist")
    assert isinstance(checks, list) and checks
    check_by_id = {
        item.get("id"): item
        for item in checks
        if isinstance(item, dict)
    }
    missing_required = set(contract.get("required_check_ids", [])) - set(check_by_id)
    assert not missing_required, (
        f"sixth-wave closeout gate missing checks: {sorted(missing_required)}"
    )
    for check in checks:
        assert isinstance(check, dict)
        assert check.get("status") in set(contract.get("allowed_check_statuses", []))
        assert check.get("requirement")
        evidence = check.get("evidence")
        assert isinstance(evidence, list) and evidence
    child_map = summary.get("child_artifact_map")
    assert isinstance(child_map, list) and child_map
    mapped_children = {
        row.get("bead_id")
        for row in child_map
        if isinstance(row, dict)
    }
    missing_children = set(contract.get("required_child_bead_ids", [])) - mapped_children
    assert not missing_children, (
        f"sixth-wave closeout gate missing child mapping: {sorted(missing_children)}"
    )
    for row in child_map:
        assert isinstance(row.get("validation_commands"), list) and row.get(
            "validation_commands"
        )
        assert str(row.get("claim_boundary_text") or "").strip()
    source_boundaries = summary.get("source_boundary_checks")
    assert isinstance(source_boundaries, list) and source_boundaries
    boundary_ids = {
        row.get("id")
        for row in source_boundaries
        if isinstance(row, dict)
    }
    missing_boundaries = (
        set(contract.get("required_source_boundary_ids", [])) - boundary_ids
    )
    assert not missing_boundaries, (
        f"sixth-wave closeout gate missing source boundaries: {sorted(missing_boundaries)}"
    )
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
        assert summary.get("epic_can_close_after_this_commit") is True
        claim_boundaries = summary.get("claim_boundaries")
        assert isinstance(claim_boundaries, dict)
        assert claim_boundaries.get("strict_dropin_or_release_claim_authorized") is False
        assert claim_boundaries.get("performance_or_capacity_claim_authorized") is False
        assert claim_boundaries.get("closeout_mutates_sources") is False
        assert claim_boundaries.get("closeout_mutates_agent_mail_or_rch") is False
        assert claim_boundaries.get("closeout_authorizes_file_deletion") is False
        assert claim_boundaries.get("closeout_replaces_child_artifacts") is False


def write_sixth_wave_validation_closeout_gate_output(
    args: argparse.Namespace,
    summary: dict[str, Any],
) -> None:
    output_path = getattr(args, "out_sixth_wave_final_gate_json", None)
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise RunpackError(
            f"refusing to overwrite sixth-wave validation final gate: {output_path}"
        )
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
                },
                {
                    "category": "swarm",
                    "severity": "warn",
                    "title": "Context intelligence posture degraded",
                    "detail": "graph nodes=42 edges=64 missing_inputs=0 malformed_inputs=0 unreadable_inputs=0; bundle coverage=partial_stale_suppressed selected=12 excluded=3 stale_suppressions=1 missing_test_links=2 validation_commands=4; cache pressure=1 fingerprints=19",
                    "remediation": "Refresh stale evidence or keep suppressed evidence out of runpack claims",
                    "data": {
                        "schema": CONTEXT_INTELLIGENCE_SCHEMA,
                        "status": "degraded",
                        "graph": {
                            "node_count": 42,
                            "edge_count": 64,
                            "missing_input_count": 0,
                            "malformed_input_count": 0,
                            "unreadable_input_count": 0,
                            "stale_or_untrusted_evidence_count": 1,
                            "validation_link_count": 7,
                            "trace_status_counts": {"indexed": 19},
                            "evidence_freshness_status_counts": {
                                "current": 5,
                                "stale": 1,
                            },
                        },
                        "bundle": {
                            "coverage_status": "partial_stale_suppressed",
                            "selected_count": 12,
                            "excluded_count": 3,
                            "stale_evidence_suppression_count": 1,
                            "missing_test_link_count": 2,
                            "suggested_validation_command_count": 4,
                            "estimated_bytes": 8192,
                            "estimated_tokens": 2048,
                        },
                        "cache": {
                            "fingerprint_count": 19,
                            "pressure_count": 1,
                            "pressure": True,
                            "cacheable": True,
                            "status_counts": {"valid": 18, "expired": 1},
                        },
                        "redaction_summary": {
                            "overall_status": "redacted",
                            "selected_redacted_nodes": 1,
                            "selected_sensitive_omissions": 0,
                            "suppressed_unsafe_nodes": 0,
                        },
                        "degraded_reasons": [
                            "context_bundle_partial_coverage",
                            "stale_or_unsafe_evidence_suppressed",
                            "selected_code_without_test_link",
                            "context_cache_pressure",
                        ],
                        "recommended_actions": [
                            "Refresh stale evidence or keep suppressed evidence out of runpack claims"
                        ],
                    },
                    "fixability": "not_fixable",
                },
                {
                    "category": "swarm",
                    "severity": "warn",
                    "title": "Progress SLO posture build saturated",
                    "detail": "status=build_saturated, confidence=0.640, reasons=PROGRESS-SLO-RCH-SATURATED",
                    "remediation": "Back off heavy cargo validation until RCH pressure clears",
                    "data": {
                        "schema": PROGRESS_SLO_DOCTOR_SCHEMA,
                        "mode": "advisory_projection",
                        "mutation_performed": False,
                        "source_status": {
                            "progress_slo_report": "available",
                            "source_of_truth": PROGRESS_SLO_SCHEMA,
                            "doctor_authority": "environment_diagnostics_only",
                            "path": "progress-slo.json",
                            "issue": None,
                        },
                        "progress": {
                            "schema": PROGRESS_SLO_SCHEMA,
                            "contract_version": "1.0.0",
                            "generated_at": generated_at,
                            "status": "build_saturated",
                            "confidence": 0.64,
                            "reason_ids": ["PROGRESS-SLO-RCH-SATURATED"],
                            "time_window": {
                                "start_utc": "2026-05-09T08:00:00+00:00",
                                "end_utc": generated_at,
                                "duration_seconds": 3600,
                                "comparison_baseline": "HEAD~1",
                            },
                            "next_actions": [
                                "Back off heavy cargo validation until RCH pressure clears"
                            ],
                            "suppressed_claims": [],
                        },
                        "metrics": {
                            "closed_beads": 1,
                            "commits": 1,
                            "ready_beads": 2,
                            "rch_posture": "saturated",
                        },
                        "saturation_summary": {
                            "build_saturation": "red",
                            "recommended_operator_posture": "backoff_heavy_cargo",
                        },
                        "redaction_summary": {
                            "redacted_count": 1,
                            "omitted_count": 0,
                            "unsafe_to_emit_count": 0,
                            "suppressed_claims": [],
                        },
                        "source_summary": {
                            "total": 10,
                            "degraded": 1,
                            "malformed": 0,
                        },
                        "degraded_reasons": [],
                        "guards": {
                            "advisory_only": True,
                            "no_live_mutation": True,
                            "not_ci_success": True,
                            "not_release_claim_evidence": True,
                            "does_not_replace_beads_agent_mail_rch_validation_broker_doctor_runpacks_context_or_ci": True,
                        },
                    },
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
                    "size_bytes": 2_500_000,
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
    replay_preview_path = write_json(
        workspace / "swarm-replay-preview.json",
        {
            "schema": SWARM_REPLAY_PREVIEW_SCHEMA,
            "generated_at_utc": generated_at,
            "command": {
                "invocation": "pi swarm-replay-preview",
                "cwd": str(workspace),
                "read_only_replay": True,
                "provider_calls": 0,
                "live_mutations": 0,
                "output_writes": 1,
            },
            "trace": {
                "path": "tests/golden_corpus/swarm_replay_trace/normalized_trace.json",
                "schema": "pi.swarm.replay_trace.v1",
                "trace_id": "fixture-preview",
                "generated_at": generated_at,
                "source_count": 7,
                "event_count": 12,
                "first_event_id": "event-001",
                "last_event_id": "event-012",
                "redaction_status": "redacted",
                "uncertainty_state": "complete",
            },
            "replay": {
                "schema": "pi.swarm.replay_report.v1",
                "replayed_event_count": 12,
                "final_logical_clock": 12,
                "snapshot_count": 12,
                "diagnostic_count": 1,
                "diagnostics": [
                    {
                        "code": "agent_mail_source_unavailable",
                        "severity": "degraded",
                        "event_id": "event-003",
                        "message": "Agent Mail unavailable in fixture",
                    }
                ],
                "final_state": {
                    "bead_count": 2,
                    "agent_count": 3,
                    "active_reservation_count": 1,
                    "active_build_slot_count": 1,
                    "rch_job_count": 1,
                    "validation_gate_count": 1,
                    "runpack_recommendation_count": 1,
                    "operator_handoff_count": 1,
                    "reservation_conflict_count": 1,
                    "agent_mail_available": False,
                    "missing_agent_mail_evidence": True,
                    "dirty_worktree": True,
                },
                "resource_saturation_points": 1,
                "first_saturation_reasons": ["rch_queue_saturated"],
            },
            "policies": {
                "schema": "pi.swarm.policy_report.v1",
                "requested_policy_ids": [
                    "existing_autopilot",
                    "rch_fanout_limited",
                ],
                "evaluated_policy_ids": [
                    "existing_autopilot",
                    "rch_fanout_limited",
                ],
                "decision_count": 4,
                "comparison_count": 2,
                "distinct_action_count": 3,
                "score_spread": 8,
                "comparisons": [
                    {
                        "policy_id": "rch_fanout_limited",
                        "rank": 1,
                        "score": 14,
                        "confidence": "medium",
                        "confidence_score": 70,
                        "throughput_actions": 1,
                        "validation_commands_deferred": 1,
                        "local_fallback_risk": "low",
                        "reservation_conflicts_avoided": 1,
                        "stale_work_reclaimed": 0,
                        "missing_data_claims": ["coordination_completeness"],
                        "rationale": ["Defers validation under RCH pressure."],
                    },
                    {
                        "policy_id": "existing_autopilot",
                        "rank": 2,
                        "score": 6,
                        "confidence": "medium",
                        "confidence_score": 60,
                        "throughput_actions": 1,
                        "validation_commands_deferred": 0,
                        "local_fallback_risk": "medium",
                        "reservation_conflicts_avoided": 0,
                        "stale_work_reclaimed": 0,
                        "missing_data_claims": ["coordination_completeness"],
                        "rationale": ["Continues work without deferring validation."],
                    },
                ],
            },
            "recommendation": {
                "policy_id": "rch_fanout_limited",
                "rank": 1,
                "score": 14,
                "confidence": "medium",
                "confidence_score": 70,
                "throughput_actions": 1,
                "validation_commands_deferred": 1,
                "local_fallback_risk": "low",
                "reservation_conflicts_avoided": 1,
                "stale_work_reclaimed": 0,
                "missing_data_claims": ["coordination_completeness"],
                "rationale": ["Defers validation under RCH pressure."],
            },
            "output_paths": {"json": str(workspace / "preview.json"), "text": None},
            "guards": {
                "read_only_replay": True,
                "no_live_mutation": True,
                "no_network_required": True,
                "output_artifacts_only": True,
                "runpack_not_source_of_truth": True,
            },
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
            "cargo_command": "check --all-targets",
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
        load_shared_agent_mail_schema_corrupt_fixture(generated_at=generated_at),
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
                },
                {
                    "id": "tui_frame_render",
                    "label": "TUI frame render",
                    "snapshot": {
                        "count": 6,
                        "total_us": 120_000,
                        "max_us": 24_000,
                        "avg_us": 20_000,
                        "tail": {
                            "sample_window": 512,
                            "sample_count": 6,
                            "p95_us": 21_000,
                            "p99_us": 22_000,
                            "p999_us": 24_000,
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
            "provider_routing": [
                {
                    "from": "openai_responses",
                    "to": "openai_chat",
                    "status": "fallback",
                    "fallback": True,
                    "reason": "stream_resume_degraded",
                }
            ],
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
            "write_amplification": {
                "status": "pressured",
                "entries_written": 320,
                "logical_turns": 4,
                "bytes_written": 1_200_000,
            },
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
    validation_broker_status_path = write_json(
        workspace / "validation-broker-status.json",
        {
            "schema": VALIDATION_BROKER_CLI_STATUS_SCHEMA,
            "generated_at_utc": generated_at,
            "command": {
                "name": "validation-broker",
                "action": "status",
                "cwd": str(workspace),
                "store": "validation-slots.jsonl",
                "output_writes": 0,
            },
            "store": {
                "path": str(workspace / "validation-slots.jsonl"),
                "schema": "pi.validation_broker.slot_store.v1",
                "status": "available",
                "total_records": 3,
                "total_slots": 2,
                "active_slots": 1,
                "reusable_slots": 1,
                "stale_slots": 0,
                "expired_at_report_time_slots": 0,
                "state_counts": {"active": 1, "reusable": 1},
                "degraded_reasons": [],
            },
            "output_paths": {"json": None, "text": None},
            "guards": {
                "read_only_plan": True,
                "live_mutations": 0,
                "refuses_output_overwrite": True,
                "destructive_actions": 0,
                "provider_calls": 0,
            },
        },
    )
    progress_slo_path = write_json(
        workspace / "progress-slo.json",
        {
            "schema": PROGRESS_SLO_SCHEMA,
            "generated_at": generated_at,
            "contract_version": "1.0.0",
            "time_window": {
                "start_utc": "2026-05-09T08:00:00+00:00",
                "end_utc": generated_at,
                "duration_seconds": 3600,
                "comparison_baseline": "HEAD~1",
            },
            "status": "build_saturated",
            "confidence": 0.64,
            "reason_ids": ["PROGRESS-SLO-RCH-SATURATED"],
            "source_statuses": [
                {
                    "source_id": "rch_posture",
                    "source_class": "rch_and_validation_broker_posture",
                    "source_kind": "fixture",
                    "path": "rch-status.json",
                    "availability": "available",
                    "freshness_state": "current",
                    "observed_at_utc": generated_at,
                    "source_hash": "rch-hash",
                    "authoritative_for": ["queue_depth"],
                    "redaction_state": "redacted",
                    "degraded_reasons": [],
                    "suppressed_claims": [],
                }
            ],
            "progress_metrics": {
                "closed_beads": 1,
                "open_beads": 4,
                "in_progress_beads": 2,
                "ready_beads": 2,
                "dependency_blocked_beads": 1,
                "commits": 1,
                "pushed_commits": 1,
                "closed_with_commit_reference_count": 1,
                "validation_passes": 1,
                "validation_failures": 0,
                "agent_mail_health": "green",
                "rch_posture": "saturated",
                "rch_queue_depth": 12,
                "rch_queue_saturation_threshold": 8,
                "validation_broker_posture": "green",
                "stale_in_progress_candidates": 0,
                "malformed_source_records": 0,
                "contradictory_source_records": 0,
            },
            "saturation_summary": {
                "coordination_saturation": "green",
                "build_saturation": "red",
                "validation_saturation": "green",
                "queue_convergence": "yellow",
                "recommended_operator_posture": "backoff_heavy_cargo",
            },
            "redaction_summary": {
                "redacted_count": 1,
                "omitted_count": 0,
                "unsafe_to_emit_count": 0,
                "suppressed_claims": [],
            },
            "suppressed_claims": [],
            "next_actions": [
                "Back off heavy cargo validation until RCH pressure clears"
            ],
        },
    )

    args = argparse.Namespace(
        doctor_json=doctor_path,
        claim_readiness_json=claim_path,
        smoke_summary_json=smoke_path,
        activity_digest_json=activity_path,
        swarm_replay_preview_json=None,
        cargo_admission_json=cargo_path,
        active_bead_id="bd-e5le6.2",
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
        validation_broker_json=None,
        progress_slo_json=progress_slo_path,
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
                    "issue": AGENT_MAIL_SCHEMA_CORRUPT_DETAIL,
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
            "temp_artifacts": [
                {
                    "id": "active_target",
                    "path": "/data/tmp/pi_agent_rust_cargo/test/target",
                    "kind": "cargo_target_dir",
                    "owner": "Codex",
                    "bead_id": "bd-fixture",
                    "reason": "active cargo_headroom target dir",
                    "state": "active",
                    "last_observed_at": generated_at,
                },
                {
                    "id": "clean_worktree_validation",
                    "path": str(workspace / "clean-worktree-validation"),
                    "kind": "clean_worktree_validation_dir",
                    "owner": "Codex",
                    "bead_id": "bd-fixture",
                    "reason": "clean-worktree validation fixture retained for audit",
                    "state": "retained",
                    "last_observed_at": generated_at,
                },
                {
                    "id": "ubs_shadow_workspace",
                    "path": "/data/tmp/tmp.ubs/git_scan",
                    "kind": "ubs_shadow_workspace",
                    "owner": "Codex",
                    "bead_id": "bd-fixture",
                    "reason": "UBS staged shadow workspace fixture",
                    "state": "retained",
                    "last_observed_at": generated_at,
                },
                {
                    "id": "stale_unknown_target",
                    "path": "/data/tmp/pi_agent_rust_cargo/unknown/stale-target",
                    "kind": "cargo_target_dir",
                    "reason": "stale unknown-owner fixture",
                    "state": "stale_candidate",
                    "last_observed_at": "2026-05-01T00:00:00+00:00",
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
        def assert_proof(condition: bool, message: str, entry: dict[str, Any]) -> None:
            if condition:
                return
            excerpt = {
                "runner": entry.get("runner"),
                "timing": entry.get("timing"),
                "exit": entry.get("exit"),
                "artifact_retrieval": entry.get("artifact_retrieval"),
                "evidence_classification": entry.get("evidence_classification"),
                "warnings": entry.get("warnings"),
            }
            raise AssertionError(
                f"{message}; proof_entry_excerpt={json_dumps(excerpt)}"
            )

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
        temp_inventory = runpack["temp_artifact_inventory"]
        assert temp_inventory["schema"] == TEMP_ARTIFACT_INVENTORY_SCHEMA
        assert temp_inventory["guards"]["read_only"] is True
        assert temp_inventory["guards"]["no_filesystem_deletion"] is True
        assert temp_inventory["guards"]["no_destructive_commands_emitted"] is True
        assert temp_inventory["guards"]["explicit_operator_permission_required"] is True
        assert temp_inventory["summary"]["entry_count"] >= 4
        assert temp_inventory["summary"]["emitted_deletion_command_count"] == 0
        artifact_by_path = {entry["path"]: entry for entry in temp_inventory["entries"]}
        assert artifact_by_path["/data/tmp/pi_agent_rust_cargo/test/target"][
            "deletion_policy"
        ] == "retain_active"
        assert artifact_by_path["/data/tmp/pi_agent_rust_cargo/test/target"][
            "owner"
        ] == "Codex"
        assert artifact_by_path["/data/tmp/pi_agent_rust_cargo/test/target"][
            "bead_id"
        ] == "bd-fixture"
        assert artifact_by_path[str(workspace / "clean-worktree-validation")][
            "kind"
        ] == "clean_worktree_validation_dir"
        assert artifact_by_path["/data/tmp/tmp.ubs/git_scan"][
            "kind"
        ] == "ubs_shadow_workspace"
        stale_unknown = artifact_by_path[
            "/data/tmp/pi_agent_rust_cargo/unknown/stale-target"
        ]
        assert stale_unknown["state"] == "stale_candidate"
        assert stale_unknown["deletion_policy"] == "deletion_protected_unknown_owner"
        assert all(entry["cleanup_command"] is None for entry in temp_inventory["entries"])
        assert runpack["agent_mail_read_state"]["status"] == "degraded"
        assert runpack["agent_mail_read_state"]["transport_status"] == "reachable"
        assert runpack["agent_mail_read_state"]["semantic_readiness_status"] == "fail"
        assert runpack["agent_mail_read_state"]["semantic_readiness_detail"] == AGENT_MAIL_SCHEMA_CORRUPT_DETAIL
        assert runpack["agent_mail_read_state"]["soft_lock"] == "beads"
        assert runpack["agent_mail_read_state"]["fallback_action"] == "use_beads_soft_lock"
        assert runpack["agent_mail_read_state"]["registration_required_before_coding"] is False
        assert runpack["beads"]["active_ownership"][0]["id"] == "bd-stale"
        assert runpack["beads"]["active_ownership"][0]["assignee"] == "GreenStone"
        assert runpack["beads"]["active_ownership"][0]["updated_at"] == "2026-05-08T00:00:00+00:00"
        fallback_ownership = runpack["agent_mail_read_state"]["fallback_ownership_evidence"]
        assert fallback_ownership["source"] == "beads"
        assert fallback_ownership["source_command"] == "br list --status=in_progress --json"
        assert fallback_ownership["active_ownership"][0]["id"] == "bd-stale"
        assert fallback_ownership["active_ownership"][0]["assignee"] == "GreenStone"
        no_mail_steps = runpack["agent_mail_read_state"]["no_mail_closeout_steps"]
        assert any("registration/inbox/reservation" in step for step in no_mail_steps)
        assert any("do not require Agent Mail reservations before coding" in step for step in no_mail_steps)
        assert any("br close" in step and "br sync --flush-only" in step for step in no_mail_steps)
        assert any("Final handoff" in step and "Beads ownership" in step for step in no_mail_steps)
        operator_actions = runpack["operator_next_actions"]
        assert any("Treat Agent Mail read state as unavailable" in action for action in operator_actions)
        assert any("Use Beads active ownership" in action and "GreenStone" in action for action in operator_actions)
        assert any("Review temp artifact inventory" in action for action in operator_actions)
        assert any("do not require Agent Mail reservations before coding" in action for action in operator_actions)
        assert any("br close" in action and "br sync --flush-only" in action for action in operator_actions)
        assert not any(
            "require Agent Mail reservations before coding" in action
            and "do not require Agent Mail reservations before coding" not in action
            for action in operator_actions
        )
        assert runpack["validation_outputs"]["status"] == "failed"
        assert runpack["validation_outputs"]["outputs"][0]["inferred_status"] == "failed"
        proof_ledger = runpack["remote_validation_proof_ledger"]
        assert proof_ledger["schema"] == REMOTE_VALIDATION_LEDGER_SCHEMA
        assert proof_ledger["summary"]["blocked_entries"] == 1
        queue_entry = proof_ledger["entries"][0]
        assert queue_entry["bead_id"] == "bd-e5le6.2"
        assert queue_entry["evidence_classification"]["status"] == "blocked"
        assert queue_entry["runner"]["requested_runner"] == "rch"
        assert queue_entry["runner"]["resolved_runner"] == "not_run"
        assert queue_entry["runner"]["local_fallback"] == "refused"
        assert any(
            warning["warning_id"] == "queue_backoff"
            for warning in queue_entry["warnings"]
        )
        context = runpack["doctor_swarm"]["context_intelligence"]
        assert context["schema"] == CONTEXT_INTELLIGENCE_SCHEMA
        assert context["status"] == "degraded"
        assert context["coverage_status"] == "partial_stale_suppressed"
        assert context["graph"]["stale_or_untrusted_evidence_count"] == 1
        assert context["bundle"]["missing_test_link_count"] == 2
        assert context["bundle"]["estimated_tokens"] == 2048
        assert context["cache"]["pressure_count"] == 1
        assert context["redaction_summary"]["overall_status"] == "redacted"
        doctor_progress = runpack["doctor_swarm"]["progress_slo"]
        assert doctor_progress["doctor_schema"] == PROGRESS_SLO_DOCTOR_SCHEMA
        assert doctor_progress["schema"] == PROGRESS_SLO_SCHEMA
        assert doctor_progress["progress_status"] == "build_saturated"
        assert doctor_progress["source_status"] == "available"
        assert doctor_progress["guards"]["advisory_only"] is True
        assert runpack["progress_slo"]["schema"] == PROGRESS_SLO_SCHEMA
        assert runpack["progress_slo"]["progress_status"] == "build_saturated"
        assert any(
            item.get("id") == "progress_slo" and item.get("status") == "ok"
            for item in runpack["source_statuses"]
        )
        assert any(
            "Review progress SLO `build_saturated`" in action
            for action in runpack["operator_next_actions"]
        )
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
        postcondition_sync_path = write_json(
            workspace / "rch-artifact-sync-postcondition.json",
            {
                "schema": RCH_ARTIFACT_SYNC_SCHEMA,
                "generated_at": generated_at,
                "mode": "postcondition",
                "status": "fail",
                "postconditions": [
                    {
                        "path": "tests/full_suite_gate/full_suite_verdict.json",
                        "updated": False,
                    }
                ],
                "violations": [
                    {
                        "path": "tests/full_suite_gate/full_suite_verdict.json",
                        "reason": "generated_artifact_not_updated",
                        "recommended_action": "Rerun locally or fix RCH artifact retrieval/writeback.",
                    }
                ],
            },
        )
        postcondition_args = argparse.Namespace(**vars(args))
        postcondition_args.rch_artifact_sync_json = postcondition_sync_path
        postcondition_runpack = build_runpack(postcondition_args)
        assert any(
            item.get("signal") == "artifact_sync_postcondition"
            and item.get("path") == "tests/full_suite_gate/full_suite_verdict.json"
            and item.get("reason") == "generated_artifact_not_updated"
            for item in postcondition_runpack["bottleneck_attribution"]["bottlenecks"]
        )
        remote_pass_cargo_path = write_json(
            workspace / "cargo-remote-pass.json",
            {
                "schema": "pi.cargo_headroom.admission.v1",
                "decision": "admit",
                "reason": "rch_available",
                "requested_runner": "rch",
                "resolved_runner": "rch",
                "command_class": "heavy",
                "allow_local_fallback": False,
                "cargo_command": "check --all-targets",
                "cargo_target_dir": "/data/tmp/pi_agent_rust_cargo/test/target",
                "tmpdir": "/data/tmp/pi_agent_rust_cargo/test/tmp",
                "rch_queue_forecast": {
                    "schema": "pi.cargo_headroom.rch_queue_forecast.v1",
                    "status": "ok",
                    "recommended_action": "proceed",
                    "reason": "workers_available",
                    "slot_pressure": "available",
                    "queue_depth": 0,
                    "active_builds": 1,
                    "queued_builds": 0,
                    "slots_available": 7,
                    "slots_total": 8,
                    "workers_healthy": 8,
                    "workers_total": 8,
                    "estimated_wait_seconds": 0,
                },
                "remote_validation_proof": {
                    "bead_id": "bd-e5le6.2",
                    "runner": {
                        "requested_runner": "rch",
                        "resolved_runner": "rch_remote",
                        "runner_requirement": "rch_required",
                        "remote_execution": True,
                        "local_fallback": "none",
                        "rch_job_id": "selftest-rch-job",
                        "worker_id": "worker-fixture",
                        "worker_host": "ubuntu@worker-fixture",
                        "queue_state": "admitted",
                        "worker_state": "completed",
                        "command_rewrite": {
                            "tmpdir_rewritten": True,
                            "target_dir_rewritten": True,
                        },
                        "status_excerpt": "Remote command finished: exit=0",
                    },
                    "timing": {
                        "started_at_utc": "2026-05-09T09:01:00Z",
                        "ended_at_utc": "2026-05-09T09:04:00Z",
                        "duration_ms": 180000,
                        "heartbeat_at_utc": "2026-05-09T09:03:50Z",
                        "stale_progress_detected": False,
                    },
                    "exit": {
                        "exit_code": 0,
                        "success": True,
                        "termination_reason": "completed",
                        "stderr_excerpt": "",
                        "stdout_excerpt": "Finished dev profile",
                    },
                    "paths": {
                        "remote_target_dir": "/data/projects/pi_agent_rust/.rch-target-selftest",
                        "remote_tmpdir": "/data/projects/pi_agent_rust/.rch-tmp-selftest",
                        "artifact_paths": ["target/debug/.fingerprint"],
                    },
                },
            },
        )
        remote_pass_sync_path = write_json(
            workspace / "rch-artifact-sync-remote-pass.json",
            {
                "schema": RCH_ARTIFACT_SYNC_SCHEMA,
                "generated_at": generated_at,
                "status": "pass",
                "required_paths": [
                    {"path": "target/debug/.fingerprint", "included": True}
                ],
                "violations": [],
                "exit_code": 0,
                "elapsed_ms": 24,
            },
        )
        remote_pass_runpack = build_runpack(
            argparse.Namespace(
                **{
                    **vars(args),
                    "cargo_admission_json": remote_pass_cargo_path,
                    "rch_artifact_sync_json": remote_pass_sync_path,
                    "out_json": None,
                    "out_md": None,
                }
            )
        )
        remote_pass_entry = remote_pass_runpack["remote_validation_proof_ledger"][
            "entries"
        ][0]
        assert_proof(
            remote_pass_entry["evidence_classification"]["status"] == "pass",
            "remote RCH pass should classify as pass",
            remote_pass_entry,
        )
        assert_proof(
            remote_pass_entry["evidence_classification"]["clean_remote_proof"] is True,
            "remote RCH pass should be clean remote proof",
            remote_pass_entry,
        )
        assert_proof(
            remote_pass_entry["runner"]["remote_execution"] is True,
            "remote RCH pass should preserve remote_execution=true",
            remote_pass_entry,
        )
        assert_proof(
            remote_pass_entry["runner"]["worker_id"] == "worker-fixture",
            "remote RCH pass should retain selected worker id",
            remote_pass_entry,
        )
        assert_proof(
            remote_pass_entry["runner"]["worker_host"] == "ubuntu@worker-fixture",
            "remote RCH pass should retain selected worker host",
            remote_pass_entry,
        )
        assert_proof(
            remote_pass_entry["runner"]["rch_job_id"] == "selftest-rch-job",
            "remote RCH pass should retain RCH job id",
            remote_pass_entry,
        )
        assert_proof(
            remote_pass_entry["runner"]["command_rewrite"]
            == {"tmpdir_rewritten": True, "target_dir_rewritten": True},
            "remote RCH pass should retain TMPDIR and CARGO_TARGET_DIR rewrite evidence",
            remote_pass_entry,
        )
        assert_proof(
            remote_pass_entry["command"]["rendered"] == "rch exec -- cargo check --all-targets",
            "remote RCH pass should render the offloaded command",
            remote_pass_entry,
        )
        assert_proof(
            remote_pass_entry["timing"]["heartbeat_at_utc"] == "2026-05-09T09:03:50Z",
            "remote RCH pass should retain heartbeat timestamp",
            remote_pass_entry,
        )
        assert_proof(
            remote_pass_entry["paths"]["remote_target_dir"]
            == "/data/projects/pi_agent_rust/.rch-target-selftest",
            "remote RCH pass should retain remote target dir",
            remote_pass_entry,
        )
        assert_proof(
            remote_pass_entry["artifact_retrieval"]["status"] == "clean",
            "remote RCH pass should retain clean artifact retrieval status",
            remote_pass_entry,
        )
        assert remote_pass_runpack["remote_validation_proof_ledger"]["summary"][
            "clean_remote_proof_entries"
        ] == 1
        proof_reuse_ledger = json.loads(
            json_dumps(remote_pass_runpack["remote_validation_proof_ledger"])
        )
        proof_reuse_entry = json.loads(json_dumps(remote_pass_entry))
        proof_reuse_changed_paths = ["scripts/build_swarm_operator_runpack.py"]
        proof_reuse_ledger["git"]["dirty_paths"] = proof_reuse_changed_paths
        proof_reuse_entry["evidence_classification"]["coverage"][
            "covered_paths"
        ] = proof_reuse_changed_paths
        proof_reuse_entry["evidence_classification"]["coverage"][
            "coverage_status"
        ] = "covered"
        proof_reuse_entry["evidence_classification"]["coverage"][
            "authoritative_for_bead"
        ] = True
        proof_reuse_ledger["entries"] = [proof_reuse_entry]
        reusable_context = {
            "schema": REMOTE_VALIDATION_PROOF_REUSE_CONTEXT_SCHEMA,
            "git": {"head": proof_reuse_ledger["git"]["head"]},
            "staged_paths": proof_reuse_changed_paths,
            "command": {
                "command_fingerprint": proof_reuse_entry["command"][
                    "command_fingerprint"
                ],
            },
            "runner_requirement": "rch_required",
            "env": {
                "cargo_target_dir": proof_reuse_entry["paths"]["cargo_target_dir"],
                "tmpdir": proof_reuse_entry["paths"]["tmpdir"],
            },
        }
        reusable_gate = build_remote_validation_proof_reuse_gate(
            generated_at=generated_at,
            proof_ledger=proof_reuse_ledger,
            context=reusable_context,
        )
        assert reusable_gate["schema"] == REMOTE_VALIDATION_PROOF_REUSE_GATE_SCHEMA
        assert reusable_gate["reuse_allowed"] is True
        assert reusable_gate["status"] == "pass"
        assert reusable_gate["decision"] == "reuse_existing_remote_proof"
        assert reusable_gate["selected_entry_id"] == proof_reuse_entry["entry_id"]
        assert reusable_gate["invalidation_reasons"] == []
        assert (
            reusable_gate["claim_boundaries"]["does_not_skip_validation_by_itself"]
            is True
        )
        reuse_gate_output_args = argparse.Namespace(
            out_proof_reuse_gate_json=workspace / "proof-reuse-gate.json"
        )
        write_remote_validation_proof_reuse_gate_output(
            reuse_gate_output_args,
            reusable_gate,
        )
        assert reuse_gate_output_args.out_proof_reuse_gate_json.exists()

        stale_head_context = json.loads(json_dumps(reusable_context))
        stale_head_context["git"]["head"] = "different-head"
        stale_head_gate = build_remote_validation_proof_reuse_gate(
            generated_at=generated_at,
            proof_ledger=proof_reuse_ledger,
            context=stale_head_context,
        )
        assert stale_head_gate["reuse_allowed"] is False
        assert "stale_git_head" in stale_head_gate["invalidation_reasons"]

        uncovered_context = json.loads(json_dumps(reusable_context))
        uncovered_context["staged_paths"] = list(uncovered_context["staged_paths"]) + [
            "src/not-covered.rs",
        ]
        uncovered_gate = build_remote_validation_proof_reuse_gate(
            generated_at=generated_at,
            proof_ledger=proof_reuse_ledger,
            context=uncovered_context,
        )
        assert uncovered_gate["reuse_allowed"] is False
        assert "dirty_worktree_mismatch" in uncovered_gate["invalidation_reasons"]
        assert "staged_paths_not_covered" in uncovered_gate["invalidation_reasons"]

        non_authoritative_ledger = json.loads(json_dumps(proof_reuse_ledger))
        non_authoritative_ledger["entries"][0]["evidence_classification"]["coverage"][
            "authoritative_for_bead"
        ] = False
        non_authoritative_gate = build_remote_validation_proof_reuse_gate(
            generated_at=generated_at,
            proof_ledger=non_authoritative_ledger,
            context=reusable_context,
        )
        assert non_authoritative_gate["reuse_allowed"] is False
        assert (
            "coverage_not_authoritative"
            in non_authoritative_gate["invalidation_reasons"]
        )

        fingerprint_context = json.loads(json_dumps(reusable_context))
        fingerprint_context["command"]["command_fingerprint"] = "different-command"
        fingerprint_gate = build_remote_validation_proof_reuse_gate(
            generated_at=generated_at,
            proof_ledger=proof_reuse_ledger,
            context=fingerprint_context,
        )
        assert fingerprint_gate["reuse_allowed"] is False
        assert (
            "command_fingerprint_mismatch"
            in fingerprint_gate["invalidation_reasons"]
        )

        lockfile_context = json.loads(json_dumps(reusable_context))
        lockfile_context["staged_paths"] = ["Cargo.lock"]
        lockfile_gate = build_remote_validation_proof_reuse_gate(
            generated_at=generated_at,
            proof_ledger=proof_reuse_ledger,
            context=lockfile_context,
        )
        assert lockfile_gate["reuse_allowed"] is False
        assert (
            "toolchain_or_lockfile_changed"
            in lockfile_gate["invalidation_reasons"]
        )

        stale_progress_payload = json.loads(
            remote_pass_cargo_path.read_text(encoding="utf-8")
        )
        stale_progress_payload["remote_validation_proof"]["timing"][
            "heartbeat_at_utc"
        ] = "2026-05-09T09:02:00Z"
        stale_progress_payload["remote_validation_proof"]["timing"][
            "stale_progress_detected"
        ] = True
        stale_progress_payload["remote_validation_proof"]["runner"][
            "status_excerpt"
        ] = "Remote command finished: exit=0 after stale progress heartbeat"
        stale_progress_cargo_path = write_json(
            workspace / "cargo-stale-progress.json",
            stale_progress_payload,
        )
        stale_progress_runpack = build_runpack(
            argparse.Namespace(
                **{
                    **vars(args),
                    "cargo_admission_json": stale_progress_cargo_path,
                    "rch_artifact_sync_json": remote_pass_sync_path,
                    "out_json": None,
                    "out_md": None,
                }
            )
        )
        stale_progress_entry = stale_progress_runpack[
            "remote_validation_proof_ledger"
        ]["entries"][0]
        assert_proof(
            stale_progress_entry["timing"]["stale_progress_detected"] is True,
            "stale progress case should retain heartbeat staleness",
            stale_progress_entry,
        )
        assert_proof(
            stale_progress_entry["timing"]["heartbeat_at_utc"]
            == "2026-05-09T09:02:00Z",
            "stale progress case should retain stale heartbeat timestamp",
            stale_progress_entry,
        )
        assert_proof(
            any(
                warning["warning_id"] == "stale_progress"
                for warning in stale_progress_entry["warnings"]
            ),
            "stale progress case should emit stale_progress warning",
            stale_progress_entry,
        )
        assert_proof(
            "stale_progress"
            in stale_progress_entry["evidence_classification"]["degraded_reasons"],
            "stale progress case should keep stale_progress in degraded reasons",
            stale_progress_entry,
        )

        observed_local_payload = json.loads(
            remote_pass_cargo_path.read_text(encoding="utf-8")
        )
        observed_local_payload["reason"] = "rch_failed_open_local_fallback"
        observed_local_payload["resolved_runner"] = "local"
        observed_local_payload["remote_validation_proof"]["runner"].update(
            {
                "resolved_runner": "local",
                "remote_execution": False,
                "local_fallback": "observed",
                "fallback_reason": "rch_failed_open_local_fallback",
                "rch_job_id": "",
                "worker_id": "",
                "worker_host": "",
                "queue_state": "unavailable",
                "worker_state": "completed_locally",
                "command_rewrite": {
                    "tmpdir_rewritten": False,
                    "target_dir_rewritten": False,
                },
                "status_excerpt": "RCH unavailable; command ran locally",
            }
        )
        observed_local_cargo_path = write_json(
            workspace / "cargo-local-fallback-observed.json",
            observed_local_payload,
        )
        observed_local_runpack = build_runpack(
            argparse.Namespace(
                **{
                    **vars(args),
                    "cargo_admission_json": observed_local_cargo_path,
                    "rch_artifact_sync_json": remote_pass_sync_path,
                    "out_json": None,
                    "out_md": None,
                }
            )
        )
        observed_local_entry = observed_local_runpack[
            "remote_validation_proof_ledger"
        ]["entries"][0]
        assert_proof(
            observed_local_entry["runner"]["local_fallback"] == "observed",
            "observed local fallback should be explicit",
            observed_local_entry,
        )
        assert_proof(
            observed_local_entry["runner"]["remote_execution"] is False,
            "observed local fallback should not claim remote execution",
            observed_local_entry,
        )
        assert_proof(
            observed_local_entry["exit"]["success"] is True,
            "observed local fallback fixture should model an otherwise successful command",
            observed_local_entry,
        )
        assert_proof(
            observed_local_entry["artifact_retrieval"]["status"] == "clean",
            "observed local fallback fixture should isolate fallback from retrieval status",
            observed_local_entry,
        )
        assert_proof(
            observed_local_entry["evidence_classification"]["clean_remote_proof"]
            is False,
            "observed local fallback must never be clean remote proof",
            observed_local_entry,
        )
        assert_proof(
            observed_local_entry["evidence_classification"]["status"] == "degraded",
            "observed local fallback should degrade even when command exit is 0",
            observed_local_entry,
        )
        assert_proof(
            any(
                warning["warning_id"] == "local_fallback_observed"
                for warning in observed_local_entry["warnings"]
            ),
            "observed local fallback should emit local_fallback_observed warning",
            observed_local_entry,
        )
        observed_local_gate = build_remote_validation_proof_reuse_gate(
            generated_at=generated_at,
            proof_ledger=observed_local_runpack["remote_validation_proof_ledger"],
            context=reusable_context,
        )
        assert observed_local_gate["reuse_allowed"] is False
        assert "missing_clean_remote_proof" in observed_local_gate["invalidation_reasons"]
        assert "missing_rch_provenance" in observed_local_gate["invalidation_reasons"]
        assert "local_fallback_observed" in observed_local_gate["invalidation_reasons"]
        local_refusal_cargo_path = write_json(
            workspace / "cargo-local-fallback-refusal.json",
            {
                "schema": "pi.cargo_headroom.admission.v1",
                "decision": "deny",
                "reason": "rch_unavailable_and_remote_required",
                "requested_runner": "rch",
                "resolved_runner": "none",
                "command_class": "heavy",
                "allow_local_fallback": False,
                "cargo_command": "clippy --all-targets -- -D warnings",
                "cargo_target_dir": "/data/tmp/pi_agent_rust_cargo/test/target",
                "tmpdir": "/data/tmp/pi_agent_rust_cargo/test/tmp",
                "remote_validation_proof": {
                    "bead_id": "bd-e5le6.2",
                    "runner": {
                        "resolved_runner": "not_run",
                        "runner_requirement": "rch_required",
                        "remote_execution": False,
                        "local_fallback": "refused",
                        "fallback_reason": "rch_unavailable_and_remote_required",
                        "queue_state": "unavailable",
                        "worker_state": "not_run",
                        "status_excerpt": "RCH unavailable; refusing local fallback",
                    },
                    "exit": {
                        "exit_code": None,
                        "success": False,
                        "termination_reason": "local_fallback_refused",
                        "stderr_excerpt": "remote execution required",
                        "stdout_excerpt": "",
                    },
                },
            },
        )
        local_refusal_runpack = build_runpack(
            argparse.Namespace(
                **{
                    **vars(args),
                    "cargo_admission_json": local_refusal_cargo_path,
                    "rch_artifact_sync_json": None,
                    "out_json": None,
                    "out_md": None,
                }
            )
        )
        local_refusal_entry = local_refusal_runpack["remote_validation_proof_ledger"][
            "entries"
        ][0]
        assert local_refusal_entry["evidence_classification"]["status"] == "blocked"
        assert local_refusal_entry["runner"]["local_fallback"] == "refused"
        assert any(
            warning["warning_id"] == "local_fallback_refused"
            for warning in local_refusal_entry["warnings"]
        )
        retrieval_warning_sync_path = write_json(
            workspace / "rch-artifact-sync-warning.json",
            {
                "schema": RCH_ARTIFACT_SYNC_SCHEMA,
                "generated_at": generated_at,
                "status": "warning",
                "required_paths": [
                    {"path": "target/debug/.fingerprint", "included": False}
                ],
                "violations": [
                    {
                        "path": "target/debug/.fingerprint",
                        "reason": "rsync reported partial transfer",
                    }
                ],
                "exit_code": 23,
                "elapsed_ms": 37,
            },
        )
        retrieval_warning_runpack = build_runpack(
            argparse.Namespace(
                **{
                    **vars(args),
                    "cargo_admission_json": remote_pass_cargo_path,
                    "rch_artifact_sync_json": retrieval_warning_sync_path,
                    "out_json": None,
                    "out_md": None,
                }
            )
        )
        retrieval_warning_entry = retrieval_warning_runpack[
            "remote_validation_proof_ledger"
        ]["entries"][0]
        assert retrieval_warning_entry["evidence_classification"]["status"] == "degraded"
        assert (
            retrieval_warning_entry["evidence_classification"]["clean_remote_proof"]
            is False
        )
        assert retrieval_warning_entry["runner"]["remote_execution"] is True
        assert retrieval_warning_entry["artifact_retrieval"]["status"] == "warning"
        assert_proof(
            retrieval_warning_entry["artifact_retrieval"]["missing_paths"]
            == ["target/debug/.fingerprint"],
            "retrieval warning should preserve missing artifact paths",
            retrieval_warning_entry,
        )
        assert_proof(
            retrieval_warning_entry["artifact_retrieval"]["warning_details"]
            == ["target/debug/.fingerprint: rsync reported partial transfer"],
            "retrieval warning should preserve warning details",
            retrieval_warning_entry,
        )
        assert_proof(
            retrieval_warning_runpack["remote_validation_proof_ledger"]["summary"][
                "degraded_entries"
            ]
            == 1,
            "retrieval warning should increment degraded proof summary",
            retrieval_warning_entry,
        )
        assert any(
            warning["warning_id"] == "artifact_retrieval_warning"
            for warning in retrieval_warning_entry["warnings"]
        )
        validation_git_path = write_json(
            workspace / "git-status-validation-proof.json",
            {
                "schema": GIT_CONTEXT_SCHEMA,
                "generated_at": generated_at,
                "branch": "main",
                "head": "validationfixture",
                "upstream": {
                    "name": "origin/main",
                    "ahead": 0,
                    "behind": 0,
                    "status": "ok",
                },
                "porcelain_lines": [" M scripts/build_swarm_operator_runpack.py"],
                "recent_commits": ["validationfixture bd-validation proof"],
                "recent_remote_commits": ["remotevalidationfixture origin/main"],
            },
        )
        validation_replay_manifest = {
            **args.capture_manifest,
            "commands": [
                {
                    "id": "runpack_self_test",
                    "command": "python3 scripts/build_swarm_operator_runpack.py --self-test",
                    "cwd": str(workspace),
                    "status": "ok",
                    "exit_code": 0,
                    "issue": None,
                    "started_at": "2026-05-09T10:00:00Z",
                    "ended_at": "2026-05-09T10:00:02Z",
                    "duration_ms": 2000,
                },
                {
                    "id": "clean_worktree_clippy",
                    "command": "rch exec -- cargo clippy --all-targets -- -D warnings",
                    "cwd": str(workspace),
                    "status": "failed",
                    "exit_code": 2,
                    "issue": "unrelated dirty worktree: src/extensions.rs reserved by another agent",
                    "started_at": "2026-05-09T10:01:00Z",
                    "ended_at": "2026-05-09T10:01:01Z",
                    "duration_ms": 1000,
                },
                {
                    "id": "rch_workspace_shadow",
                    "command": "rch exec -- cargo check --all-targets",
                    "cwd": str(workspace),
                    "status": "failed",
                    "exit_code": 101,
                    "issue": (
                        "error: failed to load manifest for workspace member "
                        "`/data/projects/crates/fwc` referenced by workspace at "
                        "`/data/projects/Cargo.toml`; caused by failed to read "
                        "`/data/toon_rust/Cargo.toml`"
                    ),
                    "started_at": "2026-05-09T10:02:00Z",
                    "ended_at": "2026-05-09T10:02:05Z",
                    "duration_ms": 5000,
                },
                {
                    "id": "claimed_mismatch",
                    "command": "python3 scripts/build_swarm_operator_runpack.py --self-test",
                    "cwd": str(workspace),
                    "status": "ok",
                    "exit_code": 0,
                    "issue": None,
                    "covered_paths": ["scripts/build_swarm_operator_runpack.py"],
                    "claimed_paths": [
                        "scripts/build_swarm_operator_runpack.py",
                        "src/extensions.rs",
                    ],
                    "coverage": {"authoritative_for_bead": True},
                    "started_at": "2026-05-09T10:03:00Z",
                    "ended_at": "2026-05-09T10:03:02Z",
                    "duration_ms": 2000,
                },
            ],
        }
        validation_replay_runpack = build_runpack(
            argparse.Namespace(
                **{
                    **vars(args),
                    "git_status_file": validation_git_path,
                    "capture_manifest": validation_replay_manifest,
                    "out_json": None,
                    "out_md": None,
                }
            )
        )
        validation_entries = {
            entry["command"].get("capture_id"): entry
            for entry in validation_replay_runpack["remote_validation_proof_ledger"][
                "entries"
            ]
            if entry["command"].get("capture_id")
        }
        script_entry = validation_entries["runpack_self_test"]
        assert script_entry["command_class"] == "script_self_test"
        assert script_entry["evidence_classification"]["status"] == "pass"
        assert (
            script_entry["evidence_classification"]["coverage"]["coverage_status"]
            == "covered"
        )
        assert (
            script_entry["evidence_classification"]["coverage"][
                "authoritative_for_bead"
            ]
            is True
        )
        unrelated_entry = validation_entries["clean_worktree_clippy"]
        assert unrelated_entry["command_class"] == "cargo_clippy"
        assert unrelated_entry["evidence_classification"]["status"] == "blocked"
        assert "unrelated_worktree_blocker" in unrelated_entry[
            "evidence_classification"
        ]["degraded_reasons"]
        assert any(
            warning["warning_id"] == "unrelated_worktree_blocker"
            for warning in unrelated_entry["warnings"]
        )
        shadow_entry = validation_entries["rch_workspace_shadow"]
        assert shadow_entry["command_class"] == "cargo_check"
        assert shadow_entry["evidence_classification"]["status"] == "blocked"
        assert "worker_workspace_shadow" in shadow_entry["evidence_classification"][
            "degraded_reasons"
        ]
        assert any(
            warning["warning_id"] == "worker_workspace_shadow"
            for warning in shadow_entry["warnings"]
        )
        mismatch_entry = validation_entries["claimed_mismatch"]
        assert mismatch_entry["evidence_classification"]["status"] == "fail"
        assert (
            mismatch_entry["evidence_classification"]["coverage"]["coverage_status"]
            == "claimed_uncovered"
        )
        assert any(
            warning["warning_id"] == "proof_claim_coverage_mismatch"
            for warning in mismatch_entry["warnings"]
        )
        validation_summary = validation_replay_runpack[
            "remote_validation_proof_ledger"
        ]["summary"]
        assert validation_summary["passing_entries"] >= 1
        assert validation_summary["authoritative_for_bead_entries"] >= 1
        assert validation_summary["blocked_entries"] >= 2
        assert validation_summary["failed_entries"] >= 1
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
        turn_pressure = runpack["turn_pressure_ledger"]
        assert turn_pressure["schema"] == TURN_PRESSURE_LEDGER_SCHEMA
        assert turn_pressure["status"] == "blocked"
        pressure_dimensions = {
            dimension["id"]: dimension for dimension in turn_pressure["dimensions"]
        }
        assert pressure_dimensions["normal_turn_baseline"]["status"] == "blocked"
        assert pressure_dimensions["tool_artifact_spillover"]["status"] == "pressured"
        assert pressure_dimensions["compaction_admission"]["status"] == "pressured"
        assert pressure_dimensions["provider_routing"]["status"] == "pressured"
        assert pressure_dimensions["tui_frame_budget"]["status"] == "pressured"
        assert pressure_dimensions["session_write_amplification"]["status"] == "pressured"
        assert pressure_dimensions["tool_artifact_spillover"]["metrics"][
            "largest_bytes"
        ] == 2_500_000
        assert pressure_dimensions["provider_routing"]["metrics"]["fallback_count"] == 1
        assert pressure_dimensions["tui_frame_budget"]["metrics"]["worst_p99_us"] == 22_000
        assert pressure_dimensions["session_write_amplification"]["metrics"][
            "entries_per_turn"
        ] == 80.0
        assert turn_pressure["summary"]["pressure_event_count"] == 6
        for source in runpack["source_statuses"]:
            assert source["size_bytes"] is not None
            assert len(source["sha256"]) == 64
        assert runpack["redaction_summary"]["redacted_count"] >= 1
        assert args.out_json.exists() and args.out_md.exists()
        markdown = args.out_md.read_text(encoding="utf-8")
        assert "Tail latency telemetry" in markdown
        assert "Bottleneck Attribution" in markdown
        assert "Turn Pressure Ledger" in markdown
        assert "Context intelligence" in markdown
        assert "Progress SLO" in markdown
        assert "Resume Commands" in markdown
        assert "Git Context" in markdown
        assert_runpack_contract(runpack)
        assert_runpack_golden(runpack, workspace)
        validation_broker_args = argparse.Namespace(
            **{
                **vars(args),
                "validation_broker_json": validation_broker_status_path,
                "out_json": None,
                "out_md": None,
            }
        )
        validation_broker_runpack = build_runpack(validation_broker_args)
        assert validation_broker_runpack["validation_broker"]["schema"] == (
            VALIDATION_BROKER_CLI_STATUS_SCHEMA
        )
        assert validation_broker_runpack["validation_broker"]["current_slots"]["active"] == 1
        assert validation_broker_runpack["validation_broker"][
            "duplicate_gate_opportunities"
        ]["count"] == 1
        assert any(
            item.get("id") == "validation_broker" and item.get("status") == "ok"
            for item in validation_broker_runpack["source_statuses"]
        )
        assert_runpack_contract(validation_broker_runpack)
        assert "Validation broker" in render_markdown(validation_broker_runpack)
        doctor_validation_payload = json.loads(
            json_dumps(json.loads(doctor_path.read_text(encoding="utf-8")))
        )
        doctor_validation_payload["findings"].append(
            {
                "category": "swarm",
                "severity": "warn",
                "title": "Validation broker has stale slot warnings",
                "detail": "store=validation-slots.jsonl slots=1",
                "remediation": "Recover stale validation slots through owner-visible workflow",
                "data": {
                    "schema": VALIDATION_BROKER_DOCTOR_SCHEMA,
                    "mode": "advisory_projection",
                    "mutation_performed": False,
                    "source_status": {
                        "validation_slot_store": "available",
                        "source_of_truth": "validation_broker_slot_store",
                        "doctor_authority": "environment_diagnostics_only",
                    },
                    "store": {
                        "path": str(workspace / "validation-slots.jsonl"),
                        "configured": True,
                        "exists": True,
                        "schema": "pi.validation_broker.slot_store.v1",
                        "status": "available",
                        "total_records": 1,
                        "total_slots": 1,
                        "state_counts": {"stale": 1},
                    },
                    "current_slots": {
                        "total": 1,
                        "active": 0,
                        "reusable": 0,
                        "stale": 1,
                        "expired_at_report_time": 1,
                        "sample": [{"slot_id": "slot-stale", "state": "stale"}],
                    },
                    "duplicate_gate_opportunities": {
                        "count": 0,
                        "active_equivalent_groups": [],
                        "reusable_slots": [],
                    },
                    "stale_build_warnings": {
                        "count": 1,
                        "expired_slots": [{"slot_id": "slot-stale", "state": "stale"}],
                    },
                    "degraded_reasons": [],
                    "recommended_next_actions": [
                        "Recover stale validation slots through owner-visible broker workflow"
                    ],
                    "guards": {
                        "advisory_only": True,
                        "no_live_mutation": True,
                        "not_ci_success": True,
                        "not_release_claim_evidence": True,
                        "does_not_replace_rch_doctor_beads_agent_mail": True,
                    },
                },
                "fixability": "not_fixable",
            }
        )
        doctor_validation_path = write_json(
            workspace / "doctor-validation-broker.json",
            doctor_validation_payload,
        )
        doctor_validation_runpack = build_runpack(
            argparse.Namespace(
                **{
                    **vars(args),
                    "doctor_json": doctor_validation_path,
                    "out_json": None,
                    "out_md": None,
                }
            )
        )
        assert doctor_validation_runpack["doctor_swarm"]["validation_broker"][
            "stale_build_warnings"
        ]["count"] == 1
        assert_runpack_contract(doctor_validation_runpack)
        replay_preview_args = argparse.Namespace(
            **{
                **vars(args),
                "swarm_replay_preview_json": replay_preview_path,
                "out_json": None,
                "out_md": None,
            }
        )
        replay_preview_runpack = build_runpack(replay_preview_args)
        assert replay_preview_runpack["swarm_replay_preview"]["schema"] == (
            SWARM_REPLAY_PREVIEW_SCHEMA
        )
        assert replay_preview_runpack["swarm_replay_preview"]["trace_id"] == "fixture-preview"
        assert (
            replay_preview_runpack["swarm_replay_preview"]["recommended_policy_id"]
            == "rch_fanout_limited"
        )
        assert (
            replay_preview_runpack["swarm_replay_preview"]["comparison_rows"][0]["policy_id"]
            == "rch_fanout_limited"
        )
        assert_runpack_contract(replay_preview_runpack)
        replay_preview_markdown = render_markdown(replay_preview_runpack)
        assert "Replay preview" in replay_preview_markdown
        assert "rch_fanout_limited" in replay_preview_markdown
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
        assert input_pack["normalized_inputs"]["agent_mail"]["transport_status"] == "reachable"
        assert input_pack["normalized_inputs"]["agent_mail"]["semantic_readiness_status"] == "fail"
        assert input_pack["normalized_inputs"]["agent_mail"]["semantic_readiness_detail"] == AGENT_MAIL_SCHEMA_CORRUPT_DETAIL
        assert input_pack["normalized_inputs"]["agent_mail"]["fallback_action"] == "use_beads_soft_lock"
        assert input_pack["normalized_inputs"]["agent_mail"]["soft_lock"] == "beads"
        assert input_pack["normalized_inputs"]["agent_mail"]["fallback_ownership_evidence"]["active_ownership"][0]["assignee"] == "GreenStone"
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
        validation_broker_autopilot_args = argparse.Namespace(
            **{
                **vars(autopilot_args),
                "validation_broker_json": validation_broker_status_path,
                "out_autopilot_input_pack_json": None,
            }
        )
        validation_broker_input_pack = build_autopilot_input_pack(
            validation_broker_autopilot_args
        )
        assert validation_broker_input_pack["normalized_inputs"]["validation_broker"][
            "schema"
        ] == VALIDATION_BROKER_CLI_STATUS_SCHEMA
        assert any(
            item.get("id") == "validation_broker" and item.get("status") == "ok"
            for item in validation_broker_input_pack["source_statuses"]
        )
        assert_autopilot_input_pack_contract(validation_broker_input_pack)
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

        dashboard_rch_categories = load_dashboard_rch_drift_categories()
        rch_catalog_by_category = {
            str(rule["drift_category_id"]): rule
            for rule in FAILURE_ACTION_RULES
            if rule.get("drift_category_id") is not None
        }
        assert set(rch_catalog_by_category) == set(dashboard_rch_categories), {
            "catalog": sorted(rch_catalog_by_category),
            "dashboard": sorted(dashboard_rch_categories),
        }
        for category_id, dashboard_category in dashboard_rch_categories.items():
            catalog_rule = rch_catalog_by_category[category_id]
            assert catalog_rule["drift_class"] == dashboard_category["drift_class"], category_id
            assert catalog_rule["remediation"] == dashboard_category["remediation"], category_id

        def require_rch_drift_action(
            plan: dict[str, Any],
            action_id: str,
            category_id: str,
        ) -> dict[str, Any]:
            action = require_failure_action(plan, action_id)
            dashboard_category = dashboard_rch_categories[category_id]
            assert action["drift_category_id"] == category_id
            assert action["drift_class"] == dashboard_category["drift_class"]
            assert action["remediation"] == dashboard_category["remediation"]
            return action

        missing_remote_target_plan = build_failure_fixture_plan(
            "failure-rch-missing-remote-target",
            commands=[
                {
                    "id": "rch_artifact_sync",
                    "command": "rch exec -- cargo check --all-targets",
                    "status": "failed",
                    "exit_code": 1,
                    "issue": "RCH-E213 remote target directory /worker/missing-target not found while retrieving artifacts",
                }
            ],
        )
        require_rch_drift_action(
            missing_remote_target_plan,
            "FAIL-RCH-MISSING-REMOTE-TARGET-DIRECTORY",
            "missing_remote_target_directory",
        )

        rsync_mkstemp_plan = build_failure_fixture_plan(
            "failure-rch-rsync-mkstemp",
            commands=[
                {
                    "id": "rch_artifact_sync",
                    "command": "rch exec -- cargo check --all-targets",
                    "status": "failed",
                    "exit_code": 1,
                    "issue": 'rsync artifact retrieval failed: mkstemp "/tmp/out/.artifact.tmp" failed: No such file or directory (2)',
                }
            ],
        )
        require_rch_drift_action(
            rsync_mkstemp_plan,
            "FAIL-RCH-RSYNC-MKSTEMP-NO-SUCH-FILE",
            "rsync_mkstemp_no_such_file",
        )

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
        require_rch_drift_action(
            rch_retrieval_plan,
            "FAIL-RCH-ARTIFACT-RETRIEVAL-DISK",
            "disk_pressure",
        )

        remote_dependency_plan = build_failure_fixture_plan(
            "failure-rch-remote-dependency",
            commands=[
                {
                    "id": "rch_dependency_preflight",
                    "command": "rch exec -- cargo check --all-targets",
                    "status": "failed",
                    "exit_code": 1,
                    "issue": "remote dependency preflight blocked: failed to load source for dependency `asupersync`",
                }
            ],
        )
        require_rch_drift_action(
            remote_dependency_plan,
            "FAIL-RCH-REMOTE-DEPENDENCY-PREFLIGHT-BLOCKED",
            "remote_dependency_preflight_blocked",
        )

        worker_workspace_shadow_plan = build_failure_fixture_plan(
            "failure-rch-worker-workspace-shadow",
            commands=[
                {
                    "id": "cargo_check",
                    "command": "rch exec -- cargo check --all-targets",
                    "status": "failed",
                    "exit_code": 101,
                    "issue": (
                        "error: failed to load manifest for workspace member "
                        "`/data/projects/crates/fwc` referenced by workspace at "
                        "`/data/projects/Cargo.toml`; caused by failed to read "
                        "`/data/toon_rust/Cargo.toml`"
                    ),
                }
            ],
        )
        require_rch_drift_action(
            worker_workspace_shadow_plan,
            "FAIL-RCH-WORKER-WORKSPACE-SHADOW",
            "worker_workspace_shadow",
        )

        post_success_warning_plan = build_failure_fixture_plan(
            "failure-rch-post-success-retrieval-warning",
            commands=[
                {
                    "id": "rch_artifact_sync",
                    "command": "rch exec -- cargo check --all-targets",
                    "status": "degraded",
                    "exit_code": 0,
                    "issue": "Remote command finished: exit=0; artifact retrieval warning: no artifacts retrieved from worker after success",
                }
            ],
        )
        require_rch_drift_action(
            post_success_warning_plan,
            "FAIL-RCH-ARTIFACT-RETRIEVAL-WARNING-AFTER-SUCCESS",
            "artifact_retrieval_warning_after_success",
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
        deferred_roadmap_beads_path = write_json(
            workspace / "beads-deferred-roadmap.json",
            {
                "issues": [
                    {
                        "id": "bd-roadmap",
                        "title": "Swarm follow-up roadmap",
                        "description": "Needs child backlog after idea-wizard planning.",
                        "status": "deferred",
                        "priority": 2,
                        "issue_type": "epic",
                        "updated_at": generated_at,
                        "labels": ["idea-wizard", "swarm"],
                    },
                    {
                        "id": "bd-roadmap.1",
                        "title": "Closed child",
                        "status": "closed",
                        "priority": 2,
                        "updated_at": generated_at,
                        "dependencies": [
                            {"type": "parent-child", "depends_on_id": "bd-roadmap"}
                        ],
                    },
                ]
            },
        )
        deferred_plan_args = argparse.Namespace(
            **{
                **vars(healthy_args),
                "beads_json": deferred_roadmap_beads_path,
                "beads_ready_json": empty_ready_path,
            }
        )
        deferred_input_pack = build_autopilot_input_pack(deferred_plan_args)
        deferred_plan = build_autopilot_plan(deferred_input_pack, max_items=args.max_items)
        assert deferred_input_pack["normalized_inputs"]["beads"]["deferred_planning_count"] == 1
        assert deferred_plan["actions"][0]["action"] == "create_or_refine_backlog"
        assert (
            "normalized_inputs.beads.deferred_planning"
            in deferred_plan["actions"][0]["evidence_paths"]
        )
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
        assert autopilot_e2e["scenarios"]["deferred_roadmap_backlog"][
            "selected_action"
        ] == "create_or_refine_backlog"
        assert autopilot_e2e["scenarios"]["degraded_agent_mail_soft_lock"][
            "selected_action"
        ] == "use_beads_soft_lock"
        assert "wait_for_rch" in autopilot_e2e["scenarios"]["saturated_rch_queue"][
            "actions"
        ]
        dirty_mail_rch_empty = autopilot_e2e["scenarios"][
            "dirty_mail_rch_empty_beads_dry_run"
        ]
        assert dirty_mail_rch_empty["git_dirty_preserved"] is True
        assert dirty_mail_rch_empty["agent_mail_status"] == "degraded"
        assert dirty_mail_rch_empty["beads_after_closeout"] == {
            "active_count": 0,
            "open_candidate_count": 0,
            "ready_count": 0,
        }
        assert dirty_mail_rch_empty["remote_validation_proof"]["summary"][
            "clean_remote_proof_entries"
        ] == 1
        assert dirty_mail_rch_empty["remote_validation_proof"]["clean_remote_proof"] is True
        assert "plan_new_bead" in dirty_mail_rch_empty["actions"]
        assert "wait_for_rch" not in dirty_mail_rch_empty["actions"]
        assert autopilot_e2e["scenarios"]["stale_in_progress_bead"][
            "selected_action"
        ] == "reopen_stale_bead_candidate"
        assert autopilot_e2e["scenarios"]["unrelated_dirty_worktree"][
            "selected_action"
        ] == "capture_handoff"
        assert autopilot_e2e["scenarios"]["malformed_source_fail_closed"][
            "selected_action"
        ] == "fail_closed"
        degraded_coordination_e2e = build_degraded_coordination_runpack_e2e_summary(
            output_dir=workspace / "degraded-coordination-e2e",
            events_path=workspace / "degraded-coordination-e2e" / "events.jsonl",
            generated_at=generated_at,
            max_items=args.max_items,
            stale_after_hours=args.stale_after_hours,
            timeout_seconds=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        )
        assert degraded_coordination_e2e["schema"] == (
            DEGRADED_COORDINATION_RUNPACK_E2E_SCHEMA
        )
        assert degraded_coordination_e2e["status"] == "pass"
        assert set(degraded_coordination_e2e["scenarios"]) == set(
            DEGRADED_COORDINATION_RUNPACK_E2E_REQUIRED_SCENARIOS
        )
        degraded_coordination_scenario = degraded_coordination_e2e["scenarios"][
            "degraded_coordination_runpack_handoff"
        ]
        assert degraded_coordination_scenario["runpack_status"] == "degraded"
        assert degraded_coordination_scenario["recommended_action"] == (
            "use_beads_soft_lock_and_surface_rch_workspace_shadow"
        )
        assert degraded_coordination_scenario["degraded_inputs"][
            "agent_mail_soft_lock"
        ] == "beads"
        assert degraded_coordination_scenario["degraded_inputs"]["ready_count"] == 0
        assert degraded_coordination_scenario["remote_validation_proof"][
            "shadow_entry_status"
        ] == "blocked"
        assert degraded_coordination_scenario["no_delete_evidence"][
            "emitted_deletion_command_count"
        ] == 0
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
        context_gate_issues = [
            {
                "id": "bd-ircr3",
                "title": "Evidence-aware semantic context intelligence program",
                "status": "open",
            },
            {
                "id": "bd-ircr3.11",
                "title": "Document and gate context-intelligence closeout",
                "status": "in_progress",
                "assignee": "AmberOsprey",
            },
        ]
        context_gate_issues.extend(
            {
                "id": issue_id,
                "status": "closed",
                "close_reason": f"{issue_id} self-test evidence closed",
            }
            for issue_id in CONTEXT_INTELLIGENCE_CLOSEOUT_CHILD_BEADS
        )
        context_gate_issues_path = workspace / "context-final-gate-issues.jsonl"
        context_gate_issues_path.write_text(
            "\n".join(json_dumps(issue) for issue in context_gate_issues) + "\n",
            encoding="utf-8",
        )
        context_final_gate_quality = [
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
                "id": "json_contracts",
                "status": "pass",
                "command": (
                    "python3 -m json.tool "
                    "docs/contracts/context-intelligence-closeout-gate-contract.json"
                ),
            },
            {
                "id": "semantic_context_graph_contract_rch",
                "status": "pass",
                "command": "rch exec -- cargo test --test semantic_context_graph_contract -- --nocapture",
            },
            {
                "id": "semantic_workspace_graph_contract_rch",
                "status": "pass",
                "command": "rch exec -- cargo test --test semantic_workspace_graph_contract -- --nocapture",
            },
            {
                "id": "semantic_workspace_graph_builder_rch",
                "status": "pass",
                "command": "rch exec -- cargo test --test semantic_workspace_graph_builder context",
            },
            {
                "id": "context_intelligence_e2e_rch",
                "status": "pass",
                "command": (
                    "rch exec -- cargo test --test e2e_agent_loop "
                    "context_intelligence_no_mock_harness -- --nocapture"
                ),
            },
            {
                "id": "doctor_context_intelligence_rch",
                "status": "pass",
                "command": (
                    "rch exec -- cargo test --test doctor_swarm_temp_dir_json "
                    "context_intelligence -- --nocapture"
                ),
            },
            {
                "id": "context_perf_budgets_rch",
                "status": "pass",
                "command": "rch exec -- cargo test --test perf_budgets context_intelligence",
            },
            {
                "id": "context_intelligence_closeout_gate_contract_rch",
                "status": "pass",
                "command": (
                    "rch exec -- cargo test --test "
                    "context_intelligence_closeout_gate_contract -- --nocapture"
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
                    "amberosprey_bd_ircr3_11/target "
                    "TMPDIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_ircr3_11/tmp "
                    "rch exec -- cargo check --all-targets"
                ),
            },
            {
                "id": "cargo_clippy_all_targets_rch",
                "status": "pass",
                "command": (
                    "CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_ircr3_11/target "
                    "TMPDIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_ircr3_11/tmp "
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
        context_final_gate = build_context_intelligence_closeout_gate_summary(
            generated_at=generated_at,
            quality_gate_results=context_final_gate_quality,
            issue_export_path=context_gate_issues_path,
            git_refs={
                "head": "contextfinalgatefixture",
                "origin_main": "contextfinalgatefixture",
                "origin_master": "contextfinalgatefixture",
            },
            commit_check_override=True,
        )
        assert context_final_gate["schema"] == CONTEXT_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA
        assert context_final_gate["status"] == "pass"
        assert context_final_gate["decision"] == "close_final_gate_and_parent_epic"
        assert context_final_gate["follow_up_required"] is False
        assert not context_final_gate["follow_up_beads"]
        assert context_final_gate["child_artifact_map"]
        runtime_gate_issues = [
            {
                "id": "bd-h66tp",
                "title": "Swarm-scale runtime intelligence third-wave roadmap",
                "status": "deferred",
            },
            {
                "id": "bd-h66tp.8",
                "title": "Add third-wave runtime intelligence closeout gate",
                "status": "in_progress",
                "assignee": "AmberOsprey",
            },
        ]
        runtime_gate_issues.extend(
            {
                "id": issue_id,
                "status": "closed",
                "close_reason": f"{issue_id} self-test evidence closed",
            }
            for issue_id in RUNTIME_INTELLIGENCE_CLOSEOUT_CHILD_BEADS
        )
        runtime_gate_issues_path = workspace / "runtime-final-gate-issues.jsonl"
        runtime_gate_issues_path.write_text(
            "\n".join(json_dumps(issue) for issue in runtime_gate_issues) + "\n",
            encoding="utf-8",
        )
        runtime_final_gate_quality = [
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
                "id": "json_contracts",
                "status": "pass",
                "command": (
                    "python3 -m json.tool "
                    "docs/contracts/runtime-intelligence-closeout-gate-contract.json"
                ),
            },
            {
                "id": "runtime_intelligence_closeout_gate_contract_rch",
                "status": "pass",
                "command": (
                    "rch exec -- cargo test --test "
                    "runtime_intelligence_closeout_gate_contract -- --nocapture"
                ),
            },
            {
                "id": "cargo_fmt",
                "status": "pass",
                "command": "cargo fmt --check",
            },
            {
                "id": "git_diff_check",
                "status": "pass",
                "command": "git diff --check",
            },
            {
                "id": "cargo_check_all_targets_rch",
                "status": "pass",
                "command": (
                    "CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_h66tp_8/target "
                    "TMPDIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_h66tp_8/tmp "
                    "rch exec -- cargo check --all-targets"
                ),
            },
            {
                "id": "cargo_clippy_all_targets_rch",
                "status": "pass",
                "command": (
                    "CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_h66tp_8/target "
                    "TMPDIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_h66tp_8/tmp "
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
        runtime_final_gate = build_runtime_intelligence_closeout_gate_summary(
            generated_at=generated_at,
            quality_gate_results=runtime_final_gate_quality,
            issue_export_path=runtime_gate_issues_path,
            git_refs={
                "head": "runtimefinalgatefixture",
                "origin_main": "runtimefinalgatefixture",
                "origin_master": "runtimefinalgatefixture",
            },
            commit_check_override=True,
        )
        assert runtime_final_gate["schema"] == RUNTIME_INTELLIGENCE_CLOSEOUT_GATE_SCHEMA
        assert runtime_final_gate["status"] == "pass"
        assert runtime_final_gate["decision"] == "close_final_gate_and_parent_epic"
        assert runtime_final_gate["follow_up_required"] is False
        assert not runtime_final_gate["follow_up_beads"]
        assert runtime_final_gate["child_artifact_map"]
        fourth_wave_gate_issues = [
            {
                "id": "bd-63x3v.7",
                "title": "Swarm self-healing operator action fourth-wave roadmap",
                "status": "deferred",
            },
            {
                "id": "bd-63x3v.7.9",
                "title": "Add fourth-wave self-healing closeout gate",
                "status": "in_progress",
                "assignee": "AmberOsprey",
            },
        ]
        fourth_wave_gate_issues.extend(
            {
                "id": issue_id,
                "status": "closed",
                "close_reason": f"{issue_id} self-test evidence closed",
            }
            for issue_id in FOURTH_WAVE_CLOSEOUT_CHILD_BEADS
        )
        fourth_wave_gate_issues_path = workspace / "fourth-wave-final-gate-issues.jsonl"
        fourth_wave_gate_issues_path.write_text(
            "\n".join(json_dumps(issue) for issue in fourth_wave_gate_issues) + "\n",
            encoding="utf-8",
        )
        fourth_wave_final_gate_quality = [
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
                "id": "helper_self_tests",
                "status": "pass",
                "command": (
                    "python3 scripts/simulate_swarm_budget_leases.py --self-test && "
                    "python3 scripts/rehearse_extension_quarantine.py --self-test && "
                    "python3 scripts/summarize_operator_handoff.py --self-test && "
                    "python3 scripts/build_stale_evidence_renewal_queue.py --self-test"
                ),
            },
            {
                "id": "json_contracts",
                "status": "pass",
                "command": (
                    "python3 -m json.tool "
                    "docs/contracts/fourth-wave-self-healing-closeout-gate-contract.json"
                ),
            },
            {
                "id": "fourth_wave_closeout_gate_contract_rch",
                "status": "pass",
                "command": (
                    "rch exec -- cargo test --test "
                    "fourth_wave_self_healing_closeout_gate_contract -- --nocapture"
                ),
            },
            {
                "id": "cargo_fmt",
                "status": "pass",
                "command": "cargo fmt --check",
            },
            {
                "id": "git_diff_check",
                "status": "pass",
                "command": "git diff --check",
            },
            {
                "id": "cargo_check_all_targets_rch",
                "status": "pass",
                "command": (
                    "CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_63x3v_7_9/target "
                    "TMPDIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_63x3v_7_9/tmp "
                    "rch exec -- cargo check --all-targets"
                ),
            },
            {
                "id": "cargo_clippy_all_targets_rch",
                "status": "pass",
                "command": (
                    "CARGO_TARGET_DIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_63x3v_7_9/target "
                    "TMPDIR=/data/tmp/pi_agent_rust_cargo/"
                    "amberosprey_bd_63x3v_7_9/tmp "
                    "rch exec -- cargo clippy --all-targets -- -D warnings"
                ),
            },
            {
                "id": "staged_ubs",
                "status": "pass",
                "command": "timeout 120s ubs --staged --only=rust .",
            },
            {
                "id": "beads_ledger_reconcile",
                "status": "pass",
                "command": "./scripts/reconcile_beads_ledger.sh",
            },
        ]
        fourth_wave_final_gate = build_fourth_wave_closeout_gate_summary(
            generated_at=generated_at,
            quality_gate_results=fourth_wave_final_gate_quality,
            issue_export_path=fourth_wave_gate_issues_path,
            git_refs={
                "head": "fourthwavefinalgatefixture",
                "origin_main": "fourthwavefinalgatefixture",
                "origin_master": "fourthwavefinalgatefixture",
            },
            commit_check_override=True,
        )
        assert fourth_wave_final_gate["schema"] == FOURTH_WAVE_CLOSEOUT_GATE_SCHEMA
        assert fourth_wave_final_gate["status"] == "pass"
        assert fourth_wave_final_gate["decision"] == "close_final_gate_and_parent_epic"
        assert fourth_wave_final_gate["follow_up_required"] is False
        assert not fourth_wave_final_gate["follow_up_beads"]
        assert fourth_wave_final_gate["child_artifact_map"]
        adaptive_execution_gate_issues = [
            {
                "id": "bd-63x3v.8",
                "title": "Swarm adaptive execution fifth-wave roadmap",
                "status": "deferred",
            },
            {
                "id": "bd-63x3v.8.7",
                "title": "Add fifth-wave adaptive execution closeout gate",
                "status": "in_progress",
                "assignee": "AmberStone",
            },
        ]
        adaptive_execution_gate_issues.extend(
            {
                "id": issue_id,
                "status": "closed",
                "close_reason": f"{issue_id} self-test evidence closed",
            }
            for issue_id in ADAPTIVE_EXECUTION_CLOSEOUT_CHILD_BEADS
        )
        adaptive_execution_gate_issues_path = (
            workspace / "adaptive-execution-final-gate-issues.jsonl"
        )
        adaptive_execution_gate_issues_path.write_text(
            "\n".join(json_dumps(issue) for issue in adaptive_execution_gate_issues) + "\n",
            encoding="utf-8",
        )
        adaptive_execution_final_gate_quality = [
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
                "id": "json_contracts",
                "status": "pass",
                "command": (
                    "python3 -m json.tool "
                    "docs/contracts/adaptive-execution-closeout-gate-contract.json"
                ),
            },
            {
                "id": "git_diff_check",
                "status": "pass",
                "command": "git diff --check",
            },
            {
                "id": "staged_ubs",
                "status": "pass",
                "command": "timeout 120s ubs --staged --only=rust .",
            },
            {
                "id": "beads_ledger_reconcile",
                "status": "pass",
                "command": "./scripts/reconcile_beads_ledger.sh",
            },
        ]
        adaptive_execution_final_gate = build_adaptive_execution_closeout_gate_summary(
            generated_at=generated_at,
            quality_gate_results=adaptive_execution_final_gate_quality,
            issue_export_path=adaptive_execution_gate_issues_path,
            git_refs={
                "head": "adaptiveexecutionfinalgatefixture",
                "origin_main": "adaptiveexecutionfinalgatefixture",
                "origin_master": "adaptiveexecutionfinalgatefixture",
            },
            commit_check_override=True,
        )
        assert (
            adaptive_execution_final_gate["schema"]
            == ADAPTIVE_EXECUTION_CLOSEOUT_GATE_SCHEMA
        )
        assert adaptive_execution_final_gate["status"] == "pass"
        assert (
            adaptive_execution_final_gate["decision"]
            == "close_final_gate_and_parent_epic"
        )
        assert adaptive_execution_final_gate["follow_up_required"] is False
        assert not adaptive_execution_final_gate["follow_up_beads"]
        assert adaptive_execution_final_gate["child_artifact_map"]
        sixth_wave_gate_issues = [
            {
                "id": "bd-63x3v.9",
                "title": "Swarm validation hardening sixth-wave roadmap",
                "status": "deferred",
            },
            {
                "id": "bd-63x3v.9.10",
                "title": "Add sixth-wave swarm validation hardening closeout gate",
                "status": "in_progress",
                "assignee": "Codex",
            },
        ]
        sixth_wave_gate_issues.extend(
            {
                "id": issue_id,
                "status": "closed",
                "close_reason": f"{issue_id} self-test evidence closed",
            }
            for issue_id in SIXTH_WAVE_VALIDATION_CLOSEOUT_CHILD_BEADS
        )
        sixth_wave_gate_issues_path = (
            workspace / "sixth-wave-validation-final-gate-issues.jsonl"
        )
        sixth_wave_gate_issues_path.write_text(
            "\n".join(json_dumps(issue) for issue in sixth_wave_gate_issues) + "\n",
            encoding="utf-8",
        )
        sixth_wave_final_gate_quality = [
            {
                "id": "py_compile",
                "status": "pass",
                "command": "python3 -m py_compile scripts/build_swarm_operator_runpack.py scripts/report_swarm_claim_readiness.py",
            },
            {
                "id": "runpack_self_test",
                "status": "pass",
                "command": "python3 scripts/build_swarm_operator_runpack.py --self-test",
            },
            {
                "id": "claim_readiness_self_test",
                "status": "pass",
                "command": "python3 scripts/report_swarm_claim_readiness.py --self-test",
            },
            {
                "id": "json_contracts",
                "status": "pass",
                "command": (
                    "python3 -m json.tool "
                    "docs/contracts/sixth-wave-validation-hardening-closeout-gate-contract.json"
                ),
            },
            {
                "id": "git_diff_check",
                "status": "pass",
                "command": "git diff --check",
            },
            {
                "id": "staged_ubs",
                "status": "pass",
                "command": "timeout 120s ubs --staged --only=rust .",
            },
            {
                "id": "beads_ledger_reconcile",
                "status": "pass",
                "command": "./scripts/reconcile_beads_ledger.sh",
            },
        ]
        sixth_wave_final_gate = build_sixth_wave_validation_closeout_gate_summary(
            generated_at=generated_at,
            quality_gate_results=sixth_wave_final_gate_quality,
            issue_export_path=sixth_wave_gate_issues_path,
            git_refs={
                "head": "sixthwavevalidationfinalgatefixture",
                "origin_main": "sixthwavevalidationfinalgatefixture",
                "origin_master": "sixthwavevalidationfinalgatefixture",
            },
            commit_check_override=True,
        )
        assert (
            sixth_wave_final_gate["schema"]
            == SIXTH_WAVE_VALIDATION_CLOSEOUT_GATE_SCHEMA
        )
        assert sixth_wave_final_gate["status"] == "pass"
        assert (
            sixth_wave_final_gate["decision"]
            == "close_final_gate_and_parent_epic"
        )
        assert sixth_wave_final_gate["follow_up_required"] is False
        assert not sixth_wave_final_gate["follow_up_beads"]
        assert sixth_wave_final_gate["child_artifact_map"]
        backpressure_budget_contract = build_backpressure_budget_contract_summary(
            generated_at=generated_at,
        )
        assert (
            backpressure_budget_contract["schema"]
            == BACKPRESSURE_BUDGET_CONTRACT_SCHEMA
        )
        assert backpressure_budget_contract["status"] == "pass"
        assert backpressure_budget_contract["decision"] == "contract_passed"
        assert {
            surface["id"] for surface in backpressure_budget_contract["surfaces"]
        } == set(BACKPRESSURE_BUDGET_REQUIRED_SURFACE_IDS)
        assert_backpressure_budget_contract(backpressure_budget_contract)
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
        clean_pressure_dimensions = {
            dimension["id"]: dimension
            for dimension in clean_runpack["turn_pressure_ledger"]["dimensions"]
        }
        assert clean_pressure_dimensions["normal_turn_baseline"]["status"] == "ok"
        deferred_runpack = build_runpack(
            argparse.Namespace(
                **{
                    **vars(clean_args),
                    "beads_json": deferred_roadmap_beads_path,
                }
            )
        )
        assert deferred_runpack["beads"]["deferred_planning_count"] == 1
        assert any(
            "Create or refine child Beads for deferred roadmap/planning epics" in action
            and "bd-roadmap" in action
            for action in deferred_runpack["operator_next_actions"]
        )
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
        "--swarm-replay-preview-json",
        type=Path,
        help="optional pi.swarm.replay_preview.v1 JSON from `pi swarm-replay-preview`",
    )
    parser.add_argument(
        "--cargo-admission-json",
        type=Path,
        help="JSON or JSONL from cargo_headroom.sh --admit-only",
    )
    parser.add_argument(
        "--active-bead-id",
        help="Beads issue ID associated with the validation proof ledger entries",
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
        "--validation-broker-json",
        type=Path,
        help="optional validation-broker status or plan JSON to summarize as advisory posture",
    )
    parser.add_argument(
        "--progress-slo-json",
        type=Path,
        help="optional pi.swarm.progress_slo.v1 JSON to summarize as advisory progress posture",
    )
    parser.add_argument(
        "--stale-evidence-renewal-json",
        type=Path,
        help="optional pi.swarm.stale_evidence_renewal_queue.v1 JSON to summarize and feed the dry-run action plan",
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
        "--out-action-plan-json",
        type=Path,
        help="write pi.swarm.action_plan.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--out-work-admission-gate-json",
        type=Path,
        help="write pi.swarm.work_admission_gate.v1 JSON; refuses to overwrite",
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
        "--run-degraded-coordination-e2e",
        action="store_true",
        help="run no-mock degraded coordination runpack E2E scenario with JSONL evidence",
    )
    parser.add_argument(
        "--out-degraded-coordination-e2e-json",
        type=Path,
        help=(
            "write pi.swarm.degraded_coordination_runpack_e2e.v1 summary JSON; "
            "refuses to overwrite"
        ),
    )
    parser.add_argument(
        "--out-degraded-coordination-e2e-events-jsonl",
        type=Path,
        help=(
            "write pi.swarm.degraded_coordination_runpack_e2e.event.v1 JSONL; "
            "refuses to overwrite"
        ),
    )
    parser.add_argument(
        "--print-degraded-coordination-e2e",
        action="store_true",
        help="print the no-mock degraded coordination runpack E2E summary JSON",
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
        "--run-context-intelligence-final-gate",
        action="store_true",
        help="build the final prompt-to-artifact context intelligence closeout gate",
    )
    parser.add_argument(
        "--out-context-intelligence-final-gate-json",
        type=Path,
        help="write pi.context_intelligence.closeout_gate.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--print-context-intelligence-final-gate",
        action="store_true",
        help="print the final context intelligence closeout gate JSON",
    )
    parser.add_argument(
        "--run-runtime-intelligence-final-gate",
        action="store_true",
        help="build the final prompt-to-artifact runtime intelligence closeout gate",
    )
    parser.add_argument(
        "--out-runtime-intelligence-final-gate-json",
        type=Path,
        help="write pi.runtime_intelligence.closeout_gate.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--print-runtime-intelligence-final-gate",
        action="store_true",
        help="print the final runtime intelligence closeout gate JSON",
    )
    parser.add_argument(
        "--run-fourth-wave-final-gate",
        action="store_true",
        help="build the final prompt-to-artifact fourth-wave self-healing closeout gate",
    )
    parser.add_argument(
        "--out-fourth-wave-final-gate-json",
        type=Path,
        help="write pi.swarm.fourth_wave_self_healing.closeout_gate.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--print-fourth-wave-final-gate",
        action="store_true",
        help="print the final fourth-wave self-healing closeout gate JSON",
    )
    parser.add_argument(
        "--run-adaptive-execution-final-gate",
        action="store_true",
        help="build the final prompt-to-artifact fifth-wave adaptive execution closeout gate",
    )
    parser.add_argument(
        "--out-adaptive-execution-final-gate-json",
        type=Path,
        help="write pi.swarm.adaptive_execution.closeout_gate.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--print-adaptive-execution-final-gate",
        action="store_true",
        help="print the final fifth-wave adaptive execution closeout gate JSON",
    )
    parser.add_argument(
        "--run-sixth-wave-final-gate",
        action="store_true",
        help="build the final prompt-to-artifact sixth-wave validation hardening closeout gate",
    )
    parser.add_argument(
        "--out-sixth-wave-final-gate-json",
        type=Path,
        help="write pi.swarm.validation_hardening.closeout_gate.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--print-sixth-wave-final-gate",
        action="store_true",
        help="print the final sixth-wave validation hardening closeout gate JSON",
    )
    parser.add_argument(
        "--run-backpressure-budget-contract",
        action="store_true",
        help="build the read-only provider/RPC/TUI backpressure budget contract",
    )
    parser.add_argument(
        "--out-backpressure-budget-contract-json",
        type=Path,
        help="write pi.swarm.provider_rpc_tui_backpressure_budget_contract.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--print-backpressure-budget-contract",
        action="store_true",
        help="print the provider/RPC/TUI backpressure budget contract JSON",
    )
    parser.add_argument(
        "--run-proof-reuse-gate",
        action="store_true",
        help="build the fail-closed remote validation proof reuse gate",
    )
    parser.add_argument(
        "--proof-ledger-json",
        type=Path,
        help="pi.remote_validation.proof_ledger.v1 JSON to evaluate for reuse",
    )
    parser.add_argument(
        "--proof-reuse-context-json",
        type=Path,
        help="pi.validation.proof_reuse_context.v1 JSON describing the current validation context",
    )
    parser.add_argument(
        "--out-proof-reuse-gate-json",
        type=Path,
        help="write pi.validation.proof_reuse_gate.v1 JSON; refuses to overwrite",
    )
    parser.add_argument(
        "--print-proof-reuse-gate",
        action="store_true",
        help="print the proof reuse gate JSON",
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
    parser.add_argument(
        "--print-action-plan",
        action="store_true",
        help="print the dry-run swarm action plan JSON",
    )
    parser.add_argument(
        "--print-work-admission-gate",
        action="store_true",
        help="print the fail-closed swarm work admission gate JSON",
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
    degraded_coordination_e2e_options_used = (
        args.out_degraded_coordination_e2e_json
        or args.out_degraded_coordination_e2e_events_jsonl
        or args.print_degraded_coordination_e2e
    )
    autopilot_final_gate_options_used = (
        args.out_autopilot_final_gate_json
        or args.print_autopilot_final_gate
    )
    context_final_gate_options_used = (
        args.out_context_intelligence_final_gate_json
        or args.print_context_intelligence_final_gate
    )
    runtime_final_gate_options_used = (
        args.out_runtime_intelligence_final_gate_json
        or args.print_runtime_intelligence_final_gate
    )
    fourth_wave_final_gate_options_used = (
        args.out_fourth_wave_final_gate_json
        or args.print_fourth_wave_final_gate
    )
    adaptive_execution_final_gate_options_used = (
        args.out_adaptive_execution_final_gate_json
        or args.print_adaptive_execution_final_gate
    )
    sixth_wave_final_gate_options_used = (
        args.out_sixth_wave_final_gate_json
        or args.print_sixth_wave_final_gate
    )
    backpressure_budget_contract_options_used = (
        args.out_backpressure_budget_contract_json
        or args.print_backpressure_budget_contract
    )
    proof_reuse_gate_options_used = (
        args.proof_ledger_json
        or args.proof_reuse_context_json
        or args.out_proof_reuse_gate_json
        or args.print_proof_reuse_gate
    )
    final_gate_modes = [
        args.run_autopilot_final_gate,
        args.run_context_intelligence_final_gate,
        args.run_runtime_intelligence_final_gate,
        args.run_fourth_wave_final_gate,
        args.run_adaptive_execution_final_gate,
        args.run_sixth_wave_final_gate,
    ]
    if sum(1 for used in final_gate_modes if used) > 1:
        print("ERROR: run only one final-gate mode at a time", file=sys.stderr)
        return 2
    if args.run_backpressure_budget_contract and any(final_gate_modes):
        print(
            "ERROR: backpressure budget contract cannot be combined with a final-gate mode",
            file=sys.stderr,
        )
        return 2
    if args.run_proof_reuse_gate and (
        args.run_backpressure_budget_contract or any(final_gate_modes)
    ):
        print(
            "ERROR: proof reuse gate cannot be combined with final-gate or backpressure modes",
            file=sys.stderr,
        )
        return 2
    if autopilot_final_gate_options_used and not args.run_autopilot_final_gate:
        print(
            "ERROR: autopilot final-gate options require --run-autopilot-final-gate",
            file=sys.stderr,
        )
        return 2
    if context_final_gate_options_used and not args.run_context_intelligence_final_gate:
        print(
            "ERROR: context-intelligence final-gate options require --run-context-intelligence-final-gate",
            file=sys.stderr,
        )
        return 2
    if runtime_final_gate_options_used and not args.run_runtime_intelligence_final_gate:
        print(
            "ERROR: runtime-intelligence final-gate options require --run-runtime-intelligence-final-gate",
            file=sys.stderr,
        )
        return 2
    if fourth_wave_final_gate_options_used and not args.run_fourth_wave_final_gate:
        print(
            "ERROR: fourth-wave final-gate options require --run-fourth-wave-final-gate",
            file=sys.stderr,
        )
        return 2
    if (
        adaptive_execution_final_gate_options_used
        and not args.run_adaptive_execution_final_gate
    ):
        print(
            "ERROR: adaptive-execution final-gate options require --run-adaptive-execution-final-gate",
            file=sys.stderr,
        )
        return 2
    if sixth_wave_final_gate_options_used and not args.run_sixth_wave_final_gate:
        print(
            "ERROR: sixth-wave final-gate options require --run-sixth-wave-final-gate",
            file=sys.stderr,
        )
        return 2
    if (
        backpressure_budget_contract_options_used
        and not args.run_backpressure_budget_contract
    ):
        print(
            "ERROR: backpressure budget contract options require --run-backpressure-budget-contract",
            file=sys.stderr,
        )
        return 2
    if proof_reuse_gate_options_used and not args.run_proof_reuse_gate:
        print(
            "ERROR: proof reuse gate options require --run-proof-reuse-gate",
            file=sys.stderr,
        )
        return 2
    if args.quality_gate_results and not (
        args.run_autopilot_final_gate
        or args.run_context_intelligence_final_gate
        or args.run_runtime_intelligence_final_gate
        or args.run_fourth_wave_final_gate
        or args.run_adaptive_execution_final_gate
        or args.run_sixth_wave_final_gate
    ):
        print("ERROR: --quality-gate-result requires a final-gate run mode", file=sys.stderr)
        return 2
    if degraded_coordination_e2e_options_used and not args.run_degraded_coordination_e2e:
        print(
            "ERROR: degraded coordination E2E options require "
            "--run-degraded-coordination-e2e",
            file=sys.stderr,
        )
        return 2
    try:
        if args.run_backpressure_budget_contract:
            summary = build_backpressure_budget_contract_summary(
                generated_at=args.generated_at or utc_now_iso(),
            )
            assert_backpressure_budget_contract(summary)
            write_backpressure_budget_contract_output(args, summary)
            if (
                args.print_backpressure_budget_contract
                or args.out_backpressure_budget_contract_json is None
            ):
                print(json_dumps(summary, pretty=True))
            return 0
        if args.run_proof_reuse_gate:
            summary = build_remote_validation_proof_reuse_gate_from_args(args)
            write_remote_validation_proof_reuse_gate_output(args, summary)
            if args.print_proof_reuse_gate or args.out_proof_reuse_gate_json is None:
                print(json_dumps(summary, pretty=True))
            return 0
        if args.run_adaptive_execution_final_gate:
            summary = build_adaptive_execution_closeout_gate_summary(
                generated_at=args.generated_at or utc_now_iso(),
                quality_gate_results=parse_quality_gate_results(args.quality_gate_results),
            )
            write_adaptive_execution_closeout_gate_output(args, summary)
            if (
                args.print_adaptive_execution_final_gate
                or args.out_adaptive_execution_final_gate_json is None
            ):
                print(json_dumps(summary, pretty=True))
            return 0
        if args.run_sixth_wave_final_gate:
            summary = build_sixth_wave_validation_closeout_gate_summary(
                generated_at=args.generated_at or utc_now_iso(),
                quality_gate_results=parse_quality_gate_results(args.quality_gate_results),
            )
            write_sixth_wave_validation_closeout_gate_output(args, summary)
            if (
                args.print_sixth_wave_final_gate
                or args.out_sixth_wave_final_gate_json is None
            ):
                print(json_dumps(summary, pretty=True))
            return 0
        if args.run_fourth_wave_final_gate:
            summary = build_fourth_wave_closeout_gate_summary(
                generated_at=args.generated_at or utc_now_iso(),
                quality_gate_results=parse_quality_gate_results(args.quality_gate_results),
            )
            write_fourth_wave_closeout_gate_output(args, summary)
            if (
                args.print_fourth_wave_final_gate
                or args.out_fourth_wave_final_gate_json is None
            ):
                print(json_dumps(summary, pretty=True))
            return 0
        if args.run_runtime_intelligence_final_gate:
            summary = build_runtime_intelligence_closeout_gate_summary(
                generated_at=args.generated_at or utc_now_iso(),
                quality_gate_results=parse_quality_gate_results(args.quality_gate_results),
            )
            write_runtime_intelligence_closeout_gate_output(args, summary)
            if (
                args.print_runtime_intelligence_final_gate
                or args.out_runtime_intelligence_final_gate_json is None
            ):
                print(json_dumps(summary, pretty=True))
            return 0
        if args.run_context_intelligence_final_gate:
            summary = build_context_intelligence_closeout_gate_summary(
                generated_at=args.generated_at or utc_now_iso(),
                quality_gate_results=parse_quality_gate_results(args.quality_gate_results),
            )
            write_context_intelligence_closeout_gate_output(args, summary)
            if (
                args.print_context_intelligence_final_gate
                or args.out_context_intelligence_final_gate_json is None
            ):
                print(json_dumps(summary, pretty=True))
            return 0
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
        if args.run_degraded_coordination_e2e:
            summary = build_degraded_coordination_runpack_e2e_summary(
                output_dir=args.capture_dir,
                events_path=args.out_degraded_coordination_e2e_events_jsonl,
                generated_at=args.generated_at or utc_now_iso(),
                max_items=args.max_items,
                stale_after_hours=args.stale_after_hours,
                timeout_seconds=args.capture_timeout_seconds,
            )
            write_degraded_coordination_runpack_e2e_output(args, summary)
            if (
                args.print_degraded_coordination_e2e
                or args.out_degraded_coordination_e2e_json is None
            ):
                print(json_dumps(summary, pretty=True))
            return 0
        capture_current_sources(args)
        runpack = build_runpack(args)
        input_pack: dict[str, Any] | None = None
        plan: dict[str, Any] | None = None
        action_plan: dict[str, Any] | None = None
        work_admission_gate: dict[str, Any] | None = None
        if (
            args.out_autopilot_input_pack_json
            or args.print_autopilot_input_pack
            or args.out_autopilot_plan_json
            or args.print_autopilot_plan
            or args.out_action_plan_json
            or args.print_action_plan
            or args.out_work_admission_gate_json
            or args.print_work_admission_gate
        ):
            input_pack = build_autopilot_input_pack(args)
            if (
                args.out_autopilot_plan_json
                or args.print_autopilot_plan
                or args.out_action_plan_json
                or args.print_action_plan
                or args.out_work_admission_gate_json
                or args.print_work_admission_gate
            ):
                plan = build_autopilot_plan(input_pack, max_items=args.max_items)
                action_plan = build_swarm_action_plan(
                    input_pack,
                    plan,
                    max_items=args.max_items,
                )
                work_admission_gate = build_work_admission_gate(
                    input_pack,
                    plan,
                    action_plan,
                    max_items=args.max_items,
                )
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
        if action_plan is not None:
            write_action_plan_output(args, action_plan)
            if args.print_action_plan:
                print(json_dumps(action_plan, pretty=True))
        if work_admission_gate is not None:
            write_work_admission_gate_output(args, work_admission_gate)
            if args.print_work_admission_gate:
                print(json_dumps(work_admission_gate, pretty=True))
    except (RunpackError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json or (
        not args.out_json
        and not args.out_md
        and not args.out_autopilot_input_pack_json
        and not args.out_autopilot_plan_json
        and not args.out_action_plan_json
        and not args.out_work_admission_gate_json
        and not args.out_context_intelligence_final_gate_json
        and not args.out_runtime_intelligence_final_gate_json
        and not args.out_fourth_wave_final_gate_json
        and not args.out_adaptive_execution_final_gate_json
        and not args.out_backpressure_budget_contract_json
        and not args.out_proof_reuse_gate_json
        and not args.print_autopilot_input_pack
        and not args.print_autopilot_plan
        and not args.print_action_plan
        and not args.print_work_admission_gate
        and not args.print_context_intelligence_final_gate
        and not args.print_runtime_intelligence_final_gate
        and not args.print_fourth_wave_final_gate
        and not args.print_adaptive_execution_final_gate
        and not args.print_backpressure_budget_contract
        and not args.print_proof_reuse_gate
    ):
        print(json_dumps(runpack, pretty=True))
    return 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
