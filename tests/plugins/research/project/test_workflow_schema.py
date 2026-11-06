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
from workspace_delivery import DELIVERY_OPERATIONS
from workspace_fields import ACTION_NESTED, NESTED_INPUTS
from workspace_lib import WorkspaceError, allocate_project

from tests.conftest import REPO_ROOT
from tests.plugins.research.project.test_workflow_automation import invoke

ACTIONS = sorted(workflows.FIELDS.keys() | workflows.READ_ACTIONS)
BIN = REPO_ROOT / "plugins/research/bin"
SKILL_SCRIPTS = REPO_ROOT / "plugins/research/skills/project/scripts"

# The variant key sets the runtime demands, written out here so the printed `required_by` is
# compared with an independent expectation rather than with the table that produced it.
WORKTREE_VARIANTS = {
    "operation=record": {"branch"},
    "operation=close": {"decision"},
    "operation=pull_request": {"host", "number", "url"},
    "operation=pull_request,exempt=no_remote": {"exempt"},
    "operation=pull_request,exempt=local_only": {"exempt"},
    "operation=alpha": {"alpha", "base", "files", "reason"},
    "operation=alpha,exempt=true": {"exempt", "reason"},
    "operation=release": {"version"},
    "operation=release,replaces_exemption=true": {"files", "replaces_exemption", "version"},
    "operation=merge": {"evidence", "merge_state", "merged_head", "method"},
}
CONFIRM_VARIANTS = {
    "kind=requirements": {"proposal_sha256"},
    "kind=planning": {"proposal_sha256"},
    "kind=architecture": {"proposal_sha256"},
    "kind=alignment": {"planning_sha256", "requirements_sha256"},
}


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
        assert len(ACTIONS) == 28

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
            required = set(workflows.input_schema(action)["required"])
            assert {"expected_revision", "id"} <= required
            assert "write action" in workflows.input_schema(action)["note"]
        assert "read action" in workflows.input_schema("resume")["note"]
        assert workflows.input_schema("freshness")["allowed"] == []


class TestRequiredBy:
    """`required_by` is the discovery the retro asked for: the keys per operation or kind."""

    def test_the_two_discriminated_actions_list_their_variants(self) -> None:
        assert set(workflows.input_schema("worktree")["required_by"]) == set(WORKTREE_VARIANTS)
        assert set(workflows.input_schema("confirm")["required_by"]) == set(CONFIRM_VARIANTS)

    @pytest.mark.parametrize("action, variants", (("worktree", WORKTREE_VARIANTS), ("confirm", CONFIRM_VARIANTS)))
    def test_each_variant_is_the_complete_sorted_key_set(self, action: str, variants: dict[str, set[str]]) -> None:
        schema = workflows.input_schema(action)
        universal = set(schema["required"])
        for name, extra in variants.items():
            assert schema["required_by"][name] == sorted(universal | extra), name
            assert set(schema["required_by"][name]) <= set(schema["allowed"])

    def test_worker_event_prints_every_event_with_all_its_fields_required(self) -> None:
        # 2026-11-06-001 friction: the schema listed the fields but not which were required or which
        # events exist, so a caller learned both from refusals.
        schema = workflows.input_schema("worker-event")
        fields = {"assignment_revision", "event", "handle", "observation", "scope", "task"}
        assert fields <= set(schema["required"])
        events = ("intent", "launched", "observed", "completed", "stopped", "failed")
        assert set(schema["required_by"]) == {f"event={name}" for name in events}
        for name in events:
            assert schema["required_by"][f"event={name}"] == schema["required"], name

    @pytest.mark.parametrize("action", ACTIONS)
    def test_every_other_action_prints_no_variants(self, action: str) -> None:
        if action not in ("worktree", "confirm", "goldfish", "worker-event"):
            assert workflows.input_schema(action)["required_by"] == {}

    def test_the_table_matches_the_vocabulary_the_runtime_accepts(self, project: Path) -> None:
        # The variant names are the discriminator values, so a renamed operation or kind cannot
        # linger in the printed schema while the runtime accepts something else.
        operations = {name.split("=", 1)[1].split(",")[0] for name in WORKTREE_VARIANTS}
        assert operations == DELIVERY_OPERATIONS | {"record", "close"}
        for name in CONFIRM_VARIANTS:
            kind = name.split("=", 1)[1]
            with pytest.raises(WorkspaceError) as caught:
                workflows.workflow(project, "confirm", {"id": f"probe-{kind}", "expected_revision": 0, "kind": kind})
            assert "confirmation kind must be" not in str(caught.value), kind


