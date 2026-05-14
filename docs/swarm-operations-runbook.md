# Swarm Operations Runbook

Practical workflow for launching, monitoring, throttling, recovering, and handing off large Pi agent swarms.

This runbook is operator guidance. It does not replace Beads as the work ledger, Agent Mail as the reservation/message ledger, `pi doctor` as the live diagnostic surface, or the release evidence gates as claim authority.

## Source Of Truth

| Surface | Authority | Command or artifact |
|---------|-----------|---------------------|
| Work ownership | Beads issue state and comments | `br ready --json`, `br show <id>`, `br update <id> --claim --actor "$AGENT_NAME"` |
| Cross-agent coordination | Agent Mail messages, reservations, and build slots | MCP Agent Mail `macro_start_session`, `file_reservation_paths`, `fetch_inbox` |
| Live swarm readiness | Doctor swarm diagnostics | `pi doctor --only swarm --format json` |
| Cargo/RCH admission | Cargo headroom preflight | `scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets` |
| Remote build status | RCH queue and worker state | `rch status`, `rch queue`, `rch doctor` |
| Handoff bundle | Operator runpack | `python3 scripts/build_swarm_operator_runpack.py --capture-current ...` |
| Saturation and timeline evidence | Redacted swarm activity ledger | `docs/swarm-activity-ledger.md`, schema `pi.swarm.activity_digest.v1` |
| Deterministic replay evidence | Swarm flight recorder | `docs/swarm-flight-recorder.md`, schema `pi.swarm.flight_recorder.report.v1` |
| Offline replay policy comparison | Swarm replay operator workflow | `docs/swarm-replay-operator-workflow.md`, `pi swarm-replay-preview --trace <trace.json>` |

## Startup Checklist

Run these before claiming work in a multi-agent session:

```bash
export AGENT_NAME="${AGENT_NAME:-$(whoami)}"
export PI_CARGO_AGENT_SUFFIX="$AGENT_NAME"
export CARGO_TARGET_DIR="/data/tmp/pi_agent_rust_cargo/${AGENT_NAME}/target"
export TMPDIR="/data/tmp/pi_agent_rust_cargo/${AGENT_NAME}/tmp"
mkdir -p "$CARGO_TARGET_DIR" "$TMPDIR"

git status --short --branch
br ready --json
bv --recipe actionable --robot-plan
pi doctor --only swarm --format json > /data/tmp/pi_swarm_runpack/doctor.json
scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets \
  --decision-json /data/tmp/pi_swarm_runpack/cargo-admission.json
rch status
rch queue
```

Green startup means:

- `git status --short --branch` has no uncommitted work from this agent.
- `br ready --json` has a real open issue, not a tombstone or deleted item.
- `pi doctor --only swarm --format json` has no red finding that says new swarm work must stop.
- `scripts/cargo_headroom.sh --runner rch --admit-only ...` returns `admit` or `allow`.
- `rch queue` does not show saturated or stale heavy builds that would make more cargo work irresponsible.

If any check is degraded, keep the raw command output and choose the response from the recovery table below. Do not convert degraded coordination or RCH state into a vague "tests failed" note.

## Claim A Bead

Use `bv` for prioritization and `br` for the actual claim:

```bash
bv --recipe actionable --robot-plan
br ready --json
br show <issue-id>
br update <issue-id> --claim --actor "$AGENT_NAME"
br comments add <issue-id> --author "$AGENT_NAME" --message \
  "Claimed by $AGENT_NAME. Scope: <files/modules>. Validation: <commands>. Coordination: <Agent Mail status>."
```

Before editing, reserve the narrowest practical file set in Agent Mail:

```text
file_reservation_paths(
  project_key="/data/projects/pi_agent_rust",
  agent_name="$AGENT_NAME",
  paths=["src/module.rs", "tests/module_tests.rs"],
  ttl_seconds=3600,
  exclusive=true,
  reason="<issue-id>"
)
```

If Agent Mail writes fail because the MCP database is unavailable or corrupt, record the failure in the Beads comment and continue with the Beads claim as the soft lock. Do not wait in coordination-only loops when useful non-overlapping work is available.

## Cargo And Test Policy

CPU-heavy Rust commands must go through RCH:

