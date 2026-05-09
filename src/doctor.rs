//! Comprehensive environment health checker for `pi doctor`.
//!
//! When invoked without a path, checks config, directories, auth, shell tools,
//! and sessions. When invoked with a path, runs extension preflight analysis.
//! With `--fix`, automatically repairs safe issues (missing dirs, permissions).

use crate::auth::{AuthStorage, CredentialStatus};
use crate::config::Config;
use crate::error::Result;
use crate::provider_metadata::provider_auth_env_keys;
use crate::resource_governor::{
    AdmissionAction, HostResourceBudgets, HostResourceSample, ResourceOperationKind,
    ResourceRequest, SwarmAdmissionController, SwarmCapacityDimension,
    SwarmCapacityEvidenceSummary, SwarmCapacityPlan, SwarmCapacityPlanError,
    SwarmCapacityPlannerConfig, SwarmHostInventory, SwarmLiveLoad, TailLatencyRegimeSample,
};
use crate::session::SessionHeader;
use crate::session_index::walk_sessions;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fmt;
use std::fmt::Write as _;
use std::io::{BufRead as _, BufReader, Write as _};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const SWARM_STALE_IN_PROGRESS_HOURS: i64 = 24;
const SWARM_DETAIL_LIMIT: usize = 5;
const SWARM_DISK_WARN_AVAILABLE_KB: u64 = 10 * 1024 * 1024;
const SWARM_PROBE_TIMEOUT: Duration = Duration::from_secs(5);
const SWARM_DOCTOR_ADMISSION_SCHEMA: &str = "pi.doctor.swarm_admission.v1";
const MIB_BYTES: u64 = 1024 * 1024;

// ── Core Types ──────────────────────────────────────────────────────

/// How severe a finding is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Pass,
    Info,
    Warn,
    Fail,
}

impl fmt::Display for Severity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Pass => write!(f, "PASS"),
            Self::Info => write!(f, "INFO"),
            Self::Warn => write!(f, "WARN"),
            Self::Fail => write!(f, "FAIL"),
        }
    }
}

/// Whether a finding can be auto-fixed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Fixability {
    /// Cannot be auto-fixed.
    NotFixable,
    /// Can be auto-fixed with `--fix`.
    AutoFixable,
    /// Was auto-fixed in this run.
    Fixed,
}

/// Which subsystem a check belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum CheckCategory {
    Config,
    Dirs,
    Auth,
    Shell,
    Sessions,
    Swarm,
    Extensions,
}

impl CheckCategory {
    const fn label(self) -> &'static str {
        match self {
            Self::Config => "Configuration",
            Self::Dirs => "Directories",
            Self::Auth => "Authentication",
            Self::Shell => "Shell & Tools",
            Self::Sessions => "Sessions",
            Self::Swarm => "Swarm Coordination",
            Self::Extensions => "Extensions",
        }
    }
}

impl fmt::Display for CheckCategory {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.label())
    }
}

impl std::str::FromStr for CheckCategory {
    type Err = String;
    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        match s.to_ascii_lowercase().as_str() {
            "config" => Ok(Self::Config),
            "dirs" | "directories" => Ok(Self::Dirs),
            "auth" | "authentication" => Ok(Self::Auth),
            "shell" => Ok(Self::Shell),
            "sessions" => Ok(Self::Sessions),
            "swarm" | "coordination" | "leases" => Ok(Self::Swarm),
            "extensions" | "ext" => Ok(Self::Extensions),
            other => Err(format!("unknown category: {other}")),
        }
    }
}

/// A single diagnostic finding.
#[derive(Debug, Clone, Serialize)]
pub struct Finding {
    pub category: CheckCategory,
    pub severity: Severity,
    pub title: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub remediation: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<serde_json::Value>,
    pub fixability: Fixability,
}

impl Finding {
    fn pass(category: CheckCategory, title: impl Into<String>) -> Self {
        Self {
            category,
            severity: Severity::Pass,
            title: title.into(),
            detail: None,
            remediation: None,
            data: None,
            fixability: Fixability::NotFixable,
        }
    }

    fn info(category: CheckCategory, title: impl Into<String>) -> Self {
        Self {
            category,
            severity: Severity::Info,
            title: title.into(),
            detail: None,
            remediation: None,
            data: None,
            fixability: Fixability::NotFixable,
        }
    }

    fn warn(category: CheckCategory, title: impl Into<String>) -> Self {
        Self {
            category,
            severity: Severity::Warn,
            title: title.into(),
            detail: None,
            remediation: None,
            data: None,
            fixability: Fixability::NotFixable,
        }
    }

    fn fail(category: CheckCategory, title: impl Into<String>) -> Self {
        Self {
            category,
            severity: Severity::Fail,
            title: title.into(),
            detail: None,
            remediation: None,
            data: None,
            fixability: Fixability::NotFixable,
        }
    }

    fn with_detail(mut self, detail: impl Into<String>) -> Self {
        self.detail = Some(detail.into());
        self
    }

    fn with_remediation(mut self, remediation: impl Into<String>) -> Self {
        self.remediation = Some(remediation.into());
        self
    }

    fn with_data(mut self, data: serde_json::Value) -> Self {
        self.data = Some(data);
        self
    }

    const fn auto_fixable(mut self) -> Self {
        self.fixability = Fixability::AutoFixable;
        self
    }

    const fn fixed(mut self) -> Self {
        self.fixability = Fixability::Fixed;
        self.severity = Severity::Pass;
        self
    }
}

/// Summary counters.
#[derive(Debug, Clone, Default, Serialize)]
pub struct DoctorSummary {
    pub pass: usize,
    pub info: usize,
    pub warn: usize,
    pub fail: usize,
}

/// Full diagnostic report.
#[derive(Debug, Clone, Serialize)]
pub struct DoctorReport {
    pub findings: Vec<Finding>,
    pub summary: DoctorSummary,
    pub overall: Severity,
}

impl DoctorReport {
    fn from_findings(findings: Vec<Finding>) -> Self {
        let mut summary = DoctorSummary::default();
        let mut overall = Severity::Pass;
        for f in &findings {
            match f.severity {
                Severity::Pass => summary.pass += 1,
                Severity::Info => summary.info += 1,
                Severity::Warn => {
                    summary.warn += 1;
                    if overall < Severity::Warn {
                        overall = Severity::Warn;
                    }
                }
                Severity::Fail => {
                    summary.fail += 1;
                    overall = Severity::Fail;
                }
            }
        }
        Self {
            findings,
            summary,
            overall,
        }
    }

    /// Render human-friendly text output.
    pub fn render_text(&self) -> String {
        let mut out = String::with_capacity(2048);
        out.push_str("Pi Doctor\n=========\n");

        // Group findings by category, preserving insertion order
        let mut seen_categories: Vec<CheckCategory> = Vec::new();
        for f in &self.findings {
            if !seen_categories.contains(&f.category) {
                seen_categories.push(f.category);
            }
        }

        for cat in &seen_categories {
            let cat_findings: Vec<&Finding> = self
                .findings
                .iter()
                .filter(|f| f.category.eq(cat))
                .collect();
            let cat_worst = cat_findings
                .iter()
                .map(|f| f.severity)
                .max()
                .unwrap_or(Severity::Pass);
            let _ = writeln!(out, "\n[{cat_worst}] {cat}");
            for f in &cat_findings {
                let _ = writeln!(out, "  [{}] {}", f.severity, f.title);
                if let Some(detail) = &f.detail {
                    let _ = writeln!(out, "       {detail}");
                }
                if let Some(rem) = &f.remediation {
                    let _ = writeln!(out, "       Fix: {rem}");
                }
                if matches!(f.fixability, Fixability::AutoFixable) {
                    out.push_str("       (fixable with --fix)\n");
                }
            }
        }

        let _ = writeln!(
            out,
            "\nOverall: {} ({} pass, {} info, {} warn, {} fail)",
            self.overall,
            self.summary.pass,
            self.summary.info,
            self.summary.warn,
            self.summary.fail
        );
        out
    }

    /// Render as JSON.
    pub fn to_json(&self) -> Result<String> {
        Ok(serde_json::to_string_pretty(self)?)
    }

    /// Render as markdown.
    pub fn render_markdown(&self) -> String {
        let mut out = String::with_capacity(2048);
        out.push_str("# Pi Doctor Report\n\n");

        let mut seen_categories: Vec<CheckCategory> = Vec::new();
        for f in &self.findings {
            if !seen_categories.contains(&f.category) {
                seen_categories.push(f.category);
            }
        }

        for cat in &seen_categories {
            let _ = writeln!(out, "## {cat}\n");
            for f in self.findings.iter().filter(|f| f.category.eq(cat)) {
                let icon = match f.severity {
                    Severity::Pass => "✅",
                    Severity::Info => "ℹ️",
                    Severity::Warn => "⚠️",
                    Severity::Fail => "❌",
                };
                let _ = write!(out, "- {icon} **{}**", f.title);
                if let Some(detail) = &f.detail {
                    let _ = write!(out, " — {detail}");
                }
                out.push('\n');
                if let Some(rem) = &f.remediation {
                    let _ = writeln!(out, "  - Fix: {rem}");
                }
            }
            out.push('\n');
        }

        let _ = writeln!(
            out,
            "**Overall: {}** ({} pass, {} info, {} warn, {} fail)",
            self.overall,
            self.summary.pass,
            self.summary.info,
            self.summary.warn,
            self.summary.fail
        );
        out
    }
}

// ── Options ─────────────────────────────────────────────────────────

/// Options for `run_doctor`.
pub struct DoctorOptions<'a> {
    pub cwd: &'a Path,
    pub extension_path: Option<&'a str>,
    pub policy_override: Option<&'a str>,
    pub fix: bool,
    pub only: Option<HashSet<CheckCategory>>,
}

// ── Entry Point ─────────────────────────────────────────────────────

/// Run all applicable doctor checks and return a report.
#[allow(clippy::too_many_lines)]
pub fn run_doctor(opts: &DoctorOptions<'_>) -> Result<DoctorReport> {
    let mut findings = Vec::new();
    let extension_only_default = opts.extension_path.is_some() && opts.only.is_none();

    let should_run = |cat: CheckCategory| -> bool {
        if extension_only_default {
            return false;
        }
        opts.only.as_ref().is_none_or(|set| set.contains(&cat))
    };

    if let Some(ext_path) = opts.extension_path {
        if opts
            .only
            .as_ref()
            .is_none_or(|set| set.contains(&CheckCategory::Extensions))
        {
            check_extension(opts.cwd, ext_path, opts.policy_override, &mut findings);
        }
    } else if opts
        .only
        .as_ref()
        .is_some_and(|set| set.contains(&CheckCategory::Extensions))
    {
        findings.push(
            Finding::fail(
                CheckCategory::Extensions,
                "Extensions check requires an extension path",
            )
            .with_remediation(
                "Run `pi doctor <path-to-extension>` to evaluate extension compatibility",
            ),
        );
    }

    if should_run(CheckCategory::Config) {
        check_config(opts.cwd, &mut findings);
    }
    if should_run(CheckCategory::Dirs) {
        check_dirs(opts.fix, &mut findings);
    }
    if should_run(CheckCategory::Auth) {
        check_auth(opts.fix, &mut findings);
    }
    if should_run(CheckCategory::Shell) {
        check_shell(&mut findings);
    }
    if should_run(CheckCategory::Sessions) {
        check_sessions(&mut findings);
    }
    if should_run(CheckCategory::Swarm) {
        check_swarm(opts.cwd, &mut findings);
    }

    Ok(DoctorReport::from_findings(findings))
}

// ── Check: Config ───────────────────────────────────────────────────

fn check_config(cwd: &Path, findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Config;

    // Global settings
    let global_path = Config::global_dir().join("settings.json");
    check_settings_file(cat, &global_path, "Global settings", findings);

    // Project settings
    let project_path = cwd.join(Config::project_dir()).join("settings.json");
    if project_path.exists() {
        check_settings_file(
            cat,
            &project_path,
            "Project settings (.pi/settings.json)",
            findings,
        );
    } else {
        findings.push(Finding::pass(cat, "No project settings (OK)"));
    }
}

fn check_settings_file(cat: CheckCategory, path: &Path, label: &str, findings: &mut Vec<Finding>) {
    if !path.exists() {
        findings.push(Finding::pass(cat, format!("{label}: not present (OK)")));
        return;
    }
    match std::fs::read_to_string(path) {
        Ok(content) => {
            let value: serde_json::Value = match serde_json::from_str(&content) {
                Ok(value) => value,
                Err(e) => {
                    findings.push(
                        Finding::fail(cat, format!("{label}: JSON parse error"))
                            .with_detail(e.to_string())
                            .with_remediation(format!("Fix the JSON syntax in {}", path.display())),
                    );
                    return;
                }
            };

            let serde_json::Value::Object(map) = value else {
                findings.push(
                    Finding::fail(
                        cat,
                        format!("{label}: top-level value must be a JSON object"),
                    )
                    .with_detail(format!("Found non-object JSON in {}", path.display()))
                    .with_remediation(format!("Wrap settings in {{ ... }} in {}", path.display())),
                );
                return;
            };

            let unknown: Vec<&String> = map.keys().filter(|k| !is_known_config_key(k)).collect();
            if unknown.is_empty() {
                findings.push(Finding::pass(cat, label.to_string()));
            } else {
                findings.push(
                    Finding::warn(cat, format!("{label}: unknown keys"))
                        .with_detail(format!(
                            "Unknown keys: {}",
                            unknown
                                .iter()
                                .map(|k| k.as_str())
                                .collect::<Vec<_>>()
                                .join(", ")
                        ))
                        .with_remediation("Check for typos in settings key names"),
                );
            }
        }
        Err(e) => {
            findings.push(
                Finding::fail(cat, format!("{label}: read error"))
                    .with_detail(e.to_string())
                    .with_remediation(format!("Check file permissions on {}", path.display())),
            );
        }
    }
}

