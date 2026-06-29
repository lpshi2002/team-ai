import json
import sqlite3
import subprocess
import unittest
import unicodedata
from unittest import mock
from pathlib import Path


class TeamAiRuntimeTest(unittest.TestCase):
    def test_refresh_stores_sources_spans_and_promoted_memories(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "자료").mkdir()
            (repo / "회의록").mkdir()
            (repo / "자료" / "idea.md").write_text(
                "Day 3 자료는 memory와 context engineering을 설명한다.\n\n원칙: 계산은 도구가 수행한다.\n",
                encoding="utf-8",
            )
            (repo / "회의록" / "meeting.txt").write_text(
                "GraphRAG는 좋아 보인다.\n결정: 이번에는 전체 그래프보다 원문 추적성을 우선한다.\n",
                encoding="utf-8",
            )

            result = refresh(repo)

            self.assertEqual(result["sources"], 2)
            db = sqlite3.connect(repo / ".team-ai" / "local" / "knowledge.sqlite")
            try:
                self.assertEqual(db.execute("select count(*) from sources").fetchone()[0], 2)
                self.assertGreater(db.execute("select count(*) from spans").fetchone()[0], 2)
                memories = db.execute("select kind, authority, origin, status, content from memories").fetchall()
            finally:
                db.close()
            self.assertLess(len(memories), 4)
            self.assertTrue(any(row[:4] == ("principle", "external_knowledge", "source", "active") for row in memories))
            self.assertTrue(any(row[:4] == ("decision", "individual_opinion", "source", "active") for row in memories))
            self.assertFalse(any("Day 3 자료는" in row[4] for row in memories))

    def test_build_context_refreshes_and_finds_new_file(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "자료").mkdir()
            (repo / "자료" / "portfolio.md").write_text(
                "Portfolio X-Ray는 ETF look-through와 집중위험을 다룬다.",
                encoding="utf-8",
            )

            packet = build_context(repo, "ETF 집중위험")

            span_text = "\n".join(item["text"] for item in packet["spans"])
            self.assertIn("Portfolio X-Ray", span_text)
            self.assertIn("자료/portfolio.md", packet["spans"][0]["source_ref"])

    def test_build_context_reads_team_ai_sources_layout(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            references = repo / ".team-ai" / "sources" / "references"
            meetings = repo / ".team-ai" / "sources" / "meetings"
            references.mkdir(parents=True)
            meetings.mkdir(parents=True)
            (references / "finance.md").write_text(
                "원칙: source layout reference evidence",
                encoding="utf-8",
            )
            (meetings / "meeting.md").write_text(
                "결정: source layout meeting evidence",
                encoding="utf-8",
            )

            packet = build_context(repo, "source layout evidence", limit=5)
            refs = [span["source_ref"] for span in packet["source_evidence"]]

            self.assertIn(".team-ai/sources/references/finance.md#page=1", refs)
            self.assertIn(".team-ai/sources/meetings/meeting.md#page=1", refs)

    def test_build_context_handles_large_pasted_request(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_dir = repo / ".team-ai" / "sources" / "references"
            source_dir.mkdir(parents=True)
            (source_dir / "note.md").write_text("원칙: long request search stays bounded.", encoding="utf-8")
            question = " ".join(f"term{i}" for i in range(1400)) + " bounded"

            packet = build_context(repo, question, limit=3)

            self.assertEqual(packet["refresh"]["sources"], 1)

    def test_authored_memory_old_source_ref_resolves_after_source_move(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            meeting_dir = repo / ".team-ai" / "sources" / "meetings"
            memory_dir = repo / ".team-ai" / "memory"
            meeting_dir.mkdir(parents=True)
            memory_dir.mkdir(parents=True)
            (meeting_dir / "meeting.md").write_text("원칙: moved source ref remains readable.", encoding="utf-8")
            (memory_dir / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-0001",
                        "kind": "principle",
                        "scope": "project:finance-architecture",
                        "content": "moved source ref remains readable.",
                        "authority": "individual_opinion",
                        "origin": "source",
                        "status": "active",
                        "source_refs": ["회의록/meeting.md#page=1"],
                        "related": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            packet = build_context(repo, "moved source ref", limit=5)
            memory = next(item for item in packet["memories"] if item["id"] == "MEM-AUTH-0001")

            self.assertEqual(memory["supporting_spans"][0]["source_ref"], ".team-ai/sources/meetings/meeting.md#page=1")

    def test_ai_generated_output_is_not_team_explicit(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "output").mkdir()
            (repo / "output" / "draft.md").write_text(
                "팀은 GraphRAG를 선호한다고 추정된다.",
                encoding="utf-8",
            )

            refresh(repo)

            db = sqlite3.connect(repo / ".team-ai" / "local" / "knowledge.sqlite")
            try:
                rows = db.execute("select authority, origin from memories").fetchall()
            finally:
                db.close()
            self.assertEqual(rows, [("ai_inferred", "inferred")])

    def test_build_context_imports_compiled_snapshot_when_db_is_missing(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context, refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "자료").mkdir()
            (repo / "자료" / "portfolio.md").write_text(
                "Portfolio X-Ray는 ETF look-through와 집중위험을 다룬다.",
                encoding="utf-8",
            )
            refresh(repo)
            db_path = repo / ".team-ai" / "local" / "knowledge.sqlite"
            self.assertTrue(db_path.exists())
            db_path.unlink()

            packet = build_context(repo, "ETF 집중위험")

            self.assertTrue(db_path.exists())
            self.assertIn("Portfolio X-Ray", "\n".join(item["text"] for item in packet["spans"]))
            self.assertEqual(packet["refresh"]["changed"], 0)

    def test_refresh_removes_knowledge_when_source_file_is_deleted(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_dir = repo / "자료"
            source_dir.mkdir()
            source = source_dir / "remove.md"
            source.write_text("삭제될 프로젝트 아이디어\n\n삭제 후에는 지식에도 남으면 안 된다.", encoding="utf-8")
            refresh(repo)

            source.unlink()
            result = refresh(repo)

            self.assertEqual(result["sources"], 0)
            db = sqlite3.connect(repo / ".team-ai" / "local" / "knowledge.sqlite")
            try:
                self.assertEqual(
                    db.execute("select count(*) from sources where path = ?", ("자료/remove.md",)).fetchone()[0],
                    0,
                )
                self.assertEqual(db.execute("select count(*) from spans").fetchone()[0], 0)
                self.assertEqual(db.execute("select count(*) from memories").fetchone()[0], 0)
            finally:
                db.close()

            for name in ("sources.jsonl", "spans.jsonl", "memories.jsonl"):
                compiled = repo / ".team-ai" / "compiled" / name
                self.assertNotIn("자료/remove.md", compiled.read_text(encoding="utf-8"))

    def test_refresh_keeps_generic_spans_out_of_memory(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "자료").mkdir()
            (repo / "자료" / "notes.md").write_text(
                "Day 3 자료는 memory와 context engineering을 설명한다.\n"
                "원칙: 나중에 답변 품질을 바꿀 정보만 memory로 승격한다.\n",
                encoding="utf-8",
            )

            result = refresh(repo)

            db = sqlite3.connect(repo / ".team-ai" / "local" / "knowledge.sqlite")
            try:
                spans = [row[0] for row in db.execute("select text from spans").fetchall()]
                memories = [row[0] for row in db.execute("select content from memories").fetchall()]
            finally:
                db.close()

            self.assertEqual(result["sources"], 1)
            self.assertTrue(any("Day 3 자료는" in text for text in spans))
            self.assertFalse(any("Day 3 자료는" in text for text in memories))
            self.assertTrue(any("답변 품질" in text for text in memories))

    def test_refresh_imports_authored_memory_without_api(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context, refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team-ai" / "memory").mkdir(parents=True)
            memory_file = repo / ".team-ai" / "memory" / "curated.jsonl"
            memory_file.write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-0001",
                        "kind": "principle",
                        "scope": "team",
                        "content": "memory는 지식 창고가 아니라 팀 판단의 압축본이다.",
                        "reason": "사용자가 저장 시 하네스로 강제한다고 명시했다.",
                        "authority": "user_explicit",
                        "origin": "user",
                        "status": "active",
                        "source_refs": [],
                        "related": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = refresh(repo)

            self.assertEqual(result["spans"], 0)
            self.assertEqual(result["memories"], 1)
            packet = build_context(repo, "memory 압축본")
            self.assertEqual(packet["memories"][0]["id"], "MEM-AUTH-0001")

            memory_file.unlink()
            self.assertEqual(refresh(repo)["memories"], 0)

    def test_build_context_prioritizes_authored_memory(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "자료").mkdir()
            (repo / "회의록").mkdir()
            (repo / ".team-ai" / "memory").mkdir(parents=True)
            (repo / "회의록" / "example.md").write_text("프로젝트 방향은 회의록에서 확인한다.", encoding="utf-8")
            (repo / "자료" / "noise.md").write_text(
                "\n".join(f"원칙: 프로젝트 방향 일반 자료 {i}" for i in range(20)),
                encoding="utf-8",
            )
            (repo / ".team-ai" / "memory" / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-0009",
                        "kind": "principle",
                        "scope": "project:finance-architecture",
                        "content": "프로젝트 방향은 회의록의 해야 할 일과 결정 기준을 우선한다.",
                        "authority": "ai_inferred",
                        "origin": "inferred",
                        "status": "active",
                        "source_refs": ["회의록/example.md#page=1"],
                        "related": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            packet = build_context(repo, "프로젝트 방향", limit=1)

            self.assertEqual(packet["memories"][0]["id"], "MEM-AUTH-0009")

    def test_build_context_reads_team_ai_project_state(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_path = repo / ".team-ai" / "state.yaml"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                "project_id: finance-architecture\n"
                "current_goal: 실제 금융 AI 프로젝트를 아이디어 타게팅한다.\n",
                encoding="utf-8",
            )

            packet = build_context(repo, "실제 금융 AI 프로젝트")

            self.assertIn("아이디어 타게팅", "\n".join(span["text"] for span in packet["spans"]))
            self.assertIn("finance-architecture", packet["project_state"])
            self.assertIn("아이디어 타게팅", packet["project_state"]["finance-architecture"]["text"])

    def test_build_context_imports_shared_pack_state_memory_and_decisions(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "finance-architecture"
            pack = workspace / "team-ai" / "shared" / "packs" / "harness-team-ai"
            (repo / ".team-ai").mkdir(parents=True)
            (pack / "memory").mkdir(parents=True)
            (pack / "decisions").mkdir(parents=True)
            (pack / "sources" / "meetings").mkdir(parents=True)
            (repo / ".team-ai" / "project.yaml").write_text(
                "project_id: finance-architecture\n"
                "scope: project:finance-architecture\n"
                "imports:\n"
                "  - scope: harness:team-ai\n"
                "    path: ../team-ai/shared/packs/harness-team-ai\n"
                "scope_modes:\n"
                "  harness:\n"
                "    include:\n"
                "      - harness:team-ai\n",
                encoding="utf-8",
            )
            (pack / "state.yaml").write_text(
                "project_id: team-ai-harness\ncurrent_goal: shared pack state unique\n",
                encoding="utf-8",
            )
            (pack / "memory" / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-9001",
                        "kind": "principle",
                        "scope": "harness:team-ai",
                        "content": "shared pack memory unique",
                        "authority": "user_explicit",
                        "origin": "user",
                        "status": "active",
                        "source_refs": [],
                        "related": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (pack / "decisions" / "DEC-9001.md").write_text(
                "---\n"
                "id: DEC-9001\n"
                "title: Shared pack decision unique\n"
                "status: active\n"
                "scope:\n"
                "  - harness:team-ai\n"
                "authority: user-approved\n"
                "decided_at: 2026-06-29\n"
                "---\n\n"
                "## Decision\n\nUse shared packs.\n",
                encoding="utf-8",
            )
            (pack / "sources" / "meetings" / "source.md").write_text(
                "shared pack source unique",
                encoding="utf-8",
            )

            packet = build_context(repo, "하네스 shared pack memory source unique", mode="harness")

            self.assertIn("team-ai-harness", packet["project_state"])
            self.assertEqual(packet["active_decisions"][0]["id"], "DEC-9001")
            self.assertEqual(packet["memories"][0]["id"], "MEM-AUTH-9001")
            self.assertTrue(
                any(
                    span["source_ref"].endswith("team-ai/shared/packs/harness-team-ai/sources/meetings/source.md#page=1")
                    for span in packet["source_evidence"]
                )
            )

    def test_build_context_keeps_program_scope_shared_pack_state(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "finance-architecture"
            pack = workspace / "team-ai" / "shared" / "packs" / "finance-ai-ideation"
            (repo / ".team-ai").mkdir(parents=True)
            (pack / "sources" / "meetings").mkdir(parents=True)
            (repo / ".team-ai" / "project.yaml").write_text(
                "project_id: finance-ai-ideation\n"
                "scope: program:finance-ai-ideation\n"
                "imports:\n"
                "  - scope: program:finance-ai-ideation\n"
                "    path: ../team-ai/shared/packs/finance-ai-ideation\n"
                "scope_modes:\n"
                "  normal:\n"
                "    include:\n"
                "      - program:finance-ai-ideation\n",
                encoding="utf-8",
            )
            (pack / "state.yaml").write_text(
                "project_id: finance-ai-ideation\ncurrent_goal: program pack state unique\n",
                encoding="utf-8",
            )
            (pack / "sources" / "meetings" / "source.md").write_text(
                "program pack source unique",
                encoding="utf-8",
            )

            packet = build_context(repo, "program pack source unique")

            self.assertIn("finance-ai-ideation", packet["project_state"])
            self.assertIn("program pack state unique", packet["project_state"]["finance-ai-ideation"]["text"])

    def test_auto_context_treats_memory_recall_as_project_recall(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "team-ai"
            finance = repo / "shared" / "packs" / "finance-ai-ideation"
            harness = repo / "shared" / "packs" / "harness-team-ai"
            (repo / ".team-ai").mkdir(parents=True)
            (finance / "memory").mkdir(parents=True)
            (finance / "sources" / "meetings").mkdir(parents=True)
            (harness / "memory").mkdir(parents=True)
            (repo / ".team-ai" / "project.yaml").write_text(
                "project_id: team-ai-workspace\n"
                "scope: workspace:team-ai\n"
                "imports:\n"
                "  - scope: program:finance-ai-ideation\n"
                "    path: shared/packs/finance-ai-ideation\n"
                "  - scope: harness:team-ai\n"
                "    path: shared/packs/harness-team-ai\n"
                "scope_modes:\n"
                "  normal:\n"
                "    include:\n"
                "      - workspace:team-ai\n"
                "      - program:finance-ai-ideation\n"
                "  harness:\n"
                "    include:\n"
                "      - harness:team-ai\n",
                encoding="utf-8",
            )
            (finance / "memory" / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-FIN-0001",
                        "kind": "discussion",
                        "scope": "program:finance-ai-ideation",
                        "content": "회의에서는 금융사기 탐지 아이디어가 나왔다.",
                        "authority": "user_explicit",
                        "origin": "user",
                        "status": "active",
                        "source_refs": [],
                        "related": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (harness / "memory" / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-HARNESS-0001",
                        "kind": "principle",
                        "scope": "harness:team-ai",
                        "content": "memory schema harness only",
                        "authority": "user_explicit",
                        "origin": "user",
                        "status": "active",
                        "source_refs": [],
                        "related": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (finance / "sources" / "meetings" / "2026.06.25 meeting.md").write_text(
                "회의에서는 금융사기 탐지 아이디어가 나왔다.\n후속 span도 있다.",
                encoding="utf-8",
            )
            (finance / "sources" / "meetings" / "2026.06.26 meeting.md").write_text(
                "회의에서는 평가 기준을 얘기했다.",
                encoding="utf-8",
            )

            packet = build_context(repo, "기억 가져와줘. 지난번 회의 뭐 나왔지?", limit=5)

            self.assertEqual(packet["mode"], "normal")
            self.assertEqual(packet["intent"], "meeting_recall")
            self.assertIn("program:finance-ai-ideation", packet["allowed_scopes"])
            self.assertNotIn("harness:team-ai", packet["allowed_scopes"])
            self.assertEqual(packet["memories"][0]["id"], "MEM-AUTH-FIN-0001")
            source_refs = [span["source_ref"] for span in packet["source_evidence"][:2]]
            self.assertTrue(any("2026.06.25 meeting.md" in ref for ref in source_refs))
            self.assertTrue(any("2026.06.26 meeting.md" in ref for ref in source_refs))

    def test_pack_uri_source_ref_resolves_from_renamed_coworker_pack(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "product"
            pack = workspace / "my-team-ai" / "shared" / "packs" / "finance-ai-ideation"
            (repo / ".team-ai").mkdir(parents=True)
            (pack / "sources" / "meetings").mkdir(parents=True)
            (pack / "memory").mkdir(parents=True)
            (repo / ".team-ai" / "project.yaml").write_text(
                "project_id: product\n"
                "scope: project:product\n"
                "imports:\n"
                "  - scope: program:finance-ai-ideation\n"
                "    path: ../my-team-ai/shared/packs/finance-ai-ideation\n"
                "scope_modes:\n"
                "  normal:\n"
                "    include:\n"
                "      - project:product\n"
                "      - program:finance-ai-ideation\n",
                encoding="utf-8",
            )
            (pack / "sources" / "meetings" / "meeting.md").write_text(
                "원칙: pack uri support remains readable.",
                encoding="utf-8",
            )
            (pack / "memory" / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-FIN-0001",
                        "kind": "principle",
                        "scope": "program:finance-ai-ideation",
                        "content": "pack uri support remains readable.",
                        "authority": "individual_opinion",
                        "origin": "source",
                        "status": "active",
                        "source_refs": ["pack://finance-ai-ideation/sources/meetings/meeting.md#page=1"],
                        "related": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            packet = build_context(repo, "pack uri support", limit=5)
            memory = next(item for item in packet["memories"] if item["id"] == "MEM-AUTH-FIN-0001")

            self.assertTrue(memory["supporting_spans"])
            self.assertTrue(
                memory["supporting_spans"][0]["source_ref"].endswith(
                    "my-team-ai/shared/packs/finance-ai-ideation/sources/meetings/meeting.md#page=1"
                )
            )

    def test_active_decisions_are_bucketed_by_scope(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            project_decisions = repo / ".team-ai" / "decisions"
            project_decisions.mkdir(parents=True)
            (project_decisions / "DEC-0001.md").write_text(
                "---\n"
                "id: DEC-0001\n"
                "title: Project decision\n"
                "status: active\n"
                "authority: user-approved\n"
                "scope:\n"
                "  - project:finance-architecture\n"
                "---\n\n"
                "## Decision\n\nProject.\n",
                encoding="utf-8",
            )

            packet = build_context(repo, "프로젝트 결정", limit=5)

            self.assertEqual(packet["active_decisions"][0]["id"], "DEC-0001")
            self.assertEqual(packet["active_decisions_by_scope"]["project"][0]["id"], "DEC-0001")

    def test_build_context_ranks_specific_new_source_before_generic_matches(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_dir = repo / "자료"
            source_dir.mkdir()
            (source_dir / "aaa_generic.md").write_text(
                "\n".join(f"Ledger 계약 리스크 일반 배경 {i}" for i in range(20)),
                encoding="utf-8",
            )
            (source_dir / "ux_added_source.md").write_text(
                "Carbon Ledger Diligence는 공급망 탄소부채와 계약 리스크를 연결해서 본다.",
                encoding="utf-8",
            )

            packet = build_context(repo, "Carbon Ledger 계약 리스크", limit=3)

            self.assertEqual(packet["spans"][0]["source_ref"], "자료/ux_added_source.md#page=1")

    def test_memory_events_project_current_state_and_history(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            memory_dir = repo / ".team-ai" / "memory"
            meeting_dir = repo / "회의록"
            memory_dir.mkdir(parents=True)
            meeting_dir.mkdir()
            (meeting_dir / "example.md").write_text("RAG 정책 논의 근거", encoding="utf-8")
            event_path = memory_dir / "events.jsonl"
            old_memory = {
                "id": "MEM-AUTH-0001",
                "kind": "open_question",
                "scope": "project:finance-architecture",
                "canonical_key": "project:finance-architecture:rag_policy",
                "content": "RAG 정책은 저장할지 버릴지 아직 미정이다.",
                "authority": "individual_opinion",
                "origin": "source",
                "status": "active",
                "source_refs": ["회의록/example.md#page=1"],
                "related": [],
                "valid_from": "2026-06-25",
                "valid_to": None,
            }
            new_memory = {
                "id": "MEM-AUTH-0002",
                "kind": "decision",
                "scope": "project:finance-architecture",
                "canonical_key": "project:finance-architecture:rag_policy",
                "content": "RAG 정책은 휘발성 검색 결과를 durable memory에 저장하지 않는다.",
                "reason": "검색 결과가 이후 답변을 오염시킬 수 있기 때문이다.",
                "authority": "team_explicit",
                "origin": "source",
                "status": "active",
                "source_refs": ["회의록/example.md#page=1"],
                "related": [],
                "valid_from": "2026-06-26",
                "approved_by": "user",
                "approved_at": "2026-06-26",
            }
            event_path.write_text(
                "\n".join(
                    json.dumps(
                        {"event_id": event_id, "operation": "upsert", "memory": memory},
                        ensure_ascii=False,
                    )
                    for event_id, memory in (("MEM-EVT-0001", old_memory), ("MEM-EVT-0002", new_memory))
                )
                + "\n",
                encoding="utf-8",
            )

            current_packet = build_context(repo, "RAG 정책", limit=5)
            current_contents = [memory["content"] for memory in current_packet["memories"]]
            self.assertIn(new_memory["content"], current_contents)
            self.assertNotIn(old_memory["content"], current_contents)

            history_packet = build_context(repo, "RAG 정책은 왜 바뀌었어?", limit=5)
            history_by_content = {memory["content"]: memory["status"] for memory in history_packet["memories"]}
            self.assertEqual(history_by_content[old_memory["content"]], "superseded")
            self.assertEqual(history_by_content[new_memory["content"]], "active")
            old_packet = next(memory for memory in history_packet["memories"] if memory["content"] == old_memory["content"])
            self.assertEqual(old_packet["valid_to"], "2026-06-26")
            self.assertEqual(old_packet["superseded_by"], "MEM-AUTH-0002")

    def test_refresh_refuses_to_delete_when_source_root_disappears(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_dir = repo / "자료"
            source_dir.mkdir()
            (source_dir / "keep.md").write_text("원칙: 사라진 루트는 삭제로 처리하지 않는다.", encoding="utf-8")
            refresh(repo)

            source_dir.rename(repo / "missing-for-now")

            with self.assertRaisesRegex(RuntimeError, "source root missing"):
                refresh(repo)

            db = sqlite3.connect(repo / ".team-ai" / "local" / "knowledge.sqlite")
            try:
                self.assertEqual(db.execute("select count(*) from sources").fetchone()[0], 1)
                self.assertEqual(db.execute("select count(*) from spans").fetchone()[0], 1)
            finally:
                db.close()

    def test_meeting_question_does_not_become_team_explicit(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "회의록").mkdir()
            (repo / "회의록" / "meeting.md").write_text(
                "모델을 결정해야 하는가?",
                encoding="utf-8",
            )

            refresh(repo)

            db = sqlite3.connect(repo / ".team-ai" / "local" / "knowledge.sqlite")
            try:
                rows = db.execute("select authority from memories").fetchall()
            finally:
                db.close()
            self.assertEqual(rows, [("individual_opinion",)])

    def test_context_includes_active_decisions_and_full_memory_metadata(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            decision_dir = repo / ".team-ai" / "decisions"
            memory_dir = repo / ".team-ai" / "memory"
            decision_dir.mkdir(parents=True)
            memory_dir.mkdir(parents=True)
            (decision_dir / "DEC-0001.md").write_text(
                "---\n"
                "id: DEC-0001\n"
                "title: Use the lightweight harness\n"
                "status: active\n"
                "authority: user-approved\n"
                "decided_at: 2026-06-25\n"
                "---\n\n"
                "## Decision\n\nUse the lightweight harness.\n",
                encoding="utf-8",
            )
            (memory_dir / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-0001",
                        "kind": "principle",
                        "scope": "team",
                        "content": "근거와 권위를 함께 반환한다.",
                        "reason": "답변 시 판단 근거가 필요하다.",
                        "authority": "user_explicit",
                        "origin": "user",
                        "status": "active",
                        "source_refs": [],
                        "related": ["DEC-0001"],
                        "occurred_at": "2026-06-25",
                        "valid_from": "2026-06-25",
                        "valid_to": None,
                        "superseded_by": None,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            packet = build_context(repo, "근거 권위", limit=1)

            self.assertEqual(packet["active_decisions"][0]["id"], "DEC-0001")
            memory = packet["memories"][0]
            self.assertEqual(memory["reason"], "답변 시 판단 근거가 필요하다.")
            self.assertEqual(memory["related"], ["DEC-0001"])
            self.assertEqual(memory["occurred_at"], "2026-06-25")

    def test_current_context_excludes_ai_output_as_source_evidence(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "output").mkdir()
            (repo / "output" / "draft.md").write_text("팀은 단독 근거로 쓰면 안 된다.", encoding="utf-8")

            packet = build_context(repo, "단독 근거")

            self.assertEqual(packet["source_evidence"], [])
            self.assertEqual(packet["memories"][0]["authority"], "ai_inferred")

    def test_source_id_survives_file_move(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_dir = repo / "자료"
            source_dir.mkdir()
            first = source_dir / "a.md"
            second = source_dir / "b.md"
            first.write_text("원칙: 파일 이동은 같은 source로 유지한다.", encoding="utf-8")
            refresh(repo)

            db = sqlite3.connect(repo / ".team-ai" / "local" / "knowledge.sqlite")
            try:
                first_id = db.execute("select id from sources").fetchone()[0]
            finally:
                db.close()

            first.rename(second)
            refresh(repo)

            db = sqlite3.connect(repo / ".team-ai" / "local" / "knowledge.sqlite")
            try:
                rows = db.execute("select id, path from sources").fetchall()
            finally:
                db.close()
            self.assertEqual(rows, [(first_id, "자료/b.md")])

    def test_decomposed_korean_root_is_normalized(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            decomposed = repo / unicodedata.normalize("NFD", "자료")
            decomposed.mkdir()
            (decomposed / "note.md").write_text("원칙: 경로는 NFC로 비교한다.", encoding="utf-8")

            packet = build_context(repo, "NFC")

            self.assertEqual(packet["spans"][0]["source_ref"], "자료/note.md#page=1")

    def test_authored_memory_rejects_invalid_enums(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team-ai" / "memory").mkdir(parents=True)
            (repo / ".team-ai" / "memory" / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-0001",
                        "kind": "banana",
                        "scope": "wrong",
                        "content": "bad",
                        "authority": "superuser",
                        "origin": "source",
                        "status": "forever",
                        "source_refs": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid kind"):
                refresh(repo)

    def test_duplicate_content_files_do_not_merge_sources(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_dir = repo / "자료"
            source_dir.mkdir()
            (source_dir / "a.md").write_text("원칙: 같은 내용 복사본은 별도 source다.", encoding="utf-8")
            (source_dir / "copy.md").write_text("원칙: 같은 내용 복사본은 별도 source다.", encoding="utf-8")

            refresh(repo)

            db = sqlite3.connect(repo / ".team-ai" / "local" / "knowledge.sqlite")
            try:
                rows = db.execute("select id, path from sources order by path").fetchall()
            finally:
                db.close()
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0][0], rows[1][0])

    def test_root_restore_recovers_without_duplicate_sources(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_dir = repo / "자료"
            source_dir.mkdir()
            (source_dir / "keep.md").write_text("원칙: 루트 복구는 중복 source를 만들지 않는다.", encoding="utf-8")
            refresh(repo)

            hidden = repo / "hidden"
            source_dir.rename(hidden)
            with self.assertRaisesRegex(RuntimeError, "source root missing"):
                refresh(repo)
            hidden.rename(source_dir)
            refresh(repo)

            db = sqlite3.connect(repo / ".team-ai" / "local" / "knowledge.sqlite")
            try:
                self.assertEqual(db.execute("select count(*) from sources").fetchone()[0], 1)
                self.assertEqual(db.execute("select count(distinct id) from sources").fetchone()[0], 1)
            finally:
                db.close()

    def test_unapproved_decision_record_is_not_active_decision(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            decision_dir = repo / ".team-ai" / "decisions"
            decision_dir.mkdir(parents=True)
            (decision_dir / "DEC-0001.md").write_text(
                "---\n"
                "id: DEC-0001\n"
                "title: Draft decision\n"
                "status: active\n"
                "authority: ai-draft\n"
                "decided_at: 2026-06-25\n"
                "scope:\n"
                "  - project:team-ai-harness\n"
                "---\n\n"
                "## Decision\n\nDraft only.\n",
                encoding="utf-8",
            )

            packet = build_context(repo, "하네스 결정")

            self.assertEqual(packet["active_decisions"], [])

    def test_missing_source_ref_is_rejected(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team-ai" / "memory").mkdir(parents=True)
            (repo / ".team-ai" / "memory" / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-0001",
                        "kind": "principle",
                        "scope": "team",
                        "content": "근거 없는 source_ref는 거부한다.",
                        "authority": "individual_opinion",
                        "origin": "source",
                        "status": "active",
                        "source_refs": ["회의록/missing.md#page=1"],
                        "related": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "source_ref does not resolve"):
                refresh(repo)

    def test_failed_compiled_export_recovers_on_next_refresh(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "자료").mkdir()
            (repo / "자료" / "note.md").write_text("원칙: snapshot은 manifest로 검증한다.", encoding="utf-8")

            with mock.patch("team_ai.runtime._validate_compiled_dir", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    refresh(repo)

            result = refresh(repo)

            self.assertEqual(result["changed"], 0)
            self.assertTrue((repo / ".team-ai" / "compiled" / "manifest.json").exists())
            manifest = json.loads((repo / ".team-ai" / "compiled" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result["snapshot_id"], manifest["generation_id"])

    def test_import_rejects_tampered_compiled_snapshot(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context, refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "자료").mkdir()
            (repo / "자료" / "note.md").write_text("원칙: compiled import는 manifest를 검증한다.", encoding="utf-8")
            refresh(repo)
            spans_path = repo / ".team-ai" / "compiled" / "spans.jsonl"
            spans_path.write_text(spans_path.read_text(encoding="utf-8").replace("manifest", "tampered"), encoding="utf-8")
            (repo / ".team-ai" / "local" / "knowledge.sqlite").unlink()

            with self.assertRaisesRegex(RuntimeError, "compiled snapshot hash mismatch"):
                build_context(repo, "tampered")

    def test_memory_search_is_limited_to_target_scopes(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team-ai" / "memory").mkdir(parents=True)
            memories = [
                {
                    "id": "MEM-AUTH-0001",
                    "kind": "open_question",
                    "scope": "project:finance-architecture",
                    "content": "RAG 정책 finance memory",
                    "authority": "user_explicit",
                    "origin": "user",
                    "status": "active",
                    "source_refs": [],
                    "related": [],
                },
                {
                    "id": "MEM-AUTH-0002",
                    "kind": "open_question",
                    "scope": "project:team-ai-harness",
                    "content": "RAG 정책 harness memory",
                    "authority": "user_explicit",
                    "origin": "user",
                    "status": "active",
                    "source_refs": [],
                    "related": [],
                },
            ]
            (repo / ".team-ai" / "memory" / "curated.jsonl").write_text(
                "".join(json.dumps(memory, ensure_ascii=False) + "\n" for memory in memories),
                encoding="utf-8",
            )

            packet = build_context(repo, "하네스 RAG 정책", limit=5)
            contents = [memory["content"] for memory in packet["memories"]]

            self.assertIn("RAG 정책 harness memory", contents)
            self.assertNotIn("RAG 정책 finance memory", contents)

    def test_harness_mode_filters_source_spans_by_scope(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team-ai").mkdir()
            (repo / "회의록").mkdir()
            (repo / ".team-ai" / "harness_state.yaml").write_text(
                "project_id: team-ai-harness\ncurrent_goal: scope leak 하네스 개선\n",
                encoding="utf-8",
            )
            (repo / "회의록" / "meeting.md").write_text(
                "scope leak 금융 프로젝트 회의 내용",
                encoding="utf-8",
            )

            packet = build_context(repo, "하네스 scope leak", mode="harness", limit=5)
            refs = [span["source_ref"] for span in packet["source_evidence"]]

            self.assertIn(".team-ai/harness_state.yaml#page=1", refs)
            self.assertFalse(any(ref.startswith("회의록/") for ref in refs))
            self.assertTrue(any(match.get("source_ref", "").startswith("회의록/") for match in packet["excluded_matches"]))

    def test_context_packet_separates_authored_hints_and_low_trust(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team-ai" / "memory").mkdir(parents=True)
            (repo / ".team-ai" / "project.yaml").write_text(
                "project_id: finance-architecture\n"
                "scope: project:finance-architecture\n"
                "scope_modes:\n"
                "  normal:\n"
                "    include:\n"
                "      - project:finance-architecture\n"
                "      - org:jo\n"
                "      - domain:finance-ai\n",
                encoding="utf-8",
            )
            (repo / ".team-ai" / "memory" / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-0001",
                        "kind": "principle",
                        "scope": "team",
                        "content": "packet split authored memory",
                        "authority": "user_explicit",
                        "origin": "user",
                        "status": "active",
                        "source_refs": [],
                        "related": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (repo / "자료").mkdir()
            (repo / "자료" / "note.md").write_text("원칙: packet split retrieval hint", encoding="utf-8")
            (repo / "output").mkdir()
            (repo / "output" / "draft.md").write_text("팀은 packet split low trust라고 추정된다.", encoding="utf-8")

            packet = build_context(repo, "packet split", mode="normal", limit=10)

            self.assertEqual(packet["authored_memories"][0]["id"], "MEM-AUTH-0001")
            self.assertTrue(any("retrieval hint" in memory["content"] for memory in packet["retrieval_hints"]))
            self.assertTrue(any(memory["authority"] == "ai_inferred" for memory in packet["low_trust_hints"]))
            self.assertFalse(any(span["source_ref"].startswith("output/") for span in packet["source_evidence"]))

    def test_counterevidence_uses_decision_countercase(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            decision_dir = repo / ".team-ai" / "decisions"
            decision_dir.mkdir(parents=True)
            (decision_dir / "DEC-0001.md").write_text(
                "---\n"
                "id: DEC-0001\n"
                "title: Keep harness light\n"
                "status: active\n"
                "authority: user-approved\n"
                "decided_at: 2026-06-25\n"
                "scope:\n"
                "  - project:team-ai-harness\n"
                "---\n\n"
                "## Decision\n\nKeep the harness light.\n\n"
                "## Strongest Countercase\n\n"
                "A richer graph can preserve more nuance.\n",
                encoding="utf-8",
            )

            packet = build_context(repo, "하네스 결정", mode="harness", limit=5)

            self.assertTrue(any("richer graph" in item["text"] for item in packet["counterevidence"]))

    def test_related_memory_expands_one_hop(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team-ai" / "memory").mkdir(parents=True)
            memories = [
                {
                    "id": "MEM-AUTH-0001",
                    "kind": "principle",
                    "scope": "team",
                    "content": "related anchor unique",
                    "authority": "user_explicit",
                    "origin": "user",
                    "status": "active",
                    "source_refs": [],
                    "related": ["MEM-AUTH-0002"],
                },
                {
                    "id": "MEM-AUTH-0002",
                    "kind": "principle",
                    "scope": "team",
                    "content": "related one hop unique",
                    "authority": "user_explicit",
                    "origin": "user",
                    "status": "active",
                    "source_refs": [],
                    "related": [],
                },
            ]
            (repo / ".team-ai" / "memory" / "curated.jsonl").write_text(
                "".join(json.dumps(memory, ensure_ascii=False) + "\n" for memory in memories),
                encoding="utf-8",
            )

            packet = build_context(repo, "related anchor", limit=1)

            self.assertEqual(packet["memories"][0]["id"], "MEM-AUTH-0001")
            self.assertEqual(packet["related_memories"][0]["id"], "MEM-AUTH-0002")

    def test_unapproved_decision_record_is_not_high_trust_evidence(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            decision_dir = repo / ".team-ai" / "decisions"
            decision_dir.mkdir(parents=True)
            (decision_dir / "DEC-0001.md").write_text(
                "---\n"
                "id: DEC-0001\n"
                "title: Draft only\n"
                "status: active\n"
                "authority: ai-draft\n"
                "decided_at: 2026-06-25\n"
                "scope:\n"
                "  - project:team-ai-harness\n"
                "---\n\n"
                "## Decision\n\nDraft evidence should not be high trust.\n",
                encoding="utf-8",
            )

            packet = build_context(repo, "Draft evidence 하네스", limit=5)

            self.assertEqual(packet["active_decisions"], [])
            decision_spans = [
                span for span in packet["source_evidence"] if span["source_ref"].startswith(".team-ai/decisions/")
            ]
            self.assertTrue(decision_spans)
            self.assertTrue(all(span["trust_level"] != "high" for span in decision_spans))

    def test_team_explicit_memory_cannot_use_unapproved_decision_ref(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import refresh

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            decision_dir = repo / ".team-ai" / "decisions"
            decision_dir.mkdir(parents=True)
            (repo / ".team-ai" / "memory").mkdir(parents=True)
            (decision_dir / "DEC-0001.md").write_text(
                "---\n"
                "id: DEC-0001\n"
                "title: Draft only\n"
                "status: active\n"
                "authority: ai-draft\n"
                "decided_at: 2026-06-25\n"
                "scope:\n"
                "  - project:team-ai-harness\n"
                "---\n\n"
                "## Decision\n\nDraft only.\n",
                encoding="utf-8",
            )
            (repo / ".team-ai" / "memory" / "curated.jsonl").write_text(
                json.dumps(
                    {
                        "id": "MEM-AUTH-0001",
                        "kind": "decision",
                        "scope": "project:team-ai-harness",
                        "content": "승인되지 않은 결정은 팀 결정이 아니다.",
                        "authority": "team_explicit",
                        "origin": "source",
                        "status": "active",
                        "source_refs": [".team-ai/decisions/DEC-0001.md#page=1"],
                        "related": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "team_explicit memory requires approval"):
                refresh(repo)

    def test_supporting_span_prefers_memory_content_match(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "회의록").mkdir()
            intro = "이 문장은 단순한 소개 문장으로 충분히 길다."
            evidence = "원칙: 실제 근거는 이 두 번째 문장이다."
            (repo / "회의록" / "meeting.md").write_text(f"{intro}\n{evidence}", encoding="utf-8")

            packet = build_context(repo, "실제 근거", limit=5)
            memory = next(memory for memory in packet["memories"] if memory["content"] == evidence)

            self.assertEqual(memory["supporting_spans"][0]["text"], evidence)

    def test_config_source_roots_are_used(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team-ai").mkdir()
            (repo / ".team-ai" / "config.yaml").write_text(
                "source_roots:\n"
                "  - custom\n",
                encoding="utf-8",
            )
            (repo / "custom").mkdir()
            (repo / "custom" / "note.md").write_text("원칙: config source root를 사용한다.", encoding="utf-8")

            packet = build_context(repo, "config source root")

            self.assertEqual(packet["refresh"]["sources"], 1)
            self.assertEqual(packet["spans"][0]["source_ref"], "custom/note.md#page=1")

    def test_implementation_context_includes_repo_map(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context, latest_context_run

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "tests").mkdir()
            (repo / "src" / "foo.py").write_text(
                "import json\n\nclass Service:\n    pass\n\ndef handler():\n    return json.dumps({})\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_foo.py").write_text("def test_handler():\n    assert True\n", encoding="utf-8")

            packet = build_context(repo, "foo handler 구현", mode="implementation")
            context = packet["implementation_context"]

            self.assertIn("src/foo.py", context["likely_files"])
            self.assertIn("tests/test_foo.py", context["nearest_tests"])
            python = {item["path"]: item for item in context["repo_map"]["python"]}
            self.assertEqual(python["src/foo.py"]["classes"], ["Service"])
            self.assertEqual(python["src/foo.py"]["functions"], ["handler"])
            self.assertTrue((repo / ".team-ai" / "local" / "last_context.json").exists())
            run = latest_context_run(repo, "implementation")
            self.assertIsNotNone(run)
            self.assertEqual(run["mode"], "implementation")
            self.assertEqual(run["snapshot_id"], packet["refresh"]["snapshot_id"])

    def test_verify_done_runs_configured_commands(self):
        from tempfile import TemporaryDirectory

        from team_ai.verify import verify_done

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team-ai").mkdir()
            (repo / ".team-ai" / "config.yaml").write_text(
                "verification:\n"
                "  commands:\n"
                "    - python -c \"print('ok')\"\n",
                encoding="utf-8",
            )

            result = verify_done(repo, {"project_state": {"finance-architecture": {}}, "active_decisions": []})

            self.assertTrue(result["done"])
            self.assertEqual(result["commands_run"][0]["status"], "passed")
            self.assertEqual(result["context_used"]["project_state"], ["finance-architecture"])

    def test_verify_done_requires_implementation_context_for_product_changes(self):
        from tempfile import TemporaryDirectory

        from team_ai.runtime import build_context
        from team_ai.verify import verify_done

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            (repo / ".team-ai").mkdir()
            (repo / ".team-ai" / "config.yaml").write_text(
                "verification:\n"
                "  commands:\n"
                "    - python -c \"print('ok')\"\n",
                encoding="utf-8",
            )
            (repo / "src").mkdir()
            (repo / "src" / "foo.py").write_text("VALUE = 1\n", encoding="utf-8")

            missing = verify_done(repo)
            self.assertFalse(missing["done"])
            self.assertTrue(any("implementation context" in risk for risk in missing["risks"]))

            build_context(repo, "foo 구현", mode="implementation")
            result = verify_done(repo)

            self.assertTrue(result["done"])

    def test_guard_rejects_unapproved_active_decision(self):
        from tempfile import TemporaryDirectory

        from team_ai.guard import guard

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            decision_dir = repo / ".team-ai" / "decisions"
            decision_dir.mkdir(parents=True)
            (repo / ".team-ai" / "config.yaml").write_text(
                "verification:\n"
                "  commands:\n"
                "    - python -c \"print('ok')\"\n",
                encoding="utf-8",
            )
            (decision_dir / "DEC-0001.md").write_text(
                "---\n"
                "id: DEC-0001\n"
                "title: Draft decision\n"
                "status: active\n"
                "authority: ai-draft\n"
                "decided_at: 2026-06-25\n"
                "---\n\n"
                "## Decision\n\nDraft only.\n",
                encoding="utf-8",
            )

            result = guard(repo)

            self.assertFalse(result["ok"])
            self.assertTrue(any("active decision lacks approval" in error for error in result["errors"]))

    def test_guard_rejects_product_to_harness_import_boundary(self):
        from tempfile import TemporaryDirectory

        from team_ai.guard import guard

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            product_dir = repo / "src" / "finance_architecture"
            product_dir.mkdir(parents=True)
            (repo / ".team-ai").mkdir()
            (repo / ".team-ai" / "config.yaml").write_text(
                "verification:\n"
                "  commands:\n"
                "    - python -c \"print('ok')\"\n",
                encoding="utf-8",
            )
            (product_dir / "app.py").write_text("from team_ai import runtime\n", encoding="utf-8")

            result = guard(repo)

            self.assertFalse(result["ok"])
            self.assertTrue(any("product code imports harness code" in error for error in result["errors"]))

    def test_guard_rejects_tracked_output(self):
        from tempfile import TemporaryDirectory

        from team_ai.guard import guard

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            (repo / ".team-ai").mkdir()
            (repo / ".team-ai" / "config.yaml").write_text(
                "verification:\n"
                "  commands:\n"
                "    - python -c \"print('ok')\"\n",
                encoding="utf-8",
            )
            (repo / "output").mkdir()
            (repo / "output" / "draft.md").write_text("generated", encoding="utf-8")
            subprocess.run(["git", "add", "output/draft.md"], cwd=repo, check=True, capture_output=True)

            result = guard(repo)

            self.assertFalse(result["ok"])
            self.assertTrue(any("forbidden tracked file: output/draft.md" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
