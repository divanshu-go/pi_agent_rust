# Swarm Activity Ledger

The swarm activity ledger is a redacted JSONL stream for reconstructing what happened during a multi-agent run without storing prompt bodies or secrets.

Each row uses schema `pi.swarm.activity_ledger.v1` and carries:

- `sequence`: monotonic producer-local order.
- `timestamp_ms`: Unix milliseconds for timeline reconstruction.
- `kind`: one of `bead_status`, `agent_mail`, `file_reservation`, `rch_job`, `verification`, `git_commit`, `recovery`, or `note`.
- `ids.correlation_id`: required stable ID used to join related Beads, Agent Mail, RCH, verification, and Git events.
- Optional IDs for bead, Agent Mail thread/message, agent, file reservation, RCH job, verification run, and git SHA.
- `details`: structured metadata redacted by default.
- `redaction`: count and field names redacted before serialization.

The module is intentionally library-first. Operators or later CLI surfaces can append events as work happens, export JSONL with `SwarmActivityLedger::to_jsonl`, and reconstruct a deterministic timeline with `timeline_from_jsonl`. Timeline reconstruction sorts by `timestamp_ms`, then `sequence`, then `correlation_id`, so incident review stays deterministic even when rows are collected out of order.

Redaction is fail-closed for common sensitive fields. Keys containing `prompt`, `body`, `transcript`, `token`, `secret`, `password`, `authorization`, `bearer`, `cookie`, or `key` serialize as `[REDACTED]`; values that look like bearer tokens, API keys, or password/token assignments are also redacted. Store command names, artifact paths, status codes, and IDs rather than prompts, model outputs, or raw credentials.

## Bounded summaries

`SwarmActivityLedger::summarize` and `SwarmActivitySketch` derive schema `pi.swarm.activity_summary.v1` from the raw rows without replacing or mutating them. The raw JSONL remains the audit source; summaries are a bounded-memory view for dashboards, handoff notes, and swarm health checks.

Summaries retain exact totals for event count, redacted entries, redacted fields, and activity kind counts. Hot spot lists are bounded independently for agents, beads, verification IDs, tools, providers/models, and selected detail key/value pairs. Ties sort deterministically by count descending and then key ascending. Long hot spot keys are truncated before retention so a single large detail value cannot dominate memory.

Latency details named `latency_ms`, `duration_ms`, or `elapsed_ms` feed a bounded sample sketch. The summary reports retained sample count, min, p50, p95, p99, max, and a conservative rank-error bound. Sketches can be merged across runs; the receiving sketch keeps its configured capacities and downsamples merged latency samples back to the requested bound.

## Swarm digest

`SwarmActivityLedger::digest`, `digest_from_jsonl`, and `SwarmActivityDigest::to_text` derive schema `pi.swarm.activity_digest.v1` for handoff and saturation checks. The digest is a bounded redacted view over existing ledger rows, not a new source of truth.

Digests include:

- Active agents by event count.
- Recent Beads status changes.
- Recent Agent Mail events.
- File reservation activity.
- Verification evidence from verification, RCH, and git events.
- Repeated blocker hot spots, grouped by stable normalized fingerprints.
- Stale Agent Mail threads measured from the newest represented event.
- Saturation signals for few newly filed bugs, duplicate work, repeated blockers, repeated edits to already-closed bead surfaces, stale introductions that never turn into claims or reservations, coordination-heavy windows with low commit/validation throughput, and stale threads.

The JSON form is stable for automation. The text form is deterministic for handoff notes and uses only the already-redacted summaries and selected detail fields. Prompt bodies, transcripts, tokens, API keys, cookies, authorization headers, and other sensitive values remain redacted before they can reach either output.

Repeated blocker fingerprints are evidence keys, not raw logs. For diagnostic entries such as Cargo, Clippy, test, or RCH failures, dynamic paths, target directories, PIDs, ports, timestamps, durations, long numeric IDs, and hex IDs are normalized before hashing so the same failure observed by multiple agents groups together. Each repeated blocker still carries a bounded `sample` excerpt from the already-redacted ledger entry so operators can recognize the original failure without exposing prompt bodies or secrets.