```bash
env CARGO_TARGET_DIR="$CARGO_TARGET_DIR" TMPDIR="$TMPDIR" \
  rch exec -- cargo check --all-targets

env CARGO_TARGET_DIR="$CARGO_TARGET_DIR" TMPDIR="$TMPDIR" \
  rch exec -- cargo clippy --all-targets -- -D warnings

env CARGO_TARGET_DIR="$CARGO_TARGET_DIR" TMPDIR="$TMPDIR" \
  rch exec -- cargo test <focused-filter> -- --nocapture
```

Use local commands only for non-heavy checks:

```bash
cargo fmt --check
git diff --check
timeout 60s ubs --staged --only=rust .
python3 scripts/check_ubs_staged_delta.py
./scripts/reconcile_beads_ledger.sh
```

If `timeout 60s ubs --staged --only=rust .` times out or is dominated by whole-file baseline noise, run `python3 scripts/check_ubs_staged_delta.py`. The delta gate is acceptable only when it reports no warning or critical finding on staged changed lines. Keep the raw timeout or baseline-noise summary in the handoff.

## Monitor An Active Swarm

Use this status loop while work is in progress:

```bash
git status --short --branch
br list --status=in_progress --json
br ready --json
rch status
rch queue
pi doctor --only swarm --format json
```

Watch for:

- Multiple agents editing the same file without Agent Mail reservations or Beads comments.
- `br list --status=in_progress --json` entries with old `updated_at` timestamps and no recent comments.
- `rch queue` entries with stale progress, repeated artifact retrieval failures, or slot pressure.
- `pi doctor --only swarm --format json` findings for Agent Mail build slots, reservation conflicts, cgroup memory pressure, target/TMPDIR headroom, or RCH classifier failures.
- Dirty worktree entries outside your claimed file set.

Do not revert unrelated dirty files. Treat them as another agent's work unless the owning bead or the user explicitly says otherwise.

## Throttle Or Pause

Back off new claims when any of these are true:

| Signal | Command | Action |
|--------|---------|--------|
| RCH admission denies or backs off | `scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets` | Stop starting heavy cargo jobs. Continue docs, source inspection, or small non-cargo fixes. |
| Queue pressure is high | `rch queue` | Wait for active jobs to finish before launching more cargo. |
| Agent Mail reservations conflict | `pi doctor --only swarm --format json` or Agent Mail reservation response | Narrow the file set, choose a different bead, or coordinate with the holder. |
| Beads has stale in-progress work | `br list --status=in_progress --json` | Comment on the stale issue, verify no recent owner activity, then reopen only if it is clearly abandoned. |
| Drop-in or release evidence is stale | `scripts/report_swarm_claim_readiness.py` | Do not make release-facing claims. File or work the evidence gap. |
| Worktree is dirty outside your scope | `git status --short --branch` | Ignore unrelated changes and keep your commit narrowly staged. |

## Stalled Bead Recovery

Use this only for clearly abandoned work:

```bash
br show <issue-id>
br comments list <issue-id>
git log --oneline --decorate --all -- <claimed-file>
br update <issue-id> --status open --assignee "" --actor "$AGENT_NAME"
br comments add <issue-id> --author "$AGENT_NAME" --message \
  "Reopened as stale: no recent owner activity found; no file changes reverted."
```

Do not reopen an in-progress bead just because Agent Mail is degraded. A current Beads comment, recent commit, or active file reservation is enough evidence that another agent may still own it.

## Recovery Drills

### Agent Mail Degraded

1. Run `pi doctor --only swarm --format json` and save the finding.
2. Try the MCP read path: `fetch_inbox` or `list_agents`.
3. If writes fail, use Beads status/comments as the soft lock.
4. Keep work on a narrow file set and mention the degraded Mail state in the final handoff.

### RCH Retrieval Or Disk Pressure

1. Run `rch status` and `rch queue`.
2. Run `scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets`.
3. If the classifier points at local target/TMPDIR headroom, move `CARGO_TARGET_DIR` and `TMPDIR` under `/data/tmp/pi_agent_rust_cargo/$AGENT_NAME`.
4. If the remote command failed, treat it as a code or remote-build failure only after the raw RCH output identifies that class.

### Dirty Worktree

