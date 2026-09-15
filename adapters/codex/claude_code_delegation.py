#!/usr/bin/env python3
"""Codex stdio MCP adapter for durable Claude Code delegation."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, TextIO, Tuple, Union


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "running", "waiting"}
ALLOWED_MODELS = {"opus", "fable", "claude-fable-5", "claude-fable-5-1"}
DEFAULT_MODEL = "opus"
ALLOWED_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
ALLOWED_PERMISSION_MODES = {"default", "acceptEdits", "plan", "auto"}
MAX_TEXT_BYTES = 256_000
MAX_CAPTURE_BYTES = 8_000_000
MAX_REDACTED_LOG_BYTES = 32_000
CALLBACK_EVENT_NAME = "claude_delegation_completed"
CALLBACK_LEASE_SECONDS = 300
CALLBACK_MARKER_PREFIX = "[CLAUDE_DELEGATION_COMPLETION_EVENT:"
WORKER_IDENTITY = "claude-delegation-worker-v1"
QUEUED_RECOVERY_GRACE_SECONDS = 120


class ToolError(RuntimeError):
    """An error safe to return to an MCP client."""


class CallbackUnavailable(RuntimeError):
    """The exact originating Codex thread no longer accepts delivery."""


class CallbackTransientError(RuntimeError):
    """Completion delivery can be retried without changing its target."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def parse_allowed_roots(raw: Optional[str]) -> List[Path]:
    """Parse an explicit os.pathsep-delimited root list."""
    if raw is None or not raw.strip():
        raise ToolError("CLAUDE_DELEGATION_ALLOWED_ROOTS must name at least one explicit root")
    roots: List[Path] = []
    for item in raw.split(os.pathsep):
        if not item.strip():
            raise ToolError("CLAUDE_DELEGATION_ALLOWED_ROOTS contains an empty root")
        root = Path(item).expanduser().resolve()
        if not root.is_dir():
            raise ToolError("configured allowed root does not exist or is not a directory")
        roots.append(root)
    return roots


class SecretRedactor:
    """Remove common credentials and credential-bearing environment values."""

    _assignment = re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|authorization|cookie)"
        r"(\s*[:=]\s*)([^\s,;]+)"
    )
    _bearer = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
    _known_key = re.compile(r"\b(?:sk|sk-ant|xox[baprs]|gh[opusr])-[A-Za-z0-9_-]{8,}\b")
    _jwt = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
    _private_key = re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    )

    def __init__(self, environment: Optional[Dict[str, str]] = None):
        environment = dict(os.environ if environment is None else environment)
        secret_name = re.compile(r"(?i)(?:key|token|secret|password|passwd|authorization|cookie)")
        self._values = sorted(
            {value for name, value in environment.items() if secret_name.search(name) and len(value) >= 4},
            key=len,
            reverse=True,
        )

    def __call__(self, value: Any, limit: int = 2_000) -> str:
        text = str(value)
        for secret in self._values:
            text = text.replace(secret, "[REDACTED]")
        text = self._bearer.sub("Bearer [REDACTED]", text)
        text = self._assignment.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]", text)
        text = self._known_key.sub("[REDACTED]", text)
        text = self._jwt.sub("[REDACTED]", text)
        text = self._private_key.sub("[REDACTED PRIVATE KEY]", text)
        return text[-limit:]


