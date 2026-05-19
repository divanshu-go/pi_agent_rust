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
| Progress posture | Read-only progress SLO report | `pi swarm-progress --input <progress-slo-input.json> --out-json <progress-slo.json>` |
| Queue convergence | Read-only empty-queue convergence report | `python3 scripts/report_empty_queue_convergence.py --json` |
| Dry-run self-healing guidance | Runpack action plan and work admission gate | `python3 scripts/build_swarm_operator_runpack.py --out-action-plan-json ... --out-work-admission-gate-json ...` |
| Evidence renewal posture | Stale evidence renewal queue | `python3 scripts/build_stale_evidence_renewal_queue.py --out-json ...` |
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
capture_dir="${PI_SWARM_CAPTURE_DIR:-/data/tmp/pi_swarm_runpack/${AGENT_NAME}}"
mkdir -p "$CARGO_TARGET_DIR" "$TMPDIR" "$capture_dir"

git status --short --branch
br ready --json
bv --recipe actionable --robot-plan
if curl -fsS http://127.0.0.1:8765/health > "$capture_dir/agent-mail-health.json"; then
  agent_mail_arg=(--agent-mail-health-json "$capture_dir/agent-mail-health.json")
else
  agent_mail_arg=()
fi
python3 scripts/report_empty_queue_convergence.py --json \
  --beads-jsonl .beads/issues.jsonl \
  "${agent_mail_arg[@]}"
# When available, add:
#   --validation-broker-json <validation-broker-status-or-plan.json>
pi doctor --only swarm --format json > "$capture_dir/doctor.json"
scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets \
  --decision-json "$capture_dir/cargo-admission.json"
rch status
rch queue
```

Green startup means:

- `git status --short --branch` has no uncommitted work from this agent.
- `br ready --json` has a real open issue, not a tombstone or deleted item.
- `scripts/report_empty_queue_convergence.py --json` reports
  `status=ready_work_available` before claiming a new bead, or
  `status=work_to_plan` when only deferred roadmap/planning epics remain and
  new/refined child Beads should be created, or
  `status=queue_clean` only when no ready/in-progress work remains and
  no deferred planning epic still needs child backlog.
- If `--validation-broker-json` is supplied, stale slots, saturated slot
  posture, malformed JSON, and duplicate expensive cargo gate opportunities
  appear as advisory operator context. Malformed supplied broker JSON fails
  closed with a warning; missing broker JSON remains optional.
- If Agent Mail health JSON is supplied, degraded or schema-corrupt Agent Mail
  appears as an explicit `use_beads_fallback` action with the exact recovery
  action, while ready Beads work remains visible. If health capture fails, keep
  the report's `agent_mail_status=unavailable` warning and use the Beads claim
  as the soft lock.
- `pi doctor --only swarm --format json` has no red finding that says new swarm work must stop.
- `scripts/cargo_headroom.sh --runner rch --admit-only ...` returns
  `decision=allow` with `admission_action=allow`. `admission_action=defer`
  means the gate must wait, and `admission_action=fallback` means the command
  would run locally only because fallback was explicitly allowed.
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

If a local pre-commit hook appears to be running a broad repo scan instead of
the staged UBS contract, run
`python3 scripts/check_ubs_staged_delta.py --check-pre-commit-hook --json`.
The audit is read-only: it reports `.git/hooks/pre-commit` drift without
editing the hook.

## Remote Validation Proof Ledger

Remote validation proof is governed by
`docs/contracts/remote-validation-proof-ledger-contract.json` with ledger schema
`pi.remote_validation.proof_ledger.v1`. The ledger is operator evidence only;
it is not release performance evidence, benchmark support, strict drop-in
certification evidence, or a replacement for RCH, `cargo_headroom.sh`, CI, UBS,
Beads, Agent Mail, or claim-integrity gates.

Each proof entry must identify the command, command class, runner requirement,
resolved runner, RCH worker/job, remote/local fallback state, start/end
timestamps, exit status, `CARGO_TARGET_DIR`, `TMPDIR`, remote target/tmp paths,
artifact retrieval status, changed and covered paths, warnings, and the final
evidence classification. Cargo, script self-test, staged UBS, and Beads ledger
reconciliation commands are normalized into the same ledger shape.

Interpretation rules:

- `clean_remote_proof=true` requires an RCH remote run, successful command exit,
  no local fallback, and clean or non-applicable artifact retrieval.
- `local_fallback=observed` is never remote proof.
- `local_fallback=refused` is a correct fail-closed blocker for RCH-required
  gates, not a pass.
- Queue backoff must be recorded as a blocked proof entry rather than converted
  into a skipped green gate.
- Artifact retrieval warnings must stay visible in handoff. A remote command can
  exit 0 while artifact retrieval is degraded; that is not clean remote proof.
- `authoritative_for_bead=true` requires a passing proof whose covered paths
  include every claimed changed path. A proof that explicitly claims authority
  but leaves a claimed path uncovered must fail closed with
  `proof_claim_coverage_mismatch`.
- Unrelated worktree blockers and RCH worker workspace-shadow failures must be
  represented as blocked proof entries, not as source regressions.

When the proof ledger is embedded in an operator runpack, inspect these fields
before closing any RCH-required bead:

```bash
jq '.remote_validation_proof_ledger.summary' runpack.json
jq '.remote_validation_proof_ledger.entries[] | {
  command: .command.rendered,
  class: .command_class,
  resolved: .runner.resolved_runner,
  remote: .runner.remote_execution,
  local_fallback: .runner.local_fallback,
  artifacts: .artifact_retrieval.status,
  coverage: .evidence_classification.coverage.coverage_status,
  authoritative: .evidence_classification.coverage.authoritative_for_bead,
  clean: .evidence_classification.clean_remote_proof,
  status: .evidence_classification.status,
  warnings: [.warnings[].warning_id]
}' runpack.json
```

Golden examples live in
`tests/golden_corpus/remote_validation_proof_ledger/examples.json` and cover a
clean remote pass, local-fallback refusal, queue backoff, and artifact retrieval
warning.

### Remote Validation Proof Reuse Gate

The proof reuse gate is governed by
`docs/contracts/remote-validation-proof-reuse-gate-contract.json` and emits
`pi.validation.proof_reuse_gate.v1`. It is a read-only admission aid for
deciding whether an existing remote validation proof can cover the exact current
command, git head, staged paths, runner requirement, `CARGO_TARGET_DIR`, and
`TMPDIR` context.

Use it only as a fail-closed preflight:

```bash
python3 scripts/build_swarm_operator_runpack.py \
  --run-proof-reuse-gate \
  --proof-ledger-json runpack-proof-ledger.json \
  --proof-reuse-context-json current-proof-context.json \
  --print-proof-reuse-gate