1. Run `git status --short --branch`.
2. Stage only files for the current bead.
3. Do not use `git reset --hard`, `git clean`, `git checkout --`, or `rm` cleanup commands.
4. If unrelated dirty files block a command, record the exact blocker and ask for direction.

### Saturated Review Loop

1. Build or read the swarm activity digest described in `docs/swarm-activity-ledger.md`.
2. If saturation reasons show duplicate work, stale introductions, repeated blockers, or low validation throughput, stop launching broad review agents.
3. Pick one narrow implementation bead, a focused `testing-*` skill, or a concrete follow-up bead from the digest recommendation.

## Handoff Bundle

Capture a handoff bundle before ending a swarm shift:

```bash
capture_dir="/data/tmp/pi_swarm_runpack/${AGENT_NAME}-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$capture_dir"

python3 scripts/build_swarm_operator_runpack.py \
  --capture-current \
  --capture-dir "$capture_dir" \
  --project-root /data/projects/pi_agent_rust \
  --agent-name "$AGENT_NAME" \
  --out-json "$capture_dir/operator-runpack.json" \
  --out-md "$capture_dir/operator-runpack.md" \
  --out-autopilot-input-pack-json "$capture_dir/autopilot-input-pack.json" \
  --out-autopilot-plan-json "$capture_dir/autopilot-plan.json"
```

The runpack schema is governed by `docs/contracts/swarm-operator-runpack-contract.json`. The runpack is a redacted index over existing evidence, not a release performance claim and not a replacement for the source artifacts.
The autopilot input pack schema is governed by `docs/contracts/swarm-autopilot-input-pack-contract.json`. It normalizes source statuses for the dry-run planner, but it is still advisory and never replaces Doctor, Beads, Agent Mail, RCH, git, or the source artifacts themselves.
The autopilot plan schema is governed by `docs/contracts/swarm-autopilot-plan-contract.json`. It maps the input pack to ordered dry-run actions such as `claim_ready_bead`, `wait_for_rch`, `adjust_swarm_budget`, `use_beads_soft_lock`, `reopen_stale_bead_candidate`, `run_docs_only_work`, `capture_handoff`, or `stop_and_surface_blocker`.
When the command emits the companion input pack and plan, the runpack also includes `autopilot_handoff` with schema `pi.swarm.autopilot_handoff.v1`. That section names the input-pack and plan schemas, artifact paths, selected advisory action, and source provenance so a new agent can inspect one handoff bundle without treating the runpack as a new source of truth.
The plan also includes `work_partitions` for ready Beads. Those entries recommend reservation globs, likely collision surfaces to avoid, alternate file families, confidence, and degraded caveats. They are diagnostic only; operators still claim through Beads and reserve through Agent Mail when it is healthy.
The input pack and plan also carry `budget_drift` evidence with schema `pi.swarm.budget_drift.v1`. It compares the last accepted swarm resource preflight profile with live cgroup, memory, scratch-path, RCH queue, and active-owner observations. Status `stable` keeps the current ceiling, `degraded` recommends reduced fanout with hysteresis, and `deny_new_work` recommends admitting no new agents or heavyweight RCH verification until the live signals recover.
The plan also includes `failure_actions` for common operational blockers. Those entries use stable catalog IDs for RCH artifact retrieval, local Cargo target/TMPDIR pressure, remote compiler failures, Agent Mail schema/read-only degradation, Beads JSONL drift, stale Beads ownership, and unknown operational failures. Unknown entries fail closed with a redacted raw excerpt and safe inspection commands instead of guessing a root cause.
The no-mock autopilot E2E harness emits `pi.swarm.autopilot_e2e.v1` plus `pi.swarm.autopilot_e2e.event.v1` JSONL events. It uses temp Beads and temp git workspaces where safe, fixture-captured degraded Agent Mail and RCH inputs where live mutation would be unsafe, and verifies healthy claim, empty queue, Beads soft-lock fallback, saturated RCH, stale bead review, unrelated dirty worktree, and malformed-source fail-closed scenarios. This is operator admission evidence only; it is not a release speed, drop-in, or benchmark claim.
The final closeout gate emits `pi.swarm.autopilot_decision_gate.v1`, governed by `docs/contracts/swarm-autopilot-decision-gate-contract.json`. It compares the shipped input pack, planner, work partitions, failure-action catalog, budget drift watcher, E2E/logging evidence, runpack handoff, safety guards, pushed commits, and quality gates to the prompt-to-artifact checklist. A failed gate emits `follow_up_beads` and `decision=file_follow_up_beads_before_closing_epic`; a passing gate is still only closeout evidence over Beads, git, RCH, Doctor, Agent Mail, and source artifacts, not a new source of truth.
The context-intelligence closeout gate emits `pi.context_intelligence.closeout_gate.v1`, governed by `docs/contracts/context-intelligence-closeout-gate-contract.json`. It maps each `bd-ircr3` child bead to code, tests, docs or evidence, commands, close reasons, and commit hashes; then it checks graph contracts, graph builder, freshness and claim gates, bundle planner, redaction and invalidation, preview surface, prompt injection, no-mock E2E, performance budgets, Doctor/runpack posture, operator docs, README freshness, pushed commits, staged UBS, and Beads ledger reconciliation. A passing context gate is closeout evidence only and does not replace Beads, git, RCH, Doctor, runpacks, or source files.

