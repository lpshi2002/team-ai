from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runtime import build_context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load team-ai context for a repository question.")
    parser.add_argument("question", nargs="+")
    parser.add_argument(
        "--mode",
        choices=("auto", "answer", "normal", "harness", "global_shared", "compare_projects", "implementation"),
        default="auto",
    )
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args(argv)

    question = " ".join(args.question)
    packet = build_context(".", question, limit=args.limit, mode=args.mode)
    print(_summary(packet, args.mode))
    return 0


def _summary(packet: dict, mode: str) -> str:
    retrieval = packet.get("retrieval") or {}
    lines = [
        "Harness context loaded",
        f"snapshot: {packet['refresh']['snapshot_id']}",
        f"mode: {packet.get('mode', mode)}",
        f"intent: {packet.get('intent', 'current')}",
    ]
    if retrieval:
        lines.append(f"retrieval: {retrieval.get('status', 'no_match')}")
    lines.extend(
        [
            f"allowed scopes: {', '.join(packet.get('allowed_scopes', []))}",
            f"projects: {', '.join(sorted(packet.get('project_state', {})))}",
            "",
            "Active decisions:",
        ]
    )
    decisions = packet.get("active_decisions") or []
    lines.extend(f"- {item.get('id')}: {item.get('title')}" for item in decisions[:5])
    if not decisions:
        lines.append("- none")
    lines.extend(["", "Relevant memories:"])
    memories = packet.get("memories") or []
    lines.extend(f"- {item.get('id')}: {item.get('content', '')[:160]}" for item in memories[:8])
    if not memories:
        lines.append("- none")
    lines.extend(["", "Source evidence:"])
    spans = packet.get("source_evidence") or []
    lines.extend(f"- {item.get('source_ref')}" for item in spans[:8])
    if not spans:
        lines.append("- none")
    if packet.get("implementation_context"):
        impl = packet["implementation_context"]
        lines.extend(["", "Implementation:"])
        lines.append(f"- likely files: {', '.join(impl.get('likely_files') or ['none'])}")
        lines.append(f"- nearest tests: {', '.join(impl.get('nearest_tests') or ['none'])}")
        lines.append(f"- verification: {', '.join(impl.get('verification_commands') or ['none'])}")
    local = Path(".team-ai/local")
    lines.extend(["", f"Full packet written to: {local / 'last_context.json'}"])
    lines.append(f"Run trace appended to: {local / 'runs.jsonl'}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
