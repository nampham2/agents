# Promote, compact and retire lessons

Read this when closing a project that promotes, compacts or retires a lesson topic. Reviewing and
staging lessons are in [memory-operations.md](memory-operations.md).

Prefer an existing topic:

```sh
research-project promote-memory <slug> --workspace-root <root> --body-file <lesson.md> \
  --source <project-id> [--keywords "term, term"]
```

A new topic also needs `--create`, `--description`, `--kind preference|environment|method`
and `--scope`. Without `--create`, unknown slugs return nearest topics. Promotion appends an
incident, merges sources and rebuilds indexes under locks; heed the 85% headroom warning.

Promotion does not update the rule. When supported evidence corrects it, or compaction is due:

```sh
research-project read-memory <slug> --workspace-root <root> --full
research-project compact-memory <slug> --workspace-root <root> --rule-file <rule.md> \
  --expected-sha256 <token> --keywords "<incident vocabulary>"
```

Keep rules under 2 KB and retain distinctive incident keywords. The old rule becomes an incident.
`retire-memory <slug> [--superseded-by <slug>]` preserves history; `--reactivate` reverses it.
Load [memory-architecture.md](memory-architecture.md) only for format/migration/validation repairs.
