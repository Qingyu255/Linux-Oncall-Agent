"""One trusted local store. Atomic artifacts precede transactional evidence admission."""

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from oncall.domain import Evidence, Hypothesis, Observation, Report, utcnow


class EvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts = root / "artifacts"
        self.artifacts.mkdir(exist_ok=True, mode=0o700)
        # FastAPI runs synchronous MCP tools in worker threads. Serialize one
        # connection explicitly rather than allowing concurrent cursor use.
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "state.sqlite3", check_same_thread=False, timeout=5)
        with self.lock:
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
              CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, status TEXT NOT NULL,
                started TEXT NOT NULL, mode TEXT NOT NULL, report TEXT);
              CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, run_id TEXT NOT NULL
                REFERENCES runs(id), artifact_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL
                REFERENCES runs(id), time TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS hypotheses(run_id TEXT NOT NULL REFERENCES runs(id),
                id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(run_id,id,version));
            """)
            # Recovery never silently resumes stale authority.
            self.db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
            self.db.commit()

    def create_run(self, mode: str) -> str:
        run = uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO runs VALUES (?, 'running', ?, ?, NULL)",
                (run, utcnow().isoformat(), mode),
            )
        return run

    def event(self, run: str, kind: str, payload: dict[str, Any]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO events(run_id,time,kind,payload) VALUES (?,?,?,?)",
                (run, utcnow().isoformat(), kind, json.dumps(payload)),
            )

    def add(self, run: str, observation: Observation) -> Evidence:
        data = observation.raw.encode()
        if len(data) > 1024 * 1024:
            raise ValueError("Artifact exceeds 1 MiB")
        artifact = uuid.uuid4().hex
        digest = hashlib.sha256(data).hexdigest()
        temporary = self.artifacts / f"{artifact}.tmp"
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.artifacts / artifact)
        evidence = Evidence(
            evidence_id=uuid.uuid4().hex,
            investigation_id=run,
            artifact_id=artifact,
            artifact_sha256=digest,
            artifact_bytes=len(data),
            artifact_truncated=observation.raw_truncated,
            **observation.model_dump(exclude={"raw", "raw_bytes", "raw_truncated"}),
        )
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO evidence VALUES (?,?,?,?)",
                (evidence.evidence_id, run, artifact, evidence.model_dump_json()),
            )
        return evidence

    def evidence(self, run: str) -> list[Evidence]:
        with self.lock:
            rows = self.db.execute(
                "SELECT payload FROM evidence WHERE run_id=? ORDER BY rowid", (run,)
            ).fetchall()
        return [Evidence.model_validate_json(row[0]) for row in rows]

    def events(self, run: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT seq,time,kind,payload FROM events WHERE run_id=? ORDER BY seq", (run,)
            ).fetchall()
        return [
            {"sequence": row[0], "time": row[1], "kind": row[2], "payload": json.loads(row[3])}
            for row in rows
        ]

    def artifact_page(self, run: str, artifact: str, offset: int, limit: int) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute(
                "SELECT payload FROM evidence WHERE run_id=? AND artifact_id=?", (run, artifact)
            ).fetchone()
        if row is None:
            raise ValueError("Artifact does not belong to investigation")
        evidence = Evidence.model_validate_json(row[0])
        data = (self.artifacts / evidence.artifact_id).read_bytes()
        if hashlib.sha256(data).hexdigest() != evidence.artifact_sha256:
            raise ValueError("Artifact integrity check failed")
        if offset < 0 or offset > len(data) or not 1 <= limit <= 16384:
            raise ValueError("Invalid artifact page bounds")
        end = min(offset + limit, len(data))
        while True:
            page = data[offset:end]
            try:
                text = page.decode("utf-8")
                break
            except UnicodeDecodeError as error:
                can_extend = end < len(data) and end < offset + limit + 4
                if error.reason == "unexpected end of data" and can_extend:
                    end += 1
                    continue
                if error.reason == "unexpected end of data" and error.start > 0:
                    page = page[: error.start]
                    text = page.decode("utf-8")
                    break
                raise ValueError("Artifact offset is not a UTF-8 boundary") from error
        next_offset = offset + len(page)
        return {
            "artifact_id": artifact,
            "sha256": evidence.artifact_sha256,
            "offset": offset,
            "next_offset": next_offset,
            "eof": next_offset == len(data),
            "total_bytes": len(data),
            "line_start": data[:offset].count(b"\n") + 1,
            "line_end": data[:next_offset].count(b"\n") + 1,
            "text": text,
        }

    def artifact(self, run: str, artifact: str, offset: int, limit: int) -> str:
        """Compatibility helper for trusted callers that only need page text."""
        return str(self.artifact_page(run, artifact, offset, limit)["text"])

    def update_hypothesis(self, run: str, hypothesis: Hypothesis) -> Hypothesis:
        with self.lock:
            row = self.db.execute(
                "SELECT MAX(version) FROM hypotheses WHERE run_id=? AND id=?",
                (run, hypothesis.hypothesis_id),
            ).fetchone()
            version = (row[0] or 0) + 1
            stored = hypothesis.model_copy(update={"version": version})
            with self.db:
                self.db.execute(
                    "INSERT INTO hypotheses VALUES (?,?,?,?)",
                    (run, stored.hypothesis_id, version, stored.model_dump_json()),
                )
        return stored

    def hypotheses(self, run: str) -> list[Hypothesis]:
        with self.lock:
            rows = self.db.execute(
                """
                SELECT h.payload FROM hypotheses h
                JOIN (SELECT id, MAX(version) version FROM hypotheses
                      WHERE run_id=? GROUP BY id) latest
                  ON h.id=latest.id AND h.version=latest.version
                WHERE h.run_id=? ORDER BY h.id
                """,
                (run, run),
            ).fetchall()
        return [Hypothesis.model_validate_json(row[0]) for row in rows]

    def finish(self, run: str, status: str, report: Report | None = None) -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE runs SET status=?,report=? WHERE id=?",
                (status, report.model_dump_json() if report else None, run),
            )

    def state(self, run: str) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute(
                "SELECT status,mode,report FROM runs WHERE id=?", (run,)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown investigation")
        return {
            "investigation_id": run,
            "status": row[0],
            "mode": row[1],
            "report": json.loads(row[2]) if row[2] else None,
            "evidence": [x.model_dump(mode="json") for x in self.evidence(run)],
            "hypotheses": [x.model_dump(mode="json") for x in self.hypotheses(run)],
        }

    def close(self) -> None:
        with self.lock:
            self.db.close()