/// Known top-level config keys (from `Config` struct fields + their camelCase aliases).
fn is_known_config_key(key: &str) -> bool {
    matches!(
        key,
        "theme"
            | "hideThinkingBlock"
            | "hide_thinking_block"
            | "showHardwareCursor"
            | "show_hardware_cursor"
            | "defaultProvider"
            | "default_provider"
            | "defaultModel"
            | "default_model"
            | "defaultThinkingLevel"
            | "default_thinking_level"
            | "enabledModels"
            | "enabled_models"
            | "steeringMode"
            | "steering_mode"
            | "followUpMode"
            | "follow_up_mode"
            | "quietStartup"
            | "quiet_startup"
            | "collapseChangelog"
            | "collapse_changelog"
            | "lastChangelogVersion"
            | "last_changelog_version"
            | "doubleEscapeAction"
            | "double_escape_action"
            | "editorPaddingX"
            | "editor_padding_x"
            | "autocompleteMaxVisible"
            | "autocomplete_max_visible"
            | "sessionPickerInput"
            | "session_picker_input"
            | "sessionStore"
            | "sessionBackend"
            | "session_store"
            | "compaction"
            | "branchSummary"
            | "branch_summary"
            | "retry"
            | "shellPath"
            | "shell_path"
            | "shellCommandPrefix"
            | "shell_command_prefix"
            | "ghPath"
            | "gh_path"
            | "images"
            | "terminal"
            | "thinkingBudgets"
            | "thinking_budgets"
            | "packages"
            | "extensions"
            | "skills"
            | "prompts"
            | "themes"
            | "enableSkillCommands"
            | "enable_skill_commands"
            | "extensionPolicy"
            | "extension_policy"
            | "repairPolicy"
            | "repair_policy"
            | "extensionRisk"
            | "extension_risk"
            | "checkForUpdates"
            | "check_for_updates"
            | "sessionDurability"
            | "session_durability"
            | "markdown"
            | "queueMode"
    )
}

// ── Check: Dirs ─────────────────────────────────────────────────────

fn check_dirs(fix: bool, findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Dirs;
    let dirs = [
        ("Agent directory", Config::global_dir()),
        ("Sessions directory", Config::sessions_dir()),
        ("Packages directory", Config::package_dir()),
    ];

    for (label, dir) in &dirs {
        check_dir(cat, label, dir, fix, findings);
    }
}

fn check_dir(cat: CheckCategory, label: &str, dir: &Path, fix: bool, findings: &mut Vec<Finding>) {
    if dir.is_dir() {
        // Check write permission
        match tempfile::NamedTempFile::new_in(dir) {
            Ok(mut probe_file) => match probe_file.write_all(b"probe") {
                Ok(()) => {
                    findings.push(Finding::pass(cat, format!("{label} ({})", dir.display())));
                }
                Err(e) => {
                    findings.push(
                        Finding::fail(cat, format!("{label}: not writable"))
                            .with_detail(format!("{}: {e}", dir.display()))
                            .with_remediation(format!("chmod u+w {}", dir.display())),
                    );
                }
            },
            Err(e) => {
                findings.push(
                    Finding::fail(cat, format!("{label}: not writable"))
                        .with_detail(format!("{}: {e}", dir.display()))
                        .with_remediation(format!("chmod u+w {}", dir.display())),
                );
            }
        }
    } else if fix {
        match std::fs::create_dir_all(dir) {
            Ok(()) => {
                findings.push(
                    Finding::pass(cat, format!("{label}: created ({})", dir.display())).fixed(),
                );
            }
            Err(e) => {
                findings.push(
                    Finding::fail(cat, format!("{label}: could not create"))
                        .with_detail(format!("{}: {e}", dir.display()))
                        .with_remediation(format!("mkdir -p {}", dir.display())),
                );
            }
        }
    } else {
        findings.push(
            Finding::warn(cat, format!("{label}: missing"))
                .with_detail(format!("{} does not exist", dir.display()))
                .with_remediation(format!("mkdir -p {}", dir.display()))
                .auto_fixable(),
        );
    }
}

// ── Check: Auth ─────────────────────────────────────────────────────

#[allow(clippy::too_many_lines)]
#[cfg_attr(not(unix), allow(unused_variables))]
fn check_auth(fix: bool, findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Auth;
    let auth_path = Config::auth_path();

    if !auth_path.exists() {
        findings.push(
            Finding::info(cat, "auth.json: not present")
                .with_detail("No credentials stored yet")
                .with_remediation("Run `pi` and follow the login prompt, or set ANTHROPIC_API_KEY"),
        );
        // Still check env vars
        check_auth_env_vars(cat, findings);
        return;
    }

    // Check if auth.json parses
    let auth = match AuthStorage::load(auth_path.clone()) {
        Ok(auth) => {
            findings.push(Finding::pass(cat, "auth.json parses correctly"));
            Some(auth)
        }
        Err(e) => {
            findings.push(
                Finding::fail(cat, "auth.json: parse error")
                    .with_detail(e.to_string())
                    .with_remediation("Check auth.json syntax or delete and re-authenticate"),
            );
            None
        }
    };

    // Check file permissions (Unix only)
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = std::fs::metadata(&auth_path) {
            let mode = meta.permissions().mode() & 0o777;
            if mode.eq(&0o600) {
                findings.push(Finding::pass(cat, "auth.json permissions (600)"));
            } else if fix {
                match std::fs::set_permissions(&auth_path, std::fs::Permissions::from_mode(0o600)) {
                    Ok(()) => {
                        findings.push(
                            Finding::pass(
                                cat,
                                format!("auth.json permissions fixed (was {mode:o}, now 600)"),
                            )
                            .fixed(),
                        );
                    }
                    Err(e) => {
                        findings.push(
                            Finding::fail(cat, "auth.json: could not fix permissions")
                                .with_detail(e.to_string()),
                        );
                    }
                }
            } else {
                findings.push(
                    Finding::warn(
                        cat,
                        format!("auth.json permissions are {mode:o}, should be 600"),
                    )
                    .with_remediation(format!("chmod 600 {}", auth_path.display()))
                    .auto_fixable(),
                );
            }
        }
    }

    // Check stored credentials
    if let Some(auth) = &auth {
        let providers = auth.provider_names();
        if providers.is_empty() {
            findings.push(
                Finding::info(cat, "No stored credentials")
                    .with_remediation("Run `pi` to authenticate or set an API key env var"),
            );
        } else {
            for provider in &providers {
                let status = auth.credential_status(provider);
                match status {
                    CredentialStatus::ApiKey => {
                        findings.push(Finding::pass(
                            cat,
                            format!("{provider}: API key configured"),
                        ));
                    }
                    CredentialStatus::OAuthValid { .. } => {
                        findings.push(Finding::pass(cat, format!("{provider}: OAuth token valid")));
                    }
                    CredentialStatus::OAuthExpired { .. } => {
                        findings.push(
                            Finding::warn(cat, format!("{provider}: OAuth token expired"))
                                .with_remediation(format!("Run `pi /login {provider}` to refresh")),
                        );
                    }
                    CredentialStatus::BearerToken => {
                        findings.push(Finding::pass(
                            cat,
                            format!("{provider}: bearer token configured"),
                        ));
                    }
                    CredentialStatus::AwsCredentials => {
                        findings.push(Finding::pass(
                            cat,
                            format!("{provider}: AWS credentials configured"),
                        ));
                    }
                    CredentialStatus::ServiceKey => {
                        findings.push(Finding::pass(
                            cat,
                            format!("{provider}: service key configured"),
                        ));
                    }
                    CredentialStatus::Missing => {
                        // Shouldn't happen since we're iterating stored providers
                        findings.push(Finding::info(cat, format!("{provider}: no credentials")));
                    }
                }
            }
        }
    }

    check_auth_env_vars(cat, findings);
}

/// Check common auth-related environment variables.
fn check_auth_env_vars(cat: CheckCategory, findings: &mut Vec<Finding>) {
    let key_providers = [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ];

    for (provider, env_key) in &key_providers {
        let env_keys = provider_auth_env_keys(provider);
        let has_env = env_keys.iter().any(|k| std::env::var(k).is_ok());
        if has_env {
            findings.push(Finding::pass(
                cat,
                format!("{provider}: env var set ({env_key})"),
            ));
        } else {
            findings.push(
                Finding::info(cat, format!("{provider}: no env var"))
                    .with_detail(format!("Set {env_key} or run `pi /login {provider}`")),
            );
        }
    }
}

// ── Check: Shell ────────────────────────────────────────────────────

fn check_shell(findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Shell;

    // Required tools (Fail if missing)
    check_tool(
        cat,
        "bash",
        &["--version"],
        Severity::Fail,
        ToolCheckMode::PresenceOnly,
        findings,
    );
    check_tool(
        cat,
        "sh",
        &["--version"],
        Severity::Fail,
        ToolCheckMode::PresenceOnly,
        findings,
    );

    // Important tools (Warn if missing)
    check_tool(
        cat,
        "git",
        &["--version"],
        Severity::Warn,
        ToolCheckMode::PresenceOnly,
        findings,
    );
    check_tool(
        cat,
        "rg",
        &["--version"],
        Severity::Warn,
        ToolCheckMode::PresenceOnly,
        findings,
    );

    let fd_bin = if which_tool("fd").is_some() {
        "fd"
    } else {
        "fdfind"
    };
    check_tool(
        cat,
        fd_bin,
        &["--version"],
        Severity::Warn,
        ToolCheckMode::PresenceOnly,
        findings,
    );

    // Optional tools (Info if missing)
    check_tool(
        cat,
        "gh",
        &["--version"],
        Severity::Info,
        ToolCheckMode::PresenceOnly,
        findings,
    );
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ToolCheckMode {
    PresenceOnly,
    ProbeExecution,
}

fn check_tool(
    cat: CheckCategory,
    tool: &str,
    args: &[&str],
    missing_severity: Severity,
    mode: ToolCheckMode,
    findings: &mut Vec<Finding>,
) {
    let discovered_path = which_tool(tool);
    if matches!(mode, ToolCheckMode::PresenceOnly) {
        if let Some(path) = discovered_path {
            findings.push(Finding::pass(cat, format!("{tool} ({path})")));
            return;
        }
        report_missing_tool(cat, tool, missing_severity, findings);
        return;
    }

    let command_target = discovered_path.as_deref().unwrap_or(tool);

    let mut command = Command::new(command_target); // ubs:ignore false positive: private doctor tool probe; production callers pass fixed tool names.
    match command
        .args(args)
        .stdin(std::process::Stdio::null())
        .output()
    {
        Ok(output) if output.status.success() => {
            // Extract version from first line of stdout
            let version = String::from_utf8_lossy(&output.stdout);
            let first_line = version.lines().next().unwrap_or("").trim();
            let label = discovered_path.as_ref().map_or_else(
                || {
                    if first_line.is_empty() {
                        tool.to_string()
                    } else {
                        format!("{tool}: {first_line}")
                    }
                },
                |path| format!("{tool} ({path})"),
            );
            findings.push(Finding::pass(cat, label));
        }
        Ok(output)
            if discovered_path.is_some()
                && probe_failure_is_known_nonfatal(tool, args, &output) =>
        {
            // Some shells (e.g. dash as /bin/sh) do not support --version.
            // If this is the known non-fatal probe case, treat tool as present.
            let path = discovered_path.unwrap_or_default();
            findings.push(Finding::pass(cat, format!("{tool} ({path})")));
        }
        Ok(output) => {
            let suffix = if matches!(missing_severity, Severity::Info) {
                " (optional)"
            } else {
                ""
            };
            let detail = {
                let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                if stderr.is_empty() {
                    format!("Exit status: {:?}", output.status.code())
                } else {
                    stderr
                }
            };
            findings.push(Finding {
                category: cat,
                severity: missing_severity,
                title: format!("{tool}: invocation failed{suffix}"),
                detail: Some(detail),
                remediation: discovered_path
                    .as_ref()
                    .map(|path| format!("Verify this executable is healthy: {path}")),
                data: None,
                fixability: Fixability::NotFixable,
            });
        }
        Err(err) => {
            if discovered_path.is_some() || !matches!(err.kind(), std::io::ErrorKind::NotFound) {
                let suffix = if matches!(missing_severity, Severity::Info) {
                    " (optional)"
                } else {
                    ""
                };
                findings.push(Finding {
                    category: cat,
                    severity: missing_severity,
                    title: format!("{tool}: invocation failed{suffix}"),
                    detail: Some(err.to_string()),
                    remediation: discovered_path
                        .as_ref()
                        .map(|path| format!("Verify this executable is healthy: {path}")),
                    data: None,
                    fixability: Fixability::NotFixable,
                });
            } else {
                report_missing_tool(cat, tool, missing_severity, findings);
            }
        }
    }
}

fn report_missing_tool(
    cat: CheckCategory,
    tool: &str,
    missing_severity: Severity,
    findings: &mut Vec<Finding>,
) {
    let suffix = if matches!(missing_severity, Severity::Info) {
        " (optional)"
    } else {
        ""
    };
    let mut f = Finding {
        category: cat,
        severity: missing_severity,
        title: format!("{tool}: not found{suffix}"),
        detail: None,
        remediation: None,
        data: None,
        fixability: Fixability::NotFixable,
    };
    if tool.eq("gh") {
        f.remediation = Some("Install: https://cli.github.com/".to_string());
    }
    findings.push(f);
}

fn probe_failure_is_known_nonfatal(
    tool: &str,
    args: &[&str],
    output: &std::process::Output,
) -> bool {
    if tool.ne("sh") || args.ne(&["--version"]) {
        return false;
    }
    let stderr = String::from_utf8_lossy(&output.stderr).to_ascii_lowercase();
    stderr.contains("illegal option")
        || stderr.contains("unknown option")
        || stderr.contains("invalid option")
}

fn which_tool(tool: &str) -> Option<String> {
    let tool_path = Path::new(tool);
    if tool_path.components().count() > 1 {
        return is_executable(tool_path).then(|| tool_path.display().to_string());
    }

    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        if let Some(path) = resolve_executable_in_dir(&dir, tool) {
            return Some(path.display().to_string());
        }
    }
    None
}

