//! Differential test harness for slash command parity between pi-mono and Rust Pi.
//!
//! This module guards slash-command parity coverage until the real pi-mono and
//! Rust Pi RPC runner is wired.

use serde_json::{Value, json};
use std::collections::HashMap;
use tempfile::TempDir;

const DIFFERENTIAL_RUNNER_NOT_IMPLEMENTED: &str =
    "RPC slash-command differential runner is not implemented";

/// A slash command test scenario.
#[derive(Debug, Clone)]
pub struct SlashCommandScenario {
    /// Name of the test case
    pub name: String,
    /// The slash command to execute
    pub command: String,
    /// Expected behavior description
    pub description: String,
    /// Whether this command should work in streaming mode
    pub supports_streaming: bool,
    /// Additional setup needed before running the command
    pub setup: Vec<String>,
}

/// Canonicalizes RPC response JSON by removing non-deterministic fields.
pub fn canonicalize_response(mut response: Value) -> Value {
    // Remove time-sensitive fields
    if let Some(obj) = response.as_object_mut() {
        obj.retain(|key, _| !is_nondeterministic_response_key(key));

        // Canonicalize paths to be relative
        if let Some(path) = obj.get_mut("path") {
            if let Some(path_str) = path.as_str() {
                // Convert absolute paths to relative
                if let Ok(canonical) = std::path::Path::new(path_str).canonicalize() {
                    if let Some(file_name) = canonical.file_name() {
                        *path = json!(file_name);
                    }
                }
            }
        }

        // Recursively canonicalize nested objects
        for value in obj.values_mut() {
            *value = canonicalize_response(value.clone());
        }
    } else if let Some(arr) = response.as_array_mut() {
        for item in arr {
            *item = canonicalize_response(item.clone());
        }
    }

    response
}

fn is_nondeterministic_response_key(key: &str) -> bool {
    let lower = key.to_ascii_lowercase();
    lower.contains("timestamp") || lower == "id" || lower == "duration"
}

/// Test runner for differential slash command testing.
pub struct DifferentialTester {
    temp_dir: TempDir,
    pub scenarios: Vec<SlashCommandScenario>,
}

impl DifferentialTester {
    /// Create a new differential tester.
    pub fn new() -> anyhow::Result<Self> {
        let temp_dir = tempfile::tempdir()?;

        Ok(Self {
            temp_dir,
            scenarios: Self::default_scenarios(),
        })
    }

    /// Default set of slash command scenarios to test.
    #[allow(clippy::too_many_lines)]
    fn default_scenarios() -> Vec<SlashCommandScenario> {
        vec![
            SlashCommandScenario {
                name: "help_basic".to_string(),
                command: "/help".to_string(),
                description: "Basic help command should show command list".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "help_alias_h".to_string(),
                command: "/h".to_string(),
                description: "Help alias /h should work identically to /help".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "help_alias_question".to_string(),
                command: "/?".to_string(),
                description: "Help alias /? should work identically to /help".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "clear_basic".to_string(),
                command: "/clear".to_string(),
                description: "Clear command should reset conversation history".to_string(),
                supports_streaming: false,
                setup: vec!["hello world".to_string()], // Add some history first
            },
            SlashCommandScenario {
                name: "clear_alias_cls".to_string(),
                command: "/cls".to_string(),
                description: "Clear alias /cls should work identically to /clear".to_string(),
                supports_streaming: false,
                setup: vec!["test message".to_string()],
            },
            SlashCommandScenario {
                name: "model_list".to_string(),
                command: "/model".to_string(),
                description: "Model command without args should show model selector".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "model_alias_m".to_string(),
                command: "/m".to_string(),
                description: "Model alias /m should work identically to /model".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "thinking_off".to_string(),
                command: "/thinking off".to_string(),
                description: "Thinking command should set thinking level to off".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "thinking_high".to_string(),
                command: "/thinking high".to_string(),
                description: "Thinking command should set thinking level to high".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "thinking_alias_t".to_string(),
                command: "/t medium".to_string(),
                description: "Thinking alias /t should work identically to /thinking".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "exit_basic".to_string(),
                command: "/exit".to_string(),
                description: "Exit command should terminate session".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "exit_alias_quit".to_string(),
                command: "/quit".to_string(),
                description: "Exit alias /quit should work identically to /exit".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "exit_alias_q".to_string(),
                command: "/q".to_string(),
                description: "Exit alias /q should work identically to /exit".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "session_info".to_string(),
                command: "/session".to_string(),
                description: "Session command should show session information".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "session_alias_info".to_string(),
                command: "/info".to_string(),
                description: "Session alias /info should work identically to /session".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "tree_basic".to_string(),
                command: "/tree".to_string(),
                description: "Tree command should show session tree structure".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "compact_basic".to_string(),
                command: "/compact".to_string(),
                description: "Compact command should trigger conversation compaction".to_string(),
                supports_streaming: false,
                setup: vec!["message 1".to_string(), "message 2".to_string()], // Need some history
            },
            SlashCommandScenario {
                name: "theme_list".to_string(),
                command: "/theme".to_string(),
                description: "Theme command without args should list available themes".to_string(),
                supports_streaming: false,
                setup: vec![],
            },
            SlashCommandScenario {
                name: "history_basic".to_string(),
                command: "/history".to_string(),
                description: "History command should show input history".to_string(),
                supports_streaming: false,
                setup: vec!["test input".to_string()],
            },
            SlashCommandScenario {
                name: "history_alias_hist".to_string(),
                command: "/hist".to_string(),
                description: "History alias /hist should work identically to /history".to_string(),
                supports_streaming: false,
                setup: vec!["another test".to_string()],
            },
        ]
    }

