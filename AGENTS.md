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

## Answer Routing

- For recall questions about meetings, memories, past decisions, or source evidence, run `team-ai context "USER_REQUEST"` first and answer from retrieved memories and sources. Keep AI opinion out unless the user asks for it.
- For judgment, recommendation, project direction, or idea-selection questions, form an independent AI view before loading team context, then run `team-ai context "USER_REQUEST"` and answer by separating independent judgment, team-context fit, disagreements, and next checks.
- For challenge, risk, or "argue against this" questions, load the relevant context and foreground weaknesses, counterarguments, and uncertainty. Do not treat retrieved team preference as proof.
- For implementation or harness-runtime questions, run `team-ai context --mode implementation "team-ai CLI/harness USER_REQUEST"` before changing harness code.

## Commands

When asked to install this repo for a teammate, run:

```bash
./install.sh
```

Before changing harness code, run:

```bash
team-ai context --mode implementation "team-ai CLI/harness USER_REQUEST"
```

Create a connected project from this repo root or its parent with:

```bash
team-ai init project PROJECT_NAME --idea "selected idea"
```
