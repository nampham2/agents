"""A bad workflow request names what is wrong, and the schema a caller can ask for is the one enforced.

The input error used to be one sentence for a non-object, an unknown key and a missing key, and it never
named the offender; `workflow --help` listed 27 action names and no keys. These tests tie the printed
schema to what validation actually does, for every action, so the two cannot drift apart again.

New names are looked up when a test runs, so a build without the feature fails each test on its own.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import workspace_journal as journal
import workspace_workflows as workflows
from workspace_lib import WorkspaceError, allocate_project

from tests.conftest import REPO_ROOT
from tests.plugins.research.project.test_workflow_automation import invoke

ACTIONS = sorted(workflows.FIELDS.keys() | workflows.READ_ACTIONS)
BIN = REPO_ROOT / "plugins/research/bin"
SKILL_SCRIPTS = REPO_ROOT / "plugins/research/skills/project/scripts"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return allocate_project(tmp_path / "workspace", title="Schema", working_directory=target, create_root=True)


class TestInputHelpers:
    def test_an_unknown_key_is_named_with_what_is_allowed(self) -> None:
        with pytest.raises(WorkspaceError) as caught:
            journal.object_input({"bogus": 1, "a": 1}, {"a", "b"}, where="workflow demo")
        assert str(caught.value) == "workflow demo: unknown field(s) ['bogus']; allowed: ['a', 'b']"

    def test_a_missing_key_is_named_with_what_is_required(self) -> None:
        with pytest.raises(WorkspaceError) as caught:
            journal.object_input({"a": 1}, {"a", "b", "c"}, {"a", "b", "c"}, where="workflow demo")
        assert str(caught.value) == "workflow demo: missing required field(s) ['b', 'c']; required: ['a', 'b', 'c']"

    def test_a_non_object_says_what_it_was(self) -> None:
        for value, name in (([], "list"), ("x", "str"), (None, "NoneType"), (3, "int")):
            with pytest.raises(WorkspaceError, match=f"expected a JSON object, got {name}"):
                journal.object_input(value, {"a"})

    def test_without_a_location_the_message_has_no_prefix(self) -> None:
        with pytest.raises(WorkspaceError) as caught:
            journal.object_input({"x": 1}, {"a"})
        assert str(caught.value).startswith("unknown field(s)")

    def test_a_valid_object_is_returned_unchanged(self) -> None:
        value = {"a": 1}
        assert journal.object_input(value, {"a", "b"}, {"a"}) is value

    def test_string_and_list_inputs_name_the_field_when_given_one(self) -> None:
        with pytest.raises(WorkspaceError, match=r"^reflection must be non-empty text\Z"):
            journal.string_input("  ", "reflection")
        with pytest.raises(WorkspaceError, match=r"^expected non-empty text\Z"):
            journal.string_input(None)
        with pytest.raises(WorkspaceError, match=r"^items must be an array of at most 3 items\Z"):
            journal.list_input([1, 2, 3, 4], 3, "items")
        with pytest.raises(WorkspaceError, match=r"^expected an array of at most 3 items\Z"):
            journal.list_input("x", 3)
        assert journal.string_input("ok", "x") == "ok" and journal.list_input([1], 3, "x") == [1]


class TestWorkflowErrors:
    def test_an_unknown_key_names_the_action_and_the_key(self, project: Path) -> None:
        with pytest.raises(WorkspaceError, match=r"workflow resume: unknown field\(s\) \['bogus'\]"):
            workflows.workflow(project, "resume", {"bogus": 1})

    def test_a_missing_key_names_the_action_and_the_key(self, project: Path) -> None:
        expected = r"workflow checkpoint: missing required field\(s\) \['expected_revision'\]"
        with pytest.raises(WorkspaceError, match=expected):
            workflows.workflow(project, "checkpoint", {"id": "a"})

    def test_a_non_object_request_says_so(self, project: Path) -> None:
        bad: Any = []
        with pytest.raises(WorkspaceError, match="workflow resume: expected a JSON object, got list"):
            workflows.workflow(project, "resume", bad)

    def test_a_nested_check_names_its_own_location(self, project: Path) -> None:
        payload = {"id": "v", "checks": [{"task": "T01"}]}
        with pytest.raises(WorkspaceError, match=r"workflow verify check: missing required field\(s\) \['argv'\]"):
            workflows.workflow(project, "verify", payload)

    def test_an_empty_text_field_is_named(self, project: Path) -> None:
        payload = {
            "id": "c1", "expected_revision": 0, "tokens": {}, "kind": " ", "proposal_sha256": "x",
            "review_id": "R", "response": "r", "source": "s", "scope": "c",
        }
        with pytest.raises(WorkspaceError, match="kind must be non-empty text"):
            workflows.workflow(project, "confirm", payload)

    def test_an_unknown_action_is_still_a_clear_error(self, project: Path) -> None:
        with pytest.raises(WorkspaceError, match="unknown workflow action: nope"):
            workflows.workflow(project, "nope", {})


class TestSchemaTable:
    def test_every_action_has_exactly_one_schema(self) -> None:
        assert set(ACTIONS) == workflows.FIELDS.keys() | journal.READ_INPUTS.keys()
        assert workflows.READ_ACTIONS == set(journal.READ_INPUTS), "read actions are derived from the table"
        assert len(ACTIONS) == 27

    @pytest.mark.parametrize("action", ACTIONS)
    def test_the_printed_schema_is_what_validation_enforces(self, project: Path, action: str) -> None:
        schema = workflows.input_schema(action)
        with pytest.raises(WorkspaceError) as caught:
            workflows.workflow(project, action, {"__bogus__": 1})
        message = str(caught.value)
        assert message.startswith(f"workflow {action}: "), message
        assert f"unknown field(s) ['__bogus__']; allowed: {schema['allowed']}" in message

    @pytest.mark.parametrize("action", ACTIONS)
    def test_required_keys_are_allowed_keys_and_both_are_sorted(self, action: str) -> None:
        schema = workflows.input_schema(action)
        assert set(schema["required"]) <= set(schema["allowed"])
        assert schema["allowed"] == sorted(schema["allowed"]) and schema["required"] == sorted(schema["required"])
        assert schema["action"] == action and schema["note"]

    def test_write_actions_all_require_an_id_and_a_revision_and_read_actions_say_so(self) -> None:
        for action in workflows.FIELDS:
            assert workflows.input_schema(action)["required"] == ["expected_revision", "id"]
            assert "write action" in workflows.input_schema(action)["note"]
        assert "read action" in workflows.input_schema("resume")["note"]
        assert workflows.input_schema("freshness")["allowed"] == []


class TestCommandLine:
    def test_schema_prints_the_keys_and_reads_no_project(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-such-project"
        for action in ("resume", "checkpoint", "freshness"):
            code, stdout, _ = invoke(["workflow", str(missing), action, "--schema"])
            assert code == 0
            assert json.loads(stdout) == workflows.input_schema(action)
        assert not missing.exists()

    def test_a_workflow_without_input_or_schema_says_what_to_pass(self, project: Path) -> None:
        code, _, stderr = invoke(["workflow", str(project), "resume"])
        assert code == 1 and "needs an input file" in stderr and "--schema" in stderr

    @pytest.mark.parametrize("surface", [BIN, SKILL_SCRIPTS])
    @pytest.mark.parametrize("tool", ["research-project", "research-validate"])
    def test_help_names_the_command_not_the_implementation_module(self, surface: Path, tool: str) -> None:
        result = subprocess.run([str(surface / tool), "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert result.stdout.startswith(f"usage: {tool}"), result.stdout[:80]
        assert "manage_workspace.py" not in result.stdout and "validate_workspace.py" not in result.stdout

    @pytest.mark.parametrize("surface", [BIN, SKILL_SCRIPTS])
    def test_a_subcommand_error_and_usage_carry_the_command_name(self, surface: Path) -> None:
        bad = subprocess.run([str(surface / "research-project"), "workflow"], capture_output=True, text=True)
        assert bad.returncode == 2 and "research-project workflow: error" in bad.stderr
        usage = subprocess.run(
            [str(surface / "research-project"), "record-evidence", "--help"], capture_output=True, text=True
        )
        assert "usage: research-project record-evidence" in usage.stdout