```

`reuse_allowed=true` means the selected proof matched every required context
field and covered every current changed path. `reuse_allowed=false` means rerun
validation through RCH. Any stale git head, dirty-worktree mismatch, staged-path
coverage gap, missing RCH provenance, command fingerprint mismatch, target/tmp
drift, or current `Cargo.lock` / `rust-toolchain.toml` change invalidates reuse.

The gate never skips validation by itself and does not mutate Beads, git, Agent
Mail, RCH workers, source files, or temp artifacts.

### Validation Proof-Memory Index

The validation proof-memory index is governed by
`docs/contracts/validation-proof-memory-index-contract.json` and emits
`pi.validation.proof_memory_index.v1`. It is a read-only index over checked
remote-validation proof fixtures and proof-reuse decisions for the current
command, git head, staged paths, RCH provenance, `CARGO_TARGET_DIR`, `TMPDIR`,
and artifact retrieval context.

Use it as an operator audit artifact, not a validation skipper:

```bash
python3 scripts/build_swarm_operator_runpack.py \
  --run-validation-proof-memory-index \
  --print-validation-proof-memory-index
```

The current fixture artifact is
`docs/evidence/validation-proof-memory-index.json`. It must contain one reusable
remote proof and fail-closed fixtures for stale git head, stale source time,
missing artifact, local fallback, dirty-worktree mismatch, command-fingerprint
mismatch, path-coverage mismatch, non-authoritative coverage, and failing
closeout/runpack freshness inputs. Any non-reusable class means rerun or refresh
validation through the appropriate gate before closeout.

The index never mutates RCH, Agent Mail, Beads, git, source files, temp
artifacts, or runtime scheduling policy. It does not authorize release
performance, benchmark, capacity, or strict drop-in claims.

### Operator Work Recommender

The operator work recommender is governed by
`docs/contracts/operator-work-recommendation-contract.json` and emits
`pi.swarm.operator_work_recommendation.v1`. It consumes the incident replay and
validation proof-memory artifacts, then ranks advisory next-work decisions for
healthy ready Beads, no ready work, Agent Mail corruption, RCH saturation, stale
proof refresh, duplicate-work risk, and dirty-worktree admission denial.

Use it to inspect the next safe operator posture before claiming work:

```bash
python3 scripts/build_swarm_operator_runpack.py \
  --run-operator-work-recommendation \
  --print-operator-work-recommendation
```

The current fixture artifact is
`docs/evidence/operator-work-recommendation.json`. Every recommendation cites
exact evidence paths, names rejected unsafe alternatives, gives a confidence
score, and includes an operator-facing explanation. Missing, stale,
contradictory, unredacted, or authority-confused source evidence fails closed
to `refresh_or_surface_operator_blocker`.

The recommender is read-only. It never claims Beads, writes Agent Mail
reservations, launches RCH, runs cargo, mutates git, deletes files, or replaces
source systems. Operators still execute any selected action through the normal
Beads, Agent Mail, RCH, git, and validation workflows.

### Operator Smoothness SLO

The operator smoothness SLO is governed by
`docs/contracts/operator-smoothness-slo-contract.json` and emits
`pi.operator.smoothness_slo.v1`. It uses deterministic high-volume fixtures for
provider stream deltas, RPC output pressure, TUI frame rendering, tool-update
coalescing, and session-write pressure.

Use it to inspect semantic visibility under synthetic swarm output pressure:

```bash
python3 scripts/build_swarm_operator_runpack.py \
  --run-operator-smoothness-slo \
  --print-operator-smoothness-slo
```

The current fixture artifact is `docs/evidence/operator-smoothness-slo.json`.
Every case includes surface metrics with p50/p95/p99 visibility counters,
semantic milestone counts, low-value coalescing counts, backlog budgets, and
failure logs. Negative controls fail closed for delayed semantic visibility,
non-monotonic timelines, runaway frame backlog, and missing surface coverage.
The counters are engineering fixture evidence only; they do not authorize
benchmark, capacity, release performance, strict drop-in, or runtime mutation
claims.

### Extension Resource Firewall Matrix

The extension resource firewall matrix is governed by
`docs/contracts/extension-resource-firewall-matrix-contract.json` and emits
`pi.ext.resource_firewall_matrix.v1` from the deterministic extension stress
fixture. It covers cheap-read floods, large payload emission, denied capability
churn, slow hostcalls, repeated failure, and steady-peer progress.

Use the focused stress-test slice to produce the target/perf evidence:

```bash
export CARGO_TARGET_DIR="/data/tmp/pi_agent_rust_cargo/${USER:-agent}/target"
export TMPDIR="/data/tmp/pi_agent_rust_cargo/${USER:-agent}/tmp"
mkdir -p "$CARGO_TARGET_DIR" "$TMPDIR"
rch exec -- env CARGO_TARGET_DIR="$CARGO_TARGET_DIR" TMPDIR="$TMPDIR" cargo test --test extensions_stress resource_firewall_matrix -- --nocapture
```

The test writes `resource_firewall_matrix.json` under the configured
`CARGO_TARGET_DIR`'s `perf/` directory. Every row includes resource class,
extension role, hostcall class, budget, observed units, admission decision,
denial mode, fallback behavior, payload-redaction status, capability-boundary
status, peer-progress preservation, and operator-visible counters. Negative
controls fail closed for missing counters, missing peer progress, and unredacted
payload bodies. The matrix extends hostcall cost attribution evidence; it does
not replace runtime enforcement, capability policy, RCH validation, Agent Mail,
Beads, UBS, CI, or benchmark/capacity/release claims.

## Temp Artifact Inventory

Swarm runpacks include `temp_artifact_inventory` with schema
`pi.swarm.temp_artifact_inventory.v1`. This is a read-only inventory of scratch
and evidence paths observed through cargo admission, RCH proof entries, smoke
harness artifacts, validation output captures, and capture-manifest temp
artifacts.

The inventory never executes cleanup and does not emit deletion commands. Every
entry records a deletion policy:

- `retain_active`: owned or active paths that must be preserved.
- `requires_explicit_operator_approval`: known-owner stale candidates that still
  require written approval before deletion.
- `deletion_protected_unknown_owner`: unknown-owner paths, always protected.

Operators may use the emitted review commands such as `stat` and `du -sh` to
inspect pressure, but deleting files or directories still requires explicit
written permission outside the runpack.

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

## Progress SLO Operator Workflow

`pi swarm-progress` classifies whether a swarm is making progress from a
normalized `ProgressSloEvaluationInput` snapshot. It is read-only advisory
evidence. It does not read live Beads, send Agent Mail, reserve files, start or
cancel RCH jobs, mutate git, close beads, waive validation gates, or support
release-facing speed, capacity, benchmark, or strict drop-in claims.

Capture the source facts first, then build or reuse the normalized input from
those artifacts:

```bash
capture_dir="/data/tmp/pi_swarm_progress/${AGENT_NAME:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$capture_dir"

br list --json > "$capture_dir/beads.json"
br ready --json > "$capture_dir/beads-ready.json"
br list --status=in_progress --json > "$capture_dir/beads-in-progress.json"
git status --short --branch > "$capture_dir/git-status.txt"
rch status > "$capture_dir/rch-status.txt"
rch queue > "$capture_dir/rch-queue.txt"
PI_SWARM_PROGRESS_SLO_JSON="$capture_dir/progress-slo.json" \
  pi doctor --only swarm --format json > "$capture_dir/doctor-swarm.json"
