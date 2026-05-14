//! Durable validation slot lease store for the live validation broker.
//!
//! The store is append-only JSONL. Loading is fail-closed: malformed or
//! unavailable records produce a degraded snapshot instead of inventing a green
//! validation state.

use std::collections::BTreeMap;
use std::fmt::Display;
use std::fs::{self, OpenOptions};
use std::io::Write as _;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};

use crate::error::{Error, Result};

pub const VALIDATION_BROKER_SLOT_SCHEMA: &str = "pi.validation_broker.slot.v1";
pub const VALIDATION_BROKER_SLOT_STORE_SCHEMA: &str = "pi.validation_broker.slot_store.v1";
pub const VALIDATION_BROKER_SLOT_RECORD_SCHEMA: &str = "pi.validation_broker.slot_store.record.v1";
pub const VALIDATION_BROKER_INPUT_SCHEMA: &str = "pi.validation_broker.input_snapshot.v1";
pub const VALIDATION_BROKER_SOURCE_PROVENANCE_SCHEMA: &str =
    "pi.validation_broker.source_provenance.v1";
pub const VALIDATION_BROKER_RCH_INPUT_SCHEMA: &str = "pi.validation_broker.rch_input.v1";
pub const VALIDATION_BROKER_HEADROOM_INPUT_SCHEMA: &str = "pi.validation_broker.headroom_input.v1";
pub const VALIDATION_BROKER_DOCTOR_INPUT_SCHEMA: &str = "pi.validation_broker.doctor_input.v1";
pub const VALIDATION_BROKER_GIT_INPUT_SCHEMA: &str = "pi.validation_broker.git_input.v1";
pub const VALIDATION_BROKER_BEADS_INPUT_SCHEMA: &str = "pi.validation_broker.beads_input.v1";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ValidationSlotState {
    Requested,
    Active,
    Reusable,
    Stale,
    Failed,
    Released,
    Expired,
    Degraded,
}

