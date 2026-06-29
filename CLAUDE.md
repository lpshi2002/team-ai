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
