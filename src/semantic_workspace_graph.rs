//! Deterministic semantic workspace graph builder.
//!
//! The graph is advisory context only. It indexes workspace facts with
//! freshness and actionability metadata, but it never replaces Beads, Agent
//! Mail, README evidence gates, or validation commands as sources of truth.

#![allow(clippy::missing_const_for_fn, clippy::too_many_lines)]

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error as StdError;
use std::fmt;
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::time::UNIX_EPOCH;

pub const SEMANTIC_WORKSPACE_GRAPH_SCHEMA: &str = "pi.semantic_workspace_graph.v1";
pub const GRAPH_BUILDER_SCHEMA: &str = "pi.semantic_workspace_graph.builder_trace.v1";

const DEFAULT_STALE_AFTER_DAYS: i64 = 90;

#[derive(Debug, Clone)]
pub struct SemanticWorkspaceGraphBuilder {
    root: PathBuf,
    options: SemanticWorkspaceGraphBuildOptions,
}

#[derive(Debug, Clone)]
pub struct SemanticWorkspaceGraphBuildOptions {
    pub root_inputs: Vec<PathBuf>,
    pub reference_time_utc: Option<DateTime<Utc>>,
    pub stale_after_days: i64,
}

impl Default for SemanticWorkspaceGraphBuildOptions {
    fn default() -> Self {
        Self {
            root_inputs: vec![
                PathBuf::from("src"),
                PathBuf::from("tests"),
                PathBuf::from("README.md"),
                PathBuf::from("docs"),
                PathBuf::from(".beads/issues.jsonl"),
            ],
            reference_time_utc: None,
            stale_after_days: DEFAULT_STALE_AFTER_DAYS,
        }
    }
}

