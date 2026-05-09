use serde::Deserialize;
use serde_json::Value;
use std::os::unix::fs::PermissionsExt as _;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn case_dir(case_name: &str) -> PathBuf {
    PathBuf::from("/tmp")
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
        .expect("stdout should contain admission JSON");
    serde_json::from_str(line).expect("admission JSON should parse")
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

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum PathMode {
    WithoutRch,
    MockRch,
}

#[derive(Debug, Deserialize)]
struct AdmissionFixture {
    name: String,
    path_mode: PathMode,
    mock_rch_stderr: Option<String>,
    args: Vec<String>,
    expected_status: i32,
    expected_decision: String,
    expected_reason: String,
    expected_command_class: String,
    expected_resolved_runner: String,
    expected_rch_detail: String,
    expected_allow_local_fallback: bool,
}

fn admission_fixtures() -> Vec<AdmissionFixture> {
    let path = repo_root().join("tests/fixtures/cargo_headroom_admission/admission_cases.json");
    let content =
        std::fs::read_to_string(&path).expect("admission fixture file should be readable");
    serde_json::from_str(&content).expect("admission fixture file should parse")
}

#[test]
fn fixture_matrix_keeps_rch_admission_decisions_stable() {
    for fixture in admission_fixtures() {
        let mock_dir = case_dir(&format!("fixture-{}", fixture.name)).join("bin");
        let path = match fixture.path_mode {
            PathMode::WithoutRch => "/usr/bin:/bin".to_string(),
            PathMode::MockRch => {
                let stderr = fixture
                    .mock_rch_stderr
                    .as_deref()
                    .expect("mock_rch fixtures must provide stderr");
                write_mock_rch(
                    &mock_dir,
                    &format!(
                        "#!/usr/bin/env bash\nif [[ \"$1\" == \"check\" ]]; then\n    echo \"{}\" >&2\n    exit 1\nfi\nexit 1\n",
                        stderr.replace('\\', "\\\\").replace('"', "\\\"")
                    ),
                );
                format!("{}:/usr/bin:/bin", mock_dir.display())
            }
        };
        let args: Vec<&str> = fixture.args.iter().map(String::as_str).collect();
        let output = run_admission(&format!("fixture-{}", fixture.name), &path, &args);

        assert_eq!(
            output.status.code(),
            Some(fixture.expected_status),
            "{} status mismatch\nstdout:\n{}\nstderr:\n{}",
            fixture.name,
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );

        let decision = decision_from_stdout(&output);
        assert_eq!(decision["schema"], "pi.cargo_headroom.admission.v1");
        assert_eq!(decision["decision"], fixture.expected_decision);
        assert_eq!(decision["reason"], fixture.expected_reason);
        assert_eq!(decision["command_class"], fixture.expected_command_class);
        assert_eq!(
            decision["resolved_runner"],
            fixture.expected_resolved_runner
        );
        assert_eq!(decision["rch_detail"], fixture.expected_rch_detail);
        assert_eq!(
            decision["allow_local_fallback"],
            fixture.expected_allow_local_fallback
        );

        let cargo_target_dir = decision["cargo_target_dir"]
            .as_str()
            .expect("cargo_target_dir must be a string");
        let tmpdir = decision["tmpdir"]
            .as_str()
            .expect("tmpdir must be a string");
        assert!(
            cargo_target_dir.contains(&fixture.name),
            "{} cargo_target_dir should identify fixture run: {cargo_target_dir}",
            fixture.name
        );
        assert!(
            tmpdir.contains(&fixture.name),
            "{} tmpdir should identify fixture run: {tmpdir}",
            fixture.name
        );

        let recommended_target = decision["recommended_cargo_target_dir"]
            .as_str()
            .expect("recommended_cargo_target_dir must be a string");
        let recommended_tmp = decision["recommended_tmpdir"]
            .as_str()
            .expect("recommended_tmpdir must be a string");
        assert!(
            recommended_target.ends_with("/target"),
            "{} recommended target must be concrete: {recommended_target}",
            fixture.name
        );
        assert!(
            recommended_tmp.ends_with("/tmp"),
            "{} recommended tmpdir must be concrete: {recommended_tmp}",
            fixture.name
        );

        let target_remediation = decision["storage_remediation"]["cargo_target_dir"]
            .as_str()
            .expect("cargo target remediation must be a string");
        let tmpdir_remediation = decision["storage_remediation"]["tmpdir"]
            .as_str()
            .expect("tmpdir remediation must be a string");
        assert!(target_remediation.contains("CARGO_TARGET_DIR"));
        assert!(target_remediation.contains("--target-dir"));
        assert!(target_remediation.contains(cargo_target_dir));
        assert!(tmpdir_remediation.contains("TMPDIR"));
        assert!(tmpdir_remediation.contains("--tmpdir"));
        assert!(tmpdir_remediation.contains(tmpdir));
    }
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