impl ValidationSlotState {
    #[must_use]
    pub const fn is_terminal(&self) -> bool {
        matches!(
            self,
            Self::Failed | Self::Released | Self::Expired | Self::Degraded
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationSlotArtifact {
    pub path: String,
    pub sha256: Option<String>,
    pub schema: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationSlotRequest {
    pub slot_id: String,
    pub owner_agent: String,
    pub bead_id: String,
    pub command: Vec<String>,
    pub command_class: String,
    pub cwd: String,
    pub git_head: String,
    pub feature_flags: Vec<String>,
    pub target_dir: String,
    pub tmpdir: String,
    pub runner: String,
    pub rust_toolchain: Option<String>,
    pub rch_job_id: Option<String>,
    pub environment: BTreeMap<String, String>,
    pub expected_artifacts: Vec<ValidationSlotArtifact>,
    pub artifact_schema: Option<String>,
    pub artifact_hash: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationSlotLease {
    pub schema: String,
    pub slot_id: String,
    pub state: ValidationSlotState,
    pub owner_agent: String,
    pub bead_id: String,
    pub command: Vec<String>,
    pub command_class: String,
    pub cwd: String,
    pub command_fingerprint: String,
    pub environment_fingerprint: String,
    pub git_head: String,
    pub feature_flags: Vec<String>,
    pub target_dir: String,
    pub tmpdir: String,
    pub runner: String,
    pub rust_toolchain: Option<String>,
    pub rch_job_id: Option<String>,
    pub started_at_utc: String,
    pub heartbeat_at_utc: String,
    pub expires_at_utc: String,
    pub expected_artifacts: Vec<ValidationSlotArtifact>,
    pub artifacts: Vec<ValidationSlotArtifact>,
    pub artifact_schema: Option<String>,
    pub artifact_hash: Option<String>,
    pub release_reason: Option<String>,
    pub state_reason: Option<String>,
}

impl ValidationSlotLease {
    pub fn acquire(
        request: ValidationSlotRequest,
        started_at_utc: impl Into<String>,
        expires_at_utc: impl Into<String>,
    ) -> Result<Self> {
        validate_request(&request)?;
        let started_at_utc = started_at_utc.into();
        let expires_at_utc = expires_at_utc.into();
        ensure_future_expiry(&started_at_utc, &expires_at_utc)?;
        let command_fingerprint = command_fingerprint(&request)?;
        let environment_fingerprint = environment_fingerprint(&request.environment)?;

        Ok(Self {
            schema: VALIDATION_BROKER_SLOT_SCHEMA.to_string(),
            slot_id: request.slot_id,
            state: ValidationSlotState::Active,
            owner_agent: request.owner_agent,
            bead_id: request.bead_id,
            command_fingerprint,
            environment_fingerprint,
            command: request.command,
            command_class: request.command_class,
            cwd: request.cwd,
            git_head: request.git_head,
            feature_flags: request.feature_flags,
            target_dir: request.target_dir,
            tmpdir: request.tmpdir,
            runner: request.runner,
            rust_toolchain: request.rust_toolchain,
            rch_job_id: request.rch_job_id,
            heartbeat_at_utc: started_at_utc.clone(),
            started_at_utc,
            expires_at_utc,
            expected_artifacts: request.expected_artifacts,
            artifacts: Vec::new(),
            artifact_schema: request.artifact_schema,
            artifact_hash: request.artifact_hash,
            release_reason: None,
            state_reason: None,
        })
    }

    pub fn renew(
        &mut self,
        owner_agent: &str,
        heartbeat_at_utc: impl Into<String>,
        expires_at_utc: impl Into<String>,
    ) -> Result<()> {
        self.ensure_owner(owner_agent)?;
        if self.state.is_terminal() {
            return Err(Error::validation(format!(
                "cannot renew terminal slot {} in state {:?}",
                self.slot_id, self.state
            )));
        }
        let heartbeat_at_utc = heartbeat_at_utc.into();
        let expires_at_utc = expires_at_utc.into();
        ensure_future_expiry(&heartbeat_at_utc, &expires_at_utc)?;
        self.heartbeat_at_utc = heartbeat_at_utc;
        self.expires_at_utc = expires_at_utc;
        self.state = ValidationSlotState::Active;
        self.state_reason = None;
        Ok(())
    }

    pub fn mark_reusable(
        &mut self,
        owner_agent: &str,
        heartbeat_at_utc: impl Into<String>,
        artifacts: Vec<ValidationSlotArtifact>,
    ) -> Result<()> {
        self.ensure_owner(owner_agent)?;
        if self.state.is_terminal() {
            return Err(Error::validation(format!(
                "cannot reuse terminal slot {} in state {:?}",
                self.slot_id, self.state
            )));
        }
        if artifacts.is_empty() {
            return Err(Error::validation("reusable slots require artifacts"));
        }
        self.heartbeat_at_utc = heartbeat_at_utc.into();
        parse_utc(&self.heartbeat_at_utc)?;
        self.artifacts = artifacts;
        self.state = ValidationSlotState::Reusable;
        self.state_reason = Some("validation_succeeded".to_string());
        Ok(())
    }

    pub fn mark_stale(
        &mut self,
        now_utc: impl Into<String>,
        reason: impl Into<String>,
    ) -> Result<()> {
        let now_utc = now_utc.into();
        parse_utc(&now_utc)?;
        let reason = non_empty(reason.into(), "stale reason")?;
        if !self.is_stale_at(&now_utc)? {
            return Err(Error::validation(format!(
                "slot {} is not stale at {now_utc}",
                self.slot_id
            )));
        }
        if self.state.is_terminal() {
            return Err(Error::validation(format!(
                "cannot mark terminal slot {} stale",
                self.slot_id
            )));
        }
        self.state = ValidationSlotState::Stale;
        self.heartbeat_at_utc = now_utc;
        self.state_reason = Some(reason);
        Ok(())
    }

    pub fn release(
        &mut self,
        owner_agent: &str,
        heartbeat_at_utc: impl Into<String>,
        reason: impl Into<String>,
    ) -> Result<()> {
        self.ensure_owner(owner_agent)?;
        let reason = non_empty(reason.into(), "release reason")?;
        self.heartbeat_at_utc = heartbeat_at_utc.into();
        parse_utc(&self.heartbeat_at_utc)?;
        self.state = ValidationSlotState::Released;
        self.release_reason = Some(reason);
        self.state_reason = Some("released_by_owner".to_string());
        Ok(())
    }

    pub fn fail(
        &mut self,
        owner_agent: &str,
        heartbeat_at_utc: impl Into<String>,
        reason: impl Into<String>,
    ) -> Result<()> {
        self.ensure_owner(owner_agent)?;
        let reason = non_empty(reason.into(), "failure reason")?;
        self.heartbeat_at_utc = heartbeat_at_utc.into();
        parse_utc(&self.heartbeat_at_utc)?;
        self.state = ValidationSlotState::Failed;
        self.state_reason = Some(reason);
        Ok(())
    }

    pub fn is_stale_at(&self, now_utc: &str) -> Result<bool> {
        let now = parse_utc(now_utc)?;
        let expires = parse_utc(&self.expires_at_utc)?;
        Ok(now > expires)
    }

    pub fn matches_request_equivalence(&self, request: &ValidationSlotRequest) -> Result<bool> {
        Ok(self.command_fingerprint == command_fingerprint(request)?
            && self.environment_fingerprint == environment_fingerprint(&request.environment)?
            && self.cwd == request.cwd
            && self.git_head == request.git_head
            && self.feature_flags == request.feature_flags
            && self.target_dir == request.target_dir
            && self.tmpdir == request.tmpdir
            && self.runner == request.runner
            && self.rust_toolchain == request.rust_toolchain
            && self.artifact_schema == request.artifact_schema
            && self.artifact_hash == request.artifact_hash)
    }

    fn ensure_owner(&self, owner_agent: &str) -> Result<()> {
        if self.owner_agent == owner_agent {
            Ok(())
        } else {
            Err(Error::validation(format!(
                "slot {} is owned by {}, not {owner_agent}",
                self.slot_id, self.owner_agent
            )))
        }
    }

    fn validate(&self) -> Result<()> {
        if self.schema != VALIDATION_BROKER_SLOT_SCHEMA {
            return Err(Error::validation(format!(
                "slot {} has unexpected schema {}",
                self.slot_id, self.schema
            )));
        }
        require_non_empty(&self.slot_id, "slot_id")?;
        require_non_empty(&self.owner_agent, "owner_agent")?;
        require_non_empty(&self.bead_id, "bead_id")?;
        require_non_empty(&self.command_fingerprint, "command_fingerprint")?;
        require_non_empty(&self.environment_fingerprint, "environment_fingerprint")?;
        require_non_empty(&self.git_head, "git_head")?;
        require_non_empty(&self.target_dir, "target_dir")?;
        require_non_empty(&self.tmpdir, "tmpdir")?;
        require_non_empty(&self.runner, "runner")?;
        parse_utc(&self.started_at_utc)?;
        parse_utc(&self.heartbeat_at_utc)?;
        parse_utc(&self.expires_at_utc)?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationSlotStoreRecord {
    pub schema: String,
    pub event: String,
    pub recorded_at_utc: String,
    pub lease: ValidationSlotLease,
}

impl ValidationSlotStoreRecord {
    pub fn new(
        event: impl Into<String>,
        recorded_at_utc: impl Into<String>,
        lease: ValidationSlotLease,
    ) -> Result<Self> {
        let event = non_empty(event.into(), "event")?;
        let recorded_at_utc = recorded_at_utc.into();
        parse_utc(&recorded_at_utc)?;
        lease.validate()?;
        Ok(Self {
            schema: VALIDATION_BROKER_SLOT_RECORD_SCHEMA.to_string(),
            event,
            recorded_at_utc,
            lease,
        })
    }

    fn validate(&self) -> Result<()> {
        if self.schema != VALIDATION_BROKER_SLOT_RECORD_SCHEMA {
            return Err(Error::validation(format!(
                "record has unexpected schema {}",
                self.schema
            )));
        }
        require_non_empty(&self.event, "event")?;
        parse_utc(&self.recorded_at_utc)?;
        self.lease.validate()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ValidationSlotStoreStatus {
    Available,
    Degraded,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationSlotStoreSnapshot {
    pub schema: String,
    pub status: ValidationSlotStoreStatus,
    pub leases: Vec<ValidationSlotLease>,
    pub latest_by_slot_id: BTreeMap<String, ValidationSlotLease>,
    pub degraded_reasons: Vec<String>,
}

impl ValidationSlotStoreSnapshot {
    #[must_use]
    pub fn is_degraded(&self) -> bool {
        self.status == ValidationSlotStoreStatus::Degraded
    }
}

#[derive(Debug, Clone)]
pub struct ValidationSlotStore {
    path: PathBuf,
}

impl ValidationSlotStore {
    #[must_use]
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append_record(&self, record: &ValidationSlotStoreRecord) -> Result<()> {
        record.validate()?;
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        serde_json::to_writer(&mut file, record)?;
        file.write_all(b"\n")?;
        file.flush()?;
        Ok(())
    }

    pub fn append_lease(
        &self,
        event: impl Into<String>,
        recorded_at_utc: impl Into<String>,
        lease: &ValidationSlotLease,
    ) -> Result<()> {
        let record = ValidationSlotStoreRecord::new(event, recorded_at_utc, lease.clone())?;
        self.append_record(&record)
    }

    #[must_use]
    pub fn load_snapshot(&self) -> ValidationSlotStoreSnapshot {
        let mut leases = Vec::new();
        let mut latest_by_slot_id = BTreeMap::new();
        let mut degraded_reasons = Vec::new();

        let raw = match fs::read_to_string(&self.path) {
            Ok(raw) => raw,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                return snapshot(leases, latest_by_slot_id, degraded_reasons);
            }
            Err(err) => {
                degraded_reasons.push(format!("store_unavailable: {err}"));
                return snapshot(leases, latest_by_slot_id, degraded_reasons);
            }
        };

        for (line_index, line) in raw.lines().enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            match serde_json::from_str::<ValidationSlotStoreRecord>(line) {
                Ok(record) => match record.validate() {
                    Ok(()) => {
                        leases.push(record.lease);
                    }
                    Err(err) => {
                        degraded_reasons.push(line_degraded_reason(
                            line_index,
                            "invalid lease",
                            err,
                        ));
                    }
                },
                Err(err) => {
                    degraded_reasons.push(line_degraded_reason(
                        line_index,
                        "malformed record",
                        err,
                    ));
                }
            }
        }

        latest_by_slot_id = leases
            .iter()
            .map(|lease| (lease.slot_id.clone(), lease.clone()))
            .collect();
        snapshot(leases, latest_by_slot_id, degraded_reasons)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ValidationSourceState {
    Available,
    Degraded,
    Unavailable,
}

impl ValidationSourceState {
    #[must_use]
    pub const fn is_degraded(&self) -> bool {
        matches!(self, Self::Degraded | Self::Unavailable)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationSourceProvenance {
    pub schema: String,
    pub source: String,
    pub command: Vec<String>,
    pub cwd: String,
    pub captured_at_utc: String,
    pub artifact_path: Option<String>,
}

impl ValidationSourceProvenance {
    pub fn new(
        source: impl Into<String>,
        command: Vec<String>,
        cwd: impl Into<String>,
        captured_at_utc: impl Into<String>,
        artifact_path: Option<String>,
    ) -> Result<Self> {
        let provenance = Self {
            schema: VALIDATION_BROKER_SOURCE_PROVENANCE_SCHEMA.to_string(),
            source: source.into(),
            command,
            cwd: cwd.into(),
            captured_at_utc: captured_at_utc.into(),
            artifact_path,
        };
        provenance.validate()?;
        Ok(provenance)
    }

    fn validate(&self) -> Result<()> {
        if self.schema != VALIDATION_BROKER_SOURCE_PROVENANCE_SCHEMA {
            return Err(Error::validation(format!(
                "source provenance has unexpected schema {}",
                self.schema
            )));
        }
        require_non_empty(&self.source, "source")?;
        require_non_empty(&self.cwd, "source cwd")?;
        parse_utc(&self.captured_at_utc)?;
        for segment in &self.command {
            require_non_empty(segment, "source command segment")?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationSourceHealth {
    pub state: ValidationSourceState,
    pub provenance: ValidationSourceProvenance,
    pub degraded_reasons: Vec<String>,
}

impl ValidationSourceHealth {
    const fn available(provenance: ValidationSourceProvenance) -> Self {
        Self {
            state: ValidationSourceState::Available,
            provenance,
            degraded_reasons: Vec::new(),
        }
    }

    const fn degraded(provenance: ValidationSourceProvenance, reasons: Vec<String>) -> Self {
        Self {
            state: ValidationSourceState::Degraded,
            provenance,
            degraded_reasons: reasons,
        }
    }

    fn unavailable(provenance: ValidationSourceProvenance, reason: String) -> Self {
        Self {
            state: ValidationSourceState::Unavailable,
            provenance,
            degraded_reasons: vec![reason],
        }
    }

    #[must_use]
    pub const fn is_degraded(&self) -> bool {
        self.state.is_degraded()
    }
}

pub fn normalize_available_source(
    provenance: ValidationSourceProvenance,
) -> Result<ValidationSourceHealth> {
    provenance.validate()?;
    Ok(ValidationSourceHealth::available(provenance))
}

pub fn normalize_unavailable_source(
    provenance: ValidationSourceProvenance,
    reason: impl Into<String>,
) -> Result<ValidationSourceHealth> {
    provenance.validate()?;
    let reason = non_empty(reason.into(), "unavailable reason")?;
    Ok(ValidationSourceHealth::unavailable(provenance, reason))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationRchInput {
    pub schema: String,
    pub health: ValidationSourceHealth,
    pub active_builds: Option<u64>,
    pub queued_builds: Option<u64>,
    pub free_slots: Option<u64>,
    pub total_slots: Option<u64>,
    pub local_fallback: bool,
    pub saturated: bool,
}

pub fn normalize_rch_queue_text(
    provenance: ValidationSourceProvenance,
    raw: &str,
) -> Result<ValidationRchInput> {
    provenance.validate()?;
    let mut degraded_reasons = Vec::new();
    if raw.trim().is_empty() {
        degraded_reasons.push("rch_queue_output_missing".to_string());
    }

    let active_builds = count_from_line(raw, "Active Build");
    let queued_builds = count_from_line(raw, "Queued Build").or(Some(0));
    let (free_slots, total_slots) = worker_slots(raw);
    let local_fallback = contains_any(raw, &["fail open", "fails open", "local fallback"]);

    if active_builds.is_none() {
        degraded_reasons.push("rch_active_build_count_missing".to_string());
    }
    if free_slots.is_none() || total_slots.is_none() {
        degraded_reasons.push("rch_worker_slot_count_missing".to_string());
    }
    if local_fallback {
        degraded_reasons.push("rch_local_fallback_detected".to_string());
    }

    let saturated = queued_builds.unwrap_or_default() > 0 || free_slots == Some(0);
    Ok(ValidationRchInput {
        schema: VALIDATION_BROKER_RCH_INPUT_SCHEMA.to_string(),
        health: source_health(provenance, degraded_reasons),
        active_builds,
        queued_builds,
        free_slots,
        total_slots,
        local_fallback,
        saturated,
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationHeadroomInput {
    pub schema: String,
    pub health: ValidationSourceHealth,
    pub available_bytes: Option<u64>,
    pub required_bytes: Option<u64>,
    pub low_headroom: bool,
}

pub fn normalize_headroom_json(
    provenance: ValidationSourceProvenance,
    value: &Value,
) -> Result<ValidationHeadroomInput> {
    provenance.validate()?;
    let mut degraded_reasons = Vec::new();
    if !value.is_object() {
        degraded_reasons.push("headroom_source_not_object".to_string());
    }
    let available_bytes = u64_field(value, &["available_bytes", "free_bytes", "free"]);
    let required_bytes = u64_field(
        value,
        &[
            "required_bytes",
            "min_required_bytes",
            "minimum_required_bytes",
        ],
    );
    if available_bytes.is_none() {
        degraded_reasons.push("headroom_available_bytes_missing".to_string());
    }
    if required_bytes.is_none() {
        degraded_reasons.push("headroom_required_bytes_missing".to_string());
    }
    let low_headroom = matches!(
        (available_bytes, required_bytes),
        (Some(available), Some(required)) if available < required
    );

    Ok(ValidationHeadroomInput {
        schema: VALIDATION_BROKER_HEADROOM_INPUT_SCHEMA.to_string(),
        health: source_health(provenance, degraded_reasons),
        available_bytes,
        required_bytes,
        low_headroom,
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationDoctorCheck {
    pub name: String,
    pub status: String,
    pub message: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationDoctorInput {
    pub schema: String,
    pub health: ValidationSourceHealth,
    pub checks: Vec<ValidationDoctorCheck>,
    pub has_failures: bool,
}

pub fn normalize_doctor_json(
    provenance: ValidationSourceProvenance,
    value: &Value,
) -> Result<ValidationDoctorInput> {
    provenance.validate()?;
    let mut degraded_reasons = Vec::new();
    let checks_value = value.get("checks").or_else(|| {
        value
            .get("preflight")
            .and_then(|preflight| preflight.get("checks"))
    });
    let mut checks = Vec::new();

    if let Some(raw_checks) = checks_value.and_then(Value::as_array) {
        for (index, raw_check) in raw_checks.iter().enumerate() {
            let name = string_field(raw_check, &["name", "id"]).unwrap_or_else(|| {
                degraded_reasons.push(format!("doctor_check_{}_name_missing", index + 1));
                format!("unnamed_check_{}", index + 1)
            });
            let status = string_field(raw_check, &["status", "result"]).unwrap_or_else(|| {
                degraded_reasons.push(format!("doctor_check_{}_status_missing", index + 1));
                "unknown".to_string()
            });
            checks.push(ValidationDoctorCheck {
                name,
                status,
                message: string_field(raw_check, &["message", "reason"]),
            });
        }
    } else {
        degraded_reasons.push("doctor_checks_missing".to_string());
    }

    if checks.is_empty() {
        degraded_reasons.push("doctor_checks_empty".to_string());
    }

    let has_failures = checks.iter().any(|check| !is_success_status(&check.status));
    Ok(ValidationDoctorInput {
        schema: VALIDATION_BROKER_DOCTOR_INPUT_SCHEMA.to_string(),
        health: source_health(provenance, degraded_reasons),
        checks,
        has_failures,
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationGitInput {
    pub schema: String,
    pub health: ValidationSourceHealth,
    pub head: String,
    pub branch: Option<String>,
    pub dirty: bool,
    pub staged_paths: Vec<String>,
    pub unstaged_paths: Vec<String>,
    pub untracked_paths: Vec<String>,
}

pub fn normalize_git_status_text(
    provenance: ValidationSourceProvenance,
    head: impl Into<String>,
    status: &str,
) -> Result<ValidationGitInput> {
    provenance.validate()?;
    let head = non_empty(head.into(), "git head")?;
    let mut branch = None;
    let mut staged_paths = Vec::new();
    let mut unstaged_paths = Vec::new();
    let mut untracked_paths = Vec::new();
    let mut degraded_reasons = Vec::new();

    for line in status.lines() {
        if let Some(raw_branch) = line.strip_prefix("## ") {
            branch = raw_branch
                .split("...")
                .next()
                .and_then(|candidate| candidate.split_whitespace().next())
                .filter(|candidate| !candidate.is_empty())
                .map(ToOwned::to_owned);
            continue;
        }
        if line.trim().is_empty() {
            continue;
        }
        let Some(code) = line.get(..2) else {
            degraded_reasons.push(format!("git_status_line_malformed: {line}"));
            continue;
        };
        let Some(separator) = line.get(2..3) else {
            degraded_reasons.push(format!("git_status_line_malformed: {line}"));
            continue;
        };
        let Some(raw_path) = line.get(3..) else {
            degraded_reasons.push(format!("git_status_line_malformed: {line}"));
            continue;
        };
        if separator != " " || !code.bytes().all(is_git_short_status_code) {
            degraded_reasons.push(format!("git_status_line_malformed: {line}"));
            continue;
        }
        let path = raw_path.trim().to_string();
        if path.is_empty() {
            degraded_reasons.push("git_status_path_missing".to_string());
            continue;
        }
        if code == "??" {
            untracked_paths.push(path);
        } else {
            let mut chars = code.chars();
            let staged = chars.next().is_some_and(|state| state != ' ');
            let unstaged = chars.next().is_some_and(|state| state != ' ');
            if staged {
                staged_paths.push(path.clone());
            }
            if unstaged {
                unstaged_paths.push(path);
            }
        }
    }

    if branch.is_none() {
        degraded_reasons.push("git_branch_missing".to_string());
    }
    let dirty =
        !staged_paths.is_empty() || !unstaged_paths.is_empty() || !untracked_paths.is_empty();

    Ok(ValidationGitInput {
        schema: VALIDATION_BROKER_GIT_INPUT_SCHEMA.to_string(),
        health: source_health(provenance, degraded_reasons),
        head,
        branch,
        dirty,
        staged_paths,
        unstaged_paths,
        untracked_paths,
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationBeadInput {
    pub id: String,
    pub status: String,
    pub assignee: Option<String>,
    pub updated_at_utc: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationBeadsInput {
    pub schema: String,
    pub health: ValidationSourceHealth,
    pub ready_count: usize,
    pub in_progress: Vec<ValidationBeadInput>,
    pub stale_in_progress_ids: Vec<String>,
}

pub fn normalize_beads_json(
    provenance: ValidationSourceProvenance,
    value: &Value,
    now_utc: &str,
    stale_after_seconds: i64,
) -> Result<ValidationBeadsInput> {
    provenance.validate()?;
    let now = parse_utc(now_utc)?;
    if stale_after_seconds < 0 {
        return Err(Error::validation(
            "stale_after_seconds must be non-negative",
        ));
    }
    let mut degraded_reasons = Vec::new();
    let issue_values = value
        .as_array()
        .or_else(|| value.get("issues").and_then(Value::as_array));
    let mut ready_count = 0;
    let mut in_progress = Vec::new();
    let mut stale_in_progress_ids = Vec::new();

    if let Some(issues) = issue_values {
        for (index, issue) in issues.iter().enumerate() {
            let Some(id) = string_field(issue, &["id"]) else {
                degraded_reasons.push(format!("bead_{}_id_missing", index + 1));
                continue;
            };
            let Some(status) = string_field(issue, &["status"]) else {
                degraded_reasons.push(format!("bead_{id}_status_missing"));
                continue;
            };
            if status == "open" {
                ready_count += 1;
            }
            if status == "in_progress" {
                let updated_at_utc = string_field(issue, &["updated_at", "updated_at_utc"]);
                if let Some(updated_at) = &updated_at_utc {
                    match parse_utc(updated_at) {
                        Ok(updated) => {
                            if now.signed_duration_since(updated).num_seconds()
                                > stale_after_seconds
                            {
                                stale_in_progress_ids.push(id.clone());
                            }
                        }
                        Err(err) => {
                            degraded_reasons.push(format!("bead_{id}_updated_at_invalid: {err}"));
                        }
                    }
                } else {
                    degraded_reasons.push(format!("bead_{id}_updated_at_missing"));
                }
                in_progress.push(ValidationBeadInput {
                    id,
                    status,
                    assignee: string_field(issue, &["assignee"]),
                    updated_at_utc,
                });
            }
        }
    } else {
        degraded_reasons.push("beads_issue_array_missing".to_string());
    }

    Ok(ValidationBeadsInput {
        schema: VALIDATION_BROKER_BEADS_INPUT_SCHEMA.to_string(),
        health: source_health(provenance, degraded_reasons),
        ready_count,
        in_progress,
        stale_in_progress_ids,
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationBrokerInputParts {
    pub captured_at_utc: String,
    pub rch: ValidationRchInput,
    pub cargo_headroom: ValidationHeadroomInput,
    pub doctor: ValidationDoctorInput,
    pub git: ValidationGitInput,
    pub beads: ValidationBeadsInput,
    pub scratch_headroom: ValidationHeadroomInput,
    pub agent_mail: ValidationSourceHealth,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationBrokerInputSnapshot {
    pub schema: String,
    pub captured_at_utc: String,
    pub rch: ValidationRchInput,
    pub cargo_headroom: ValidationHeadroomInput,
    pub doctor: ValidationDoctorInput,
    pub git: ValidationGitInput,
    pub beads: ValidationBeadsInput,
    pub scratch_headroom: ValidationHeadroomInput,
    pub agent_mail: ValidationSourceHealth,
    pub degraded_reasons: Vec<String>,
}

impl ValidationBrokerInputSnapshot {
    pub fn from_parts(parts: ValidationBrokerInputParts) -> Result<Self> {
        parse_utc(&parts.captured_at_utc)?;
        let mut degraded_reasons = Vec::new();
        collect_source_reasons(&mut degraded_reasons, &parts.rch.health);
        collect_source_reasons(&mut degraded_reasons, &parts.cargo_headroom.health);
        collect_source_reasons(&mut degraded_reasons, &parts.doctor.health);
        collect_source_reasons(&mut degraded_reasons, &parts.git.health);
        collect_source_reasons(&mut degraded_reasons, &parts.beads.health);
        collect_source_reasons(&mut degraded_reasons, &parts.scratch_headroom.health);
        collect_source_reasons(&mut degraded_reasons, &parts.agent_mail);

        Ok(Self {
            schema: VALIDATION_BROKER_INPUT_SCHEMA.to_string(),
            captured_at_utc: parts.captured_at_utc,
            rch: parts.rch,
            cargo_headroom: parts.cargo_headroom,
            doctor: parts.doctor,
            git: parts.git,
            beads: parts.beads,
            scratch_headroom: parts.scratch_headroom,
            agent_mail: parts.agent_mail,
            degraded_reasons,
        })
    }

    #[must_use]
    pub fn is_degraded(&self) -> bool {
        !self.degraded_reasons.is_empty()
    }
}

fn source_health(
    provenance: ValidationSourceProvenance,
    degraded_reasons: Vec<String>,
) -> ValidationSourceHealth {
    if degraded_reasons.is_empty() {
        ValidationSourceHealth::available(provenance)
    } else {
        ValidationSourceHealth::degraded(provenance, degraded_reasons)
    }
}

fn collect_source_reasons(
    degraded_reasons: &mut Vec<String>,
    source_health: &ValidationSourceHealth,
) {
    for reason in &source_health.degraded_reasons {
        degraded_reasons.push(format!("{}: {reason}", source_health.provenance.source));
    }
}

fn count_from_line(raw: &str, marker: &str) -> Option<u64> {
    raw.lines()
        .find(|line| line.contains(marker))
        .and_then(first_u64)
}

fn worker_slots(raw: &str) -> (Option<u64>, Option<u64>) {
    raw.lines()
        .find(|line| line.contains("slots free"))
        .map(numbers_in_line)
        .and_then(|numbers| match numbers.as_slice() {
            [free, total, ..] => Some((Some(*free), Some(*total))),
            _ => None,
        })
        .unwrap_or((None, None))
}

fn numbers_in_line(line: &str) -> Vec<u64> {
    line.split(|ch: char| !ch.is_ascii_digit())
        .filter(|segment| !segment.is_empty())
        .filter_map(|segment| segment.parse::<u64>().ok())
        .collect()
}

fn first_u64(line: &str) -> Option<u64> {
    numbers_in_line(line).into_iter().next()
}

fn contains_any(raw: &str, needles: &[&str]) -> bool {
    let haystack = raw.to_ascii_lowercase();
    needles.iter().any(|needle| haystack.contains(needle))
}

const fn is_git_short_status_code(code: u8) -> bool {
    matches!(
        code,
        b' ' | b'M' | b'T' | b'A' | b'D' | b'R' | b'C' | b'U' | b'?' | b'!'
    )
}

fn string_field(value: &Value, names: &[&str]) -> Option<String> {
    names
        .iter()
        .find_map(|name| value.get(*name).and_then(Value::as_str))
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn u64_field(value: &Value, names: &[&str]) -> Option<u64> {
    names.iter().find_map(|name| {
        value
            .get(*name)
            .and_then(|field| field.as_u64().or_else(|| field.as_str()?.parse().ok()))
    })
}

fn is_success_status(status: &str) -> bool {
    matches!(
        status.trim().to_ascii_lowercase().as_str(),
        "ok" | "pass" | "passed" | "success" | "healthy" | "available"
    )
}

fn line_degraded_reason(line_index: usize, label: &str, err: impl Display) -> String {
    format!("line {} {label}: {err}", line_index + 1)
}

fn snapshot(
    leases: Vec<ValidationSlotLease>,
    latest_by_slot_id: BTreeMap<String, ValidationSlotLease>,
    degraded_reasons: Vec<String>,
) -> ValidationSlotStoreSnapshot {
    let status = if degraded_reasons.is_empty() {
        ValidationSlotStoreStatus::Available
    } else {
        ValidationSlotStoreStatus::Degraded
    };
    ValidationSlotStoreSnapshot {
        schema: VALIDATION_BROKER_SLOT_STORE_SCHEMA.to_string(),
        status,
        leases,
        latest_by_slot_id,
        degraded_reasons,
    }
}

fn validate_request(request: &ValidationSlotRequest) -> Result<()> {
    require_non_empty(&request.slot_id, "slot_id")?;
    require_non_empty(&request.owner_agent, "owner_agent")?;
    require_non_empty(&request.bead_id, "bead_id")?;
    if request.command.is_empty() {
        return Err(Error::validation("command must not be empty"));
    }
    for segment in &request.command {
        require_non_empty(segment, "command segment")?;
    }
    require_non_empty(&request.command_class, "command_class")?;
    require_non_empty(&request.cwd, "cwd")?;
    require_non_empty(&request.git_head, "git_head")?;
    require_non_empty(&request.target_dir, "target_dir")?;
    require_non_empty(&request.tmpdir, "tmpdir")?;
    require_non_empty(&request.runner, "runner")?;
    for feature_flag in &request.feature_flags {
        require_non_empty(feature_flag, "feature flag")?;
    }
    Ok(())
}

fn ensure_future_expiry(start_utc: &str, expires_utc: &str) -> Result<()> {
    let start = parse_utc(start_utc)?;
    let expires = parse_utc(expires_utc)?;
    if expires > start {
        Ok(())
    } else {
        Err(Error::validation(format!(
            "expires_at_utc {expires_utc} must be after {start_utc}"
        )))
    }
}

fn parse_utc(raw: &str) -> Result<DateTime<Utc>> {
    let parsed = DateTime::parse_from_rfc3339(raw)
        .map_err(|err| Error::validation(format!("invalid UTC timestamp {raw:?}: {err}")))?;
    if parsed.offset().local_minus_utc() == 0 {
        Ok(parsed.with_timezone(&Utc))
    } else {
        Err(Error::validation(format!(
            "timestamp {raw:?} must use UTC offset"
        )))
    }
}

fn command_fingerprint(request: &ValidationSlotRequest) -> Result<String> {
    fingerprint_json(&json!({
        "command": request.command,
        "command_class": request.command_class,
        "cwd": request.cwd,
        "feature_flags": request.feature_flags,
        "rust_toolchain": request.rust_toolchain,
    }))
}

fn environment_fingerprint(environment: &BTreeMap<String, String>) -> Result<String> {
    fingerprint_json(&json!(environment))
}

fn fingerprint_json(value: &Value) -> Result<String> {
    let encoded = serde_json::to_vec(value)?;
    let digest = Sha256::digest(encoded);
    Ok(hex_lower(&digest))
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        let _ = write!(out, "{byte:02x}");
    }
    out
}

fn non_empty(value: String, label: &str) -> Result<String> {
    if value.trim().is_empty() {
        Err(Error::validation(format!("{label} must not be empty")))
    } else {
        Ok(value)
    }
}

fn require_non_empty(value: &str, label: &str) -> Result<()> {
    if value.trim().is_empty() {
        Err(Error::validation(format!("{label} must not be empty")))
    } else {
        Ok(())
    }
}
