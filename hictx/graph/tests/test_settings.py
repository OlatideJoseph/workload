from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from hictx.graph.models import Setting
from hictx.graph.services import app_settings
from hictx.graph.services.llm import (
    AnthropicTaskRunner,
    GeminiTaskRunner,
    OpenAITaskRunner,
)

_NO_ENV = {k: "" for k in
           ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")}


def _clear_env():
    import os
    for k in _NO_ENV:
        os.environ.pop(k, None)


class AppSettingsTests(TestCase):
    def test_per_provider_keys_and_selection(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            _clear_env()
            app_settings.set_api_key("openai", "sk-openai-abcd")
            app_settings.set_provider("openai")
            self.assertEqual(app_settings.get_provider(), "openai")
            self.assertEqual(app_settings.get_api_key("openai"), "sk-openai-abcd")
            self.assertEqual(app_settings.key_source("openai"), "stored")
            self.assertEqual(app_settings.key_hint("openai"), "…abcd")
            self.assertFalse(app_settings.is_configured("anthropic"))

    def test_env_overrides_stored_per_provider(self):
        app_settings.set_api_key("gemini", "stored-gem")
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "env-gem-9999"}):
            self.assertEqual(app_settings.get_api_key("gemini"), "env-gem-9999")
            self.assertEqual(app_settings.key_source("gemini"), "env")

    def test_active_task_runner_matches_selected_provider(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            _clear_env()
            app_settings.set_api_key("openai", "k")
            app_settings.set_provider("openai")
            self.assertIsInstance(app_settings.active_task_runner(), OpenAITaskRunner)
            app_settings.set_api_key("gemini", "k")
            app_settings.set_provider("gemini")
            self.assertIsInstance(app_settings.active_task_runner(), GeminiTaskRunner)

    def test_active_task_runner_requires_key(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            _clear_env()
            app_settings.set_provider("anthropic")
            Setting.objects.all().delete()
            with self.assertRaises(RuntimeError):
                app_settings.active_task_runner()

    def test_status_lists_all_providers(self):
        s = app_settings.status()
        self.assertEqual({p["id"] for p in s["providers"]},
                         {"anthropic", "openai", "gemini"})


class LLMSettingsEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_get_lists_providers_and_selection(self):
        r = self.client.get("/api/settings/llm/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("provider", body)
        self.assertEqual(len(body["providers"]), 3)

    def test_put_selects_provider_and_sets_key_without_returning_it(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            _clear_env()
            r = self.client.put(
                "/api/settings/llm/",
                {"provider": "openai", "key": "sk-xyz-1234", "model": "gpt-4o"},
                format="json",
            )
            body = r.json()
            self.assertEqual(body["provider"], "openai")
            self.assertEqual(body["model"], "gpt-4o")
            self.assertTrue(body["configured"])
            self.assertNotIn("key", body)
            openai = next(p for p in body["providers"] if p["id"] == "openai")
            self.assertEqual(openai["hint"], "…1234")

    def test_put_unknown_provider_rejected(self):
        r = self.client.put("/api/settings/llm/", {"provider": "nope"}, format="json")
        self.assertEqual(r.status_code, 400)
