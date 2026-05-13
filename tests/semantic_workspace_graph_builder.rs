#![forbid(unsafe_code)]
#![allow(clippy::too_many_lines)]

use chrono::{DateTime, Utc};
use pi::semantic_workspace_graph::{
    BeadActionabilityStatus, EvidenceFreshnessStatus, GraphInputStatus, SemanticNodeType,
    SemanticWorkspaceGraph, SemanticWorkspaceGraphBuilder,
};
use serde_json::json;
use std::error::Error;
use std::fs;
use std::path::Path;
use tempfile::TempDir;

type TestResult<T = ()> = Result<T, Box<dyn Error>>;

fn reference_time() -> TestResult<DateTime<Utc>> {
    Ok(DateTime::parse_from_rfc3339("2026-05-13T00:00:00Z")?.with_timezone(&Utc))
}

fn write_fixture(root: &Path, relative_path: &str, content: &str) -> TestResult {
    let path = root.join(relative_path);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, content)?;
    Ok(())
}

fn fixture_workspace() -> TestResult<TempDir> {
    let temp = tempfile::tempdir()?;
    let root = temp.path();

    write_fixture(
        root,
        "src/lib.rs",
        r"
pub mod providers;

pub struct Widget;

pub fn build_widget() -> Widget {
    Widget
}
",
    )?;
    write_fixture(
        root,
        "src/providers/openai.rs",
        r"
pub struct OpenAiProvider;

pub fn stream_response() {}
",
    )?;
    write_fixture(
        root,
        "tests/widget_flow.rs",
        r"
#[test]
fn builds_widget() {
    assert_eq!(2 + 2, 4);
}
",
    )?;
    write_fixture(
        root,
        "README.md",
        r"
# Pi Fixture

## Evidence

See docs/evidence/dropin-certification-verdict.json.
",
    )?;
    write_fixture(
        root,
        "docs/evidence/dropin-certification-verdict.json",
        r#"{
  "schema": "pi.dropin_certification.verdict.v1",
  "generated_at": "2026-01-01T00:00:00Z",
  "overall_verdict": "CERTIFIED",
  "claim_surface": "release_facing"
}"#,
    )?;
    write_fixture(
        root,
        "docs/evidence/uncertified.json",
        r#"{
  "schema": "pi.dropin_certification.verdict.v1",
  "generated_at": "2026-05-13T00:00:00Z",
  "overall_verdict": "NOT_CERTIFIED",
  "claim_surface": "release_facing"
}"#,
    )?;
    write_fixture(root, "docs/evidence/malformed.json", "{ not valid json")?;

    let issues = [
        json!({
            "id": "bd-open",
            "title": "Open work",
            "status": "open",
            "priority": 1,
            "issue_type": "feature",
            "updated_at": "2026-05-13T00:00:00Z"
        })
        .to_string(),
        json!({
            "id": "bd-blocked",
            "title": "Blocked work",
            "status": "open",
            "priority": 1,
            "issue_type": "feature",
            "updated_at": "2026-05-13T00:00:00Z",
            "dependencies": [
                {
                    "issue_id": "bd-blocked",
                    "depends_on_id": "bd-open",
                    "type": "blocks"
                }
            ]
        })
        .to_string(),
        json!({
            "id": "bd-claimed",
            "title": "Claimed work",
            "status": "in_progress",
            "priority": 1,
            "issue_type": "task",
            "updated_at": "2026-05-13T00:00:00Z"
        })
        .to_string(),
        json!({
            "id": "bd-closed",
            "title": "Closed work",
            "status": "closed",
            "priority": 2,
            "issue_type": "task",
            "closed_at": "2026-05-01T00:00:00Z"
        })
        .to_string(),
        json!({
            "id": "bd-tombstone",
            "title": "Deleted work",
            "status": "tombstone",
            "deleted": true
        })
        .to_string(),
        "{ not valid bead json".to_string(),
    ]
    .join("\n");
    write_fixture(root, ".beads/issues.jsonl", &issues)?;

    Ok(temp)
}

fn build_fixture_graph(root: &Path) -> TestResult<SemanticWorkspaceGraph> {
    Ok(SemanticWorkspaceGraphBuilder::new(root)
        .with_reference_time(reference_time()?)
        .add_expected_path("docs/evidence/missing.json")
        .build()?)
}

fn node_with_source<'a>(
    graph: &'a SemanticWorkspaceGraph,
    node_type: SemanticNodeType,
    source_path: &str,
) -> TestResult<&'a pi::semantic_workspace_graph::SemanticGraphNode> {
    graph
        .nodes
        .iter()
        .find(|node| node.node_type == node_type && node.source_path == source_path)
        .ok_or_else(|| format!("missing {node_type:?} node for {source_path}").into())
}

fn bead_status(
    graph: &SemanticWorkspaceGraph,
    bead_id: &str,
) -> TestResult<BeadActionabilityStatus> {
    let node = graph
        .nodes
        .iter()
        .find(|node| {
            node.node_type == SemanticNodeType::Bead
                && node.metadata.get("bead_id") == Some(&json!(bead_id))
        })
        .ok_or_else(|| format!("missing bead node for {bead_id}"))?;
    node.bead_actionability_status
        .ok_or_else(|| format!("missing bead actionability for {bead_id}").into())
}

