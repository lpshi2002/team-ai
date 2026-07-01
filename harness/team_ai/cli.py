from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .context import main as context_main
from .guard import guard
from .runtime import refresh
from .verify import verify_done


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "context":
        return context_main(argv[1:])
    parser = argparse.ArgumentParser(prog="team-ai")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("context", help="Load context for a repository question.")
    sub.add_parser("refresh", help="Refresh local source and memory indexes.")
    sub.add_parser("verify", help="Run configured project verification.")
    sub.add_parser("guard", help="Run harness guard checks.")
    init = sub.add_parser("init", help="Initialize a connected project.")
    init_sub = init.add_subparsers(dest="init_command", required=True)
    project = init_sub.add_parser("project", help="Create a project folder wired to this workspace harness.")
    project.add_argument("name")
    project.add_argument("--from-pack", help="Seed the project from a shared idea/decision pack.")
    project.add_argument("--idea", help="Seed the project with a selected idea direction.")
    project.add_argument("--decision", help="Seed the project from a decision id.")
    project.add_argument("--goal", help="Override the generated initial goal.")
    args = parser.parse_args(argv)

    if args.command == "context":
        return context_main([])
    if args.command == "init" and args.init_command == "project":
        return _init_project(Path("."), args.name, args.from_pack, args.idea, args.decision, args.goal)
    if args.command == "refresh":
        print(json.dumps(refresh("."), ensure_ascii=False, indent=2))
        return 0
    if args.command == "verify":
        result = verify_done(".")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["done"] else 1
    result = guard(".")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def _init_project(
    workspace: Path,
    name: str,
    from_pack: str | None = None,
    idea: str | None = None,
    decision: str | None = None,
    goal: str | None = None,
) -> int:
    workspace = workspace.resolve()
    coworker = _coworker_root(workspace)
    project_parent = workspace.parent if workspace == coworker else workspace
    root = project_parent / name
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"project already exists and is not empty: {root}")
    shared_packs = _relpath(coworker / "shared" / "packs", root)
    seed_pack = _seed_pack(coworker, root, from_pack)
    package = name.replace("-", "_")
    for path in (
        root / ".team-ai" / "sources" / "meetings",
        root / ".team-ai" / "sources" / "references",
        root / ".team-ai" / "sources" / "notes",
        root / ".team-ai" / "sources" / "uploads",
        root / ".team-ai" / "memory",
        root / ".team-ai" / "decisions",
        root / "src" / package,
        root / "tests",
    ):
        path.mkdir(parents=True, exist_ok=True)
    files = {
        root / ".team-ai" / "project.yaml": _project_yaml(name, shared_packs, seed_pack),
        root / ".team-ai" / "state.yaml": _state_yaml(name, seed_pack, idea, decision, goal),
        root / ".team-ai" / "config.yaml": _config_yaml(name, shared_packs),
        root / ".team-ai" / "memory" / "curated.jsonl": "",
        root / "src" / package / "__init__.py": "",
        root / "tests" / "test_import.py": _test_import_py(package),
        root / "AGENTS.md": _agents_md(),
        root / "README.md": _project_readme(name, shared_packs, seed_pack),
        root / ".gitignore": _gitignore(),
        root / "pyproject.toml": _pyproject_toml(name, package),
    }
    for path, text in files.items():
        if not path.exists():
            path.write_text(text, encoding="utf-8")
    if seed_pack and (idea or decision):
        decision_path = root / ".team-ai" / "decisions" / "DEC-INIT-0001.md"
        if not decision_path.exists():
            decision_path.write_text(_seed_decision_md(name, seed_pack, idea, decision, goal), encoding="utf-8")
    print(f"created {root}")
    return 0


def _coworker_root(workspace: Path) -> Path:
    candidates = (
        workspace,
        workspace / "team-ai",
        workspace / "ai-coworker",
        Path(__file__).resolve().parents[2],
    )
    for candidate in candidates:
        if (candidate / "harness").exists() and (candidate / "shared" / "packs").exists():
            return candidate.resolve()
    raise SystemExit("run from the coworker repo root or its parent")


def _relpath(path: Path, start: Path) -> str:
    return os.path.relpath(path, start).replace(os.sep, "/")


def _seed_pack(coworker: Path, project_root: Path, requested: str | None) -> dict[str, str] | None:
    packs = coworker / "shared" / "packs"
    name = requested or "finance-ai-ideation"
    path = packs / name
    if requested is None and not path.exists():
        return None
    if not path.exists():
        raise SystemExit(f"shared pack not found: {name}")
    return {"name": name, "scope": _pack_scope(path, name), "path": _relpath(path, project_root)}