When closing the autopilot epic, collect the actual command outcomes and pass them to the final gate:

```bash
final_gate_dir="/data/tmp/pi_swarm_autopilot_final_gate/${AGENT_NAME:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$final_gate_dir"

python3 scripts/build_swarm_operator_runpack.py \
  --run-autopilot-final-gate \
  --out-autopilot-final-gate-json "$final_gate_dir/summary.json" \
  --quality-gate-result "py_compile=pass:python3 -m py_compile scripts/build_swarm_operator_runpack.py" \
  --quality-gate-result "runpack_self_test=pass:python3 scripts/build_swarm_operator_runpack.py --self-test" \
  --quality-gate-result "autopilot_e2e=pass:python3 scripts/build_swarm_operator_runpack.py --run-autopilot-e2e" \
  --quality-gate-result "json_contracts=pass:python3 -m json.tool docs/contracts/swarm-autopilot-decision-gate-contract.json" \
  --quality-gate-result "cargo_fmt=pass:cargo fmt --check" \
  --quality-gate-result "cargo_check_all_targets_rch=pass:CARGO_TARGET_DIR=$CARGO_TARGET_DIR TMPDIR=$TMPDIR rch exec -- cargo check --all-targets" \
  --quality-gate-result "cargo_clippy_all_targets_rch=pass:CARGO_TARGET_DIR=$CARGO_TARGET_DIR TMPDIR=$TMPDIR rch exec -- cargo clippy --all-targets -- -D warnings" \
  --quality-gate-result "staged_ubs=pass:timeout 60s ubs --staged --only=rust ." \
  --quality-gate-result "beads_ledger_reconcile=pass:./scripts/reconcile_beads_ledger.sh"
```

When closing the context-intelligence epic, collect the actual command outcomes and pass them to the final gate:

```bash
final_gate_dir="/data/tmp/pi_context_intelligence_final_gate/${AGENT_NAME:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$final_gate_dir"

python3 scripts/build_swarm_operator_runpack.py \
  --run-context-intelligence-final-gate \
  --out-context-intelligence-final-gate-json "$final_gate_dir/summary.json" \
  --quality-gate-result "py_compile=pass:python3 -m py_compile scripts/build_swarm_operator_runpack.py" \
  --quality-gate-result "runpack_self_test=pass:python3 scripts/build_swarm_operator_runpack.py --self-test" \
  --quality-gate-result "json_contracts=pass:python3 -m json.tool docs/contracts/context-intelligence-closeout-gate-contract.json" \
  --quality-gate-result "semantic_context_graph_contract_rch=pass:rch exec -- cargo test --test semantic_context_graph_contract -- --nocapture" \
  --quality-gate-result "semantic_workspace_graph_contract_rch=pass:rch exec -- cargo test --test semantic_workspace_graph_contract -- --nocapture" \
  --quality-gate-result "semantic_workspace_graph_builder_rch=pass:rch exec -- cargo test --test semantic_workspace_graph_builder context" \
  --quality-gate-result "context_intelligence_e2e_rch=pass:rch exec -- cargo test --test e2e_agent_loop context_intelligence_no_mock_harness -- --nocapture" \
  --quality-gate-result "doctor_context_intelligence_rch=pass:rch exec -- cargo test --test doctor_swarm_temp_dir_json context_intelligence -- --nocapture" \
  --quality-gate-result "context_perf_budgets_rch=pass:rch exec -- cargo test --test perf_budgets context_intelligence" \
  --quality-gate-result "context_intelligence_closeout_gate_contract_rch=pass:rch exec -- cargo test --test context_intelligence_closeout_gate_contract -- --nocapture" \
  --quality-gate-result "cargo_fmt=pass:cargo fmt --check" \
  --quality-gate-result "cargo_check_all_targets_rch=pass:CARGO_TARGET_DIR=$CARGO_TARGET_DIR TMPDIR=$TMPDIR rch exec -- cargo check --all-targets" \
  --quality-gate-result "cargo_clippy_all_targets_rch=pass:CARGO_TARGET_DIR=$CARGO_TARGET_DIR TMPDIR=$TMPDIR rch exec -- cargo clippy --all-targets -- -D warnings" \
  --quality-gate-result "staged_ubs=pass:timeout 60s ubs --staged --only=rust ." \
  --quality-gate-result "beads_ledger_reconcile=pass:./scripts/reconcile_beads_ledger.sh"
```

