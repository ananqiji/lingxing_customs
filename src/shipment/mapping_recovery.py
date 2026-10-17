"""Durable mapping failures, independent of the disposable API cache."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from src.shipment.models import RawCustomsData
from src.shipment.retention import shipment_retention_cutoff
from src.shipment.shop_mapping import missing_mapping_value


class MappingRecoveryStore:
    def __init__(self, path: str | Path = "state/shipment_mapping.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pending (
                source TEXT NOT NULL, shipment_no TEXT NOT NULL,
                shipment_day TEXT NOT NULL, shops TEXT NOT NULL,
                reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source, shipment_no)
            )""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def pending(self, today: date, sources: list[str]) -> list[dict]:
        cutoff = shipment_retention_cutoff(today).isoformat()
        with self._connect() as db:
            db.execute("UPDATE pending SET status='expired' WHERE shipment_day < ?", (cutoff,))
            rows = db.execute(
                "SELECT * FROM pending WHERE status='pending' AND shipment_day BETWEEN ? AND ?",
                (cutoff, today.isoformat()),
            ).fetchall()
        return [dict(row) for row in rows if row["source"] in sources]

    def record(self, raw: RawCustomsData, skipped: set[tuple[str, str]]) -> None:
        grouped = {}
        for item in raw.shipment_items:
            key = (item.source, item.shipment_no)
            if key not in skipped:
                continue
            group = grouped.setdefault(key, {"days": set(), "shops": set(), "fields": set()})
            group["days"].add(item.shipment_date[:10])
            group["shops"].add(item.seller_name)
            if missing_mapping_value(item.final_customer):
                group["fields"].add("final_cust")
            if missing_mapping_value(item.purchase_entity):
                group["fields"].add("working_corp_name")
        with self._connect() as db:
            for (source, shipment_no), group in grouped.items():
                # The fetchers provide one confirmed shipment day per shipment.
                day = max(group["days"])
                date.fromisoformat(day)
                db.execute("""INSERT INTO pending (source, shipment_no, shipment_day, shops, reason)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source, shipment_no) DO UPDATE SET
                    shipment_day=excluded.shipment_day, shops=excluded.shops,
                    reason=excluded.reason, status='pending', updated_at=CURRENT_TIMESTAMP""",
                    (source, shipment_no, day, json.dumps(sorted(group["shops"]), ensure_ascii=False),
                     "missing " + ",".join(sorted(group["fields"]))),
                )

    def complete(self, keys: set[tuple[str, str]]) -> None:
        # Call only AFTER the MySQL transaction commits. A crash in between is
        # safe: the stable row IDs make the next recovery attempt idempotent.
        with self._connect() as db:
            db.executemany("DELETE FROM pending WHERE source=? AND shipment_no=?", sorted(keys))