```

Evaluate a prepared normalized input and keep both machine and human-readable
artifacts:

```bash
pi swarm-progress \
  --input "$capture_dir/progress-slo-input.json" \
  --since HEAD~1 \
  --out-json "$capture_dir/progress-slo.json" \
  --out-text "$capture_dir/progress-slo.txt"
```

`--since` is optional, but when it is supplied it must match
`input.time_window.comparison_baseline`. The command refuses to overwrite
existing output files; use a fresh capture directory instead of deleting old
evidence.

Use `jq` to inspect the fields operators usually need:

```bash
jq '{schema, status, confidence, reason_ids, next_actions}' \
  "$capture_dir/progress-slo.json"

jq '.saturation_summary | {
  coordination_saturation,
  build_saturation,
  validation_saturation,
  queue_convergence,
  recommended_operator_posture
}' "$capture_dir/progress-slo.json"

jq '.source_statuses[] | {
  id: .source_id,
  kind: .source_kind,
  availability,
  freshness_state,
  redaction_state,
  degraded_reason
}' "$capture_dir/progress-slo.json"

jq '.redaction_summary | {
  redacted_source_count,
  unsafe_to_emit_source_count,
  suppressed_claims
}' "$capture_dir/progress-slo.json"
```

Interpret statuses conservatively:

| Status | Typical reason | Operator action |
| --- | --- | --- |
| `progressing` | Closed beads, pushed commits, and validation passes moved in the window. | Continue the current swarm, but still use Beads and Agent Mail for ownership. |
| `converged_no_open_work` | No open or in-progress work remains. | Stop claiming new implementation work; file a new bead only for a concrete uncovered gap. |
| `quiet_blocked` | Open work exists but ready work is blocked. | Inspect dependencies with `br show <id> --json` and unblock the named source issue. |
| `coordination_degraded` | Agent Mail is red, corrupt, read-only, or missing. | Use Beads status/comments as the soft lock, keep file scope narrow, and record the exact Mail error. |
| `build_saturated` | RCH or validation broker pressure is high. | Stop launching heavyweight Cargo jobs; continue docs, source inspection, or non-heavy fixes until RCH recovers. |
| `stalled` | In-progress beads look stale and no useful progress is visible. | Review `br show`, comments, git history, and Agent Mail evidence before reopening; never reopen based on age alone. |
| `malformed_source_degraded` | A required source was malformed or contradictory. | Repair or regenerate the source artifact; do not act on the optimistic parts of the report. |
| `insufficient_evidence_degraded` | Required source data was missing, stale, or unsafe to emit. | Refresh source artifacts and rerun; treat the report as a blocker, not a pass. |

When the report should appear in Doctor or an operator runpack, pass the JSON
explicitly:

```bash
PI_SWARM_PROGRESS_SLO_JSON="$capture_dir/progress-slo.json" \
  pi doctor --only swarm --format json \
  | jq '.findings[] | select(.id == "progress_slo_current_posture")'

python3 scripts/build_swarm_operator_runpack.py \
  --capture-current \
  --capture-dir "$capture_dir/runpack" \
  --project-root /data/projects/pi_agent_rust \
  --agent-name "${AGENT_NAME:-agent}" \
  --progress-slo-json "$capture_dir/progress-slo.json" \
  --out-json "$capture_dir/operator-runpack.json" \
  --out-md "$capture_dir/operator-runpack.md"
```

Privacy boundaries:

- Store bead IDs, source IDs, schema names, counts, command labels, exit status,
  file paths, source hashes, and redaction summaries.
- Do not embed prompt bodies, provider transcripts, raw Agent Mail message
  bodies, bearer tokens, cookies, API keys, secrets, or full environment dumps.
- If a source reports `redacted`, `sensitive_omitted`, or `unsafe_to_emit`,
  keep the suppressed claim visible and avoid treating the missing raw data as
  green evidence.
- A progress SLO report is current only for its source window and source hashes.
  Rebuild it for a new handoff rather than carrying stale status forward.

## Fourth-Wave Self-Healing Workflow

Fourth-wave self-healing artifacts help operators choose the next safe action
when a swarm is noisy. They are dry-run guidance only. They do not claim beads,
reserve files, kill processes, quarantine extensions, regenerate evidence,
overwrite outputs, start or cancel RCH work, push commits, or authorize strict
drop-in release wording.

Use the workflow in this order:

1. Capture source facts: Beads, git status, Doctor swarm output, RCH status,
   cargo headroom, validation broker status when available, and any source
   evidence that the runpack will summarize.
2. Build dry-run diagnostics: stale-evidence renewal queue, optional budget
   lease simulation, optional extension quarantine rehearsal, runpack, autopilot
   input pack, autopilot plan, action plan, and work admission gate.
3. Read `work-admission-gate.json` before starting new work. If it says
   `wait`, `renew_evidence`, `pause_escalate`, or any non-admitting decision,
   stop admitting new implementation agents until a human operator either runs
   the named safe commands or explicitly records an override.
4. When the plan recommends a mutating real-world action, copy the command into
   the handoff as a proposed command, not an executed action. Require explicit
   human confirmation before changing Beads ownership, Agent Mail reservations,
   extension configuration, git refs, or evidence files.

Example capture that writes only new files under an empty capture directory:

```bash
capture_dir="/data/tmp/pi_swarm_fourth_wave/${AGENT_NAME:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$capture_dir"

br ready --json > "$capture_dir/beads-ready.json"
br list --status=in_progress --json > "$capture_dir/beads-in-progress.json"
git status --short --branch > "$capture_dir/git-status.txt"
pi doctor --only swarm --format json > "$capture_dir/doctor-swarm.json"
scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets \
  --decision-json "$capture_dir/cargo-admission.json"
rch status > "$capture_dir/rch-status.txt"
rch queue > "$capture_dir/rch-queue.txt"

python3 scripts/build_stale_evidence_renewal_queue.py \
  --source-root /data/projects/pi_agent_rust \
  --freshness-hours 336 \
  --max-items 25 \
  --out-json "$capture_dir/stale-evidence-renewal.json"

python3 scripts/build_swarm_operator_runpack.py \
  --capture-current \
  --capture-dir "$capture_dir/runpack-sources" \
  --project-root /data/projects/pi_agent_rust \
  --agent-name "$AGENT_NAME" \
  --stale-evidence-renewal-json "$capture_dir/stale-evidence-renewal.json" \
  --out-json "$capture_dir/operator-runpack.json" \
  --out-md "$capture_dir/operator-runpack.md" \
  --out-autopilot-input-pack-json "$capture_dir/autopilot-input-pack.json" \
  --out-autopilot-plan-json "$capture_dir/autopilot-plan.json" \
  --out-action-plan-json "$capture_dir/action-plan.json" \
  --out-work-admission-gate-json "$capture_dir/work-admission-gate.json"
```

Optional drills stay dry-run and should be captured beside the runpack:

```bash
python3 scripts/simulate_swarm_budget_leases.py \
  --fixture-id rch_saturation \
  --out-json "$capture_dir/budget-lease-simulation.json"

