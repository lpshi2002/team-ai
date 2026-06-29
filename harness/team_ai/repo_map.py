from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from .config import load_config


SKIP_DIRS = {".git", ".team-ai/compiled", ".team-ai/local", ".team-ai/derived", "__pycache__", ".pytest_cache"}


def implementation_context(repo: str | Path, question: str, max_files: int = 200) -> dict:
    repo = Path(repo)
    repo_map = build_repo_map(repo, max_files=max_files)
    likely = _likely_files(question, repo_map)
    return {
        "repo_map": repo_map,
        "likely_files": likely,
        "nearest_tests": _nearest_tests(likely, repo_map["tests"]),
        "entrypoints": _entrypoints(repo_map["files"]),
        "verification_commands": load_config(repo)["verification_commands"],
    }


def build_repo_map(repo: str | Path, max_files: int = 200) -> dict:
    repo = Path(repo)
    files = [_rel(path, repo) for path in sorted(repo.rglob("*")) if path.is_file() and not _skip(path, repo)]
    files = files[:max_files]
    python = [_python_summary(repo / rel, rel) for rel in files if rel.endswith(".py")]
    return {
        "files": files,
        "python": [item for item in python if item["classes"] or item["functions"] or item["imports"]],
        "tests": [rel for rel in files if rel.startswith("tests/") or "/tests/" in rel or Path(rel).name.startswith("test_")],
        "docs": [rel for rel in files if Path(rel).name in {"README.md", "AGENTS.md", "CLAUDE.md", "pyproject.toml", "package.json"}],
        "recent_changed_files": _git_changed_files(repo),
    }


def _python_summary(path: Path, rel: str) -> dict:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return {"path": rel, "classes": [], "functions": [], "imports": []}
    return {
        "path": rel,
        "classes": [node.name for node in tree.body if isinstance(node, ast.ClassDef)],
        "functions": [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))],
        "imports": sorted(_imports(tree)),
    }


def _imports(tree: ast.AST) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    return imports


def _likely_files(question: str, repo_map: dict, limit: int = 8) -> list[str]:
    terms = [term.casefold() for term in question.replace("_", " ").split() if len(term) > 1]
    scored = []
    for rel in repo_map["files"]:
        haystack = rel.replace("_", " ").casefold()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, rel))
    return [rel for _, rel in sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]]


def _nearest_tests(likely_files: list[str], tests: list[str], limit: int = 8) -> list[str]:
    if not likely_files:
        return tests[:limit]
    stems = {Path(rel).stem.removeprefix("test_") for rel in likely_files}
    matched = [test for test in tests if Path(test).stem.removeprefix("test_") in stems]
    return (matched or tests)[:limit]


def _entrypoints(files: list[str]) -> list[str]:
    names = {"main.py", "app.py", "server.py", "manage.py", "pyproject.toml", "package.json"}
    return [rel for rel in files if Path(rel).name in names]


def _git_changed_files(repo: Path) -> list[str]:
    result = subprocess.run(["git", "status", "--short"], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode:
        return []
    return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]


def _skip(path: Path, repo: Path) -> bool:
    rel = _rel(path, repo)
    parts = rel.split("/")
    if any(part in {"__pycache__", ".pytest_cache"} for part in parts):
        return True
    return any(rel == skip or rel.startswith(f"{skip}/") for skip in SKIP_DIRS)


def _rel(path: Path, repo: Path) -> str:
    return path.relative_to(repo).as_posix()
