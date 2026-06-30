from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import unicodedata
from fnmatch import fnmatch
from datetime import datetime, timezone
from pathlib import Path

from .config import TEAM_AI_DIR, load_config


TEXT_SUFFIXES = {".md", ".txt", ".html", ".py", ".yaml", ".yml", ".json"}
PDF_SUFFIXES = {".pdf"}
PERCEPTION_VERSION = "memory-promote-v2"
AUTHORED_MEMORY_PREFIX = "MEM-AUTH-"
MEMORY_EVENTS_FILE = "events.jsonl"
GENERATED_MEMORY_FILES = {"current.jsonl"}
RRF_K = 60
COMPILED_TABLES = ("sources", "spans", "memories")
COMPILED_MANIFEST = "manifest.json"
SNAPSHOT_VERSION = 1
CONTEXT_TEXT_LIMIT = 1600
MAX_SEARCH_TERMS = 64
MEMORY_KINDS = {
    "concept",
    "constraint",
    "decision",
    "discussion",
    "fact",
    "note",
    "open_question",
    "preference",
    "principle",
    "risk",
    "task",
}
MEMORY_AUTHORITIES = {
    "external_knowledge",
    "project_observation",
    "individual_opinion",
    "team_explicit",
    "user_explicit",
    "ai_inferred",
}
MEMORY_ORIGINS = {"decision", "inferred", "source", "state", "user"}
MEMORY_STATUSES = {"active", "contested", "deprecated", "draft", "superseded"}
MEMORY_VISIBILITIES = {"private", "project", "workspace", "public"}
MEMORY_SHARE_POLICIES = {"local_only", "share_candidate", "shared_approved", "deprecated_shared"}
MEMORY_INHERITANCE = {"default", "explicit_import_only", "never_inherit"}
COUNTEREVIDENCE_RE = re.compile(r"반대|우려|위험|리스크|한계|문제|보류|기각|counter|risk|concern|limit", re.I)
_PDF_EXTRACTOR_SIGNATURE: str | None = None


def refresh(repo: str | Path) -> dict[str, int]:
    repo = Path(repo)
    db = _connect(repo)
    try:
        _init_db(db)
        _import_compiled_if_empty(repo, db)
        scanned = _scan_source_files(repo)
        _guard_refresh(repo, db, scanned)
        source_ids = _source_ids_for_scan(db, scanned)
        seen: set[str] = set()
        changed = 0
        changed_sources: list[tuple[str, str, Path]] = []
        for path, rel, content_hash, digest in scanned:
            seen.add(rel)
            source_id = source_ids[rel]
            row = db.execute("select hash, path from sources where id = ?", (source_id,)).fetchone()
            if row and row["hash"] == digest and row["path"] == rel:
                continue
            old_row = db.execute("select path from sources where id = ?", (source_id,)).fetchone()
            old_rel = old_row["path"] if old_row else rel
            changed += 1
            changed_sources.append((source_id, rel, path))
            _upsert_source(db, source_id, rel, path, digest)
            _replace_spans_and_memories(db, source_id, rel, path, {old_rel, rel})
        _mark_deleted_sources(db, seen)
        memory_events = _write_memory_events_for_changed_meetings(repo, db, changed_sources)
        _replace_authored_memories(repo, db)
        _rescope_generated_memories(db)
        _validate_integrity(db)
        _rebuild_search_indexes(db)
        db.commit()
        counts = {
            "sources": db.execute("select count(*) from sources where deleted = 0").fetchone()[0],
            "spans": db.execute("select count(*) from spans").fetchone()[0],
            "memories": db.execute("select count(*) from memories").fetchone()[0],
            "changed": changed,
            "memory_events": memory_events,
        }
        counts["snapshot_id"] = _export_compiled(repo, db)
        return counts
    finally:
        db.close()


def build_context(repo: str | Path, question: str, limit: int = 8, mode: str | None = None) -> dict:
    repo = Path(repo)
    requested_mode = _classify_query_mode(question) if mode in (None, "auto", "answer") else mode
    counts = refresh(repo)
    terms = _terms(question)
    intent = _query_intent(question)
    config = load_config(repo)
    db = _connect(repo)
    try:
        project_state = _project_states(repo)
        project = _load_project_config(repo, config, project_state)
        query_mode = _resolve_query_mode(requested_mode, question)
        scopes = _resolve_allowed_scopes(project, query_mode, question, project_state)
        project_state = _scope_project_states(project_state, scopes)
        active_decisions = _decision_records(repo, active_only=True, scopes=scopes)
        decision_history = _decision_records(repo, active_only=False, scopes=scopes) if intent == "history" else []
        spans = _search_rows(db, "spans", "text", terms, limit, question, intent)
        memory_rows = _search_rows(db, "memories", "content", terms, limit * 4, question, intent)
        excluded_matches: list[dict] = []
        spans = _filter_span_scope(spans, scopes, config, excluded_matches)[:limit]
        memories = _filter_memory_scope(memory_rows, scopes, excluded_matches)[:limit]
        memory_packets = [_memory_packet(db, row) for row in memories]
        memory_packets, overrides = _apply_local_overrides(memory_packets, project["scope"])
        related_memories = _expand_related_memories(db, memory_packets, scopes)
        span_packets = [_span_packet(row, config) for row in spans]
        buckets = _memory_buckets(memory_packets)
        packet = {
            "question": question,
            "mode": query_mode,
            "intent": intent,
            "active_project": project["scope"],
            "allowed_scopes": sorted(scopes),
            "refresh": counts,
            "project_state": project_state,
            "active_decisions": active_decisions,
            "active_decisions_by_scope": _decision_buckets(active_decisions),
            "decision_history": decision_history,
            "source_evidence": span_packets,
            "spans": span_packets,
            "memories": memory_packets,
            "authored_memories": buckets["authored_memories"],
            "project_memories": buckets["project_memories"],
            "shared_memories": buckets["shared_memories"],
            "source_highlights": span_packets,
            "retrieval_hints": buckets["retrieval_hints"],
            "low_trust_hints": buckets["low_trust_hints"],
            "related_memories": related_memories,
            "overrides": overrides,
            "excluded_matches": excluded_matches[:20],
            "open_questions": _open_questions(project_state, memory_packets),
            "tasks": _tasks(project_state),
            "counterevidence": _build_counterevidence(repo, active_decisions, memory_packets, span_packets, scopes),
        }
        if requested_mode == "implementation":
            from .repo_map import implementation_context

            packet["implementation_context"] = implementation_context(repo, question)
        _record_context_run(repo, packet, requested_mode)
        return packet
    finally:
        db.close()