## Completion Checklist

Before closing a bead:

```bash
env CARGO_TARGET_DIR="$CARGO_TARGET_DIR" TMPDIR="$TMPDIR" \
  rch exec -- cargo check --all-targets
env CARGO_TARGET_DIR="$CARGO_TARGET_DIR" TMPDIR="$TMPDIR" \
  rch exec -- cargo clippy --all-targets -- -D warnings
cargo fmt --check
git diff --check
git add <changed-files> .beads/issues.jsonl
timeout 60s ubs --staged --only=rust .
python3 scripts/check_ubs_staged_delta.py
./scripts/reconcile_beads_ledger.sh
br close <issue-id> --reason "<completed evidence>"
br sync --flush-only
git add .beads/issues.jsonl
AGENT_NAME="$AGENT_NAME" git commit -m "<type>: <summary>"
git pull --rebase
git push
git push origin main:master
git status --short --branch
```

For docs-only changes, use docs-focused validation instead of forcing cargo:

```bash
command -v git br bv rch cargo jq python3
python3 scripts/build_swarm_operator_runpack.py --self-test
e2e_dir="/data/tmp/pi_swarm_autopilot_e2e/${AGENT_NAME:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
python3 scripts/build_swarm_operator_runpack.py \
  --run-autopilot-e2e \
  --capture-dir "$e2e_dir" \
  --out-autopilot-e2e-json "$e2e_dir/summary.json" \
  --out-autopilot-e2e-events-jsonl "$e2e_dir/events.jsonl"
python3 -m json.tool docs/contracts/swarm-operator-runpack-contract.json >/dev/null
python3 -m json.tool docs/contracts/swarm-autopilot-input-pack-contract.json >/dev/null
python3 -m json.tool docs/contracts/swarm-autopilot-plan-contract.json >/dev/null
python3 -m json.tool docs/contracts/swarm-autopilot-decision-gate-contract.json >/dev/null
cargo fmt --check
git diff --check
./scripts/reconcile_beads_ledger.sh
```

## Schema Examples

Doctor swarm preflight evidence:

```json
{
  "schema": "pi.doctor.swarm_resource_preflight.v1",
  "status": "pass",
  "effective_cpu_cores": 64,
  "memory_limit_bytes": 274877906944,
  "recommended_budgets": {
    "agent_fanout": 8,
    "rch_verification_fanout": 4
  }
}
```

Cargo/RCH admission evidence:

```json
{
  "schema": "pi.cargo_headroom.admission.v1",
  "decision": "admit",
  "requested_runner": "rch",
  "resolved_runner": "rch",
  "cargo_command": "cargo check --all-targets",
  "rch_queue_forecast": {
    "schema": "pi.cargo_headroom.rch_queue_forecast.v1",
    "recommended_action": "proceed"
  }
}
```

Operator runpack evidence:

