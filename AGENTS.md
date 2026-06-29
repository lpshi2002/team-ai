<!-- vowline:start -->
Always use the skill `vowline` consistently, including for all sub-agents.
<!-- vowline:end -->

# Agent Rules

This is the private AI coworker workspace.

## Layout

- `harness/`: CLI/runtime/tests for the coworker harness.
- `shared/`: private packs with sources, memory, decisions, and state.
- `.team-ai/`: root knowledge workspace config for finance ideation recall.

## Boundaries

- Keep runtime code in `harness/`.
- Keep durable team/project knowledge in `shared/packs/`.
- Do not copy harness code into project repos.
- Do not commit local DBs, caches, generated compiled snapshots, secrets, or `.old-git/`.
- Default recall questions use finance ideation material, not harness internals.
- Use harness context only for explicit team-ai CLI, guard, verify, schema, runtime, or scope-rule questions.

## Commands

When asked to install this repo for a teammate, run:

```bash
./install.sh
```

Before answering meeting, memory, decision, project direction, or idea questions, run:

```bash
team-ai context "USER_REQUEST"
```

Before changing harness code, run:

```bash
team-ai context --mode implementation "team-ai CLI/harness USER_REQUEST"
```

Create a connected project from this repo root or its parent with:

```bash
team-ai init project PROJECT_NAME --idea "selected idea"
```
