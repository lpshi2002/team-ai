from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .runtime import _approved_active_decision, _connect, _front_matter, refresh
from .verify import verify_done


def guard(repo: str | Path = ".") -> dict:
    repo = Path(repo)
    errors: list[str] = []
    counts = None
    verification = None
    try:
        counts = refresh(repo)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"refresh failed: {exc}")
    errors.extend(_decision_errors(repo))
    errors.extend(_scope_errors(repo))
    errors.extend(_boundary_errors(repo))
    errors.extend(_git_tracking_errors(repo))
    try:
        verification = verify_done(repo)
        if not verification["done"]:
            errors.extend(verification["risks"])
            errors.extend(
                f"verification failed: {item['cmd']}"
                for item in verification["commands_run"]
                if item["status"] != "passed"
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"verification failed: {exc}")
    return {
        "ok": not errors,
        "refresh": counts,
        "verification": verification,
        "errors": errors,
    }


def _decision_errors(repo: Path) -> list[str]:
    errors = []
    base = repo / ".team-ai" / "decisions"
    if not base.exists():
        return errors
    for path in sorted(base.glob("*.md")):
        front = _front_matter(path.read_text(encoding="utf-8"))
        if front.get("status") == "active" and not _approved_active_decision(front):
            errors.append(f"active decision lacks approval: {path.relative_to(repo).as_posix()}")
    return errors


def _scope_errors(repo: Path) -> list[str]:
    db_path = repo / ".team-ai" / "local" / "knowledge.sqlite"
    if not db_path.exists():
        return []
    errors = []
    db = _connect(repo)
    try:
        rows = db.execute("select id from memories where scope = 'cross-project'").fetchall()
        errors.extend(f"deprecated cross-project memory scope: {row['id']}" for row in rows)
        rows = db.execute(
            "select id from memories where authority = 'ai_inferred' and share_policy = 'shared_approved'"
        ).fetchall()
        errors.extend(f"ai_inferred memory cannot be shared_approved: {row['id']}" for row in rows)
    finally:
        db.close()
    return errors


def _boundary_errors(repo: Path) -> list[str]:
    errors = []
    if (repo / "tools" / "team_ai").exists():
        errors.append("harness engine code is still inside project repo: tools/team_ai")
    product = repo / "src" / "finance_architecture"
    if product.exists():
        for path in sorted(product.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if _imports(text, "tools.team_ai") or _imports(text, "team_ai"):
                errors.append(f"product code imports harness code: {path.relative_to(repo).as_posix()}")
    harness = repo / "tools" / "team_ai"
    if harness.exists():
        for path in sorted(harness.rglob("*.py")):
            if "/tests/" in path.as_posix():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if _imports(text, "finance_architecture") or _imports(text, "src.finance_architecture"):
                errors.append(f"harness code imports product code: {path.relative_to(repo).as_posix()}")
    return errors


def _git_tracking_errors(repo: Path) -> list[str]:
    result = subprocess.run(["git", "ls-files"], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode:
        return []
    forbidden = (
        ".env",
        ".team-ai/cache/",
        ".team-ai/tmp/",
        ".team-ai/logs/",
        ".team-ai/local/",
        "output/",
        "outputs/",
        "tools/team_ai/",
    )
    errors = []
    for path in result.stdout.splitlines():
        name = Path(path).name.lower()
        if (
            path in forbidden
            or path.startswith(".env.")
            or any(path.startswith(prefix) for prefix in forbidden if prefix.endswith("/"))
            or path.endswith((".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx"))
            or any(token in name for token in ("secret", "token", "api_key", "credentials"))
        ):
            errors.append(f"forbidden tracked file: {path}")
    return errors


def _imports(text: str, module: str) -> bool:
    escaped = re.escape(module)
    return bool(re.search(rf"^\s*(?:from\s+{escaped}\b|import\s+{escaped}\b)", text, re.M))


def main() -> int:
    result = guard(".")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