fn resolve_executable_in_dir(dir: &Path, tool: &str) -> Option<PathBuf> {
    #[cfg(windows)]
    {
        let candidate = dir.join(tool);
        if is_executable(&candidate) {
            return Some(candidate);
        }
        let pathext = std::env::var_os("PATHEXT").unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD".into());
        for ext in std::env::split_paths(&pathext) {
            let ext = ext.to_string_lossy();
            let suffix = ext.trim_matches('.');
            if suffix.is_empty() {
                continue;
            }
            let candidate = dir.join(format!("{tool}.{suffix}"));
            if is_executable(&candidate) {
                return Some(candidate);
            }
        }
        None
    }

    #[cfg(not(windows))]
    {
        let candidate = dir.join(tool);
        is_executable(&candidate).then_some(candidate)
    }
}

fn is_executable(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        std::fs::metadata(path)
            .ok()
            .is_some_and(|metadata| metadata.permissions().mode() & 0o111 != 0)
    }

    #[cfg(not(unix))]
    {
        true
    }
}

// ── Check: Swarm Coordination ───────────────────────────────────────

#[allow(clippy::too_many_lines)]
fn check_swarm(cwd: &Path, findings: &mut Vec<Finding>) {
    check_swarm_beads(cwd, findings);
    check_swarm_live_admission(cwd, findings);
    check_swarm_br_status(cwd, findings);
    check_swarm_agent_mail(cwd, findings);
    check_swarm_git(cwd, findings);
    check_swarm_rch(findings);
    check_swarm_temp_dirs(findings);
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct BeadsLedgerSummary {
    total: usize,
    open: usize,
    in_progress: usize,
    active: usize,
    parse_errors: usize,
    stale_in_progress: Vec<StaleIssue>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct StaleIssue {
    id: String,
    title: String,
    updated_at: String,
    age_hours: i64,
}

#[derive(Debug, Deserialize)]
struct BeadsIssueRecord {
    id: String,
    #[serde(default)]
    title: String,
    status: String,
    updated_at: Option<String>,
}

fn check_swarm_beads(cwd: &Path, findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Swarm;
    let ledger_path = cwd.join(".beads/issues.jsonl");
    if !ledger_path.is_file() {
        findings.push(
            Finding::warn(cat, "Beads ledger not found")
                .with_detail(format!("Expected {}", ledger_path.display()))
                .with_remediation("Run from a Beads-backed checkout or initialize Beads first"),
        );
        return;
    }

    let content = match std::fs::read_to_string(&ledger_path) {
        Ok(content) => content,
        Err(err) => {
            findings.push(
                Finding::fail(cat, "Beads ledger is not readable")
                    .with_detail(format!("{}: {err}", ledger_path.display()))
                    .with_remediation("Check ledger permissions before starting more agents"),
            );
            return;
        }
    };

    let summary = summarize_beads_ledger(&content, Utc::now(), SWARM_STALE_IN_PROGRESS_HOURS);
    if summary.parse_errors.eq(&0) {
        findings.push(
            Finding::pass(cat, "Beads ledger parses").with_detail(format!(
                "{} issues; {} active ({} open, {} in_progress)",
                summary.total, summary.active, summary.open, summary.in_progress
            )),
        );
    } else {
        findings.push(
            Finding::fail(cat, "Beads ledger has malformed JSONL rows")
                .with_detail(format!(
                    "{} parse error(s) in {} rows",
                    summary.parse_errors, summary.total
                ))
                .with_remediation("Run `br doctor --json` and rebuild from healthy issues.jsonl before claiming more work"),
        );
    }

    if summary.stale_in_progress.is_empty() {
        findings.push(Finding::pass(cat, "No stale in_progress beads detected"));
    } else {
        findings.push(
            Finding::warn(cat, "Stale in_progress beads need coordination")
                .with_detail(format_stale_issues(&summary.stale_in_progress))
                .with_remediation("Use Agent Mail to contact owners; only reset a bead after confirming the owner is stale"),
        );
    }
}

fn check_swarm_live_admission(cwd: &Path, findings: &mut Vec<Finding>) {
    let ledger_path = cwd.join(".beads/issues.jsonl");
    let content = match std::fs::read_to_string(&ledger_path) {
        Ok(content) => content,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            let warnings = vec!["Beads ledger missing".to_string()];
            findings.push(swarm_admission_blocked_finding(
                Severity::Warn,
                "Live swarm admission decision denies new work",
                format!("Beads ledger not found at {}", ledger_path.display()),
                &warnings,
            ));
            return;
        }
        Err(err) => {
            let warnings = vec!["Beads ledger unreadable".to_string()];
            findings.push(swarm_admission_blocked_finding(
                Severity::Fail,
                "Live swarm admission decision unavailable",
                format!("{} is not readable: {err}", ledger_path.display()),
                &warnings,
            ));
            return;
        }
    };

    let summary = summarize_beads_ledger(&content, Utc::now(), SWARM_STALE_IN_PROGRESS_HOURS);
    if summary.parse_errors > 0 {
        let warnings = vec!["Beads ledger has malformed JSONL rows".to_string()];
        findings.push(swarm_admission_blocked_finding(
            Severity::Fail,
            "Live swarm admission decision denies new work",
            format!(
                "Beads ledger has {} malformed JSONL row(s); coordination state is corrupted",
                summary.parse_errors
            ),
            &warnings,
        ));
        return;
    }

    let sample = HostResourceSample::current();
    let mut warnings = resource_sample_warnings(&sample);
    if !summary.stale_in_progress.is_empty() {
        warnings.push(format!(
            "{} stale in_progress bead(s) may overstate live agent load",
            summary.stale_in_progress.len()
        ));
    }

    let plan = match build_swarm_doctor_capacity_plan(&sample) {
        Ok(plan) => plan,
        Err(err) => {
            findings.push(swarm_admission_blocked_finding(
                Severity::Fail,
                "Live swarm admission decision unavailable",
                format!("capacity plan could not be built from live host sample: {err}"),
                &warnings,
            ));
            return;
        }
    };

    let live_load = live_load_from_beads_summary(&summary);
    findings.push(classify_swarm_admission(plan, sample, live_load, &warnings));
}

fn build_swarm_doctor_capacity_plan(
    sample: &HostResourceSample,
) -> std::result::Result<SwarmCapacityPlan, SwarmCapacityPlanError> {
    let budgets = HostResourceBudgets::from_host();
    let cpu_cores = budgets.cpu_cores.max(1);
    let inventory = SwarmHostInventory::new(
        cpu_cores,
        cpu_cores,
        bytes_to_mib_ceil(budgets.max_rss_bytes.saturating_mul(2)).max(1),
    );
    let evidence = SwarmCapacityEvidenceSummary {
        complete_records: 1,
        host_capacity_rows: 1,
        host_capacity_mismatch_rows: 0,
        max_p99_ms: 250,
        max_p999_ms: 1_000,
        max_queue_depth: budgets.max_queue_depth.max(1),
        max_rss_mb: sample.rss_bytes.map_or(256, bytes_to_mib_ceil).max(1),
        max_cpu_pct: 1.0,
    };

    SwarmCapacityPlannerConfig::default().plan_from_summary(evidence, inventory)
}

fn live_load_from_beads_summary(summary: &BeadsLedgerSummary) -> SwarmLiveLoad {
    SwarmLiveLoad::empty()
        .with_active_agents(
            env_u64("PI_DOCTOR_SWARM_ACTIVE_AGENTS")
                .unwrap_or_else(|| usize_to_u64(summary.in_progress)),
        )
        .with_active_tool_calls(env_u64("PI_DOCTOR_SWARM_ACTIVE_TOOL_CALLS").unwrap_or(0))
        .with_extension_hostcall_lanes(
            env_u64("PI_DOCTOR_SWARM_EXTENSION_HOSTCALL_LANES").unwrap_or(0),
        )
        .with_active_rch_jobs(env_u64("PI_DOCTOR_SWARM_ACTIVE_RCH_JOBS").unwrap_or(0))
}

fn classify_swarm_admission(
    plan: SwarmCapacityPlan,
    sample: HostResourceSample,
    live_load: SwarmLiveLoad,
    warnings: &[String],
) -> Finding {
    let mut controller = SwarmAdmissionController::from_plan(plan);
    let queue_depth = u64_to_usize_saturating(live_load.active_tool_calls.saturating_add(1));
    let resource_pressure_ratio =
        resource_pressure_ratio(&sample, &controller.plan().resource_budgets);
    let tail_latency_sample = TailLatencyRegimeSample::new(
        controller.plan().evidence.max_p99_ms,
        controller.plan().evidence.max_p999_ms,
        queue_depth,
        resource_pressure_ratio,
    );
    let request = ResourceRequest::new(ResourceOperationKind::Tool, "doctor.swarm_admission")
        .with_estimated_tool_output_bytes(
            controller
                .plan()
                .resource_budgets
                .max_tool_output_bytes
                .min(MIB_BYTES),
        )
        .with_queue_depth(queue_depth);
    let decision = controller.decide(&request, sample, tail_latency_sample, live_load);
    let next_actions = swarm_admission_next_actions(decision.action, !warnings.is_empty());
    let remediation = next_actions.join("; ");
    let detail = format_swarm_admission_detail(&decision, warnings);
    let data = swarm_admission_data(&decision, warnings, &next_actions);

    match (decision.action, warnings.is_empty()) {
        (AdmissionAction::Admit, true) => {
            Finding::pass(CheckCategory::Swarm, "Live swarm admission decision: admit")
        }
        (AdmissionAction::Admit, false) => Finding::warn(
            CheckCategory::Swarm,
            "Live swarm admission decision degraded: admit",
        ),
        (AdmissionAction::Backpressure, _) => Finding::warn(
            CheckCategory::Swarm,
            "Live swarm admission decision: backpressure",
        ),
        (AdmissionAction::Deny, _) => {
            Finding::fail(CheckCategory::Swarm, "Live swarm admission decision: deny")
        }
    }
    .with_detail(detail)
    .with_remediation(remediation)
    .with_data(data)
}

fn swarm_admission_blocked_finding(
    severity: Severity,
    title: impl Into<String>,
    detail: String,
    warnings: &[String],
) -> Finding {
    let next_actions = vec![
        "Do not launch new swarm work while the admission action is deny".to_string(),
        "Repair or refresh the coordination inputs".to_string(),
        "Rerun `pi doctor --only swarm --format json`".to_string(),
    ];
    let remediation = next_actions.join("; ");
    let data = serde_json::json!({
        "schema": SWARM_DOCTOR_ADMISSION_SCHEMA,
        "source": {
            "capacity_plan": "unavailable",
            "live_load": "unavailable",
            "resource_sample": "unavailable"
        },
        "action": action_label(AdmissionAction::Deny),
        "reason": detail,
        "retry_after_ms": 0,
        "pressure_dimension": capacity_dimension_label(SwarmCapacityDimension::None),
        "capacity_pressure": null,
        "planned_budgets": null,
        "live_counts": null,
        "resource_sample": null,
        "admission_decision": null,
        "stale_data_warnings": warnings,
        "next_actions": next_actions
    });

    match severity {
        Severity::Pass => Finding::pass(CheckCategory::Swarm, title),
        Severity::Info => Finding::info(CheckCategory::Swarm, title),
        Severity::Warn => Finding::warn(CheckCategory::Swarm, title),
        Severity::Fail => Finding::fail(CheckCategory::Swarm, title),
    }
    .with_detail(detail)
    .with_remediation(remediation)
    .with_data(data)
}

fn swarm_admission_data(
    decision: &crate::resource_governor::SwarmAdmissionControllerDecision,
    warnings: &[String],
    next_actions: &[String],
) -> serde_json::Value {
    serde_json::json!({
        "schema": SWARM_DOCTOR_ADMISSION_SCHEMA,
        "source": {
            "capacity_plan": "doctor_live_host_sample",
            "live_load": "beads_ledger_with_env_overrides",
            "resource_sample": "host_resource_sample_current"
        },
        "action": action_label(decision.action),
        "reason": decision.reason,
        "retry_after_ms": decision.retry_after_ms,
        "pressure_dimension": capacity_dimension_label(decision.capacity_pressure.dimension),
        "capacity_pressure": decision.capacity_pressure,
        "planned_budgets": {
            "agent_concurrency": decision.recommended_agent_concurrency,
            "tool_concurrency": decision.recommended_tool_concurrency,
            "extension_hostcall_lanes": decision.recommended_extension_hostcall_lanes,
            "rch_verification_fanout": decision.recommended_rch_verification_fanout,
            "max_queue_depth": decision.resource_decision.budgets.max_queue_depth,
            "max_tool_output_bytes": decision.resource_decision.budgets.max_tool_output_bytes,
            "memory_pressure_threshold_ratio": decision.resource_decision.budgets.backpressure_ratio,
            "deny_ratio": decision.resource_decision.budgets.deny_ratio,
            "plan_confidence": decision.plan_confidence
        },
        "live_counts": decision.live_load,
        "resource_sample": decision.resource_decision.sample,
        "admission_decision": decision.telemetry(),
        "stale_data_warnings": warnings,
        "next_actions": next_actions
    })
}

fn format_swarm_admission_detail(
    decision: &crate::resource_governor::SwarmAdmissionControllerDecision,
    warnings: &[String],
) -> String {
    let pressure = decision.capacity_pressure;
    let mut detail = format!(
        "action={}, reason={}, retry_after_ms={}, pressure={} {}/{} ({:.2}x), live_agents={}, live_tool_calls={}, live_extension_lanes={}, live_rch_jobs={}, planned_agents={}, planned_tool_calls={}, planned_extension_lanes={}, planned_rch_jobs={}",
        action_label(decision.action),
        decision.reason,
        decision.retry_after_ms,
        capacity_dimension_label(pressure.dimension),
        pressure.observed,
        pressure.budget,
        pressure.ratio,
        decision.live_load.active_agents,
        decision.live_load.active_tool_calls,
        decision.live_load.extension_hostcall_lanes,
        decision.live_load.active_rch_jobs,
        decision.recommended_agent_concurrency,
        decision.recommended_tool_concurrency,
        decision.recommended_extension_hostcall_lanes,
        decision.recommended_rch_verification_fanout
    );
    if !warnings.is_empty() {
        detail.push_str("; stale_data_warnings=");
        detail.push_str(&warnings.join("; "));
    }
    detail
}

fn swarm_admission_next_actions(action: AdmissionAction, has_warnings: bool) -> Vec<String> {
    let mut actions = match action {
        AdmissionAction::Admit => vec![
            "Proceed with new swarm work only after current leases are visible".to_string(),
            "Keep CARGO_TARGET_DIR and TMPDIR on high-capacity storage before cargo checks"
                .to_string(),
        ],
        AdmissionAction::Backpressure => vec![
            "Delay new swarm work until the reported retry_after_ms has elapsed".to_string(),
            "Reduce active agents or tool calls on the pressure dimension".to_string(),
            "Rerun `pi doctor --only swarm --format json` before heavyweight cargo checks"
                .to_string(),
        ],
        AdmissionAction::Deny => vec![
            "Do not launch new swarm work while the admission action is deny".to_string(),
            "Stop or defer agents on the pressure dimension".to_string(),
            "Rerun `pi doctor --only swarm --format json` after pressure clears".to_string(),
        ],
    };
    if has_warnings {
        actions.push(
            "Repair stale or missing inputs before treating the decision as healthy".to_string(),
        );
    }
    actions
}

const fn action_label(action: AdmissionAction) -> &'static str {
    match action {
        AdmissionAction::Admit => "admit",
        AdmissionAction::Backpressure => "backpressure",
        AdmissionAction::Deny => "deny",
    }
}

