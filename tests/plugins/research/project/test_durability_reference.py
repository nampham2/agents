"""The durability features are documented where a fresh agent reaches them (project 2026-11-06-002)."""

from pathlib import Path

REFERENCES = Path(__file__).resolve().parents[4] / "plugins/research/skills/project/references"


def read(name: str) -> str:
    return (REFERENCES / name).read_text(encoding="utf-8")


def test_the_durability_reference_covers_every_feature() -> None:
    text = read("durability.md")
    for phrase in (
        "workflow <project-dir> effect -",
        '"operation":"resolve"',
        "may have run",
        "stop_reason: blocked",
        "task block",
        "research-project unlock <path> --workspace-root <root>",
        "silent_minutes",
        "event: observed",
        "workflow <project-dir> wait -",
        "wait due",
        '"reuses": NN',
        "accepted-residual",
        "durability_warnings",
    ):
        assert phrase in text, phrase


def test_each_feature_is_reachable_from_the_documents_that_route_to_it() -> None:
    assert "[Durability](durability.md)" in read("automation.md")
    assert "`reuses` ([durability.md](durability.md))" in read("goldfish-review.md")
    assert "(`triage` `reflection`)" in read("memory-operations.md")
    assert "[durability.md](durability.md)" in read("stopping.md")
    assert "[durability.md](durability.md)" in read("task-workers.md")
