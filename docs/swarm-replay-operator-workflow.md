# Swarm Replay Operator Workflow

Purpose: Explain how operators and developers should capture, preview, and interpret offline swarm replay evidence without treating it as live coordination truth or release evidence.

This guide covers the replay lab shipped under `bd-in57w`: the read-only trace ingestor in `src/swarm_replay.rs`, the `pi swarm-replay-preview` CLI surface, the operator runpack integration, and the no-mock E2E evidence harness. It is written for multi-agent operators who need to understand what happened in a swarm, compare advisory policies, and hand off the next safe action without mutating Beads, Agent Mail, git, RCH, or live build slots.

## What Replay Is

Swarm replay is an offline analysis tool over captured coordination artifacts.

It can:

- Normalize Beads, Agent Mail archive snapshots, reservation records, RCH queue/status facts, runpack handoff data, git status, validation artifacts, activity ledger rows, and flight recorder rows into a `pi.swarm.replay_trace.v1` trace.
- Replay those events into deterministic snapshots.
- Evaluate built-in advisory policies over those snapshots.
- Emit JSON, text, comparison, manifest, and JSONL event evidence for audit.
- Feed a replay preview into `scripts/build_swarm_operator_runpack.py` so a handoff bundle can show policy comparison context.

It cannot:

- Claim or reopen Beads.
- Send Agent Mail messages or reserve files.
- Cancel, start, or prioritize RCH jobs.
- Stage, commit, push, stash, reset, clean, or edit git state.
- Replace `pi doctor --only swarm`, Beads, Agent Mail, RCH, CI, or release evidence gates.
- Prove release-facing performance, strict drop-in certification, or live swarm readiness.

Treat replay output as reproducible operator evidence. Use source systems for authority.

## Source Boundaries

| Question | Source of truth | Replay role |
|----------|-----------------|-------------|
| Who owns a task now? | `br show`, `br list --status=in_progress --json` | Shows what the captured trace observed at capture time. |
| Are reservations active? | Agent Mail file reservations when Mail is healthy | Shows captured reservations and conflicts, including degraded or missing-Mail evidence. |
| Is RCH saturated now? | `rch status`, `rch queue`, `scripts/cargo_headroom.sh --runner rch --admit-only ...` | Shows captured queue pressure and advisory policy deltas. |
| Is it safe to make release claims? | Claim-integrity, certification, and perf evidence gates | Never authoritative for release claims. |
| What should the next operator inspect? | Beads, Doctor, Agent Mail, RCH, git, and runpack source artifacts | Ranks advisory policies and highlights missing data. |

## Capture Inputs

For a full operator handoff, capture current source facts first:

```bash
capture_dir="/data/tmp/pi_swarm_replay/${AGENT_NAME:-agent}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$capture_dir"

br list --json > "$capture_dir/beads.json"
br ready --json > "$capture_dir/beads-ready.json"
git status --short --branch > "$capture_dir/git-status.txt"
rch status > "$capture_dir/rch-status.txt"
rch queue > "$capture_dir/rch-queue.txt"

pi doctor --only swarm --format json > "$capture_dir/doctor-swarm.json"
scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets \
  --decision-json "$capture_dir/cargo-admission.json"
```

Agent Mail may be unavailable or partially degraded. If MCP writes fail, keep whatever read-only evidence is available: inbox snapshots, agent lists, reservation export files, or the Doctor swarm finding. Do not invent a green Mail state to make replay look complete.

When producing runpack evidence, keep replay artifacts beside the runpack capture:

```bash
python3 scripts/build_swarm_operator_runpack.py \
  --capture-current \
  --capture-dir "$capture_dir/runpack" \
  --project-root /data/projects/pi_agent_rust \
  --agent-name "${AGENT_NAME:-agent}" \
  --out-json "$capture_dir/operator-runpack.json" \
  --out-md "$capture_dir/operator-runpack.md"
```

The runpack is a redacted index over source artifacts, not a new source of truth.

## Preview A Trace

Use the checked-in golden trace when validating the CLI surface:

```bash
pi swarm-replay-preview \
  --trace tests/golden_corpus/swarm_replay_trace/normalized_trace.json \
  --format json
```