Use saturation as a stop-and-redirect signal, not as a performance claim. When the digest reports closed-surface churn, stale introductions, or high chatter with low throughput, stop launching more agents on the same review loop and switch to a narrower implementation bead, a deeper audit of one subsystem, or explicit blocker cleanup. The `saturation.signals` field lists the active typed signals, and `saturation.evidence_pointers` names the redacted agent, bead, thread, or window counts that caused each signal so operators can verify the decision without reading prompt bodies.

When saturation is active, the digest may also emit `recommendations`: deterministic advisory triage hints derived only from the redacted `saturation` fields. These recommendations name a next work mode such as a narrow implementation bead, `testing-golden-artifacts`, `testing-conformance-harnesses`, `mock-code-finder`, `deadlock-finder`, or profiling, along with confidence and the redacted evidence pointers that support the hint. They do not invoke tools, assign work, or override Beads/Agent Mail ownership; operators must treat them as starting points for choosing a narrower next action, not as a mandate.

## Tail-latency regime guard

`TailLatencyRegimeGuard` in `src/resource_governor.rs` consumes live p99, p999, queue-depth, and resource-pressure samples to detect when a swarm has left its calibrated operating regime. It requires consecutive violating samples before entering conservative fallback and consecutive recovered samples before returning to calibrated mode, so brief spikes do not flap the controller.

Regime decisions emit schema `pi.resource_governor.tail_latency_regime.v1` with the active regime, fallback state, hysteresis streaks, the live sample, and fallback reasons such as `p99_latency`, `p999_latency`, `queue_depth`, `resource_pressure`, or `hysteresis_hold`. When fallback is active, callers can apply the decision to `HostResourceBudgets` to reduce output, queue-depth, process, file-descriptor, load, and RSS budgets before admission checks.

## Capacity planner

`plan_swarm_capacity_from_jsonl` in `src/resource_governor.rs` turns the session workload matrix `swarm_metrics` JSONL rows and a `SwarmHostInventory` into schema `pi.resource_governor.capacity_plan.v1`. The generated plan includes conservative starting values for active agent concurrency, tool concurrency, extension hostcall lanes, RCH verification fanout, memory pressure thresholds, backoff windows, `HostResourceBudgets`, and `TailLatencyRegimeConfig`.

The planner fails closed when no complete `swarm_metrics` evidence is present, when required nested fields are missing, when host inventory is zero, or when latency/RSS/queue values cannot be parsed as finite non-negative numbers. Rows without `swarm_metrics` are ignored so mixed harness JSONL can still be processed; rows that claim `swarm_metrics` but omit required fields are rejected.

Use `SwarmCapacityPlan::what_if` to replay the same evidence summary against smaller CPU/RAM inventories. This is intended for quick operator budgeting, for example checking that a 64-core/256GiB evidence run would recommend lower agent and RCH fanout on a 16-core/1GiB constrained host before those budgets are wired into a `ResourceGovernor`.

Capacity recommendations are starting points, not proof of a safe maximum. Confidence drops or uncertainties are emitted for sparse evidence, host-capacity mismatches, zero reported CPU usage, queue-depth floors, and RSS headroom pressure. File-descriptor limits are still bounded with conservative built-in defaults because the current swarm harness records CPU/RAM inventory but not host fd limits.

`generate_operator_budget_profiles_from_jsonl` replays one validated capacity evidence run into schema `pi.resource_governor.operator_budget_profiles.v1` for common large-host starting points:

- `cpu16_mem64gib`: 16 logical CPUs, 64 GiB RAM.
- `cpu32_mem128gib`: 32 logical CPUs, 128 GiB RAM.
- `cpu64_mem256gib`: 64 logical CPUs, 256 GiB RAM.

Each profile carries agent concurrency, tool concurrency, extension hostcall lanes, RCH verification fanout, memory-pressure thresholds, backoff windows, `HostResourceBudgets`, tail-latency guard settings, confidence, and caveats. Profiles derived from a different source inventory are downgraded from high to medium confidence and include a source-evidence caveat. Every profile also includes `starting_point_not_release_performance_claim` so operator budgets cannot be mistaken for benchmark or release claims.

The profile generator fails closed for empty profile sets, zero CPU/RAM inventories, missing `swarm_metrics`, invalid latency/RSS/queue evidence, or malformed host-class inventories. Use the default profiles as initial swarm-admission inputs, then regenerate them from fresh local evidence before raising ceilings on a production host.

