from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src.common.cache import JsonCache
from src.shipment.job import run_shipment_job
from src.shipment.mapping_recovery import MappingRecoveryStore
from src.shipment.models import RawCustomsData, ShipmentItem, CustomsWorkbookData
from src.shipment.shop_mapping import ShopMappingRecord, missing_mapping_value
from src.shipment import export_mysql
from tests.shipment.test_export_mysql import sample_row, FakeConnection, mysql_config, MYSQL_COLUMNS


def item(number="SP1", shop="A", day="2026-10-09", sku="SKU1"):
    return ShipmentItem(day, number, sku, Decimal(2), seller_name=shop)


class RecoveryTest(unittest.TestCase):
    def test_store_survives_restart_and_cache_clear_with_seven_day_boundary(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "pending.sqlite3"
            store = MappingRecoveryStore(path)
            raw = RawCustomsData(shipment_items=[item(day="2026-10-06"), item("SP2", day="2026-10-05")])
            store.record(raw, {("amazon", "SP1"), ("amazon", "SP2")})
            JsonCache(Path(directory) / ".cache").clear()
            reopened = MappingRecoveryStore(path)
            rows = reopened.pending(date(2026, 10, 12), ["amazon"])
            self.assertEqual([row["shipment_no"] for row in rows], ["SP1"])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT status FROM pending WHERE shipment_no='SP2'").fetchone()[0], "expired")
            reopened.complete({("amazon", "SP1")})
            self.assertEqual(reopened.pending(date(2026, 10, 12), ["amazon"]), [])

    def test_missing_value_statistics_cover_null_blank_and_pending(self):
        for value in (None, "", "  ", "待确认", " 待确认 "):
            self.assertTrue(missing_mapping_value(value))
        self.assertFalse(missing_mapping_value("Customer"))

    def run_job(self, store, days, mapping, *, fail_commit=False):
        calls = []
        class Source:
            def load(self, shipment_time):
                calls.append(shipment_time)
                if isinstance(days.get(shipment_time), Exception):
                    raise days[shipment_time]
                return RawCustomsData(shipment_items=list(days.get(shipment_time, [])))
        args = SimpleNamespace(clear_cache=False, use_sample_data=False, refresh_cache=False,
            shipment_source="amazon", shipment_time_provided=False, output=None, db_preflight=False, write_db=True)
        result = SimpleNamespace(upserted_rows=1, stale_deleted_by_source={}, retention_deleted_rows=0)
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            stack.enter_context(patch("src.shipment.job.MappingRecoveryStore", return_value=store))
            stack.enter_context(patch("src.shipment.job._today", return_value=date(2026, 10, 12)))
            stack.enter_context(patch("src.shipment.job._retention_cleanup_for_this_run", return_value=False))
            stack.enter_context(patch("src.shipment.job._shipment_data_sources", return_value=[Source()]))
            loader = stack.enter_context(patch("src.shipment.job._load_shop_mapping", return_value=SimpleNamespace(mapping=mapping)))
            for name in ("_apply_product_master_data", "_apply_warehouse_region_mapping", "write_overseas_field_debug",
                         "_print_api_performance_summary"):
                stack.enter_context(patch("src.shipment.job." + name))
            reporter = stack.enter_context(patch("src.shipment.job.report_shipment_customs_data_issues"))
            stack.enter_context(patch("src.shipment.job.preflight_customs_rows_mysql", return_value=SimpleNamespace(table="customs_bill_parcels", row_count=1)))
            writer = stack.enter_context(patch("src.shipment.job.export_customs_rows_to_mysql", return_value=result,
                side_effect=RuntimeError("commit failed") if fail_commit else None))
            if fail_commit:
                with self.assertRaisesRegex(RuntimeError, "commit failed"):
                    run_shipment_job(args)
            else:
                run_shipment_job(args)
            report_raw = reporter.call_args.args[0]
            self.assertTrue(all(row.shipment_date[:10] in {"2026-10-11", "2026-10-12"} for row in report_raw.shipment_items))
            return writer.call_args, calls, loader.call_args_list

    def test_historical_failure_or_wrong_date_keeps_pending_and_normal_writes(self):
        for historical in (RuntimeError("API unavailable"), [item("SPBAD", day="2026-10-10")]):
            with self.subTest(historical=historical), TemporaryDirectory() as directory:
                store = MappingRecoveryStore(Path(directory) / "pending.sqlite3")
                store.record(RawCustomsData(shipment_items=[item("SPBAD")]), {("amazon", "SPBAD")})
                call, _, _ = self.run_job(store, {
                    "2026-10-09": historical, "2026-10-11": [item("SPGOOD", day="2026-10-11")],
                }, {"A": ShopMappingRecord("A", purchase_entity="Entity", final_customer="Customer")})
                self.assertEqual({r.shipment_no for r in call.args[0].customs_rows}, {"SPGOOD"})
                self.assertIn(("amazon", "SPBAD"), call.kwargs["excluded_shipments"])
                self.assertEqual(len(store.pending(date(2026, 10, 12), ["amazon"])), 1)

    def test_whole_shipment_skipped_then_historical_retry_only_after_commit(self):
        with TemporaryDirectory() as directory:
            store = MappingRecoveryStore(Path(directory) / "state.sqlite3")
            mapping = {"A": ShopMappingRecord("A", purchase_entity="Entity", final_customer="Customer")}
            days = {"2026-10-11": [item("SPBAD", "A", "2026-10-11"), item("SPBAD", "MISSING", "2026-10-11", "SKU2"),
                                      item("SPGOOD", "A", "2026-10-11")]}
            call, _, loads = self.run_job(store, days, mapping)
            self.assertEqual({r.shipment_no for r in call.args[0].customs_rows}, {"SPGOOD"})
            self.assertEqual(call.kwargs["excluded_shipments"], {("amazon", "SPBAD")})
            self.assertTrue(loads[-1].kwargs["force_refresh"])
            self.assertEqual(len(store.pending(date(2026, 10, 12), ["amazon"])), 1)
            # Simulate a failure from three days earlier that fell out of the normal window.
            store.record(RawCustomsData(shipment_items=[item("SPBAD", "MISSING")]), {("amazon", "SPBAD")})
            mapping["MISSING"] = mapping["A"]
            historical = {"2026-10-09": [item("SPBAD", "MISSING"), item("SPUNRELATED")]}
            self.run_job(store, historical, mapping, fail_commit=True)
            self.assertEqual(len(store.pending(date(2026, 10, 12), ["amazon"])), 1)
            call, fetched, _ = self.run_job(store, historical, mapping)
            self.assertIn("2026-10-09", fetched)
            self.assertEqual({r.shipment_no for r in call.args[0].customs_rows}, {"SPBAD"})
            self.assertEqual(call.kwargs["cleanup_shipments"], {("amazon", "2026-10-09"): {"SPBAD"}})
            self.assertEqual(store.pending(date(2026, 10, 12), ["amazon"]), [])

    def test_pending_shipment_absent_from_api_is_preserved(self):
        with TemporaryDirectory() as directory:
            store = MappingRecoveryStore(Path(directory) / "pending.sqlite3")
            store.record(RawCustomsData(shipment_items=[item("SPABSENT", day="2026-10-11")]), {("amazon", "SPABSENT")})
            call, _, _ = self.run_job(store, {"2026-10-11": [item("SPGOOD", day="2026-10-11")]},
                {"A": ShopMappingRecord("A", purchase_entity="Entity", final_customer="Customer")})
            self.assertIn(("amazon", "SPABSENT"), call.kwargs["excluded_shipments"])
            self.assertEqual(len(store.pending(date(2026, 10, 12), ["amazon"])), 1)

    def test_cleanup_sql_preserves_skipped_and_unrelated_historical_shipments(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE parcels (id TEXT PRIMARY KEY, confirm_shipment TEXT, tran_id TEXT)")
            db.executemany("INSERT INTO parcels VALUES (?, '2026-06-09', ?)", [
                ("old", "SP260609001"), ("abc123", "SP260609001"), ("skipped", "SPBAD"), ("unrelated", "SPOTHER")])
            class Cursor:
                rowcount = 0
                def execute(self, sql, params):
                    self.rowcount = db.execute(sql.replace("%s", "?"), params).rowcount
            data = CustomsWorkbookData([sample_row()], [], [])
            deleted = export_mysql._delete_stale_rows_for_current_batch(Cursor(), "parcels", data,
                excluded_shipments={("amazon", "SPBAD")}, cleanup_shipments={("amazon", "2026-06-09"): {"SP260609001"}})
            self.assertEqual(deleted, {"amazon": 1})
            self.assertEqual({row[0] for row in db.execute("SELECT id FROM parcels")}, {"abc123", "skipped", "unrelated"})
            # Normal day-wide cleanup still excludes the skipped shipment.
            export_mysql._delete_stale_rows_for_current_batch(Cursor(), "parcels", data,
                excluded_shipments={("amazon", "SPBAD")})
            self.assertEqual({row[0] for row in db.execute("SELECT id FROM parcels")}, {"abc123", "skipped"})

    def test_export_boundary_excludes_writes_and_commit_failure_rolls_back(self):
        connection = FakeConnection(columns=[c for _, c in MYSQL_COLUMNS],
            indexes=[{"Key_name": "PRIMARY", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "id"}])
        good = sample_row()
        bad = replace(good, id="bad", shipment_no="SPBAD")
        data = CustomsWorkbookData([good, bad], [], [])
        with patch.object(export_mysql, "_open_mysql_connection", return_value=(connection, None)):
            result = export_mysql.export_customs_rows_to_mysql(data, mysql_config(False), excluded_shipments={("amazon", "SPBAD")})
        self.assertEqual(result.upserted_rows, 1)
        self.assertEqual(len(connection.cursor_obj.executemany_calls[0][1]), 1)
        with patch.object(export_mysql, "_open_mysql_connection", return_value=(connection, None)), patch.object(connection, "commit", side_effect=RuntimeError("commit failed")):
            with self.assertRaises(RuntimeError):
                export_mysql.export_customs_rows_to_mysql(data, mysql_config(False))
        self.assertTrue(connection.rolled_back)