#[test]
fn builder_indexes_workspace_surfaces_and_classifies_fail_closed() -> TestResult {
    let temp = fixture_workspace()?;
    let graph = build_fixture_graph(temp.path())?;
    let graph_again = build_fixture_graph(temp.path())?;

    assert_eq!(
        serde_json::to_value(&graph)?,
        serde_json::to_value(&graph_again)?
    );

    for node_type in [
        SemanticNodeType::CodeSymbol,
        SemanticNodeType::FileRegion,
        SemanticNodeType::TestCase,
        SemanticNodeType::DocSection,
        SemanticNodeType::EvidenceArtifact,
        SemanticNodeType::Bead,
        SemanticNodeType::ProviderSurface,
        SemanticNodeType::ValidationCommand,
    ] {
        assert!(
            !graph.nodes_by_type(node_type).is_empty(),
            "expected at least one {node_type:?} node"
        );
    }

    let stale = node_with_source(
        &graph,
        SemanticNodeType::EvidenceArtifact,
        "docs/evidence/dropin-certification-verdict.json",
    )?;
    assert_eq!(stale.freshness_status, Some(EvidenceFreshnessStatus::Stale));
    assert_eq!(
        stale.metadata.get("release_claim_allowed"),
        Some(&json!(false))
    );

    let uncertified = node_with_source(
        &graph,
        SemanticNodeType::EvidenceArtifact,
        "docs/evidence/uncertified.json",
    )?;
    assert_eq!(
        uncertified.freshness_status,
        Some(EvidenceFreshnessStatus::Uncertified)
    );

    let malformed = node_with_source(
        &graph,
        SemanticNodeType::EvidenceArtifact,
        "docs/evidence/malformed.json",
    )?;
    assert_eq!(
        malformed.freshness_status,
        Some(EvidenceFreshnessStatus::Malformed)
    );

    let missing = node_with_source(
        &graph,
        SemanticNodeType::EvidenceArtifact,
        "docs/evidence/missing.json",
    )?;
    assert_eq!(
        missing.freshness_status,
        Some(EvidenceFreshnessStatus::Missing)
    );

    assert_eq!(
        bead_status(&graph, "bd-open")?,
        BeadActionabilityStatus::ActionableOpen
    );
    assert_eq!(
        bead_status(&graph, "bd-blocked")?,
        BeadActionabilityStatus::Blocked
    );
    assert_eq!(
        bead_status(&graph, "bd-claimed")?,
        BeadActionabilityStatus::ClaimedInProgress
    );
    assert_eq!(
        bead_status(&graph, "bd-closed")?,
        BeadActionabilityStatus::ClosedReferenceOnly
    );
    assert_eq!(
        bead_status(&graph, "bd-tombstone")?,
        BeadActionabilityStatus::TombstoneReferenceOnly
    );
    assert_eq!(
        bead_status(&graph, "malformed-line-6")?,
        BeadActionabilityStatus::UnknownFailClosed
    );

    assert!(graph.trace.iter().any(|event| {
        event.status == GraphInputStatus::Missing
            && event.source_path == "docs/evidence/missing.json"
    }));
    assert!(graph.trace.iter().any(|event| {
        event.status == GraphInputStatus::Malformed
            && event.source_path == "docs/evidence/malformed.json"
    }));
    assert!(graph.trace.iter().any(|event| {
        event.status == GraphInputStatus::Malformed && event.source_path == ".beads/issues.jsonl"
    }));

    let command_nodes = graph.nodes_by_type(SemanticNodeType::ValidationCommand);
    assert!(command_nodes.iter().any(|node| {
        node.metadata.get("command") == Some(&json!("cargo test --test widget_flow builds_widget"))
    }));

    Ok(())
}

#[test]
fn content_hashes_invalidate_without_changing_path_stable_ids() -> TestResult {
    let temp = fixture_workspace()?;
    let before = build_fixture_graph(temp.path())?;
    let before_fingerprint = before
        .input_fingerprints
        .iter()
        .find(|fingerprint| fingerprint.source_path == "src/lib.rs")
        .ok_or("missing src/lib.rs fingerprint before edit")?;
    let before_file_node = node_with_source(&before, SemanticNodeType::FileRegion, "src/lib.rs")?;

    write_fixture(
        temp.path(),
        "src/lib.rs",
        r"
pub mod providers;

pub struct Widget;

pub fn build_widget() -> Widget {
    Widget
}

pub fn build_second_widget() -> Widget {
    Widget
}
",
    )?;

    let after = build_fixture_graph(temp.path())?;
    let after_fingerprint = after
        .input_fingerprints
        .iter()
        .find(|fingerprint| fingerprint.source_path == "src/lib.rs")
        .ok_or("missing src/lib.rs fingerprint after edit")?;
    let after_file_node = node_with_source(&after, SemanticNodeType::FileRegion, "src/lib.rs")?;

    assert_ne!(before_fingerprint.sha256, after_fingerprint.sha256);
    assert_eq!(before_file_node.id, after_file_node.id);
    assert!(after.nodes.iter().any(|node| {
        node.node_type == SemanticNodeType::CodeSymbol && node.title == "build_second_widget"
    }));

    Ok(())
}

#[test]
fn malformed_fixture_classifications_do_not_emit_raw_secret_words() -> TestResult {
    let temp = tempfile::tempdir()?;
    write_fixture(
        temp.path(),
        "docs/evidence/bad.json",
        "{ token: secret authorization",
    )?;

    let graph = SemanticWorkspaceGraphBuilder::new(temp.path()).build()?;
    let encoded = serde_json::to_value(&graph)?;
    let text = serde_json::to_string(&encoded)?;

    assert!(!text.contains("authorization"));
    assert!(!text.contains("token"));
    assert!(!text.contains("secret"));

    let bad = node_with_source(
        &graph,
        SemanticNodeType::EvidenceArtifact,
        "docs/evidence/bad.json",
    )?;
    assert_eq!(
        bad.freshness_status,
        Some(EvidenceFreshnessStatus::Malformed)
    );

    Ok(())
}
