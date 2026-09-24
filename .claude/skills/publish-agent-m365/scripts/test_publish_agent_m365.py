"""Hermetic tests for publish_agent_m365.py.

No real Azure, network, or `az` calls are made - every external boundary
(`run_az`, `request`, `requests.get`, and `input`) is monkeypatched. Run with:

    python -m unittest discover -s .github/skills/publish-agent-m365/scripts -p "test_*.py"
    # or
    pytest .github/skills/publish-agent-m365/scripts/test_publish_agent_m365.py
"""
import importlib.util
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

# Import the script as a module by file path (it is not on the import path).
_SPEC = importlib.util.spec_from_file_location(
    "publish_agent_m365", Path(__file__).with_name("publish_agent_m365.py")
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def base_args(**overrides):
    """A Namespace pre-populated with every attribute resolve_config touches."""
    ns = Namespace(
        endpoint=None, agent_name=None, resource_group=None, bot_name=None,
        agent_display_name=None, publish_scope=None, app_version=None,
        short_description=None, full_description=None, developer_name=None,
        developer_website_url=None, privacy_url=None, terms_of_use_url=None,
        bot_arm_id=None, skip_bicep=False, force_bicep=False, force_patch=False,
        no_network_check=False, check_bot=False, scan_subscription=False,
        only="identity,bicep,patch,publish",
        no_prompt=False, dry_run=False,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class ScopeSchemeTests(unittest.TestCase):
    def test_scope_to_scheme_mapping(self):
        self.assertEqual(mod.SCOPE_TO_SCHEME["Shared"], "BotServiceRbac")
        self.assertEqual(mod.SCOPE_TO_SCHEME["Personal"], "BotServiceRbac")
        self.assertEqual(mod.SCOPE_TO_SCHEME["Tenant"], "BotServiceTenant")

    def test_every_scope_choice_has_a_scheme(self):
        for scope in ("Shared", "Personal", "Tenant"):
            self.assertIn(scope, mod.SCOPE_TO_SCHEME)


class AccountNameTests(unittest.TestCase):
    def test_derives_account_from_endpoint_host(self):
        a = base_args(endpoint="https://myfoundry.services.ai.azure.com/api/projects/proj1")
        self.assertEqual(mod.account_name(a), "myfoundry")

    def test_handles_trailing_path(self):
        a = base_args(endpoint="https://abc123.services.ai.azure.com/api/projects/p")
        self.assertEqual(mod.account_name(a), "abc123")


class PromptTests(unittest.TestCase):
    def test_supplied_flag_value_is_returned_without_prompting(self):
        a = base_args(no_prompt=False)
        with mock.patch("builtins.input", side_effect=AssertionError("should not prompt")):
            self.assertEqual(mod.prompt(a, "Label", "given"), "given")

    def test_no_prompt_uses_default(self):
        a = base_args(no_prompt=True)
        self.assertEqual(mod.prompt(a, "Label", None, default="dv"), "dv")

    def test_no_prompt_missing_required_exits(self):
        a = base_args(no_prompt=True)
        with self.assertRaises(SystemExit):
            mod.prompt(a, "Endpoint", None, required=True)

    def test_interactive_empty_falls_back_to_default(self):
        a = base_args(no_prompt=False)
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(mod.prompt(a, "Label", None, default="dv"), "dv")

    def test_interactive_rejects_empty_when_required(self):
        a = base_args(no_prompt=False)
        with mock.patch("builtins.input", side_effect=["", "  ", "real"]):
            self.assertEqual(mod.prompt(a, "Label", None, required=True), "real")

    def test_interactive_enforces_choices(self):
        a = base_args(no_prompt=False)
        with mock.patch("builtins.input", side_effect=["Nope", "Tenant"]):
            self.assertEqual(
                mod.prompt(a, "Scope", None, choices=["Shared", "Tenant"]), "Tenant")


class ResolveConfigTests(unittest.TestCase):
    def _full_flags(self, **extra):
        defaults = dict(
            no_prompt=True,
            endpoint="https://f.services.ai.azure.com/api/projects/p/",
            resource_group="rg-test",
            agent_name="MyAgent",
            publish_scope="Shared",
            app_version="1.2.3",
            short_description="short",
            full_description="full",
            developer_name="Dev",
            developer_website_url="https://dev.example",
            privacy_url="https://priv.example",
            terms_of_use_url="https://tou.example",
            bot_name="bot1",
        )
        defaults.update(extra)
        return base_args(**defaults)

    def test_all_flags_resolved_and_endpoint_trimmed(self):
        a = self._full_flags()
        mod.resolve_config(a, token="t")
        self.assertEqual(a.endpoint, "https://f.services.ai.azure.com/api/projects/p")  # trailing / stripped
        self.assertEqual(a.resource_group, "rg-test")
        self.assertEqual(a.agent_name, "MyAgent")
        self.assertEqual(a.agent_display_name, "MyAgent")  # defaults to agent name

    def test_missing_required_metadata_exits_under_no_prompt(self):
        a = self._full_flags(short_description=None)  # now required, no default
        with self.assertRaises(SystemExit):
            mod.resolve_config(a, token="t")

    def test_missing_endpoint_exits_under_no_prompt(self):
        a = self._full_flags(endpoint=None)
        with self.assertRaises(SystemExit):
            mod.resolve_config(a, token="t")

    def test_check_bot_skips_metadata_prompts(self):
        # With check_bot set, only endpoint/rg/agent are needed - metadata omitted.
        a = base_args(
            no_prompt=True, check_bot=True,
            endpoint="https://f.services.ai.azure.com/api/projects/p",
            resource_group="rg", agent_name="A",
        )
        mod.resolve_config(a, token="t")  # must not raise despite missing metadata
        self.assertEqual(a.agent_name, "A")


class PatchBodyTests(unittest.TestCase):
    def _capture_patch(self, scope):
        a = base_args(
            endpoint="https://f.services.ai.azure.com/api/projects/p",
            agent_name="MyAgent", publish_scope=scope, dry_run=False,
        )
        captured = {}

        def fake_request(method, url, token, dry_run, json_body=None, content_type=None):
            captured["method"] = method
            captured["url"] = url
            captured["body"] = json_body
            captured["content_type"] = content_type
            return {}

        with mock.patch.object(mod, "request", fake_request):
            mod.step_patch(a, token="t")
        return captured

    def test_shared_scope_uses_botservicerbac(self):
        cap = self._capture_patch("Shared")
        schemes = [s["type"] for s in cap["body"]["agent_endpoint"]["authorization_schemes"]]
        self.assertIn("Entra", schemes)
        self.assertIn("BotServiceRbac", schemes)
        self.assertEqual(cap["method"], "PATCH")
        self.assertEqual(cap["content_type"], "application/merge-patch+json")

    def test_tenant_scope_uses_botservicetenant(self):
        cap = self._capture_patch("Tenant")
        schemes = [s["type"] for s in cap["body"]["agent_endpoint"]["authorization_schemes"]]
        self.assertIn("BotServiceTenant", schemes)

    def test_patch_retains_responses_protocol_and_enables_endpoint(self):
        cap = self._capture_patch("Shared")
        proto = cap["body"]["agent_endpoint"]["protocol_configuration"]
        self.assertIn("responses", proto)  # PATCH replaces config - must re-send responses
        self.assertTrue(proto["activity"]["enable_m365_public_endpoint"])


class PublishBodyTests(unittest.TestCase):
    def test_publish_body_shape(self):
        a = base_args(
            endpoint="https://f.services.ai.azure.com/api/projects/p", agent_name="MyAgent",
            agent_display_name="My Agent", publish_scope="Shared", app_version="1.0.0",
            short_description="s", full_description="f", developer_name="Dev",
            developer_website_url="https://d", privacy_url="https://p", terms_of_use_url="https://t",
            dry_run=False,
        )
        captured = {}

        def fake_request(method, url, token, dry_run, json_body=None, content_type=None):
            captured["url"] = url
            captured["body"] = json_body
            return {"titleId": "T123"}

        with mock.patch.object(mod, "request", fake_request):
            mod.step_publish(a, token="t", bot_arm_id="/subscriptions/x/bot")

        body = captured["body"]
        self.assertEqual(body["botServiceArmId"], "/subscriptions/x/bot")
        self.assertEqual(body["publishScope"], "Shared")
        self.assertEqual(body["appVersion"], "1.0.0")
        self.assertEqual(body["agentDisplayName"], "My Agent")
        self.assertFalse(body["publishAsAutopilot"])
        self.assertIn("microsoft365/publish", captured["url"])

    def test_publish_without_bot_arm_id_exits(self):
        a = base_args(endpoint="https://f/api/projects/p", agent_name="A")
        with self.assertRaises(SystemExit):
            mod.step_publish(a, token="t", bot_arm_id=None)


class NetworkPostureTests(unittest.TestCase):
    def _posture_with(self, az_json):
        a = base_args(
            endpoint="https://acct.services.ai.azure.com/api/projects/p",
            resource_group="rg", dry_run=False,
        )
        with mock.patch.object(mod, "run_az", return_value=az_json):
            return mod.network_posture(a)

    def test_disabled_is_restricted(self):
        pna, restricted, resolved = self._posture_with('{"pna":"Disabled","ip":[],"vnet":[]}')
        self.assertEqual(pna, "Disabled")
        self.assertTrue(restricted)
        self.assertTrue(resolved)

    def test_enabled_with_no_rules_is_unrestricted(self):
        pna, restricted, resolved = self._posture_with('{"pna":"Enabled","ip":[],"vnet":[]}')
        self.assertEqual(pna, "Enabled")
        self.assertFalse(restricted)
        self.assertTrue(resolved)

    def test_enabled_with_ip_rules_is_restricted(self):
        _, restricted, _ = self._posture_with('{"pna":"Enabled","ip":[{"value":"1.2.3.4"}],"vnet":[]}')
        self.assertTrue(restricted)

    def test_unresolvable_defaults_to_restricted(self):
        pna, restricted, resolved = self._posture_with(None)
        self.assertIsNone(pna)
        self.assertTrue(restricted)   # safe default: assume PATCH required
        self.assertFalse(resolved)


class ListBotsScopeTests(unittest.TestCase):
    def _url_for(self, **overrides):
        kwargs = dict(resource_group="rg-test")
        kwargs.update(overrides)
        a = base_args(**kwargs)
        captured = {}

        def fake_run_az(args, dry_run=False, check=True):
            if args[:2] == ["account", "show"]:
                return "sub-123"
            captured["cmd"] = args
            return "[]"

        with mock.patch.object(mod, "run_az", fake_run_az):
            mod.list_bots(a)
        # The ARM URL is the value right after "--url".
        cmd = captured["cmd"]
        return cmd[cmd.index("--url") + 1]

    def test_default_scans_resource_group(self):
        url = self._url_for()
        self.assertIn("/resourceGroups/rg-test/", url)

    def test_scan_subscription_omits_resource_group(self):
        url = self._url_for(scan_subscription=True)
        self.assertNotIn("/resourceGroups/", url)
        self.assertIn("/subscriptions/sub-123/providers/Microsoft.BotService", url)

    def test_missing_resource_group_falls_back_to_subscription(self):
        url = self._url_for(resource_group=None)
        self.assertNotIn("/resourceGroups/", url)


class AgentPickerTests(unittest.TestCase):
    def test_supplied_agent_name_short_circuits(self):
        a = base_args(agent_name="Given")
        self.assertEqual(mod.choose_agent(a, token="t"), "Given")

    def test_picker_lists_and_selects_by_number(self):
        a = base_args(
            endpoint="https://f.services.ai.azure.com/api/projects/p", dry_run=False,
        )
        resp = mock.Mock(ok=True)
        resp.json.return_value = {"data": [{"name": "AgentOne"}, {"name": "AgentTwo"}]}
        with mock.patch.object(mod.requests, "get", return_value=resp), \
                mock.patch("builtins.input", return_value="2"):
            self.assertEqual(mod.choose_agent(a, token="t"), "AgentTwo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
