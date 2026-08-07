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
    def test_every_tool_registers(self, registered):
        assert set(registered.tools) == config_mod.ALL_TOOLS
        assert len(registered.tools) == 10

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


def _catalog_line(description: str, max_chars: int = 60) -> str:
    """Replicate Hermes's deferred-tool catalog rendering.

    Mirrors tools/tool_search.py::_short_desc, which takes the first sentence
    and clips it to 60 characters. Plugin tools are deferred behind
    tool_search, so this one line is all the model sees until it calls
    tool_describe. If the price does not fit here, the cost steering is
    invisible at exactly the moment the model is choosing a tool.
    """
    import re

    text = " ".join((description or "").split())
    match = re.search(r"[.!?](\s|$)", text)
    if match:
        text = text[: match.start() + 1]
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(",;: ") + "\u2026"


class TestCatalogLine:
    """What the model sees before it ever calls tool_describe."""

    FREE = ("openalex_resolve", "openalex_get", "openalex_fields", "openalex_account")
    PRICED = (
        "openalex_count",
        "openalex_search",
        "openalex_related",
        "openalex_harvest",
        "openalex_classify",
    )

    def test_no_opener_is_clipped(self):
        """A clipped opener loses the half of the sentence that carries meaning."""
        for name, schema in schemas.ALL_SCHEMAS.items():
            line = _catalog_line(schema["description"])
            assert not line.endswith("\u2026"), f"{name}: {line!r}"

    def test_no_opener_is_broken_by_a_decimal_price(self):
        """The sentence splitter treats the dot in $0.0001 as a full stop.

        An opener of "Traverse the citation graph, for $0.0001." rendered as
        "...for $0." in the catalog, which reads as free.
        """
        for name, schema in schemas.ALL_SCHEMAS.items():
            line = _catalog_line(schema["description"])
            assert not line.rstrip(".").endswith("$0"), f"{name}: {line!r}"

    def test_every_opener_signals_its_cost(self):
        for name in self.FREE:
            line = _catalog_line(schemas.ALL_SCHEMAS[name]["description"])
            assert "FREE" in line.upper(), f"{name}: {line!r}"
        for name in self.PRICED:
            line = _catalog_line(schemas.ALL_SCHEMAS[name]["description"])
            assert any(
                word in line.lower() for word in ("cheap", "price", "cost", "expensive", "billed")
            ), f"{name}: {line!r}"

    def test_the_two_that_compete_point_at_each_other(self):
        """count and search answer overlapping questions at a 10x price gap."""
        assert "cheaper than a search" in _catalog_line(schemas.OPENALEX_COUNT["description"])
        assert "ten times the price of a count" in _catalog_line(
            schemas.OPENALEX_SEARCH["description"]
        )


class TestSchemaGuidance:
    """The descriptions do the cost steering, so assert it survives edits."""

    def test_count_advertises_being_cheaper_than_search(self):
        text = schemas.OPENALEX_COUNT["description"]
        assert "openalex_search" in text
        assert "$0.0001" in text

    def test_the_skills_are_discoverable_from_the_tools(self):
        """Plugin skills are not in the system prompt index.

        skill_view can only load them by name, so the tool descriptions are
        the only place the model can learn those names exist.
        """
        blob = " ".join(str(schema) for schema in schemas.ALL_SCHEMAS.values())
        assert "openalex:query-syntax" in blob
        assert "openalex:lit-review" in blob

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