class TestNestedMap:
    """`--schema` prints each nested object an action reads, from the module the checker reads."""

    @pytest.mark.parametrize("action", ACTIONS)
    def test_the_nested_map_is_the_key_set_module(self, action: str) -> None:
        expected = {
            key: {"allowed": sorted(NESTED_INPUTS[shape][0]), "required": sorted(NESTED_INPUTS[shape][1])}
            for key, shape in ACTION_NESTED.get(action, ())
        }
        assert workflows.input_schema(action)["nested"] == expected

    def test_the_actions_with_a_nested_object_print_it(self) -> None:
        assert set(workflows.input_schema("worktree")["nested"]) == {"confirmation", "continuation"}
        assert workflows.input_schema("receipt")["nested"]["receipt"]["required"] == [
            "destination", "kind", "timestamp", "value",
        ]
        assert workflows.input_schema("resume")["nested"] == {}

    def test_authorize_accepts_a_continuation(self) -> None:
        schema = workflows.input_schema("authorize")
        assert "continuation" in schema["allowed"] and set(schema["nested"]) == {"authorization", "continuation"}

    def test_the_pull_request_refusal_names_the_exemption_once(self, project: Path) -> None:
        payload = {
            "id": "worktree-pr", "expected_revision": 0, "operation": "pull_request", "path": str(project),
            "confirmation": {"source": "s", "response": "r"},
        }
        with pytest.raises(WorkspaceError) as caught:
            workflows.workflow(project, "worktree", payload)
        message = str(caught.value)
        assert "['host', 'number', 'url']" in message
        assert message.count("exempt no_remote or local_only") == 1


class TestConfirmationRefusal:
    def test_a_missing_confirmation_names_the_field_the_schema_prints(self, project: Path) -> None:
        assert {"confirmation", "operation", "path"} <= set(workflows.input_schema("worktree")["required"])
        payload = {
            "id": "worktree-target", "expected_revision": 0, "operation": "record", "path": str(project),
            "branch": "feat/x",
        }
        with pytest.raises(WorkspaceError, match=r"^confirmation: expected a JSON object, got NoneType$"):
            workflows.workflow(project, "worktree", payload)

    def test_an_empty_confirmation_names_its_two_fields(self, project: Path) -> None:
        payload = {
            "id": "worktree-target", "expected_revision": 0, "operation": "record", "path": str(project),
            "branch": "feat/x", "confirmation": {},
        }
        with pytest.raises(WorkspaceError) as caught:
            workflows.workflow(project, "worktree", payload)
        message = str(caught.value)
        assert message.startswith("confirmation: missing required field(s)")
        assert "'response', 'source'" in message


class TestCombinedRefusal:
    def test_one_request_with_three_kinds_of_error_gets_one_message(self, project: Path) -> None:
        payload = {
            "id": "worktree-target", "expected_revision": 0, "operation": "record", "path": str(project),
            "bogus": 1, "confirmation": {},
        }
        with pytest.raises(WorkspaceError) as caught:
            workflows.workflow(project, "worktree", payload)
        groups = str(caught.value).split("\n")
        assert len(groups) == 3, groups
        assert "unknown field(s) ['bogus']" in groups[0]
        assert "missing required field(s) ['branch']" in groups[1]
        assert groups[2].startswith("confirmation: missing required field(s)")
        assert "'response', 'source'" in groups[2]


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


class TestKeySetsLiveInOneModule:
    def test_no_reader_holds_a_literal_copy_of_a_shared_key_set(self) -> None:
        import ast

        import workspace_fields as fields

        shared = {
            fields.CONTINUATION_FIELDS, fields.FINDING_FIELDS, fields.FINDING_REQUIRED, fields.WORKTREE_OPTIONAL_FIELDS,
        }
        shared |= set(fields.OPERATION_FIELDS.values()) | set(fields.OPERATION_REQUIRED.values())
        for allowed, required in fields.NESTED_INPUTS.values():
            shared |= {allowed, required}
        shared.discard(frozenset())

        copies = []
        for path in sorted(SKILL_SCRIPTS.glob("*.py")):
            if path.name == "workspace_fields.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                members: list[ast.expr] = []
                if isinstance(node, ast.Set):
                    members = node.elts
                elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("set", "frozenset") and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Set | ast.List | ast.Tuple):
                        members = arg.elts
                names = [item.value for item in members if isinstance(item, ast.Constant)]
                if members and len(names) == len(members) and frozenset(names) in shared:
                    copies.append(f"{path.name}:{getattr(node, 'lineno', 0)}")
        assert not copies, f"key sets copied outside workspace_fields: {copies}"


