# Team AI

Private team workspace for the Team AI Harness and shared packs.

Give this repo link to a teammate's AI and ask it to install the coworker.
It works from Codex or Claude Code; both use the same `team-ai` CLI.

Onboard a teammate:

```bash
git clone https://github.com/lpshi2002/team-ai.git
cd team-ai
./install.sh
command -v pdftotext || brew install poppler
```

Put shared meeting notes or PDFs in
`shared/packs/finance-ai-ideation/sources/meetings/`, then ask the agent:

```text
메모리 최신화 해줘
```

The agent should draft memory upserts, ask for approval, and run
`team-ai refresh` after approval. Recall works after the shared sources and
approved memory updates are present; a bare clone only installs the harness.

Then start the agent you use from this repo root:

```bash
codex   # Codex
claude  # Claude Code
```

Codex reads `AGENTS.md`. Claude reads `CLAUDE.md`, which points back to the
same agent rules in `AGENTS.md`.

```text
team-ai/
  .team-ai/ # root knowledge workspace config
  harness/  # CLI/runtime/tests
  shared/   # private packs: sources, memory, state
```

Project repos import packs from this repo's `shared/packs/...`; generated
paths adapt to the clone folder name.

From this repo root, Codex or Claude can answer finance ideation recall questions:

```bash
team-ai context "회의 때 뭔 얘기 나왔지?"
team-ai context --current-state "지금 목표와 다음 할 일은?"
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