python3 scripts/rehearse_extension_quarantine.py \
  --fixture-id startup_crash_loop_quarantine \
  --out-json "$capture_dir/extension-quarantine-rehearsal.json"
```

Interpret the fourth-wave outputs conservatively:

| Output | Operator use | Boundary |
| --- | --- | --- |
| `action-plan.json` | Orders the next safest operator actions from captured sources. | Advisory only; commands require operator execution. |
| `work-admission-gate.json` | Decides whether to admit new implementation work, renew evidence, wait, or pause; its `dry_run_executor` classifies plan items as `would_execute`, `blocked`, `requires_operator`, or `never_execute`. | Fail-closed gate only; it does not enforce runtime throttles or mutate Beads, Agent Mail, RCH, git, or files. |
| `turn_pressure_ledger` in the runpack | Shows prompt, tool, provider, TUI, and session-write pressure without raw payload bodies. | Diagnostic only; not benchmark or release evidence. |
| `budget-lease-simulation.json` | Recommends fair per-agent budget allocation and reduced fanout under saturation. | Does not reserve capacity or mutate Agent Mail, Beads, RCH, or processes. |
| `extension-quarantine-rehearsal.json` | Rehearses quarantine or rollback decisions from fixture or captured extension facts. | Does not edit extension config or quarantine anything by itself. |
| `stale-evidence-renewal.json` | Lists stale, missing, contract-drifted, or RCH-blocked evidence and bounded renewal commands. | Does not regenerate or overwrite evidence and does not weaken the drop-in claim gate. |
| Handoff summaries | Give the next operator redacted source status, selected advisory action, and blocked/degraded reasons. | They are not source-of-truth evidence and do not replace Beads, Agent Mail, Doctor, RCH, CI, UBS, or release gates. |

The dry-run executor is an admission proof, not an executor. It may mark
read-only probes as `would_execute`, but Beads ownership changes, artifact
writes, and other mutating commands stay `requires_operator`. Agent Mail
mutation, RCH execution or mutation, local heavyweight Cargo, deletion requests,
and Beads ownership bypasses are `never_execute` entries with stable reason
codes.

Claim boundaries are unchanged: claim through Beads, reserve through Agent Mail
when healthy, and use Beads comments as the soft lock when Mail is corrupt or
read-only. A work admission gate can recommend `use_beads_soft_lock`, but it
cannot prove that unreserved files are free. Operators still inspect `br show`,
recent comments, `git log -- <file>`, and the dirty worktree before touching a
file family.

Stop admitting new implementation work when any of these are true:

- `work-admission-gate.json` has `admit_new_implementation=false`.
- `action-plan.json` selects `renew_stale_evidence`, `wait_for_pressure`, or
  `pause_or_surface_blocker`.
- `stale-evidence-renewal.json` lists blocked or renewal-required items that
  are needed for the claim being made.
- The extension rehearsal recommends quarantine or rollback and no human
  operator has approved the actual config change.
- The budget lease simulation reports saturation for the resource class needed
  by the next agent or heavy validation command.

Safe handoff wording:

```text
Fourth-wave artifacts are advisory dry-run outputs. Next recommended action:
<decision from action-plan/work-admission-gate>. Proposed commands require
explicit operator execution. No Beads, Agent Mail, extension config, git refs,
evidence files, RCH jobs, or release claims were mutated by these artifacts.
```

The fourth-wave closeout gate emits
`pi.swarm.fourth_wave_self_healing.closeout_gate.v1`, governed by
`docs/contracts/fourth-wave-self-healing-closeout-gate-contract.json`; the
current artifact is
`docs/evidence/fourth-wave-self-healing-closeout-gate.json`. The gate maps each
`bd-63x3v.7` child bead to source paths, docs/contracts/evidence, validation
commands, pushed refs, and advisory claim boundaries before the roadmap can be
closed.

## Throttle Or Pause

Back off new claims when any of these are true:

| Signal | Command | Action |
|--------|---------|--------|
| RCH admission denies or backs off | `scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets` | Stop starting heavy cargo jobs. Continue docs, source inspection, or small non-cargo fixes. |
| Local cargo/rustc process pressure is high | `scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets` | Wait for local process pressure to fall, or use `--force-admit` only for an explicitly approved override. |
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
2. Try the MCP registration/read path: `macro_start_session` or
   `register_agent`, then `fetch_inbox` or `list_agents`. Keep the exact
   health error, for example `database schema missing required tables`.
3. Try the narrow reservation write once with `file_reservation_paths`. If
   writes fail because Mail is red, read-only, or schema-corrupt, do not require
   Agent Mail reservations before coding.
4. Use Beads as the coordination record:

   ```bash
   br show <issue-id> --json
   br update <issue-id> --status in_progress --assignee "$AGENT_NAME"
   ```

5. Keep work on a narrow file set and mention the degraded Mail state in the
   final handoff.
6. Close out through Beads and git:

   ```bash
   br close <issue-id> --reason "Completed with Agent Mail unavailable; Beads used as soft lock"
   br sync --flush-only
   git add .beads/ <changed-files>
   git commit -m "<summary>"
   git push origin main
   ```

Final handoff wording should include: `Agent Mail unavailable: <exact error>;
Beads assignee/status used as soft lock; reservations/messages were not trusted
for this bead.`

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

python3 scripts/plan_semantic_validation_route.py \
  --from-git \
  --source-bead <issue-id> \
  --pretty \
  --out "$capture_dir/semantic-route-plan.json"

python3 scripts/build_swarm_operator_runpack.py \
  --capture-current \
  --capture-dir "$capture_dir" \
  --project-root /data/projects/pi_agent_rust \
  --agent-name "$AGENT_NAME" \
  --semantic-route-plan-json "$capture_dir/semantic-route-plan.json" \
  --progress-slo-json "$capture_dir/progress-slo.json" \
  --out-json "$capture_dir/operator-runpack.json" \
  --out-md "$capture_dir/operator-runpack.md" \
  --out-predictive-telemetry-ledger-json "$capture_dir/predictive-telemetry-ledger.json" \
  --out-validation-scheduler-plan-json "$capture_dir/validation-scheduler-plan.json" \
  --out-autopilot-input-pack-json "$capture_dir/autopilot-input-pack.json" \
  --out-autopilot-plan-json "$capture_dir/autopilot-plan.json"
```