Write reproducible preview artifacts with explicit output paths:

```bash
pi swarm-replay-preview \
  --trace tests/golden_corpus/swarm_replay_trace/normalized_trace.json \
  --policy conservative_manual \
  --policy rch_fanout_limited \
  --policy build_slot_protective \
  --out-json "$capture_dir/swarm-replay-preview.json" \
  --out-text "$capture_dir/swarm-replay-preview.txt"
```

The preview command refuses to overwrite requested output files. Pick a fresh capture directory or remove stale scratch artifacts yourself only when you have explicit permission to delete them.

Feed the preview into the runpack only after the JSON exists:

```bash
python3 scripts/build_swarm_operator_runpack.py \
  --capture-current \
  --capture-dir "$capture_dir/runpack" \
  --project-root /data/projects/pi_agent_rust \
  --agent-name "${AGENT_NAME:-agent}" \
  --swarm-replay-preview-json "$capture_dir/swarm-replay-preview.json" \
  --out-json "$capture_dir/operator-runpack.json" \
  --out-md "$capture_dir/operator-runpack.md"
```

The runpack projects the preview summary for handoff. It does not replace the replay trace, the policy report, or source evidence.

## Policy Deltas

The built-in policy set is deterministic:

| Policy ID | Intended bias | Typical signal |
|-----------|---------------|----------------|
| `conservative_manual` | Prefer waiting, human review, and low mutation risk. | Useful when evidence is sparse or degraded. |
| `existing_autopilot` | Model current autopilot-style next-action behavior. | Useful as a baseline for comparing new policy ideas. |
| `rch_fanout_limited` | Reduce heavyweight validation when RCH pressure is visible. | Useful when queue depth or local fallback risk is high. |
| `stale_bead_reclaiming` | Reclaim clearly stale in-progress work after evidence review. | Useful when stale Beads block ready work and owner activity is old. |
| `build_slot_protective` | Protect active build slots and avoid overlapping expensive gates. | Useful during compile storms or shared target/TMPDIR pressure. |

Read policy rankings as advisory deltas:

- `rank` and `score` compare policies within one trace only.
- `confidence` drops when source data is missing, malformed, stale, or too thin.
- `missing_data` lists claims the replay suppressed instead of guessing.
- `rationale` explains the evidence that drove the score.
- A high-scoring policy does not authorize live mutation. It tells the operator which source systems to inspect next.

If two policies disagree, prefer the one that keeps live systems unchanged until Beads, Agent Mail, RCH, and git facts are refreshed.

## Missing Or Malformed Data

Replay must fail closed. Common degraded states are expected:

| Missing fact | Replay behavior | Operator response |
|--------------|-----------------|-------------------|
| Agent Mail unavailable | Mark Mail source unavailable and suppress live-reservation certainty. | Use Beads assignee/status as soft lock and keep file scope narrow. |
| RCH queue malformed | Suppress queue-depth metrics and avoid optimistic fanout recommendations. | Run `rch status` and `rch queue` before launching cargo. |
| Runpack missing | Omit runpack recommendation and handoff fields. | Rebuild the runpack from source artifacts if handoff context matters. |
| Dirty git state missing | Avoid claiming clean-worktree readiness. | Run `git status --short --branch` and stage only owned files. |
| Policy emitted no decisions | Mark policy decision coverage as missing. | Do not rank that policy as an actionable winner. |

Missing data is a useful finding. Do not patch around it with placeholders.

## Privacy And Redaction

Replay artifacts should carry IDs, statuses, schema names, command labels, artifact paths, counts, and redacted summaries. They should not carry prompt bodies, provider transcripts, API keys, bearer tokens, cookies, secrets, or raw private message bodies.

Use these rules when adding sources:

- Store stable identifiers such as bead IDs, message IDs, reservation IDs, RCH job IDs, verification IDs, and git SHAs.
- Store command names and exit status, not full secret-bearing environments.
- Redact fields named like `prompt`, `body`, `transcript`, `token`, `secret`, `password`, `authorization`, `bearer`, `cookie`, or `key`.
- Preserve a redaction summary with count and field names so reviewers know something was removed.
- Prefer artifact paths over embedding large source payloads.

