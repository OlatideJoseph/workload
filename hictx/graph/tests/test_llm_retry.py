from django.test import SimpleTestCase

from hictx.graph.services.llm import _call_with_retries


class RetryTests(SimpleTestCase):
    def test_retries_transient_then_succeeds(self):
        calls = {"n": 0}

        def do():
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("503 UNAVAILABLE: model is overloaded")
            return "ok"

        self.assertEqual(_call_with_retries(do, attempts=3, base_delay=0), "ok")
        self.assertEqual(calls["n"], 2)

    def test_non_transient_reraised_immediately(self):
        calls = {"n": 0}

        def do():
            calls["n"] += 1
            raise ValueError("invalid api key")

        with self.assertRaises(ValueError):
            _call_with_retries(do, attempts=3, base_delay=0)
        self.assertEqual(calls["n"], 1)  # not retried

    def test_gives_up_after_attempts(self):
        calls = {"n": 0}

        def do():
            calls["n"] += 1
            raise RuntimeError("503 unavailable")

        with self.assertRaises(RuntimeError):
            _call_with_retries(do, attempts=2, base_delay=0)
        self.assertEqual(calls["n"], 2)