The runpack schema is governed by `docs/contracts/swarm-operator-runpack-contract.json`. The runpack is a redacted index over existing evidence, not a release performance claim and not a replacement for the source artifacts.
The semantic route plan schema is `pi.validation.semantic_route_plan.v1`. Generate it with `scripts/plan_semantic_validation_route.py --from-git --source-bead <issue-id> --out "$capture_dir/semantic-route-plan.json"` and pass it to the runpack with `--semantic-route-plan-json`. The route is advisory only: it summarizes changed-path buckets, proof-memory/cache heat, RCH-backed command templates, coordination admission, and coalescing order, but operators must still claim through Beads, reserve through Agent Mail when healthy, and run heavyweight Cargo validation through RCH. It must not execute commands, launch RCH, mutate Beads, mutate Agent Mail, mutate git, delete files, skip validation, or support release, benchmark, capacity, performance, strict drop-in, or claim-readiness assertions.
The predictive telemetry ledger schema is governed by `docs/contracts/predictive-swarm-telemetry-ledger-contract.json`; checked-in advisory fixture evidence lives at `docs/evidence/predictive-swarm-telemetry-ledger.json`. It ranks validation, coordination, work-queue, turn-context, bottleneck-source, and evidence-freshness pressure from existing runpack signals only, and it must not be used as release performance, capacity, Agent Mail, RCH, scheduler, Beads, git, or claim-readiness authority.
The validation scheduler plan schema is governed by `docs/contracts/validation-scheduler-plan-contract.json`; checked-in advisory fixture evidence lives at `docs/evidence/validation-scheduler-plan.json`. It ranks exact script and RCH-backed cargo command strings from the runpack's git, predictive telemetry, RCH admission, remote proof, and target-cache signals. It is read-only: it does not execute cargo, reserve workers, mutate Agent Mail or Beads, delete temp artifacts, or permit heavy cargo to fall back to local execution when RCH is unavailable.
The autopilot input pack schema is governed by `docs/contracts/swarm-autopilot-input-pack-contract.json`. It normalizes source statuses for the dry-run planner, but it is still advisory and never replaces Doctor, Beads, Agent Mail, RCH, git, or the source artifacts themselves.
The autopilot plan schema is governed by `docs/contracts/swarm-autopilot-plan-contract.json`. It maps the input pack to ordered dry-run actions such as `claim_ready_bead`, `wait_for_rch`, `adjust_swarm_budget`, `use_beads_soft_lock`, `reopen_stale_bead_candidate`, `run_docs_only_work`, `capture_handoff`, or `stop_and_surface_blocker`.
When the command emits the companion input pack and plan, the runpack also includes `autopilot_handoff` with schema `pi.swarm.autopilot_handoff.v1`. That section names the input-pack and plan schemas, artifact paths, selected advisory action, and source provenance so a new agent can inspect one handoff bundle without treating the runpack as a new source of truth.
Before relying on a handoff bundle, run `python3 scripts/check_swarm_runpack_freshness.py "$capture_dir/operator-runpack.json" --source-root /data/projects/pi_agent_rust`. The freshness guard is read-only and fails closed when the runpack or closeout-style evidence cites missing, placeholder, hash-mismatched, newer, or stale source artifacts.
For closeout evidence triage, run `python3 scripts/check_closeout_gate_freshness.py --operator-summary markdown` after the freshness audit exists. The summary groups current-artifact, missing-contract, stale-source, missing-commit, hash-drift, README-drift, and malformed-source failures, then ranks read-only inspection commands and Beads-only refresh ownership guidance. It is advisory operator context only; it does not replace the freshness JSON, Beads, Agent Mail, RCH, git, source artifacts, UBS, or claim-integrity gates.
The plan also includes `work_partitions` for ready Beads. Those entries recommend reservation globs, likely collision surfaces to avoid, alternate file families, confidence, and degraded caveats. They are diagnostic only; operators still claim through Beads and reserve through Agent Mail when it is healthy.
The input pack and plan also carry `budget_drift` evidence with schema `pi.swarm.budget_drift.v1`. It compares the last accepted swarm resource preflight profile with live cgroup, memory, scratch-path, RCH queue, and active-owner observations. Status `stable` keeps the current ceiling, `degraded` recommends reduced fanout with hysteresis, and `deny_new_work` recommends admitting no new agents or heavyweight RCH verification until the live signals recover.
The plan also includes `failure_actions` for common operational blockers. Those entries use stable catalog IDs for RCH artifact retrieval, local Cargo target/TMPDIR pressure, remote compiler failures, Agent Mail schema/read-only degradation, Beads JSONL drift, stale Beads ownership, and unknown operational failures. Unknown entries fail closed with a redacted raw excerpt and safe inspection commands instead of guessing a root cause.
The work-admission gate includes `dry_run_executor` with schema `pi.swarm.work_admission_dry_run_executor.v1`. It consumes the autopilot plan plus Beads/RCH/Agent Mail/git/headroom signals, classifies read-only probes as `would_execute`, routes source-of-truth mutations to `requires_operator`, blocks unsafe admission with explicit reasons, and permanently rejects deletion requests, Agent Mail mutation, RCH execution/mutation, local heavyweight Cargo, and Beads ownership bypasses as `never_execute`.
The no-mock autopilot E2E harness emits `pi.swarm.autopilot_e2e.v1` plus `pi.swarm.autopilot_e2e.event.v1` JSONL events. It uses temp Beads and temp git workspaces where safe, fixture-captured degraded Agent Mail and RCH inputs where live mutation would be unsafe, and verifies healthy claim, empty queue, deletion-request rejection, Beads soft-lock fallback, saturated RCH, stale bead review, unrelated dirty worktree, and malformed-source fail-closed scenarios. This is operator admission evidence only; it is not a release speed, drop-in, or benchmark claim.
The final closeout gate emits `pi.swarm.autopilot_decision_gate.v1`, governed by `docs/contracts/swarm-autopilot-decision-gate-contract.json`. It compares the shipped input pack, planner, work partitions, failure-action catalog, budget drift watcher, E2E/logging evidence, runpack handoff, safety guards, pushed commits, and quality gates to the prompt-to-artifact checklist. A failed gate emits `follow_up_beads` and `decision=file_follow_up_beads_before_closing_epic`; a passing gate is still only closeout evidence over Beads, git, RCH, Doctor, Agent Mail, and source artifacts, not a new source of truth.
The adaptive-execution closeout gate emits `pi.swarm.adaptive_execution.closeout_gate.v1`, governed by `docs/contracts/adaptive-execution-closeout-gate-contract.json`; the current artifact is `docs/evidence/adaptive-execution-closeout-gate.json`. It is advisory closeout evidence only and does not replace Beads, git, RCH, Agent Mail, CI, UBS, release certification, or source files.
The extension-compatibility closeout gate emits `pi.ext.compatibility_closeout_gate.v1`, governed by `docs/contracts/extension-compatibility-closeout-gate-contract.json`; the current artifact is `docs/evidence/extension-compatibility-closeout-gate.json`. It is advisory closeout evidence only and does not replace extension conformance runs, Beads, git, RCH, Agent Mail, CI, UBS, release certification, or source files.
The swarm-replay closeout gate emits `pi.swarm.replay_closeout_gate.v1`, governed by `docs/contracts/swarm-replay-closeout-gate-contract.json`; the current artifact is `docs/evidence/swarm-replay-closeout-gate.json`. It is advisory closeout evidence only and does not replace replay fixtures, Beads, git, RCH, Agent Mail, CI, UBS, release certification, or source files.
The context-intelligence closeout gate emits `pi.context_intelligence.closeout_gate.v1`, governed by `docs/contracts/context-intelligence-closeout-gate-contract.json`. It maps each `bd-ircr3` child bead to code, tests, docs or evidence, commands, close reasons, and commit hashes; then it checks graph contracts, graph builder, freshness and claim gates, bundle planner, redaction and invalidation, preview surface, prompt injection, no-mock E2E, performance budgets, Doctor/runpack posture, operator docs, README freshness, pushed commits, staged UBS, and Beads ledger reconciliation. A passing context gate is closeout evidence only and does not replace Beads, git, RCH, Doctor, runpacks, or source files.
The validation-broker closeout gate emits `pi.validation_broker.closeout_gate.v1`, governed by `docs/contracts/validation-broker-closeout-gate-contract.json`; the current artifact is `docs/evidence/validation-broker-closeout-gate.json`. It maps each `bd-gusp4` implementation child bead to code, tests, docs or evidence, commands, close reasons, and commit hashes; then it checks source-boundary contracts, lease storage, source normalization, admission policy, CLI lease flow, fault corpus, Doctor/runpack projection, no-mock E2E coverage, stress-budget evidence, operator docs, README freshness, pushed commits, staged UBS, and Beads ledger reconciliation. A passing validation-broker gate is closeout evidence only and does not replace Beads, git, RCH, Doctor, runpacks, Agent Mail, CI, UBS, `cargo_headroom.sh`, or source files.
The progress-SLO closeout gate emits `pi.swarm.progress_slo.closeout_gate.v1`, governed by `docs/contracts/swarm-progress-slo-closeout-gate-contract.json`; the current artifact is `docs/evidence/swarm-progress-slo-closeout-gate.json`. It maps each `bd-wzri8` implementation child bead to code, tests, docs or evidence, commands, close reasons, and commit hashes; then it checks the progress-SLO contract, deterministic evaluator, read-only CLI, Doctor/runpack projection, no-mock E2E evidence, synthetic stress budgets, operator docs, README freshness, pushed commits, staged UBS, Beads ledger reconciliation, and source-boundary checks. A passing progress-SLO gate is closeout evidence only and does not replace Beads, git, RCH, Doctor, runpacks, Agent Mail, CI, UBS, claim-integrity gates, or source files.
The runtime-intelligence closeout gate emits `pi.runtime_intelligence.closeout_gate.v1`, governed by `docs/contracts/runtime-intelligence-closeout-gate-contract.json`; the current artifact is `docs/evidence/runtime-intelligence-closeout-gate.json`. It maps each `bd-h66tp` implementation child bead to code, tests, docs or evidence, commands, close reasons, and commit hashes; then it checks compaction admission, tool-output artifacts, provider routing, scheduler fairness, frame-budget telemetry, cancellation cleanup, extension safety provenance, docs/evidence, source boundaries, pushed refs, staged UBS, Beads ledger reconciliation, and RCH-backed quality gates. A passing runtime-intelligence gate is closeout evidence only and does not replace Beads, git, RCH, Doctor, runpacks, Agent Mail, CI, UBS, claim-integrity gates, or source files.
The proof-carrying swarm test-fabric closeout gate emits `pi.swarm.proof_carrying_test_fabric.closeout_gate.v1`, governed by `docs/contracts/proof-carrying-swarm-test-fabric-closeout-gate-contract.json`; the current artifact is `docs/evidence/proof-carrying-swarm-test-fabric-closeout-gate.json`. It maps each `bd-zeccr` implementation child bead to source paths, tests or fixtures, evidence artifacts, validation commands, close reasons, pushed commits, and negative controls; then it checks no-mock lifecycle E2E, cross-surface conformance, operator evidence goldens, structure-aware fuzz/property coverage, metamorphic replay equivalence, source boundaries, pushed refs, staged UBS, Beads ledger reconciliation, and RCH-backed quality gates. A passing proof-carrying test-fabric gate is closeout evidence only and does not replace Beads, git, RCH, Agent Mail, UBS, CI, claim-integrity gates, child evidence, or source files.
The predictive-operations closeout gate emits `pi.swarm.predictive_operations.closeout_gate.v1`, governed by `docs/contracts/predictive-operations-closeout-gate-contract.json`; the current artifact is `docs/evidence/predictive-operations-closeout-gate.json`. It maps each `bd-63x3v.11` implementation child bead to source paths, tests or fixtures, evidence artifacts, validation commands, close reasons, pushed commits, and claim-boundary text; then it checks predictive telemetry fusion, validation scheduling, semantic compaction quality, hostcall cost attribution, operator-perceived latency, redundant-agent-work detection, source boundaries, pushed refs, staged UBS, Beads ledger reconciliation, and untracked follow-ups. A passing predictive-operations gate is closeout evidence only and does not replace Beads, git, RCH, Agent Mail, UBS, CI, claim-integrity gates, child evidence, generated target/perf outputs, or source files.
The ninth-wave incident replay and proof-memory closeout gate emits `pi.swarm.incident_replay_proof_memory.closeout_gate.v1`, governed by `docs/contracts/ninth-wave-incident-replay-proof-memory-closeout-gate-contract.json`; the current artifact is `docs/evidence/ninth-wave-incident-replay-proof-memory-closeout-gate.json`. It maps each `bd-9yq7i` child bead to source paths, tests or fixtures, evidence artifacts, validation commands, close reasons, pushed commits, negative controls, and claim-boundary text; then it checks incident corpus, incident replay, validation proof memory, operator work recommendation, operator smoothness SLO, extension resource firewall matrix, incident replay E2E, source boundaries, pushed refs, staged UBS, Beads ledger reconciliation, and untracked follow-ups. A passing ninth-wave gate is closeout evidence only and does not replace Beads, git, RCH, Agent Mail, UBS, CI, claim-integrity gates, child evidence, generated target/perf outputs, prior-wave evidence, or source files.
The operator-perceived latency trace emits `pi.operator.perceived_latency_trace.v1`, governed by `docs/contracts/operator-perceived-latency-trace-contract.json`; the current fixture artifact is `docs/evidence/operator-perceived-latency-trace.json`. It joins provider-stream, RPC-output, TUI-frame, tool-update, and operator-visible semantic milestones while proving low-value coalescing does not hide semantic output. The trace is advisory fixture evidence only and does not replace provider/RPC/TUI backpressure evidence or authorize benchmark, capacity, release performance, or strict drop-in claims.
The operator smoothness SLO emits `pi.operator.smoothness_slo.v1`, governed by `docs/contracts/operator-smoothness-slo-contract.json`; the current fixture artifact is `docs/evidence/operator-smoothness-slo.json`. It covers provider stream deltas, RPC output pressure, TUI frame rendering, tool-update coalescing, and session-write pressure with deterministic p50/p95/p99 visibility counters, semantic milestone counts, backlog budgets, failure logs, and fail-closed controls for delayed visibility, non-monotonic timelines, runaway frame backlog, and missing surface coverage. The SLO is advisory engineering fixture evidence only and does not replace focused surface tests or authorize benchmark, capacity, release performance, strict drop-in, runtime mutation, RCH, cargo, git, or Beads claims.
The extension resource firewall matrix emits `pi.ext.resource_firewall_matrix.v1`, governed by `docs/contracts/extension-resource-firewall-matrix-contract.json`; focused `extensions_stress` runs write `resource_firewall_matrix.json` under target/perf. It covers cheap-read flood, large payload emission, denied capability churn, slow hostcall, repeated failure, and steady-peer progress rows with budgets, observed counters, admission decisions, denial modes, fallback behavior, payload redaction, capability-boundary preservation, and fail-closed negative controls for missing counters, missing peer progress, and unredacted payload bodies. The matrix is advisory stress evidence only and does not replace runtime enforcement, hostcall cost attribution, RCH validation, Agent Mail, Beads, UBS, CI, or benchmark/capacity/release claims.
The swarm incident corpus emits `pi.swarm.incident_corpus.v1`, governed by `docs/contracts/swarm-incident-corpus-contract.json`; the current fixture artifact is `docs/evidence/swarm-incident-corpus.json`. It captures deterministic operator incidents for Agent Mail schema corruption, RCH saturation/local-fallback denial, stale evidence, duplicate work risk, dirty worktree admission denial, malformed source artifacts, and deletion or live-mutation rejection, plus fail-closed negative controls for missing sources, unsafe unredacted bodies, contradictory status, and unsafe authorization attempts. The corpus is advisory fixture evidence only and does not replace release performance, drop-in certification, Agent Mail, RCH, Beads, git, source artifacts, or destructive-action authority.
The swarm incident replay harness emits `pi.swarm.incident_replay.v1`, governed by `docs/contracts/swarm-incident-replay-contract.json`; the current fixture artifact is `docs/evidence/swarm-incident-replay.json`. It consumes the incident corpus and reconstructs source capture, Agent Mail degradation, RCH admission, Beads ownership, dirty worktree state, validation outcome, and final recommendation phases with per-step assertions and redacted excerpts. Negative controls fail closed for out-of-order events, missing sources, unredacted sensitive content, and replay output being treated as source-of-truth authority. Replay is advisory fixture evidence only and does not replace live Agent Mail, RCH, Beads, git, source artifacts, or destructive-action authority.
The swarm incident replay E2E harness emits `pi.swarm.incident_replay_e2e.v1`, governed by `docs/contracts/swarm-incident-replay-e2e-contract.json`; the current fixture artifact is `docs/evidence/swarm-incident-replay-e2e.json` with JSONL events in `docs/evidence/swarm-incident-replay-e2e-events.jsonl`. It combines real temporary Beads and git workspaces with fixture-captured degraded Agent Mail/RCH inputs to exercise healthy replay, Beads soft-lock fallback, RCH proof refresh backoff, duplicate-work risk, dirty-worktree denial, stale proof-memory refresh, extension resource firewall failure, and smoothness SLO failure. The E2E artifact is advisory operator evidence only and does not authorize live source mutation, local heavyweight Cargo fallback, release, benchmark, capacity, or drop-in claims.
The validation proof-memory index emits `pi.validation.proof_memory_index.v1`, governed by `docs/contracts/validation-proof-memory-index-contract.json`; the current fixture artifact is `docs/evidence/validation-proof-memory-index.json`. It classifies reusable, stale, missing-artifact, local-fallback, dirty-worktree mismatch, command-mismatch, path-coverage mismatch, and non-authoritative validation proof entries from checked remote-validation proof fixtures. Proof memory is advisory fixture evidence only and does not skip validation or replace RCH, Agent Mail, Beads, git, source artifacts, or claim-integrity gates.

