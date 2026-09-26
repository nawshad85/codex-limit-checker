"""Read shared account limits from a local Codex app-server process.

Only the initialization handshake and account/rateLimits/read are sent. The
subprocess inherits Codex's existing local login; this module never handles
credentials, prompts, threads, or model requests.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .models import LimitKind, RateWindow
from .utils import clamp, parse_timestamp, safe_float, safe_int


LOGGER = logging.getLogger(__name__)
_MAX_LINE_BYTES = 1024 * 1024
_QUEUE_CAPACITY = 32
_MAX_BACKOFF_SECONDS = 60.0


class AppServerError(RuntimeError):
    """A safe, user-displayable app-server failure without response contents."""

    def __init__(self, message: str, *, restart: bool = False) -> None:
        super().__init__(message)
        self.restart = restart


@dataclass(frozen=True)
class RateLimitState:
    five_hour: Optional[RateWindow]
    weekly: Optional[RateWindow]
    plan: Optional[str]
    fetched_at: float
    limit_id: Optional[str]
    source: str = "app-server"


def _safe_label(value: object) -> Optional[str]:
    if isinstance(value, str):
        value = value.strip()
        if 0 < len(value) <= 64 and all(char.isprintable() for char in value):
            return value
    return None


def _field(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return None


def _parse_window(data: object, observed_at: float) -> Optional[RateWindow]:
    if not isinstance(data, dict):
        return None
    used = safe_float(_field(data, "usedPercent", "used_percent"))
    duration = safe_int(_field(data, "windowDurationMins", "window_minutes"))
    if used is None or duration is None:
        return None
    if 240 <= duration <= 360:
        kind = LimitKind.FIVE_HOUR
    elif 9_000 <= duration <= 11_000:
        kind = LimitKind.WEEKLY
    else:
        # A primary/secondary slot does not identify the account window.
        return None
    return RateWindow(
        kind=kind,
        used_percent=clamp(used, 0.0, 100.0),
        window_minutes=duration,
        resets_at=parse_timestamp(_field(data, "resetsAt", "resets_at")),
        observed_at=observed_at,
        source="app-server",
    )


def parse_rate_limits_response(message: object, fetched_at: float) -> RateLimitState:
    """Normalize an app-server JSON-RPC rate-limits response.

    The exact ``codex`` bucket takes precedence. A legacy ``rateLimits``
    response is accepted only when it is explicitly ``codex`` or no other
    limit IDs were supplied, so a model-specific bucket cannot silently become
    the shared account quota.
    """
    if not isinstance(message, dict):
        raise AppServerError("Malformed Codex app-server response")
    if "error" in message:
        error = message.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, int) and not isinstance(code, bool):
            raise AppServerError(f"Codex app-server returned error code {code}", restart=True)
        raise AppServerError("Codex app-server returned an error", restart=True)
    result = message.get("result")
    if not isinstance(result, dict):
        raise AppServerError("Malformed Codex app-server rate-limit response")

    buckets = result.get("rateLimitsByLimitId")
    legacy = result.get("rateLimits")
    selected: Optional[dict[str, Any]] = None
    limit_id: Optional[str] = None
    if isinstance(buckets, dict) and isinstance(buckets.get("codex"), dict):
        selected = buckets["codex"]
        limit_id = "codex"
        embedded_id = _field(selected, "limitId", "limit_id")
        if embedded_id is not None and embedded_id != "codex":
            raise AppServerError("Codex quota bucket identity is inconsistent")
    elif isinstance(legacy, dict):
        legacy_id = _field(legacy, "limitId", "limit_id")
        has_other_buckets = buckets is not None and not (isinstance(buckets, dict) and not buckets)
        if legacy_id == "codex" or (legacy_id is None and not has_other_buckets):
            selected = legacy
            limit_id = legacy_id

    if selected is None:
        raise AppServerError("Shared Codex rate-limit bucket is unavailable")

    observed_at = safe_float(fetched_at)
    if observed_at is None:
        raise AppServerError("Invalid rate-limit fetch time")
    windows: dict[LimitKind, RateWindow] = {}
    for slot in ("primary", "secondary"):
        window = _parse_window(selected.get(slot), observed_at)
        if window is not None:
            windows.setdefault(window.kind, window)
    if not windows:
        raise AppServerError("Shared Codex rate-limit windows are unavailable")

    plan = _safe_label(_field(selected, "planType", "plan_type"))
    if plan is None:
        plan = _safe_label(_field(result, "planType", "plan_type"))
    if plan is not None and plan.casefold() in {"none", "null", "unknown"}:
        plan = None

    LOGGER.debug(
        "App-server selected limit ID %s; 5H window %s; weekly window %s",
        limit_id or "legacy",
        windows.get(LimitKind.FIVE_HOUR).window_minutes if LimitKind.FIVE_HOUR in windows else "N/A",
        windows.get(LimitKind.WEEKLY).window_minutes if LimitKind.WEEKLY in windows else "N/A",
    )
    return RateLimitState(
        five_hour=windows.get(LimitKind.FIVE_HOUR),
        weekly=windows.get(LimitKind.WEEKLY),
        plan=plan,
        fetched_at=observed_at,
        limit_id=limit_id,
    )


class CodexAppServerClient:
    """A serialized, restartable JSONL client with cancellable reads.

    ``close`` can run from the UI thread while ``read_rate_limits`` waits on
    the worker thread. The child is detached and terminated immediately; the
    reader checks cancellation at most 100 ms later.
    """

    def __init__(
        self,
        executable: Optional[str] = None,
        *,
        process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self._executable = executable
        self._process_factory = process_factory
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._responses: queue.Queue[tuple[object, Optional[dict[str, Any]]]] = queue.Queue(
            maxsize=_QUEUE_CAPACITY
        )
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = threading.Event()
        self._next_id = 1
        self._next_retry_at = 0.0
        self._retry_delay = 1.0

    def read_rate_limits(self, timeout: float = 8.0) -> RateLimitState:
        duration = safe_float(timeout)
        if duration is None or duration <= 0:
            raise ValueError("timeout must be a positive finite number")
        deadline = time.monotonic() + duration
        with self._request_lock:
            if self._closed.is_set():
                raise AppServerError("Codex app-server client is closed")
            if time.monotonic() < self._next_retry_at:
                raise AppServerError("Codex app-server is retrying after a failure")
            try:
                process = self._ensure_process(deadline)
                response = self._request(process, "account/rateLimits/read", None, deadline)
                state = parse_rate_limits_response(response, time.time())
            except AppServerError as exc:
                self._record_failure(restart=exc.restart)
                LOGGER.debug("App-server rate-limit fetch failed: %s", str(exc))
                raise
            else:
                self._next_retry_at = 0.0
                self._retry_delay = 1.0
                LOGGER.debug("App-server rate-limit fetch succeeded")
                return state

    def close(self) -> None:
        self._closed.set()
        with self._state_lock:
            process = self._process
            self._process = None
        if process is not None:
            self._terminate(process)

    def _ensure_process(self, deadline: float) -> subprocess.Popen[bytes]:
        with self._state_lock:
            process = self._process
        if process is not None and process.poll() is None:
            return process
        if process is not None:
            self._detach(process)
            self._terminate(process)

        executable = self._executable or shutil.which("codex") or shutil.which("codex.exe") or shutil.which("codex.cmd")
        if not executable:
            raise AppServerError("Codex CLI was not found", restart=True)
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            started = self._process_factory([executable, "app-server", "--listen", "stdio://"], **kwargs)
        except (OSError, ValueError) as exc:
            raise AppServerError(
                f"Codex app-server could not start ({type(exc).__name__})", restart=True
            ) from None
        with self._state_lock:
            if self._closed.is_set():
                should_close = True
            else:
                self._process = started
                should_close = False
        if should_close:
            self._terminate(started)
            raise AppServerError("Codex app-server client is closed", restart=True)

        try:
            threading.Thread(
                target=self._read_stdout,
                args=(started,),
                name="codex-app-server-reader",
                daemon=True,
            ).start()
        except RuntimeError:
            self._detach(started)
            self._terminate(started)
            raise AppServerError("Codex app-server reader could not start", restart=True) from None
        try:
            init_response = self._request(
                started,
                "initialize",
                {"clientInfo": {"name": "codex_usage_monitor", "title": "Codex Usage Monitor", "version": "1.0.0"}},
                deadline,
            )
            if not isinstance(init_response.get("result"), dict):
                raise AppServerError("Codex app-server initialization failed", restart=True)
            self._send(started, {"method": "initialized", "params": {}}, deadline)
        except AppServerError:
            self._detach(started)
            self._terminate(started)
            raise
        LOGGER.debug("Codex app-server connected")
        return started

    def _request(
        self,
        process: subprocess.Popen[bytes],
        method: str,
        params: Optional[dict[str, Any]],
        deadline: float,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            payload["params"] = params
        self._send(process, payload, deadline)
        while True:
            if self._closed.is_set():
                raise AppServerError("Codex app-server client is closed", restart=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("Codex app-server request timed out", restart=True)
            try:
                origin, message = self._responses.get(timeout=min(0.1, remaining))
            except queue.Empty:
                if process.poll() is not None:
                    raise AppServerError("Codex app-server exited", restart=True)
                continue
            if origin is not process:
                continue
            if message is None:
                raise AppServerError("Codex app-server exited", restart=True)
            if message.get("id") == request_id:
                if "error" in message:
                    error = message.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    if isinstance(code, int) and not isinstance(code, bool):
                        raise AppServerError(f"Codex app-server returned error code {code}", restart=True)
                    raise AppServerError("Codex app-server returned an error", restart=True)
                return message

    def _send(self, process: subprocess.Popen[bytes], payload: dict[str, Any], deadline: float) -> None:
        if self._closed.is_set():
            raise AppServerError("Codex app-server client is closed", restart=True)
        if time.monotonic() >= deadline:
            raise AppServerError("Codex app-server request timed out", restart=True)
        stream = process.stdin
        if stream is None:
            raise AppServerError("Codex app-server input is unavailable", restart=True)
        try:
            stream.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
            stream.flush()
        except (OSError, ValueError):
            raise AppServerError("Codex app-server input failed", restart=True) from None

    def _read_stdout(self, process: subprocess.Popen[bytes]) -> None:
        stream = process.stdout
        if stream is None:
            self._enqueue(process, None)
            return
        try:
            while not self._closed.is_set():
                line = stream.readline(_MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > _MAX_LINE_BYTES:
                    LOGGER.debug("Skipped oversized app-server JSONL response")
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(_MAX_LINE_BYTES + 1)
                    continue
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    LOGGER.debug("Skipped malformed app-server JSONL response")
                    continue
                if isinstance(message, dict):
                    self._enqueue(process, message)
        except (OSError, ValueError):
            LOGGER.debug("Codex app-server output closed")
        finally:
            self._enqueue(process, None)

    def _enqueue(self, process: subprocess.Popen[bytes], message: Optional[dict[str, Any]]) -> None:
        try:
            self._responses.put_nowait((process, message))
        except queue.Full:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                pass
            try:
                self._responses.put_nowait((process, message))
            except queue.Full:
                pass

    def _detach(self, process: subprocess.Popen[bytes]) -> None:
        with self._state_lock:
            if self._process is process:
                self._process = None

    def _record_failure(self, *, restart: bool) -> None:
        if self._closed.is_set():
            return
        self._next_retry_at = time.monotonic() + self._retry_delay
        self._retry_delay = min(_MAX_BACKOFF_SECONDS, self._retry_delay * 2.0)
        if restart:
            with self._state_lock:
                process = self._process
                self._process = None
            if process is not None:
                self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                LOGGER.warning("Could not confirm Codex app-server process exit")
        except OSError:
            pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
