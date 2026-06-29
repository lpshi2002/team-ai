from __future__ import annotations

from pathlib import Path


TEAM_AI_DIR = ".team-ai"
DEFAULT_SOURCE_ROOTS = (
    ".team-ai/sources",
    "자료",
    "회의록",
    "output",
    ".team-ai/project.yaml",
    ".team-ai/decisions",
    ".team-ai/state.yaml",
    ".team-ai/harness_state.yaml",
)
DEFAULT_VERIFICATION_COMMANDS = ("PYTHONPATH=src python -m unittest discover -s tests",)
DEFAULT_SCOPE_INFERENCE = (
    {"path": ".team-ai/project.yaml", "scope": "project:finance-architecture"},
    {"path": ".team-ai/state.yaml", "scope": "project:finance-architecture"},
    {"path": ".team-ai/harness_state.yaml", "scope": "harness:team-ai"},
    {"path": ".team-ai/decisions/**", "scope": "harness:team-ai"},
    {"path": ".team-ai/sources/meetings/**", "scope": "project:finance-architecture"},
    {"path": ".team-ai/sources/notes/**", "scope": "project:finance-architecture"},
    {"path": ".team-ai/sources/uploads/**", "scope": "project:finance-architecture"},
    {"path": ".team-ai/sources/references/*Agent*", "scope": "domain:agent-infra"},
    {"path": ".team-ai/sources/references/*Context*", "scope": "domain:agent-infra"},
    {"path": ".team-ai/sources/references/*SDLC*", "scope": "domain:agent-infra"},
    {"path": ".team-ai/sources/references/*Prototype*", "scope": "domain:agent-infra"},
    {"path": ".team-ai/sources/references/*MCP*", "scope": "domain:agent-infra"},
    {"path": ".team-ai/sources/references/**", "scope": "domain:finance-ai"},
    {"path": "회의록/**", "scope": "project:finance-architecture"},
    {"path": "output/**", "scope": "project:finance-architecture"},
    {"path": "자료/*Agent*", "scope": "domain:agent-infra"},
    {"path": "자료/*Context*", "scope": "domain:agent-infra"},
    {"path": "자료/*SDLC*", "scope": "domain:agent-infra"},
    {"path": "자료/*Prototype*", "scope": "domain:agent-infra"},
    {"path": "자료/**", "scope": "domain:finance-ai"},
)


def load_config(repo: str | Path) -> dict:
    path = Path(repo) / TEAM_AI_DIR / "config.yaml"
    config = {
        "source_roots": list(DEFAULT_SOURCE_ROOTS),
        "verification_commands": list(DEFAULT_VERIFICATION_COMMANDS),
        "project_config": f"{TEAM_AI_DIR}/project.yaml",
        "shared_pack_roots": [],
        "scope_inference": [dict(rule) for rule in DEFAULT_SCOPE_INFERENCE],
    }
    if not path.exists():
        return config
    current: str | None = None
    parent: str | None = None
    current_rule: dict | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        scalar = _top_level_scalar(stripped)
        if scalar:
            key, value = scalar
            if key == "project_config":
                config["project_config"] = _unquote(value)
                parent = None
                current = None
                continue
        if line.startswith("  ") and parent == "verification" and stripped == "commands:":
            current = "verification_commands"
            config[current] = []
            continue
        if not line.startswith(" ") and stripped in {
            "source_roots:",
            "verification:",
            "shared_pack_roots:",
            "scope_inference:",
        }:
            parent = stripped[:-1]
            current = {
                "source_roots": "source_roots",
                "shared_pack_roots": "shared_pack_roots",
                "scope_inference": "scope_inference",
            }.get(parent)
            if current:
                config[current] = []
            continue
        if current in {"source_roots", "verification_commands", "shared_pack_roots"} and stripped.startswith("- "):
            config[current].append(_unquote(stripped[2:].strip()))
            continue
        if current == "scope_inference":
            if stripped.startswith("- "):
                current_rule = {}
                config[current].append(current_rule)
                item = stripped[2:].strip()
                if item:
                    _set_rule_value(current_rule, item)
                continue
            if current_rule is not None:
                _set_rule_value(current_rule, stripped)
    return config


def _top_level_scalar(stripped: str) -> tuple[str, str] | None:
    if stripped.endswith(":") or ":" not in stripped:
        return None
    key, value = stripped.split(":", 1)
    if not key.replace("_", "").isalnum():
        return None
    return key, value.strip()


def _set_rule_value(rule: dict, item: str) -> None:
    if ":" not in item:
        return
    key, value = item.split(":", 1)
    rule[key.strip()] = _unquote(value.strip())


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