impl SemanticWorkspaceGraphBuilder {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self {
            root: root.into(),
            options: SemanticWorkspaceGraphBuildOptions::default(),
        }
    }

    pub fn with_options(
        root: impl Into<PathBuf>,
        options: SemanticWorkspaceGraphBuildOptions,
    ) -> Self {
        Self {
            root: root.into(),
            options,
        }
    }

    #[must_use]
    pub fn add_expected_path(mut self, path: impl Into<PathBuf>) -> Self {
        self.options.root_inputs.push(path.into());
        self
    }

    #[must_use]
    pub fn with_reference_time(mut self, reference_time_utc: DateTime<Utc>) -> Self {
        self.options.reference_time_utc = Some(reference_time_utc);
        self
    }

    pub fn build(&self) -> Result<SemanticWorkspaceGraph, SemanticGraphBuildError> {
        let metadata =
            fs::metadata(&self.root).map_err(|source| SemanticGraphBuildError::RootUnreadable {
                root: self.root.display().to_string(),
                source,
            })?;
        if !metadata.is_dir() {
            return Err(SemanticGraphBuildError::RootNotDirectory {
                root: self.root.display().to_string(),
            });
        }

        let mut state = GraphBuildState::default();
        for input in self.discover_inputs(&mut state) {
            self.ingest_file(&input, &mut state);
        }
        state.sort();

        Ok(SemanticWorkspaceGraph {
            schema: SEMANTIC_WORKSPACE_GRAPH_SCHEMA.to_string(),
            builder_schema: GRAPH_BUILDER_SCHEMA.to_string(),
            root: normalize_path(&self.root),
            nodes: state.nodes,
            edges: state.edges,
            input_fingerprints: state.input_fingerprints,
            trace: state.trace,
        })
    }

    fn discover_inputs(&self, state: &mut GraphBuildState) -> Vec<DiscoveredInput> {
        let mut seen = BTreeSet::new();
        let mut inputs = Vec::new();
        for configured in &self.options.root_inputs {
            let absolute = self.root.join(configured);
            if !absolute.exists() {
                let source_path = normalize_relative_path(&self.root, &absolute);
                Self::record_missing_input(state, &source_path);
                continue;
            }
            self.collect_path(&absolute, &mut seen, &mut inputs, state);
        }
        inputs.sort_by(|left, right| left.source_path.cmp(&right.source_path));
        inputs
    }

    fn collect_path(
        &self,
        absolute: &Path,
        seen: &mut BTreeSet<String>,
        inputs: &mut Vec<DiscoveredInput>,
        state: &mut GraphBuildState,
    ) {
        let source_path = normalize_relative_path(&self.root, absolute);
        if absolute.is_dir() {
            let Ok(entries) = fs::read_dir(absolute) else {
                state.push_trace(GraphBuildTraceEvent::new(
                    SourceSurface::Unknown.as_str(),
                    source_path,
                    GraphInputStatus::Unreadable,
                    "directory_read_failed",
                    0,
                    0,
                ));
                return;
            };

            let mut child_paths = Vec::new();
            for entry in entries.flatten() {
                child_paths.push(entry.path());
            }
            child_paths.sort_by_key(|left| normalize_path(left));
            for child in child_paths {
                if should_skip_dir(&child) {
                    continue;
                }
                self.collect_path(&child, seen, inputs, state);
            }
            return;
        }

        let Some(surface) = surface_for_path(&source_path) else {
            return;
        };
        if seen.insert(source_path.clone()) {
            inputs.push(DiscoveredInput {
                absolute_path: absolute.to_path_buf(),
                source_path,
                surface,
            });
        }
    }

    fn ingest_file(&self, input: &DiscoveredInput, state: &mut GraphBuildState) {
        let start_nodes = state.nodes.len();
        let start_edges = state.edges.len();
        let Ok(bytes) = fs::read(&input.absolute_path) else {
            state.push_trace(GraphBuildTraceEvent::new(
                input.surface.as_str(),
                input.source_path.clone(),
                GraphInputStatus::Unreadable,
                "file_read_failed",
                0,
                0,
            ));
            if input.surface == SourceSurface::EvidenceArtifacts {
                state.push_node(missing_or_unreadable_evidence_node(
                    &input.source_path,
                    EvidenceFreshnessStatus::Missing,
                    "file_read_failed",
                ));
            }
            return;
        };

        let content_sha256 = sha256_hex(&bytes);
        let size_bytes = u64::try_from(bytes.len()).unwrap_or(u64::MAX);
        let mtime_unix_ns = file_mtime_unix_ns(&input.absolute_path).unwrap_or(None);
        state.input_fingerprints.push(InputFingerprint {
            source_path: input.source_path.clone(),
            surface_id: input.surface.as_str().to_string(),
            sha256: content_sha256.clone(),
            size_bytes,
            mtime_unix_ns,
        });

        let content = String::from_utf8_lossy(&bytes);
        match input.surface {
            SourceSurface::RustCodeModules | SourceSurface::IntegrationAndContractTests => {
                Self::ingest_rust_file(input, &content, &content_sha256, size_bytes, state);
            }
            SourceSurface::ReadmeAndDocs => {
                Self::ingest_markdown_file(input, &content, &content_sha256, size_bytes, state);
            }
            SourceSurface::EvidenceArtifacts => {
                self.ingest_evidence_file(input, &content, &content_sha256, size_bytes, state);
            }
            SourceSurface::BeadsIssueGraph => {
                self.ingest_beads_jsonl(input, &content, &content_sha256, size_bytes, state);
            }
            SourceSurface::Unknown => {}
        }

        state.push_trace(GraphBuildTraceEvent::new(
            input.surface.as_str(),
            input.source_path.clone(),
            GraphInputStatus::Indexed,
            "indexed",
            state.nodes.len().saturating_sub(start_nodes),
            state.edges.len().saturating_sub(start_edges),
        ));
    }

    fn ingest_rust_file(
        input: &DiscoveredInput,
        content: &str,
        content_sha256: &str,
        size_bytes: u64,
        state: &mut GraphBuildState,
    ) {
        let line_count = count_lines(content);
        let file_node = file_region_node(
            &input.source_path,
            content_sha256,
            size_bytes,
            1,
            line_count,
            input.surface.as_str(),
        );
        let file_node_id = file_node.id.clone();
        state.push_node(file_node);

        if is_provider_surface(&input.source_path) {
            let provider_node = provider_surface_node(&input.source_path, content_sha256);
            state.push_edge(edge(
                SemanticEdgeType::Contains,
                &file_node_id,
                &provider_node.id,
                "provider_module_surface",
            ));
            state.push_node(provider_node);
        }

        let mut pending_test_attribute = false;
        for (idx, line) in content.lines().enumerate() {
            let line_number = idx.saturating_add(1);
            let trimmed = line.trim_start();
            if is_test_attribute(trimmed) {
                pending_test_attribute = true;
                continue;
            }

            if let Some(symbol) = parse_rust_symbol(trimmed) {
                if input.surface == SourceSurface::IntegrationAndContractTests
                    && pending_test_attribute
                    && symbol.kind == "fn"
                {
                    let test_node = test_case_node(
                        &input.source_path,
                        &symbol.name,
                        line_number,
                        content_sha256,
                    );
                    let command_node = validation_command_node(&input.source_path, &symbol.name);
                    state.push_edge(edge(
                        SemanticEdgeType::Exercises,
                        &file_node_id,
                        &test_node.id,
                        "rust_test_case",
                    ));
                    state.push_edge(edge(
                        SemanticEdgeType::SuggestsValidation,
                        &test_node.id,
                        &command_node.id,
                        "focused_test_command",
                    ));
                    state.push_node(test_node);
                    state.push_node(command_node);
                }

                let symbol_node = code_symbol_node(
                    &input.source_path,
                    &symbol.kind,
                    &symbol.name,
                    line_number,
                    content_sha256,
                );
                state.push_edge(edge(
                    SemanticEdgeType::Defines,
                    &file_node_id,
                    &symbol_node.id,
                    "rust_symbol",
                ));
                state.push_node(symbol_node);
                pending_test_attribute = false;
            } else if !trimmed.starts_with("#[") && !trimmed.is_empty() {
                pending_test_attribute = false;
            }
        }
    }

    fn ingest_markdown_file(
        input: &DiscoveredInput,
        content: &str,
        content_sha256: &str,
        size_bytes: u64,
        state: &mut GraphBuildState,
    ) {
        let line_count = count_lines(content);
        let file_node = file_region_node(
            &input.source_path,
            content_sha256,
            size_bytes,
            1,
            line_count,
            input.surface.as_str(),
        );
        let file_node_id = file_node.id.clone();
        state.push_node(file_node);

        for (idx, line) in content.lines().enumerate() {
            let Some((level, title)) = parse_markdown_heading(line) else {
                continue;
            };
            let section_node = doc_section_node(
                &input.source_path,
                level,
                &title,
                idx.saturating_add(1),
                content_sha256,
            );
            state.push_edge(edge(
                SemanticEdgeType::Contains,
                &file_node_id,
                &section_node.id,
                "markdown_heading",
            ));
            state.push_node(section_node);
        }
    }

    fn ingest_evidence_file(
        &self,
        input: &DiscoveredInput,
        content: &str,
        content_sha256: &str,
        size_bytes: u64,
        state: &mut GraphBuildState,
    ) {
        let line_count = count_lines(content);
        let file_node = file_region_node(
            &input.source_path,
            content_sha256,
            size_bytes,
            1,
            line_count,
            input.surface.as_str(),
        );
        let file_node_id = file_node.id.clone();
        state.push_node(file_node);

        match serde_json::from_str::<Value>(content) {
            Ok(value) => {
                let evidence_node = evidence_artifact_node(
                    &input.source_path,
                    &value,
                    content_sha256,
                    &self.options,
                );
                state.push_edge(edge(
                    SemanticEdgeType::Tracks,
                    &file_node_id,
                    &evidence_node.id,
                    "json_evidence_artifact",
                ));
                state.push_node(evidence_node);
            }
            Err(error) => {
                let mut node = missing_or_unreadable_evidence_node(
                    &input.source_path,
                    EvidenceFreshnessStatus::Malformed,
                    "json_parse_failed",
                );
                node.content_sha256 = Some(content_sha256.to_string());
                node.metadata.insert(
                    "parse_error".to_string(),
                    json!(redact_error_message(&error.to_string())),
                );
                state.push_edge(edge(
                    SemanticEdgeType::Tracks,
                    &file_node_id,
                    &node.id,
                    "malformed_json_evidence",
                ));
                state.push_node(node);
                state.push_trace(GraphBuildTraceEvent::new(
                    input.surface.as_str(),
                    input.source_path.clone(),
                    GraphInputStatus::Malformed,
                    "json_parse_failed",
                    1,
                    1,
                ));
            }
        }
    }

    fn ingest_beads_jsonl(
        &self,
        input: &DiscoveredInput,
        content: &str,
        content_sha256: &str,
        size_bytes: u64,
        state: &mut GraphBuildState,
    ) {
        let line_count = count_lines(content);
        let file_node = file_region_node(
            &input.source_path,
            content_sha256,
            size_bytes,
            1,
            line_count,
            input.surface.as_str(),
        );
        let file_node_id = file_node.id.clone();
        state.push_node(file_node);

        for (idx, line) in content.lines().enumerate() {
            let line_number = idx.saturating_add(1);
            if line.trim().is_empty() {
                continue;
            }

            match serde_json::from_str::<Value>(line) {
                Ok(value) => {
                    let classified =
                        classify_bead_actionability(&value, self.options.reference_time_utc);
                    let bead_id = value
                        .get("id")
                        .and_then(Value::as_str)
                        .unwrap_or("missing-bead-id");
                    let node = bead_node(
                        &input.source_path,
                        line_number,
                        bead_id,
                        &value,
                        &classified,
                    );
                    state.push_edge(edge(
                        SemanticEdgeType::Tracks,
                        &file_node_id,
                        &node.id,
                        "beads_jsonl_record",
                    ));
                    add_bead_dependency_edges(&node.id, &value, state);
                    state.push_node(node);
                }
                Err(error) => {
                    let classified = ClassifiedBeadActionability {
                        status: BeadActionabilityStatus::UnknownFailClosed,
                        planner_may_claim: false,
                        reason: "malformed_jsonl".to_string(),
                    };
                    let mut node = bead_node(
                        &input.source_path,
                        line_number,
                        &format!("malformed-line-{line_number}"),
                        &json!({ "id": format!("malformed-line-{line_number}") }),
                        &classified,
                    );
                    node.metadata.insert(
                        "parse_error".to_string(),
                        json!(redact_error_message(&error.to_string())),
                    );
                    state.push_edge(edge(
                        SemanticEdgeType::Tracks,
                        &file_node_id,
                        &node.id,
                        "malformed_beads_jsonl_record",
                    ));
                    state.push_node(node);
                    state.push_trace(GraphBuildTraceEvent::new(
                        input.surface.as_str(),
                        input.source_path.clone(),
                        GraphInputStatus::Malformed,
                        "beads_jsonl_parse_failed",
                        1,
                        1,
                    ));
                }
            }
        }
    }

    fn record_missing_input(state: &mut GraphBuildState, source_path: &str) {
        let surface = surface_for_path(source_path).unwrap_or(SourceSurface::Unknown);
        state.push_trace(GraphBuildTraceEvent::new(
            surface.as_str(),
            source_path.to_string(),
            GraphInputStatus::Missing,
            "expected_input_missing",
            0,
            0,
        ));
        if surface == SourceSurface::EvidenceArtifacts {
            state.push_node(missing_or_unreadable_evidence_node(
                source_path,
                EvidenceFreshnessStatus::Missing,
                "expected_input_missing",
            ));
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SemanticWorkspaceGraph {
    pub schema: String,
    pub builder_schema: String,
    pub root: String,
    pub nodes: Vec<SemanticGraphNode>,
    pub edges: Vec<SemanticGraphEdge>,
    pub input_fingerprints: Vec<InputFingerprint>,
    pub trace: Vec<GraphBuildTraceEvent>,
}

impl SemanticWorkspaceGraph {
    pub fn nodes_by_type(&self, node_type: SemanticNodeType) -> Vec<&SemanticGraphNode> {
        self.nodes
            .iter()
            .filter(|node| node.node_type == node_type)
            .collect()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InputFingerprint {
    pub source_path: String,
    pub surface_id: String,
    pub sha256: String,
    pub size_bytes: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mtime_unix_ns: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SemanticGraphNode {
    pub id: String,
    pub node_type: SemanticNodeType,
    pub source_path: String,
    pub title: String,
    pub stable_key: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line_start: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line_end: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub freshness_status: Option<EvidenceFreshnessStatus>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bead_actionability_status: Option<BeadActionabilityStatus>,
    pub redaction_status: RedactionStatus,
    pub metadata: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SemanticGraphEdge {
    pub id: String,
    pub edge_type: SemanticEdgeType,
    pub source: String,
    pub target: String,
    pub reason: String,
    pub metadata: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphBuildTraceEvent {
    pub schema: String,
    pub surface_id: String,
    pub source_path: String,
    pub status: GraphInputStatus,
    pub reason: String,
    pub node_count: usize,
    pub edge_count: usize,
}

impl GraphBuildTraceEvent {
    fn new(
        surface_id: &str,
        source_path: String,
        status: GraphInputStatus,
        reason: &str,
        node_count: usize,
        edge_count: usize,
    ) -> Self {
        Self {
            schema: GRAPH_BUILDER_SCHEMA.to_string(),
            surface_id: surface_id.to_string(),
            source_path,
            status,
            reason: reason.to_string(),
            node_count,
            edge_count,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SemanticNodeType {
    CodeSymbol,
    FileRegion,
    TestCase,
    DocSection,
    EvidenceArtifact,
    Bead,
    ProviderSurface,
    ValidationCommand,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SemanticEdgeType {
    Contains,
    Defines,
    Exercises,
    Validates,
    CitesEvidence,
    Tracks,
    Blocks,
    DependsOn,
    SuggestsValidation,
    Supersedes,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EvidenceFreshnessStatus {
    Current,
    HistoricalSnapshot,
    Stale,
    Missing,
    Malformed,
    Uncertified,
    FreshnessUnknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BeadActionabilityStatus {
    ActionableOpen,
    ClaimedInProgress,
    StalledReopenCandidate,
    Blocked,
    ClosedReferenceOnly,
    TombstoneReferenceOnly,
    UnknownFailClosed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GraphInputStatus {
    Indexed,
    Missing,
    Unreadable,
    Malformed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RedactionStatus {
    None,
    Redacted,
    SensitiveOmitted,
    UnsafeToEmit,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClassifiedBeadActionability {
    pub status: BeadActionabilityStatus,
    pub planner_may_claim: bool,
    pub reason: String,
}

#[derive(Debug)]
pub enum SemanticGraphBuildError {
    RootUnreadable { root: String, source: io::Error },
    RootNotDirectory { root: String },
}

impl fmt::Display for SemanticGraphBuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::RootUnreadable { root, source } => {
                write!(f, "semantic graph root is unreadable: {root}: {source}")
            }
            Self::RootNotDirectory { root } => {
                write!(f, "semantic graph root is not a directory: {root}")
            }
        }
    }
}

impl StdError for SemanticGraphBuildError {
    fn source(&self) -> Option<&(dyn StdError + 'static)> {
        match self {
            Self::RootUnreadable { source, .. } => Some(source),
            Self::RootNotDirectory { .. } => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DiscoveredInput {
    absolute_path: PathBuf,
    source_path: String,
    surface: SourceSurface,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum SourceSurface {
    RustCodeModules,
    IntegrationAndContractTests,
    ReadmeAndDocs,
    EvidenceArtifacts,
    BeadsIssueGraph,
    Unknown,
}

impl SourceSurface {
    fn as_str(self) -> &'static str {
        match self {
            Self::RustCodeModules => "rust_code_modules",
            Self::IntegrationAndContractTests => "integration_and_contract_tests",
            Self::ReadmeAndDocs => "readme_and_docs",
            Self::EvidenceArtifacts => "dropin_and_parity_evidence",
            Self::BeadsIssueGraph => "beads_issue_graph",
            Self::Unknown => "unknown",
        }
    }
}

#[derive(Default)]
struct GraphBuildState {
    nodes: Vec<SemanticGraphNode>,
    edges: Vec<SemanticGraphEdge>,
    input_fingerprints: Vec<InputFingerprint>,
    trace: Vec<GraphBuildTraceEvent>,
}

impl GraphBuildState {
    fn push_node(&mut self, node: SemanticGraphNode) {
        self.nodes.push(node);
    }

    fn push_edge(&mut self, edge: SemanticGraphEdge) {
        self.edges.push(edge);
    }

    fn push_trace(&mut self, event: GraphBuildTraceEvent) {
        self.trace.push(event);
    }

    fn sort(&mut self) {
        self.nodes.sort_by(|left, right| left.id.cmp(&right.id));
        self.nodes.dedup_by(|left, right| left.id == right.id);
        self.edges.sort_by(|left, right| left.id.cmp(&right.id));
        self.edges.dedup_by(|left, right| left.id == right.id);
        self.input_fingerprints
            .sort_by(|left, right| left.source_path.cmp(&right.source_path));
        self.input_fingerprints
            .dedup_by(|left, right| left.source_path == right.source_path);
        self.trace.sort_by(|left, right| {
            left.source_path
                .cmp(&right.source_path)
                .then_with(|| left.surface_id.cmp(&right.surface_id))
                .then_with(|| left.reason.cmp(&right.reason))
        });
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ParsedRustSymbol {
    kind: String,
    name: String,
}

pub fn classify_bead_actionability(
    value: &Value,
    reference_time_utc: Option<DateTime<Utc>>,
) -> ClassifiedBeadActionability {
    if value
        .get("deleted")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return ClassifiedBeadActionability {
            status: BeadActionabilityStatus::TombstoneReferenceOnly,
            planner_may_claim: false,
            reason: "tombstone_is_never_actionable".to_string(),
        };
    }

    let Some(status) = value.get("status").and_then(Value::as_str) else {
        return ClassifiedBeadActionability {
            status: BeadActionabilityStatus::UnknownFailClosed,
            planner_may_claim: false,
            reason: "missing_status".to_string(),
        };
    };

    match status {
        "open" => {
            if has_blocking_dependency(value) {
                ClassifiedBeadActionability {
                    status: BeadActionabilityStatus::Blocked,
                    planner_may_claim: false,
                    reason: "open_with_blocking_dependency".to_string(),
                }
            } else {
                ClassifiedBeadActionability {
                    status: BeadActionabilityStatus::ActionableOpen,
                    planner_may_claim: true,
                    reason: "open_without_blockers".to_string(),
                }
            }
        }
        "in_progress" => classify_in_progress_bead(value, reference_time_utc),
        "closed" => ClassifiedBeadActionability {
            status: BeadActionabilityStatus::ClosedReferenceOnly,
            planner_may_claim: false,
            reason: "closed_work_is_context_only".to_string(),
        },
        "tombstone" => ClassifiedBeadActionability {
            status: BeadActionabilityStatus::TombstoneReferenceOnly,
            planner_may_claim: false,
            reason: "tombstone_is_never_actionable".to_string(),
        },
        _ => ClassifiedBeadActionability {
            status: BeadActionabilityStatus::UnknownFailClosed,
            planner_may_claim: false,
            reason: "unknown_status".to_string(),
        },
    }
}

fn classify_in_progress_bead(
    value: &Value,
    reference_time_utc: Option<DateTime<Utc>>,
) -> ClassifiedBeadActionability {
    let Some(reference_time_utc) = reference_time_utc else {
        return ClassifiedBeadActionability {
            status: BeadActionabilityStatus::ClaimedInProgress,
            planner_may_claim: false,
            reason: "claimed_by_an_agent".to_string(),
        };
    };

    let Some(updated_at) = value.get("updated_at").and_then(Value::as_str) else {
        return ClassifiedBeadActionability {
            status: BeadActionabilityStatus::ClaimedInProgress,
            planner_may_claim: false,
            reason: "claimed_without_updated_at".to_string(),
        };
    };

    let Ok(updated_at) = DateTime::parse_from_rfc3339(updated_at) else {
        return ClassifiedBeadActionability {
            status: BeadActionabilityStatus::UnknownFailClosed,
            planner_may_claim: false,
            reason: "invalid_updated_at".to_string(),
        };
    };

    if reference_time_utc
        .signed_duration_since(updated_at.with_timezone(&Utc))
        .num_hours()
        >= 24
    {
        ClassifiedBeadActionability {
            status: BeadActionabilityStatus::StalledReopenCandidate,
            planner_may_claim: false,
            reason: "in_progress_updated_at_is_stale".to_string(),
        }
    } else {
        ClassifiedBeadActionability {
            status: BeadActionabilityStatus::ClaimedInProgress,
            planner_may_claim: false,
            reason: "claimed_by_an_agent".to_string(),
        }
    }
}

pub fn classify_evidence_freshness(
    value: &Value,
    options: &SemanticWorkspaceGraphBuildOptions,
) -> (EvidenceFreshnessStatus, bool, String) {
    if value
        .get("claim_surface")
        .and_then(Value::as_str)
        .is_some_and(|surface| surface == "historical_snapshot")
    {
        return (
            EvidenceFreshnessStatus::HistoricalSnapshot,
            false,
            "claim_surface_is_historical_snapshot".to_string(),
        );
    }

    if value
        .get("overall_verdict")
        .and_then(Value::as_str)
        .is_some_and(|verdict| verdict != "CERTIFIED")
    {
        return (
            EvidenceFreshnessStatus::Uncertified,
            false,
            "overall_verdict_not_certified".to_string(),
        );
    }

    let Some(generated_at) = value.get("generated_at").and_then(Value::as_str) else {
        return (
            EvidenceFreshnessStatus::FreshnessUnknown,
            false,
            "missing_generated_at".to_string(),
        );
    };

    let Ok(generated_at) = DateTime::parse_from_rfc3339(generated_at) else {
        return (
            EvidenceFreshnessStatus::Malformed,
            false,
            "invalid_generated_at".to_string(),
        );
    };

    let Some(reference_time_utc) = options.reference_time_utc else {
        return (
            EvidenceFreshnessStatus::FreshnessUnknown,
            false,
            "reference_time_not_provided".to_string(),
        );
    };

    if reference_time_utc
        .signed_duration_since(generated_at.with_timezone(&Utc))
        .num_days()
        > options.stale_after_days
    {
        (
            EvidenceFreshnessStatus::Stale,
            false,
            "generated_at_older_than_policy".to_string(),
        )
    } else {
        (
            EvidenceFreshnessStatus::Current,
            true,
            "generated_at_within_policy".to_string(),
        )
    }
}

fn file_region_node(
    source_path: &str,
    content_sha256: &str,
    size_bytes: u64,
    line_start: usize,
    line_end: usize,
    surface_id: &str,
) -> SemanticGraphNode {
    let stable_key = source_path.to_string();
    let mut metadata = BTreeMap::new();
    metadata.insert("surface_id".to_string(), json!(surface_id));
    SemanticGraphNode {
        id: stable_id("file_region", &[&stable_key]),
        node_type: SemanticNodeType::FileRegion,
        source_path: source_path.to_string(),
        title: source_path.to_string(),
        stable_key,
        content_sha256: Some(content_sha256.to_string()),
        size_bytes: Some(size_bytes),
        line_start: Some(line_start),
        line_end: Some(line_end),
        freshness_status: None,
        bead_actionability_status: None,
        redaction_status: RedactionStatus::None,
        metadata,
    }
}

fn code_symbol_node(
    source_path: &str,
    kind: &str,
    name: &str,
    line: usize,
    content_sha256: &str,
) -> SemanticGraphNode {
    let stable_key = format!("{source_path}:{kind}:{name}:{line}");
    let mut metadata = BTreeMap::new();
    metadata.insert("symbol_kind".to_string(), json!(kind));
    SemanticGraphNode {
        id: stable_id("code_symbol", &[&stable_key]),
        node_type: SemanticNodeType::CodeSymbol,
        source_path: source_path.to_string(),
        title: name.to_string(),
        stable_key,
        content_sha256: Some(content_sha256.to_string()),
        size_bytes: None,
        line_start: Some(line),
        line_end: Some(line),
        freshness_status: None,
        bead_actionability_status: None,
        redaction_status: RedactionStatus::None,
        metadata,
    }
}

fn test_case_node(
    source_path: &str,
    name: &str,
    line: usize,
    content_sha256: &str,
) -> SemanticGraphNode {
    let stable_key = format!("{source_path}:test:{name}:{line}");
    SemanticGraphNode {
        id: stable_id("test_case", &[&stable_key]),
        node_type: SemanticNodeType::TestCase,
        source_path: source_path.to_string(),
        title: name.to_string(),
        stable_key,
        content_sha256: Some(content_sha256.to_string()),
        size_bytes: None,
        line_start: Some(line),
        line_end: Some(line),
        freshness_status: None,
        bead_actionability_status: None,
        redaction_status: RedactionStatus::None,
        metadata: BTreeMap::new(),
    }
}

fn doc_section_node(
    source_path: &str,
    level: usize,
    title: &str,
    line: usize,
    content_sha256: &str,
) -> SemanticGraphNode {
    let stable_key = format!("{source_path}:heading:{level}:{line}:{title}");
    let mut metadata = BTreeMap::new();
    metadata.insert("heading_level".to_string(), json!(level));
    SemanticGraphNode {
        id: stable_id("doc_section", &[&stable_key]),
        node_type: SemanticNodeType::DocSection,
        source_path: source_path.to_string(),
        title: title.to_string(),
        stable_key,
        content_sha256: Some(content_sha256.to_string()),
        size_bytes: None,
        line_start: Some(line),
        line_end: Some(line),
        freshness_status: None,
        bead_actionability_status: None,
        redaction_status: RedactionStatus::None,
        metadata,
    }
}

fn evidence_artifact_node(
    source_path: &str,
    value: &Value,
    content_sha256: &str,
    options: &SemanticWorkspaceGraphBuildOptions,
) -> SemanticGraphNode {
    let artifact_schema = value
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("schema_missing");
    let stable_key = format!("{source_path}:{artifact_schema}");
    let (freshness_status, release_claim_allowed, reason) =
        classify_evidence_freshness(value, options);
    let mut metadata = BTreeMap::new();
    metadata.insert("artifact_schema".to_string(), json!(artifact_schema));
    if let Some(generated_at) = value.get("generated_at").and_then(Value::as_str) {
        metadata.insert("generated_at".to_string(), json!(generated_at));
    }
    if let Some(overall_verdict) = value.get("overall_verdict").and_then(Value::as_str) {
        metadata.insert("overall_verdict".to_string(), json!(overall_verdict));
    }
    metadata.insert(
        "release_claim_allowed".to_string(),
        json!(release_claim_allowed),
    );
    metadata.insert("freshness_reason".to_string(), json!(reason));

    SemanticGraphNode {
        id: stable_id("evidence_artifact", &[&stable_key]),
        node_type: SemanticNodeType::EvidenceArtifact,
        source_path: source_path.to_string(),
        title: artifact_schema.to_string(),
        stable_key,
        content_sha256: Some(content_sha256.to_string()),
        size_bytes: None,
        line_start: None,
        line_end: None,
        freshness_status: Some(freshness_status),
        bead_actionability_status: None,
        redaction_status: RedactionStatus::None,
        metadata,
    }
}

fn missing_or_unreadable_evidence_node(
    source_path: &str,
    freshness_status: EvidenceFreshnessStatus,
    reason: &str,
) -> SemanticGraphNode {
    let stable_key = format!("{source_path}:missing_or_unreadable");
    let mut metadata = BTreeMap::new();
    metadata.insert("freshness_reason".to_string(), json!(reason));
    metadata.insert("release_claim_allowed".to_string(), json!(false));
    SemanticGraphNode {
        id: stable_id("evidence_artifact", &[&stable_key]),
        node_type: SemanticNodeType::EvidenceArtifact,
        source_path: source_path.to_string(),
        title: source_path.to_string(),
        stable_key,
        content_sha256: None,
        size_bytes: None,
        line_start: None,
        line_end: None,
        freshness_status: Some(freshness_status),
        bead_actionability_status: None,
        redaction_status: RedactionStatus::None,
        metadata,
    }
}

fn bead_node(
    source_path: &str,
    line: usize,
    bead_id: &str,
    value: &Value,
    classified: &ClassifiedBeadActionability,
) -> SemanticGraphNode {
    let stable_key = bead_id.to_string();
    let mut metadata = BTreeMap::new();
    metadata.insert("bead_id".to_string(), json!(bead_id));
    metadata.insert(
        "planner_may_claim".to_string(),
        json!(classified.planner_may_claim),
    );
    metadata.insert(
        "actionability_reason".to_string(),
        json!(classified.reason.clone()),
    );
    if let Some(status) = value.get("status").and_then(Value::as_str) {
        metadata.insert("status".to_string(), json!(status));
    }
    if let Some(title) = value.get("title").and_then(Value::as_str) {
        metadata.insert("title".to_string(), json!(title));
    }
    if let Some(priority) = value.get("priority").and_then(Value::as_i64) {
        metadata.insert("priority".to_string(), json!(priority));
    }
    if let Some(issue_type) = value.get("issue_type").and_then(Value::as_str) {
        metadata.insert("issue_type".to_string(), json!(issue_type));
    }

    SemanticGraphNode {
        id: stable_id("bead", &[bead_id]),
        node_type: SemanticNodeType::Bead,
        source_path: source_path.to_string(),
        title: bead_id.to_string(),
        stable_key,
        content_sha256: None,
        size_bytes: None,
        line_start: Some(line),
        line_end: Some(line),
        freshness_status: None,
        bead_actionability_status: Some(classified.status),
        redaction_status: RedactionStatus::None,
        metadata,
    }
}

fn provider_surface_node(source_path: &str, content_sha256: &str) -> SemanticGraphNode {
    let provider = Path::new(source_path)
        .file_stem()
        .and_then(|stem| stem.to_str())
        .unwrap_or("unknown_provider");
    let stable_key = format!("provider:{provider}:{source_path}");
    let mut metadata = BTreeMap::new();
    metadata.insert("provider_id".to_string(), json!(provider));
    SemanticGraphNode {
        id: stable_id("provider_surface", &[&stable_key]),
        node_type: SemanticNodeType::ProviderSurface,
        source_path: source_path.to_string(),
        title: provider.to_string(),
        stable_key,
        content_sha256: Some(content_sha256.to_string()),
        size_bytes: None,
        line_start: None,
        line_end: None,
        freshness_status: None,
        bead_actionability_status: None,
        redaction_status: RedactionStatus::None,
        metadata,
    }
}

fn validation_command_node(source_path: &str, test_name: &str) -> SemanticGraphNode {
    let test_target = Path::new(source_path)
        .file_stem()
        .and_then(|stem| stem.to_str())
        .unwrap_or("unknown_test");
    let command = format!("cargo test --test {test_target} {test_name}");
    let stable_key = command.clone();
    let mut metadata = BTreeMap::new();
    metadata.insert("command".to_string(), json!(command));
    metadata.insert("test_target".to_string(), json!(test_target));
    SemanticGraphNode {
        id: stable_id("validation_command", &[&stable_key]),
        node_type: SemanticNodeType::ValidationCommand,
        source_path: source_path.to_string(),
        title: stable_key.clone(),
        stable_key,
        content_sha256: None,
        size_bytes: None,
        line_start: None,
        line_end: None,
        freshness_status: None,
        bead_actionability_status: None,
        redaction_status: RedactionStatus::None,
        metadata,
    }
}

fn edge(
    edge_type: SemanticEdgeType,
    source: &str,
    target: &str,
    reason: &str,
) -> SemanticGraphEdge {
    let edge_type_key = format!("{edge_type:?}");
    let stable_key = [edge_type_key.as_str(), source, target, reason];
    SemanticGraphEdge {
        id: stable_id("edge", &stable_key),
        edge_type,
        source: source.to_string(),
        target: target.to_string(),
        reason: reason.to_string(),
        metadata: BTreeMap::new(),
    }
}

fn add_bead_dependency_edges(current_node_id: &str, value: &Value, state: &mut GraphBuildState) {
    let Some(dependencies) = value.get("dependencies").and_then(Value::as_array) else {
        return;
    };
    for dependency in dependencies {
        let Some(depends_on_id) = dependency.get("depends_on_id").and_then(Value::as_str) else {
            continue;
        };
        let relation = dependency
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("depends_on");
        let edge_type = if relation == "blocks" {
            SemanticEdgeType::Blocks
        } else {
            SemanticEdgeType::DependsOn
        };
        let target = stable_id("bead", &[depends_on_id]);
        state.push_edge(edge(
            edge_type,
            current_node_id,
            &target,
            "beads_jsonl_dependency",
        ));
    }
}

fn has_blocking_dependency(value: &Value) -> bool {
    value
        .get("dependencies")
        .and_then(Value::as_array)
        .is_some_and(|dependencies| {
            dependencies.iter().any(|dependency| {
                dependency
                    .get("type")
                    .and_then(Value::as_str)
                    .is_some_and(|relation| relation == "blocks")
            })
        })
}

fn parse_rust_symbol(line: &str) -> Option<ParsedRustSymbol> {
    if line.starts_with("//") {
        return None;
    }

    let tokens: Vec<&str> = line
        .split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '_'))
        .filter(|token| !token.is_empty())
        .collect();
    for window in tokens.windows(2) {
        let kind = window[0];
        if matches!(kind, "fn" | "struct" | "enum" | "trait" | "mod") {
            return Some(ParsedRustSymbol {
                kind: kind.to_string(),
                name: window[1].to_string(),
            });
        }
    }
    None
}

fn parse_markdown_heading(line: &str) -> Option<(usize, String)> {
    let trimmed = line.trim_start();
    let level = trimmed.chars().take_while(|ch| *ch == '#').count();
    if level == 0 || level > 6 {
        return None;
    }
    let title = trimmed[level..].trim();
    if title.is_empty() {
        return None;
    }
    Some((level, title.to_string()))
}

fn is_test_attribute(line: &str) -> bool {
    line == "#[test]" || line.starts_with("#[tokio::test") || line.starts_with("#[asupersync::test")
}

fn is_provider_surface(source_path: &str) -> bool {
    source_path.starts_with("src/providers/")
        && has_extension(source_path, "rs")
        && !file_name_eq(source_path, "mod.rs")
}

fn should_skip_dir(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| matches!(name, ".git" | "target"))
}

fn surface_for_path(source_path: &str) -> Option<SourceSurface> {
    if source_path == ".beads/issues.jsonl" {
        return Some(SourceSurface::BeadsIssueGraph);
    }
    if source_path == "README.md"
        || source_path.starts_with("docs/") && has_extension(source_path, "md")
    {
        return Some(SourceSurface::ReadmeAndDocs);
    }
    if source_path.starts_with("docs/") && has_extension(source_path, "json") {
        return Some(SourceSurface::EvidenceArtifacts);
    }
    if source_path.starts_with("src/") && has_extension(source_path, "rs") {
        return Some(SourceSurface::RustCodeModules);
    }
    if source_path.starts_with("tests/") && has_extension(source_path, "rs") {
        return Some(SourceSurface::IntegrationAndContractTests);
    }
    None
}

fn has_extension(source_path: &str, extension: &str) -> bool {
    Path::new(source_path)
        .extension()
        .is_some_and(|value| value.eq_ignore_ascii_case(extension))
}

fn file_name_eq(source_path: &str, file_name: &str) -> bool {
    Path::new(source_path)
        .file_name()
        .is_some_and(|value| value.eq_ignore_ascii_case(file_name))
}

fn count_lines(content: &str) -> usize {
    content.lines().count().max(1)
}

fn file_mtime_unix_ns(path: &Path) -> io::Result<Option<u64>> {
    let modified = fs::metadata(path)?.modified()?;
    let Ok(duration) = modified.duration_since(UNIX_EPOCH) else {
        return Ok(None);
    };
    let nanos = duration.as_nanos();
    Ok(u64::try_from(nanos).ok())
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn stable_id(kind: &str, parts: &[&str]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(kind.as_bytes());
    for part in parts {
        hasher.update(b"\0");
        hasher.update(part.as_bytes());
    }
    let digest = format!("{:x}", hasher.finalize());
    format!("swg:{kind}:{}", &digest[..16])
}

fn normalize_relative_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .map_or_else(|_| normalize_path(path), normalize_path)
}

fn normalize_path(path: &Path) -> String {
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            Component::Prefix(prefix) => {
                parts.push(prefix.as_os_str().to_string_lossy().into_owned());
            }
            Component::RootDir => {
                parts.push(String::new());
            }
            Component::CurDir => {}
            Component::ParentDir => parts.push("..".to_string()),
            Component::Normal(part) => parts.push(part.to_string_lossy().into_owned()),
        }
    }
    if parts.len() > 1 && parts.first().is_some_and(String::is_empty) {
        format!("/{}", parts[1..].join("/"))
    } else {
        parts.join("/")
    }
}

fn redact_error_message(message: &str) -> String {
    message
        .replace("authorization", "[redacted-keyword]")
        .replace("token", "[redacted-keyword]")
        .replace("secret", "[redacted-keyword]")
}