const fn capacity_dimension_label(dimension: SwarmCapacityDimension) -> &'static str {
    match dimension {
        SwarmCapacityDimension::ActiveAgents => "active_agents",
        SwarmCapacityDimension::ActiveToolCalls => "active_tool_calls",
        SwarmCapacityDimension::ExtensionHostcallLanes => "extension_hostcall_lanes",
        SwarmCapacityDimension::RchVerificationFanout => "rch_verification_fanout",
        SwarmCapacityDimension::None => "none",
    }
}

fn resource_sample_warnings(sample: &HostResourceSample) -> Vec<String> {
    let mut warnings = Vec::new();
    if sample.load_avg_1m.is_none() {
        warnings.push("host load average unavailable".to_string());
    }
    if sample.rss_bytes.is_none() {
        warnings.push("process RSS sample unavailable".to_string());
    }
    if sample.process_count.is_none() {
        warnings.push("host process-count sample unavailable".to_string());
    }
    if sample.fd_count.is_none() {
        warnings.push("file-descriptor sample unavailable".to_string());
    }
    warnings
}

#[allow(clippy::cast_precision_loss)]
fn resource_pressure_ratio(sample: &HostResourceSample, budgets: &HostResourceBudgets) -> f64 {
    let mut ratio = 0.0_f64;
    if let Some(load_avg_1m) = sample.load_avg_1m {
        ratio = ratio.max(load_avg_1m / budgets.max_load_avg_1m.max(f64::EPSILON));
    }
    if let Some(rss_bytes) = sample.rss_bytes {
        ratio = ratio.max((rss_bytes as f64) / (budgets.max_rss_bytes.max(1) as f64));
    }
    if let Some(process_count) = sample.process_count {
        ratio = ratio.max((process_count as f64) / (budgets.max_processes.max(1) as f64));
    }
    if let Some(fd_count) = sample.fd_count {
        ratio = ratio.max((fd_count as f64) / (budgets.max_fds.max(1) as f64));
    }
    ratio
}

fn env_u64(key: &str) -> Option<u64> {
    std::env::var(key).ok()?.trim().parse::<u64>().ok()
}

const fn bytes_to_mib_ceil(bytes: u64) -> u64 {
    bytes.saturating_add(MIB_BYTES - 1) / MIB_BYTES
}

fn usize_to_u64(value: usize) -> u64 {
    u64::try_from(value).unwrap_or(u64::MAX)
}

fn u64_to_usize_saturating(value: u64) -> usize {
    usize::try_from(value).unwrap_or(usize::MAX)
}

fn summarize_beads_ledger(
    content: &str,
    now: DateTime<Utc>,
    stale_after_hours: i64,
) -> BeadsLedgerSummary {
    let mut summary = BeadsLedgerSummary::default();
    for line in content.lines().filter(|line| !line.trim().is_empty()) {
        summary.total += 1;
        let Ok(issue) = serde_json::from_str::<BeadsIssueRecord>(line) else {
            summary.parse_errors += 1;
            continue;
        };

        match issue.status.as_str() {
            "open" => {
                summary.open += 1;
                summary.active += 1;
            }
            "in_progress" => {
                summary.in_progress += 1;
                summary.active += 1;
                if let Some(stale) = stale_issue_from_record(issue, now, stale_after_hours) {
                    summary.stale_in_progress.push(stale);
                }
            }
            _ => {}
        }
    }
    summary
}

fn stale_issue_from_record(
    issue: BeadsIssueRecord,
    now: DateTime<Utc>,
    stale_after_hours: i64,
) -> Option<StaleIssue> {
    let updated_at = issue.updated_at?;
    let parsed = DateTime::parse_from_rfc3339(&updated_at).ok()?;
    let age_hours = now
        .signed_duration_since(parsed.with_timezone(&Utc))
        .num_hours();
    (age_hours >= stale_after_hours).then_some(StaleIssue {
        id: issue.id,
        title: issue.title,
        updated_at,
        age_hours,
    })
}

fn format_stale_issues(issues: &[StaleIssue]) -> String {
    let mut parts: Vec<String> = issues
        .iter()
        .take(SWARM_DETAIL_LIMIT)
        .map(|issue| {
            let title = truncate_chars(&issue.title, 54);
            format!("{}: {title} ({}h old)", issue.id, issue.age_hours)
        })
        .collect();
    if issues.len() > SWARM_DETAIL_LIMIT {
        parts.push(format!("+{} more", issues.len() - SWARM_DETAIL_LIMIT));
    }
    parts.join("; ")
}

fn check_swarm_br_status(cwd: &Path, findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Swarm;
    if which_tool("br").is_none() {
        findings.push(
            Finding::warn(cat, "br not found for Beads DB status")
                .with_remediation("Install/repair beads_rust before starting a swarm"),
        );
        return;
    }
    let args = ["sync", "--status", "--json"];
    match run_tool_with_timeout(SwarmProbeCommand::Br, &args, Some(cwd), SWARM_PROBE_TIMEOUT) {
        Ok(outcome) if outcome.timed_out => {
            findings.push(
                Finding::warn(cat, "br sync status timed out").with_remediation(
                    "Run `br doctor --json`; avoid claiming work until Beads responds",
                ),
            );
        }
        Ok(outcome) if outcome.success => {
            match serde_json::from_str::<serde_json::Value>(&outcome.stdout) {
                Ok(value) => findings.push(classify_br_sync_status(&value)),
                Err(err) => findings.push(
                    Finding::warn(cat, "br sync status returned non-JSON output")
                        .with_detail(format!("{err}; {}", redacted_output_snippet(&outcome)))
                        .with_remediation(
                            "Run `br sync --status --json` and `br doctor --json` manually",
                        ),
                ),
            }
        }
        Ok(outcome) => {
            findings.push(
                Finding::fail(cat, "Beads DB/status probe failed")
                    .with_detail(command_failure_detail(&outcome))
                    .with_remediation("Run `br doctor --json` and rebuild from healthy issues.jsonl before claiming more work"),
            );
        }
        Err(err) => {
            findings.push(
                Finding::warn(cat, "br sync status failed to start")
                    .with_detail(err.to_string())
                    .with_remediation("Run `br doctor --json` before claiming more work"),
            );
        }
    }
}

fn classify_br_sync_status(value: &serde_json::Value) -> Finding {
    let dirty_count = json_number_by_key(value, "dirty_count").unwrap_or(0);
    let jsonl_newer = json_bool_by_key(value, "jsonl_newer").unwrap_or(false);
    let db_newer = json_bool_by_key(value, "db_newer").unwrap_or(false);
    let detail =
        format!("dirty_count={dirty_count}, jsonl_newer={jsonl_newer}, db_newer={db_newer}");
    if dirty_count > 0 || jsonl_newer || db_newer {
        Finding::warn(CheckCategory::Swarm, "Beads DB/JSONL sync drift detected")
            .with_detail(detail)
            .with_remediation(
                "Run `br sync --status --json`; coordinate before importing or exporting",
            )
    } else {
        Finding::pass(CheckCategory::Swarm, "Beads DB/JSONL sync status clean").with_detail(detail)
    }
}

fn check_swarm_agent_mail(cwd: &Path, findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Swarm;
    let Some(am_path) = which_tool("am") else {
        findings.push(
            Finding::warn(cat, "Agent Mail CLI not found")
                .with_remediation("Install/repair MCP Agent Mail before running a large swarm"),
        );
        return;
    };

    findings.push(Finding::pass(cat, format!("Agent Mail CLI ({am_path})")));

    let agent_name = first_non_empty_env(&["AGENT_MAIL_AGENT", "AGENT_NAME"]);
    let project = cwd.display().to_string();

    if let Some(agent) = agent_name.as_deref() {
        findings.push(Finding::pass(
            cat,
            format!("Agent Mail agent identity: {agent}"),
        ));
        let status_args = [
            "robot",
            "status",
            "--format",
            "json",
            "--project",
            project.as_str(),
            "--agent",
            agent,
        ];
        probe_agent_mail_json(
            SwarmProbeCommand::Am,
            &status_args,
            cwd,
            "Agent Mail status",
            classify_agent_mail_status,
            "Retry after active Agent Mail maintenance finishes; if it stays busy, inspect `am robot health --format json`",
            findings,
        );

        let inbox_args = [
            "robot",
            "inbox",
            "--format",
            "json",
            "--project",
            project.as_str(),
            "--agent",
            agent,
            "--unread",
            "--limit",
            "20",
        ];
        probe_agent_mail_json(
            SwarmProbeCommand::Am,
            &inbox_args,
            cwd,
            "Agent Mail inbox",
            classify_agent_mail_inbox,
            "Run `am robot inbox --unread --format json` or fetch inbox through MCP before claiming more files",
            findings,
        );
    } else {
        findings.push(
            Finding::warn(cat, "Agent Mail agent identity not set")
                .with_detail("AGENT_MAIL_AGENT and AGENT_NAME are both unset")
                .with_remediation(
                    "Register with MCP Agent Mail and export AGENT_NAME before claiming swarm work",
                ),
        );
    }

    let reservations_args = [
        "robot",
        "reservations",
        "--format",
        "json",
        "--project",
        project.as_str(),
        "--all",
        "--expiring",
        "30",
    ];
    probe_agent_mail_json(
        SwarmProbeCommand::Am,
        &reservations_args,
        cwd,
        "Agent Mail reservations",
        classify_agent_mail_reservations,
        "Run `am robot reservations --all --format json` and resolve conflicts or renew expiring leases",
        findings,
    );
}

fn probe_agent_mail_json<F>(
    command: SwarmProbeCommand,
    args: &[&str],
    cwd: &Path,
    label: &str,
    classify: F,
    remediation: &str,
    findings: &mut Vec<Finding>,
) where
    F: FnOnce(&serde_json::Value) -> Finding,
{
    let cat = CheckCategory::Swarm;
    match run_tool_with_timeout(command, args, Some(cwd), SWARM_PROBE_TIMEOUT) {
        Ok(outcome) if outcome.timed_out => {
            findings.push(
                Finding::warn(cat, format!("{label} probe timed out"))
                    .with_detail(format!(
                        "command: am {}; timeout={}s",
                        args.join(" "),
                        SWARM_PROBE_TIMEOUT.as_secs()
                    ))
                    .with_remediation(remediation),
            );
        }
        Ok(outcome) if !outcome.success => {
            findings.push(
                Finding::warn(cat, format!("{label} probe unavailable"))
                    .with_detail(command_failure_detail(&outcome))
                    .with_remediation(remediation),
            );
        }
        Ok(outcome) => match serde_json::from_str::<serde_json::Value>(&outcome.stdout) {
            Ok(value) => findings.push(classify(&value)),
            Err(err) => findings.push(
                Finding::warn(cat, format!("{label} probe returned non-JSON output"))
                    .with_detail(format!("{err}; {}", redacted_output_snippet(&outcome)))
                    .with_remediation(remediation),
            ),
        },
        Err(err) => findings.push(
            Finding::warn(cat, format!("{label} probe failed to start"))
                .with_detail(err.to_string())
                .with_remediation(remediation),
        ),
    }
}

