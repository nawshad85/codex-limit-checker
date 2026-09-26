from __future__ import annotations

import json
import queue
import threading
import unittest

from app.codex_app_server import (
    AppServerError,
    CodexAppServerClient,
    parse_rate_limits_response,
)
from app.models import LimitKind


def _window(used: object, minutes: object, reset: object = 2_000_000_000) -> dict[str, object]:
    return {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": reset}


def _response(
    *,
    primary: object = None,
    secondary: object = None,
    limit_id: str = "codex",
) -> dict[str, object]:
    return {
        "id": 4,
        "result": {
            "rateLimits": {
                "limitId": limit_id,
                "planType": "plus",
                "primary": primary,
                "secondary": secondary,
            }
        },
    }


class RateLimitResponseTests(unittest.TestCase):
    def test_five_hour_and_weekly_windows(self) -> None:
        response = _response(primary=_window(28, 300), secondary=_window(57, 10_080))
        state = parse_rate_limits_response(response, 1_900_000_000.0)

        self.assertEqual(state.five_hour.kind, LimitKind.FIVE_HOUR)
        self.assertEqual(state.five_hour.used_percent, 28)
        self.assertEqual(state.five_hour.remaining_percent, 72)
        self.assertEqual(state.five_hour.source, "app-server")
        self.assertEqual(state.weekly.kind, LimitKind.WEEKLY)
        self.assertEqual(state.weekly.remaining_percent, 43)
        self.assertEqual(state.plan, "plus")
        self.assertEqual(state.limit_id, "codex")

    def test_only_five_hour_and_null_secondary(self) -> None:
        state = parse_rate_limits_response(
            _response(primary=_window(19, 300), secondary=None), 1_900_000_000.0
        )
        self.assertEqual(state.five_hour.used_percent, 19)
        self.assertIsNone(state.weekly)

    def test_only_weekly_and_null_primary(self) -> None:
        state = parse_rate_limits_response(
            _response(primary=None, secondary=_window(64, 10_080)), 1_900_000_000.0
        )
        self.assertIsNone(state.five_hour)
        self.assertEqual(state.weekly.used_percent, 64)

    def test_reversed_primary_and_secondary_windows(self) -> None:
        state = parse_rate_limits_response(
            _response(primary=_window(60, 10_080), secondary=_window(15, 300)),
            1_900_000_000.0,
        )
        self.assertEqual(state.five_hour.used_percent, 15)
        self.assertEqual(state.weekly.used_percent, 60)

    def test_exact_codex_bucket_wins_over_model_specific_and_legacy(self) -> None:
        response = _response(primary=_window(99, 300), limit_id="codex_special_model")
        response["result"]["rateLimitsByLimitId"] = {
            "codex_special_model": {
                "limitId": "codex_special_model",
                "primary": _window(91, 300),
                "secondary": _window(88, 10_080),
            },
            "codex": {
                "limitId": "codex",
                "planType": "pro",
                "primary": _window(12, 300),
                "secondary": _window(34, 10_080),
            },
        }
        state = parse_rate_limits_response(response, 1_900_000_000.0)
        self.assertEqual(state.five_hour.used_percent, 12)
        self.assertEqual(state.weekly.used_percent, 34)
        self.assertEqual(state.plan, "pro")

    def test_model_specific_bucket_is_never_used_as_shared_quota(self) -> None:
        response = _response(primary=_window(70, 300), limit_id="codex_special_model")
        response["result"]["rateLimitsByLimitId"] = {
            "codex_special_model": response["result"]["rateLimits"]
        }
        with self.assertRaisesRegex(AppServerError, "Shared Codex"):
            parse_rate_limits_response(response, 1_900_000_000.0)

    def test_legacy_without_limit_id_is_accepted_only_without_other_buckets(self) -> None:
        response = _response(primary=_window(20, 300))
        del response["result"]["rateLimits"]["limitId"]
        self.assertEqual(
            parse_rate_limits_response(response, 1_900_000_000.0).five_hour.used_percent,
            20,
        )
        response["result"]["rateLimitsByLimitId"] = {
            "codex_model": {"limitId": "codex_model", "primary": _window(80, 300)}
        }
        with self.assertRaises(AppServerError):
            parse_rate_limits_response(response, 1_900_000_000.0)

    def test_invalid_reset_timestamp_is_optional_and_percentages_are_clamped(self) -> None:
        state = parse_rate_limits_response(
            _response(primary=_window(120, 300, "bad"), secondary=_window(-10, 10_080)),
            1_900_000_000.0,
        )
        self.assertIsNone(state.five_hour.resets_at)
        self.assertEqual(state.five_hour.used_percent, 100)
        self.assertEqual(state.weekly.used_percent, 0)

    def test_malformed_json_rpc_and_missing_windows(self) -> None:
        for response in (None, [], {"result": None}, {"result": {}}, {"result": {"rateLimits": None}}):
            with self.subTest(response=response), self.assertRaises(AppServerError):
                parse_rate_limits_response(response, 1_900_000_000.0)


class _FakeStdout:
    def __init__(self) -> None:
        self.lines: queue.Queue[bytes] = queue.Queue()

    def readline(self, _limit: int = -1) -> bytes:
        return self.lines.get()

    def close(self) -> None:
        self.lines.put(b"")

    def push(self, message: dict[str, object]) -> None:
        self.lines.put(json.dumps(message).encode("utf-8") + b"\n")


class _FakeStdin:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process

    def write(self, value: bytes) -> int:
        self.process.receive(json.loads(value))
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, mode: str = "reply") -> None:
        self.mode = mode
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout()
        self.requests: list[dict[str, object]] = []
        self.rate_request_seen = threading.Event()
        self.exit_code: int | None = None
        self.terminate_calls = 0

    def receive(self, message: dict[str, object]) -> None:
        self.requests.append(message)
        if message.get("method") == "initialize":
            self.stdout.push({"id": message["id"], "result": {"userAgent": "test"}})
        elif message.get("method") == "account/rateLimits/read":
            self.rate_request_seen.set()
            if self.mode == "reply":
                self.stdout.push(
                    {"id": message["id"], "result": _response(primary=_window(25, 300))["result"]}
                )
            elif self.mode == "malformed_then_reply":
                self.stdout.lines.put(b"{not-json}\n")
                self.stdout.push(
                    {"id": message["id"], "result": _response(primary=_window(25, 300))["result"]}
                )
            elif self.mode == "auth_error":
                self.stdout.push({"id": message["id"], "error": {"code": -32600, "message": "private test text"}})
            elif self.mode == "exit":
                self.terminate()

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.exit_code = 0
        self.stdout.lines.put(b"")

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code if self.exit_code is not None else 0


