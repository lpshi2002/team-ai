# Team AI

Private team workspace for the Team AI Harness and shared packs.

Give this repo link to a teammate's AI and ask it to install the coworker.
After cloning, the only required command is:

```bash
./install.sh
```

```text
team-ai/
  harness/  # CLI/runtime/tests
  shared/   # private packs: sources, memory, state
```

Install the CLI from `harness/`:

```bash
./install.sh
```

Project repos import packs from this repo's `shared/packs/...`; generated
paths adapt to the clone folder name.

Create a connected project from the workspace root:

```bash
team-ai init project finance-architecture
cd finance-architecture
team-ai context "현재 프로젝트 상태 요약"
```

If `shared/packs/finance-ai-ideation` exists, new projects import it by default
so the idea corpus, decisions, and direction stay available. Use another seed
pack when needed:

```bash
team-ai init project finance-architecture --from-pack other-pack
```
