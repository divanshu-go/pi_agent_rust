//! SDK controller tests: concurrency-safe queue, steer, cancel, promote, abort,
//! and idle signaling for the embedder-facing `AgentSessionController`.
//!
//! Uses a gated provider so a prompt turn can be held open while concurrent
//! queue/steer/cancel/abort operations are exercised, then released to verify
//! drain behavior — without depending on a real LLM.

mod common;

use async_trait::async_trait;
use common::run_async;
use futures::Stream;
use pi::agent::{AgentConfig, AgentSession};
use pi::compaction::ResolvedCompactionSettings;
use pi::error::Result;
use pi::model::{
    AssistantMessage, ContentBlock, Message, StopReason, StreamEvent, TextContent, ThinkingLevel,
    Usage, UserContent,
};
use pi::provider::{Context, Provider, StreamOptions};
use pi::sdk::{AgentSessionHandle, EventListeners};
use pi::sdk_controller::{
    AgentSessionController, ControllerSpawner, QueueKind, QueueSnapshot,
};
use pi::session::Session;
use pi::tools::ToolRegistry;
use std::pin::Pin;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

// ============================================================================
// GatedProvider — first turn blocks until released; records injected messages
// ============================================================================

/// A provider whose first `stream` call blocks until a release signal fires.
/// This keeps a turn "active" so concurrent controller operations can be
/// tested.
///
/// On each call it records the user-message texts it observed in `context` so
/// tests can assert that queued steering/follow-up messages reached the turn.
struct GatedProvider {
    call_count: AtomicUsize,
    /// Receiver fired to release the first (gated) call. Taken once.
    gate: Mutex<Option<futures::channel::oneshot::Receiver<()>>>,
    /// User-message texts observed across all stream calls.
    seen_user_texts: Arc<Mutex<Vec<String>>>,
}

impl std::fmt::Debug for GatedProvider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("GatedProvider").finish()
    }
}

