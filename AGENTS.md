<!-- vowline:start -->
Always use the skill `vowline` consistently, including for all sub-agents.
<!-- vowline:end -->

# Agent Rules

This is the private AI coworker workspace.

## Layout

- `harness/`: CLI/runtime/tests for the coworker harness.
- `shared/`: private packs with sources, memory, decisions, and state.

## Boundaries

- Keep runtime code in `harness/`.
- Keep durable team/project knowledge in `shared/packs/`.
- Do not copy harness code into project repos.
- Do not commit local DBs, caches, generated compiled snapshots, secrets, or `.old-git/`.

## Commands

When asked to install this repo for a teammate, run:

```bash
./install.sh
```

Create a connected project from this repo root or its parent with:

```bash
team-ai init project PROJECT_NAME
```