```json
{
  "schema": "pi.swarm.operator_runpack.v1",
  "purpose": "operator_handoff_not_release_performance_claim",
  "status": "ready",
  "autopilot_handoff": {
    "schema": "pi.swarm.autopilot_handoff.v1",
    "status": "ready",
    "input_pack": {
      "schema": "pi.swarm.autopilot_input_pack.v1",
      "artifact_path": "/data/tmp/pi_swarm_runpack/<run>/autopilot-input-pack.json"
    },
    "plan": {
      "schema": "pi.swarm.autopilot_plan.v1",
      "selected_action": "claim_ready_bead",
      "artifact_path": "/data/tmp/pi_swarm_runpack/<run>/autopilot-plan.json"
    },
    "source_provenance": {
      "source_statuses": [
        {
          "id": "beads_ready",
          "status": "ok"
        }
      ],
      "command_count": 5
    }
  },
  "swarm_scale_safety_scorecard": {
    "schema": "pi.swarm.safety_scorecard.v1",
    "overall_status": "ready"
  }
}
```

Autopilot input-pack evidence:

```json
{
  "schema": "pi.swarm.autopilot_input_pack.v1",
  "purpose": "dry_run_swarm_autopilot_input_not_source_of_truth",
  "status": "degraded",
  "normalized_inputs": {
    "agent_mail": {
      "status": "degraded",
      "fallback_action": "use_beads_soft_lock"
    },
    "budget_drift": {
      "schema": "pi.swarm.budget_drift.v1",
      "status": "deny_new_work",
      "signals": [
        {
          "id": "rch_queue_saturated",
          "severity": "critical",
          "recommendation": "deny new heavyweight work until RCH queue pressure clears"
        }
      ],
      "recommended_adjustments": {
        "admit_new_agents": 0,
        "rch_verification_fanout": 0,
        "reason": "deny_new_work until critical budget drift clears"
      }
    }
  },
  "planner_guards": {
    "dry_run_only": true,
    "no_prose_scraping": true
  }
}
```

Autopilot plan evidence:

```json
{
  "schema": "pi.swarm.autopilot_plan.v1",
  "purpose": "dry_run_swarm_autopilot_plan_not_source_of_truth",
  "status": "ready",
  "actions": [
    {
      "rank": 1,
      "action": "claim_ready_bead",
      "evidence_paths": [
        "normalized_inputs.beads_ready.candidates",
        "work_partitions"
      ],
      "commands": [
        {
          "purpose": "Inspect ready bead before claiming",
          "command": "br show <issue-id> --json"
        }
      ]
    }
  ],
  "planner_guards": {
    "dry_run_only": true,
    "commands_require_operator_execution": true
  }
}
```

Degraded autopilot plan evidence:

```json
{
  "schema": "pi.swarm.autopilot_plan.v1",
  "purpose": "dry_run_swarm_autopilot_plan_not_source_of_truth",
  "status": "degraded",
  "budget_drift": {
    "schema": "pi.swarm.budget_drift.v1",
    "status": "deny_new_work",
    "profile_status": "ok",
    "recommended_adjustments": {
      "admit_new_agents": 0,
      "rch_verification_fanout": 0
    }
  },
  "work_partitions": [
    {
      "issue_id": "bd-provider",
      "surface_ids": [
        "provider_streaming"
      ],
      "suggested_reservation": [
        "src/provider.rs",
        "src/providers/**/*.rs",
        "tests/provider_streaming*.rs"
      ],
      "avoid": [],
      "confidence": "high",
      "degraded_caveats": []
    }
  ],
  "failure_actions": [
    {
      "id": "FAIL-AGENT-MAIL-SCHEMA",
      "catalog_schema": "pi.swarm.failure_action_catalog.v1",
      "category": "agent_mail",
      "title": "Agent Mail database schema is missing required tables",
      "match_confidence": "high",
      "explanation": "Agent Mail coordination cannot be trusted for reservations or inbox state until the mailbox schema is repaired or restored.",
      "evidence_paths": [
        "normalized_inputs.agent_mail"
      ],
      "matched_source": "agent_mail",
      "safe_commands": [
        {
          "purpose": "Preview Agent Mail repair",
          "command": "am doctor repair --dry-run"
        }
      ],
      "escalation": "Continue with Beads soft locks until Mail health is green.",
      "raw_excerpt": "status=degraded issue=database schema missing required tables",
      "redaction_summary": {
        "redacted_count": 0,
        "fields": []
      }
    }
  ],
  "actions": [
    {
      "rank": 1,
      "action": "adjust_swarm_budget",
      "evidence_paths": [
        "normalized_inputs.budget_drift.status",
        "normalized_inputs.budget_drift.signals"
      ],
      "commands": [
        {
          "purpose": "Refresh swarm resource preflight",
          "command": "pi doctor --only swarm --format json"
        }
      ]
    },
    {
      "rank": 2,
      "action": "use_beads_soft_lock",
      "evidence_paths": [
        "normalized_inputs.agent_mail.status"
      ],
      "commands": [
        {
          "purpose": "Inspect active ownership",
          "command": "br list --status=in_progress --json"
        }
      ]
    }
  ]
}
```