class AppServerTransportTests(unittest.TestCase):
    def test_handshake_and_repeated_reads_use_one_child(self) -> None:
        children: list[_FakeProcess] = []

        def factory(*_args: object, **_kwargs: object) -> _FakeProcess:
            child = _FakeProcess()
            children.append(child)
            return child

        client = CodexAppServerClient(executable="fake-codex", process_factory=factory)
        try:
            first = client.read_rate_limits(timeout=1)
            second = client.read_rate_limits(timeout=1)
            self.assertEqual(first.five_hour.used_percent, 25)
            self.assertEqual(second.five_hour.used_percent, 25)
            self.assertEqual(len(children), 1)
            self.assertEqual(
                [request["method"] for request in children[0].requests],
                ["initialize", "initialized", "account/rateLimits/read", "account/rateLimits/read"],
            )
        finally:
            client.close()
        self.assertGreaterEqual(children[0].terminate_calls, 1)

    def test_malformed_jsonl_line_is_ignored(self) -> None:
        child = _FakeProcess("malformed_then_reply")
        client = CodexAppServerClient(executable="fake-codex", process_factory=lambda *_a, **_k: child)
        try:
            self.assertEqual(client.read_rate_limits(timeout=1).five_hour.used_percent, 25)
        finally:
            client.close()

    def test_timeout_terminates_child(self) -> None:
        child = _FakeProcess("timeout")
        client = CodexAppServerClient(executable="fake-codex", process_factory=lambda *_a, **_k: child)
        with self.assertRaisesRegex(AppServerError, "timed out"):
            client.read_rate_limits(timeout=0.2)
        self.assertGreaterEqual(child.terminate_calls, 1)
        client.close()

    def test_unexpected_termination_is_reported_and_child_can_restart(self) -> None:
        children = [_FakeProcess("exit"), _FakeProcess("reply")]
        client = CodexAppServerClient(
            executable="fake-codex", process_factory=lambda *_a, **_k: children.pop(0)
        )
        with self.assertRaisesRegex(AppServerError, "exited"):
            client.read_rate_limits(timeout=1)
        # Advance the retry gate without a wall-clock sleep.
        client._next_retry_at = 0.0
        try:
            self.assertEqual(client.read_rate_limits(timeout=1).five_hour.used_percent, 25)
        finally:
            client.close()

    def test_auth_error_restarts_child_for_new_login(self) -> None:
        failed = _FakeProcess("auth_error")
        succeeding = _FakeProcess("reply")
        children = [failed, succeeding]
        client = CodexAppServerClient(
            executable="fake-codex", process_factory=lambda *_a, **_k: children.pop(0)
        )
        with self.assertRaisesRegex(AppServerError, "-32600") as caught:
            client.read_rate_limits(timeout=1)
        self.assertNotIn("private test text", str(caught.exception))
        self.assertGreaterEqual(failed.terminate_calls, 1)
        client._next_retry_at = 0.0
        try:
            self.assertEqual(client.read_rate_limits(timeout=1).five_hour.used_percent, 25)
        finally:
            client.close()
        self.assertGreaterEqual(succeeding.terminate_calls, 1)

    def test_close_cancels_pending_read_from_other_thread(self) -> None:
        child = _FakeProcess("timeout")
        client = CodexAppServerClient(executable="fake-codex", process_factory=lambda *_a, **_k: child)
        errors: list[Exception] = []

        def read() -> None:
            try:
                client.read_rate_limits(timeout=5)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=read)
        worker.start()
        self.assertTrue(child.rate_request_seen.wait(timeout=1))
        client.close()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AppServerError)
        self.assertGreaterEqual(child.terminate_calls, 1)


if __name__ == "__main__":
    unittest.main()
