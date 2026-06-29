# Team AI

Private team workspace for the Team AI Harness and shared packs.

Give this repo link to a teammate's AI and ask it to install the coworker.
After cloning, the only required command is:

```bash
./install.sh
codex
```

```text
team-ai/
  .team-ai/ # root knowledge workspace config
  harness/  # CLI/runtime/tests
  shared/   # private packs: sources, memory, state
```

Install the CLI from `harness/`:

```bash
./install.sh
```

Project repos import packs from this repo's `shared/packs/...`; generated
paths adapt to the clone folder name.

From this repo root, Codex can answer finance ideation recall questions:

```bash
team-ai context "회의 때 뭔 얘기 나왔지?"
```

Create a connected project from the workspace root:

```bash
team-ai init project fraud-signal-lab \
  --from-pack finance-ai-ideation \
  --idea "financial fraud detection and prevention"
cd fraud-signal-lab
team-ai context "현재 프로젝트 상태 요약"
```

If `shared/packs/finance-ai-ideation` exists, new projects import it by default
so the idea corpus, decisions, and direction stay available. Use another seed
pack when needed:

```bash
team-ai init project finance-architecture --from-pack other-pack --decision DEC-FIN-0001
```
