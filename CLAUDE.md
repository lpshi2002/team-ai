# Claude Entry Point

Follow `AGENTS.md` first.

This private workspace contains:

- `harness/`: coworker harness CLI/runtime.
- `shared/`: private source, memory, decision, and state packs.

Use the installed CLI from `harness/` when working with project repos:

```bash
./install.sh
team-ai context "USER_QUESTION"
team-ai context --mode implementation "USER_REQUEST"
```

In this repo root, `team-ai context "회의 때 뭐 나왔지?"` should recall the
finance ideation pack. Use harness context only when the user explicitly asks
about team-ai CLI, guard, verify, schema, runtime, or scope rules.