    /// Add a custom scenario to the test suite.
    pub fn add_scenario(&mut self, scenario: SlashCommandScenario) {
        self.scenarios.push(scenario);
    }

    /// Run all scenarios and return results.
    pub fn run_all_scenarios(&self) -> HashMap<String, TestResult> {
        let mut results = HashMap::new();

        for scenario in &self.scenarios {
            println!("Running scenario: {}", scenario.name);
            let result = Self::run_scenario(scenario);
            results.insert(scenario.name.clone(), result);
        }

        results
    }

    /// Run a single scenario.
    pub fn run_scenario(scenario: &SlashCommandScenario) -> TestResult {
        TestResult {
            scenario_name: scenario.name.clone(),
            success: false,
            rust_response: json!({
                "status": "not_run",
                "command": scenario.command,
                "reason": DIFFERENTIAL_RUNNER_NOT_IMPLEMENTED,
            }),
            pi_mono_response: json!({
                "status": "not_run",
                "command": scenario.command,
                "reason": DIFFERENTIAL_RUNNER_NOT_IMPLEMENTED,
            }),
            differences: vec![DIFFERENTIAL_RUNNER_NOT_IMPLEMENTED.to_string()],
        }
    }
}

/// Result of running a differential test scenario.
#[derive(Debug)]
pub struct TestResult {
    pub scenario_name: String,
    pub success: bool,
    pub rust_response: Value,
    pub pi_mono_response: Value,
    pub differences: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_canonicalize_response() {
        let response = json!({
            "status": "success",
            "timestamp": "2024-04-22T10:30:00Z",
            "id": "req-123",
            "path": "/tmp/session-abc",
            "data": {
                "nested_timestamp": "2024-04-22T10:30:01Z",
                "value": 42
            }
        });

        let canonicalized = canonicalize_response(response);

        // Timestamps and IDs should be removed
        assert!(canonicalized.get("timestamp").is_none());
        assert!(canonicalized.get("id").is_none());
        assert!(canonicalized["data"].get("nested_timestamp").is_none());

        // Other fields should be preserved
        assert_eq!(canonicalized["status"], "success");
        assert_eq!(canonicalized["data"]["value"], 42);
    }

    #[test]
    fn test_scenario_creation() {
        let tester = DifferentialTester::new().unwrap();
        assert!(tester.temp_dir.path().is_dir());
        assert!(!tester.scenarios.is_empty());

        // Verify we have basic commands covered
        let scenario_names: Vec<&String> = tester.scenarios.iter().map(|s| &s.name).collect();
        assert!(scenario_names.iter().any(|name| name.contains("help")));
        assert!(scenario_names.iter().any(|name| name.contains("clear")));
        assert!(scenario_names.iter().any(|name| name.contains("model")));
        assert!(scenario_names.iter().any(|name| name.contains("exit")));
        assert!(
            tester
                .scenarios
                .iter()
                .all(|scenario| !scenario.description.is_empty())
        );
        assert!(
            tester
                .scenarios
                .iter()
                .all(|scenario| scenario.setup.iter().all(|entry| !entry.is_empty()))
        );
        assert!(
            tester
                .scenarios
                .iter()
                .all(|scenario| !scenario.supports_streaming)
        );
    }

    #[test]
    fn test_scenario_run_fails_closed_until_runner_is_wired() {
        let tester = DifferentialTester::new().unwrap();

        if let Some(scenario) = tester.scenarios.first() {
            let result = DifferentialTester::run_scenario(scenario);
            assert_eq!(result.scenario_name, scenario.name);
            assert!(!result.success);
            assert_eq!(result.rust_response["status"], "not_run");
            assert_eq!(result.pi_mono_response["status"], "not_run");
            assert_eq!(result.rust_response["command"], scenario.command);
            assert_eq!(result.pi_mono_response["command"], scenario.command);
            assert!(
                result
                    .differences
                    .iter()
                    .any(|diff| diff.contains(DIFFERENTIAL_RUNNER_NOT_IMPLEMENTED)),
                "runner should fail closed with a clear implementation gap"
            );
        }
    }
}
