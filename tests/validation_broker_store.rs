#![allow(clippy::too_many_lines)]
#![forbid(unsafe_code)]

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::io::{Error as IoError, ErrorKind};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use pi::validation_broker::{
    VALIDATION_BROKER_SLOT_RECORD_SCHEMA, VALIDATION_BROKER_SLOT_SCHEMA,
    VALIDATION_BROKER_SLOT_STORE_SCHEMA, ValidationSlotArtifact, ValidationSlotLease,
    ValidationSlotRequest, ValidationSlotState, ValidationSlotStore, ValidationSlotStoreStatus,
};

type TestResult = Result<(), String>;

const START: &str = "2026-05-14T07:00:00Z";
const HEARTBEAT: &str = "2026-05-14T07:05:00Z";
const EXPIRES: &str = "2026-05-14T07:30:00Z";
const RENEWED_EXPIRES: &str = "2026-05-14T08:00:00Z";
const STALE_AT: &str = "2026-05-14T08:30:00Z";
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

fn temp_root(label: &str) -> Result<PathBuf, String> {
    let mut root = std::env::var("TMPDIR").map_or_else(|_| std::env::temp_dir(), PathBuf::from);
    root.push("pi_validation_broker_tests");
    std::fs::create_dir_all(&root).map_err(|err| format!("create temp parent: {err}"))?;

    let unique = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let mut candidate_name = String::with_capacity(label.len() + 24);
    for offset in 0..10_000 {
        candidate_name.clear();
        candidate_name.push_str(label);
        candidate_name.push('_');
        write!(&mut candidate_name, "{}", unique + offset)
            .map_err(|err| format!("write temp candidate name: {err}"))?;
        let candidate = root.join(&candidate_name);
        match std::fs::create_dir(&candidate) {
            Ok(()) => return Ok(candidate),
            Err(err) if err.kind() == ErrorKind::AlreadyExists => {}
            Err(err) => return Err(temp_root_create_error(&err)),
        }
    }

    Err("create temp root: exhausted deterministic candidates".to_string())
}

fn temp_root_create_error(err: &IoError) -> String {
    format!("create temp root: {err}")
}

fn base_request(slot_id: &str) -> ValidationSlotRequest {
    let mut environment = BTreeMap::new();
    environment.insert(
        "CARGO_TARGET_DIR".to_string(),
        "/data/tmp/pi_agent_rust_cargo/silentreef/target".to_string(),
    );
    environment.insert(
        "TMPDIR".to_string(),
        "/data/tmp/pi_agent_rust_cargo/silentreef/tmp".to_string(),
    );

    ValidationSlotRequest {
        slot_id: slot_id.to_string(),
        owner_agent: "SilentReef".to_string(),
        bead_id: "bd-gusp4.2".to_string(),
        command: vec![
            "rch".to_string(),
            "exec".to_string(),
            "--".to_string(),
            "cargo".to_string(),
            "check".to_string(),
            "--all-targets".to_string(),
        ],
        command_class: "cargo_check".to_string(),
        cwd: "/data/projects/pi_agent_rust".to_string(),
        git_head: "cf653c29b5836afabf979bb44325d4712de7088d".to_string(),
        feature_flags: vec!["default".to_string()],
        target_dir: "/data/tmp/pi_agent_rust_cargo/silentreef/target".to_string(),
        tmpdir: "/data/tmp/pi_agent_rust_cargo/silentreef/tmp".to_string(),
        runner: "rch_required".to_string(),
        rust_toolchain: Some("nightly".to_string()),
        rch_job_id: Some("rch-job-123".to_string()),
        environment,
        expected_artifacts: vec![ValidationSlotArtifact {
            path: "target/debug/deps/pi.d".to_string(),
            sha256: None,
            schema: Some("cargo_metadata".to_string()),
        }],
        artifact_schema: Some("cargo_check_result.v1".to_string()),
        artifact_hash: Some("artifact-hash-1".to_string()),
    }
}

fn acquire(slot_id: &str) -> Result<ValidationSlotLease, String> {
    ValidationSlotLease::acquire(base_request(slot_id), START, EXPIRES)
        .map_err(|err| format!("acquire lease: {err}"))
}

#[test]
fn lease_store_acquires_renews_releases_and_appends_records() -> TestResult {
    let root = temp_root("append")?;
    let store = ValidationSlotStore::new(root.join("validation-slots.jsonl"));
    let mut lease = acquire("slot-append")?;

    require(lease.schema == VALIDATION_BROKER_SLOT_SCHEMA, "slot schema")?;
    require(lease.state == ValidationSlotState::Active, "initial state")?;
    require(!lease.command_fingerprint.is_empty(), "command fingerprint")?;
    require(
        !lease.environment_fingerprint.is_empty(),
        "environment fingerprint",
    )?;

    store
        .append_lease("acquired", START, &lease)
        .map_err(|err| format!("append acquired: {err}"))?;

    lease
        .renew("SilentReef", HEARTBEAT, RENEWED_EXPIRES)
        .map_err(|err| format!("renew lease: {err}"))?;
    store
        .append_lease("renewed", HEARTBEAT, &lease)
        .map_err(|err| format!("append renewed: {err}"))?;

    lease
        .release(
            "SilentReef",
            "2026-05-14T07:10:00Z",
            "finished focused gate",
        )
        .map_err(|err| format!("release lease: {err}"))?;
    store
        .append_lease("released", "2026-05-14T07:10:00Z", &lease)
        .map_err(|err| format!("append released: {err}"))?;

    let snapshot = store.load_snapshot();
    require(
        snapshot.schema == VALIDATION_BROKER_SLOT_STORE_SCHEMA,
        "store schema",
    )?;
    require(
        snapshot.status == ValidationSlotStoreStatus::Available,
        "snapshot available",
    )?;
    require(snapshot.leases.len() == 3, "append-only history length")?;
    let latest = snapshot
        .latest_by_slot_id
        .get("slot-append")
        .ok_or_else(|| "latest slot missing".to_string())?;
    require(
        latest.state == ValidationSlotState::Released,
        "latest released state",
    )?;
    require(
        latest.release_reason.as_deref() == Some("finished focused gate"),
        "release reason preserved",
    )
}