def inspect_memory(repo: str | Path, memory_id: str) -> dict | None:
    db = _connect(Path(repo))
    try:
        row = db.execute("select * from memories where id = ?", (memory_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["source_refs"] = json.loads(result["source_refs"])
        result["related"] = json.loads(result["related"])
        return result
    finally:
        db.close()


def latest_context_run(repo: str | Path, mode: str | None = None) -> dict | None:
    path = Path(repo) / TEAM_AI_DIR / "local" / "runs.jsonl"
    if not path.exists():
        return None
    latest = None
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            run = json.loads(line)
            if run.get("type") == "context" and (mode is None or run.get("mode") == mode):
                latest = run
    return latest


def _record_context_run(repo: Path, packet: dict, mode: str) -> None:
    local = repo / TEAM_AI_DIR / "local"
    local.mkdir(parents=True, exist_ok=True)
    summary = {
        "type": "context",
        "mode": packet.get("mode", mode),
        "requested_mode": mode,
        "intent": packet.get("intent"),
        "question": packet["question"],
        "snapshot_id": packet["refresh"]["snapshot_id"],
        "allowed_scopes": packet.get("allowed_scopes", []),
        "active_decisions": [item.get("id") for item in packet.get("active_decisions", [])],
        "memories": [item.get("id") for item in packet.get("memories", [])],
        "source_refs": sorted(
            {
                span.get("source_ref")
                for span in packet.get("source_evidence", [])
                if span.get("source_ref")
            }
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (local / "last_context.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (local / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")


def _connect(repo: Path) -> sqlite3.Connection:
    db_path = repo / TEAM_AI_DIR / "local" / "knowledge.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    return db


def _compiled_dir(repo: Path) -> Path:
    return repo / TEAM_AI_DIR / "compiled"


def _init_db(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        create table if not exists sources (
            id text primary key,
            path text unique not null,
            kind text not null,
            hash text not null,
            updated_at text not null,
            deleted integer not null default 0
        );
        create table if not exists spans (
            id text primary key,
            source_id text not null,
            page integer,
            text text not null,
            source_ref text not null,
            source_type text,
            canonical integer not null default 1,
            generated_by_ai integer not null default 0,
            trust_level text,
            section text,
            speaker text,
            span_index integer,
            bbox text
        );
        create table if not exists memories (
            id text primary key,
            kind text not null,
            scope text not null,
            owner_scope text,
            visibility text,
            share_policy text,
            inheritance text,
            applies_to text,
            excludes text,
            canonical_key text,
            content text not null,
            reason text,
            authority text not null,
            origin text not null,
            status text not null,
            source_refs text not null,
            related text not null,
            occurred_at text,
            valid_from text,
            valid_to text,
            superseded_by text
        );
        create index if not exists idx_spans_source on spans(source_id);
        create index if not exists idx_memories_status on memories(status);
        """
    )
    _ensure_column(db, "memories", "canonical_key", "text")
    _ensure_column(db, "memories", "owner_scope", "text")
    _ensure_column(db, "memories", "visibility", "text")
    _ensure_column(db, "memories", "share_policy", "text")
    _ensure_column(db, "memories", "inheritance", "text")
    _ensure_column(db, "memories", "applies_to", "text")
    _ensure_column(db, "memories", "excludes", "text")
    _ensure_column(db, "spans", "source_type", "text")
    _ensure_column(db, "spans", "canonical", "integer not null default 1")
    _ensure_column(db, "spans", "generated_by_ai", "integer not null default 0")
    _ensure_column(db, "spans", "trust_level", "text")
    _ensure_column(db, "spans", "section", "text")
    _ensure_column(db, "spans", "speaker", "text")
    _ensure_column(db, "spans", "span_index", "integer")
    _ensure_column(db, "spans", "bbox", "text")
    _init_search_indexes(db)


def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row["name"] for row in db.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"alter table {table} add column {column} {declaration}")


def _init_search_indexes(db: sqlite3.Connection) -> None:
    try:
        db.executescript(
            """
            create virtual table if not exists spans_fts
                using fts5(id unindexed, text, source_ref);
            create virtual table if not exists spans_trigram
                using fts5(id unindexed, text, source_ref, tokenize='trigram');
            create virtual table if not exists memories_fts
                using fts5(id unindexed, content, canonical_key);
            create virtual table if not exists memories_trigram
                using fts5(id unindexed, content, canonical_key, tokenize='trigram');
            """
        )
    except sqlite3.OperationalError:
        pass


def _has_search_indexes(db: sqlite3.Connection) -> bool:
    row = db.execute("select 1 from sqlite_master where type = 'table' and name = 'spans_fts'").fetchone()
    return row is not None


def _rebuild_search_indexes(db: sqlite3.Connection) -> None:
    if not _has_search_indexes(db):
        return
    # ponytail: full rebuild is simpler; add incremental triggers only if refresh latency matters.
    for table in ("spans_fts", "spans_trigram", "memories_fts", "memories_trigram"):
        db.execute(f"delete from {table}")
    for row in db.execute("select id, text, source_ref from spans").fetchall():
        db.execute("insert into spans_fts(id, text, source_ref) values (?, ?, ?)", row)
        db.execute("insert into spans_trigram(id, text, source_ref) values (?, ?, ?)", row)
    for row in db.execute("select id, content, coalesce(canonical_key, '') from memories").fetchall():
        db.execute("insert into memories_fts(id, content, canonical_key) values (?, ?, ?)", row)
        db.execute("insert into memories_trigram(id, content, canonical_key) values (?, ?, ?)", row)


def _export_compiled(repo: Path, db: sqlite3.Connection) -> str:
    out = _compiled_dir(repo)
    out.mkdir(parents=True, exist_ok=True)
    tmp_dir = out / ".snapshot.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir()
    try:
        table_texts = {table: _compiled_table_text(db, table) for table in COMPILED_TABLES}
        manifest = _snapshot_manifest(table_texts)
        for table, text in table_texts.items():
            (tmp_dir / f"{table}.jsonl").write_text(text, encoding="utf-8")
        (tmp_dir / COMPILED_MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _validate_compiled_dir(tmp_dir, manifest)
        for table in COMPILED_TABLES:
            (tmp_dir / f"{table}.jsonl").replace(out / f"{table}.jsonl")
        (tmp_dir / COMPILED_MANIFEST).replace(out / COMPILED_MANIFEST)
        return manifest["generation_id"]
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)


def _compiled_table_text(db: sqlite3.Connection, table: str) -> str:
    rows = db.execute(f"select * from {table} order by id").fetchall()
    return "".join(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n" for row in rows)


def _snapshot_manifest(table_texts: dict[str, str]) -> dict:
    tables = {
        table: {
            "rows": text.count("\n"),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        for table, text in sorted(table_texts.items())
    }
    payload = json.dumps(tables, sort_keys=True).encode("utf-8")
    return {
        "version": SNAPSHOT_VERSION,
        "perception_version": PERCEPTION_VERSION,
        "generation_id": hashlib.sha256(payload).hexdigest(),
        "tables": tables,
    }


def _validate_compiled_dir(base: Path, manifest: dict) -> None:
    for table, expected in manifest["tables"].items():
        path = base / f"{table}.jsonl"
        text = path.read_text(encoding="utf-8")
        if text.count("\n") != expected["rows"]:
            raise RuntimeError(f"compiled snapshot row mismatch: {table}")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected["sha256"]:
            raise RuntimeError(f"compiled snapshot hash mismatch: {table}")


def _import_compiled_if_empty(repo: Path, db: sqlite3.Connection) -> None:
    if db.execute("select count(*) from sources").fetchone()[0]:
        return
    base = _compiled_dir(repo)
    if not (base / "sources.jsonl").exists():
        return
    manifest_path = base / COMPILED_MANIFEST
    if not manifest_path.exists():
        raise RuntimeError("compiled snapshot manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != SNAPSHOT_VERSION:
        raise RuntimeError("compiled snapshot version mismatch")
    if manifest.get("perception_version") != PERCEPTION_VERSION:
        raise RuntimeError("compiled snapshot perception version mismatch")
    _validate_compiled_dir(base, manifest)
    for table in COMPILED_TABLES:
        path = base / f"{table}.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                columns = list(row)
                placeholders = ", ".join(["?"] * len(columns))
                db.execute(
                    f"insert or replace into {table} ({', '.join(columns)}) values ({placeholders})",
                    [row[col] for col in columns],
                )
    db.commit()


def _scan_source_files(repo: Path) -> list[tuple[Path, str, str, str]]:
    scanned: list[tuple[Path, str, str, str]] = []
    for path in _iter_source_files(repo):
        rel = _repo_rel(path, repo)
        if _is_lfs_pointer(path):
            raise RuntimeError(f"source appears to be a Git LFS pointer, not content: {rel}")
        content_hash = _sha256(path)
        scanned.append((path, rel, content_hash, _source_digest(path, content_hash)))
    return scanned


def _iter_source_files(repo: Path):
    for root in _source_roots(repo):
        base = _source_root_path(repo, root)
        if not base:
            continue
        if base.is_file():
            if base.suffix.lower() in TEXT_SUFFIXES | PDF_SUFFIXES:
                yield base
            continue
        for path in sorted(base.rglob("*"), key=lambda item: _nfc(item.as_posix())):
            if not path.is_file() or path.name == ".DS_Store":
                continue
            if path.name == COMPILED_MANIFEST and path.parent.name == "compiled":
                continue
            if path.suffix.lower() in TEXT_SUFFIXES | PDF_SUFFIXES:
                yield path


def _source_root_path(repo: Path, root: str) -> Path | None:
    if not root.startswith("../") and not Path(root).is_absolute() and (repo / _escaped_unicode(root)).exists():
        raise RuntimeError(f"escaped Unicode source root is unsafe: {_escaped_unicode(root)}")
    exact = repo / root
    if exact.exists():
        return exact
    parts = root.split("/")
    base = repo
    for part in parts:
        match = _child_named_nfc(base, part)
        if not match:
            return None
        base = match
    return base


def _child_named_nfc(base: Path, name: str) -> Path | None:
    if not base.exists():
        return None
    wanted = _nfc(name)
    for child in base.iterdir():
        if child.is_dir() and _nfc(child.name) == wanted:
            return child
    return None


def _repo_rel(path: Path, repo: Path) -> str:
    return _nfc(os.path.relpath(path.resolve(), repo.resolve()).replace(os.sep, "/"))


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _escaped_unicode(value: str) -> str:
    return "".join(f"#U{ord(char):04x}" for char in value)


def _is_lfs_pointer(path: Path) -> bool:
    if path.stat().st_size > 300:
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:120]
    except OSError:
        return False
    return head.startswith("version https://git-lfs.github.com/spec/v1")


def _guard_refresh(repo: Path, db: sqlite3.Connection, scanned: list[tuple[Path, str, str, str]]) -> None:
    seen = {rel for _, rel, _, _ in scanned}
    active_paths = [
        row["path"]
        for row in db.execute("select path from sources where deleted = 0").fetchall()
    ]
    missing_roots = sorted(
        root
        for root in _source_roots(repo)
        if _source_root_path(repo, root) is None
        and not any(rel == root or rel.startswith(f"{root}/") for rel in seen)
        and any(path == root or path.startswith(f"{root}/") for path in active_paths)
    )
    if missing_roots:
        raise RuntimeError(f"refusing destructive refresh; source root missing: {', '.join(missing_roots)}")
    if len(active_paths) >= 10 and len(seen) < len(active_paths) // 2:
        raise RuntimeError(f"refusing destructive refresh; sources dropped from {len(active_paths)} to {len(seen)}")


def _source_roots(repo: Path) -> list[str]:
    roots = list(load_config(repo)["source_roots"])
    roots.extend(_display_ref(repo, pack / "sources") for pack in _shared_pack_dirs(repo))
    return roots


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _source_digest(path: Path, content_hash: str | None = None) -> str:
    content_hash = content_hash or _sha256(path)
    return f"{content_hash}:{PERCEPTION_VERSION}:{_extractor_signature(path)}"


def _source_ids_for_scan(db: sqlite3.Connection, scanned: list[tuple[Path, str, str, str]]) -> dict[str, str]:
    rows = db.execute("select id, path, hash from sources where deleted = 0").fetchall()
    existing_by_path = {row["path"]: row for row in rows}
    old_by_hash: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        old_by_hash.setdefault(_content_hash(row["hash"]), []).append(row)
    new_by_hash: dict[str, list[str]] = {}
    for _, rel, content_hash, _ in scanned:
        if rel not in existing_by_path:
            new_by_hash.setdefault(content_hash, []).append(rel)
    seen = {rel for _, rel, _, _ in scanned}
    result: dict[str, str] = {}
    for _, rel, content_hash, _ in scanned:
        if rel in existing_by_path:
            result[rel] = existing_by_path[rel]["id"]
            continue
        moved_from = [row for row in old_by_hash.get(content_hash, []) if row["path"] not in seen]
        if len(moved_from) == 1 and len(new_by_hash.get(content_hash, [])) == 1:
            result[rel] = moved_from[0]["id"]
            continue
        result[rel] = "SRC-" + hashlib.sha1(f"{content_hash}:{rel}".encode("utf-8")).hexdigest()[:12]
    return result


def _content_hash(digest: str) -> str:
    return digest.split(":", 1)[0]


def _extractor_signature(path: Path) -> str:
    if path.suffix.lower() not in PDF_SUFFIXES:
        return "text"
    return _pdf_extractor_signature()


def _pdf_extractor_signature() -> str:
    global _PDF_EXTRACTOR_SIGNATURE
    if _PDF_EXTRACTOR_SIGNATURE is not None:
        return _PDF_EXTRACTOR_SIGNATURE
    binary = shutil.which("pdftotext")
    if not binary:
        raise RuntimeError("pdftotext is required before refreshing PDF sources")
    try:
        result = subprocess.run(
            [binary, "-v"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError("pdftotext version check failed") from exc
    version = (result.stderr or result.stdout).strip().splitlines()[0] if (result.stderr or result.stdout).strip() else binary
    _PDF_EXTRACTOR_SIGNATURE = f"pdftotext:{version}"
    return _PDF_EXTRACTOR_SIGNATURE


def _span_id(source_id: str, index: int) -> str:
    return f"SPAN-{hashlib.sha1(f'{source_id}:{index}'.encode()).hexdigest()[:12]}"


def _memory_id(span_id: str) -> str:
    return "MEM-" + hashlib.sha1(span_id.encode()).hexdigest()[:12]


def _upsert_source(db: sqlite3.Connection, source_id: str, rel: str, path: Path, digest: str) -> None:
    kind = "pdf" if path.suffix.lower() in PDF_SUFFIXES else "text"
    db.execute(
        """
        insert into sources(id, path, kind, hash, updated_at, deleted)
        values (?, ?, ?, ?, ?, 0)
        on conflict(id) do update set
            path = excluded.path,
            kind = excluded.kind,
            hash = excluded.hash,
            updated_at = excluded.updated_at,
            deleted = 0
        """,
        (source_id, rel, kind, digest, datetime.now(timezone.utc).isoformat()),
    )


def _replace_spans_and_memories(
    db: sqlite3.Connection,
    source_id: str,
    rel: str,
    path: Path,
    memory_rels: set[str],
) -> None:
    db.execute("delete from spans where source_id = ?", (source_id,))
    for memory_rel in memory_rels:
        _delete_memories_for_source(db, memory_rel)
    spans = _extract_spans(rel, path)
    for index, span in enumerate(spans, 1):
        span_id = _span_id(source_id, index)
        metadata = _span_metadata(rel, path, span["text"], index)
        db.execute(
            """
            insert into spans(
                id, source_id, page, text, source_ref, source_type, canonical, generated_by_ai,
                trust_level, section, speaker, span_index, bbox
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                span_id,
                source_id,
                span["page"],
                span["text"],
                span["source_ref"],
                metadata["source_type"],
                metadata["canonical"],
                metadata["generated_by_ai"],
                metadata["trust_level"],
                metadata["section"],
                metadata["speaker"],
                index,
                None,
            ),
        )
        if not _should_promote_to_memory(span["text"], rel):
            continue
        memory = _memory_for_span(span_id, rel, span)
        db.execute(
            """
            insert into memories(
                id, kind, scope, owner_scope, visibility, share_policy, inheritance, applies_to, excludes,
                canonical_key, content, reason, authority, origin, status, source_refs,
                related, occurred_at, valid_from, valid_to, superseded_by
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory["id"],
                memory["kind"],
                memory["scope"],
                memory["owner_scope"],
                memory["visibility"],
                memory["share_policy"],
                memory["inheritance"],
                json.dumps(memory["applies_to"], ensure_ascii=False),
                json.dumps(memory["excludes"], ensure_ascii=False),
                memory.get("canonical_key"),
                memory["content"],
                memory["reason"],
                memory["authority"],
                memory["origin"],
                memory["status"],
                json.dumps(memory["source_refs"], ensure_ascii=False),
                json.dumps(memory["related"], ensure_ascii=False),
                None,
                None,
                None,
                None,
            ),
        )


def _write_memory_events_for_changed_meetings(
    repo: Path,
    db: sqlite3.Connection,
    changed_sources: list[tuple[str, str, Path]],
) -> int:
    written = 0
    for source_id, rel, path in changed_sources:
        if not _meeting_source(rel):
            continue
        event_path = _memory_event_path_for_source(repo, rel, path)
        if event_path is None:
            continue
        existing_ids = _event_memory_ids(event_path)
        events: list[dict] = []
        for row in db.execute(
            "select id, page, text, source_ref, span_index from spans where source_id = ? order by span_index",
            (source_id,),
        ).fetchall():
            if not _should_promote_to_memory(row["text"], rel):
                continue
            event = _memory_event_for_span(rel, row)
            if event["memory"]["id"] not in existing_ids:
                events.append(event)
                existing_ids.add(event["memory"]["id"])
            db.execute("delete from memories where id = ?", (_memory_id(row["id"]),))
        if not events:
            continue
        event_path.parent.mkdir(parents=True, exist_ok=True)
        with event_path.open("a", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        written += len(events)
    return written


def _memory_event_path_for_source(repo: Path, rel: str, path: Path) -> Path | None:
    pack = _shared_pack_root_for_path(path)
    if pack is not None:
        memory_dir = pack / "memory"
        return memory_dir / MEMORY_EVENTS_FILE if memory_dir.exists() else None
    if rel.startswith((".team-ai/sources/meetings/", "회의록/")):
        memory_dir = repo / TEAM_AI_DIR / "memory"
        return memory_dir / MEMORY_EVENTS_FILE if memory_dir.exists() else None
    return None


def _shared_pack_root_for_path(path: Path) -> Path | None:
    for parent in path.resolve().parents:
        if parent.parent.name == "packs" and parent.parent.parent.name == "shared":
            return parent
    return None


def _event_memory_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            event = json.loads(line)
            memory = event.get("memory") or {}
            if memory.get("id"):
                ids.add(memory["id"])
    return ids


def _memory_event_for_span(rel: str, span: sqlite3.Row) -> dict:
    source_ref = _portable_source_ref(span["source_ref"])
    scope = _scope_for_source_path(rel)
    canonical_key = f"{scope}:source:{source_ref.split('#', 1)[0]}:span:{span['span_index']}"
    content = span["text"][:900]
    memory_id = "MEM-AUTH-AUTO-" + hashlib.sha1(f"{canonical_key}:{content}".encode("utf-8")).hexdigest()[:12]
    valid_from = _date_from_source_path(rel)
    memory = {
        "id": memory_id,
        "kind": _kind(span["text"], rel),
        "scope": scope,
        "canonical_key": canonical_key,
        "content": content,
        "reason": "Automatically promoted from a synced meeting source.",
        "authority": _authority(span["text"], rel),
        "origin": "source",
        "status": "active",
        "source_refs": [source_ref],
        "related": [],
        "share_policy": "share_candidate",
    }
    if valid_from:
        memory["occurred_at"] = valid_from
        memory["valid_from"] = valid_from
    return {
        "event_id": "MEM-EVT-AUTO-" + hashlib.sha1(memory_id.encode("utf-8")).hexdigest()[:12],
        "operation": "upsert",
        "canonical_key": canonical_key,
        "effective_from": valid_from,
        "reason": memory["reason"],
        "memory": memory,
    }


def _portable_source_ref(source_ref: str) -> str:
    path, sep, suffix = source_ref.partition("#")
    match = re.search(r"(?:^|/)shared/packs/([^/]+)/(.+)$", path)
    if not match:
        return source_ref
    return f"pack://{match.group(1)}/{match.group(2)}{sep}{suffix}"


def _date_from_source_path(rel: str) -> str | None:
    match = re.search(r"(20\d{2})[._-](\d{2})[._-](\d{2})", rel)
    if not match:
        return None
    return "-".join(match.groups())


def _extract_spans(rel: str, path: Path) -> list[dict]:
    if path.suffix.lower() in PDF_SUFFIXES:
        text = _pdf_text(path)
        pages = text.split("\f") if text else [""]
        spans = []
        for page_no, page in enumerate(pages, 1):
            for chunk in _chunks(page):
                spans.append({"page": page_no, "text": chunk, "source_ref": f"{rel}#page={page_no}"})
        return spans or [{"page": 1, "text": rel, "source_ref": f"{rel}#page=1"}]
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [
        {"page": 1, "text": chunk, "source_ref": f"{rel}#page=1"}
        for chunk in _chunks(text)
    ] or [{"page": 1, "text": rel, "source_ref": f"{rel}#page=1"}]


def _pdf_text(path: Path) -> str:
    binary = shutil.which("pdftotext")
    if not binary:
        raise RuntimeError("pdftotext is required before refreshing PDF sources")
    try:
        return subprocess.run(
            [binary, "-layout", str(path), "-"],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"PDF text extraction failed: {path}") from exc


def _chunks(text: str) -> list[str]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n|\n", text) if b.strip()]
    chunks: list[str] = []
    for block in blocks:
        compact = re.sub(r"[ \t]+", " ", block)
        if len(compact) <= 900:
            chunks.append(compact)
            continue
        for i in range(0, len(compact), 900):
            chunks.append(compact[i : i + 900].strip())
    return chunks


def _span_metadata(rel: str, path: Path, text: str, index: int) -> dict:
    source_type = _source_type(rel)
    generated = 1 if source_type == "ai_output" else 0
    return {
        "source_type": source_type,
        "canonical": 0 if generated else 1,
        "generated_by_ai": generated,
        "trust_level": _trust_level(source_type, path),
        "section": _infer_section(text, index),
        "speaker": _infer_speaker(text),
    }


def _source_type(rel: str) -> str:
    if _meeting_source(rel):
        return "meeting_note"
    if _reference_source(rel):
        return "reference_material"
    if rel.startswith(".team-ai/sources/notes/"):
        return "source_note"
    if rel.startswith(".team-ai/sources/uploads/"):
        return "uploaded_source"
    if _output_source(rel):
        return "ai_output"
    if rel.startswith(".team-ai/decisions/"):
        return "decision_record"
    if rel == ".team-ai/project.yaml":
        return "project_config"
    if rel in {".team-ai/state.yaml", ".team-ai/harness_state.yaml"}:
        return "project_state"
    return "source"


def _trust_level(source_type: str, path: Path | None = None) -> str:
    if source_type == "decision_record" and (path is None or not _approved_decision_file(path)):
        return "low"
    return {
        "decision_record": "high",
        "project_config": "high",
        "project_state": "high",
        "meeting_note": "medium",
        "source_note": "medium",
        "uploaded_source": "medium",
        "reference_material": "medium",
        "compiled_snapshot": "medium",
        "ai_output": "low",
    }.get(source_type, "medium")


def _approved_decision_file(path: Path) -> bool:
    try:
        front = _front_matter(path.read_text(encoding="utf-8"))
    except OSError:
        return False
    return _approved_active_decision(front)


def _infer_section(text: str, index: int) -> str | None:
    match = re.match(r"#{1,6}\s+(.+)", text)
    if match:
        return match.group(1).strip()
    if index == 1:
        return "front"
    return None


def _infer_speaker(text: str) -> str | None:
    match = re.match(r"([가-힣A-Za-z][가-힣A-Za-z ._-]{1,40})\s*[:：]", text)
    return match.group(1).strip() if match else None


def _memory_for_span(span_id: str, rel: str, span: dict) -> dict:
    text = span["text"]
    scope = _scope_for_source_path(rel)
    return {
        "id": _memory_id(span_id),
        "kind": _kind(text, rel),
        "scope": scope,
        "owner_scope": scope,
        "visibility": _default_visibility(scope),
        "share_policy": "local_only",
        "inheritance": "never_inherit" if scope.startswith("project:") else "explicit_import_only",
        "applies_to": [],
        "excludes": [],
        "content": text[:900],
        "reason": None,
        "authority": _authority(text, rel),
        "origin": "inferred" if _output_source(rel) else "source",
        "status": "active",
        "source_refs": [span["source_ref"]],
        "related": [],
    }


def _should_promote_to_memory(text: str, rel: str) -> bool:
    if _is_noise_span(text):
        return False
    if _reference_source(rel):
        return bool(re.search(r"원칙|기준|제약|주의|금지|리스크|위험|must|should|recommend", text, re.I))
    if _meeting_source(rel) or rel.startswith(".team-ai/sources/notes/"):
        return bool(
            re.search(
                r"결정|합의|하기로|하자|해야|우선|선호|원칙|기준|보류|기각|폐기|대체|반대|우려|문제|쟁점|미해결|질문|방향|경향",
                text,
            )
        )
    if _output_source(rel):
        return bool(re.search(r"팀은|우리는|결정|합의|원칙|기준|보류|기각|선호|경향|추정|미해결|쟁점|질문", text))
    return False


def _is_noise_span(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 12:
        return True
    if re.fullmatch(r"[\W_]+", stripped):
        return True
    if re.fullmatch(r"[a-zA-Z-]+\s*:\s*[^;{}]+;?", stripped):
        return True
    if stripped.startswith(("<", "</", "{", "}", ".", "#")):
        return True
    return False


def _kind(text: str, rel: str) -> str:
    if "?" in text or "미해결" in text:
        return "open_question"
    if re.search(r"결정|보류|기각|선택|우선|추천|하자", text):
        return "decision"
    if re.search(r"원칙|기준|경향|중요", text):
        return "principle"
    if _meeting_source(rel) or rel.startswith(".team-ai/sources/notes/"):
        return "discussion"
    return "concept"


def _authority(text: str, rel: str) -> str:
    if _output_source(rel):
        return "ai_inferred"
    if _reference_source(rel):
        return "external_knowledge"
    if rel.startswith(".team-ai/decisions/"):
        return "team_explicit"
    if rel in {".team-ai/state.yaml", ".team-ai/harness_state.yaml"}:
        return "project_observation"
    return "individual_opinion"


def _mark_deleted_sources(db: sqlite3.Connection, seen: set[str]) -> None:
    for row in db.execute("select id, path from sources where deleted = 0").fetchall():
        if row["path"] not in seen:
            _delete_source_knowledge(db, row["id"], row["path"])


def _delete_source_knowledge(db: sqlite3.Connection, source_id: str, rel: str) -> None:
    db.execute("delete from spans where source_id = ?", (source_id,))
    _delete_memories_for_source(db, rel)
    db.execute("delete from sources where id = ?", (source_id,))


def _delete_memories_for_source(db: sqlite3.Connection, rel: str) -> None:
    for row in db.execute("select id, source_refs from memories").fetchall():
        try:
            refs = [_nfc(ref) for ref in json.loads(row["source_refs"])]
        except json.JSONDecodeError:
            refs = []
        if any(ref == rel or ref.startswith(f"{rel}#") for ref in refs):
            db.execute("delete from memories where id = ?", (row["id"],))


def _replace_authored_memories(repo: Path, db: sqlite3.Connection) -> None:
    db.execute("delete from memories where id like ?", (f"{AUTHORED_MEMORY_PREFIX}%",))
    projected: dict[str, dict] = {}
    active_by_key: dict[tuple[str, str], str] = {}
    for base in _memory_dirs(repo):
        if not base.exists():
            continue
        for path in sorted(base.glob("*.jsonl")):
            if path.name in GENERATED_MEMORY_FILES:
                continue
            if path.name == MEMORY_EVENTS_FILE:
                _project_memory_events(projected, active_by_key, path, repo)
                continue
            with path.open(encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    memory = _authored_memory(json.loads(line), path, line_no, repo)
                    _project_memory(projected, active_by_key, memory)
    for memory in projected.values():
        _insert_authored_memory(db, memory)


def _memory_dirs(repo: Path) -> list[Path]:
    return [repo / TEAM_AI_DIR / "memory", *[pack / "memory" for pack in _shared_pack_dirs(repo)]]


def _rescope_generated_memories(db: sqlite3.Connection) -> None:
    for row in db.execute("select * from memories where id not like ?", (f"{AUTHORED_MEMORY_PREFIX}%",)).fetchall():
        refs = _json_list(row["source_refs"])
        scope = _scope_for_source_ref(refs[0]) if refs else _normalize_scope(row["scope"])
        owner_scope = row["owner_scope"] or scope
        visibility = row["visibility"] or _default_visibility(scope)
        share_policy = row["share_policy"] or "local_only"
        inheritance = row["inheritance"] or ("never_inherit" if scope.startswith("project:") else "explicit_import_only")
        applies_to = row["applies_to"] or "[]"
        excludes = row["excludes"] or "[]"
        if (
            row["scope"],
            row["owner_scope"],
            row["visibility"],
            row["share_policy"],
            row["inheritance"],
            row["applies_to"],
            row["excludes"],
        ) == (scope, owner_scope, visibility, share_policy, inheritance, applies_to, excludes):
            continue
        db.execute(
            """
            update memories
            set scope = ?, owner_scope = ?, visibility = ?, share_policy = ?, inheritance = ?,
                applies_to = ?, excludes = ?
            where id = ?
            """,
            (scope, owner_scope, visibility, share_policy, inheritance, applies_to, excludes, row["id"]),
        )


def _project_memory_events(
    projected: dict[str, dict],
    active_by_key: dict[tuple[str, str], str],
    path: Path,
    repo: Path,
) -> None:
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            event = _authored_memory_event(json.loads(line), path, line_no)
            if event["operation"] != "upsert":
                continue
            memory = _authored_memory(event["memory"], path, line_no, repo)
            if event.get("canonical_key") and not memory.get("canonical_key"):
                memory["canonical_key"] = event["canonical_key"]
            if event.get("effective_from") and not memory.get("valid_from"):
                memory["valid_from"] = event["effective_from"]
            if event.get("reason") and not memory.get("reason"):
                memory["reason"] = event["reason"]
            for memory_id in event.get("supersedes", []):
                if memory_id in projected:
                    projected[memory_id]["status"] = "superseded"
                    projected[memory_id]["superseded_by"] = memory["id"]
                    if not projected[memory_id].get("valid_to"):
                        projected[memory_id]["valid_to"] = memory.get("valid_from")
            _project_memory(projected, active_by_key, memory)


def _project_memory(projected: dict[str, dict], active_by_key: dict[tuple[str, str], str], memory: dict) -> None:
    canonical_key = memory.get("canonical_key")
    if canonical_key and memory.get("status") == "active":
        key = (memory["scope"], canonical_key)
        previous_id = active_by_key.get(key)
        if previous_id and previous_id != memory["id"] and previous_id in projected:
            projected[previous_id]["status"] = "superseded"
            projected[previous_id]["superseded_by"] = memory["id"]
            if not projected[previous_id].get("valid_to"):
                projected[previous_id]["valid_to"] = memory.get("valid_from")
        active_by_key[key] = memory["id"]
    projected[memory["id"]] = memory


def _insert_authored_memory(db: sqlite3.Connection, memory: dict) -> None:
    db.execute(
        """
        insert into memories(
            id, kind, scope, owner_scope, visibility, share_policy, inheritance, applies_to, excludes,
            canonical_key, content, reason, authority, origin, status, source_refs,
            related, occurred_at, valid_from, valid_to, superseded_by
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory["id"],
            memory["kind"],
            memory["scope"],
            memory["owner_scope"],
            memory["visibility"],
            memory["share_policy"],
            memory["inheritance"],
            json.dumps(memory["applies_to"], ensure_ascii=False),
            json.dumps(memory["excludes"], ensure_ascii=False),
            memory.get("canonical_key"),
            memory["content"],
            memory.get("reason"),
            memory["authority"],
            memory["origin"],
            memory["status"],
            json.dumps(memory["source_refs"], ensure_ascii=False),
            json.dumps(memory.get("related", []), ensure_ascii=False),
            memory.get("occurred_at"),
            memory.get("valid_from"),
            memory.get("valid_to"),
            memory.get("superseded_by"),
        ),
    )


def _authored_memory(row: dict, path: Path, line_no: int, repo: Path | None = None) -> dict:
    row = dict(row)
    required = ("id", "kind", "scope", "content", "authority", "origin", "status", "source_refs")
    missing = [field for field in required if field not in row]
    if missing:
        raise ValueError(f"{path}:{line_no} missing fields: {', '.join(missing)}")
    if not row["id"].startswith(AUTHORED_MEMORY_PREFIX):
        raise ValueError(f"{path}:{line_no} authored memory id must start with {AUTHORED_MEMORY_PREFIX}")
    if not isinstance(row["source_refs"], list):
        raise ValueError(f"{path}:{line_no} source_refs must be a list")
    if row["kind"] not in MEMORY_KINDS:
        raise ValueError(f"{path}:{line_no} invalid kind: {row['kind']}")
    if row["scope"] == "cross-project":
        raise ValueError(f"{path}:{line_no} cross-project scope is deprecated")
    if not _valid_scope(row["scope"]):
        raise ValueError(f"{path}:{line_no} invalid scope: {row['scope']}")
    if row["authority"] not in MEMORY_AUTHORITIES:
        raise ValueError(f"{path}:{line_no} invalid authority: {row['authority']}")
    if row["origin"] not in MEMORY_ORIGINS:
        raise ValueError(f"{path}:{line_no} invalid origin: {row['origin']}")
    if row["authority"] == "ai_inferred" or row["origin"] == "inferred":
        raise ValueError(f"{path}:{line_no} authored memory must preserve source context, not inference")
    if row["status"] not in MEMORY_STATUSES:
        raise ValueError(f"{path}:{line_no} invalid status: {row['status']}")
    if row["origin"] != "user" and not row["source_refs"]:
        raise ValueError(f"{path}:{line_no} source_refs are required unless origin is user")
    row["source_refs"] = [_nfc(ref) for ref in row["source_refs"]]
    row["owner_scope"] = row.get("owner_scope") or row["scope"]
    row["visibility"] = row.get("visibility") or _default_visibility(row["scope"])
    row["share_policy"] = row.get("share_policy") or "local_only"
    row["inheritance"] = row.get("inheritance") or (
        "never_inherit" if row["scope"].startswith("project:") else "explicit_import_only"
    )
    row["applies_to"] = row.get("applies_to") or []
    row["excludes"] = row.get("excludes") or []
    if not _valid_scope(row["owner_scope"]):
        raise ValueError(f"{path}:{line_no} invalid owner_scope: {row['owner_scope']}")
    if row["visibility"] not in MEMORY_VISIBILITIES:
        raise ValueError(f"{path}:{line_no} invalid visibility: {row['visibility']}")
    if row["share_policy"] not in MEMORY_SHARE_POLICIES:
        raise ValueError(f"{path}:{line_no} invalid share_policy: {row['share_policy']}")
    if row["inheritance"] not in MEMORY_INHERITANCE:
        raise ValueError(f"{path}:{line_no} invalid inheritance: {row['inheritance']}")
    if row["authority"] == "ai_inferred" and row["share_policy"] == "shared_approved":
        raise ValueError(f"{path}:{line_no} ai_inferred memory cannot be shared_approved")
    if _output_derived(row) and row["share_policy"] == "shared_approved":
        raise ValueError(f"{path}:{line_no} output-derived memory cannot be shared_approved")
    if row["authority"] == "team_explicit" and not _has_team_explicit_approval(row, repo):
        raise ValueError(f"{path}:{line_no} team_explicit memory requires approval")
    return row


def _authored_memory_event(row: dict, path: Path, line_no: int) -> dict:
    if row.get("operation") != "upsert":
        raise ValueError(f"{path}:{line_no} unsupported memory event operation")
    if not isinstance(row.get("memory"), dict):
        raise ValueError(f"{path}:{line_no} missing memory object")
    return row


def _valid_scope(scope: str) -> bool:
    if scope == "team":
        return True
    return bool(re.match(r"^(org|harness|domain|program|project|task|workspace):[A-Za-z0-9_.-]+$", scope))


def _normalize_scope(scope: str | None) -> str:
    if scope == "cross-project":
        return "domain:agent-infra"
    return scope or "project:finance-architecture"


def _default_visibility(scope: str) -> str:
    if scope.startswith("project:") or scope.startswith("task:"):
        return "project"
    return "workspace"


def _output_derived(row: dict) -> bool:
    return row.get("origin") == "inferred" and any(str(ref).startswith("output/") for ref in row.get("source_refs", []))


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _has_team_explicit_approval(row: dict, repo: Path | None) -> bool:
    if row.get("approved_by") and row.get("approved_at"):
        return True
    return any(_approved_decision_ref(repo, ref) for ref in row.get("source_refs", []))


def _approved_decision_ref(repo: Path | None, ref: str) -> bool:
    if repo is None or not ref.startswith(".team-ai/decisions/"):
        return False
    path = repo / ref.split("#", 1)[0]
    return _approved_decision_file(path)


def _validate_integrity(db: sqlite3.Connection) -> None:
    source_paths = {row["path"] for row in db.execute("select path from sources where deleted = 0").fetchall()}
    span_refs = {row["source_ref"] for row in db.execute("select source_ref from spans").fetchall()}
    memory_ids = {row["id"] for row in db.execute("select id from memories").fetchall()}
    edges: dict[str, str] = {}
    for row in db.execute("select * from memories").fetchall():
        refs = json.loads(row["source_refs"])
        if row["scope"] == "cross-project":
            raise ValueError(f"{row['id']} uses deprecated cross-project scope")
        if row["scope"] and not _valid_scope(row["scope"]):
            raise ValueError(f"{row['id']} invalid scope: {row['scope']}")
        if row["authority"] == "ai_inferred" and row["share_policy"] == "shared_approved":
            raise ValueError(f"{row['id']} ai_inferred memory cannot be shared_approved")
        for ref in refs:
            if not _source_ref_exists(ref, source_paths, span_refs):
                raise ValueError(f"{row['id']} source_ref does not resolve: {ref}")
        if row["superseded_by"]:
            if row["superseded_by"] == row["id"]:
                raise ValueError(f"{row['id']} supersedes itself")
            if row["superseded_by"] not in memory_ids:
                raise ValueError(f"{row['id']} superseded_by missing: {row['superseded_by']}")
            edges[row["id"]] = row["superseded_by"]
        if row["valid_from"] and row["valid_to"] and row["valid_from"] > row["valid_to"]:
            raise ValueError(f"{row['id']} valid_from is after valid_to")
        if row["authority"] == "team_explicit" and not row["id"].startswith(AUTHORED_MEMORY_PREFIX):
            raise ValueError(f"{row['id']} generated memory cannot be team_explicit")
    _validate_no_cycles(edges)
    for row in db.execute("select id from spans where generated_by_ai = 1 and trust_level != 'low'").fetchall():
        raise ValueError(f"{row['id']} AI generated span must be low trust")


def _source_ref_exists(ref: str, source_paths: set[str], span_refs: set[str]) -> bool:
    if _external_ref(ref):
        return True
    if _pack_ref_suffix(ref):
        return any(_source_ref_equivalent(ref, candidate) for candidate in source_paths | span_refs)
    return any(_source_ref_exists_exact(candidate, source_paths, span_refs) for candidate in _source_ref_candidates(ref))


def _source_ref_exists_exact(ref: str, source_paths: set[str], span_refs: set[str]) -> bool:
    if "#" in ref:
        return ref in span_refs
    return ref.split("#", 1)[0] in source_paths


def _external_ref(ref: str) -> bool:
    return bool(re.match(r"^[a-z]+:", ref)) and not ref.startswith("pack://")


def _validate_no_cycles(edges: dict[str, str]) -> None:
    for start in edges:
        seen: set[str] = set()
        current = start
        while current in edges:
            if current in seen:
                raise ValueError(f"supersession cycle at {current}")
            seen.add(current)
            current = edges[current]


def _span_packet(row: sqlite3.Row, config: dict | None = None) -> dict:
    scope = _scope_for_source_ref(row["source_ref"], config)
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "source_ref": row["source_ref"],
        "scope": scope,
        "owner_scope": scope,
        "visibility": _default_visibility(scope),
        "source_pack": None,
        "text": row["text"],
        "page": row["page"],
        "source_type": row["source_type"],
        "canonical": bool(row["canonical"]),
        "generated_by_ai": bool(row["generated_by_ai"]),
        "trust_level": row["trust_level"],
        "section": row["section"],
        "speaker": row["speaker"],
        "span_index": row["span_index"],
        "bbox": json.loads(row["bbox"]) if row["bbox"] else None,
    }


def _memory_packet(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    source_refs = json.loads(row["source_refs"])
    scope = _normalize_scope(row["scope"])
    return {
        "id": row["id"],
        "kind": row["kind"],
        "scope": scope,
        "owner_scope": row["owner_scope"] or scope,
        "visibility": row["visibility"] or _default_visibility(scope),
        "share_policy": row["share_policy"] or "local_only",
        "inheritance": row["inheritance"] or ("never_inherit" if scope.startswith("project:") else "explicit_import_only"),
        "applies_to": _json_list(row["applies_to"]),
        "excludes": _json_list(row["excludes"]),
        "content": row["content"],
        "reason": row["reason"],
        "authority": row["authority"],
        "origin": row["origin"],
        "status": row["status"],
        "canonical_key": row["canonical_key"],
        "source_refs": source_refs,
        "related": json.loads(row["related"]),
        "occurred_at": row["occurred_at"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "superseded_by": row["superseded_by"],
        "supporting_spans": _supporting_spans(db, source_refs, row["content"]),
    }


def _supporting_spans(db: sqlite3.Connection, source_refs: list[str], content: str, limit: int = 3) -> list[dict]:
    packets: list[dict] = []
    for ref in source_refs:
        rows = []
        for candidate in _source_ref_candidates(ref):
            rows = db.execute("select * from spans where source_ref = ? order by span_index", (candidate,)).fetchall()
            if rows:
                break
        if not rows and _pack_ref_suffix(ref):
            rows = [
                row
                for row in db.execute("select * from spans order by source_ref, span_index").fetchall()
                if _source_ref_equivalent(ref, row["source_ref"])
            ]
        if rows:
            packets.append(_span_packet(_best_supporting_span(rows, content)))
        if len(packets) >= limit:
            break
    return packets[:limit]


def _best_supporting_span(rows: list[sqlite3.Row], content: str) -> sqlite3.Row:
    target = _compact_text(content)
    for row in rows:
        if _compact_text(row["text"]) == target:
            return row
    for row in rows:
        text = _compact_text(row["text"])
        if target in text or text in target:
            return row
    return rows[0]


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _project_states(repo: Path) -> dict:
    result = {}
    state_paths = [
        repo / TEAM_AI_DIR / "state.yaml",
        repo / TEAM_AI_DIR / "harness_state.yaml",
        *[pack / "state.yaml" for pack in _shared_pack_dirs(repo)],
    ]
    for path in state_paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        project_id = _yamlish_scalar(text, "project_id") or path.stem
        result[project_id] = {
            "source_ref": _display_ref(repo, path),
            "text": _trim_text(text),
            "current_goal": _yamlish_scalar(text, "current_goal"),
            "current_priorities": _yamlish_list(text, "current_priorities"),
            "constraints": _yamlish_list(text, "constraints"),
            "next_actions": _yamlish_list(text, "next_actions"),
            "open_questions": _yamlish_list(text, "open_questions"),
        }
    return result


def _load_project_config(repo: Path, config: dict, project_state: dict) -> dict:
    path = repo / config.get("project_config", f"{TEAM_AI_DIR}/project.yaml")
    fallback_id = "finance-architecture" if "finance-architecture" in project_state else next(iter(project_state), "finance-architecture")
    project = {
        "project_id": fallback_id,
        "scope": f"project:{fallback_id}",
        "imports": [{"scope": "domain:finance-ai"}, {"scope": "domain:agent-infra"}],
        "scope_modes": {},
    }
    if not path.exists():
        return project
    text = path.read_text(encoding="utf-8")
    project["project_id"] = _yamlish_scalar(text, "project_id") or project["project_id"]
    project["scope"] = _yamlish_scalar(text, "scope") or f"project:{project['project_id']}"
    project["imports"] = _project_imports(text)
    project["scope_modes"] = _scope_modes(text)
    return project


def _project_imports(text: str) -> list[dict]:
    imports: list[dict] = []
    for match in re.finditer(r"^  - scope:\s*([A-Za-z0-9_.:-]+)\s*\n((?:    .+\n?)*)", text, re.M):
        item = {"scope": match.group(1)}
        path_match = re.search(r"^    path:\s*(.+)$", match.group(2), re.M)
        if path_match:
            item["path"] = path_match.group(1).strip().strip("\"'")
        imports.append(item)
    return imports


def _shared_pack_dirs(repo: Path) -> list[Path]:
    project = _load_project_config(repo, load_config(repo), {})
    dirs = []
    for item in project.get("imports", []):
        raw = item.get("path")
        if not raw:
            continue
        path = Path(raw)
        dirs.append(path if path.is_absolute() else repo / path)
    return dirs


def _scope_modes(text: str) -> dict[str, dict]:
    modes: dict[str, dict] = {}
    for mode in ("normal", "harness", "global_shared", "compare_projects", "implementation"):
        match = re.search(rf"^  {mode}:\s*\n((?:    .+\n?)+)", text, re.M)
        if not match:
            continue
        block = match.group(1)
        include_match = re.search(r"^    include:\s*\n((?:      - .+\n?)+)", block, re.M)
        include = []
        if include_match:
            include = [line.strip()[2:].strip() for line in include_match.group(1).splitlines()]
        modes[mode] = {"include": include}
    return modes


def _resolve_query_mode(requested_mode: str, question: str) -> str:
    if requested_mode == "compare_projects" and not _compare_intent(question):
        raise ValueError("compare_projects mode requires explicit compare intent")
    return requested_mode


def _resolve_allowed_scopes(project: dict, mode: str, question: str, project_state: dict) -> set[str]:
    if mode == "implementation" and _harness_intent(question):
        return _scope_aliases({"org:jo", "harness:team-ai", "domain:agent-infra"})
    configured = set((project.get("scope_modes", {}).get(mode) or {}).get("include") or [])
    if configured:
        return _scope_aliases(configured)
    imports = {item["scope"] for item in project.get("imports", []) if item.get("scope")}
    if mode == "harness":
        return _scope_aliases({"org:jo", "harness:team-ai", "domain:agent-infra"})
    if mode == "global_shared":
        return _scope_aliases(imports | {"org:jo", "harness:team-ai", "domain:agent-infra", "domain:finance-ai"})
    if mode == "compare_projects":
        return _scope_aliases(imports | {f"project:{project_id}" for project_id in project_state})
    if _finance_intent(question):
        return _scope_aliases({project["scope"], *imports})
    fallback = _target_scopes(question, project_state)
    if mode in {"normal", "implementation"}:
        fallback.add(project["scope"])
    return _scope_aliases(fallback | imports or {project["scope"]})


def _scope_aliases(scopes: set[str]) -> set[str]:
    result = set(scopes)
    if "org:jo" in result:
        result.add("team")
    if "team" in result:
        result.add("org:jo")
    if "harness:team-ai" in result:
        result.add("project:team-ai-harness")
    if "project:team-ai-harness" in result:
        result.add("harness:team-ai")
    return result


def _scope_project_states(project_state: dict, scopes: set[str]) -> dict:
    state_ids = {
        scope.split(":", 1)[1]
        for scope in scopes
        if scope.startswith(("project:", "program:", "harness:", "workspace:"))
    }
    state_ids.update(
        f"{scope.split(':', 1)[1]}-workspace"
        for scope in scopes
        if scope.startswith("workspace:")
    )
    if not state_ids:
        return project_state
    return {project_id: state for project_id, state in project_state.items() if project_id in state_ids}


def _target_scopes(question: str, project_state: dict) -> set[str]:
    scopes = {"team"}
    lower = question.lower()
    if _harness_intent(question):
        scopes.add("project:team-ai-harness")
    if _finance_intent(question):
        scopes.add("project:finance-architecture")
    if len(scopes) == 1:
        scopes.update(f"project:{project_id}" for project_id in project_state)
    return scopes


def _harness_intent(question: str) -> bool:
    return bool(
        re.search(
            r"\bteam-ai\b|하네스|harness|"
            r"\bguard\b|\bverify\b|\brefresh\b|"
            r"runtime|compiled|snapshot|"
            r"memory schema|메모리 스키마|"
            r"context command|context 명령|"
            r"scope rule|scope inference|스코프 규칙|"
            r"project\.yaml|AGENTS\.md|CLI|명령어",
            question,
            re.I,
        )
    )


def _finance_intent(question: str) -> bool:
    return bool(re.search(r"금융|finance|portfolio|투자|m&a|pmi|fraud|scam|프로젝트 목적|다음 행동|아이디어", question, re.I))


def _compare_intent(question: str) -> bool:
    return bool(re.search(r"비교|차이|compare|versus| vs |프로젝트 .+ 프로젝트", question, re.I))


def _classify_query_mode(question: str) -> str:
    if _implementation_intent(question):
        return "implementation"
    if _harness_intent(question):
        return "harness"
    if _compare_intent(question):
        return "compare_projects"
    return "normal"


def _decision_records(repo: Path, active_only: bool, scopes: set[str] | None = None) -> list[dict]:
    decisions = []
    for base in [repo / TEAM_AI_DIR / "decisions", *[pack / "decisions" for pack in _shared_pack_dirs(repo)]]:
        if not base.exists():
            continue
        for path in sorted(base.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            front = _front_matter(text)
            decision_scopes = front.get("scope", [])
            if isinstance(decision_scopes, str):
                decision_scopes = [decision_scopes]
            if scopes and not (set(decision_scopes) & scopes or "team" in scopes and not decision_scopes):
                continue
            if active_only and not _approved_active_decision(front):
                continue
            decisions.append(
                {
                    "id": front.get("id", path.stem),
                    "title": front.get("title"),
                    "status": front.get("status"),
                    "authority": front.get("authority"),
                    "decided_at": front.get("decided_at"),
                    "approved_by": front.get("authority"),
                    "approved_at": front.get("decided_at"),
                    "scope": decision_scopes,
                    "source_refs": front.get("source_refs", []),
                    "source_ref": _display_ref(repo, path),
                    "text": _trim_text(text),
                }
            )
    return sorted(decisions, key=lambda item: (item.get("decided_at") or "", item["id"]))


def _decision_buckets(decisions: list[dict]) -> dict[str, list[dict]]:
    buckets = {"project": [], "program": [], "domain": [], "org": [], "harness": []}
    for decision in decisions:
        scopes = decision.get("scope") or ["team"]
        key = "org"
        for scope in scopes:
            if scope.startswith("project:"):
                key = "project"
                break
            if scope.startswith("program:"):
                key = "program"
            elif scope.startswith("domain:") and key == "org":
                key = "domain"
            elif scope.startswith("harness:"):
                key = "harness"
                break
        buckets.setdefault(key, []).append(decision)
    return buckets


def _display_ref(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _approved_active_decision(front: dict) -> bool:
    return front.get("status") == "active" and front.get("authority") in {"user-approved", "user-corrected", "team-approved"}


def _trim_text(text: str, limit: int = CONTEXT_TEXT_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _front_matter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    return _parse_yamlish(text[4:end])


def _parse_yamlish(text: str) -> dict:
    result: dict[str, str | list[str]] = {}
    current_key: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        list_match = re.match(r"\s*-\s+(.+)", line)
        if list_match and current_key:
            result.setdefault(current_key, [])
            if isinstance(result[current_key], list):
                result[current_key].append(list_match.group(1).strip())
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)", line)
        if match:
            current_key = match.group(1)
            value = match.group(2).strip()
            result[current_key] = [] if value == "" else value
    return result


def _yamlish_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(?:>\s*)?\n((?:  .+\n?)+)", text, re.M)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.M)
    return match.group(1).strip() if match else None


def _yamlish_list(text: str, key: str) -> list[str]:
    match = re.search(rf"^{re.escape(key)}:\s*\n((?:  - .+\n?)+)", text, re.M)
    if not match:
        return []
    return [line.strip()[2:].strip() for line in match.group(1).splitlines() if line.strip().startswith("- ")]


def _open_questions(project_state: dict, memories: list[dict]) -> list:
    questions = [memory for memory in memories if memory["kind"] == "open_question" and memory["status"] == "active"]
    for state in project_state.values():
        questions.extend(state.get("open_questions", []))
    return questions


def _tasks(project_state: dict) -> list[dict]:
    tasks = []
    for project_id, state in project_state.items():
        for action in state.get("next_actions", []):
            tasks.append({"project": project_id, "title": action, "status": "open", "source_ref": state["source_ref"]})
    return tasks


def _memory_buckets(memories: list[dict]) -> dict[str, list[dict]]:
    buckets = {
        "authored_memories": [],
        "project_memories": [],
        "shared_memories": [],
        "retrieval_hints": [],
        "low_trust_hints": [],
    }
    for memory in memories:
        if memory["authority"] == "ai_inferred" or _output_derived(memory):
            buckets["low_trust_hints"].append(memory)
            continue
        if memory["id"].startswith(AUTHORED_MEMORY_PREFIX):
            buckets["authored_memories"].append(memory)
        else:
            buckets["retrieval_hints"].append(memory)
        if memory["scope"].startswith("project:"):
            buckets["project_memories"].append(memory)
        elif memory["scope"].startswith(("org:", "harness:", "domain:", "program:")) or memory["scope"] == "team":
            buckets["shared_memories"].append(memory)
    return buckets


def _apply_local_overrides(memories: list[dict], project_scope: str) -> tuple[list[dict], list[dict]]:
    overrides: list[dict] = []
    overridden: set[str] = set()
    for memory in memories:
        if memory["scope"] != project_scope:
            continue
        for rel in memory.get("related", []):
            target = rel.get("overrides") if isinstance(rel, dict) else None
            if target:
                overridden.add(target)
                overrides.append({"override": memory["id"], "overrides": target})
    if not overridden:
        return memories, overrides
    return [memory for memory in memories if memory["id"] not in overridden], overrides


def _expand_related_memories(
    db: sqlite3.Connection,
    memories: list[dict],
    scopes: set[str],
    limit: int = 5,
) -> list[dict]:
    seen = {memory["id"] for memory in memories}
    related_ids: list[str] = []
    for memory in memories:
        for rel in memory.get("related", []):
            candidate = _related_id(rel)
            if candidate and candidate not in seen and candidate not in related_ids:
                related_ids.append(candidate)
    packets: list[dict] = []
    for memory_id in related_ids:
        row = db.execute("select * from memories where id = ? and status = 'active'", (memory_id,)).fetchone()
        if not row or not _scope_allowed(_normalize_scope(row["scope"]), scopes):
            continue
        packets.append(_memory_packet(db, row))
        if len(packets) >= limit:
            break
    return packets


def _related_id(rel) -> str | None:
    if isinstance(rel, str) and rel.startswith("MEM-"):
        return rel
    if isinstance(rel, dict):
        for key in ("id", "supports", "opposes", "conflicts_with", "supersedes", "overrides"):
            value = rel.get(key)
            if isinstance(value, str) and value.startswith("MEM-"):
                return value
    return None


def _build_counterevidence(
    repo: Path,
    decisions: list[dict],
    memories: list[dict],
    spans: list[dict],
    scopes: set[str],
    limit: int = 5,
) -> list[dict]:
    counter: list[dict] = []
    for decision in decisions:
        text = _decision_countercase(repo / decision["source_ref"])
        if text:
            counter.append(
                {
                    "source": decision["source_ref"],
                    "kind": "strongest_countercase",
                    "text": text,
                    "scope": (decision.get("scope") or ["team"])[0],
                }
            )
    for memory in memories:
        if len(counter) >= limit:
            break
        if memory["status"] == "superseded" or COUNTEREVIDENCE_RE.search(memory["content"]):
            counter.append(
                {
                    "source": memory["id"],
                    "kind": "memory_counterevidence",
                    "text": memory["content"],
                    "scope": memory["scope"],
                }
            )
    for span in spans:
        if len(counter) >= limit:
            break
        if _scope_allowed(span["scope"], scopes) and COUNTEREVIDENCE_RE.search(span["text"]):
            counter.append(
                {
                    "source": span["source_ref"],
                    "kind": "source_counterevidence",
                    "text": _trim_text(span["text"], 600),
                    "scope": span["scope"],
                }
            )
    return counter[:limit]


def _decision_countercase(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^## Strongest Countercase\s*\n(.+?)(?=\n## |\Z)", text, re.M | re.S)
    return _trim_text(match.group(1), 600) if match else None


def _filter_span_scope(
    rows: list[sqlite3.Row],
    scopes: set[str],
    config: dict,
    excluded_matches: list[dict],
) -> list[sqlite3.Row]:
    kept = []
    for row in rows:
        scope = _scope_for_source_ref(row["source_ref"], config)
        if _scope_allowed(scope, scopes):
            kept.append(row)
            continue
        excluded_matches.append(
            {
                "id": row["id"],
                "scope": scope,
                "kind": "span",
                "source_ref": row["source_ref"],
                "reason": "scope_not_allowed",
            }
        )
    return kept


def _filter_memory_scope(
    rows: list[sqlite3.Row],
    scopes: set[str],
    excluded_matches: list[dict] | None = None,
) -> list[sqlite3.Row]:
    kept = []
    for row in rows:
        scope = _normalize_scope(row["scope"])
        if _scope_allowed(scope, scopes):
            kept.append(row)
            continue
        if excluded_matches is not None:
            excluded_matches.append(
                {
                    "id": row["id"],
                    "scope": scope,
                    "kind": "memory",
                    "reason": "scope_not_allowed",
                }
            )
    return kept


def _scope_allowed(scope: str, scopes: set[str]) -> bool:
    return scope in _scope_aliases(set(scopes))


def _scope_for_source_ref(source_ref: str, config: dict | None = None) -> str:
    return _scope_for_source_path(source_ref.split("#", 1)[0], config)


def _scope_for_source_path(rel: str, config: dict | None = None) -> str:
    rel = _nfc(rel)
    shared_scope = _shared_pack_scope(rel)
    if shared_scope:
        return shared_scope
    for rule in (config or {}).get("scope_inference", []):
        pattern = _nfc(str(rule.get("path", "")))
        scope = rule.get("scope")
        if pattern and scope and fnmatch(rel, pattern):
            return _normalize_scope(scope)
    if rel == ".team-ai/project.yaml" or rel == ".team-ai/state.yaml":
        return "project:finance-architecture"
    if rel == ".team-ai/harness_state.yaml" or rel.startswith(".team-ai/decisions/"):
        return "harness:team-ai"
    if _meeting_source(rel) or rel.startswith(".team-ai/sources/notes/") or rel.startswith(".team-ai/sources/uploads/") or _output_source(rel):
        return "project:finance-architecture"
    if _reference_source(rel):
        if re.search(r"Agent|Context|SDLC|Prototype|MCP|Skills|Introduction to Agents", rel, re.I):
            return "domain:agent-infra"
        return "domain:finance-ai"
    if rel.startswith("tools/team_ai/"):
        return "harness:team-ai"
    if rel.startswith("src/finance_architecture/"):
        return "project:finance-architecture"
    return "project:finance-architecture"


def _shared_pack_scope(rel: str) -> str | None:
    match = re.search(r"(?:^|/|\.\./)(?:[^/]+/)?shared/packs/([^/]+)/", rel)
    if not match:
        return None
    return {
        "org-jo": "org:jo",
        "harness-team-ai": "harness:team-ai",
        "domain-finance-ai": "domain:finance-ai",
        "domain-agent-infra": "domain:agent-infra",
        "finance-ai-ideation": "program:finance-ai-ideation",
    }.get(match.group(1))


def _terms(question: str) -> list[str]:
    raw_terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9&+.-]*|[가-힣A-Za-z0-9_]+", question)
    terms: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        if len(term) <= 1:
            continue
        key = term.casefold()
        if key in seen:
            continue
        terms.append(term)
        seen.add(key)
        if len(terms) >= MAX_SEARCH_TERMS:
            break
    return terms


def _meeting_source(rel: str) -> bool:
    return rel.startswith("회의록/") or rel.startswith(".team-ai/sources/meetings/") or "/sources/meetings/" in rel


def _reference_source(rel: str) -> bool:
    return rel.startswith("자료/") or rel.startswith(".team-ai/sources/references/") or "/sources/references/" in rel


def _output_source(rel: str) -> bool:
    return rel.startswith("output/")


def _source_ref_candidates(ref: str) -> list[str]:
    # ponytail: keep old refs readable until curated memory is approved for rewrite.
    candidates = [ref]
    suffix = _pack_ref_suffix(ref)
    if suffix:
        candidates.extend(
            (
                suffix,
                f"../team-ai/{suffix}",
                f"../ai-coworker/{suffix}",
            )
        )
    aliases = (
        ("회의록/", ".team-ai/sources/meetings/"),
        ("회의록/", "../team-ai/shared/packs/finance-ai-ideation/sources/meetings/"),
        ("회의록/", "../team-ai-shared/packs/finance-ai-ideation/sources/meetings/"),
        ("자료/", ".team-ai/sources/references/"),
        ("자료/", "../team-ai/shared/packs/finance-ai-ideation/sources/references/"),
        ("자료/", "../team-ai-shared/packs/finance-ai-ideation/sources/references/"),
    )
    for old, new in aliases:
        if ref.startswith(old):
            candidates.append(new + ref[len(old) :])
        elif ref.startswith(new):
            candidates.append(old + ref[len(new) :])
    return candidates


def _pack_ref_suffix(ref: str) -> str | None:
    match = re.match(r"pack://([^/]+)/(.+)", ref)
    if not match:
        return None
    return f"shared/packs/{match.group(1)}/{match.group(2)}"


def _source_ref_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    suffix = _pack_ref_suffix(left) or _pack_ref_suffix(right)
    if not suffix:
        return False
    other = right if _pack_ref_suffix(left) else left
    return other == suffix or other.endswith(f"/{suffix}")


def _query_intent(question: str) -> str:
    if re.search(r"예전|과거|이전|전에는|변경|바뀌|왜 .*됐|왜.*바뀌|히스토리|history", question, re.I):
        return "history"
    if re.search(r"회의|지난번|뭔 얘기|뭐 나왔|브레인스토밍|meeting|minutes", question, re.I):
        return "meeting_recall"
    if re.search(r"원문|근거|자료|파일|source|evidence", question, re.I):
        return "evidence"
    return "current"


def _implementation_intent(question: str) -> bool:
    return bool(re.search(r"구현|수정|코드|테스트|버그|MVP|API|implement|code|test|bug|fix|src/", question, re.I))


def _search_rows(
    db: sqlite3.Connection,
    table: str,
    column: str,
    terms: list[str],
    limit: int,
    question: str = "",
    intent: str = "current",
):
    if table == "spans":
        return _search_spans(db, terms, limit, question, intent)
    if table == "memories":
        return _search_memories(db, terms, limit, question, intent)
    order = _search_order(table)
    if not terms:
        return db.execute(f"select * from {table}{order} limit ?", (limit,)).fetchall()
    where = " or ".join([f"{column} like ?" for _ in terms])
    params = [f"%{term}%" for term in terms]
    rows = db.execute(f"select * from {table} where {where}{order} limit ?", (*params, limit)).fetchall()
    if rows:
        return rows
    return db.execute(f"select * from {table}{order} limit ?", (limit,)).fetchall()


def _search_spans(db: sqlite3.Connection, terms: list[str], limit: int, question: str, intent: str):
    if intent == "meeting_recall":
        return _search_meeting_spans(db, terms, limit, question)
    if not terms:
        rows = db.execute("select * from spans limit ?", (limit * 2,)).fetchall()
        return _filter_span_rows(rows, intent)[:limit]
    ids = _fuse_ranked_ids(
        _fts_ids(db, "spans_fts", terms),
        _fts_ids(db, "spans_trigram", terms),
        _like_ids(db, "spans", "text", terms, question, extra_columns=("source_ref",)),
    )
    rank_by_id = {row_id: rank for rank, row_id in enumerate(ids)}
    rows = _filter_span_rows(_rows_for_ids(db, "spans", ids, len(ids)), intent)
    rows.sort(key=lambda row: (*_span_policy_key(row), rank_by_id.get(row["id"], len(ids)), row["id"]))
    return rows[:limit]


def _filter_span_rows(rows: list[sqlite3.Row], intent: str) -> list[sqlite3.Row]:
    if intent != "history":
        return [row for row in rows if not row["generated_by_ai"]]
    return rows


def _span_policy_key(row: sqlite3.Row) -> tuple:
    return (
        1 if row["generated_by_ai"] else 0,
        0 if row["canonical"] else 1,
        {"high": 0, "medium": 1, "low": 2}.get(row["trust_level"], 3),
    )


def _search_memories(db: sqlite3.Connection, terms: list[str], limit: int, question: str, intent: str):
    if intent == "meeting_recall":
        return _search_meeting_memories(db, terms, limit, question)
    status_clause = "" if intent == "history" else " where status = 'active'"
    if not terms:
        return db.execute(f"select * from memories{status_clause}{_search_order('memories')} limit ?", (limit,)).fetchall()
    ids = _fuse_ranked_ids(
        _fts_ids(db, "memories_fts", terms),
        _fts_ids(db, "memories_trigram", terms),
        _like_ids(db, "memories", "content", terms, question, extra_columns=("canonical_key",)),
    )
    rank_by_id = {row_id: rank for rank, row_id in enumerate(ids)}
    rows = _rows_for_ids(db, "memories", ids, len(ids))
    if intent != "history":
        rows = [row for row in rows if row["status"] == "active"]
    rows.sort(key=lambda row: (*_memory_policy_key(row), rank_by_id.get(row["id"], len(ids))))
    return rows[:limit]


def _search_meeting_spans(db: sqlite3.Connection, terms: list[str], limit: int, question: str):
    rows = db.execute("select * from spans where source_type = 'meeting_note' order by source_ref desc, span_index").fetchall()
    if terms:
        scored = [(row, _text_score(row, "text", terms, question, ("source_ref",))) for row in rows]
        rows = [row for row, score in sorted(scored, key=lambda item: item[1], reverse=True) if score > 0] or rows
    rows = _representative_sources_first(rows)
    return _filter_span_rows(rows, "current")[:limit]


def _search_meeting_memories(db: sqlite3.Connection, terms: list[str], limit: int, question: str):
    rows = db.execute(
        """
        select * from memories
        where status = 'active'
          and (
            source_refs like '%/sources/meetings/%'
            or source_refs like '%회의록/%'
            or kind in ('discussion', 'principle', 'open_question', 'decision')
          )
        """
    ).fetchall()
    if terms:
        scored = [(row, _text_score(row, "content", terms, question, ("canonical_key",))) for row in rows]
        rows = [row for row, score in sorted(scored, key=lambda item: item[1], reverse=True) if score > 0] or rows
    rows.sort(key=_memory_policy_key)
    return rows[:limit]


def _representative_sources_first(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    seen: set[str] = set()
    first: list[sqlite3.Row] = []
    rest: list[sqlite3.Row] = []
    for row in rows:
        key = row["source_id"]
        if key in seen:
            rest.append(row)
            continue
        seen.add(key)
        first.append(row)
    return first + rest


def _fts_ids(db: sqlite3.Connection, table: str, terms: list[str]) -> list[str]:
    if not _has_search_indexes(db):
        return []
    query = " OR ".join(_quote_fts(term) for term in terms)
    try:
        rows = db.execute(f"select id from {table} where {table} match ? order by rank", (query,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [row["id"] for row in rows]


def _quote_fts(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _like_ids(
    db: sqlite3.Connection,
    table: str,
    column: str,
    terms: list[str],
    question: str,
    extra_columns: tuple[str, ...] = (),
) -> list[str]:
    columns = (column, *extra_columns)
    where = " or ".join(f"coalesce({col}, '') like ?" for col in columns for _ in terms)
    params = [f"%{term}%" for _ in columns for term in terms]
    rows = db.execute(f"select * from {table} where {where}", params).fetchall()
    rows = sorted(rows, key=lambda row: _text_score(row, column, terms, question, extra_columns), reverse=True)
    return [row["id"] for row in rows]


def _text_score(row: sqlite3.Row, column: str, terms: list[str], question: str, extra_columns: tuple[str, ...]) -> int:
    haystack = " ".join(str(row[col] or "") for col in (column, *extra_columns)).lower()
    question_lower = question.lower()
    score = 1000 if question_lower and question_lower in haystack else 0
    score += sum(len(term) * 10 for term in terms if term.lower() in haystack)
    score += sum(1 for term in terms if term.lower() in haystack)
    return score


def _fuse_ranked_ids(*ranked_lists: list[str]) -> list[str]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, row_id in enumerate(ranked, 1):
            scores[row_id] = scores.get(row_id, 0.0) + 1.0 / (RRF_K + rank)
    return [row_id for row_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def _rows_for_ids(db: sqlite3.Connection, table: str, ids: list[str], limit: int):
    if not ids:
        return []
    selected = ids[: max(limit, 1)]
    placeholders = ", ".join("?" for _ in selected)
    rows = db.execute(f"select * from {table} where id in ({placeholders})", selected).fetchall()
    by_id = {row["id"]: row for row in rows}
    return [by_id[row_id] for row_id in selected if row_id in by_id]


def _memory_policy_key(row: sqlite3.Row) -> tuple:
    scope = _normalize_scope(row["scope"])
    return (
        0 if row["status"] == "active" else 1,
        0 if row["id"].startswith(AUTHORED_MEMORY_PREFIX) else 1,
        _authority_rank(row["authority"]),
        0 if scope == "team" or scope.startswith(("project:", "org:", "harness:", "domain:")) else 1,
        row["id"],
    )


def _authority_rank(authority: str) -> int:
    return {
        "user_explicit": 0,
        "team_explicit": 1,
        "project_observation": 2,
        "ai_inferred": 3,
        "individual_opinion": 4,
    }.get(authority, 5)


def _search_order(table: str) -> str:
    if table != "memories":
        return ""
    return """
        order by
            case when id like 'MEM-AUTH-%' then 0 else 1 end,
            case authority
                when 'user_explicit' then 0
                when 'team_explicit' then 1
                when 'project_observation' then 2
                when 'ai_inferred' then 3
                when 'individual_opinion' then 4
                else 5
            end,
            case
                when scope = 'team' then 0
                when scope like 'project:%' then 0
                when scope like 'org:%' then 0
                when scope like 'harness:%' then 0
                when scope like 'domain:%' then 0
                else 1
            end,
            id
    """
