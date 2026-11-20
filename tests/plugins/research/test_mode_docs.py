"""The skill text for coordinator mode, automated mode and the two hosts (T08, C7 and C9).

Every check reads the shipped Markdown with its code marks removed and its whitespace collapsed, so a
re-wrap or a changed code mark does not break a sentence match. The host word that was removed from
the skill is built from two halves, so that this file does not name it.
"""

from __future__ import annotations

import re

from tests.conftest import REPO_ROOT

SKILL = REPO_ROOT / "plugins/research/skills/project"
MODES_WORD_CEILING = 1200
REMOVED_HOST = "co" + "dex"
KINDS = {
    "task-change": ("skip", "keep"),
    "replan": ("changed", "none"),
    "task-accept": ("accept", "reject"),
    "project-close": ("close", "continue"),
}


def raw(relative: str) -> str:
    return (SKILL / relative).read_text(encoding="utf-8")


def flat(relative: str) -> str:
    """The file without code marks and with whitespace collapsed."""
    return " ".join(raw(relative).replace("`", "").split())


def test_modes_exists_and_stays_within_its_word_ceiling() -> None:
    assert (SKILL / "references/modes.md").is_file()
    assert len(raw("references/modes.md").split()) <= MODES_WORD_CEILING


def test_skill_md_points_to_modes() -> None:
    assert (
        "At policy 6 read [modes.md](references/modes.md) before the first task: coordinator mode is on by "
        "default, so a fresh worker runs each task and task finish needs its recorded attempt."
    ) in flat("SKILL.md")


def test_task_workers_states_the_policy_six_stop() -> None:
    assert (
        "At policy 6 with coordinator on, a host that cannot start a fresh agent stops the work: checkpoint "
        "with stop_reason user-decision and ask the user to turn coordinator off with workflow settings. "
        "Do not continue inline."
    ) in flat("references/task-workers.md")


def test_task_workers_keeps_the_inline_fallback_for_policy_five_and_below() -> None:
    assert (
        "At policy 5 and below, if fresh context, observation, or stopping is unavailable or delegation is "
        "prohibited, continue inline; do not launch a detached CLI workaround."
    ) in flat("references/task-workers.md")


def test_the_worker_reference_names_the_claude_and_pi_start() -> None:
    text = raw("references/task-workers.md")
    assert "`Agent`" in text and "non-fork" in text
    for token in ("`pi-subagents`", "`subagent`", '`context: "fresh"`', "`subagents_enable`"):
        assert token in text, token
    assert 'subagent({ agent: "worker", task: "<assignment>", async: false, options: { context: "fresh" } })' in (
        " ".join(text.split())
    )
    assert 'subagent({ agent: "reviewer", task: "<persona and target paths>", async: false, options: {' in (
        " ".join(text.split())
    )
    assert "pi.dev/packages" not in text


def test_the_reviewer_reference_names_the_claude_and_pi_start() -> None:
    text = raw("references/goldfish-review.md")
    assert '(Claude: non-fork `Agent`; Pi: `subagent`, `context: "fresh"`)' in " ".join(text.split())
    assert "task-workers.md" in text
    assert len(text.split()) <= 300


def test_the_two_rewritten_references_do_not_name_the_removed_host() -> None:
    for relative in ("references/task-workers.md", "references/goldfish-review.md", "references/modes.md"):
        assert REMOVED_HOST not in raw(relative).lower(), relative


def test_task_workers_documents_the_persona_the_replan_link_and_the_policy_five_limit() -> None:
    text = flat("references/task-workers.md")
    assert "persona" in text and "worker-personas.md" in text
    assert "the worker reads" in text
    assert "modes.md" in text
    assert "## Delegating coordinator mode (policy 5 only)" in raw("references/task-workers.md")
    assert "At policy 6 the two warnings follow coordinator" in text


def test_modes_states_that_the_agent_never_writes_a_confirmation() -> None:
    assert "Never write your own words into a confirmation." in flat("references/modes.md")


def test_modes_states_the_split_rule_and_the_restart_rule() -> None:
    text = flat("references/modes.md")
    assert "Split a task that mixes file work with an external step." in text
    assert (
        "A restart is a new assignment: after task block and task start a fresh worker must complete the task."
    ) in text


def test_modes_states_the_cancel_and_maintenance_rule() -> None:
    assert (
        "With automated on, workflow cancel and workflow maintenance need the user's words in a confirmation."
    ) in flat("references/modes.md")


def test_modes_names_every_gated_kind_and_its_choice_pair() -> None:
    rows = raw("references/modes.md").splitlines()
    for kind, pair in KINDS.items():
        found = [row for row in rows if f"`{kind}`" in row and row.lstrip().startswith("|")]
        assert found, kind
        assert all(f"`{choice}`" in found[0] for choice in pair), (kind, found[0])


def test_modes_covers_the_two_options_the_replan_and_the_offer() -> None:
    text = flat("references/modes.md")
    for needle in (
        "coordinator", "automated", "--replan none", "--replan changed", "--replan-reason", "--start-next",
        "options: null", "automated: false", "execution-changes.md", "authorize", "evidence-round",
        "exhausted", "assessor", "task-accept", "stop_reason user-decision",
    ):
        assert needle in text, needle


def test_modes_is_reachable_from_the_documents_that_change_with_it() -> None:
    for relative in (
        "SKILL.md", "README.md", "references/task-workers.md", "references/review-risk.md",
        "references/automation.md", "references/automation-execution.md",
    ):
        assert re.search(r"\]\((?:references/)?modes\.md\)", raw(relative)), relative


def test_the_options_table_has_the_mode_options_and_the_refusals() -> None:
    text = flat("references/review-risk.md")
    assert "| coordinator | on | policy 6; off: task finish needs no worker attempt or replan |" in text
    assert "| automated | off | policy 6; needs coordinator |" in text
    assert "| delegating | off | policy 5 only; coordinator replaces it at policy 6 |" in text
    for message in (
        "options.coordinator needs policy_version 6",
        "options.automated needs policy_version 6",
        "options.delegating needs policy_version 5",
        "options.delegating ends at policy_version 5; this project is at policy_version 6",
        "options.automated needs options.coordinator",
    ):
        assert message in text, message


def test_the_command_references_name_the_new_keys() -> None:
    assert "--replan none|changed --replan-reason" in flat("references/commands.md")
    router = flat("references/automation.md")
    assert "| decide |" in router and "by agent" in router and "by user" in router
    assert "modes.md" in router
    assert router.count("confirmation") >= 3
    assert "records no worker event" in flat("references/automation-execution.md")


def test_stopping_and_schema_name_the_mode_facts() -> None:
    stopping = flat("references/stopping.md")
    assert "an exhausted automated decision, or a host with no fresh agents at policy 6" in stopping
    assert "move_detail" in stopping and "task start" in stopping
    schema = flat("references/workspace-schema.md")
    for needle in (
        "policy_version (0 to 6)", "coordinator", "automated", "automated_decisions", "mode_counters",
        "replan", "persona",
    ):
        assert needle in schema, needle


def test_the_skill_readme_has_a_modes_paragraph() -> None:
    text = raw("README.md")
    assert "## Modes" in text and "references/modes.md" in text