def _json_contains(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_json_contains(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(_json_contains(item, needle) for item in value)
    return False


class CodexAppServerTransport:
    """Deliver a completion to one exact Codex thread through app-server."""

    def __init__(self, codex_bin: str, redactor: SecretRedactor, timeout: int = 30):
        self.codex_bin = codex_bin
        self.redact = redactor
        self.timeout = timeout

    @staticmethod
    def _marker_seen(rollout_path: Optional[str], marker: str) -> bool:
        if not rollout_path:
            return False
        path = Path(rollout_path).expanduser().resolve()
        sessions_root = (Path.home() / ".codex" / "sessions").resolve()
        if not path.is_file() or not _is_within(path, sessions_root):
            return False
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                return any(marker in line for line in stream)
        except OSError:
            return False

    def _start_daemon(self) -> None:
        executable = shutil.which(self.codex_bin)
        if not executable:
            raise CallbackTransientError("Codex CLI was not found")
        try:
            result = subprocess.run(
                [executable, "app-server", "daemon", "start"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CallbackTransientError("Codex app-server daemon could not start") from exc
        if result.returncode != 0:
            raise CallbackTransientError("Codex app-server daemon could not start")

    def __call__(self, event: Dict[str, Any]) -> Dict[str, Any]:
        self._start_daemon()
        executable = shutil.which(self.codex_bin)
        assert executable is not None
        try:
            process = subprocess.Popen(
                [executable, "app-server", "proxy"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                close_fds=True,
                shell=False,
            )
        except OSError as exc:
            raise CallbackTransientError("Codex app-server proxy could not start") from exc
        assert process.stdin is not None and process.stdout is not None
        sequence = 0

        def rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal sequence
            sequence += 1
            request_id = sequence
            request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            try:
                process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except OSError as exc:
                raise CallbackTransientError("Codex app-server connection closed") from exc
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
                if not ready:
                    break
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response.get("error", {})
                    message = self.redact(error.get("message", "Codex app-server request failed"), 500)
                    lowered = message.lower()
                    if method in {"thread/read", "thread/resume"} and any(
                        text in lowered for text in ("not found", "unknown thread", "does not exist", "deleted")
                    ):
                        raise CallbackUnavailable(message)
                    raise CallbackTransientError(message)
                result = response.get("result", {})
                return result if isinstance(result, dict) else {}
            raise CallbackTransientError("Codex app-server request timed out")

        try:
            initialized = rpc(
                "initialize",
                {
                    "clientInfo": {
                        "name": "claude-delegation-callback",
                        "title": "Claude delegation callback",
                        "version": "1.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            if not initialized:
                raise CallbackTransientError("Codex app-server initialization failed")
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}) + "\n")
            process.stdin.flush()
            thread_id = str(event["callback_target"]["thread_id"])
            read_result = rpc("thread/read", {"threadId": thread_id, "includeTurns": True})
            thread = read_result.get("thread", {})
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                raise CallbackUnavailable("originating Codex thread is unavailable")
            rollout_path = thread.get("path") if isinstance(thread.get("path"), str) else None
            resume_marker = str(event["resume_marker"])
            event_marker = str(event["event_marker"])
            if self._marker_seen(rollout_path, resume_marker):
                return {"accepted": True, "already_present": True, "thread_id": thread_id}

            resumed = rpc("thread/resume", {"threadId": thread_id})
            resumed_thread = resumed.get("thread", {})
            if isinstance(resumed_thread, dict) and isinstance(resumed_thread.get("path"), str):
                rollout_path = resumed_thread["path"]
            if self._marker_seen(rollout_path, resume_marker):
                return {"accepted": True, "already_present": True, "thread_id": thread_id}
            context = (
                resume_marker
                + "\nA durable Claude delegation tool completion is present in thread history. "
                "Inspect the repository and relevant test output independently before any user-facing success claim. "
                "Treat failed or cancelled jobs truthfully. Never retry, downgrade, or fan out a failed Fable job. "
                "When final handling is verified and no follow-up is expected, call claude_code_close for this job."
            )
            started = rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [],
                    "toolOutput": {
                        "name": CALLBACK_EVENT_NAME,
                        "namespace": "claude_delegation",
                        "output": event_marker + "\n" + json.dumps(event["payload"], sort_keys=True),
                    },
                    "additionalContext": {
                        CALLBACK_EVENT_NAME: {"kind": "application", "value": context}
                    },
                },
            )
            turn = started.get("turn", {})
            return {
                "accepted": True,
                "already_present": False,
                "thread_id": thread_id,
                "turn_id": turn.get("id") if isinstance(turn, dict) else None,
            }
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.stdout.close()
            except OSError:
                pass
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass


class DelegationManager:
    def __init__(
        self,
        state_dir: Union[Path, str],
        allowed_roots: List[Union[Path, str]],
        claude_bin: str = "claude",
        default_max_turns: int = 40,
        default_effort: Optional[str] = None,
        redactor: Optional[SecretRedactor] = None,
        completion_transport: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        codex_bin: Optional[str] = None,
        prevent_idle_sleep: Optional[bool] = None,
        caffeinate_bin: str = "/usr/bin/caffeinate",
    ):
        if not allowed_roots:
            raise ToolError("at least one explicit allowed workdir root is required")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.allowed_roots = [Path(root).expanduser().resolve() for root in allowed_roots]
        for root in self.allowed_roots:
            if not root.is_dir():
                raise ToolError("configured allowed root does not exist or is not a directory")
            if _is_within(self.state_dir, root):
                raise ToolError("state directory must be outside delegated workdir roots")
        self.claude_bin = claude_bin
        self.default_max_turns = default_max_turns
        self.default_effort = default_effort
        self.redact = redactor or SecretRedactor()
        self.codex_bin = codex_bin or os.environ.get("CLAUDE_DELEGATION_CODEX_CLI", "codex")
        if prevent_idle_sleep is None:
            raw_sleep_setting = os.environ.get("CLAUDE_DELEGATION_PREVENT_IDLE_SLEEP", "1")
            prevent_idle_sleep = raw_sleep_setting.strip().lower() not in {"0", "false", "no", "off"}
        self.prevent_idle_sleep = prevent_idle_sleep
        self.caffeinate_bin = caffeinate_bin
        self.completion_transport = completion_transport or CodexAppServerTransport(
            self.codex_bin, self.redact
        )

    def _jobdir(self, job_id: str) -> Path:
        if not re.fullmatch(r"claude-[a-zA-Z0-9_-]{1,128}", job_id or ""):
            raise ToolError("invalid job_id")
        return self.state_dir / job_id

    def _ensure_state_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)

    @contextlib.contextmanager
    def _locked_job(self, job_id: str, create: bool = False) -> Iterator[Path]:
        jobdir = self._jobdir(job_id)
        if create:
            self._ensure_state_dir()
            jobdir.mkdir(mode=0o700)
        elif not jobdir.is_dir():
            raise ToolError("unknown job_id: " + job_id)
        lock_path = jobdir / "job.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield jobdir
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_unlocked(jobdir: Path, job_id: str) -> Dict[str, Any]:
        try:
            data = json.loads((jobdir / "job.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ToolError("unknown job_id: " + job_id) from exc
        except (json.JSONDecodeError, OSError) as exc:
            raise ToolError("job state is corrupt: " + job_id) from exc
        if not isinstance(data, dict) or data.get("job_id") != job_id:
            raise ToolError("job state is corrupt: " + job_id)
        return data

    @staticmethod
    def _write_unlocked(jobdir: Path, record: Dict[str, Any]) -> None:
        record["updated_at"] = utc_now()
        target = jobdir / "job.json"
        temporary = jobdir / ".job.json.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(str(temporary), str(target))

    def _read_job(self, job_id: str) -> Dict[str, Any]:
        with self._locked_job(job_id) as jobdir:
            return self._read_unlocked(jobdir, job_id)

    def _update_job(self, job_id: str, update: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        with self._locked_job(job_id) as jobdir:
            record = self._read_unlocked(jobdir, job_id)
            update(record)
            self._write_unlocked(jobdir, record)
            return record

    def _event(self, job_id: str, event: str, **metadata: Any) -> None:
        """Persist metadata only. Claude output and prompts never enter logs."""
        jobdir = self._jobdir(job_id)
        allowed = {
            "exit_code",
            "stdout_sha256",
            "stderr_sha256",
            "kind",
            "event_id",
            "terminal_status",
            "delivery_status",
        }
        payload = {"at": utc_now(), "event": event}
        payload.update({key: metadata[key] for key in allowed if key in metadata})
        path = jobdir / "events.log"
        with path.open("a", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write(json.dumps(payload, sort_keys=True) + "\n")

    def _write_redacted_stream_logs(self, job_id: str, turn_number: int, stdout: str, stderr: str) -> None:
        """Persist bounded redacted output only; never persist the prompt or raw streams."""
        jobdir = self._jobdir(job_id)
        for kind, value in (("stdout", stdout), ("stderr", stderr)):
            path = jobdir / ("claude-turn-" + str(turn_number).zfill(4) + "." + kind + ".log")
            safe = self.redact(value, limit=MAX_REDACTED_LOG_BYTES)
            with path.open("w", encoding="utf-8") as stream:
                os.chmod(path, 0o600)
                stream.write(safe)
                if safe and not safe.endswith("\n"):
                    stream.write("\n")

    def _validate_workdir(self, workdir: str) -> Path:
        if not isinstance(workdir, str) or not workdir.strip():
            raise ToolError("workdir is required")
        try:
            path = Path(workdir).expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise ToolError("workdir is invalid") from exc
        if not path.is_dir():
            raise ToolError("workdir does not exist or is not a directory")
        if not any(_is_within(path, root) for root in self.allowed_roots):
            raise ToolError("workdir is outside CLAUDE_DELEGATION_ALLOWED_ROOTS")
        return path

    def _validate_runtime(self) -> str:
        executable = shutil.which(self.claude_bin)
        if not executable:
            raise ToolError("Claude CLI was not found")
        try:
            result = subprocess.run(
                [executable, "auth", "status"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError("Claude authentication check timed out") from exc
        except OSError as exc:
            raise ToolError("Claude authentication check could not run") from exc
        if result.returncode != 0:
            raise ToolError("Claude CLI is not authenticated")
        return str(Path(executable).resolve())

    @staticmethod
    def _validate_text(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ToolError(name + " is required")
        if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ToolError(name + " is too large")
        return value.strip()

    def _prompt(self, task: str, workdir: Path, followup: bool = False) -> str:
        label = "Follow-up requirement for the existing task" if followup else "Implementation task"
        return (
            label
            + ":\n"
            + task
            + "\n\nRepository/workdir: "
            + str(workdir)
            + "\n\nInspect the repository before changing files. Work only within the stated workdir. "
            "Use secure, minimal changes. Run relevant tests, lint, and/or build before finishing. "
            "Report changed files, validation commands and results, and remaining blockers. "
            "Do not commit, push, deploy, expose secrets, or alter unrelated files unless explicitly asked."
        )

    @staticmethod
    def _repo_snapshot(workdir: Path) -> Dict[str, Any]:
        try:
            head = subprocess.run(
                ["git", "-C", str(workdir), "rev-parse", "HEAD"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
                check=False,
                shell=False,
            )
            status = subprocess.run(
                ["git", "-C", str(workdir), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=20,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"kind": "unavailable", "head": None, "paths": {}}
        if head.returncode != 0 or status.returncode != 0:
            return {"kind": "non_git", "head": None, "paths": {}}
        paths: Dict[str, str] = {}
        entries = [item for item in (status.stdout or "").split("\0") if item]
        index = 0
        while index < len(entries):
            entry = entries[index]
            code = entry[:2]
            path = entry[3:] if len(entry) > 3 else ""
            if code.startswith(("R", "C")) and index + 1 < len(entries):
                index += 1
                path = entries[index]
            if path:
                candidate = workdir / path
                digest = "missing"
                try:
                    if candidate.is_file() and not candidate.is_symlink():
                        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
                except OSError:
                    digest = "unreadable"
                paths[path] = code + ":" + digest
            index += 1
        return {"kind": "git", "head": (head.stdout or "").strip(), "paths": paths}

    @staticmethod
    def _changed_files(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
        if before.get("kind") != "git" or after.get("kind") != "git":
            return []
        first = before.get("paths", {}) if isinstance(before.get("paths"), dict) else {}
        second = after.get("paths", {}) if isinstance(after.get("paths"), dict) else {}
        names = sorted(name for name in set(first) | set(second) if first.get(name) != second.get(name))
        return names[:200]

    @staticmethod
    def _event_id(job_id: str) -> str:
        digest = hashlib.sha256((CALLBACK_EVENT_NAME + "\0" + job_id).encode("utf-8")).hexdigest()
        return "cde-" + digest

    def _completion_payload(self, record: Dict[str, Any]) -> Dict[str, Any]:
        jobdir = self._jobdir(record["job_id"])
        after = self._repo_snapshot(Path(record["workdir"]))
        return {
            "event": CALLBACK_EVENT_NAME,
            "event_id": self._event_id(record["job_id"]),
            "job_id": record["job_id"],
            "terminal_status": record["status"],
            "workdir": record["workdir"],
            "model": record["model"],
            "changed_files_or_components": self._changed_files(record.get("repo_snapshot", {}), after),
            "claude_claimed_validation": record.get("latest_validation"),
            "blocker": record.get("blocker"),
            "next_action": record.get("next_action"),
            "state_path": str(jobdir / "job.json"),
            "log_paths": [str(jobdir / "events.log")]
            + [str(path) for path in sorted(jobdir.glob("claude-turn-*.log"))],
        }

    def emit_completion(self, job_id: str, deliver: bool = True) -> Dict[str, Any]:
        emitted = False
        event_id = self._event_id(job_id)
        with self._locked_job(job_id) as jobdir:
            record = self._read_unlocked(jobdir, job_id)
            if record.get("status") not in TERMINAL_STATUSES:
                raise ToolError("completion event requires a terminal job")
            if record.get("sleep_guard_status") in {"pending", "armed"}:
                record["sleep_guard_status"] = "released"
            completion = record.get("completion_event")
            if not isinstance(completion, dict):
                payload = self._completion_payload(record)
                callback = record.get("callback")
                delivery_status = "pending" if isinstance(callback, dict) else "unavailable"
                completion = {
                    "event_name": CALLBACK_EVENT_NAME,
                    "event_id": event_id,
                    "idempotency_key": event_id,
                    "event_marker": CALLBACK_MARKER_PREFIX + event_id + "]",
                    "resume_marker": CALLBACK_MARKER_PREFIX + event_id + ":RESUME]",
                    "emitted_at": utc_now(),
                    "delivery_status": delivery_status,
                    "delivery_attempts": 0,
                    "delivery_lease_until": None,
                    "delivered_at": None,
                    "delivery_error": None,
                    "payload": payload,
                }
                record["completion_event"] = completion
                self._write_unlocked(jobdir, record)
                emitted = True
        if emitted:
            self._event(
                job_id,
                CALLBACK_EVENT_NAME,
                event_id=event_id,
                terminal_status=record["status"],
                delivery_status=completion["delivery_status"],
            )
        if deliver and completion.get("delivery_status") == "pending":
            return self.deliver_completion(job_id)
        return completion

    @staticmethod
    def _lease_expired(value: Any) -> bool:
        if not isinstance(value, str):
            return True
        try:
            return dt.datetime.fromisoformat(value) <= dt.datetime.now(dt.timezone.utc)
        except ValueError:
            return True

    def deliver_completion(self, job_id: str) -> Dict[str, Any]:
        with self._locked_job(job_id) as jobdir:
            record = self._read_unlocked(jobdir, job_id)
            completion = record.get("completion_event")
            callback = record.get("callback")
            if not isinstance(completion, dict):
                raise ToolError("completion event has not been emitted")
            event_id = str(completion.get("event_id", self._event_id(job_id)))
            status = completion.get("delivery_status")
            if status in {"delivered", "unavailable"}:
                return completion
            if status == "delivering" and not self._lease_expired(completion.get("delivery_lease_until")):
                return completion
            if not isinstance(callback, dict):
                completion["delivery_status"] = "unavailable"
                completion["delivery_error"] = "originating Codex callback metadata is unavailable"
                self._write_unlocked(jobdir, record)
                return completion
            completion["delivery_status"] = "delivering"
            completion["delivery_attempts"] = int(completion.get("delivery_attempts", 0)) + 1
            completion["delivery_lease_until"] = (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=CALLBACK_LEASE_SECONDS)
            ).isoformat()
            self._write_unlocked(jobdir, record)
            envelope = dict(completion)
            envelope["callback_target"] = callback["target"]
            envelope["origin"] = callback["origin"]
        try:
            outcome = self.completion_transport(envelope)
            delivery_status = "delivered"
            delivery_error = None
        except CallbackUnavailable:
            outcome = {}
            delivery_status = "unavailable"
            delivery_error = "originating Codex thread is unavailable or deleted"
        except CallbackTransientError as exc:
            outcome = {}
            delivery_status = "pending"
            delivery_error = self.redact(str(exc), 500)
        except Exception as exc:
            outcome = {}
            delivery_status = "pending"
            delivery_error = self.redact("callback transport error: " + type(exc).__name__, 500)

        def finish(record: Dict[str, Any]) -> None:
            current = record["completion_event"]
            current["delivery_status"] = delivery_status
            current["delivery_lease_until"] = None
            current["delivery_error"] = delivery_error
            if delivery_status == "delivered":
                current["delivered_at"] = utc_now()
                current["coordinator_turn_id"] = outcome.get("turn_id")
                current["already_present"] = bool(outcome.get("already_present"))

        final = self._update_job(job_id, finish)["completion_event"]
        self._event(
            job_id,
            "completion_delivery_" + delivery_status,
            event_id=event_id,
            terminal_status=final["payload"]["terminal_status"],
            delivery_status=delivery_status,
        )
        return final

    def recover_pending_completions(self) -> Dict[str, int]:
        self._ensure_state_dir()
        result = {
            "delivered": 0,
            "pending": 0,
            "unavailable": 0,
            "recovered_failed": 0,
            "active": 0,
            "skipped": 0,
        }
        for candidate in sorted(self.state_dir.glob("claude-*")):
            if not candidate.is_dir():
                continue
            try:
                job_id = candidate.name
                record = self._read_job(job_id)
                if record.get("status") in ACTIVE_STATUSES:
                    if self._worker_alive(job_id, record):
                        result["active"] += 1
                        continue
                    if record.get("pid") is None and self._record_age_seconds(record) < QUEUED_RECOVERY_GRACE_SECONDS:
                        result["active"] += 1
                        continue

                    terminalized = {"value": False}

                    def mark_orphaned(item: Dict[str, Any]) -> None:
                        if item.get("status") in ACTIVE_STATUSES and item.get("pid") == record.get("pid"):
                            item.update(
                                {
                                    "status": "failed",
                                    "phase": "worker unavailable after wake or restart",
                                    "blocker": "delegation worker process no longer exists after sleep, logout, or restart",
                                    "next_action": "inspect repository state before deciding whether to dispatch a new job",
                                    "sleep_guard_status": "released",
                                    "recovered_at": utc_now(),
                                }
                            )
                            terminalized["value"] = True

                    self._update_job(job_id, mark_orphaned)
                    if terminalized["value"]:
                        self._event(job_id, "worker_missing_after_wake")
                        self.emit_completion(job_id, deliver=False)
                        result["recovered_failed"] += 1
                    record = self._read_job(job_id)
                if record.get("status") not in TERMINAL_STATUSES:
                    result["skipped"] += 1
                    continue
                completion = record.get("completion_event")
                if not isinstance(completion, dict):
                    completion = self.emit_completion(job_id, deliver=False)
                if completion.get("delivery_status") in {"pending", "delivering"}:
                    completion = self.deliver_completion(job_id)
                delivery = str(completion.get("delivery_status", "skipped"))
                result[delivery if delivery in result else "skipped"] += 1
            except (ToolError, OSError):
                result["skipped"] += 1
        return result

    @staticmethod
    def _record_age_seconds(record: Dict[str, Any]) -> float:
        value = record.get("created_at")
        if not isinstance(value, str):
            return float("inf")
        try:
            created = dt.datetime.fromisoformat(value)
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt.timezone.utc)
            return max(0.0, (dt.datetime.now(dt.timezone.utc) - created).total_seconds())
        except ValueError:
            return float("inf")

    def _start_sleep_guard(self, worker_pid: int) -> Tuple[Optional[int], str]:
        """Prevent idle sleep while a worker lives; lid-close sleep still pauses safely."""
        if not self.prevent_idle_sleep:
            return None, "disabled"
        executable = Path(self.caffeinate_bin).expanduser()
        if not executable.is_file() or not os.access(str(executable), os.X_OK):
            return None, "unavailable"
        try:
            guard = subprocess.Popen(
                [str(executable), "-i", "-w", str(worker_pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                shell=False,
            )
        except OSError:
            return None, "unavailable"
        return guard.pid, "armed"

    @staticmethod
    def _command(
        executable: str,
        model: str,
        effort: Optional[str],
        max_turns: int,
        permission_mode: str,
        session_id: Optional[str] = None,
    ) -> List[str]:
        command = [
            executable,
            "-p",
            "--model",
            model,
            "--output-format",
            "json",
            "--max-turns",
            str(max_turns),
        ]
        if effort:
            command.extend(["--effort", effort])
        if permission_mode != "default":
            command.extend(["--permission-mode", permission_mode])
        if session_id:
            command.extend(["--resume", session_id])
        return command

    def dispatch(
        self,
        task: str,
        workdir: str,
        model: str = DEFAULT_MODEL,
        effort: Optional[str] = None,
        max_turns: Optional[int] = None,
        permission_mode: str = "default",
        dry_run: bool = False,
        callback_context: Optional[Dict[str, Any]] = None,
        restart_of_job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        task = self._validate_text("task", task)
        if model not in ALLOWED_MODELS:
            raise ToolError(
                "invalid model; allowed values are opus, fable, claude-fable-5, and claude-fable-5-1"
            )
        if effort is not None and effort not in ALLOWED_EFFORTS:
            raise ToolError("invalid effort")
        if permission_mode not in ALLOWED_PERMISSION_MODES:
            raise ToolError("invalid permission_mode")
        turns = self.default_max_turns if max_turns is None else max_turns
        if not isinstance(turns, int) or isinstance(turns, bool) or not 1 <= turns <= 200:
            raise ToolError("max_turns must be an integer from 1 to 200")
        resolved = self._validate_workdir(workdir)
        executable = self._validate_runtime()
        selected_effort = effort if effort is not None else self.default_effort
        command = self._command(executable, model, selected_effort, turns, permission_mode)
        if dry_run:
            return {
                "dry_run": True,
                "workdir": str(resolved),
                "model": model,
                "command": command,
                "prompt_transport": "stdin",
            }
        if not isinstance(callback_context, dict):
            raise ToolError("originating Codex thread metadata is required for durable completion delivery")
        thread_id = callback_context.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ToolError("originating Codex thread metadata is required for durable completion delivery")

        job_id = "claude-" + uuid.uuid4().hex
        created = utc_now()
        record: Dict[str, Any] = {
            "job_id": job_id,
            "status": "queued",
            "phase": "queued",
            "objective": "start the delegated Claude Code task",
            "task": task,
            "workdir": str(resolved),
            "allowed_roots": [str(root) for root in self.allowed_roots],
            "claude_executable": executable,
            "model": model,
            "effort": selected_effort,
            "max_turns": turns,
            "permission_mode": permission_mode,
            "messages": [],
            "initial_completed": False,
            "close_requested": False,
            "created_at": created,
            "updated_at": created,
            "pid": None,
            "worker_identity": WORKER_IDENTITY,
            "heartbeat_at": None,
            "sleep_guard_pid": None,
            "sleep_guard_status": "pending" if self.prevent_idle_sleep else "disabled",
            "latest_validation": None,
            "blocker": None,
            "next_action": "launch worker",
            "session_id": None,
            "exit_code": None,
            "turn_count": 0,
            "repo_snapshot": self._repo_snapshot(resolved),
            "callback": {
                "registered_at": created,
                "origin": {
                    "thread_id": thread_id,
                    "session_id": callback_context.get("session_id") or thread_id,
                    "turn_id": callback_context.get("turn_id"),
                    "coordinator_identity": callback_context.get("coordinator_identity", "coordinator"),
                    "mcp_call_id": callback_context.get("mcp_call_id"),
                },
                "target": {
                    "type": "codex_app_server_application_context",
                    "thread_id": thread_id,
                },
                "original_task_link": "job.task",
                "workdir": str(resolved),
            },
            "completion_event": None,
            "restart_of_job_id": restart_of_job_id,
            "restarted_by_job_id": None,
        }
        with self._locked_job(job_id, create=True) as jobdir:
            self._write_unlocked(jobdir, record)
        self._event(job_id, "queued")

        worker_command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            job_id,
            "--state-dir",
            str(self.state_dir),
            "--claude-bin",
            executable,
            "--codex-bin",
            self.codex_bin,
        ]
        for root in self.allowed_roots:
            worker_command.extend(["--allowed-root", str(root)])
        try:
            process = subprocess.Popen(
                worker_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                shell=False,
            )
        except OSError as exc:
            self._update_job(
                job_id,
                lambda item: item.update(
                    {
                        "status": "failed",
                        "phase": "worker launch failed",
                        "blocker": "delegation worker could not start",
                        "next_action": "check the configured Python runtime",
                    }
                ),
            )
            self._event(job_id, "worker_launch_failed")
            self.emit_completion(job_id)
            raise ToolError("delegation worker could not start") from exc

        sleep_guard_pid, sleep_guard_status = self._start_sleep_guard(process.pid)

        def mark_running(item: Dict[str, Any]) -> None:
            if item["status"] not in TERMINAL_STATUSES:
                item.update(
                    {
                        "pid": process.pid,
                        "status": "running",
                        "phase": "initializing",
                        "heartbeat_at": utc_now(),
                        "sleep_guard_pid": sleep_guard_pid,
                        "sleep_guard_status": sleep_guard_status,
                        "next_action": "Claude will inspect the workdir",
                    }
                )

        final = self._update_job(job_id, mark_running)
        self._event(job_id, "worker_started")
        return {
            "job_id": job_id,
            "status": final["status"],
            "workdir": str(resolved),
            "model": model,
            "callback_registered": True,
            "originating_thread_id": thread_id,
            "sleep_guard_status": final.get("sleep_guard_status"),
            "restart_of_job_id": restart_of_job_id,
        }

    @staticmethod
    def _alive(pid: Any) -> bool:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _worker_alive(self, job_id: str, record: Dict[str, Any]) -> bool:
        pid = record.get("pid")
        if not self._alive(pid):
            return False
        if record.get("worker_identity") != WORKER_IDENTITY:
            return True
        try:
            result = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "command="],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        if result.returncode != 0:
            return False
        command = result.stdout or ""
        return (
            str(Path(__file__).resolve()) in command
            and "--worker" in command
            and job_id in command
        )

    def status(self, job_id: str) -> Dict[str, Any]:
        terminalized = {"value": False}

        def reconcile(record: Dict[str, Any]) -> None:
            if record.get("status") in ACTIVE_STATUSES and not self._worker_alive(job_id, record):
                record.update(
                    {
                        "status": "failed",
                        "phase": "worker unavailable after wake or restart",
                        "blocker": "delegation worker process no longer exists after sleep, logout, or restart",
                        "next_action": "inspect repository state before deciding whether to dispatch a new job",
                        "sleep_guard_status": "released",
                        "recovered_at": utc_now(),
                    }
                )
                terminalized["value"] = True

        record = self._update_job(job_id, reconcile)
        if terminalized["value"]:
            self.emit_completion(job_id)
            record = self._read_job(job_id)
        messages = record.get("messages", [])
        result_keys = (
            "job_id",
            "status",
            "phase",
            "objective",
            "workdir",
            "model",
            "effort",
            "max_turns",
            "permission_mode",
            "pid",
            "created_at",
            "updated_at",
            "heartbeat_at",
            "sleep_guard_pid",
            "sleep_guard_status",
            "recovered_at",
            "latest_validation",
            "blocker",
            "next_action",
            "exit_code",
            "session_id",
            "close_requested",
            "restart_of_job_id",
            "restarted_by_job_id",
        )
        result = {key: record.get(key) for key in result_keys}
        completion = record.get("completion_event")
        if isinstance(completion, dict):
            result["terminal_result"] = completion.get("payload")
            result["callback_delivery"] = {
                "event_id": completion.get("event_id"),
                "status": completion.get("delivery_status"),
                "attempts": completion.get("delivery_attempts"),
            }
        result["queued_messages"] = sum(1 for message in messages if message.get("state") == "queued")
        result["inflight_messages"] = sum(1 for message in messages if message.get("state") == "inflight")
        return result

    def message(self, job_id: str, message: str) -> Dict[str, Any]:
        message = self._validate_text("message", message)

        def enqueue(record: Dict[str, Any]) -> None:
            if record.get("status") in TERMINAL_STATUSES:
                raise ToolError("cannot message terminal job (" + str(record.get("status")) + ")")
            if record.get("close_requested"):
                raise ToolError("cannot message a job after close was requested")
            record.setdefault("messages", []).append(
                {"id": uuid.uuid4().hex, "message": message, "queued_at": utc_now(), "state": "queued"}
            )
            record["next_action"] = "drain queued follow-up after the current Claude turn"

        record = self._update_job(job_id, enqueue)
        count = sum(1 for item in record["messages"] if item.get("state") == "queued")
        self._event(job_id, "message_queued")
        return {"job_id": job_id, "queued": True, "queued_messages": count}

    def close(self, job_id: str) -> Dict[str, Any]:
        def request_close(record: Dict[str, Any]) -> None:
            if record.get("status") not in TERMINAL_STATUSES:
                record["close_requested"] = True
                record["next_action"] = "finish the current Claude turn and close gracefully"
            else:
                record.setdefault("resources_released_at", utc_now())

        record = self._update_job(job_id, request_close)
        terminal = record["status"] in TERMINAL_STATUSES
        if not terminal:
            self._event(job_id, "close_requested")
        return {
            "job_id": job_id,
            "status": record["status"],
            "closed": terminal,
            "close_requested": bool(record.get("close_requested")) if not terminal else False,
            "resources_released": bool(record.get("resources_released_at")),
        }

    def list_active(self) -> Dict[str, Any]:
        """List active jobs without exposing task or message bodies."""
        self._ensure_state_dir()
        jobs: List[Dict[str, Any]] = []
        for candidate in sorted(self.state_dir.glob("claude-*")):
            if not candidate.is_dir():
                continue
            try:
                record = self._read_job(candidate.name)
                if record.get("status") not in ACTIVE_STATUSES:
                    continue
                status = self.status(candidate.name)
                if status.get("status") not in ACTIVE_STATUSES:
                    continue
                jobs.append(
                    {
                        key: status.get(key)
                        for key in (
                            "job_id",
                            "status",
                            "model",
                            "workdir",
                            "created_at",
                            "phase",
                            "sleep_guard_status",
                        )
                    }
                )
            except (ToolError, OSError):
                continue
        jobs.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("job_id") or "")))
        return {"active_jobs": jobs, "count": len(jobs)}

    def restart(
        self,
        source_job_id: str,
        task: str,
        callback_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Replace one user-closed non-Fable job; never retry failures."""
        source = self._read_job(source_job_id)
        if source.get("status") not in TERMINAL_STATUSES:
            raise ToolError("source job is still active; use claude_code_message")
        if source.get("status") != "cancelled" or not source.get("close_requested"):
            raise ToolError("only a job closed by the user can be restarted")
        if str(source.get("model") or "").startswith(("fable", "claude-fable")):
            raise ToolError("Fable jobs cannot be restarted automatically")
        if source.get("restarted_by_job_id"):
            raise ToolError("source job already has a replacement")
        result = self.dispatch(
            task=task,
            workdir=str(source["workdir"]),
            model=str(source.get("model") or DEFAULT_MODEL),
            effort=source.get("effort"),
            max_turns=source.get("max_turns"),
            permission_mode=str(source.get("permission_mode") or "default"),
            callback_context=callback_context,
            restart_of_job_id=source_job_id,
        )
        replacement_id = str(result["job_id"])
        self._update_job(
            source_job_id,
            lambda record: record.update({"restarted_by_job_id": replacement_id}),
        )
        self._event(source_job_id, "replacement_started")
        return result


def _capture(stream: TextIO, chunks: List[bytes], state: Dict[str, bool]) -> None:
    total = 0
    while True:
        piece = stream.buffer.read(64 * 1024)
        if not piece:
            break
        remaining = MAX_CAPTURE_BYTES - total
        if remaining > 0:
            chunks.append(piece[:remaining])
            total += min(len(piece), remaining)
        if len(piece) > remaining:
            state["truncated"] = True


def _run_claude(
    manager: DelegationManager,
    job_id: str,
    command: List[str],
    prompt: str,
    workdir: Path,
) -> Tuple[int, str, str, bool]:
    """Run an argv-only child while keeping prompts out of the process list and logs."""
    process = subprocess.Popen(
        command,
        cwd=str(workdir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        close_fds=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stdout_chunks: List[bytes] = []
    stderr_chunks: List[bytes] = []
    stdout_state = {"truncated": False}
    stderr_state = {"truncated": False}
    stdout_thread = threading.Thread(target=_capture, args=(process.stdout, stdout_chunks, stdout_state), daemon=True)
    stderr_thread = threading.Thread(target=_capture, args=(process.stderr, stderr_chunks, stderr_state), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        process.stdin.write(prompt)
        process.stdin.close()
        while process.poll() is None:
            manager._update_job(
                job_id,
                lambda record: record.update({"heartbeat_at": utc_now(), "pid": os.getpid()}),
            )
            time.sleep(0.25)
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return process.returncode or 0, stdout, stderr, stdout_state["truncated"] or stderr_state["truncated"]


def run_worker(
    job_id: str,
    state_dir: str,
    claude_bin: str,
    allowed_roots: List[str],
    codex_bin: str = "codex",
    completion_transport: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> int:
    manager = DelegationManager(
        state_dir,
        allowed_roots,
        claude_bin=claude_bin,
        codex_bin=codex_bin,
        completion_transport=completion_transport,
    )
    try:
        executable = manager._validate_runtime()
    except ToolError:
        manager._update_job(
            job_id,
            lambda record: record.update(
                {
                    "status": "failed",
                    "phase": "runtime check failed",
                    "blocker": "Claude CLI authentication or availability check failed",
                    "next_action": "run claude auth status locally, then dispatch a new job",
                }
            ),
        )
        manager._event(job_id, "runtime_check_failed")
        manager.emit_completion(job_id)
        return 1

    manager._update_job(job_id, lambda record: record.update({"pid": os.getpid(), "heartbeat_at": utc_now()}))
    while True:
        batch_ids: List[str] = []
        request_holder: Dict[str, Any] = {}

        def prepare(record: Dict[str, Any]) -> None:
            if record.get("close_requested"):
                record.update(
                    {"status": "cancelled", "phase": "closed", "blocker": None, "next_action": None}
                )
                request_holder["closed"] = True
                return
            if not record.get("initial_completed"):
                request_holder.update({"request": record["task"], "followup": False})
            else:
                queued = [item for item in record.get("messages", []) if item.get("state") == "queued"]
                if not queued:
                    record.update(
                        {"status": "completed", "phase": "completed", "blocker": None, "next_action": None}
                    )
                    request_holder["completed"] = True
                    return
                if not record.get("session_id"):
                    record.update(
                        {
                            "status": "failed",
                            "phase": "resume unavailable",
                            "blocker": "Claude did not return a resumable session id",
                            "next_action": "dispatch a new job containing the follow-up requirements",
                        }
                    )
                    request_holder["failed"] = True
                    return
                for item in queued:
                    item["state"] = "inflight"
                    batch_ids.append(item["id"])
                request_holder.update(
                    {
                        "request": "\n\n".join(item["message"] for item in queued),
                        "followup": True,
                    }
                )
            record.update(
                {
                    "status": "running",
                    "phase": "Claude Code running",
                    "objective": "complete queued follow-up" if request_holder.get("followup") else "complete delegated task",
                    "next_action": "wait for Claude response",
                    "heartbeat_at": utc_now(),
                    "pid": os.getpid(),
                    "turn_count": int(record.get("turn_count", 0)) + 1,
                }
            )
            request_holder["turn_number"] = record["turn_count"]

        record = manager._update_job(job_id, prepare)
        if request_holder.get("closed"):
            manager._event(job_id, "closed")
            manager.emit_completion(job_id)
            return 0
        if request_holder.get("completed"):
            manager._event(job_id, "completed")
            manager.emit_completion(job_id)
            return 0
        if request_holder.get("failed"):
            manager._event(job_id, "resume_unavailable")
            manager.emit_completion(job_id)
            return 1

        workdir = manager._validate_workdir(record["workdir"])
        session_id = record.get("session_id") if request_holder["followup"] else None
        command = manager._command(
            executable,
            record["model"],
            record.get("effort"),
            record["max_turns"],
            record["permission_mode"],
            session_id,
        )
        prompt = manager._prompt(request_holder["request"], workdir, request_holder["followup"])
        try:
            exit_code, stdout, stderr, truncated = _run_claude(manager, job_id, command, prompt, workdir)
        except OSError:
            exit_code, stdout, stderr, truncated = 1, "", "", False
        metadata = {
            "exit_code": exit_code,
            "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
            "kind": "followup" if request_holder["followup"] else "initial",
        }
        manager._write_redacted_stream_logs(
            job_id,
            int(request_holder["turn_number"]),
            stdout,
            stderr,
        )
        manager._event(job_id, "claude_turn_finished", **metadata)

        if exit_code != 0 or truncated:
            def fail(record: Dict[str, Any]) -> None:
                record.update(
                    {
                        "status": "failed",
                        "phase": "Claude failed",
                        "exit_code": exit_code,
                        "blocker": (
                            "Claude output exceeded the safe in-memory capture limit"
                            if truncated
                            else "Claude exited unsuccessfully; output was omitted to protect secrets"
                        ),
                        "next_action": "run the task directly to inspect the failure, then dispatch a new job",
                    }
                )

            manager._update_job(job_id, fail)
            manager.emit_completion(job_id)
            return exit_code or 1

        session: Optional[str] = session_id
        validation = "Claude completed without a JSON result"
        try:
            payload = json.loads(stdout)
            if isinstance(payload, dict):
                raw_session = payload.get("session_id")
                if isinstance(raw_session, str) and raw_session:
                    session = raw_session
                validation = manager.redact(payload.get("result", "Claude completed"), limit=2_000)
        except json.JSONDecodeError:
            pass

        def finish_turn(record: Dict[str, Any]) -> None:
            record.update(
                {
                    "initial_completed": True,
                    "session_id": session,
                    "latest_validation": validation,
                    "exit_code": 0,
                    "phase": "turn completed",
                    "next_action": "process queued follow-ups or finish",
                    "heartbeat_at": utc_now(),
                }
            )
            for item in record.get("messages", []):
                if item.get("id") in batch_ids:
                    item["state"] = "delivered"
                    item["delivered_at"] = utc_now()

        manager._update_job(job_id, finish_turn)


def tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": "claude_code_dispatch",
            "description": "Launch a durable authenticated local Claude Code delegation job.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task", "workdir"],
                "properties": {
                    "task": {"type": "string"},
                    "workdir": {"type": "string"},
                    "model": {
                        "enum": ["opus", "fable", "claude-fable-5", "claude-fable-5-1"],
                        "default": DEFAULT_MODEL,
                    },
                    "effort": {"enum": ["low", "medium", "high", "xhigh", "max"]},
                    "max_turns": {"type": "integer", "minimum": 1, "maximum": 200},
                    "permission_mode": {
                        "enum": ["default", "acceptEdits", "plan", "auto"],
                        "default": "default",
                    },
                    "dry_run": {"type": "boolean", "default": False},
                },
            },
        },
        {
            "name": "claude_code_status",
            "description": "Read concise durable status without exposing Claude logs or queued message bodies.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
            },
        },
        {
            "name": "claude_code_message",
            "description": "Durably queue a follow-up for an active Claude Code job.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["job_id", "message"],
                "properties": {"job_id": {"type": "string"}, "message": {"type": "string"}},
            },
        },
        {
            "name": "claude_code_close",
            "description": "Request graceful close after the current Claude turn; never kills Claude mid-turn.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
            },
        },
        {
            "name": "claude_code_list",
            "description": "List active Claude Code jobs without task bodies, transcripts, or secrets.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        },
        {
            "name": "claude_code_restart",
            "description": "Start one replacement for a user-closed non-Fable job; never retries failures.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_job_id", "task"],
                "properties": {
                    "source_job_id": {"type": "string"},
                    "task": {"type": "string"},
                },
            },
        },
    ]


def callback_context_from_mcp_meta(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    thread_id = value.get("threadId")
    metadata = value.get("x-codex-turn-metadata", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    if not isinstance(thread_id, str) or not thread_id:
        candidate = metadata.get("thread_id")
        thread_id = candidate if isinstance(candidate, str) else None
    if not thread_id:
        return {}
    subagent_kind = metadata.get("subagent_kind")
    return {
        "thread_id": thread_id,
        "session_id": metadata.get("session_id") if isinstance(metadata.get("session_id"), str) else thread_id,
        "turn_id": metadata.get("turn_id") if isinstance(metadata.get("turn_id"), str) else None,
        "coordinator_identity": subagent_kind if isinstance(subagent_kind, str) and subagent_kind else "coordinator",
        "mcp_call_id": value.get("callId") if isinstance(value.get("callId"), str) else None,
    }


def serve(manager: DelegationManager, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout) -> None:
    operations = {
        "claude_code_dispatch": manager.dispatch,
        "claude_code_status": manager.status,
        "claude_code_message": manager.message,
        "claude_code_close": manager.close,
        "claude_code_list": manager.list_active,
        "claude_code_restart": manager.restart,
    }
    for line in input_stream:
        request: Any = None
        try:
            request = json.loads(line)
            method = request.get("method")
            params = request.get("params", {})
            if method == "initialize":
                result: Dict[str, Any] = {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "claude-auto-router-codex", "version": "2.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": tool_definitions()}
            elif method == "tools/call":
                name = params.get("name")
                if name not in operations:
                    raise ToolError("unknown tool")
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise ToolError("tool arguments must be an object")
                try:
                    if name in {"claude_code_dispatch", "claude_code_restart"}:
                        arguments = dict(arguments)
                        arguments["callback_context"] = callback_context_from_mcp_meta(params.get("_meta"))
                    payload = operations[name](**arguments)
                    result = {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}
                except (ToolError, TypeError) as exc:
                    result = {
                        "content": [{"type": "text", "text": manager.redact(str(exc), limit=500)}],
                        "isError": True,
                    }
            elif method == "ping":
                result = {}
            elif method in {"notifications/initialized", "notifications/cancelled", "exit"}:
                continue
            else:
                raise ToolError("unsupported method")
            if "id" in request:
                output_stream.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n")
                output_stream.flush()
        except (json.JSONDecodeError, ToolError, TypeError, AttributeError) as exc:
            if isinstance(request, dict) and "id" in request:
                response = {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32600, "message": manager.redact(str(exc), limit=500)},
                }
                output_stream.write(json.dumps(response) + "\n")
                output_stream.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("CLAUDE_DELEGATION_STATE_DIR", "~/.local/state/codex-claude-delegation"),
    )
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_DELEGATION_CLI", "claude"))
    parser.add_argument("--codex-bin", default=os.environ.get("CLAUDE_DELEGATION_CODEX_CLI", "codex"))
    parser.add_argument("--allowed-root", action="append", default=[])
    parser.add_argument("--recover-callbacks", action="store_true")
    arguments = parser.parse_args()
    try:
        roots = arguments.allowed_root or [str(root) for root in parse_allowed_roots(os.environ.get("CLAUDE_DELEGATION_ALLOWED_ROOTS"))]
        if arguments.worker:
            return run_worker(arguments.worker, arguments.state_dir, arguments.claude_bin, roots, arguments.codex_bin)
        manager = DelegationManager(
            arguments.state_dir,
            roots,
            arguments.claude_bin,
            codex_bin=arguments.codex_bin,
        )
        if arguments.recover_callbacks:
            manager.recover_pending_completions()
            return 0
        serve(manager)
        return 0
    except ToolError as exc:
        sys.stderr.write("claude-code-delegation: " + SecretRedactor()(str(exc), limit=500) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
