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
