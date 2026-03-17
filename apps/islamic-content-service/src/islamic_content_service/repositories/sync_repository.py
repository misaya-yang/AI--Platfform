from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..db import Database


class SyncRepository:
    def __init__(self, db: Database):
        self.db = db

    async def start_sync_run(self, source_name: str, scope: dict[str, Any]) -> str:
        run_id = uuid.uuid4().hex
        await self.db.execute(
            """
            UPDATE source_sync_runs
            SET status = 'abandoned',
                error_summary = COALESCE(error_summary, 'Superseded by a newer sync run'),
                completed_at = NOW()
            WHERE source_name = $1
              AND status = 'running'
            """,
            source_name,
        )
        await self.db.execute(
            """
            INSERT INTO source_sync_runs (run_id, source_name, scope, status, started_at)
            VALUES ($1, $2, $3::jsonb, 'running', NOW())
            """,
            run_id,
            source_name,
            json.dumps(scope),
        )
        return run_id

    async def finish_sync_run(
        self,
        run_id: str,
        *,
        status: str,
        metrics: dict[str, Any],
        error_summary: str | None = None,
    ) -> None:
        await self.db.execute(
            """
            UPDATE source_sync_runs
            SET status = $2,
                metrics_json = $3::jsonb,
                error_summary = $4,
                completed_at = NOW()
            WHERE run_id = $1
            """,
            run_id,
            status,
            json.dumps(metrics),
            error_summary,
        )

    async def save_snapshot(
        self,
        *,
        source_name: str,
        snapshot_kind: str,
        snapshot_key: str,
        request_path: str,
        request_params: dict[str, Any] | None,
        response_payload: dict[str, Any],
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO source_snapshots (
                source_name, snapshot_kind, snapshot_key, request_path,
                request_params_json, response_json, fetched_at
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, NOW())
            ON CONFLICT (source_name, snapshot_kind, snapshot_key)
            DO UPDATE SET
                request_path = EXCLUDED.request_path,
                request_params_json = EXCLUDED.request_params_json,
                response_json = EXCLUDED.response_json,
                fetched_at = NOW()
            """,
            source_name,
            snapshot_kind,
            snapshot_key,
            request_path,
            json.dumps(request_params or {}),
            json.dumps(response_payload),
        )

    async def get_counts(self) -> dict[str, int]:
        queries = {
            "quran_chapters": "SELECT COUNT(*) FROM quran_chapters",
            "quran_ayahs": "SELECT COUNT(*) FROM quran_ayahs",
            "quran_ayah_translations": "SELECT COUNT(*) FROM quran_ayah_translations",
            "quran_ayah_audio": "SELECT COUNT(*) FROM quran_ayah_audio",
            "quran_words": "SELECT COUNT(*) FROM quran_words",
            "quran_chapter_audio_tracks": "SELECT COUNT(*) FROM quran_chapter_audio_tracks",
            "quran_audio_timings": "SELECT COUNT(*) FROM quran_chapter_audio_timings",
            "quran_triplet_ranges": "SELECT COUNT(*) FROM quran_triplet_ranges",
            "hadith_collections": "SELECT COUNT(*) FROM hadith_collections",
            "hadith_books": "SELECT COUNT(*) FROM hadith_books",
            "hadith_items": "SELECT COUNT(*) FROM hadith_items",
            "dua_categories": "SELECT COUNT(*) FROM dua_categories",
            "dua_items": "SELECT COUNT(*) FROM dua_items",
            "source_sync_runs": "SELECT COUNT(*) FROM source_sync_runs",
        }
        counts: dict[str, int] = {}
        for name, query in queries.items():
            value = await self.db.fetchval(query)
            counts[name] = int(value or 0)
        return counts

    async def get_latest_runs(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT DISTINCT ON (source_name)
                source_name, status, metrics_json, error_summary, started_at, completed_at
            FROM source_sync_runs
            ORDER BY source_name, started_at DESC
            """
        )
        return [
            {
                "source_name": row["source_name"],
                "status": row["status"],
                "metrics": row["metrics_json"] or {},
                "error_summary": row["error_summary"],
                "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
            }
            for row in rows
        ]

    async def get_latest_completed_at(self, source_name: str) -> str | None:
        value = await self.db.fetchval(
            """
            SELECT completed_at
            FROM source_sync_runs
            WHERE source_name = $1 AND status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
            """,
            source_name,
        )
        return value.isoformat() if value else None

    async def build_manifest(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": await self.get_counts(),
            "latest_runs": await self.get_latest_runs(),
        }