#[test]
fn stale_detection_requires_expiry_and_explicit_reason() -> TestResult {
    let mut lease = acquire("slot-stale")?;

    require(
        !lease.is_stale_at(HEARTBEAT).map_err(to_string)?,
        "not stale",
    )?;
    require(lease.is_stale_at(STALE_AT).map_err(to_string)?, "stale")?;
    require(
        lease.mark_stale(STALE_AT, "   ").is_err(),
        "blank stale reason rejected",
    )?;
    require(
        ValidationSlotLease::acquire(
            base_request("slot-non-utc"),
            "2026-05-14T07:00:00+01:00",
            EXPIRES,
        )
        .is_err(),
        "non-UTC timestamp rejected",
    )?;

    lease
        .mark_stale(STALE_AT, "owner heartbeat expired")
        .map_err(|err| format!("mark stale: {err}"))?;
    require(lease.state == ValidationSlotState::Stale, "stale state")?;
    require(
        lease.state_reason.as_deref() == Some("owner heartbeat expired"),
        "stale reason recorded",
    )
}

#[test]
fn malformed_records_degrade_snapshot_but_keep_valid_history() -> TestResult {
    let root = temp_root("malformed")?;
    let store = ValidationSlotStore::new(root.join("validation-slots.jsonl"));
    let lease = acquire("slot-valid")?;
    store
        .append_lease("acquired", START, &lease)
        .map_err(|err| format!("append acquired: {err}"))?;

    let path = store.path();
    let mut raw = std::fs::read_to_string(path).map_err(|err| format!("read store: {err}"))?;
    let wrong_schema_record = raw
        .lines()
        .next()
        .ok_or_else(|| "valid record missing".to_string())?
        .replacen(
            VALIDATION_BROKER_SLOT_RECORD_SCHEMA,
            "wrong.validation_record_schema",
            1,
        );
    raw.push_str("{not-json}\n");
    raw.push_str(&wrong_schema_record);
    raw.push('\n');
    std::fs::write(path, raw).map_err(|err| format!("write malformed store: {err}"))?;

    let snapshot = store.load_snapshot();
    require(snapshot.is_degraded(), "snapshot degraded")?;
    require(snapshot.leases.len() == 1, "valid history retained")?;
    require(
        snapshot
            .degraded_reasons
            .iter()
            .any(|reason| reason.contains("malformed record")),
        "malformed reason recorded",
    )?;
    require(
        snapshot
            .degraded_reasons
            .iter()
            .any(|reason| reason.contains("unexpected schema")),
        "schema reason recorded",
    )
}

#[test]
fn unavailable_store_loads_as_read_only_degraded_snapshot() -> TestResult {
    let root = temp_root("unavailable")?;
    let store_path = root.join("validation-slots.jsonl");
    std::fs::create_dir_all(&store_path).map_err(|err| format!("create dir store: {err}"))?;
    let store = ValidationSlotStore::new(&store_path);

    let snapshot = store.load_snapshot();
    require(snapshot.is_degraded(), "directory path is degraded")?;
    require(snapshot.leases.is_empty(), "no invented leases")?;
    require(
        snapshot
            .degraded_reasons
            .iter()
            .any(|reason| reason.contains("store_unavailable")),
        "unavailable reason recorded",
    )
}

#[test]
fn reusable_slots_require_matching_provenance_for_coalescing() -> TestResult {
    let mut lease = acquire("slot-reusable")?;
    lease
        .mark_reusable(
            "SilentReef",
            HEARTBEAT,
            vec![ValidationSlotArtifact {
                path: "target/debug/deps/pi.d".to_string(),
                sha256: Some("artifact-hash-1".to_string()),
                schema: Some("cargo_check_result.v1".to_string()),
            }],
        )
        .map_err(|err| format!("mark reusable: {err}"))?;

    let matching = base_request("slot-reusable");
    require(
        lease
            .matches_request_equivalence(&matching)
            .map_err(to_string)?,
        "matching request should coalesce",
    )?;

    let mut different_git = base_request("slot-reusable");
    different_git.git_head = "different-head".to_string();
    require(
        !lease
            .matches_request_equivalence(&different_git)
            .map_err(to_string)?,
        "git mismatch must not coalesce",
    )?;

    let mut different_target = base_request("slot-reusable");
    different_target.target_dir = "/data/tmp/other-agent/target".to_string();
    require(
        !lease
            .matches_request_equivalence(&different_target)
            .map_err(to_string)?,
        "target mismatch must not coalesce",
    )
}

fn require(condition: bool, message: &str) -> TestResult {
    if condition {
        Ok(())
    } else {
        Err(message.to_string())
    }
}

fn to_string(err: impl std::fmt::Display) -> String {
    err.to_string()
}