### Validation Broker Operator Workflow

The validation broker is an advisory coordination aid for expensive validation
work. It helps agents decide whether to run a gate now, wait for an active slot,
reuse equivalent evidence, narrow the command, or recover stale slots. It does
not claim beads, reserve files, schedule RCH jobs, waive CI, or turn stale data
into a green validation result.
In short: it does not claim beads, does not replace RCH, and does not skip
required gates.

Use the broker only after the normal ownership checks are visible:

1. Check Beads for actionable work and stale ownership with `br ready --json`,
   `br show <id> --json`, and `br list --status=in_progress --json`.
2. Reserve files through Agent Mail when the Mail DB is healthy. If Mail is
   red, read-only, or schema-corrupt, use the Beads assignee as the soft lock
   and record the Mail blocker in the bead or handoff.
3. Run `pi doctor --only swarm --format json` and
   `scripts/cargo_headroom.sh --admit-only ...` before heavyweight gates so
   scratch-space, cgroup, CPU, memory, and RCH posture remain explicit.
4. Ask the broker for a plan before launching duplicate or broad validation
   commands. Treat the result as advice, not permission to skip required gates.

Typical read-only status capture:

```bash
pi validation-broker status \
  --store "$PI_VALIDATION_BROKER_STORE" \
  --format json \
  --out-json "$capture_dir/validation-broker-status.json"
```

