"""Thread-safe, serializable attempt ledger for one dossier run.

The ledger records what the engine actually attempted. It is evidence about
the scan, not evidence about the subject. No secrets, response bodies, cookies,
or authorization headers are stored.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


class Attempt:
    def __init__(
        self,
        ledger: "AttemptLedger",
        stage: str,
        connector: str,
        *,
        claim_index: Optional[int] = None,
        query: str = "",
        target: str = "",
        metadata: Optional[dict] = None,
    ) -> None:
        self.ledger = ledger
        self.stage = stage
        self.connector = connector
        self.claim_index = claim_index
        self.query = query
        self.target = target
        self.metadata = dict(metadata or {})
        self.started = time.monotonic()
        self.started_at = _now_iso()
        self.finished = False

    def finish(
        self,
        status: str,
        *,
        result_count: int = 0,
        final_url: str = "",
        error: str = "",
        metadata: Optional[dict] = None,
    ) -> None:
        if self.finished:
            return
        self.finished = True
        merged = dict(self.metadata)
        merged.update(dict(metadata or {}))
        self.ledger.record(
            {
                "stage": _safe_text(self.stage, 80),
                "connector": _safe_text(self.connector, 80),
                "claim_index": self.claim_index,
                "query": _safe_text(self.query),
                "target": _safe_text(self.target),
                "status": _safe_text(status, 60),
                "result_count": max(0, int(result_count or 0)),
                "final_url": _safe_text(final_url),
                "error": _safe_text(error, 300),
                "started_at": self.started_at,
                "elapsed_ms": max(
                    0, round((time.monotonic() - self.started) * 1000)
                ),
                "metadata": merged,
            }
        )

    def __enter__(self) -> "Attempt":
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if exc is not None:
            self.finish("error", error=f"{type(exc).__name__}: {exc}")
        elif not self.finished:
            self.finish("completed")
        return False


class AttemptLedger:
    def __init__(self, job_id: str = "") -> None:
        self.job_id = _safe_text(job_id, 100)
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._sequence = 0

    def attempt(
        self,
        stage: str,
        connector: str,
        *,
        claim_index: Optional[int] = None,
        query: str = "",
        target: str = "",
        metadata: Optional[dict] = None,
    ) -> Attempt:
        return Attempt(
            self,
            stage,
            connector,
            claim_index=claim_index,
            query=query,
            target=target,
            metadata=metadata,
        )

    def record(self, record: dict) -> None:
        item = dict(record or {})
        with self._lock:
            self._sequence += 1
            item["sequence"] = self._sequence
            item["job_id"] = self.job_id
            self._records.append(item)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._records]


def ledger_for(claim: object) -> Optional[AttemptLedger]:
    ledger = getattr(claim, "_attempt_ledger", None)
    return ledger if isinstance(ledger, AttemptLedger) else None
