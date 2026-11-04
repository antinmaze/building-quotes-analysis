import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import BaseModel

from devis_mngt.cloud import AzureBackend, Settings, rate_limit_delay
from devis_mngt.limits import CloudLimits, LimitExceeded


class Result(BaseModel):
    value: str


class RateLimited(Exception):
    status_code = 429
    request_id = "test-request"

    def __init__(self, headers):
        self.response = SimpleNamespace(headers=headers)


class PacingTests(unittest.TestCase):
    def backend(self, effects, max_calls=10):
        client = MagicMock()
        client.beta.chat.completions.parse.side_effect = effects
        backend = AzureBackend(
            Settings("https://di.example", "https://aoai.example", "test"),
            openai_client=client, limits=CloudLimits(max_llm_calls=max_calls),
            journal=MagicMock(),
        )
        return backend, client

    def success(self):
        return SimpleNamespace(
            usage=None, choices=[SimpleNamespace(
                message=SimpleNamespace(refusal=None, parsed=Result(value="ok")),
            )],
        )

    def test_retry_honors_header_and_counts_both_attempts(self):
        backend, client = self.backend([RateLimited({"retry-after": "30"}), self.success()])
        with patch("devis_mngt.cloud.monotonic", return_value=100), \
             patch("devis_mngt.cloud.sleep") as sleep:
            self.assertEqual(backend.structured("", {}, Result).value, "ok")
        sleep.assert_called_once_with(30)
        self.assertEqual(backend.limits.llm_calls, 2)
        self.assertEqual(client.beta.chat.completions.parse.call_count, 2)

    def test_exhausted_retries_propagate_last_error(self):
        error = RateLimited({})
        backend, client = self.backend([error, error, error])
        with patch("devis_mngt.cloud.monotonic", return_value=100), \
             patch("devis_mngt.cloud.sleep") as sleep:
            with self.assertRaises(RateLimited):
                backend.structured("", {}, Result)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60])
        self.assertEqual(client.beta.chat.completions.parse.call_count, 3)
        self.assertEqual(backend.limits.llm_calls, 3)

    def test_call_limit_stops_before_retry_and_sleep(self):
        backend, client = self.backend([RateLimited({})], max_calls=1)
        with patch("devis_mngt.cloud.sleep") as sleep:
            with self.assertRaises(LimitExceeded):
                backend.structured("", {}, Result)
        sleep.assert_not_called()
        self.assertEqual(client.beta.chat.completions.parse.call_count, 1)

    def test_long_service_delay_is_not_shortened(self):
        backend, client = self.backend([RateLimited({"retry-after": "301"})])
        with patch("devis_mngt.cloud.sleep") as sleep:
            with self.assertRaises(RateLimited):
                backend.structured("", {}, Result)
        sleep.assert_not_called()
        self.assertEqual(client.beta.chat.completions.parse.call_count, 1)

    def test_delay_headers_and_invalid_values(self):
        self.assertEqual(rate_limit_delay({"retry-after-ms": "4500"}, 0), 4.5)
        self.assertEqual(rate_limit_delay(
            {"retry-after-ms": "1000", "retry-after": "30"}, 0), 30)
        for value in ("nan", "inf", "-1", "invalid"):
            self.assertEqual(rate_limit_delay({"retry-after": value}, 1), 60)
        clock = MagicMock()
        from datetime import datetime, timezone
        clock.now.return_value = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc)
        with patch("devis_mngt.cloud.datetime", clock):
            self.assertEqual(rate_limit_delay(
                {"retry-after": "Fri, 16 Oct 2026 17:00:30 GMT"}, 0), 30)