Typical plan request:

```bash
pi validation-broker plan \
  --request "$capture_dir/validation-request.json" \
  --inputs "$capture_dir/validation-inputs.json" \
  --store "$PI_VALIDATION_BROKER_STORE" \
  --policy "$capture_dir/validation-policy.json" \
  --format json \
  --out-json "$capture_dir/validation-broker-plan.json"
```

Interpret decisions conservatively:

| Decision | Operator action |
| --- | --- |
| `allow` | Run the requested gate through the declared runner and still record the actual command result. |
| `wait` | Do not launch a duplicate heavyweight gate; wait for the active owner or ask for an update. |
| `coalesce` | Reuse only the named artifacts whose command, git head, target/TMPDIR, runner, feature flags, and hashes match the request. |
| `narrow` | Replace the broad command with the broker's narrower required action, then validate that narrower scope honestly. |
| `deny_local_fallback` | Do not let an RCH-required command fail open into a local build. Surface the RCH or headroom blocker. |
| `stale_recover` | Mark the stale slot visibly, open a non-overlapping slot or rerun after provenance mismatch, and do not kill processes. |
| `degraded_block` | Stop and surface the missing, stale, malformed, or unavailable source rows. |

Acquire, renew, and release mutate only the append-only slot store:

```bash
pi validation-broker acquire \
  --request "$capture_dir/validation-request.json" \
  --store "$PI_VALIDATION_BROKER_STORE" \
  --started-at "$started_at_utc" \
  --expires-at "$expires_at_utc"

pi validation-broker renew \
  --store "$PI_VALIDATION_BROKER_STORE" \
  --slot-id "$slot_id" \
  --owner "$AGENT_NAME" \
  --heartbeat-at "$heartbeat_at_utc" \
  --expires-at "$expires_at_utc"

pi validation-broker release \
  --store "$PI_VALIDATION_BROKER_STORE" \
  --slot-id "$slot_id" \
  --owner "$AGENT_NAME" \
  --at "$released_at_utc" \
  --reason "gate completed and artifacts recorded"
```

The broker's reusable-evidence path is fail-closed. Reuse is valid only when
the broker names the slot and its provenance matches the current request. A
similar command from another git head, target directory, TMPDIR, runner,
feature set, dirty-path scope, or artifact hash is a rejected reusable slot, not
a pass.

