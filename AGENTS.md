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

## Memory Curation

- Summarize one meeting issue per memory in one sentence. Keep source-backed context, decisions, open questions, and explicit user corrections; do not add criteria or claims inferred by the AI.
- Use `canonical_key` as `scope:YYYY-MM-DD:topic`, for example `program:finance-ai-ideation:2026-06-29:fraud-detection-priority`.
- Promote a memory only when it is likely to change future answer quality, preserve team direction, or prevent repeated decisions. Drop raw detail, duplicate phrasing, weak guesses, and items already recoverable by ordinary source search.
- Use person or authority prefixes for authored ids, such as `MEM-AUTH-CHO-0001`. Merge and supersede by `canonical_key`; the memory id is not the merge key.
- Example only, not active memory: `{"id":"MEM-AUTH-EXAMPLE-0001","kind":"decision","scope":"program:finance-ai-ideation","canonical_key":"program:finance-ai-ideation:2026-06-29:fraud-detection-priority","content":"Fraud detection was discussed as a possible finance direction, but it was not promoted to a committed project choice.","authority":"user_explicit","origin":"source","status":"active","source_refs":["pack://finance-ai-ideation/sources/meetings/2026.06.29 meeting.md#page=1"]}`
- Example only, not active memory: `{"id":"MEM-AUTH-EXAMPLE-0002","kind":"principle","scope":"harness:team-ai","canonical_key":"harness:team-ai:2026-06-30:curation-source-discipline","content":"Curated memory preserves source-backed context and explicit corrections without adding new AI-inferred criteria.","authority":"user_explicit","origin":"user","status":"active","source_refs":[]}`

## Memory Update Trigger

- When new files appear under `shared/packs/*/sources/meetings/`, or the user asks to refresh/update memory, draft `memory/events.jsonl` upserts using MEM-AUTH-HARNESS-0017 and the curation rules above, ask for user approval, then run `team-ai refresh`.
- Do not commit private live memory, local DBs, caches, compiled snapshots, or unapproved memory events.

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