fn classify_agent_mail_status(value: &serde_json::Value) -> Finding {
    let unread = json_number_by_key(value, "unread").unwrap_or(0);
    let urgent = json_number_by_key(value, "urgent").unwrap_or(0);
    let ack_required = json_number_by_key(value, "ack_required").unwrap_or(0);
    let mut finding = if urgent > 0 || ack_required > 0 {
        Finding::warn(CheckCategory::Swarm, "Agent Mail status needs attention")
            .with_remediation("Read urgent or ack-required messages before taking new leases")
    } else {
        Finding::pass(CheckCategory::Swarm, "Agent Mail status reachable")
    };
    finding.detail = Some(format!(
        "unread={unread}, urgent={urgent}, ack_required={ack_required}"
    ));
    finding
}

fn classify_agent_mail_inbox(value: &serde_json::Value) -> Finding {
    let messages = json_array_len_by_key(value, "messages")
        .or_else(|| json_array_len_by_key(value, "items"))
        .unwrap_or_else(|| json_number_by_key_as_usize(value, "unread").unwrap_or(0));
    let urgent = json_number_by_key(value, "urgent").unwrap_or(0);
    if urgent > 0 {
        Finding::warn(
            CheckCategory::Swarm,
            "Agent Mail inbox has urgent unread messages",
        )
        .with_detail(format!("unread_sample={messages}, urgent={urgent}"))
        .with_remediation("Read or acknowledge urgent mail before claiming more files")
    } else {
        Finding::pass(CheckCategory::Swarm, "Agent Mail inbox reachable")
            .with_detail(format!("unread_sample={messages}, urgent={urgent}"))
    }
}

fn classify_agent_mail_reservations(value: &serde_json::Value) -> Finding {
    let active = json_array_len_by_key(value, "reservations")
        .or_else(|| json_array_len_by_key(value, "items"))
        .unwrap_or_else(|| json_number_by_key_as_usize(value, "active").unwrap_or(0));
    let has_conflict = json_truthy_key_contains(value, "conflict");
    let expiring = json_number_by_key(value, "expiring").unwrap_or(0);
    if has_conflict {
        Finding::warn(
            CheckCategory::Swarm,
            "Agent Mail reservation conflicts detected",
        )
        .with_detail(format!("active={active}, expiring_soon={expiring}"))
        .with_remediation("Resolve conflicting file reservations before editing overlapping paths")
    } else if expiring > 0 {
        Finding::warn(CheckCategory::Swarm, "Agent Mail reservations expire soon")
            .with_detail(format!("active={active}, expiring_soon={expiring}"))
            .with_remediation("Renew active reservations before long-running verification")
    } else {
        Finding::pass(CheckCategory::Swarm, "Agent Mail reservations reachable")
            .with_detail(format!("active={active}, expiring_soon={expiring}"))
    }
}