Privacy and redaction boundaries:

- Broker status, plan, runpack, and autopilot summaries should carry schema IDs,
  source availability, source hashes, degraded reasons, and bounded excerpts
  instead of raw prompt bodies, mailbox tokens, command logs, or secrets.
- Dynamic paths, PIDs, ports, timestamps, durations, long numeric IDs, and hex
  IDs should be normalized before blocker fingerprints are compared across
  agents.
- Agent Mail health and reservation facts are coordination evidence only. If
  Mail is unavailable, do not infer that nobody owns a file; fall back to Beads
  and visible handoff notes.
- Synthetic stress artifacts such as
  `docs/evidence/validation-broker-stress-budgets.json` are engineering budget
  evidence only. They are not release performance evidence and do not support
  README speed, capacity, or strict drop-in claims.

Validation broker troubleshooting:

| Symptom | Operator response |
| --- | --- |
| Agent Mail schema-corrupt, red, or read-only | Use Beads assignee and visible handoff notes as the soft lock; do not infer absent reservations and do not wait in coordination purgatory. |
| RCH-required gate would fail open locally | Treat `deny_local_fallback` as a hard blocker, surface the RCH status or queue evidence, and rerun only when remote execution is available. |
| Scratch-space, target-dir, or TMPDIR headroom is low | Run the documented cargo headroom preflight, switch to an isolated high-capacity target/TMPDIR pair when allowed, and do not launch broad gates until headroom is explicit. |
| Slot store is missing, malformed, or unavailable | Treat the broker posture as degraded, avoid coalescing evidence, and record the malformed source path or missing artifact in the handoff. |
| Reusable artifact provenance does not match | Reject reuse and run the required gate for the current command, git head, runner, features, target directory, TMPDIR, and artifact hash. |

These commands remain mandatory before commit when code changed, even when a
broker plan says `allow` or `coalesce`:

```bash
cargo fmt --check
git diff --check
rch exec -- cargo check --all-targets
rch exec -- cargo clippy --all-targets -- -D warnings
ubs --staged --only=rust .
./scripts/reconcile_beads_ledger.sh
```

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
# Mirror the legacy compatibility branch per AGENTS.md after pushing main.
git status --short --branch
```

For docs-only changes, use docs-focused validation instead of forcing cargo:

```bash
command -v git br bv rch cargo jq python3
python3 scripts/build_swarm_operator_runpack.py --self-test
python3 scripts/check_swarm_runpack_freshness.py --self-test
python3 scripts/check_swarm_runpack_freshness.py --run-runpack-smoke
python3 scripts/report_empty_queue_convergence.py --self-test
e2e_dir="/data/tmp/pi_swarm_autopilot_e2e/${AGENT_NAME:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
python3 scripts/build_swarm_operator_runpack.py \
  --run-autopilot-e2e \
  --capture-dir "$e2e_dir" \
  --out-autopilot-e2e-json "$e2e_dir/summary.json" \
  --out-autopilot-e2e-events-jsonl "$e2e_dir/events.jsonl"
python3 -m json.tool docs/contracts/swarm-operator-runpack-contract.json >/dev/null
python3 -m json.tool docs/contracts/validation-scheduler-plan-contract.json >/dev/null
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

Degraded-coordination runpack no-mock E2E evidence:

```bash
e2e_dir="/data/tmp/pi_swarm_degraded_coordination_e2e/${AGENT_NAME:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
python3 scripts/build_swarm_operator_runpack.py \
  --run-degraded-coordination-e2e \
  --capture-dir "$e2e_dir" \
  --out-degraded-coordination-e2e-json "$e2e_dir/summary.json" \
  --out-degraded-coordination-e2e-events-jsonl "$e2e_dir/events.jsonl"
```

The summary emits `pi.swarm.degraded_coordination_runpack_e2e.v1` and JSONL
events with `pi.swarm.degraded_coordination_runpack_e2e.event.v1`. The scenario
uses a real temporary Beads workspace for one fresh in-progress bead and one
blocked open bead, fixture-captured Agent Mail semantic-readiness failure, and
an RCH worker workspace-shadow blocker. A passing verdict means the runpack
recommends Beads soft-lock ownership, keeps validation degraded instead of
green, and emits no cleanup or deletion commands for temp artifacts.

Incident replay E2E evidence:

```bash
e2e_dir="/data/tmp/pi_swarm_incident_replay_e2e/${AGENT_NAME:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
python3 scripts/build_swarm_operator_runpack.py \
  --run-swarm-incident-replay-e2e \
  --capture-dir "$e2e_dir" \
  --out-swarm-incident-replay-e2e-json "$e2e_dir/summary.json" \
  --out-swarm-incident-replay-e2e-events-jsonl "$e2e_dir/events.jsonl"
```

The summary emits `pi.swarm.incident_replay_e2e.v1` and JSONL events with
`pi.swarm.incident_replay_e2e.event.v1`. It uses real temp Beads and git
workspaces where safe, fixture-captured Agent Mail/RCH failures where live
mutation would be unsafe, checked-in incident replay/proof-memory/operator-work
sources, and extension firewall plus smoothness-SLO failure evidence. A passing
verdict proves the harness fails closed to explicit operator actions and does
not authorize cleanup commands, local heavyweight Cargo fallback, release,
benchmark, capacity, strict drop-in, or live source-system claims.

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

Validation-broker final closeout-gate evidence:

```json
{
  "schema": "pi.validation_broker.closeout_gate.v1",
  "purpose": "prompt_to_artifact_validation_broker_closeout_gate_not_source_of_truth",
  "status": "pass",
  "required_checks": [
    "child_beads_closed",
    "contract_and_source_inventory",
    "lease_store_schema",
    "source_normalization",
    "admission_policy",
    "cli_surface",
    "fault_corpus_stale_recovery",
    "doctor_runpack",
    "no_mock_e2e",
    "stress_budgets",
    "operator_docs_privacy",
    "readme_freshness",
    "source_boundaries",
    "pushed_commits",
    "quality_gates"
  ],
  "missing_checks": [],
  "remaining_follow_ups": [],
  "follow_up_required": false,
  "follow_up_beads": [],
  "decision": "close_final_gate_and_parent_epic",
  "epic_can_close_after_this_commit": true
}
```

Progress-SLO final closeout-gate evidence:

```json
{
  "schema": "pi.swarm.progress_slo.closeout_gate.v1",
  "purpose": "prompt_to_artifact_swarm_progress_slo_closeout_gate_not_source_of_truth",
  "status": "pass",
  "required_checks": [
    "child_beads_closed",
    "contract_and_source_inventory",
    "deterministic_evaluator",
    "cli_surface",
    "doctor_runpack_projection",
    "no_mock_e2e",
    "stress_budgets",
    "operator_docs_privacy",
    "readme_freshness",
    "source_boundaries",
    "pushed_commits",
    "quality_gates"
  ],
  "missing_checks": [],
  "remaining_follow_ups": [],
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