If redaction status is unknown, treat the artifact as not ready for broad handoff.

## Large-Host Budget Profiles

Replay often appears in swarms running on hosts with 64 or more cores and 256 GiB or more RAM. Large machines still need explicit admission limits because RCH slots, target directories, file descriptors, terminal rendering, and extension hostcall lanes can saturate before raw CPU or memory is exhausted.

Use this sequence before increasing fanout:

```bash
pi doctor --only swarm --format json > "$capture_dir/doctor-swarm.json"
scripts/cargo_headroom.sh --runner rch --admit-only check --all-targets \
  --decision-json "$capture_dir/cargo-admission.json"
rch status
rch queue
```

Then compare replay guidance with the current Doctor and RCH facts. `rch_fanout_limited` and `build_slot_protective` are designed to steer operators away from launching more heavyweight checks when captured pressure was already high. They do not set permanent limits; they provide a conservative starting point until fresh local evidence exists.

## Agent Mail Outages

Agent Mail health can be partially useful: reads may work while bootstrap, send, or reservation writes fail. In that state:

1. Try `health_check`, `fetch_inbox`, and `list_agents`.
2. If inbox reads work, handle ack-required messages.
3. If writes fail, record the database error in the handoff.
4. Claim through Beads and keep the file surface narrow.
5. Mention that Beads assignee state is the soft lock.

Replay should reflect the degraded state rather than pretending Mail reservations are authoritative.

## RCH Contention

All CPU-heavy Cargo work in this repository must use `rch exec -- ...` or the repo wrapper that enforces RCH. Replay helps explain why a validation action was deferred, but it does not grant permission to skip required gates.

Use captured replay as a warning, then refresh live state:

```bash
rch status
rch queue
env CARGO_TARGET_DIR="/data/tmp/pi_agent_rust_cargo/${AGENT_NAME:-agent}/target" \
  TMPDIR="/data/tmp/pi_agent_rust_cargo/${AGENT_NAME:-agent}/tmp" \
  rch exec -- cargo check --all-targets
```

If RCH is saturated, continue docs, source inspection, or non-heavy work. Do not force local all-target builds to make a bead look complete.

## Troubleshooting

| Symptom | Likely cause | Response |
|---------|--------------|----------|
| `swarm-replay-preview requires --trace` | Missing trace path. | Pass `--trace <pi.swarm.replay_trace.v1 JSON>`. |
| `requires trace schema ...` | Wrong JSON artifact. | Use the normalized trace, not a runpack, preview, or policy report. |
| `unsupported swarm-replay-preview policy` | Typo or unsupported policy ID. | Use one of the five built-in policy IDs listed above. |
| Output path already exists | CLI refuses to overwrite evidence. | Use a new capture path; delete only with explicit permission. |
| Preview confidence is low | Missing or malformed source facts. | Refresh source artifacts and rerun preview. |
| Runpack omits replay section | Preview JSON was not passed or failed schema checks. | Pass `--swarm-replay-preview-json <path>` after generating preview JSON. |

## Validation

For docs-only changes to this guide, run:

```bash
python3 scripts/check_docs_purpose_headers.py
python3 -m json.tool docs/contracts/swarm-replay-trace-contract.json >/dev/null
python3 -m json.tool docs/schema/swarm_replay_preview.json >/dev/null
cargo fmt --check
git diff --check
./scripts/reconcile_beads_ledger.sh
```

If examples or CLI flags change, also run the focused CLI test through RCH:

```bash
env CARGO_TARGET_DIR="/data/tmp/pi_agent_rust_cargo/${AGENT_NAME:-agent}/target" \
  TMPDIR="/data/tmp/pi_agent_rust_cargo/${AGENT_NAME:-agent}/tmp" \
  rch exec -- cargo test --test swarm_replay_preview_cli -- --nocapture
```

Before closing a replay bead, stage the docs and Beads changes, then run `ubs --staged --only=rust .`. For docs-only commits this should still be recorded; it may report no staged Rust files.
