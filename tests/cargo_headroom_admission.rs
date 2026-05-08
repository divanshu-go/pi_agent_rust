use serde_json::Value;
use std::os::unix::fs::PermissionsExt as _;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn case_dir(case_name: &str) -> PathBuf {
    std::env::temp_dir()
        .join("pi_agent_rust_cargo_headroom_admission")
        .join(format!("{}-{}", case_name, std::process::id()))
}

fn run_admission(case_name: &str, path: &str, args: &[&str]) -> Output {
    let root = repo_root();
    let dir = case_dir(case_name);
    let target_dir = dir.join("target");
    let tmpdir = dir.join("tmp");

    let mut command_args = vec![
        "--admit-only",
        "--min-free-mb",
        "1",
        "--target-dir",
        target_dir
            .to_str()
            .expect("test target dir should be valid UTF-8"),
        "--tmpdir",
        tmpdir.to_str().expect("test tmpdir should be valid UTF-8"),
    ];
    command_args.extend_from_slice(args);

    Command::new(root.join("scripts/cargo_headroom.sh"))
        .env("PATH", path)
        .args(command_args)
        .output()
        .expect("cargo_headroom.sh should execute")
}

fn decision_from_stdout(output: &Output) -> Value {
    let stdout = String::from_utf8_lossy(&output.stdout);
    let line = stdout
        .lines()
        .rev()
        .find(|line| line.starts_with('{'))
        .unwrap_or_else(|| panic!("missing admission JSON in stdout:\n{stdout}"));
    serde_json::from_str(line).unwrap_or_else(|err| {
        panic!("admission JSON should parse: {err}\nline: {line}\nstdout:\n{stdout}")
    })
}

fn write_mock_rch(dir: &Path, body: &str) {
    std::fs::create_dir_all(dir).expect("mock rch dir should be created");
    let path = dir.join("rch");
    std::fs::write(&path, body).expect("mock rch script should be written");
    let mut permissions = std::fs::metadata(&path)
        .expect("mock rch metadata should be readable")
        .permissions();
    permissions.set_mode(0o755);
    std::fs::set_permissions(path, permissions).expect("mock rch script should be executable");
}

#[test]
fn auto_runner_backs_off_heavy_command_when_rch_is_missing() {
    let output = run_admission(
        "missing-rch-heavy",
        "/usr/bin:/bin",
        &[
            "--runner",
            "auto",
            "clippy",
            "--all-targets",
            "--",
            "-D",
            "warnings",
        ],
    );

    assert_eq!(output.status.code(), Some(2));
    let decision = decision_from_stdout(&output);
    assert_eq!(decision["decision"], "backoff");
    assert_eq!(decision["reason"], "rch_unavailable");
    assert_eq!(decision["command_class"], "heavy");
    assert_eq!(decision["resolved_runner"], "none");
}

#[test]
fn auto_runner_allows_safe_local_command_when_rch_is_missing() {
    let output = run_admission(
        "missing-rch-safe",
        "/usr/bin:/bin",
        &["--runner", "auto", "fmt", "--check"],
    );

    assert!(output.status.success());
    let decision = decision_from_stdout(&output);
    assert_eq!(decision["decision"], "degraded");
    assert_eq!(decision["reason"], "safe_local_command");
    assert_eq!(decision["command_class"], "safe_local");
    assert_eq!(decision["resolved_runner"], "local");
}

#[test]
fn auto_runner_requires_explicit_local_fallback_for_heavy_command() {
    let output = run_admission(
        "explicit-local-fallback",
        "/usr/bin:/bin",
        &[
            "--runner",
            "auto",
            "--allow-local-fallback",
            "test",
            "--all-targets",
        ],
    );

    assert!(output.status.success());
    let decision = decision_from_stdout(&output);
    assert_eq!(decision["decision"], "degraded");
    assert_eq!(decision["reason"], "explicit_local_fallback");
    assert_eq!(decision["allow_local_fallback"], true);
    assert_eq!(decision["resolved_runner"], "local");
}

#[test]
fn auto_runner_reports_saturated_rch_check_detail() {
    let mock_dir = case_dir("saturated-rch").join("bin");
    write_mock_rch(
        &mock_dir,
        r#"#!/usr/bin/env bash
if [[ "$1" == "check" ]]; then
    echo "queue saturated" >&2
    exit 1
fi
exit 1
"#,
    );
    let path = format!("{}:/usr/bin:/bin", mock_dir.display());
    let output = run_admission(
        "saturated-rch",
        &path,
        &["--runner", "auto", "test", "--all-targets"],
    );

    assert_eq!(output.status.code(), Some(2));
    let decision = decision_from_stdout(&output);
    assert_eq!(decision["decision"], "backoff");
    assert_eq!(decision["reason"], "rch_unavailable");
    assert_eq!(decision["rch_detail"], "queue saturated");
}

#[test]
fn insufficient_target_headroom_emits_backoff_decision() {
    let dir = case_dir("insufficient-headroom");
    let target_dir = dir.join("target");
    let tmpdir = dir.join("tmp");
    let output = Command::new(repo_root().join("scripts/cargo_headroom.sh"))
        .env("PATH", "/usr/bin:/bin")
        .args([
            "--admit-only",
            "--runner",
            "local",
            "--min-free-mb",
            "999999999",
            "--target-dir",
            target_dir
                .to_str()
                .expect("test target dir should be valid UTF-8"),
            "--tmpdir",
            tmpdir.to_str().expect("test tmpdir should be valid UTF-8"),
            "fmt",
            "--check",
        ])
        .output()
        .expect("cargo_headroom.sh should execute");

    assert_eq!(output.status.code(), Some(2));
    let decision = decision_from_stdout(&output);
    assert_eq!(decision["decision"], "backoff");
    assert_eq!(decision["reason"], "insufficient_headroom");
    assert_eq!(decision["command_class"], "blocked");
}