class TestRequiredFor:
    @pytest.mark.parametrize(
        ("operation", "request_keys", "entry", "expected"),
        [
            ("record", {}, None, {"branch"}),
            ("record", {"kind": "none"}, None, set()),
            ("close", {}, None, {"decision"}),
            ("pull_request", {}, None, {"host", "url", "number"}),
            ("pull_request", {"exempt": "local_only"}, None, set()),
            ("alpha", {"exempt": True}, None, {"reason"}),
            ("alpha", {}, None, {"base", "alpha", "reason"}),
            ("alpha", {}, {}, {"base", "alpha", "reason", "files"}),
            ("alpha", {}, {"version_line": {}}, {"base", "alpha", "reason"}),
            ("release", {}, None, {"version"}),
            ("release", {}, {}, {"version", "files"}),
            ("release", {}, {"version_line": {}}, {"version"}),
            ("release", {"replaces_exemption": True}, {"version_line": {}}, {"version", "files"}),
            ("merge", {"merge_state": "declined"}, None, {"merge_state"}),
            ("merge", {"merge_state": "merged"}, None, {"merge_state", "method", "evidence"}),
            ("merge", {}, None, {"merge_state"}),
            ("nonsense", {}, None, set()),
        ],
    )
    def test_each_variant_enforces_its_own_keys(
        self, operation: str, request_keys: dict[str, Any], entry: Any, expected: set[str]
    ) -> None:
        from workspace_delivery import required_for

        assert required_for(operation, request_keys, entry) == expected


class TestFieldErrors:
    def test_a_non_object_and_a_clean_object(self) -> None:
        from workspace_fields import field_errors

        assert field_errors([], {"a"}).not_object == "list"
        assert not field_errors({"a": 1}, {"a"}, {"a"})
        found = field_errors({"b": 1}, {"a"}, {"a"})
        assert (found.unknown, found.missing) == (["b"], ["a"])

    def test_a_list_item_and_a_non_list_are_labelled(self) -> None:
        shape = (("items[]", "triage_item"),)
        with pytest.raises(WorkspaceError) as caught:
            journal.object_input({"items": [1, {"section": "s"}]}, {"items"}, nested=shape)
        assert str(caught.value).split("\n")[0] == "items[0]: expected a JSON object, got int"
        assert "items[1]: missing required field(s) ['disposition', 'reason']" in str(caught.value)
        with pytest.raises(WorkspaceError, match=r"^items: expected a JSON object, got str$"):
            journal.object_input({"items": "x"}, {"items"}, nested=shape)

    def test_a_non_object_request_with_nested_shapes(self) -> None:
        with pytest.raises(WorkspaceError, match=r"^expected a JSON object, got list$"):
            journal.object_input([], {"a"}, nested=(("confirmation", "confirmation"),))


class TestOperationCodeKeepsItsOwnRefusals:
    """The workflow entry refuses a missing key first; the operation code still refuses it when called directly."""

    def test_the_first_record_needs_files(self) -> None:
        import workspace_delivery as delivery

        with pytest.raises(WorkspaceError, match="files are required for the first alpha or release record"):
            delivery._files({}, None)

    def test_a_merged_record_needs_a_method(self) -> None:
        import workspace_delivery as delivery

        entry = {"pr_exemption": {"reason": "local_only", "source": "s", "response": "r"}}
        with pytest.raises(WorkspaceError, match="method squash, merge or rebase"):
            delivery._merge(entry, {"merge_state": "merged"}, {"source": "s", "response": "r"})


class TestVariantKeysWithoutState:
    def test_an_unreadable_project_leaves_the_entry_dependent_key_unenforced(self, tmp_path: Path) -> None:
        (tmp_path / "project.json").write_text(json.dumps({"schema_version": 1}))
        request = {"operation": "alpha", "path": str(tmp_path)}
        assert workflows._variant_required(tmp_path, "worktree", request) == {"base", "alpha", "reason"}
