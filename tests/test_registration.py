"""Does the plugin register the way Hermes will call it?"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from hermes_plugins import openalex as plugin
from hermes_plugins.openalex import config as config_mod
from hermes_plugins.openalex import schemas

PLUGIN_DIR = Path(plugin.__file__).parent


class FakeContext:
    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.skills = {}
        self.cli_commands = {}
        self.commands = {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None, emoji="", **kw):
        assert name not in self.tools, f"{name} registered twice"
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
        }

    def register_hook(self, hook_name, callback):
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_skill(self, name, path, description=""):
        assert Path(path).exists(), f"skill file missing: {path}"
        self.skills[name] = {"path": path, "description": description}

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands[name] = {"setup_fn": setup_fn, "handler_fn": handler_fn}

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = {"handler": handler, "args_hint": args_hint}


@pytest.fixture
def registered(isolated_config):
    ctx = FakeContext()
    plugin.register(ctx)
    return ctx


class TestRegistration:
    def test_all_nine_tools_register(self, registered):
        assert set(registered.tools) == config_mod.ALL_TOOLS
        assert len(registered.tools) == 9

    def test_every_tool_shares_one_toolset(self, registered):
        assert {t["toolset"] for t in registered.tools.values()} == {"openalex"}

    def test_schema_name_matches_registry_name(self, registered):
        for name, entry in registered.tools.items():
            assert entry["schema"]["name"] == name

    def test_schemas_are_well_formed(self, registered):
        for name, entry in registered.tools.items():
            schema = entry["schema"]
            assert schema["description"].strip(), name
            params = schema["parameters"]
            assert params["type"] == "object"
            for required in params.get("required", []):
                assert required in params["properties"], f"{name}: {required}"

    def test_lifecycle_hooks_and_surfaces(self, registered):
        assert "on_session_start" in registered.hooks
        assert "on_session_reset" in registered.hooks
        assert set(registered.skills) == {"query-syntax", "lit-review"}
        assert "openalex" in registered.cli_commands
        assert "openalex" in registered.commands


class TestVisibility:
    def test_core_profile_hides_the_expensive_tools(self, registered, isolated_config):
        config_mod.reset()
        assert registered.tools["openalex_count"]["check_fn"]() is True
        assert registered.tools["openalex_harvest"]["check_fn"]() is False
        assert registered.tools["openalex_classify"]["check_fn"]() is False

    def test_full_profile_reveals_harvest(self, registered, isolated_config):
        isolated_config["profile"] = "full"
        config_mod.reset()
        assert registered.tools["openalex_harvest"]["check_fn"]() is True

    def test_classify_needs_its_own_opt_in_beyond_the_profile(self, registered, isolated_config):
        """A tool that would always refuse should not be advertised."""
        isolated_config["profile"] = "full"
        config_mod.reset()
        assert registered.tools["openalex_classify"]["check_fn"]() is False

        isolated_config["budget"] = {"allow_text_classification": True}
        config_mod.reset()
        assert registered.tools["openalex_classify"]["check_fn"]() is True

    def test_the_cli_and_the_model_see_the_same_set(self, registered, isolated_config):
        """visible_tools is the single source of truth.

        The CLI prints cfg.visible_tools() while registration gates on
        check_fn. If those diverge, `hermes openalex profile` advertises tools
        the model cannot actually call.
        """
        for settings in (
            {},
            {"profile": "full"},
            {"profile": "full", "budget": {"allow_text_classification": True}},
            {"profile": "full", "tools": {"disabled": ["openalex_search"]}},
        ):
            isolated_config.clear()
            isolated_config.update(settings)
            config_mod.reset()
            from_check = {n for n, e in registered.tools.items() if e["check_fn"]()}
            from_config = config_mod.load(refresh=True).visible_tools()
            assert from_check == from_config, settings

    def test_everything_core_works_without_a_key(self, registered, isolated_config):
        """Unlike a keyless API, OpenAlex just gives you a smaller budget."""
        config_mod.reset()
        visible = {n for n, e in registered.tools.items() if e["check_fn"]()}
        assert visible == config_mod.CORE_TOOLS


class TestManifest:
    def _manifest(self):
        return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))

    def test_manifest_matches_the_code(self):
        manifest = self._manifest()
        assert manifest["name"] == "openalex"
        assert manifest["kind"] == "standalone"
        assert manifest["manifest_version"] == 1
        assert manifest["version"] == plugin.__version__

    def test_declared_tools_are_the_core_profile(self):
        assert set(self._manifest()["provides_tools"]) == config_mod.CORE_TOOLS

    def test_declared_hooks_are_registered(self, registered):
        assert set(self._manifest()["provides_hooks"]) <= set(registered.hooks)

    def test_the_key_is_declared_as_optional_in_its_description(self):
        entry = self._manifest()["requires_env"][0]
        assert entry["name"] == "OPENALEX_API_KEY"
        assert entry["secret"] is True
        assert "still runs" in entry["description"]


class TestSchemaGuidance:
    """The descriptions do the cost steering, so assert it survives edits."""

    def test_count_advertises_being_ten_times_cheaper(self):
        text = schemas.OPENALEX_COUNT["description"]
        assert "TEN TIMES CHEAPER" in text
        assert "openalex_search" in text

    def test_search_points_back_at_count(self):
        text = schemas.OPENALEX_SEARCH["description"]
        assert "openalex_count" in text
        assert "$0.001" in text

    def test_search_warns_about_the_fulltext_default(self):
        assert "FULL TEXT" in schemas.OPENALEX_SEARCH["description"]

    def test_free_tools_say_they_are_free(self):
        for schema in (schemas.OPENALEX_RESOLVE, schemas.OPENALEX_GET):
            assert "FREE" in schema["description"]

    def test_classify_leads_with_the_price(self):
        assert "$0.01 PER CALL" in schemas.OPENALEX_CLASSIFY["description"]


class TestSkillContent:
    @pytest.mark.parametrize("name", ["query-syntax", "lit-review"])
    def test_frontmatter_is_valid(self, name):
        text = (PLUGIN_DIR / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        front = yaml.safe_load(text.split("---", 2)[1])
        assert front["name"] == name
        assert len(front["description"]) <= 60, "Hermes truncates the index at 60 chars"
