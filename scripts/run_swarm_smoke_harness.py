#!/usr/bin/env python3
"""Run a no-mock smoke harness for the multi-agent swarm workflow.

The harness creates a disposable project under TMPDIR or /data/tmp, exercises
real Beads, Agent Mail, and RCH admission surfaces, then writes a deterministic
artifact bundle. It intentionally leaves temp projects and artifacts in place.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HARNESS_SCHEMA = "pi.swarm.smoke_harness.v1"
EVENT_SCHEMA = "pi.swarm.smoke_harness.event.v1"
DEFAULT_AGENTS = ("BlueLake", "GreenStone", "PurpleBridge")
DEFAULT_MCP_URL = "http://127.0.0.1:8765/mcp"
FORBIDDEN_WORKTREE_COMMAND_FRAGMENTS = (
    "git stash",
    "git reset",
    "git clean",
    "git checkout --",
    "git restore",
)
SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "bearer",
    "body",
    "cookie",
    "key",
    "password",
    "prompt",
    "registration_token",
    "secret",
    "token",
    "transcript",
)
SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"(?:api[_-]?key|authorization|password|registration_token|secret|token)"
    r"\s*[:=]\s*[\"']?[^\"'\s,}]+)"
)


class HarnessError(RuntimeError):
    """Raised when a required smoke-harness phase fails."""


@dataclass
class RedactionStats:
    redacted_count: int = 0
    fields: set[str] = field(default_factory=set)

    def merge(self, other: "RedactionStats") -> None:
        self.redacted_count += other.redacted_count
        self.fields.update(other.fields)

    def to_json(self) -> dict[str, Any]:
        return {
            "redacted_count": self.redacted_count,
            "fields": sorted(self.fields),
        }


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    cwd: str
    exit_code: int
    elapsed_ms: int
    stdout: str
    stderr: str

    def command_text(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)


class Clock:
    def __init__(self, fixed_start_ms: int | None = None) -> None:
        self.fixed_start_ms = fixed_start_ms
        self.sequence = 0

    def timestamp_ms(self) -> int:
        if self.fixed_start_ms is None:
            return int(time.time() * 1000)
        value = self.fixed_start_ms + self.sequence
        self.sequence += 1
        return value


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_string(value: str, field: str) -> tuple[str, RedactionStats]:
    stats = RedactionStats()
    if SENSITIVE_VALUE_RE.search(value):
        stats.redacted_count += 1
        stats.fields.add(field)
        return SENSITIVE_VALUE_RE.sub("[REDACTED]", value), stats
    return value, stats


def redact_json(value: Any, field: str = "value") -> tuple[Any, RedactionStats]:
    stats = RedactionStats()
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            child_field = f"{field}.{key}" if field else str(key)
            if is_sensitive_key(str(key)):
                out[key] = "[REDACTED]"
                stats.redacted_count += 1
                stats.fields.add(child_field)
                continue
            redacted, child_stats = redact_json(item, child_field)
            stats.merge(child_stats)
            out[key] = redacted
        return out, stats
    if isinstance(value, list):
        out_list = []
        for index, item in enumerate(value):
            redacted, child_stats = redact_json(item, f"{field}[{index}]")
            stats.merge(child_stats)
            out_list.append(redacted)
        return out_list, stats
    if isinstance(value, str):
        return redact_string(value, field)
    return value, stats


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def artifact_manifest_entry(artifact_id: str, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "id": artifact_id,
        "path": str(path),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_json_line(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise HarnessError("command did not emit a JSON object")


def parse_json_document(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise HarnessError("command emitted no JSON")
    return json.loads(stripped)


def issue_status_from_payload(payload: Any, issue_id: str) -> str | None:
    if isinstance(payload, dict):
        if payload.get("id") == issue_id:
            status = payload.get("status")
            return str(status) if status is not None else None
        for key in ("issue", "updated", "created"):
            nested = payload.get(key)
            status = issue_status_from_payload(nested, issue_id)
            if status is not None:
                return status
        issues = payload.get("issues")
        if isinstance(issues, list):
            return issue_status_from_payload(issues, issue_id)
    if isinstance(payload, list):
        for item in payload:
            status = issue_status_from_payload(item, issue_id)
            if status is not None:
                return status
    return None


def forbidden_worktree_command(command_text: str) -> str | None:
    lowered = command_text.lower()
    for fragment in FORBIDDEN_WORKTREE_COMMAND_FRAGMENTS:
        if fragment in lowered:
            return fragment
    return None


class EventLog:
    def __init__(self, correlation_id: str, clock: Clock) -> None:
        self.correlation_id = correlation_id
        self.clock = clock
        self.sequence = 0
        self.events: list[dict[str, Any]] = []
        self.redaction = RedactionStats()

    def emit(
        self,
        *,
        kind: str,
        phase: str,
        status: str,
        details: dict[str, Any] | None = None,
        agent_names: list[str] | None = None,
        bead_ids: list[str] | None = None,
        reservation_ids: list[int] | None = None,
        rch_admission: dict[str, Any] | None = None,
    ) -> None:
        redacted_details, detail_stats = redact_json(details or {}, "details")
        redacted_rch, rch_stats = redact_json(rch_admission, "rch_admission")
        self.redaction.merge(detail_stats)
        self.redaction.merge(rch_stats)
        event = {
            "schema": EVENT_SCHEMA,
            "sequence": self.sequence,
            "timestamp_ms": self.clock.timestamp_ms(),
            "kind": kind,
            "phase": phase,
            "status": status,
            "correlation_id": self.correlation_id,
            "agent_names": sorted(agent_names or []),
            "bead_ids": sorted(bead_ids or []),
            "reservation_ids": sorted(reservation_ids or []),
            "rch_admission": redacted_rch,
            "details": redacted_details,
            "redaction": detail_stats.to_json(),
        }
        self.events.append(event)
        self.sequence += 1

    def command(
        self,
        phase: str,
        result: CommandResult,
        status: str,
        parsed: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "command": result.command_text(),
            "cwd": result.cwd,
            "exit_code": result.exit_code,
            "elapsed_ms": result.elapsed_ms,
            "stdout_tail": result.stdout[-1200:],
            "stderr_tail": result.stderr[-1200:],
        }
        if parsed is not None:
            details["parsed"] = parsed
        self.emit(kind="command", phase=phase, status=status, details=details)


class CommandRunner:
    def __init__(self, event_log: EventLog, timeout_seconds: int) -> None:
        self.event_log = event_log
        self.timeout_seconds = timeout_seconds
        self.results: list[CommandResult] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        phase: str,
        env: dict[str, str] | None = None,
        allow_failure: bool = False,
    ) -> CommandResult:
        start = time.monotonic_ns()
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        elapsed_ms = max(0, (time.monotonic_ns() - start) // 1_000_000)
        result = CommandResult(
            argv=argv,
            cwd=str(cwd),
            exit_code=completed.returncode,
            elapsed_ms=elapsed_ms,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        self.results.append(result)
        status = "ok" if result.exit_code == 0 else "failed_expected" if allow_failure else "failed"
        self.event_log.command(phase, result, status)
        if result.exit_code != 0 and not allow_failure:
            raise HarnessError(
                f"command failed in {phase}: {result.command_text()}\n{result.stderr.strip()}"
            )
        return result


class McpClient:
    def __init__(self, url: str, timeout_seconds: int, event_log: EventLog) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.event_log = event_log
        self.request_id = 0

    def call(self, name: str, arguments: dict[str, Any], *, phase: str) -> dict[str, Any]:
        self.request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        start = time.monotonic_ns()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as exc:
            elapsed_ms = max(0, (time.monotonic_ns() - start) // 1_000_000)
            self.event_log.emit(
                kind="agent_mail",
                phase=phase,
                status="failed",
                details={"tool": name, "elapsed_ms": elapsed_ms, "error": str(exc)},
            )
            raise HarnessError(f"Agent Mail MCP call failed for {name}: {exc}") from exc
        elapsed_ms = max(0, (time.monotonic_ns() - start) // 1_000_000)
        response_payload = json.loads(raw)
        if "error" in response_payload:
            self.event_log.emit(
                kind="agent_mail",
                phase=phase,
                status="failed",
                details={
                    "tool": name,
                    "elapsed_ms": elapsed_ms,
                    "response": response_payload,
                },
            )
            raise HarnessError(f"Agent Mail MCP tool returned error for {name}")
        parsed = self._parse_tool_result(response_payload)
        self.event_log.emit(
            kind="agent_mail",
            phase=phase,
            status="ok",
            details={"tool": name, "elapsed_ms": elapsed_ms, "response": parsed},
        )
        return parsed

    @staticmethod
    def _parse_tool_result(payload: dict[str, Any]) -> dict[str, Any]:
        content = payload.get("result", {}).get("content", [])
        if not content:
            return {}
        text = content[0].get("text", "")
        if not text:
            return {}
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}


@dataclass
class HarnessConfig:
    repo_root: Path
    out_dir: Path | None
    correlation_id: str
    mcp_url: str
    command_timeout_seconds: int
    stale_after_seconds: int
    fixed_start_ms: int | None = None


class SwarmSmokeHarness:
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        self.event_log = EventLog(config.correlation_id, Clock(config.fixed_start_ms))
        self.runner = CommandRunner(self.event_log, config.command_timeout_seconds)
        self.mcp = McpClient(config.mcp_url, config.command_timeout_seconds, self.event_log)
        self.agent_names = list(DEFAULT_AGENTS)
        self.bead_ids: list[str] = []
        self.reservation_ids: list[int] = []
        self.rch_admissions: list[dict[str, Any]] = []
        self.scenarios: dict[str, dict[str, Any]] = {}
        self.workspace = self._create_workspace()
        self.project_dir = self.workspace / "project"
        self.artifact_dir = config.out_dir or self.workspace / "artifacts"

    def _create_workspace(self) -> Path:
        tmp_parent = Path(os.environ.get("TMPDIR") or "/data/tmp")
        tmp_parent.mkdir(parents=True, exist_ok=True)
        prefix = f"pi_swarm_smoke_{self.config.correlation_id}_"
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(tmp_parent)))

    def prepare_project(self) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=False)
        (self.project_dir / "src").mkdir()
        (self.project_dir / "src" / "demo.rs").write_text(
            "fn main() {\n    println!(\"swarm smoke fixture\");\n}\n",
            encoding="utf-8",
        )
        (self.project_dir / "README.md").write_text(
            "# Swarm smoke fixture\n\nGenerated by run_swarm_smoke_harness.py.\n",
            encoding="utf-8",
        )
        self.event_log.emit(
            kind="fixture",
            phase="prepare_project",
            status="ok",
            details={
                "temp_project": str(self.project_dir),
                "note": "fixture is intentionally retained; no cleanup/deletion is performed",
            },
        )

    def run_beads_flow(self) -> None:
        br = shutil.which("br")
        if br is None:
            raise HarnessError("br executable not found")
        self.runner.run([br, "init", "--prefix", "smoke"], cwd=self.project_dir, phase="beads.init")
        created = self.runner.run(
            [
                br,
                "create",
                "--title",
                "Swarm smoke sample bead",
                "--type",
                "task",
                "--priority",
                "2",
                "--description",
                "Temp project bead for no-mock swarm smoke harness.",
                "--json",
            ],
            cwd=self.project_dir,
            phase="beads.create",
        )
        created_payload = parse_json_document(created.stdout)
        bead_id = created_payload["id"]
        self.bead_ids.append(bead_id)
        self.event_log.emit(
            kind="bead_status",
            phase="beads.create",
            status="ok",
            bead_ids=[bead_id],
            details={"created": created_payload},
        )
        self.runner.run(
            [
                br,
                "update",
                bead_id,
                "--status",
                "in_progress",
                "--claim",
                "--actor",
                self.agent_names[0],
                "--json",
            ],
            cwd=self.project_dir,
            phase="beads.claim",
        )
        claimed = self.runner.run(
            [br, "show", bead_id, "--json"],
            cwd=self.project_dir,
            phase="beads.verify_claim",
        )
        claimed_status = issue_status_from_payload(parse_json_document(claimed.stdout), bead_id)
        self.runner.run(
            [br, "close", bead_id, "--reason", "Completed by swarm smoke harness", "--json"],
            cwd=self.project_dir,
            phase="beads.close",
        )
        closed = self.runner.run(
            [br, "show", bead_id, "--json"],
            cwd=self.project_dir,
            phase="beads.verify_close",
        )
        closed_status = issue_status_from_payload(parse_json_document(closed.stdout), bead_id)
        transition_ok = claimed_status == "in_progress" and closed_status == "closed"
        self.event_log.emit(
            kind="bead_status",
            phase="beads.transition_verify",
            status="ok" if transition_ok else "failed",
            bead_ids=[bead_id],
            details={
                "claimed_status": claimed_status,
                "closed_status": closed_status,
            },
        )

        stale_created = self.runner.run(
            [
                br,
                "create",
                "--title",
                "Stale in-progress smoke bead",
                "--type",
                "task",
                "--priority",
                "2",
                "--description",
                "Temp stale-bead fixture for no-mock swarm smoke harness.",
                "--json",
            ],
            cwd=self.project_dir,
            phase="beads.stale_create",
        )
        stale_payload = parse_json_document(stale_created.stdout)
        stale_id = stale_payload["id"]
        self.bead_ids.append(stale_id)
        self.runner.run(
            [
                br,
                "update",
                stale_id,
                "--status",
                "in_progress",
                "--claim",
                "--actor",
                self.agent_names[1],
                "--json",
            ],
            cwd=self.project_dir,
            phase="beads.stale_claim",
        )
        in_progress = self.runner.run(
            [br, "list", "--status=in_progress", "--json"],
            cwd=self.project_dir,
            phase="beads.stale_scan",
        )
        stale_candidates = self._detect_stale_beads(parse_json_document(in_progress.stdout))
        self.scenarios["stale_in_progress_bead"] = {
            "status": "pass" if stale_candidates else "fail",
            "stale_after_seconds": self.config.stale_after_seconds,
            "stale_bead_ids": [item["id"] for item in stale_candidates],
        }
        self.event_log.emit(
            kind="bead_status",
            phase="beads.stale_scan",
            status="ok" if stale_candidates else "failed",
            bead_ids=[item["id"] for item in stale_candidates],
            details={"stale_candidates": stale_candidates},
        )
        self.runner.run([br, "sync", "--flush-only"], cwd=self.project_dir, phase="beads.sync")
        self.scenarios["healthy_beads_flow"] = {
            "status": "pass" if transition_ok else "fail",
            "created_claimed_closed_bead": bead_id,
            "claimed_status": claimed_status,
            "closed_status": closed_status,
            "temp_project": str(self.project_dir),
        }

    def _detect_stale_beads(self, payload: Any) -> list[dict[str, Any]]:
        issues = payload.get("issues", []) if isinstance(payload, dict) else payload
        if not isinstance(issues, list):
            return []
        now = datetime.now(timezone.utc)
        stale: list[dict[str, Any]] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            updated_at = str(issue.get("updated_at") or "")
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except ValueError:
                age_seconds = None
            else:
                age_seconds = max(0, int((now - updated).total_seconds()))
            if age_seconds is None or age_seconds >= self.config.stale_after_seconds:
                stale.append(
                    {
                        "id": issue.get("id"),
                        "status": issue.get("status"),
                        "updated_at": updated_at,
                        "age_seconds": age_seconds,
                    }
                )
        return stale

    def run_agent_mail_flow(self) -> None:
        self.mcp.call("health_check", {}, phase="agent_mail.health")
        self.mcp.call("ensure_project", {"human_key": str(self.project_dir)}, phase="agent_mail.project")
        for name in self.agent_names:
            self.mcp.call(
                "register_agent",
                {
                    "project_key": str(self.project_dir),
                    "program": "swarm-smoke-harness",
                    "model": "fixture",
                    "name": name,
                    "task_description": f"{self.config.correlation_id} smoke harness agent",
                },
                phase=f"agent_mail.register.{name}",
            )
        message = self.mcp.call(
            "send_message",
            {
                "project_key": str(self.project_dir),
                "sender_name": self.agent_names[0],
                "to": [self.agent_names[1], self.agent_names[2]],
                "subject": f"[{self.config.correlation_id}] Smoke harness start",
                "body_md": "Smoke harness fixture message. No prompts or secrets are included.",
                "thread_id": self.config.correlation_id,
                "importance": "normal",
            },
            phase="agent_mail.send",
        )
        reservation = self.mcp.call(
            "file_reservation_paths",
            {
                "project_key": str(self.project_dir),
                "agent_name": self.agent_names[0],
                "paths": ["src/demo.rs"],
                "ttl_seconds": 600,
                "exclusive": True,
                "reason": self.config.correlation_id,
            },
            phase="agent_mail.reserve",
        )
        granted = reservation.get("granted", [])
        self.reservation_ids.extend(item["id"] for item in granted if "id" in item)
        conflict = self.mcp.call(
            "file_reservation_paths",
            {
                "project_key": str(self.project_dir),
                "agent_name": self.agent_names[1],
                "paths": ["src/demo.rs"],
                "ttl_seconds": 600,
                "exclusive": True,
                "reason": f"{self.config.correlation_id}-conflict",
            },
            phase="agent_mail.conflict",
        )
        self.mcp.call(
            "release_file_reservations",
            {
                "project_key": str(self.project_dir),
                "agent_name": self.agent_names[0],
                "paths": ["src/demo.rs"],
            },
            phase="agent_mail.release",
        )
        conflict_observed = bool(conflict.get("conflicts"))
        self.scenarios["agent_mail_healthy_flow"] = {
            "status": "pass",
            "message_count": message.get("count"),
            "agent_names": self.agent_names,
        }
        self.scenarios["reservation_conflict"] = {
            "status": "pass" if conflict_observed else "fail",
            "conflict_observed": conflict_observed,
            "reservation_ids": self.reservation_ids,
        }
        self.event_log.emit(
            kind="file_reservation",
            phase="agent_mail.summary",
            status="ok" if conflict_observed else "failed",
            agent_names=self.agent_names,
            reservation_ids=self.reservation_ids,
            details={"reservation": reservation, "conflict": conflict},
        )

    def run_dirty_worktree_flow(self) -> None:
        git = shutil.which("git")
        if git is None:
            raise HarnessError("git executable not found")

        self.runner.run([git, "init", "-b", "main"], cwd=self.project_dir, phase="dirty.git_init")
        self.runner.run(
            [git, "config", "user.email", "swarm-smoke@example.invalid"],
            cwd=self.project_dir,
            phase="dirty.git_config_email",
        )
        self.runner.run(
            [git, "config", "user.name", "Swarm Smoke Harness"],
            cwd=self.project_dir,
            phase="dirty.git_config_name",
        )
        self.runner.run(
            [git, "add", "README.md", "src/demo.rs", ".beads"],
            cwd=self.project_dir,
            phase="dirty.git_add_baseline",
        )
        self.runner.run(
            [git, "commit", "-m", "seed swarm smoke fixture"],
            cwd=self.project_dir,
            phase="dirty.git_commit_baseline",
        )

        unrelated_dir = self.project_dir / "unrelated"
        unrelated_dir.mkdir()
        unrelated_note = unrelated_dir / "agent-notes.txt"
        unrelated_note.write_text(
            "unrelated dirty fixture: preserve me\n",
            encoding="utf-8",
        )
        readme = self.project_dir / "README.md"
        with readme.open("a", encoding="utf-8") as handle:
            handle.write("\nDirty worktree fixture: preserve this tracked edit.\n")
        protected_files = {
            str(readme.relative_to(self.project_dir)): file_sha256(readme),
            str(unrelated_note.relative_to(self.project_dir)): file_sha256(unrelated_note),
            "src/demo.rs": file_sha256(self.project_dir / "src" / "demo.rs"),
        }
        before_status = self.runner.run(
            [git, "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=self.project_dir,
            phase="dirty.status_before_agents",
        )

        agent_results: list[dict[str, Any]] = []
        agent_errors: list[str] = []
        agent_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def simulate_agent(agent_name: str, assigned_file: Path) -> None:
            try:
                barrier.wait(timeout=self.config.command_timeout_seconds)
                assigned_file.parent.mkdir(parents=True, exist_ok=True)
                assigned_file.write_text(
                    f"agent={agent_name}\ncorrelation_id={self.config.correlation_id}\n",
                    encoding="utf-8",
                )
                payload = {
                    "agent_name": agent_name,
                    "assigned_file": str(assigned_file.relative_to(self.project_dir)),
                    "sha256": file_sha256(assigned_file),
                }
                with agent_lock:
                    agent_results.append(payload)
            except (OSError, RuntimeError, threading.BrokenBarrierError) as exc:
                with agent_lock:
                    agent_errors.append(f"{agent_name}: {exc}")

        threads = [
            threading.Thread(
                target=simulate_agent,
                args=(agent_name, self.project_dir / "agent_work" / f"{agent_name}.txt"),
                name=f"swarm-smoke-{agent_name}",
            )
            for agent_name in self.agent_names[:2]
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=self.config.command_timeout_seconds)
        alive_threads = [thread.name for thread in threads if thread.is_alive()]

        after_status = self.runner.run(
            [git, "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=self.project_dir,
            phase="dirty.status_after_agents",
        )
        missing_files: list[str] = []
        mutated_files: list[str] = []
        for relative_path, expected_hash in protected_files.items():
            path = self.project_dir / relative_path
            if not path.exists():
                missing_files.append(relative_path)
                continue
            if file_sha256(path) != expected_hash:
                mutated_files.append(relative_path)
        forbidden_commands = [
            {
                "command": result.command_text(),
                "forbidden_fragment": fragment,
            }
            for result in self.runner.results
            if (fragment := forbidden_worktree_command(result.command_text())) is not None
        ]
        status_lines = after_status.stdout.splitlines()
        dirty_readme_preserved = any(line.endswith("README.md") for line in status_lines)
        dirty_untracked_preserved = any(line.endswith("unrelated/agent-notes.txt") for line in status_lines)
        dirty_ok = (
            not missing_files
            and not mutated_files
            and not forbidden_commands
            and not agent_errors
            and not alive_threads
            and dirty_readme_preserved
            and dirty_untracked_preserved
            and len(agent_results) == 2
        )
        details = {
            "status_before": before_status.stdout.splitlines(),
            "status_after": status_lines,
            "protected_files": protected_files,
            "missing_files": missing_files,
            "mutated_files": mutated_files,
            "forbidden_commands": forbidden_commands,
            "agent_results": sorted(agent_results, key=lambda item: item["agent_name"]),
            "agent_errors": agent_errors,
            "alive_threads": alive_threads,
            "dirty_readme_preserved": dirty_readme_preserved,
            "dirty_untracked_preserved": dirty_untracked_preserved,
        }
        self.scenarios["dirty_worktree_preserved"] = {
            "status": "pass" if dirty_ok else "fail",
            **details,
        }
        self.scenarios["concurrent_simulated_agents"] = {
            "status": "pass" if len(agent_results) == 2 and not agent_errors and not alive_threads else "fail",
            "agent_results": sorted(agent_results, key=lambda item: item["agent_name"]),
            "agent_errors": agent_errors,
            "alive_threads": alive_threads,
        }
        self.scenarios["unsafe_worktree_command_guard"] = {
            "status": "pass" if not forbidden_commands else "fail",
            "forbidden_commands": forbidden_commands,
            "forbidden_fragments": list(FORBIDDEN_WORKTREE_COMMAND_FRAGMENTS),
        }
        self.event_log.emit(
            kind="dirty_worktree",
            phase="dirty.verify_preservation",
            status="ok" if dirty_ok else "failed",
            agent_names=self.agent_names[:2],
            details=details,
        )

    def run_rch_admission_flow(self) -> None:
        script = self.config.repo_root / "scripts" / "cargo_headroom.sh"
        if not script.exists():
            raise HarnessError(f"missing cargo headroom script: {script}")
        rch_target = self.workspace / "cargo-target"
        rch_tmp = self.workspace / "cargo-tmp"
        base_args = [
            str(script),
            "--runner",
            "auto",
            "--admit-only",
            "--target-dir",
            str(rch_target),
            "--tmpdir",
            str(rch_tmp),
            "--min-free-mb",
            "1",
        ]
        admitted = self.runner.run(
            base_args + ["check", "--lib"],
            cwd=self.config.repo_root,
            phase="rch.admission",
            allow_failure=True,
        )
        admitted_payload = parse_json_line(admitted.stdout)
        self.rch_admissions.append(admitted_payload)
        self.event_log.emit(
            kind="rch_job",
            phase="rch.admission",
            status="ok" if admitted_payload.get("decision") == "allow" else "degraded",
            rch_admission=admitted_payload,
            details={"exit_code": admitted.exit_code, "elapsed_ms": admitted.elapsed_ms},
        )

        degraded_env = os.environ.copy()
        degraded_env["PATH"] = "/usr/bin:/bin"
        degraded = self.runner.run(
            base_args + ["check", "--all-targets"],
            cwd=self.config.repo_root,
            phase="rch.degraded",
            env=degraded_env,
            allow_failure=True,
        )
        degraded_payload = parse_json_line(degraded.stdout)
        self.rch_admissions.append(degraded_payload)
        degraded_observed = degraded_payload.get("decision") in {"backoff", "degraded"}
        self.event_log.emit(
            kind="rch_job",
            phase="rch.degraded",
            status="ok" if degraded_observed else "failed",
            rch_admission=degraded_payload,
            details={"exit_code": degraded.exit_code, "elapsed_ms": degraded.elapsed_ms},
        )
        self.scenarios["rch_admission"] = {
            "status": "pass",
            "decision": admitted_payload.get("decision"),
            "reason": admitted_payload.get("reason"),
            "exit_code": admitted.exit_code,
        }
        self.scenarios["rch_unavailable_or_degraded"] = {
            "status": "pass" if degraded_observed else "fail",
            "decision": degraded_payload.get("decision"),
            "reason": degraded_payload.get("reason"),
            "exit_code": degraded.exit_code,
        }

    def run(self) -> dict[str, Any]:
        self.prepare_project()
        self.run_beads_flow()
        self.run_dirty_worktree_flow()
        self.run_agent_mail_flow()
        self.run_rch_admission_flow()
        return self.write_artifacts()

    def write_artifacts(self) -> dict[str, Any]:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        events_path = self.artifact_dir / "events.jsonl"
        summary_path = self.artifact_dir / "summary.json"
        existing = [str(path) for path in (events_path, summary_path) if path.exists()]
        if existing:
            raise HarnessError(
                "refusing to overwrite existing smoke-harness artifacts: "
                + ", ".join(existing)
            )
        with events_path.open("w", encoding="utf-8") as handle:
            for event in self.event_log.events:
                handle.write(json_dumps(event))
                handle.write("\n")
        artifact_manifest = [artifact_manifest_entry("events_jsonl", events_path)]
        failed_scenarios = [
            name for name, scenario in self.scenarios.items() if scenario.get("status") != "pass"
        ]
        summary = {
            "schema": HARNESS_SCHEMA,
            "correlation_id": self.config.correlation_id,
            "generated_at": utc_now_iso(),
            "status": "pass" if not failed_scenarios else "fail",
            "temp_project": str(self.project_dir),
            "workspace": str(self.workspace),
            "artifacts": {
                "events_jsonl": str(events_path),
                "summary_json": str(summary_path),
            },
            "artifact_manifest": artifact_manifest,
            "agent_names": self.agent_names,
            "bead_ids": self.bead_ids,
            "reservation_ids": self.reservation_ids,
            "rch_admission_decisions": self.rch_admissions,
            "command_timings": [
                {
                    "command": result.command_text(),
                    "cwd": result.cwd,
                    "exit_code": result.exit_code,
                    "elapsed_ms": result.elapsed_ms,
                }
                for result in self.runner.results
            ],
            "scenarios": self.scenarios,
            "failed_scenarios": failed_scenarios,
            "redaction_summary": self.event_log.redaction.to_json(),
        }
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return summary


def assert_condition(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_self_test(args: argparse.Namespace) -> int:
    config = HarnessConfig(
        repo_root=args.repo_root.resolve(),
        out_dir=args.out_dir,
        correlation_id="selftest-swarm-smoke",
        mcp_url=args.mcp_url,
        command_timeout_seconds=args.command_timeout_seconds,
        stale_after_seconds=0,
        fixed_start_ms=1778286000000,
    )
    try:
        summary = SwarmSmokeHarness(config).run()
        assert_condition(summary["status"] == "pass", "summary status should pass")
        assert_condition(len(summary["agent_names"]) == 3, "three Agent Mail identities expected")
        assert_condition(len(summary["bead_ids"]) >= 2, "created and stale bead IDs expected")
        assert_condition(
            bool(summary["reservation_ids"]),
            "file reservation IDs should be captured",
        )
        assert_condition(
            summary["scenarios"]["reservation_conflict"]["conflict_observed"],
            "reservation conflict should be observed",
        )
        assert_condition(
            bool(summary["scenarios"]["stale_in_progress_bead"]["stale_bead_ids"]),
            "stale in-progress bead should be detected",
        )
        assert_condition(
            summary["scenarios"]["healthy_beads_flow"]["claimed_status"] == "in_progress",
            "claimed bead status transition should be verified",
        )
        assert_condition(
            summary["scenarios"]["healthy_beads_flow"]["closed_status"] == "closed",
            "closed bead status transition should be verified",
        )
        assert_condition(
            summary["scenarios"]["dirty_worktree_preserved"]["status"] == "pass",
            "unrelated dirty worktree files should be preserved",
        )
        assert_condition(
            summary["scenarios"]["concurrent_simulated_agents"]["status"] == "pass",
            "concurrent simulated agents should complete assigned writes",
        )
        assert_condition(
            summary["scenarios"]["unsafe_worktree_command_guard"]["status"] == "pass",
            "stash/reset/revert-style worktree commands should not be used",
        )
        assert_condition(
            summary["scenarios"]["rch_unavailable_or_degraded"]["decision"]
            in {"backoff", "degraded"},
            "RCH unavailable/degraded admission should be recorded",
        )
        assert_condition(
            all("elapsed_ms" in item for item in summary["command_timings"]),
            "command timings should include elapsed_ms",
        )
        assert_condition(
            summary["redaction_summary"]["redacted_count"] > 0,
            "Agent Mail registration tokens should be redacted from artifacts",
        )
        for artifact_path in summary["artifacts"].values():
            assert_condition(Path(artifact_path).exists(), f"missing artifact: {artifact_path}")
        manifest = summary.get("artifact_manifest")
        assert_condition(isinstance(manifest, list) and manifest, "artifact manifest expected")
        for entry in manifest:
            assert_condition(Path(entry["path"]).exists(), f"missing manifest artifact: {entry}")
            assert_condition(entry["size_bytes"] > 0, f"manifest artifact should be non-empty: {entry}")
            assert_condition(len(entry["sha256"]) == 64, f"manifest artifact should carry sha256: {entry}")
        artifact_dir = Path(summary["artifacts"]["summary_json"]).parent
        overwrite_probe = SwarmSmokeHarness(
            HarnessConfig(
                repo_root=args.repo_root.resolve(),
                out_dir=artifact_dir,
                correlation_id="selftest-swarm-smoke-overwrite-probe",
                mcp_url=args.mcp_url,
                command_timeout_seconds=args.command_timeout_seconds,
                stale_after_seconds=0,
                fixed_start_ms=1778286001000,
            )
        )
        try:
            overwrite_probe.write_artifacts()
        except HarnessError as exc:
            message = str(exc)
            assert_condition(
                "events.jsonl" in message and "summary.json" in message,
                "overwrite guard should name both existing artifact files",
            )
        else:
            raise AssertionError("existing smoke-harness artifacts should be refused")
    except (AssertionError, HarnessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"SELF-TEST FAIL: {exc}")
        return 2
    print("SELF-TEST PASS")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root that owns scripts/cargo_headroom.sh",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="artifact directory to write; default is under the retained temp workspace",
    )
    parser.add_argument(
        "--correlation-id",
        default=f"swarm-smoke-{int(time.time())}",
        help="correlation ID included in every event and Agent Mail thread",
    )
    parser.add_argument(
        "--mcp-url",
        default=DEFAULT_MCP_URL,
        help="Agent Mail MCP HTTP endpoint",
    )
    parser.add_argument(
        "--command-timeout-seconds",
        type=int,
        default=45,
        help="timeout for each external command or MCP call",
    )
    parser.add_argument(
        "--stale-after-seconds",
        type=int,
        default=0,
        help="age threshold for reporting in-progress beads as stale in the temp fixture",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print only summary JSON",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run the full temp-project smoke harness with assertions",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command_timeout_seconds <= 0:
        print("ERROR: --command-timeout-seconds must be positive", file=sys.stderr)
        return 2
    if args.stale_after_seconds < 0:
        print("ERROR: --stale-after-seconds must be non-negative", file=sys.stderr)
        return 2
    if args.self_test:
        return run_self_test(args)

    config = HarnessConfig(
        repo_root=args.repo_root.resolve(),
        out_dir=args.out_dir,
        correlation_id=args.correlation_id,
        mcp_url=args.mcp_url,
        command_timeout_seconds=args.command_timeout_seconds,
        stale_after_seconds=args.stale_after_seconds,
    )
    try:
        summary = SwarmSmokeHarness(config).run()
    except (HarnessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"status: {summary['status']}")
        print(f"correlation_id: {summary['correlation_id']}")
        print(f"temp_project: {summary['temp_project']}")
        print(f"events: {summary['artifacts']['events_jsonl']}")
        print(f"summary: {summary['artifacts']['summary_json']}")
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
