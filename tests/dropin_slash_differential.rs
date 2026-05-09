//! Integration tests for slash command differential parity.
//!
//! This test suite tracks slash-command differential coverage and fails closed
//! until the real pi-mono/Rust Pi RPC runner is wired.

#[path = "dropin_slash_differential/mod.rs"]
mod dropin_slash_differential;
use dropin_slash_differential::*;

fn assert_runner_not_implemented(scenario: &SlashCommandScenario, result: &TestResult) {
    assert!(
        !result.success,
        "scenario '{}' must not report synthetic differential success",
        scenario.name
    );
    assert_eq!(result.scenario_name, scenario.name);
    assert_eq!(result.rust_response["status"], "not_run");
    assert_eq!(result.pi_mono_response["status"], "not_run");
    assert_eq!(result.rust_response["command"], scenario.command);
    assert_eq!(result.pi_mono_response["command"], scenario.command);
    assert!(
        result
            .differences
            .iter()
            .any(|diff| diff.contains("not implemented")),
        "scenario '{}' should explain that the real runner is not implemented",
        scenario.name
    );
}

/// The harness must not report slash-command parity until real RPC execution exists.
#[test]
fn test_slash_command_differential_harness_fails_closed() {
    let tester = DifferentialTester::new().expect("Failed to create differential tester");

    let results = tester.run_all_scenarios();
    assert!(!results.is_empty(), "expected slash command scenarios");

    let mut unexpected_successes = Vec::new();
    for (scenario_name, result) in results {
        if result.success {
            unexpected_successes.push(scenario_name);
            continue;
        }
        assert!(
            result
                .differences
                .iter()
                .any(|diff| diff.contains("not implemented")),
            "scenario '{scenario_name}' should fail closed with an implementation gap"
        );
    }

    assert!(
        unexpected_successes.is_empty(),
        "slash differential harness reported synthetic success for: {unexpected_successes:?}"
    );
}

/// Test that basic slash command parsing works correctly.
#[test]
fn test_slash_command_parsing() {
    // Verify that our test scenarios cover the actual slash commands
    // supported by the Rust implementation
    let tester = DifferentialTester::new().expect("Failed to create tester");

    // Check that we have test scenarios for core commands
    let scenario_commands: Vec<String> =
        tester.scenarios.iter().map(|s| s.command.clone()).collect();

    // Verify coverage of essential commands
    let essential_commands = vec![
        "/help",
        "/h",
        "/?",
        "/clear",
        "/cls",
        "/model",
        "/m",
        "/thinking",
        "/t",
        "/exit",
        "/quit",
        "/q",
        "/session",
        "/info",
        "/tree",
        "/compact",
    ];

    for essential in essential_commands {
        assert!(
            scenario_commands
                .iter()
                .any(|cmd| cmd.starts_with(essential)),
            "Missing test scenario for essential command: {essential}"
        );
    }
}

/// Test response canonicalization functionality.
#[test]
fn test_response_canonicalization() {
    use serde_json::json;

    let test_response = json!({
        "status": "success",
        "timestamp": "2024-04-22T17:49:00Z",
        "id": "req-test-123",
        "duration": 150,
        "path": "/tmp/test-session",
        "data": {
            "message": "Command executed",
            "nested_timestamp": "2024-04-22T17:49:01Z",
            "tokens": 42
        }
    });

    let canonicalized = canonicalize_response(test_response);

    // Non-deterministic fields should be removed
    assert!(canonicalized.get("timestamp").is_none());
    assert!(canonicalized.get("id").is_none());
    assert!(canonicalized.get("duration").is_none());
    assert!(canonicalized["data"].get("nested_timestamp").is_none());

    // Deterministic fields should be preserved
    assert_eq!(canonicalized["status"], "success");
    assert_eq!(canonicalized["data"]["message"], "Command executed");
    assert_eq!(canonicalized["data"]["tokens"], 42);
}

/// Test combinatorial slash command scenarios.
#[test]
fn test_combinatorial_slash_commands() {
    let mut tester = DifferentialTester::new().expect("Failed to create tester");

    // Add combinatorial test scenarios
    tester.add_scenario(SlashCommandScenario {
        name: "model_then_thinking".to_string(),
        command: "/thinking high".to_string(),
        description: "Set thinking level after potential model change".to_string(),
        supports_streaming: false,
        setup: vec!["/model".to_string()], // First show model selector
    });

    tester.add_scenario(SlashCommandScenario {
        name: "clear_then_help".to_string(),
        command: "/help".to_string(),
        description: "Help command should work after clearing history".to_string(),
        supports_streaming: false,
        setup: vec!["some conversation".to_string(), "/clear".to_string()],
    });

    tester.add_scenario(SlashCommandScenario {
        name: "multiple_thinking_changes".to_string(),
        command: "/thinking off".to_string(),
        description: "Multiple thinking level changes should work".to_string(),
        supports_streaming: false,
        setup: vec!["/thinking high".to_string(), "/thinking medium".to_string()],
    });

    // Run just the combinatorial scenarios
    let combinatorial_scenarios: Vec<_> = tester
        .scenarios
        .iter()
        .filter(|s| {
            s.name.contains("model_then_")
                || s.name.contains("clear_then_")
                || s.name.contains("multiple_")
        })
        .cloned()
        .collect();

    for scenario in combinatorial_scenarios {
        let result = DifferentialTester::run_scenario(&scenario);
        assert_runner_not_implemented(&scenario, &result);
    }
}

/// Test error handling for invalid slash commands.
#[test]
fn test_invalid_slash_command_handling() {
    let mut tester = DifferentialTester::new().expect("Failed to create tester");

    // Add invalid command scenarios
    let invalid_scenarios = vec![
        SlashCommandScenario {
            name: "invalid_command".to_string(),
            command: "/nonexistent".to_string(),
            description: "Invalid slash command should be handled gracefully".to_string(),
            supports_streaming: false,
            setup: vec![],
        },
        SlashCommandScenario {
            name: "malformed_thinking".to_string(),
            command: "/thinking invalid_level".to_string(),
            description: "Invalid thinking level should show error".to_string(),
            supports_streaming: false,
            setup: vec![],
        },
        SlashCommandScenario {
            name: "empty_slash".to_string(),
            command: "/".to_string(),
            description: "Empty slash command should be handled".to_string(),
            supports_streaming: false,
            setup: vec![],
        },
    ];

    for scenario in invalid_scenarios {
        tester.add_scenario(scenario.clone());
        let result = DifferentialTester::run_scenario(&scenario);
        assert_runner_not_implemented(&scenario, &result);
    }
}