def _pack_scope(path: Path, name: str) -> str:
    pack_yaml = path / "pack.yaml"
    if pack_yaml.exists():
        for line in pack_yaml.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("scope:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    return f"program:{name}"


def _project_yaml(name: str, shared_packs: str, seed_pack: dict[str, str] | None = None) -> str:
    scope = f"project:{name}"
    seed_import = ""
    seed_scope = ""
    if seed_pack:
        seed_import = f"""  - scope: {seed_pack["scope"]}
    path: {seed_pack["path"]}
    required: false
"""
        seed_scope = f"      - {seed_pack['scope']}\n"
    return f"""project_id: {name}
scope: {scope}

harness:
  required_version: ">=0.1.0"
  mode: project_local

default_write_scope: {scope}

imports:
  - scope: org:jo
    path: {shared_packs}/org-jo
    required: false
  - scope: harness:team-ai
    path: {shared_packs}/harness-team-ai
    required: false
{seed_import}
scope_modes:
  normal:
    include:
      - {scope}
      - org:jo
{seed_scope}    exclude_other_projects: true
  implementation:
    include:
      - {scope}
      - org:jo
{seed_scope}    exclude_other_projects: true
  harness:
    include:
      - org:jo
      - harness:team-ai
    exclude_project_sources_by_default: true
  compare_projects:
    require_explicit_user_intent: true

source_scope_rules:
  - path: .team-ai/project.yaml
    scope: {scope}
  - path: .team-ai/state.yaml
    scope: {scope}
  - path: .team-ai/decisions/**
    scope: {scope}
  - path: .team-ai/memory/**
    scope: {scope}
  - path: .team-ai/sources/**
    scope: {scope}
  - path: src/**
    scope: {scope}
  - path: tests/**
    scope: {scope}
"""


def _state_yaml(
    name: str,
    seed_pack: dict[str, str] | None = None,
    idea: str | None = None,
    decision: str | None = None,
    goal: str | None = None,
) -> str:
    seed = ""
    if seed_pack:
        selected = ""
        if idea or decision:
            selected = f"""
selected_direction:
  idea: {idea or "null"}
  decision: {decision or "null"}
  source_pack: {seed_pack["name"]}
"""
        seed = f"""
seed_context:
  pack: {seed_pack["name"]}
  scope: {seed_pack["scope"]}
  path: {seed_pack["path"]}
{selected}"""
    subject = idea or decision or "the first project direction"
    current_goal = goal or f"Build the first implementation slice for {subject}."
    return f"""project_id: {name}
status: initial
current_goal: >
  {current_goal}
{seed}
next_actions:
  - define the first concrete user workflow
  - identify evidence sources needed for a credible demo
  - create the smallest testable implementation slice
"""


def _config_yaml(name: str, shared_packs: str) -> str:
    return f"""source_roots:
  - .team-ai/sources
  - .team-ai/project.yaml
  - .team-ai/decisions
  - .team-ai/state.yaml

project_config: .team-ai/project.yaml

shared_pack_roots:
  - {shared_packs}

scope_inference:
  - path: .team-ai/**
    scope: project:{name}
  - path: src/**
    scope: project:{name}
  - path: tests/**
    scope: project:{name}

verification:
  commands:
    - PYTHONPATH=src python -m unittest discover -s tests
"""


def _agents_md() -> str:
    return """# Agent Rules

Before answering questions about prior meetings, memories, decisions, project
direction, or implementation direction, run:

```bash
team-ai context "USER_REQUEST"
```

Before changing product code under `src/` or `tests/`, run:

```bash
team-ai context --mode implementation "USER_REQUEST"
```

Treat "회의 때 뭐 나왔지", "지난번에 뭐라고 했지", "기억 가져와줘",
"결정 내용이 뭐야", and "이 아이디어로 만들어줘" as project knowledge
recall requests.

Only use harness context when the user explicitly asks about team-ai harness,
CLI behavior, guard, verify, memory schema, AGENTS.md mechanics, project.yaml
scope rules, runtime, or compiled snapshots.

- Active project is defined in `.team-ai/project.yaml`.
- Raw materials and meeting notes live under `.team-ai/sources/`.
- Approved durable memories live under `.team-ai/memory/`.
- Product code belongs under `src/`; tests belong under `tests/`.
- Before finalizing implementation changes, run `team-ai guard`.
"""


def _project_readme(name: str, shared_packs: str, seed_pack: dict[str, str] | None = None) -> str:
    seed = ""
    if seed_pack:
        seed = f"""
Seed idea/decision pack:

```text
{seed_pack["path"]} ({seed_pack["scope"]})
```
"""
    return f"""# {name}

This project is wired to the sibling Team AI workspace:

```text
workspace/
  coworker repo/
  {name}/
```

Shared packs:

```text
{shared_packs}
```

From this project root:

```bash
team-ai context "현재 프로젝트 상태 요약"
team-ai context --mode implementation "MVP 구조 구현"
team-ai guard
```
{seed}"""


def _gitignore() -> str:
    return """.DS_Store
__pycache__/
*.py[cod]
.venv/
.env
.env.*
*.sqlite
*.sqlite3
*.db
.team-ai/local/
.team-ai/compiled/
.team-ai/cache/
.team-ai/tmp/
.team-ai/logs/
output/
outputs/
"""


def _seed_decision_md(
    name: str,
    seed_pack: dict[str, str],
    idea: str | None,
    decision: str | None,
    goal: str | None,
) -> str:
    direction = idea or decision or "selected seed direction"
    source_refs = ""
    if decision:
        source_refs = f"""source_refs:
  - pack://{seed_pack["name"]}/decisions/{decision}.md
"""
    return f"""---
id: DEC-INIT-0001
title: Seed project from finance ideation direction
status: active
authority: user-approved
scope:
  - project:{name}
{source_refs}---

## Decision

This project is seeded from `{seed_pack["name"]}` with direction: {direction}.

## Goal

{goal or f"Build the first implementation slice for {direction}."}

## Constraints

- Do not treat brainstormed meeting ideas as final team decisions unless explicitly approved.
- Preserve the original problem framing from the seed pack.
- Do not choose a multi-agent architecture unless decomposition is justified.
"""


def _pyproject_toml(name: str, package: str) -> str:
    return f"""[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"

[tool.setuptools.packages.find]
where = ["src"]
include = ["{package}*"]
"""


def _test_import_py(package: str) -> str:
    return f"""import unittest


class ImportTest(unittest.TestCase):
    def test_imports_project_package(self):
        import {package}

        self.assertIsNotNone({package})
"""


if __name__ == "__main__":
    raise SystemExit(main())
