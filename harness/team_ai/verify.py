from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import load_config
from .runtime import latest_context_run


def verify_done(repo: str | Path, context_packet: dict | None = None, require_context: bool = True) -> dict:
    repo = Path(repo)
    commands = load_config(repo)["verification_commands"]
    results = [_run(repo, command) for command in commands]
    changed_files = _changed_files(repo)
    risks = [] if results else ["no verification commands configured"]
    if require_context:
        risks.extend(_context_risks(repo, changed_files))
    return {
        "commands_run": results,
        "changed_files": changed_files,
        "context_used": _context_used(context_packet or {}),
        "done": bool(results) and all(result["status"] == "passed" for result in results) and not risks,
        "risks": risks,
    }


def _run(repo: Path, command: str) -> dict:
    result = subprocess.run(command, cwd=repo, shell=True, text=True, capture_output=True, check=False, timeout=120)
    return {
        "cmd": command,
        "status": "passed" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


def _changed_files(repo: Path) -> list[str]:
    result = subprocess.run(["git", "status", "--short"], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode:
        return []
    return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]


def _context_risks(repo: Path, changed_files: list[str]) -> list[str]:
    if not any(_requires_implementation_context(path) for path in changed_files):
        return []
    run = latest_context_run(repo, "implementation")
    if not run:
        return [
            "implementation context was not loaded; run: "
            "team-ai context --mode implementation \"...\""
        ]
    manifest_id = _compiled_snapshot_id(repo)
    if manifest_id and run.get("snapshot_id") != manifest_id:
        return ["implementation context snapshot is stale; rerun team-ai context --mode implementation"]
    return []


def _requires_implementation_context(path: str) -> bool:
    return path.startswith("src/") or path.startswith("tests/")


def _compiled_snapshot_id(repo: Path) -> str | None:
    path = repo / ".team-ai" / "compiled" / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("generation_id")
    except json.JSONDecodeError:
        return None


def _context_used(packet: dict) -> dict:
    return {
        "project_state": sorted((packet.get("project_state") or {}).keys()),
        "active_decisions": [item.get("id") for item in packet.get("active_decisions", [])],
        "memories": [item.get("id") for item in packet.get("memories", [])],
        "source_refs": sorted(
            {
                span.get("source_ref")
                for span in packet.get("source_evidence", [])
                if span.get("source_ref")
            }
        ),
    }


def main() -> int:
    result = verify_done(Path("."))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["done"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
