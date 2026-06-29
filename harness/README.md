# Team AI Harness

`team-ai-harness` is the reusable harness engine for project repositories. In
the private team workspace it lives at `team-ai/harness`.

It provides the `team-ai` CLI:

```bash
team-ai context "question"
team-ai context --mode implementation "request"
team-ai refresh
team-ai verify
team-ai guard
```

From the workspace root, create a connected project:

```bash
team-ai init project finance-architecture
```

When `shared/packs/finance-ai-ideation` exists, it is imported by default as
the seed idea/decision pack. Override it with `--from-pack other-pack`.

Project repositories import shared packs from `../team-ai/shared/packs/...`.
They should not copy this package under `tools/`.