## Live admission controller

`SwarmAdmissionController` composes a validated `SwarmCapacityPlan`, `ResourceGovernor`, and `TailLatencyRegimeGuard` into schema `pi.resource_governor.swarm_admission_controller.v1`. Each decision takes the request, live host sample, live p99/p999/queue/resource-pressure sample, and current swarm load counts, then returns one final `admit`, `backpressure`, or `deny` action.

The controller uses the plan's resource budgets for host-pressure checks, the plan's tail-latency thresholds for conservative fallback, and the plan's active-agent/tool/RCH/extension-lane recommendations as live capacity ceilings. Capacity pressure can make a decision stricter than the host-resource decision, so a host that looks healthy still backpressures or denies when the swarm is already at the planned concurrency budget.

## Admission replay

`replay_swarm_admission_from_jsonl` in `src/resource_governor.rs` replays schema `pi.swarm.activity_ledger.v1` rows against a prevalidated `SwarmCapacityPlan` and captured `SwarmAdmissionReplaySample` values. The report schema is `pi.resource_governor.swarm_admission_replay.v1`.

Replay is offline incident analysis, not live doctor output. It never samples the current host, Agent Mail, Beads, or RCH. Every decision is derived from already-redacted ledger rows and captured resource samples, so an old incident can be replayed deterministically after the live machine state has changed.

Replayable ledger kinds are `bead_status`, `agent_mail`, `file_reservation`, `rch_job`, and `verification`. Rows are sorted by `timestamp_ms`, then `sequence`, then `correlation_id`, matching timeline reconstruction. Optional detail fields can refine the request:

- `request_operation` or `operation`: one of `tool`, `exec`, `http`, `session`, `ui`, `events`, `log`, or `unknown`.
- `request_capability` or `capability`: capability label attached to the replay request.
- `estimated_tool_output_bytes` or `tool_output_bytes`: request output budget input.
- `queue_depth`: request queue-depth input.
- `expected_action`, `expected_admission_action`, or `admission_action`: optional comparison value for divergence markers.

Each report includes a decision timeline, the dominant capacity pressure for every replayed decision, and divergence markers for duplicate correlation IDs, stale or missing samples, invalid expected-action details, and expected-action mismatches. Missing optional request fields use deterministic defaults for the ledger kind. Missing or stale resource samples are fail-closed: the report status becomes `fail_closed` and the affected event does not receive an optimistic decision.

`assert_swarm_digest_admission_replay_alignment` produces a separate schema `pi.resource_governor.swarm_admission_replay_digest_alignment.v1` assertion report that compares the transcript-derived digest with the replay report. The digest remains authoritative for transcript summarization and saturation evidence (`saturation.reasons` and `saturation.evidence_pointers`). The replay report remains authoritative for captured host-resource, tail-latency, live-load, and admission-decision evidence. The alignment assertion only checks that those artifacts agree on severity.

When the digest is saturated, replay must show backpressure, denial, or a fail-closed replay status before operators treat the run as unsafe to expand. When the digest has no saturation signal, replay must stay safe before operators treat the transcript and admission evidence as aligned. Any mismatch emits `status = fail_closed` with an actionable assertion such as pausing new agent launches, inspecting digest evidence, or refreshing captured replay samples before changing fanout budgets. This keeps the replay schema backwards-compatible while giving fixtures a deterministic bridge from transcript saturation to admission expectations.

## No-mock swarm smoke harness

`scripts/run_swarm_smoke_harness.py` exercises the operator workflow against real local coordination surfaces in a retained temp project. It creates a disposable Beads workspace, verifies claim-to-close status transitions, initializes a temp git repo with unrelated dirty files, runs concurrent simulated agents against assigned files, checks that dirty files are not removed or mutated, registers three Agent Mail identities through the MCP HTTP endpoint, sends a fixture thread message, reserves and releases a real file reservation, forces a reservation conflict, scans an in-progress bead as stale, and records `scripts/cargo_headroom.sh --admit-only` decisions for the live RCH posture plus an isolated PATH where RCH is unavailable.

Safe self-test:

```bash
python3 scripts/run_swarm_smoke_harness.py --self-test
```

Operator run with a fixed artifact directory:

```bash
python3 scripts/run_swarm_smoke_harness.py \
  --correlation-id bd-2zcs5.26-smoke \
  --out-dir /data/tmp/pi_swarm_smoke_artifacts/bd-2zcs5.26
```

The harness writes schema `pi.swarm.smoke_harness.v1` summaries and `pi.swarm.smoke_harness.event.v1` JSONL events. Every event includes the correlation ID, command timing when a command ran, redaction metadata, and the relevant agent names, bead IDs, reservation IDs, or RCH admission decision. Agent Mail registration tokens and sensitive-looking command output are redacted before they reach the artifact bundle. The smoke fixture treats any in-progress temp bead as stale by default; pass `--stale-after-seconds` to test a longer operator threshold. Dirty-worktree scenarios fail if protected unrelated files disappear, change unexpectedly, lose their dirty git status, or if the harness command log contains stash/reset/clean/restore-style worktree mutation commands. If `events.jsonl` or `summary.json` already exists in the requested output directory, the harness fails rather than overwriting evidence.

The harness does not delete or reset production files. Generated fixture projects and artifacts are intentionally left under `TMPDIR` or `/data/tmp` so operators can inspect them after a failed smoke run. If the live RCH posture is degraded, the RCH admission scenario records the backoff decision instead of forcing a local heavy cargo fallback.

## Operator runpack wrapper

`scripts/build_swarm_operator_runpack.py` assembles schema `pi.swarm.operator_runpack.v1` from existing source artifacts for a single operator handoff view. It accepts captured JSON from `pi doctor --only swarm --format json`, `scripts/report_swarm_claim_readiness.py`, `scripts/run_swarm_smoke_harness.py`, `scripts/cargo_headroom.sh --admit-only`, Beads JSON, git porcelain output, and the latest `pi.swarm.activity_digest.v1` digest. The script reads only the files passed to it, redacts sensitive fields and token-shaped values, and refuses malformed provided sources instead of emitting partial optimistic evidence.

The runpack includes schema `pi.swarm.safety_scorecard.v1` as `swarm_scale_safety_scorecard`. Its seven dimensions cover coordination health, cargo/RCH posture, perf evidence freshness, dirty-worktree tolerance, stalled-Bead hygiene, resource-governor readiness, and smoke-test coverage. A dimension cannot score green unless the required source artifacts loaded successfully and the dimension retains dotted evidence paths back to the summarized runpack fields.

Safe self-test:

```bash
python3 scripts/build_swarm_operator_runpack.py --self-test
```

Example operator capture:

```bash
python3 scripts/build_swarm_operator_runpack.py \
  --doctor-json /data/tmp/pi_swarm_runpack/doctor.json \
  --claim-readiness-json /data/tmp/pi_swarm_runpack/claim-readiness.json \
  --smoke-summary-json /data/tmp/pi_swarm_runpack/smoke-summary.json \
  --activity-digest-json tests/full_suite_gate/swarm_activity_digest.json \
  --cargo-admission-json /data/tmp/pi_swarm_runpack/cargo-admission.json \
  --beads-json /data/tmp/pi_swarm_runpack/beads.json \
  --git-status-file /data/tmp/pi_swarm_runpack/git-status.txt \
  --out-json /data/tmp/pi_swarm_runpack/operator-runpack.json \
  --out-md /data/tmp/pi_swarm_runpack/operator-runpack.md
```

The runpack is deliberately not a new source of truth. Beads remains authoritative for task ownership, Agent Mail remains authoritative for reservations and messages, doctor remains authoritative for live swarm diagnostics, and claim-readiness remains authoritative for release-facing evidence status. Treat the runpack as a bounded, redacted index that tells the next operator which source artifacts to inspect first.

Example row:

```json
{"schema":"pi.swarm.activity_ledger.v1","sequence":0,"timestamp_ms":1778223600000,"kind":"verification","summary":"cargo check completed","ids":{"correlation_id":"bd-2zcs5.17:verify:1","bead_id":"bd-2zcs5.17","agent_name":"CopperOx","rch_job_id":"29832517041259999","verification_id":"check-all-targets"},"details":{"command":"cargo check --all-targets","status":"passed"},"redaction":{"redacted_count":0}}
```

Use the ledger for incident review and handoff. It complements Beads and Agent Mail; it does not replace them as sources of truth.