impl GatedProvider {
    fn new(gate: futures::channel::oneshot::Receiver<()>) -> Self {
        Self {
            call_count: AtomicUsize::new(0),
            gate: Mutex::new(Some(gate)),
            seen_user_texts: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn assistant_msg(stop: StopReason, content: Vec<ContentBlock>) -> AssistantMessage {
        AssistantMessage {
            content,
            api: "gated".to_string(),
            provider: "gated".to_string(),
            model: "gated-model".to_string(),
            usage: Usage {
                total_tokens: 5,
                output: 5,
                ..Usage::default()
            },
            stop_reason: stop,
            error_message: None,
            timestamp: 0,
        }
    }

    fn done_stream(
        msg: AssistantMessage,
    ) -> Pin<Box<dyn Stream<Item = Result<StreamEvent>> + Send>> {
        let partial = Self::assistant_msg(StopReason::Stop, Vec::new());
        Box::pin(futures::stream::iter(vec![
            Ok(StreamEvent::Start { partial }),
            Ok(StreamEvent::Done {
                reason: msg.stop_reason,
                message: msg,
            }),
        ]))
    }

    fn record_context(&self, context: &Context<'_>) {
        let mut texts = self.seen_user_texts.lock().expect("lock");
        for msg in context.messages.iter() {
            if let Message::User(u) = msg {
                match &u.content {
                    UserContent::Text(t) => texts.push(t.clone()),
                    UserContent::Blocks(blocks) => {
                        for b in blocks {
                            if let ContentBlock::Text(t) = b {
                                texts.push(t.text.clone());
                            }
                        }
                    }
                }
            }
        }
    }
}

#[async_trait]
#[allow(clippy::unnecessary_literal_bound)]
impl Provider for GatedProvider {
    fn name(&self) -> &str {
        "gated"
    }
    fn api(&self) -> &str {
        "gated"
    }
    fn model_id(&self) -> &str {
        "gated-model"
    }

    async fn stream(
        &self,
        context: &Context<'_>,
        _options: &StreamOptions,
    ) -> Result<Pin<Box<dyn Stream<Item = Result<StreamEvent>> + Send>>> {
        self.record_context(context);
        let idx = self.call_count.fetch_add(1, Ordering::SeqCst);
        if idx == 0 {
            // Block the first turn until the test fires the gate.
            let gate = self.gate.lock().expect("lock").take();
            if let Some(rx) = gate {
                let _ = rx.await;
            }
        }
        // After release (or on later calls) emit final text and stop.
        Ok(Self::done_stream(Self::assistant_msg(
            StopReason::Stop,
            vec![ContentBlock::Text(TextContent::new("done".to_string()))],
        )))
    }
}

// ============================================================================
// Helpers
// ============================================================================

/// Build a controller around a gated session. Returns (controller, gate sender,
/// seen-texts handle). Must be called inside a `run_async` task so the spawner
/// can capture the ambient asupersync runtime handle.
fn build_gated_controller() -> (
    AgentSessionController,
    futures::channel::oneshot::Sender<()>,
    Arc<Mutex<Vec<String>>>,
) {
    let (gate_tx, gate_rx) = futures::channel::oneshot::channel();
    let provider = Arc::new(GatedProvider::new(gate_rx));
    let seen = Arc::clone(&provider.seen_user_texts);
    let provider: Arc<dyn Provider> = provider;

    let cwd = std::env::temp_dir();
    let tools = ToolRegistry::new(&[], &cwd, None);
    let config = AgentConfig {
        system_prompt: None,
        max_tool_iterations: 10,
        stream_options: StreamOptions {
            api_key: Some("test-key".to_string()),
            ..StreamOptions::default()
        },
        block_images: false,
        fail_closed_hooks: false,
        tool_approval: None,
    };
    let agent = pi::agent::Agent::new(provider, tools, config);
    let session = Arc::new(asupersync::sync::Mutex::new(Session::create_with_dir(Some(
        cwd,
    ))));
    let agent_session =
        AgentSession::new(agent, session, false, ResolvedCompactionSettings::default());
    let handle =
        AgentSessionHandle::from_session_with_listeners(agent_session, EventListeners::default());

    let spawner: ControllerSpawner = Arc::new(|fut| {
        asupersync::runtime::Runtime::current_handle()
            .expect("ambient asupersync runtime handle")
            .spawn(fut);
    });
    let controller = AgentSessionController::from_handle(handle, spawner, Some("chat".to_string()));
    (controller, gate_tx, seen)
}

/// Yield to the scheduler a few times so spawned background work can progress.
async fn yield_now_n(n: usize) {
    for _ in 0..n {
        let (tx, rx) = futures::channel::oneshot::channel::<()>();
        asupersync::runtime::Runtime::current_handle()
            .expect("rt")
            .spawn(async move {
                let _ = tx.send(());
            });
        let _ = rx.await;
    }
}

// ============================================================================
// Tests
// ============================================================================

#[test]
fn controller_snapshot_empty_after_creation() {
    run_async(async {
        let (controller, _gate, _seen) = build_gated_controller();
        let snap = controller.queue_snapshot();
        assert!(!snap.active, "fresh controller should be idle");
        assert!(snap.queued.is_empty(), "queue should start empty");
        assert_eq!(snap.session_id.as_deref(), Some("chat"));
    });
}

#[test]
fn controller_prompt_while_active_returns_busy() {
    run_async(async {
        let (controller, gate, _seen) = build_gated_controller();
        // Start a turn (gated open).
        controller.prompt("first", Vec::new()).expect("first prompt");
        yield_now_n(4).await;
        assert!(controller.is_active(), "turn should be active");

        let err = controller.prompt("second", Vec::new());
        assert!(err.is_err(), "overlapping prompt must be rejected");

        // Release and drain.
        let _ = gate.send(());
        controller.wait_for_idle().await.expect("wait idle");
        assert!(!controller.is_active());
    });
}

#[test]
fn controller_queue_follow_up_while_active_does_not_block() {
    run_async(async {
        let (controller, gate, _seen) = build_gated_controller();
        controller.prompt("first", Vec::new()).expect("prompt");
        yield_now_n(4).await;
        assert!(controller.is_active());

        // Queue must return immediately even though a turn holds the session.
        let q = controller
            .queue_follow_up("later", Vec::new(), Some("later".to_string()))
            .expect("queue follow up");
        assert_eq!(q.kind, QueueKind::FollowUp);

        let snap = controller.queue_snapshot();
        assert_eq!(snap.queued.len(), 1);
        assert_eq!(snap.queued[0].preview, "later");
        assert!(snap.active);

        let _ = gate.send(());
        controller.wait_for_idle().await.expect("idle");
    });
}

#[test]
fn controller_steer_while_active_is_accepted_immediately() {
    run_async(async {
        let (controller, gate, _seen) = build_gated_controller();
        controller.prompt("first", Vec::new()).expect("prompt");
        yield_now_n(4).await;

        controller.steer("steer-now", Vec::new()).expect("steer");
        let snap = controller.queue_snapshot();
        assert_eq!(snap.queued.len(), 1);
        assert_eq!(snap.queued[0].kind, QueueKind::Steering);

        let _ = gate.send(());
        controller.wait_for_idle().await.expect("idle");
    });
}

#[test]
fn controller_cancel_removes_queued_once() {
    run_async(async {
        let (controller, gate, _seen) = build_gated_controller();
        controller.prompt("first", Vec::new()).expect("prompt");
        yield_now_n(4).await;

        let q = controller
            .queue_follow_up("cancel-me", Vec::new(), None)
            .expect("queue");
        assert_eq!(controller.queue_snapshot().queued.len(), 1);

        assert!(controller.cancel_queued(&q.id).expect("cancel"));
        assert_eq!(controller.queue_snapshot().queued.len(), 0);
        // Second cancel is a no-op.
        assert!(!controller.cancel_queued(&q.id).expect("cancel2"));

        let _ = gate.send(());
        controller.wait_for_idle().await.expect("idle");
    });
}

#[test]
fn controller_promote_preserves_message_and_images() {
    use pi::model::ImageContent;
    run_async(async {
        let (controller, gate, _seen) = build_gated_controller();
        controller.prompt("first", Vec::new()).expect("prompt");
        yield_now_n(4).await;

        let img = ImageContent {
            data: "QUJD".to_string(),
            mime_type: "image/png".to_string(),
        };
        let q = controller
            .queue_follow_up("promote-me", vec![img], Some("promote-me".to_string()))
            .expect("queue");
        assert_eq!(controller.queue_snapshot().queued[0].kind, QueueKind::FollowUp);

        let promoted = controller.promote_queued_to_steer(&q.id).expect("promote");
        assert!(promoted, "follow-up should promote");

        let snap = controller.queue_snapshot();
        assert_eq!(snap.queued.len(), 1, "still one entry, now steering");
        assert_eq!(snap.queued[0].kind, QueueKind::Steering);
        assert_eq!(snap.queued[0].id, q.id, "id preserved across promotion");
        assert_eq!(snap.queued[0].preview, "promote-me");

        // Promoting again (already steering) returns false.
        assert!(!controller.promote_queued_to_steer(&q.id).expect("promote2"));

        let _ = gate.send(());
        controller.wait_for_idle().await.expect("idle");
    });
}

#[test]
fn controller_abort_all_clears_queue() {
    run_async(async {
        let (controller, gate, _seen) = build_gated_controller();
        controller.prompt("first", Vec::new()).expect("prompt");
        yield_now_n(4).await;

        controller
            .queue_follow_up("f1", Vec::new(), None)
            .expect("q1");
        controller.steer("s1", Vec::new()).expect("s1");
        assert_eq!(controller.queue_snapshot().queued.len(), 2);

        // Release the gate so the aborted turn can reach terminal state, then
        // abort_all (which waits for idle internally).
        let _ = gate.send(());
        let summary = controller.abort_all().await.expect("abort all");
        assert_eq!(summary.cleared_queued, 2, "both queued entries cleared");
        assert!(controller.queue_snapshot().queued.is_empty());
        assert!(!controller.is_active());
    });
}

#[test]
fn controller_abort_active_only_preserves_follow_ups() {
    run_async(async {
        let (controller, gate, _seen) = build_gated_controller();
        controller.prompt("first", Vec::new()).expect("prompt");
        yield_now_n(4).await;

        controller
            .queue_follow_up("keep1", Vec::new(), None)
            .expect("q1");
        controller
            .queue_follow_up("keep2", Vec::new(), None)
            .expect("q2");
        controller.steer("drop-steer", Vec::new()).expect("s1");
        assert_eq!(controller.queue_snapshot().queued.len(), 3);

        let _ = gate.send(());
        let summary = controller.abort_active_only().await.expect("abort active");
        assert_eq!(summary.cleared_queued, 1, "only steering cleared");
        let snap = controller.queue_snapshot();
        assert_eq!(snap.queued.len(), 2, "follow-ups preserved");
        assert!(snap.queued.iter().all(|q| q.kind == QueueKind::FollowUp));
    });
}

#[test]
fn controller_wait_for_idle_resolves_after_agent_end() {
    run_async(async {
        let (controller, gate, seen) = build_gated_controller();
        controller.prompt("hello", Vec::new()).expect("prompt");
        yield_now_n(4).await;
        assert!(controller.is_active());

        let _ = gate.send(());
        controller.wait_for_idle().await.expect("idle");
        assert!(!controller.is_active(), "idle after agent_end");

        // The turn ran the prompt through the model.
        let texts = seen.lock().expect("lock").clone();
        assert!(
            texts.iter().any(|t| t == "hello"),
            "prompt text should reach the model: {texts:?}"
        );
    });
}

#[test]
fn controller_subscribe_queue_emits_snapshots() {
    run_async(async {
        let (controller, gate, _seen) = build_gated_controller();
        let snaps: Arc<Mutex<Vec<QueueSnapshot>>> = Arc::new(Mutex::new(Vec::new()));
        let snaps_ref = Arc::clone(&snaps);
        let _sub = controller.subscribe_queue(move |snap| {
            snaps_ref.lock().expect("lock").push(snap);
        });

        controller.prompt("first", Vec::new()).expect("prompt");
        yield_now_n(4).await;
        controller
            .queue_follow_up("f1", Vec::new(), None)
            .expect("q1");

        let captured = snaps.lock().expect("lock").clone();
        assert!(
            captured.iter().any(|s| s.queued.len() == 1),
            "subscriber should observe the enqueue snapshot"
        );

        let _ = gate.send(());
        controller.wait_for_idle().await.expect("idle");
    });
}

#[test]
fn controller_queued_follow_up_drains_into_turn() {
    run_async(async {
        let (controller, gate, seen) = build_gated_controller();
        controller.prompt("initial", Vec::new()).expect("prompt");
        yield_now_n(4).await;

        controller
            .queue_follow_up("the-follow-up", Vec::new(), None)
            .expect("queue");

        // Release: the first turn ends, then the agent drains the follow-up at
        // idle and runs a second turn with it.
        let _ = gate.send(());
        controller.wait_for_idle().await.expect("idle");
        yield_now_n(8).await;
        controller.wait_for_idle().await.expect("idle2");

        let texts = seen.lock().expect("lock").clone();
        assert!(
            texts.iter().any(|t| t == "the-follow-up"),
            "queued follow-up should drain into a model turn: {texts:?}"
        );
        // Queue is empty once drained.
        assert!(controller.queue_snapshot().queued.is_empty());
    });
}

#[test]
fn controller_set_and_read_thinking_level() {
    // Parity gap E3: the controller must expose thinking-level control so the
    // embedded chat path can change it (the RPC client did via set_thinking_level).
    run_async(async {
        let (controller, _gate, _seen) = build_gated_controller();
        controller
            .set_thinking_level(ThinkingLevel::Medium)
            .await
            .expect("set thinking level");
        let level = controller.thinking_level().await.expect("read level");
        assert_eq!(level, Some(ThinkingLevel::Medium));
    });
}

#[test]
fn controller_cycle_thinking_level_advances_and_wraps() {
    run_async(async {
        let (controller, _gate, _seen) = build_gated_controller();
        controller
            .set_thinking_level(ThinkingLevel::High)
            .await
            .expect("seed High");
        let next = controller.cycle_thinking_level().await.expect("cycle 1");
        assert_eq!(next, ThinkingLevel::XHigh, "High -> XHigh");
        let wrapped = controller.cycle_thinking_level().await.expect("cycle 2");
        assert_eq!(wrapped, ThinkingLevel::Off, "XHigh wraps to Off");
    });
}

#[test]
fn controller_messages_accessor_returns_history() {
    // Parity gap I5: the controller must expose message history for the frontend.
    run_async(async {
        let (controller, gate, _seen) = build_gated_controller();
        controller.prompt("remember this", Vec::new()).expect("prompt");
        yield_now_n(4).await;
        let _ = gate.send(());
        controller.wait_for_idle().await.expect("idle");

        let msgs = controller.messages().await.expect("messages");
        assert!(
            msgs.iter().any(|m| matches!(m, Message::User(_))),
            "history should contain the user turn: {} messages",
            msgs.len()
        );
    });
}