Autopilot no-mock E2E evidence:

```json
{
  "schema": "pi.swarm.autopilot_e2e.v1",
  "purpose": "no_mock_swarm_autopilot_e2e_operator_evidence_not_release_claim",
  "status": "pass",
  "required_scenarios": [
    "healthy_ready_claim",
    "empty_ready_queue",
    "degraded_agent_mail_soft_lock",
    "saturated_rch_queue",
    "stale_in_progress_bead",
    "unrelated_dirty_worktree",
    "malformed_source_fail_closed"
  ],
  "events_jsonl": "/data/tmp/pi_swarm_autopilot_e2e/<run>/events.jsonl",
  "guards": {
    "uses_real_temp_beads": true,
    "uses_real_temp_git": true,
    "fixture_captures_degraded_rch_and_agent_mail": true,
    "dangerous_commands_blocked": true,
    "heavy_rust_validation_requires_rch": true
  }
}
```

Autopilot final decision-gate evidence:

```json
{
  "schema": "pi.swarm.autopilot_decision_gate.v1",
  "purpose": "prompt_to_artifact_autopilot_epic_close_gate_not_source_of_truth",
  "status": "pass",
  "required_checks": [
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
    "quality_gates"
  ],
  "missing_checks": [],
  "follow_up_required": false,
  "follow_up_beads": [],
  "decision": "close_final_gate_and_parent_epic",
  "epic_can_close_after_this_commit": true
}
```

Context-intelligence final closeout-gate evidence:

```json
{
  "schema": "pi.context_intelligence.closeout_gate.v1",
  "purpose": "prompt_to_artifact_context_intelligence_closeout_gate_not_source_of_truth",
  "status": "pass",
  "required_checks": [
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
    "quality_gates"
  ],
  "missing_checks": [],
  "follow_up_required": false,
  "follow_up_beads": [],
  "decision": "close_final_gate_and_parent_epic",
  "epic_can_close_after_this_commit": true
}
```

Swarm flight-recorder report evidence:

```json
{
  "schema": "pi.swarm.flight_recorder.report.v1",
  "event_count": 12,
  "coordination_failures": [],
  "replay_command": "cargo test --test e2e_swarm_flight_recorder -- --exact multi_agent_flight_recorder_bundle_replays_without_credentials --nocapture"
}
```

## Validation Record

When this runbook changes, run at least:

```bash
command -v git br bv rch cargo jq python3
python3 scripts/build_swarm_operator_runpack.py --self-test
e2e_dir="/data/tmp/pi_swarm_autopilot_e2e/${AGENT_NAME:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
python3 scripts/build_swarm_operator_runpack.py \
  --run-autopilot-e2e \
  --capture-dir "$e2e_dir" \
  --out-autopilot-e2e-json "$e2e_dir/summary.json" \
  --out-autopilot-e2e-events-jsonl "$e2e_dir/events.jsonl"
python3 -m json.tool docs/contracts/swarm-operator-runpack-contract.json >/dev/null
python3 -m json.tool docs/contracts/swarm-autopilot-input-pack-contract.json >/dev/null
python3 -m json.tool docs/contracts/swarm-autopilot-plan-contract.json >/dev/null
python3 -m json.tool docs/contracts/swarm-autopilot-decision-gate-contract.json >/dev/null
cargo fmt --check
git diff --check
./scripts/reconcile_beads_ledger.sh
```

If a validation command is unavailable or degraded, record the command, exit code, and stderr in the Beads closeout instead of claiming the runbook is fully validated.
