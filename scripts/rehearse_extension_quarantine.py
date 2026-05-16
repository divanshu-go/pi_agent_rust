#!/usr/bin/env python3
"""Rehearse extension crash quarantine and rollback decisions.

This tool is a deterministic, no-network rehearsal surface. It reads fixture or
operator-supplied extension failure evidence and emits guidance only; it never
edits extension configuration, removes files, calls the network, or reserves
work in Beads or Agent Mail.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INPUT_SCHEMA = "pi.extension.quarantine_rehearsal_input.v1"
OUTPUT_SCHEMA = "pi.extension.quarantine_rehearsal.v1"
CONTRACT_SCHEMA = "pi.extension.quarantine_rehearsal_contract.v1"
FIXTURE_SCHEMA = "pi.extension.quarantine_rehearsal_fixtures.v1"
ACTION_PLAN_SCHEMA = "pi.swarm.action_plan.v1"
CONTRACT_PATH = Path("docs/contracts/extension-quarantine-rehearsal-contract.json")
FIXTURE_PATH = Path("tests/fixtures/extension_quarantine_rehearsal/scenarios.json")
DEFAULT_FAILURE_THRESHOLD = 3
EVENT_KINDS = ("startup_failure", "runtime_failure", "permission_drift")
STATUSES = (
    "quarantine_recommended",
    "rollback_recommended",
    "permission_escalation_rejected",
    "blocked",
)
DECISIONS = (
    "quarantine",
    "rollback",
    "reject_permission_change",
    "pause_escalate",
)
REASON_CODES = (
    "startup_crash_loop",
    "runtime_crash_loop",
    "permission_escalation_rejected",
    "stale_provenance",
    "missing_provenance",
    "missing_policy",
    "rollback_candidate_selected",
    "rollback_candidate_untrusted",
    "action_plan_projection",
)
SAFETY_CLASSES = (
    "read_only_probe",
    "evidence_capture",
    "beads_mutation_requires_operator",
    "manual_config_change_requires_operator",
)
FORBIDDEN_ACTIONS = (
    "automatic extension disable",
    "automatic rollback",
    "automatic config edit",
    "file deletion",
    "network fetch",
)


class RehearsalError(Exception):
    """Raised when rehearsal input, fixtures, or contracts are not usable."""


def json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RehearsalError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RehearsalError(f"malformed JSON file {path}: {exc}") from exc


def as_object(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RehearsalError(f"{field} must be an object")
    return value


def as_list(value: Any, *, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RehearsalError(f"{field} must be an array")
    return value


def as_string_list(value: Any, *, field: str) -> list[str]:
    return [str(item) for item in as_list(value, field=field) if str(item)]


def as_non_negative_int(value: Any, *, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise RehearsalError(f"{field} must be an integer, not bool")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise RehearsalError(f"{field} must be a non-negative integer")
    if result < 0:
        raise RehearsalError(f"{field} must be non-negative")
    return result


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RehearsalError("input payload must be an object")
    schema = payload.get("schema")
    if schema not in (None, INPUT_SCHEMA):
        raise RehearsalError(f"unsupported input schema: {schema}")

    extension = as_object(payload.get("extension"), field="extension")
    extension_id = str(extension.get("id") or "").strip()
    if not extension_id:
        raise RehearsalError("extension.id is required")

    event = as_object(payload.get("event"), field="event")
    event_kind = str(event.get("kind") or "").strip()
    if event_kind not in EVENT_KINDS:
        raise RehearsalError(
            f"event.kind must be one of: {', '.join(EVENT_KINDS)}"
        )

    provenance = as_object(payload.get("provenance"), field="provenance")
    policy = as_object(payload.get("policy"), field="policy")
    threshold = as_non_negative_int(
        policy.get("quarantine_threshold", DEFAULT_FAILURE_THRESHOLD),
        field="policy.quarantine_threshold",
    )
    if threshold == 0:
        threshold = DEFAULT_FAILURE_THRESHOLD

    return {
        "schema": INPUT_SCHEMA,
        "scenario_id": str(payload.get("scenario_id") or extension_id),
        "extension": {
            "id": extension_id,
            "version": str(extension.get("version") or "unknown"),
            "manifest_path": str(extension.get("manifest_path") or ""),
            "config_path": str(extension.get("config_path") or ""),
        },
        "event": {
            "kind": event_kind,
            "failures_24h": as_non_negative_int(
                event.get("failures_24h"), field="event.failures_24h"
            ),
            "startup_failures": as_non_negative_int(
                event.get("startup_failures"), field="event.startup_failures"
            ),
            "runtime_failures": as_non_negative_int(
                event.get("runtime_failures"), field="event.runtime_failures"
            ),
            "requested_permissions": sorted(
                set(
                    as_string_list(
                        event.get("requested_permissions"),
                        field="event.requested_permissions",
                    )
                )
            ),
            "last_error": str(event.get("last_error") or ""),
        },
        "provenance": {
            "status": str(provenance.get("status") or "missing"),
            "evidence_paths": sorted(
                set(
                    as_string_list(
                        provenance.get("evidence_paths"),
                        field="provenance.evidence_paths",
                    )
                )
            ),
            "generated_at": str(provenance.get("generated_at") or ""),
            "manifest_hash": str(provenance.get("manifest_hash") or ""),
        },
        "policy": {
            "status": str(policy.get("status") or "missing"),
            "allowed_permissions": sorted(
                set(
                    as_string_list(
                        policy.get("allowed_permissions"),
                        field="policy.allowed_permissions",
                    )
                )
            ),
            "quarantine_threshold": threshold,
        },
        "rollback_candidates": normalize_rollback_candidates(
            payload.get("rollback_candidates")
        ),
    }


def normalize_rollback_candidates(value: Any) -> list[dict[str, Any]]:
    candidates = []
    for index, item in enumerate(as_list(value, field="rollback_candidates")):
        candidate = as_object(item, field=f"rollback_candidates[{index}]")
        candidates.append(
            {
                "version": str(candidate.get("version") or f"candidate-{index + 1}"),
                "artifact_path": str(candidate.get("artifact_path") or ""),
                "provenance_status": str(candidate.get("provenance_status") or "missing"),
                "policy_status": str(candidate.get("policy_status") or "missing"),
                "compatibility": str(candidate.get("compatibility") or "unknown"),
                "rank": as_non_negative_int(candidate.get("rank", index + 1), field=f"rollback_candidates[{index}].rank"),
            }
        )
    return sorted(
        candidates,
        key=lambda item: (
            item["rank"],
            item["version"],
            item["artifact_path"],
        ),
    )


def choose_rollback_candidate(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    rejected = []
    for candidate in candidates:
        trusted = (
            candidate["provenance_status"] == "present"
            and candidate["policy_status"] == "present"
            and candidate["compatibility"] == "compatible"
            and bool(candidate["artifact_path"])
        )
        if trusted:
            return candidate, rejected
        rejected.append(candidate["version"])
    return None, rejected


def base_operator_commands(extension_id: str, scenario_id: str) -> list[dict[str, Any]]:
    fixture_arg = shlex.quote(scenario_id)
    bead_title = shlex.quote(
        f"Investigate extension {extension_id} quarantine rehearsal"
    )
    return [
        {
            "id": "rerun_rehearsal",
            "command": (
                "python3 scripts/rehearse_extension_quarantine.py "
                f"--fixture-id {fixture_arg} --json"
            ),
            "safety_class": "read_only_probe",
            "mutates_state": False,
            "requires_confirmation": False,
            "rationale": "Replays the no-network fixture and prints the same deterministic guidance.",
        },
        {
            "id": "inspect_extension_runtime_diff",
            "command": "git diff -- src/extensions.rs src/extensions_js.rs src/extension_dispatcher.rs",
            "safety_class": "read_only_probe",
            "mutates_state": False,
            "requires_confirmation": False,
            "rationale": "Shows local runtime changes without modifying configuration or evidence.",
        },
        {
            "id": "file_followup_bead",
            "command": (
                "br create --title "
                f"{bead_title} "
                "--type bug --priority 1"
            ),
            "safety_class": "beads_mutation_requires_operator",
            "mutates_state": True,
            "requires_confirmation": True,
            "rationale": "Creates tracking only after an operator accepts the rehearsal guidance.",
        },
    ]


def build_action_plan_projection(
    *,
    status: str,
    decision: str,
    reason_codes: list[str],
    evidence_paths: list[str],
    operator_commands: list[dict[str, Any]],
) -> dict[str, Any]:
    action_status = "blocked" if status == "blocked" else "degraded"
    command_classes = sorted({command["safety_class"] for command in operator_commands})
    return {
        "schema": ACTION_PLAN_SCHEMA,
        "status": action_status,
        "purpose": "extension_quarantine_rehearsal_action_plan_projection",
        "source_plan_schema": OUTPUT_SCHEMA,
        "input_pack_schema": INPUT_SCHEMA,
        "input_pack_status": "rehearsed",
        "next_safest_action": {
            "decision": "pause_or_surface_blocker",
            "source_action": decision,
            "rank": 1,
            "title": "Surface extension quarantine rehearsal guidance",
            "severity": "high" if status == "blocked" else "medium",
            "confidence": "high",
            "rationale": "Extension quarantine and rollback rehearsals are advisory only and require operator confirmation before runtime or config changes.",
            "evidence_paths": evidence_paths,
            "command_safety_classes": command_classes,
            "commands_require_operator_execution": True,
        },
        "operator_actions": operator_commands,
        "failure_actions": [
            {
                "id": "extension_rehearsal_fail_closed",
                "reason_codes": reason_codes,
                "decision": "pause_or_surface_blocker",
                "rationale": "Do not start automatic quarantine, rollback, or permission changes from rehearsal output.",
            }
        ],
        "source_statuses": {
            "extension_rehearsal": status,
        },
        "source_classification": {
            "extension_rehearsal": "advisory_evidence",
        },
        "degraded_reasons": reason_codes,
        "forbidden_actions": list(FORBIDDEN_ACTIONS),
        "planner_guards": {
            "dry_run_only": True,
            "no_source_mutation": True,
            "commands_require_operator_execution": True,
            "dangerous_runnable_commands_blocked": True,
            "output_overwrite_refusal": True,
        },
        "redaction_summary": {
            "secrets_redacted": 0,
            "sensitive_fields_omitted": [
                "extension runtime stderr",
                "provider prompts",
                "environment tokens",
            ],
        },
    }


def evaluate(payload: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    normalized = normalize_payload(payload)
    extension_id = normalized["extension"]["id"]
    scenario_id = normalized["scenario_id"]
    event = normalized["event"]
    provenance = normalized["provenance"]
    policy = normalized["policy"]
    evidence_paths = provenance["evidence_paths"][:]
    reason_codes: list[str] = []
    rejected_rollback_candidates: list[str] = []
    selected_rollback, rejected_rollback_candidates = choose_rollback_candidate(
        normalized["rollback_candidates"]
    )

    if provenance["status"] == "missing":
        status = "blocked"
        decision = "pause_escalate"
        reason_codes.append("missing_provenance")
    elif policy["status"] == "missing":
        status = "blocked"
        decision = "pause_escalate"
        reason_codes.append("missing_policy")
    elif provenance["status"] == "stale":
        status = "blocked"
        decision = "pause_escalate"
        reason_codes.append("stale_provenance")
    else:
        requested_permissions = set(event["requested_permissions"])
        allowed_permissions = set(policy["allowed_permissions"])
        permission_drift = bool(requested_permissions - allowed_permissions)
        if event["kind"] == "permission_drift" and permission_drift:
            status = "permission_escalation_rejected"
            decision = "reject_permission_change"
            reason_codes.append("permission_escalation_rejected")
        elif selected_rollback is not None and event["kind"] == "runtime_failure":
            status = "rollback_recommended"
            decision = "rollback"
            reason_codes.extend(["runtime_crash_loop", "rollback_candidate_selected"])
        elif event["kind"] == "runtime_failure":
            status = "quarantine_recommended"
            decision = "quarantine"
            reason_codes.append("runtime_crash_loop")
            if normalized["rollback_candidates"]:
                reason_codes.append("rollback_candidate_untrusted")
        elif event["kind"] == "startup_failure":
            status = "quarantine_recommended"
            decision = "quarantine"
            reason_codes.append("startup_crash_loop")
        else:
            status = "blocked"
            decision = "pause_escalate"
            reason_codes.append("missing_policy")

    if selected_rollback is None and rejected_rollback_candidates:
        reason_codes.append("rollback_candidate_untrusted")
    reason_codes.append("action_plan_projection")
    reason_codes = sorted(set(reason_codes), key=REASON_CODES.index)

    operator_commands = base_operator_commands(extension_id, scenario_id)
    if decision in ("quarantine", "rollback", "reject_permission_change"):
        operator_commands.append(
            {
                "id": "manual_extension_config_change",
                "command": "manual operator confirmation required; no command emitted",
                "safety_class": "manual_config_change_requires_operator",
                "mutates_state": True,
                "requires_confirmation": True,
                "rationale": "Runtime extension changes are intentionally not executable from this rehearsal artifact.",
            }
        )

    observations = build_observations(normalized, selected_rollback, rejected_rollback_candidates)
    action_plan_projection = build_action_plan_projection(
        status=status,
        decision=decision,
        reason_codes=reason_codes,
        evidence_paths=evidence_paths,
        operator_commands=operator_commands,
    )

    return {
        "schema": OUTPUT_SCHEMA,
        "generated_at": generated_at,
        "status": status,
        "decision": decision,
        "purpose": "no_network_extension_quarantine_rehearsal",
        "extension": normalized["extension"],
        "event": event,
        "reason_codes": reason_codes,
        "guardrails": {
            "dry_run_only": True,
            "no_network": True,
            "no_source_mutation": True,
            "no_config_mutation": True,
            "no_file_deletion": True,
            "commands_require_operator_execution": True,
            "fail_closed_on_missing_policy_or_provenance": True,
        },
        "provenance": provenance,
        "policy": policy,
        "observations": observations,
        "rollback_selection": {
            "selected": selected_rollback,
            "rejected_versions": rejected_rollback_candidates,
        },
        "recommendations": build_recommendations(
            decision=decision,
            extension_id=extension_id,
            reason_codes=reason_codes,
            evidence_paths=evidence_paths,
        ),
        "operator_commands": operator_commands,
        "action_plan_integration": {
            "compatible": True,
            "action_plan_schema": ACTION_PLAN_SCHEMA,
            "suggested_decision": action_plan_projection["next_safest_action"]["decision"],
            "projection": action_plan_projection,
        },
        "authority_boundary": {
            "does_not_replace": [
                "extension capability policy",
                "extension conformance tests",
                "operator approval",
                "Beads",
                "Agent Mail",
                "git",
                "CI",
            ],
            "may_recommend_only": True,
            "must_not_execute_commands": True,
            "must_refuse_output_overwrite": True,
        },
    }


def build_observations(
    payload: dict[str, Any],
    selected_rollback: dict[str, Any] | None,
    rejected_rollback_candidates: list[str],
) -> list[dict[str, Any]]:
    event = payload["event"]
    policy = payload["policy"]
    observations = [
        {
            "id": "failure_window",
            "kind": event["kind"],
            "failures_24h": event["failures_24h"],
            "startup_failures": event["startup_failures"],
            "runtime_failures": event["runtime_failures"],
            "threshold": policy["quarantine_threshold"],
        }
    ]
    if event["requested_permissions"]:
        observations.append(
            {
                "id": "permission_delta",
                "requested": event["requested_permissions"],
                "allowed": policy["allowed_permissions"],
                "unauthorized": sorted(
                    set(event["requested_permissions"]) - set(policy["allowed_permissions"])
                ),
            }
        )
    if selected_rollback is not None:
        observations.append(
            {
                "id": "rollback_candidate",
                "selected_version": selected_rollback["version"],
                "artifact_path": selected_rollback["artifact_path"],
            }
        )
    if rejected_rollback_candidates:
        observations.append(
            {
                "id": "rejected_rollback_candidates",
                "versions": rejected_rollback_candidates,
            }
        )
    return observations


def build_recommendations(
    *,
    decision: str,
    extension_id: str,
    reason_codes: list[str],
    evidence_paths: list[str],
) -> list[dict[str, Any]]:
    title_by_decision = {
        "quarantine": "Recommend quarantine after repeated extension failures",
        "rollback": "Recommend rollback to trusted extension candidate",
        "reject_permission_change": "Reject extension permission escalation",
        "pause_escalate": "Pause until provenance and policy evidence are usable",
    }
    return [
        {
            "id": f"{extension_id}:{decision}",
            "decision": decision,
            "title": title_by_decision[decision],
            "reason_codes": reason_codes,
            "evidence_paths": evidence_paths,
            "rationale": (
                "This rehearsal emits operator guidance only; apply runtime or "
                "configuration changes through the normal reviewed implementation path."
            ),
        }
    ]


def load_fixture(fixture_id: str, *, repo_root: Path) -> dict[str, Any]:
    fixture = load_json(repo_root / FIXTURE_PATH)
    scenarios = fixture.get("scenarios")
    if fixture.get("schema") != FIXTURE_SCHEMA or not isinstance(scenarios, list):
        raise RehearsalError(f"invalid fixture file: {FIXTURE_PATH}")
    for scenario in scenarios:
        if isinstance(scenario, dict) and scenario.get("id") == fixture_id:
            input_payload = scenario.get("input")
            if not isinstance(input_payload, dict):
                raise RehearsalError(f"fixture {fixture_id} has invalid input")
            return input_payload
    known = ", ".join(sorted(str(item.get("id")) for item in scenarios if isinstance(item, dict)))
    raise RehearsalError(f"unknown fixture id {fixture_id!r}; known fixtures: {known}")


def assert_contract(output: dict[str, Any], *, repo_root: Path) -> None:
    contract = load_json(repo_root / CONTRACT_PATH)
    if contract.get("schema") != CONTRACT_SCHEMA:
        raise RehearsalError(f"invalid contract schema in {CONTRACT_PATH}")
    for key in contract["required_top_level_keys"]:
        if key not in output:
            raise RehearsalError(f"contract violation: missing output key {key}")
    if output["status"] not in contract["allowed_statuses"]:
        raise RehearsalError(f"contract violation: unsupported status {output['status']}")
    if output["decision"] not in contract["allowed_decisions"]:
        raise RehearsalError(f"contract violation: unsupported decision {output['decision']}")
    for reason in output["reason_codes"]:
        if reason not in contract["reason_codes"]:
            raise RehearsalError(f"contract violation: unsupported reason code {reason}")
    for guard in contract["required_true_guardrails"]:
        if output["guardrails"].get(guard) is not True:
            raise RehearsalError(f"contract violation: guardrail {guard} is not true")
    for command in output["operator_commands"]:
        safety_class = command.get("safety_class")
        if safety_class not in contract["operator_command_safety_classes"]:
            raise RehearsalError(
                f"contract violation: unsupported command safety class {safety_class}"
            )
    projection = output["action_plan_integration"]["projection"]
    for key in contract["action_plan_projection_required_keys"]:
        if key not in projection:
            raise RehearsalError(
                f"contract violation: missing action plan projection key {key}"
            )


def assert_expected(
    output: dict[str, Any],
    expected: dict[str, Any],
    *,
    fixture_id: str,
) -> None:
    if output["status"] != expected.get("status"):
        raise RehearsalError(f"{fixture_id}: expected status {expected.get('status')}, got {output['status']}")
    if output["decision"] != expected.get("decision"):
        raise RehearsalError(f"{fixture_id}: expected decision {expected.get('decision')}, got {output['decision']}")
    for reason in expected.get("reason_codes", []):
        if reason not in output["reason_codes"]:
            raise RehearsalError(f"{fixture_id}: missing reason code {reason}")
    selected_version = expected.get("selected_rollback_version")
    selected = output["rollback_selection"]["selected"]
    if selected_version is not None:
        if not selected or selected.get("version") != selected_version:
            raise RehearsalError(f"{fixture_id}: expected rollback {selected_version}, got {selected}")
    if expected.get("action_plan_decision"):
        actual = output["action_plan_integration"]["suggested_decision"]
        if actual != expected["action_plan_decision"]:
            raise RehearsalError(f"{fixture_id}: expected action plan {expected['action_plan_decision']}, got {actual}")
    if expected.get("mutating_commands_require_confirmation"):
        for command in output["operator_commands"]:
            if command["mutates_state"] and not command["requires_confirmation"]:
                raise RehearsalError(f"{fixture_id}: mutating command lacks confirmation")


def self_test(*, repo_root: Path, generated_at: str) -> dict[str, Any]:
    fixture = load_json(repo_root / FIXTURE_PATH)
    if fixture.get("schema") != FIXTURE_SCHEMA:
        raise RehearsalError(f"invalid fixture schema in {FIXTURE_PATH}")
    scenario_results = []
    for scenario in fixture.get("scenarios", []):
        fixture_id = str(scenario.get("id"))
        output = evaluate(scenario["input"], generated_at=generated_at)
        assert_contract(output, repo_root=repo_root)
        assert_expected(output, scenario.get("expected", {}), fixture_id=fixture_id)
        scenario_results.append(
            {
                "id": fixture_id,
                "status": output["status"],
                "decision": output["decision"],
                "reason_codes": output["reason_codes"],
            }
        )
    return {
        "schema": "pi.extension.quarantine_rehearsal_self_test.v1",
        "generated_at": generated_at,
        "status": "pass",
        "scenario_count": len(scenario_results),
        "scenarios": scenario_results,
    }


def write_output(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise RehearsalError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(payload), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rehearse extension quarantine and rollback guidance."
    )
    parser.add_argument("--input-json", type=Path, help="Input rehearsal JSON")
    parser.add_argument("--fixture-id", help="Run a named fixture scenario")
    parser.add_argument("--out-json", type=Path, help="Write output JSON")
    parser.add_argument("--generated-at", help="Override generated_at timestamp")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    parser.add_argument("--self-test", action="store_true", help="Run fixture self-test")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    generated_at = args.generated_at or utc_now_iso()
    try:
        if args.self_test:
            payload = self_test(repo_root=repo_root, generated_at=generated_at)
        else:
            if args.fixture_id:
                input_payload = load_fixture(args.fixture_id, repo_root=repo_root)
            elif args.input_json:
                input_payload = load_json(args.input_json)
            else:
                raise RehearsalError("provide --input-json, --fixture-id, or --self-test")
            payload = evaluate(input_payload, generated_at=generated_at)
            assert_contract(payload, repo_root=repo_root)
        if args.out_json:
            write_output(args.out_json, payload)
        if args.json or not args.out_json:
            sys.stdout.write(json_dumps(payload))
        return 0
    except RehearsalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