fn check_swarm_git(cwd: &Path, findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Swarm;
    if which_tool("git").is_none() {
        findings.push(
            Finding::warn(cat, "git not found for swarm dirty-state check")
                .with_remediation("Install git or run the dirty-state check manually"),
        );
        return;
    }
    let args = ["status", "--porcelain=v1", "--untracked-files=all"];
    match run_tool_with_timeout(
        SwarmProbeCommand::Git,
        &args,
        Some(cwd),
        SWARM_PROBE_TIMEOUT,
    ) {
        Ok(outcome) if outcome.timed_out => {
            findings.push(
                Finding::warn(cat, "git status timed out")
                    .with_remediation("Run `git status --short` before staging or committing"),
            );
        }
        Ok(outcome) if outcome.success => {
            let summary = summarize_git_porcelain(&outcome.stdout);
            if summary.total.eq(&0) {
                findings.push(Finding::pass(cat, "Git working tree clean"));
            } else {
                findings.push(
                    Finding::warn(cat, "Git working tree has uncommitted changes")
                        .with_detail(format!(
                            "{} total ({} staged, {} unstaged, {} untracked, {} deleted)",
                            summary.total,
                            summary.staged,
                            summary.unstaged,
                            summary.untracked,
                            summary.deleted
                        ))
                        .with_remediation(
                            "Run `git status --short` and avoid overwriting other agents' files",
                        ),
                );
            }
        }
        Ok(outcome) => {
            findings.push(
                Finding::warn(cat, "git status failed")
                    .with_detail(command_failure_detail(&outcome))
                    .with_remediation("Run `git status --short` manually before staging"),
            );
        }
        Err(err) => {
            findings.push(
                Finding::warn(cat, "git status failed to start")
                    .with_detail(err.to_string())
                    .with_remediation("Run `git status --short` manually before staging"),
            );
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct GitPorcelainSummary {
    staged: usize,
    unstaged: usize,
    untracked: usize,
    deleted: usize,
    total: usize,
}

fn summarize_git_porcelain(output: &str) -> GitPorcelainSummary {
    let mut summary = GitPorcelainSummary::default();
    for line in output.lines().filter(|line| !line.trim().is_empty()) {
        summary.total += 1;
        let bytes = line.as_bytes();
        let x = bytes.first().copied().unwrap_or(b' ');
        let y = bytes.get(1).copied().unwrap_or(b' ');
        if x.eq(&b'?') && y.eq(&b'?') {
            summary.untracked += 1;
            continue;
        }
        if x.ne(&b' ') {
            summary.staged += 1;
        }
        if y.ne(&b' ') {
            summary.unstaged += 1;
        }
        if x.eq(&b'D') || y.eq(&b'D') {
            summary.deleted += 1;
        }
    }
    summary
}

fn check_swarm_rch(findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Swarm;
    if which_tool("rch").is_none() {
        findings.push(
            Finding::warn(cat, "rch not found").with_remediation(
                "Install/repair rch before running heavyweight swarm verification",
            ),
        );
        return;
    }
    match run_tool_with_timeout(
        SwarmProbeCommand::Rch,
        &["status"],
        None,
        SWARM_PROBE_TIMEOUT,
    ) {
        Ok(outcome) if outcome.timed_out => {
            findings.push(Finding::warn(cat, "rch status timed out").with_remediation(
                "Run `rch status` or defer heavyweight cargo checks until workers respond",
            ));
        }
        Ok(outcome) if outcome.success => {
            findings.push(
                Finding::pass(cat, "rch status reachable")
                    .with_detail(redacted_output_snippet(&outcome)),
            );
        }
        Ok(outcome) => {
            findings.push(
                Finding::warn(cat, "rch status failed")
                    .with_detail(command_failure_detail(&outcome))
                    .with_remediation("Run `rch doctor` and use a high-capacity local target dir if rch is unavailable"),
            );
        }
        Err(err) => {
            findings.push(
                Finding::warn(cat, "rch status failed to start")
                    .with_detail(err.to_string())
                    .with_remediation("Run `rch doctor` and use a high-capacity local target dir if rch is unavailable"),
            );
        }
    }
}

fn check_swarm_temp_dirs(findings: &mut Vec<Finding>) {
    check_swarm_temp_dir("CARGO_TARGET_DIR", findings);
    check_swarm_temp_dir("TMPDIR", findings);
}

fn check_swarm_temp_dir(env_name: &str, findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Swarm;
    let Some(raw_path) = std::env::var_os(env_name) else {
        findings.push(
            Finding::warn(cat, format!("{env_name} is not set"))
                .with_detail("Heavyweight swarm checks should use /data/tmp/pi_agent_rust_cargo/<agent>")
                .with_remediation("Export CARGO_TARGET_DIR and TMPDIR under /data/tmp/pi_agent_rust_cargo/<agent>/ before cargo checks"),
        );
        return;
    };
    let path = PathBuf::from(raw_path);
    if !path.is_dir() {
        findings.push(
            Finding::warn(cat, format!("{env_name} does not point to a directory"))
                .with_detail(path.display().to_string())
                .with_remediation(format!(
                    "Create {} or export {env_name} to an existing high-capacity directory",
                    path.display()
                )),
        );
        return;
    }

    match disk_available_kb(&path) {
        Ok(Some(available_kb)) if available_kb < SWARM_DISK_WARN_AVAILABLE_KB => {
            findings.push(
                Finding::warn(cat, format!("{env_name} has low free space"))
                    .with_detail(format!(
                        "{} available at {}",
                        format_available_kb(available_kb),
                        path.display()
                    ))
                    .with_remediation("Switch to a larger /data/tmp target or wait for cleanup before heavy cargo checks"),
            );
        }
        Ok(Some(available_kb)) => {
            findings.push(
                Finding::pass(cat, format!("{env_name} headroom")).with_detail(format!(
                    "{} available at {}",
                    format_available_kb(available_kb),
                    path.display()
                )),
            );
        }
        Ok(None) => {
            findings.push(
                Finding::info(cat, format!("{env_name} headroom unavailable"))
                    .with_detail(path.display().to_string())
                    .with_remediation(
                        "Run `df -h` on the configured path before heavy cargo checks",
                    ),
            );
        }
        Err(err) => {
            findings.push(
                Finding::info(cat, format!("{env_name} headroom probe failed"))
                    .with_detail(err.to_string())
                    .with_remediation(
                        "Run `df -h` on the configured path before heavy cargo checks",
                    ),
            );
        }
    }
}

fn disk_available_kb(path: &Path) -> std::io::Result<Option<u64>> {
    if which_tool("df").is_none() {
        return Ok(None);
    }
    let path_arg = path.display().to_string();
    let outcome = run_tool_with_timeout(
        SwarmProbeCommand::Df,
        &["-Pk", path_arg.as_str()],
        None,
        SWARM_PROBE_TIMEOUT,
    )?;
    if outcome.success {
        Ok(parse_df_available_kb(&outcome.stdout))
    } else {
        Ok(None)
    }
}

fn parse_df_available_kb(output: &str) -> Option<u64> {
    output
        .lines()
        .skip(1)
        .find_map(|line| line.split_whitespace().nth(3)?.parse::<u64>().ok())
}

fn format_available_kb(kb: u64) -> String {
    if kb >= 1024 * 1024 {
        let tenths = kb.saturating_mul(10) / (1024 * 1024);
        format!("{}.{:01} GiB", tenths / 10, tenths % 10)
    } else {
        let tenths = kb.saturating_mul(10) / 1024;
        format!("{}.{:01} MiB", tenths / 10, tenths % 10)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct CommandOutcome {
    timed_out: bool,
    success: bool,
    status_code: Option<i32>,
    stdout: String,
    stderr: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SwarmProbeCommand {
    Am,
    Br,
    Df,
    Git,
    Rch,
}

fn run_tool_with_timeout(
    tool: SwarmProbeCommand,
    args: &[&str],
    cwd: Option<&Path>,
    timeout: Duration,
) -> std::io::Result<CommandOutcome> {
    let mut command = match tool {
        SwarmProbeCommand::Am => Command::new("am"),
        SwarmProbeCommand::Br => Command::new("br"),
        SwarmProbeCommand::Df => Command::new("df"),
        SwarmProbeCommand::Git => Command::new("git"),
        SwarmProbeCommand::Rch => Command::new("rch"),
    };
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(cwd) = cwd {
        command.current_dir(cwd);
    }

    let mut child = command.spawn()?;
    let started = Instant::now();
    let mut timed_out = false;
    loop {
        if child.try_wait()?.is_some() {
            break;
        }
        if started.elapsed() >= timeout {
            timed_out = true;
            let _ = child.kill();
            break;
        }
        thread::sleep(Duration::from_millis(25));
    }

    let output = child.wait_with_output()?;
    Ok(CommandOutcome {
        timed_out,
        success: output.status.success(),
        status_code: output.status.code(),
        stdout: String::from_utf8_lossy(&output.stdout).to_string(),
        stderr: String::from_utf8_lossy(&output.stderr).to_string(),
    })
}

fn command_failure_detail(outcome: &CommandOutcome) -> String {
    format!(
        "exit={:?}; {}",
        outcome.status_code,
        redacted_output_snippet(outcome)
    )
}

fn redacted_output_snippet(outcome: &CommandOutcome) -> String {
    let stdout = redact_sensitive_lines(outcome.stdout.trim(), 3);
    if !stdout.is_empty() {
        return stdout;
    }
    let stderr = redact_sensitive_lines(outcome.stderr.trim(), 3);
    if !stderr.is_empty() {
        return stderr;
    }
    "no output".to_string()
}

fn redact_sensitive_lines(text: &str, max_lines: usize) -> String {
    text.lines()
        .take(max_lines)
        .map(|line| {
            if line_is_sensitive(line) {
                "[redacted sensitive output line]".to_string()
            } else {
                truncate_chars(line.trim(), 220)
            }
        })
        .collect::<Vec<_>>()
        .join(" | ")
}

fn line_is_sensitive(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    [
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "credential",
        "password",
        "secret",
        "token",
    ]
    .iter()
    .any(|needle| lower.contains(needle))
}

fn truncate_chars(text: &str, max_chars: usize) -> String {
    if text.chars().count() <= max_chars {
        return text.to_string();
    }
    let mut truncated = text.chars().take(max_chars).collect::<String>();
    truncated.push_str("...");
    truncated
}

fn first_non_empty_env(keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| {
        std::env::var(key)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    })
}

fn json_number_by_key(value: &serde_json::Value, key: &str) -> Option<u64> {
    match value {
        serde_json::Value::Object(map) => {
            if let Some(found) = map.get(key).and_then(json_value_as_u64) {
                return Some(found);
            }
            map.values()
                .find_map(|child| json_number_by_key(child, key))
        }
        serde_json::Value::Array(values) => values
            .iter()
            .find_map(|child| json_number_by_key(child, key)),
        _ => None,
    }
}

fn json_number_by_key_as_usize(value: &serde_json::Value, key: &str) -> Option<usize> {
    json_number_by_key(value, key).and_then(|number| usize::try_from(number).ok())
}

fn json_array_len_by_key(value: &serde_json::Value, key: &str) -> Option<usize> {
    match value {
        serde_json::Value::Object(map) => {
            if let Some(serde_json::Value::Array(values)) = map.get(key) {
                return Some(values.len());
            }
            map.values()
                .find_map(|child| json_array_len_by_key(child, key))
        }
        serde_json::Value::Array(values) => values
            .iter()
            .find_map(|child| json_array_len_by_key(child, key)),
        _ => None,
    }
}

fn json_bool_by_key(value: &serde_json::Value, key: &str) -> Option<bool> {
    match value {
        serde_json::Value::Object(map) => {
            if let Some(serde_json::Value::Bool(found)) = map.get(key) {
                return Some(*found);
            }
            map.values().find_map(|child| json_bool_by_key(child, key))
        }
        serde_json::Value::Array(values) => {
            values.iter().find_map(|child| json_bool_by_key(child, key))
        }
        _ => None,
    }
}

fn json_truthy_key_contains(value: &serde_json::Value, needle: &str) -> bool {
    match value {
        serde_json::Value::Object(map) => map.iter().any(|(key, child)| {
            (key.to_ascii_lowercase().contains(needle) && json_value_is_truthy(child))
                || json_truthy_key_contains(child, needle)
        }),
        serde_json::Value::Array(values) => values
            .iter()
            .any(|child| json_truthy_key_contains(child, needle)),
        _ => false,
    }
}

fn json_value_as_u64(value: &serde_json::Value) -> Option<u64> {
    match value {
        serde_json::Value::Number(number) => number.as_u64(),
        serde_json::Value::Array(values) => Some(values.len() as u64),
        _ => None,
    }
}

fn json_value_is_truthy(value: &serde_json::Value) -> bool {
    match value {
        serde_json::Value::Bool(value) => *value,
        serde_json::Value::Number(number) => number.as_u64().is_some_and(|value| value > 0),
        serde_json::Value::String(value) => {
            let normalized = value.trim().to_ascii_lowercase();
            !normalized.is_empty()
                && normalized.ne("0")
                && normalized.ne("false")
                && normalized.ne("none")
        }
        serde_json::Value::Array(values) => !values.is_empty(),
        serde_json::Value::Object(map) => !map.is_empty(),
        serde_json::Value::Null => false,
    }
}

// ── Check: Sessions ─────────────────────────────────────────────────

fn check_sessions(findings: &mut Vec<Finding>) {
    let cat = CheckCategory::Sessions;
    let sessions_dir = Config::sessions_dir();

    if !sessions_dir.is_dir() {
        findings.push(Finding::info(
            cat,
            "Sessions directory does not exist (no sessions yet)",
        ));
        return;
    }

    let entries = walk_sessions(&sessions_dir);
    let total = entries.len().min(500); // Cap scan
    let mut corrupt = 0u32;

    for entry in entries.into_iter().take(500) {
        let Ok(path) = entry else {
            corrupt += 1;
            continue;
        };
        if !is_session_healthy(&path) {
            corrupt += 1;
        }
    }

    if corrupt.eq(&0) {
        findings.push(Finding::pass(cat, format!("{total} sessions, 0 corrupt")));
    } else {
        findings.push(
            Finding::warn(cat, format!("{total} sessions, {corrupt} corrupt"))
                .with_detail("Some session files are empty or have invalid headers")
                .with_remediation("Corrupt sessions can be safely deleted"),
        );
    }
}

/// Quick health check: non-empty and first line parses as a valid session header.
fn is_session_healthy(path: &Path) -> bool {
    #[cfg(feature = "sqlite-sessions")]
    if path
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq("sqlite"))
    {
        return futures::executor::block_on(async {
            crate::session_sqlite::load_session_meta(path)
                .await
                .is_ok_and(|meta| meta.header.is_valid())
        });
    }

    let Ok(file) = std::fs::File::open(path) else {
        return false;
    };
    let mut reader = BufReader::new(file);
    let mut line = String::new();
    match reader.read_line(&mut line) {
        Ok(0) | Err(_) => false, // empty or unreadable
        Ok(_) => serde_json::from_str::<SessionHeader>(&line).is_ok_and(|header| header.is_valid()),
    }
}

// ── Check: Extension ────────────────────────────────────────────────

fn check_extension(
    cwd: &Path,
    path: &str,
    policy_override: Option<&str>,
    findings: &mut Vec<Finding>,
) {
    use crate::extension_preflight::{FindingSeverity, PreflightAnalyzer, PreflightVerdict};

    let cat = CheckCategory::Extensions;
    let ext_path = if Path::new(path).is_absolute() {
        PathBuf::from(path)
    } else {
        cwd.join(path)
    };

    if !ext_path.exists() {
        findings.push(
            Finding::fail(
                cat,
                format!("Extension path not found: {}", ext_path.display()),
            )
            .with_remediation("Check the path and try again"),
        );
        return;
    }

    let config_path = Config::config_path_override_from_env(cwd);
    let resolved = match Config::load_with_roots(config_path.as_deref(), &Config::global_dir(), cwd)
    {
        Ok(config) => config.resolve_extension_policy_with_metadata(policy_override),
        Err(err) => {
            findings.push(
                Finding::fail(
                    cat,
                    "Failed to load configuration for extension policy resolution",
                )
                .with_detail(err.to_string())
                .with_remediation(
                    "Fix the malformed settings.json, point PI_CONFIG_PATH at a valid file, or rerun with `--policy <safe|balanced|permissive>` to inspect extension compatibility independently",
                ),
            );
            let has_explicit_policy =
                policy_override.is_some() || std::env::var_os("PI_EXTENSION_POLICY").is_some();
            if has_explicit_policy {
                Config::default().resolve_extension_policy_with_metadata(policy_override)
            } else {
                // If project config is unreadable, fail closed instead of silently
                // analyzing under the default permissive profile.
                Config::default().resolve_extension_policy_with_metadata(Some("safe"))
            }
        }
    };
    let ext_id = ext_path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("unknown");

    let analyzer = PreflightAnalyzer::new(&resolved.policy, Some(ext_id));
    let report = analyzer.analyze(&ext_path);

    // Convert preflight verdict to a top-level finding
    match report.verdict {
        PreflightVerdict::Pass => {
            findings.push(Finding::pass(
                cat,
                format!("Extension {ext_id}: compatible"),
            ));
        }
        PreflightVerdict::Warn => {
            findings.push(
                Finding::warn(cat, format!("Extension {ext_id}: partial compatibility"))
                    .with_detail(format!(
                        "{} warning(s), {} info",
                        report.summary.warnings, report.summary.info
                    )),
            );
        }
        PreflightVerdict::Fail => {
            findings.push(
                Finding::fail(cat, format!("Extension {ext_id}: incompatible"))
                    .with_detail(format!(
                        "{} error(s), {} warning(s)",
                        report.summary.errors, report.summary.warnings
                    ))
                    .with_remediation(format!("Try: pi doctor {path} --policy permissive")),
            );
        }
    }

    // Convert individual preflight findings
    for pf in &report.findings {
        let severity = match pf.severity {
            FindingSeverity::Error => Severity::Fail,
            FindingSeverity::Warning => Severity::Warn,
            FindingSeverity::Info => Severity::Info,
        };
        let mut f = Finding {
            category: cat,
            severity,
            title: pf.message.clone(),
            detail: pf.file.as_ref().map(|file| {
                pf.line
                    .map_or_else(|| format!("at {file}"), |line| format!("at {file}:{line}"))
            }),
            remediation: pf.remediation.clone(),
            data: None,
            fixability: Fixability::NotFixable,
        };
        // Ensure we don't lose location info
        if f.detail.is_none() && pf.file.is_some() {
            f.detail.clone_from(&pf.file);
        }
        findings.push(f);
    }
}

// ── Tests ───────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::{Path, PathBuf};

    fn write_extension_fixture(cwd: &Path, source: &str) -> PathBuf {
        let extension_dir = cwd.join("ext");
        std::fs::create_dir_all(&extension_dir).expect("create extension dir");
        std::fs::write(extension_dir.join("index.js"), source).expect("write extension source");
        extension_dir
    }

    fn test_swarm_capacity_plan() -> SwarmCapacityPlan {
        let evidence = SwarmCapacityEvidenceSummary {
            complete_records: 5,
            host_capacity_rows: 5,
            host_capacity_mismatch_rows: 0,
            max_p99_ms: 100,
            max_p999_ms: 300,
            max_queue_depth: 16,
            max_rss_mb: 256,
            max_cpu_pct: 40.0,
        };
        SwarmCapacityPlannerConfig::default()
            .plan_from_summary(evidence, SwarmHostInventory::new(8, 8, 32_768))
            .expect("test capacity plan")
    }

    fn healthy_resource_sample() -> HostResourceSample {
        HostResourceSample {
            load_avg_1m: Some(1.0),
            rss_bytes: Some(128 * MIB_BYTES),
            process_count: Some(16),
            fd_count: Some(64),
        }
    }

    fn finding_data(finding: &Finding) -> &serde_json::Value {
        finding.data.as_ref().expect("structured finding data")
    }

    #[test]
    fn severity_ordering() {
        assert!(Severity::Pass < Severity::Info);
        assert!(Severity::Info < Severity::Warn);
        assert!(Severity::Warn < Severity::Fail);
    }

    #[test]
    fn severity_display() {
        assert_eq!(Severity::Pass.to_string(), "PASS");
        assert_eq!(Severity::Fail.to_string(), "FAIL");
    }

    #[test]
    fn check_category_parse() {
        assert_eq!(
            "config".parse::<CheckCategory>().unwrap(),
            CheckCategory::Config
        );
        assert_eq!(
            "dirs".parse::<CheckCategory>().unwrap(),
            CheckCategory::Dirs
        );
        assert_eq!(
            "directories".parse::<CheckCategory>().unwrap(),
            CheckCategory::Dirs
        );
        assert_eq!(
            "auth".parse::<CheckCategory>().unwrap(),
            CheckCategory::Auth
        );
        assert_eq!(
            "shell".parse::<CheckCategory>().unwrap(),
            CheckCategory::Shell
        );
        assert_eq!(
            "sessions".parse::<CheckCategory>().unwrap(),
            CheckCategory::Sessions
        );
        assert_eq!(
            "swarm".parse::<CheckCategory>().unwrap(),
            CheckCategory::Swarm
        );
        assert_eq!(
            "coordination".parse::<CheckCategory>().unwrap(),
            CheckCategory::Swarm
        );
        assert_eq!(
            "leases".parse::<CheckCategory>().unwrap(),
            CheckCategory::Swarm
        );
        assert_eq!(
            "extensions".parse::<CheckCategory>().unwrap(),
            CheckCategory::Extensions
        );
        assert_eq!(
            "ext".parse::<CheckCategory>().unwrap(),
            CheckCategory::Extensions
        );
        assert!("unknown".parse::<CheckCategory>().is_err());
    }

    #[test]
    fn finding_builders() {
        let f = Finding::pass(CheckCategory::Config, "test")
            .with_detail("detail")
            .with_remediation("fix it");
        assert_eq!(f.severity, Severity::Pass);
        assert_eq!(f.detail.as_deref(), Some("detail"));
        assert_eq!(f.remediation.as_deref(), Some("fix it"));

        let f = Finding::warn(CheckCategory::Auth, "warn test").auto_fixable();
        assert_eq!(f.fixability, Fixability::AutoFixable);

        let f = Finding::fail(CheckCategory::Dirs, "fail test").fixed();
        assert_eq!(f.severity, Severity::Pass); // fixed downgrades to pass
        assert_eq!(f.fixability, Fixability::Fixed);
    }

    #[test]
    fn report_summary() {
        let findings = vec![
            Finding::pass(CheckCategory::Config, "ok"),
            Finding::info(CheckCategory::Auth, "info"),
            Finding::warn(CheckCategory::Shell, "warn"),
            Finding::fail(CheckCategory::Dirs, "fail"),
        ];
        let report = DoctorReport::from_findings(findings);
        assert_eq!(report.summary.pass, 1);
        assert_eq!(report.summary.info, 1);
        assert_eq!(report.summary.warn, 1);
        assert_eq!(report.summary.fail, 1);
        assert_eq!(report.overall, Severity::Fail);
    }

    #[test]
    fn report_all_pass() {
        let findings = vec![
            Finding::pass(CheckCategory::Config, "a"),
            Finding::pass(CheckCategory::Dirs, "b"),
        ];
        let report = DoctorReport::from_findings(findings);
        assert_eq!(report.overall, Severity::Pass);
    }

    #[test]
    fn render_text_includes_header() {
        let report =
            DoctorReport::from_findings(vec![Finding::pass(CheckCategory::Config, "all good")]);
        let text = report.render_text();
        assert!(text.contains("Pi Doctor"));
        assert!(text.contains("[PASS] Configuration"));
        assert!(text.contains("[PASS] all good"));
    }

    #[test]
    fn render_text_includes_swarm_category() {
        let report =
            DoctorReport::from_findings(vec![Finding::pass(CheckCategory::Swarm, "ready")]);
        let text = report.render_text();
        assert!(text.contains("[PASS] Swarm Coordination"));
        assert!(text.contains("[PASS] ready"));
    }

    #[test]
    fn render_json_valid() {
        let report = DoctorReport::from_findings(vec![Finding::pass(CheckCategory::Config, "ok")]);
        let json = report.to_json().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert!(parsed.get("findings").is_some());
        assert!(parsed.get("summary").is_some());
        assert!(parsed.get("overall").is_some());
    }

    #[test]
    fn render_markdown_includes_header() {
        let report =
            DoctorReport::from_findings(vec![Finding::warn(CheckCategory::Auth, "expired")]);
        let md = report.render_markdown();
        assert!(md.contains("# Pi Doctor Report"));
        assert!(md.contains("## Authentication"));
    }

    #[test]
    fn known_config_keys_includes_common() {
        assert!(is_known_config_key("theme"));
        assert!(is_known_config_key("defaultModel"));
        assert!(is_known_config_key("extensionPolicy"));
        assert!(!is_known_config_key("nonexistent_key_xyz"));
    }

    #[test]
    fn swarm_beads_summary_detects_stale_in_progress() {
        let now = DateTime::parse_from_rfc3339("2026-05-08T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let content = r#"{"id":"bd-old","title":"Old owner","status":"in_progress","updated_at":"2026-05-07T11:00:00Z"}
{"id":"bd-fresh","title":"Fresh owner","status":"in_progress","updated_at":"2026-05-08T11:30:00Z"}
{"id":"bd-open","title":"Open work","status":"open","updated_at":"2026-05-08T10:00:00Z"}
"#;

        let summary = summarize_beads_ledger(content, now, 24);

        assert_eq!(summary.total, 3);
        assert_eq!(summary.active, 3);
        assert_eq!(summary.open, 1);
        assert_eq!(summary.in_progress, 2);
        assert_eq!(summary.parse_errors, 0);
        assert_eq!(summary.stale_in_progress.len(), 1);
        assert_eq!(summary.stale_in_progress[0].id.as_str(), "bd-old");
    }

    #[test]
    fn swarm_beads_summary_counts_parse_errors() {
        let now = DateTime::parse_from_rfc3339("2026-05-08T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let content = r#"{"id":"bd-open","title":"Open work","status":"open","updated_at":"2026-05-08T10:00:00Z"}
not-json
"#;

        let summary = summarize_beads_ledger(content, now, 24);

        assert_eq!(summary.total, 2);
        assert_eq!(summary.open, 1);
        assert_eq!(summary.parse_errors, 1);
    }

    #[test]
    fn swarm_admission_reports_healthy_decision_data() {
        let finding = classify_swarm_admission(
            test_swarm_capacity_plan(),
            healthy_resource_sample(),
            SwarmLiveLoad::empty()
                .with_active_agents(1)
                .with_active_tool_calls(1),
            &[],
        );

        assert_eq!(finding.severity, Severity::Pass);
        let data = finding_data(&finding);
        assert_eq!(data["schema"], SWARM_DOCTOR_ADMISSION_SCHEMA);
        assert_eq!(data["action"], "admit");
        assert_eq!(data["pressure_dimension"], "active_agents");
        assert_eq!(data["live_counts"]["active_agents"], serde_json::json!(1));
        assert_eq!(
            data["planned_budgets"]["agent_concurrency"],
            serde_json::json!(4)
        );
        assert!(data["admission_decision"].is_object());
    }

    #[test]
    fn swarm_admission_reports_backpressure_guidance() {
        let finding = classify_swarm_admission(
            test_swarm_capacity_plan(),
            healthy_resource_sample(),
            SwarmLiveLoad::empty().with_active_agents(3),
            &[],
        );

        assert_eq!(finding.severity, Severity::Warn);
        let data = finding_data(&finding);
        assert_eq!(data["action"], "backpressure");
        assert_eq!(data["pressure_dimension"], "active_agents");
        assert!(data["retry_after_ms"].as_u64().unwrap_or(0) > 0);
        assert!(
            finding
                .remediation
                .as_deref()
                .unwrap_or_default()
                .contains("Delay new swarm work")
        );
    }

    #[test]
    fn swarm_admission_reports_deny_guidance() {
        let finding = classify_swarm_admission(
            test_swarm_capacity_plan(),
            healthy_resource_sample(),
            SwarmLiveLoad::empty().with_active_tool_calls(8),
            &[],
        );

        assert_eq!(finding.severity, Severity::Fail);
        let data = finding_data(&finding);
        assert_eq!(data["action"], "deny");
        assert_eq!(data["pressure_dimension"], "active_tool_calls");
        assert!(
            finding
                .remediation
                .as_deref()
                .unwrap_or_default()
                .contains("Do not launch new swarm work")
        );
    }

    #[test]
    fn swarm_admission_marks_stale_input_degraded() {
        let warnings = vec!["host load average unavailable".to_string()];
        let finding = classify_swarm_admission(
            test_swarm_capacity_plan(),
            healthy_resource_sample(),
            SwarmLiveLoad::empty().with_active_agents(1),
            &warnings,
        );

        assert_eq!(finding.severity, Severity::Warn);
        let data = finding_data(&finding);
        assert_eq!(data["action"], "admit");
        assert_eq!(
            data["stale_data_warnings"][0],
            "host load average unavailable"
        );
        assert!(
            finding
                .detail
                .as_deref()
                .unwrap_or_default()
                .contains("stale_data_warnings")
        );
    }

    #[test]
    fn swarm_admission_blocks_corrupted_coordination() {
        let warnings = vec!["Beads ledger has malformed JSONL rows".to_string()];
        let finding = swarm_admission_blocked_finding(
            Severity::Fail,
            "Live swarm admission decision denies new work",
            "Beads ledger has 1 malformed JSONL row; coordination state is corrupted".to_string(),
            &warnings,
        );

        assert_eq!(finding.severity, Severity::Fail);
        let data = finding_data(&finding);
        assert_eq!(data["action"], "deny");
        assert!(data["admission_decision"].is_null());
        assert_eq!(
            data["stale_data_warnings"][0],
            "Beads ledger has malformed JSONL rows"
        );
    }

    #[test]
    fn swarm_git_porcelain_summary_counts_dirty_kinds() {
        let summary =
            summarize_git_porcelain(" M src/doctor.rs\nA  README.md\n?? notes.md\n D stale.rs\n");

        assert_eq!(summary.total, 4);
        assert_eq!(summary.staged, 1);
        assert_eq!(summary.unstaged, 2);
        assert_eq!(summary.untracked, 1);
        assert_eq!(summary.deleted, 1);
    }

    #[test]
    fn swarm_agent_mail_status_warns_on_ack_required() {
        let value = serde_json::json!({
            "inbox": {
                "unread": 2,
                "urgent": 0,
                "ack_required": 1
            }
        });

        let finding = classify_agent_mail_status(&value);

        assert_eq!(finding.severity, Severity::Warn);
        assert!(finding.detail.unwrap().contains("ack_required=1"));
    }

    #[test]
    fn swarm_br_status_warns_on_sync_drift() {
        let value = serde_json::json!({
            "dirty_count": 2,
            "jsonl_newer": false,
            "db_newer": true
        });

        let finding = classify_br_sync_status(&value);

        assert_eq!(finding.severity, Severity::Warn);
        assert!(finding.detail.unwrap().contains("dirty_count=2"));
    }

    #[test]
    fn swarm_br_status_passes_when_clean() {
        let value = serde_json::json!({
            "dirty_count": 0,
            "jsonl_newer": false,
            "db_newer": false
        });

        let finding = classify_br_sync_status(&value);

        assert_eq!(finding.severity, Severity::Pass);
    }

    #[test]
    fn swarm_agent_mail_reservations_detect_conflicts() {
        let value = serde_json::json!({
            "reservations": [
                {"path": "src/doctor.rs", "agent": "CalmBridge"}
            ],
            "conflicts": [
                {"path": "src/doctor.rs", "holder": "OtherAgent"}
            ]
        });

        let finding = classify_agent_mail_reservations(&value);

        assert_eq!(finding.severity, Severity::Warn);
        assert!(finding.title.contains("conflicts"));
    }

    #[test]
    fn swarm_agent_mail_reservations_pass_when_clear() {
        let value = serde_json::json!({
            "reservations": [],
            "expiring": 0,
            "conflicts": []
        });

        let finding = classify_agent_mail_reservations(&value);

        assert_eq!(finding.severity, Severity::Pass);
    }

    #[test]
    fn swarm_redacts_sensitive_probe_output() {
        let outcome = CommandOutcome {
            timed_out: false,
            success: false,
            status_code: Some(1),
            stdout: "status ok\nOPENAI_API_KEY=secret-value\n".to_string(),
            stderr: String::new(),
        };

        let snippet = redacted_output_snippet(&outcome);

        assert!(snippet.contains("status ok"));
        assert!(snippet.contains("[redacted sensitive output line]"));
        assert!(!snippet.contains("secret-value"));
    }

    #[test]
    fn swarm_df_parser_and_formatting_are_stable() {
        let output = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100000 12000 88000 13% /data\n";

        assert_eq!(parse_df_available_kb(output), Some(88_000));
        assert_eq!(format_available_kb(88_000), "85.9 MiB");
        assert_eq!(format_available_kb(11 * 1024 * 1024), "11.0 GiB");
    }

    #[test]
    fn session_healthy_empty_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("empty.jsonl");
        std::fs::write(&path, "").unwrap();
        assert!(!is_session_healthy(&path));
    }

    #[test]
    fn session_healthy_valid_json() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("valid.jsonl");
        std::fs::write(
            &path,
            r#"{"type":"session","version":3,"id":"doctor-jsonl","timestamp":"2026-01-01T00:00:00.000Z","cwd":"/tmp"}"#,
        )
        .unwrap();
        assert!(is_session_healthy(&path));
    }

    #[test]
    fn session_healthy_invalid_json() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("invalid.jsonl");
        std::fs::write(&path, "not json at all\n").unwrap();
        assert!(!is_session_healthy(&path));
    }

    #[test]
    fn session_healthy_rejects_non_header_json() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("array.jsonl");
        std::fs::write(&path, "[1,2,3]\n").unwrap();
        assert!(!is_session_healthy(&path));
    }

    #[cfg(feature = "sqlite-sessions")]
    #[test]
    fn session_healthy_valid_sqlite() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("valid.sqlite");
        let header = SessionHeader {
            id: "doctor-sqlite".to_string(),
            ..SessionHeader::default()
        };
        futures::executor::block_on(async {
            crate::session_sqlite::save_session(&path, &header, &[])
                .await
                .expect("save sqlite session");
        });
        assert!(is_session_healthy(&path));
    }

    #[cfg(feature = "sqlite-sessions")]
    #[test]
    fn session_healthy_rejects_invalid_sqlite_header() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("invalid.sqlite");
        let header = SessionHeader {
            id: "doctor-sqlite".to_string(),
            ..SessionHeader::default()
        };
        futures::executor::block_on(async {
            crate::session_sqlite::save_session(&path, &header, &[])
                .await
                .expect("save sqlite session");
        });
        let invalid_header = SessionHeader {
            r#type: "not-session".to_string(),
            ..header
        };
        let invalid_json =
            serde_json::to_string(&invalid_header).expect("serialize invalid session header");
        let config = sqlmodel_sqlite::SqliteConfig::file(path.to_string_lossy())
            .flags(sqlmodel_sqlite::OpenFlags::create_read_write());
        let conn = sqlmodel_sqlite::SqliteConnection::open(&config).expect("open sqlite db");
        conn.execute_sync(
            "UPDATE pi_session_header SET json = ?1",
            &[sqlmodel_core::Value::Text(invalid_json)],
        )
        .expect("corrupt sqlite header row");
        assert!(!is_session_healthy(&path));
    }

    #[test]
    fn check_dir_creates_missing_with_fix() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("sub/nested");
        let mut findings = Vec::new();
        check_dir(CheckCategory::Dirs, "test", &missing, true, &mut findings);
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].severity, Severity::Pass);
        assert_eq!(findings[0].fixability, Fixability::Fixed);
        assert!(missing.is_dir());
    }

    #[test]
    fn check_dir_warns_missing_without_fix() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("sub/nested");
        let mut findings = Vec::new();
        check_dir(CheckCategory::Dirs, "test", &missing, false, &mut findings);
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].severity, Severity::Warn);
        assert_eq!(findings[0].fixability, Fixability::AutoFixable);
        assert!(!missing.exists());
    }

    #[test]
    fn check_shell_finds_bash() {
        let mut findings = Vec::new();
        check_tool(
            CheckCategory::Shell,
            "bash",
            &["--version"],
            Severity::Fail,
            ToolCheckMode::ProbeExecution,
            &mut findings,
        );
        // bash should be available in CI/dev environments
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].severity, Severity::Pass);
    }

    #[cfg(unix)]
    #[test]
    fn check_tool_falls_back_when_probe_args_are_unsupported() {
        let mut findings = Vec::new();
        check_tool(
            CheckCategory::Shell,
            "sh",
            &["--version"],
            Severity::Fail,
            ToolCheckMode::ProbeExecution,
            &mut findings,
        );
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].severity, Severity::Pass);
    }

    #[cfg(unix)]
    #[test]
    fn check_tool_reports_invocation_failure_for_broken_executable() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("broken_tool.sh");
        // Mark a non-binary, non-script blob as executable so spawn fails with
        // "exec format error" rather than "not found".
        std::fs::write(&script, "not an executable format").unwrap();
        let mut perms = std::fs::metadata(&script).unwrap().permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&script, perms).unwrap();

        let mut findings = Vec::new();
        check_tool(
            CheckCategory::Shell,
            script.to_str().unwrap(),
            &["--version"],
            Severity::Fail,
            ToolCheckMode::ProbeExecution,
            &mut findings,
        );

        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].severity, Severity::Fail);
        assert!(findings[0].title.contains("invocation failed"));
    }

    #[test]
    fn check_settings_file_rejects_non_object_json() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        std::fs::write(&path, "[1,2,3]").unwrap();
        let mut findings = Vec::new();
        check_settings_file(CheckCategory::Config, &path, "Settings", &mut findings);
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].severity, Severity::Fail);
        assert!(
            findings[0]
                .title
                .contains("top-level value must be a JSON object")
        );
    }

    #[test]
    fn fixability_display() {
        // Ensure serialization works
        let json = serde_json::to_string(&Fixability::AutoFixable).unwrap();
        assert!(json.contains("autoFixable") || json.contains("auto"));
    }

    #[test]
    fn run_doctor_path_mode_defaults_to_extension_checks_only() {
        let dir = tempfile::tempdir().unwrap();
        let opts = DoctorOptions {
            cwd: dir.path(),
            extension_path: Some("missing-ext"),
            policy_override: None,
            fix: false,
            only: None,
        };
        let report = run_doctor(&opts).unwrap();
        assert!(
            !report.findings.is_empty(),
            "missing extension path should produce at least one finding"
        );
        assert!(
            report
                .findings
                .iter()
                .all(|f| matches!(f.category, CheckCategory::Extensions)),
            "path mode should not run unrelated environment categories by default"
        );
    }

    #[test]
    fn run_doctor_only_extensions_without_path_reports_error_finding() {
        let mut only = HashSet::new();
        only.insert(CheckCategory::Extensions);
        let dir = tempfile::tempdir().unwrap();
        let opts = DoctorOptions {
            cwd: dir.path(),
            extension_path: None,
            policy_override: None,
            fix: false,
            only: Some(only),
        };
        let report = run_doctor(&opts).unwrap();
        assert!(
            report.findings.iter().any(|f| {
                matches!(f.category, CheckCategory::Extensions)
                    && matches!(f.severity, Severity::Fail)
            }),
            "extensions-only mode without a path should emit a clear failure finding"
        );
    }

    #[test]
    fn run_doctor_extension_path_uses_supplied_cwd_for_policy_resolution() {
        let project = tempfile::tempdir().expect("project dir");
        let config_dir = project.path().join(".pi");
        std::fs::create_dir_all(&config_dir).expect("create project config dir");
        std::fs::write(
            config_dir.join("settings.json"),
            r#"{ "extensionPolicy": { "profile": "safe" } }"#,
        )
        .expect("write project settings");
        write_extension_fixture(
            project.path(),
            r#"
const { exec } = require("child_process");
export default function(pi) {
    pi.exec("ls");
}
"#,
        );

        let opts = DoctorOptions {
            cwd: project.path(),
            extension_path: Some("ext"),
            policy_override: None,
            fix: false,
            only: None,
        };
        let report = run_doctor(&opts).expect("doctor report");

        assert!(
            report.findings.iter().any(|f| f.title.contains("exec")),
            "doctor should honor the supplied cwd's safe policy and flag exec use"
        );
    }

    #[test]
    fn run_doctor_extension_path_reports_config_load_failure_without_aborting() {
        let project = tempfile::tempdir().expect("project dir");
        let config_dir = project.path().join(".pi");
        std::fs::create_dir_all(&config_dir).expect("create project config dir");
        std::fs::write(config_dir.join("settings.json"), r#"{ "extensionPolicy": "#)
            .expect("write malformed project settings");
        write_extension_fixture(
            project.path(),
            r#"
import net from "node:net";
"#,
        );

        let opts = DoctorOptions {
            cwd: project.path(),
            extension_path: Some("ext"),
            policy_override: None,
            fix: false,
            only: None,
        };
        let report = run_doctor(&opts).expect("doctor report");

        assert!(
            report
                .findings
                .iter()
                .all(|f| matches!(f.category, CheckCategory::Extensions)),
            "extension path mode should keep findings scoped to extensions"
        );
        assert!(
            report.findings.iter().any(|f| {
                f.title
                    .as_str()
                    .eq("Failed to load configuration for extension policy resolution")
            }),
            "doctor should surface config load failures as findings instead of returning Err"
        );
        assert!(
            report.findings.iter().any(|f| f.title.contains("node:net")),
            "doctor should continue extension analysis after a config load failure"
        );
    }

    #[test]
    fn run_doctor_extension_path_config_load_failure_falls_back_to_safe_policy() {
        let project = tempfile::tempdir().expect("project dir");
        let config_dir = project.path().join(".pi");
        std::fs::create_dir_all(&config_dir).expect("create project config dir");
        std::fs::write(config_dir.join("settings.json"), r#"{ "extensionPolicy": "#)
            .expect("write malformed project settings");
        write_extension_fixture(
            project.path(),
            r#"
export default function(pi) {
    pi.exec("ls");
}
"#,
        );

        let opts = DoctorOptions {
            cwd: project.path(),
            extension_path: Some("ext"),
            policy_override: None,
            fix: false,
            only: None,
        };
        let report = run_doctor(&opts).expect("doctor report");

        assert!(
            report
                .findings
                .iter()
                .any(|f| f.title.as_str().eq("Extension ext: incompatible")),
            "doctor should fail closed under a safe fallback when config loading fails"
        );
        assert!(
            report.findings.iter().any(|f| f.title.contains("exec")),
            "safe fallback should still flag denied exec usage"
        );
    }

    #[test]
    fn run_doctor_extension_path_config_load_failure_honors_cli_policy_override() {
        let project = tempfile::tempdir().expect("project dir");
        let config_dir = project.path().join(".pi");
        std::fs::create_dir_all(&config_dir).expect("create project config dir");
        std::fs::write(config_dir.join("settings.json"), r#"{ "extensionPolicy": "#)
            .expect("write malformed project settings");
        write_extension_fixture(
            project.path(),
            r#"
export default function(pi) {
    pi.exec("ls");
}
"#,
        );

        let opts = DoctorOptions {
            cwd: project.path(),
            extension_path: Some("ext"),
            policy_override: Some("permissive"),
            fix: false,
            only: None,
        };
        let report = run_doctor(&opts).expect("doctor report");

        assert!(
            report
                .findings
                .iter()
                .any(|f| f.title.as_str().eq("Extension ext: compatible")),
            "explicit CLI overrides should still control fallback analysis"
        );
        assert!(
            !report.findings.iter().any(|f| f.title.contains("exec")),
            "permissive override should suppress safe-only exec denial findings"
        );
    }

    mod proptest_doctor {
        use super::*;
        use proptest::prelude::*;

        const ALL_SEVERITIES: &[Severity] = &[
            Severity::Pass,
            Severity::Info,
            Severity::Warn,
            Severity::Fail,
        ];

        const CATEGORY_ALIASES: &[&str] = &[
            "config",
            "dirs",
            "directories",
            "auth",
            "authentication",
            "shell",
            "sessions",
            "extensions",
            "ext",
        ];

        proptest! {
            /// Severity ordering is total: Pass < Info < Warn < Fail.
            #[test]
            fn severity_ordering_total(a in 0..4usize, b in 0..4usize) {
                let sa = ALL_SEVERITIES[a];
                let sb = ALL_SEVERITIES[b];
                match a.cmp(&b) {
                    std::cmp::Ordering::Less => assert!(sa < sb),
                    std::cmp::Ordering::Equal => assert!(sa.eq(&sb)),
                    std::cmp::Ordering::Greater => assert!(sa > sb),
                }
            }

            /// Severity display produces uppercase 4-char labels.
            #[test]
            fn severity_display_uppercase(idx in 0..4usize) {
                let s = ALL_SEVERITIES[idx];
                let display = s.to_string();
                assert_eq!(display.len(), 4);
                assert!(display.chars().all(|c| c.is_ascii_uppercase()));
            }

            /// `CheckCategory::from_str` accepts all known aliases.
            #[test]
            fn check_category_known_aliases(idx in 0..CATEGORY_ALIASES.len()) {
                let alias = CATEGORY_ALIASES[idx];
                assert!(alias.parse::<CheckCategory>().is_ok());
            }

            /// `CheckCategory::from_str` is case-insensitive.
            #[test]
            fn check_category_case_insensitive(idx in 0..CATEGORY_ALIASES.len()) {
                let alias = CATEGORY_ALIASES[idx];
                let upper = alias.to_uppercase();
                let lower_result = alias.parse::<CheckCategory>();
                let upper_result = upper.parse::<CheckCategory>();
                assert_eq!(lower_result, upper_result);
            }

            /// Unknown category names are rejected.
            #[test]
            fn check_category_unknown_rejected(s in "[a-z]{10,20}") {
                assert!(s.parse::<CheckCategory>().is_err());
            }

            /// `CheckCategory::label` returns non-empty strings.
            #[test]
            fn check_category_label_non_empty(idx in 0..6usize) {
                let cats = [
                    CheckCategory::Config,
                    CheckCategory::Dirs,
                    CheckCategory::Auth,
                    CheckCategory::Shell,
                    CheckCategory::Sessions,
                    CheckCategory::Extensions,
                ];
                let label = cats[idx].label();
                assert!(!label.is_empty());
                // Label starts with uppercase
                assert!(label.starts_with(|c: char| c.is_uppercase()));
            }

            /// `DoctorReport::from_findings` summary counts match input.
            #[test]
            fn from_findings_counts_match(
                pass in 0..5usize,
                info in 0..5usize,
                warn in 0..5usize,
                fail in 0..5usize
            ) {
                let mut findings = Vec::new();
                for _ in 0..pass {
                    findings.push(Finding::pass(CheckCategory::Config, "test"));
                }
                for _ in 0..info {
                    findings.push(Finding::info(CheckCategory::Config, "test"));
                }
                for _ in 0..warn {
                    findings.push(Finding::warn(CheckCategory::Config, "test"));
                }
                for _ in 0..fail {
                    findings.push(Finding::fail(CheckCategory::Config, "test"));
                }

                let report = DoctorReport::from_findings(findings);
                assert_eq!(report.summary.pass, pass);
                assert_eq!(report.summary.info, info);
                assert_eq!(report.summary.warn, warn);
                assert_eq!(report.summary.fail, fail);
            }

            /// `DoctorReport::from_findings` overall severity is max of inputs.
            #[test]
            fn from_findings_overall_severity(
                pass in 0..3usize,
                info in 0..3usize,
                warn in 0..3usize,
                fail in 0..3usize
            ) {
                let mut findings = Vec::new();
                for _ in 0..pass {
                    findings.push(Finding::pass(CheckCategory::Config, "test"));
                }
                for _ in 0..info {
                    findings.push(Finding::info(CheckCategory::Config, "test"));
                }
                for _ in 0..warn {
                    findings.push(Finding::warn(CheckCategory::Config, "test"));
                }
                for _ in 0..fail {
                    findings.push(Finding::fail(CheckCategory::Config, "test"));
                }

                let report = DoctorReport::from_findings(findings);

                if fail > 0 {
                    assert_eq!(report.overall, Severity::Fail);
                } else if warn > 0 {
                    assert_eq!(report.overall, Severity::Warn);
                } else {
                    assert_eq!(report.overall, Severity::Pass);
                }
            }

            /// `is_known_config_key` accepts both camelCase and snake_case forms.
            #[test]
            fn config_key_pairs(idx in 0..10usize) {
                let pairs = [
                    ("hideThinkingBlock", "hide_thinking_block"),
                    ("showHardwareCursor", "show_hardware_cursor"),
                    ("defaultProvider", "default_provider"),
                    ("defaultModel", "default_model"),
                    ("defaultThinkingLevel", "default_thinking_level"),
                    ("enabledModels", "enabled_models"),
                    ("steeringMode", "steering_mode"),
                    ("followUpMode", "follow_up_mode"),
                    ("quietStartup", "quiet_startup"),
                    ("collapseChangelog", "collapse_changelog"),
                ];
                let (camel, snake) = pairs[idx];
                assert!(is_known_config_key(camel), "camelCase key {camel} should be known");
                assert!(is_known_config_key(snake), "snake_case key {snake} should be known");
            }

            /// `is_known_config_key` rejects garbage keys.
            #[test]
            fn config_key_rejects_garbage(s in "[A-Z]{20,30}") {
                assert!(!is_known_config_key(&s));
            }

            /// Severity serde roundtrip is lowercase.
            #[test]
            fn severity_serde_lowercase(idx in 0..4usize) {
                let s = ALL_SEVERITIES[idx];
                let json = serde_json::to_string(&s).unwrap();
                let expected = format!("\"{}\"", s.to_string().to_lowercase());
                assert_eq!(json, expected);
            }

            /// Finding builder chain preserves fields.
            #[test]
            fn finding_builder_chain(title in "[a-z ]{1,20}", detail in "[a-z ]{1,20}") {
                let f = Finding::warn(CheckCategory::Shell, title.clone())
                    .with_detail(detail.clone())
                    .with_remediation("fix it")
                    .auto_fixable();
                assert_eq!(f.title, title);
                assert_eq!(f.detail.as_deref(), Some(detail.as_str()));
                assert_eq!(f.remediation.as_deref(), Some("fix it"));
                assert_eq!(f.fixability, Fixability::AutoFixable);
                assert_eq!(f.severity, Severity::Warn);
            }

            /// `fixed()` resets severity to Pass.
            #[test]
            fn finding_fixed_resets_severity(idx in 0..4usize) {
                let builders = [
                    Finding::pass(CheckCategory::Config, "t"),
                    Finding::info(CheckCategory::Config, "t"),
                    Finding::warn(CheckCategory::Config, "t"),
                    Finding::fail(CheckCategory::Config, "t"),
                ];
                let fixed = builders[idx].clone().fixed();
                assert_eq!(fixed.severity, Severity::Pass);
                assert_eq!(fixed.fixability, Fixability::Fixed);
            }
        }
    }
}
