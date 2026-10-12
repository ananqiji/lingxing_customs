from __future__ import annotations

import unittest
from threading import Barrier
from datetime import date, datetime
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src.shipment.job import _load_raw_data_for_dates, _shipment_times, run_shipment_job
from src.shipment.models import RawCustomsData


class ShipmentJobTest(unittest.TestCase):
    def setUp(self):
        self.mapping_patch = patch("src.shipment.job.load_shop_mapping_for_current_slot", return_value=SimpleNamespace(
            mapping={}, warning=None, slot_key="test", source="test", loaded_rows=0))
        self.mapping_patch.start()
        self.addCleanup(self.mapping_patch.stop)
        self.state_dir = TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        from src.shipment.mapping_recovery import MappingRecoveryStore
        store_patch = patch("src.shipment.job.MappingRecoveryStore", return_value=MappingRecoveryStore(self.state_dir.name + "/pending.sqlite3"))
        store_patch.start()
        self.addCleanup(store_patch.stop)

    def test_omitted_shipment_time_runs_yesterday_and_today(self) -> None:
        args = SimpleNamespace(shipment_time=None, shipment_time_provided=False)

        with patch("src.shipment.job._today", return_value=date(2026, 6, 17)):
            shipment_times = _shipment_times(args)

        self.assertEqual(shipment_times, ["2026-06-16", "2026-06-17"])

    def test_explicit_shipment_time_runs_only_that_day(self) -> None:
        args = SimpleNamespace(shipment_time="2026-06-13", shipment_time_provided=True)

        with patch("src.shipment.job._today", return_value=date(2026, 6, 17)):
            shipment_times = _shipment_times(args)

        self.assertEqual(shipment_times, ["2026-06-13"])

    def test_write_db_without_output_skips_excel_export(self) -> None:
        args = SimpleNamespace(
            clear_cache=False,
            use_sample_data=True,
            refresh_cache=False,
            shipment_time="2026-06-13",
            shipment_time_provided=True,
            output=None,
            db_preflight=False,
            write_db=True,
        )

        with (
            patch("src.shipment.job.SampleDataSource", return_value=FakeShipmentDataSource()),
            patch("src.shipment.job.build_customs_workbook_data", return_value=SimpleNamespace(customs_rows=[], issue_rows=[], purchase_split_rows=[])),
            patch("src.shipment.job.export_customs_workbook") as export_excel,
            patch("src.shipment.job.preflight_customs_rows_mysql", return_value=SimpleNamespace(table="customs_bill_parcels", row_count=0, duplicate_id_count=0)),
            patch(
                "src.shipment.job.export_customs_rows_to_mysql",
                return_value=SimpleNamespace(upserted_rows=0, stale_deleted_by_source={}, retention_deleted_rows=0),
            ),
            patch("src.shipment.job.report_shipment_customs_data_issues") as report_issues,
        ):
            run_shipment_job(args)

        export_excel.assert_not_called()
        report_issues.assert_called_once()

    def test_default_write_db_runs_retention_cleanup_once_per_day(self) -> None:
        args = SimpleNamespace(
            clear_cache=False,
            use_sample_data=True,
            refresh_cache=False,
            shipment_source="sample",
            shipment_time=None,
            shipment_time_provided=False,
            output=None,
            db_preflight=False,
            write_db=True,
        )
        workbook_data = SimpleNamespace(customs_rows=[], issue_rows=[], purchase_split_rows=[])

        with TemporaryDirectory() as tmpdir:
            with (
                patch.dict("os.environ", {"LINGXING_CACHE_DIR": tmpdir}),
                patch("src.shipment.job._today", return_value=date(2026, 8, 12)),
                patch("src.shipment.job._now", return_value=datetime(2026, 8, 12, 7, 0)),
                patch("src.shipment.job.SampleDataSource", return_value=FakeShipmentDataSource()),
                patch("src.shipment.job.build_customs_workbook_data", return_value=workbook_data),
                patch("src.shipment.job.preflight_customs_rows_mysql", return_value=SimpleNamespace(table="customs_bill_parcels", row_count=0, duplicate_id_count=0)),
                patch(
                    "src.shipment.job.export_customs_rows_to_mysql",
                    return_value=SimpleNamespace(upserted_rows=0, stale_deleted_by_source={}, retention_deleted_rows=5),
                ) as export_mysql,
                patch("src.shipment.job.report_shipment_customs_data_issues"),
            ):
                run_shipment_job(args)
                run_shipment_job(args)

        self.assertEqual([call.kwargs["delete_retention"] for call in export_mysql.call_args_list], [True, False])

    def test_default_write_db_skips_retention_cleanup_after_morning_window(self) -> None:
        args = SimpleNamespace(
            clear_cache=False,
            use_sample_data=True,
            refresh_cache=False,
            shipment_source="sample",
            shipment_time=None,
            shipment_time_provided=False,
            output=None,
            db_preflight=False,
            write_db=True,
        )
        workbook_data = SimpleNamespace(customs_rows=[], issue_rows=[], purchase_split_rows=[])

        with TemporaryDirectory() as tmpdir:
            with (
                patch.dict("os.environ", {"LINGXING_CACHE_DIR": tmpdir}),
                patch("src.shipment.job._today", return_value=date(2026, 8, 12)),
                patch("src.shipment.job._now", return_value=datetime(2026, 8, 12, 7, 5)),
                patch("src.shipment.job.SampleDataSource", return_value=FakeShipmentDataSource()),
                patch("src.shipment.job.build_customs_workbook_data", return_value=workbook_data),
                patch("src.shipment.job.preflight_customs_rows_mysql", return_value=SimpleNamespace(table="customs_bill_parcels", row_count=0, duplicate_id_count=0)),
                patch(
                    "src.shipment.job.export_customs_rows_to_mysql",
                    return_value=SimpleNamespace(upserted_rows=0, stale_deleted_by_source={}, retention_deleted_rows=0),
                ) as export_mysql,
                patch("src.shipment.job.report_shipment_customs_data_issues"),
            ):
                run_shipment_job(args)

        self.assertFalse(export_mysql.call_args.kwargs["delete_retention"])

    def test_explicit_shipment_time_write_db_skips_retention_cleanup(self) -> None:
        args = SimpleNamespace(
            clear_cache=False,
            use_sample_data=True,
            refresh_cache=False,
            shipment_source="sample",
            shipment_time="2026-08-05",
            shipment_time_provided=True,
            output=None,
            db_preflight=False,
            write_db=True,
        )
        workbook_data = SimpleNamespace(customs_rows=[], issue_rows=[], purchase_split_rows=[])

        with TemporaryDirectory() as tmpdir:
            with (
                patch.dict("os.environ", {"LINGXING_CACHE_DIR": tmpdir}),
                patch("src.shipment.job._today", return_value=date(2026, 8, 12)),
                patch("src.shipment.job._now", return_value=datetime(2026, 8, 12, 7, 0)),
                patch("src.shipment.job.SampleDataSource", return_value=FakeShipmentDataSource()),
                patch("src.shipment.job.build_customs_workbook_data", return_value=workbook_data),
                patch("src.shipment.job.preflight_customs_rows_mysql", return_value=SimpleNamespace(table="customs_bill_parcels", row_count=0, duplicate_id_count=0)),
                patch(
                    "src.shipment.job.export_customs_rows_to_mysql",
                    return_value=SimpleNamespace(upserted_rows=0, stale_deleted_by_source={}, retention_deleted_rows=0),
                ) as export_mysql,
                patch("src.shipment.job.report_shipment_customs_data_issues"),
            ):
                run_shipment_job(args)

        self.assertFalse(export_mysql.call_args.kwargs["delete_retention"])

    def test_product_master_failure_does_not_stop_shipment_job(self) -> None:
        args = SimpleNamespace(
            clear_cache=False,
            use_sample_data=True,
            refresh_cache=False,
            shipment_time="2026-06-13",
            shipment_time_provided=True,
            output=None,
            db_preflight=False,
            write_db=False,
        )
        workbook_data = SimpleNamespace(customs_rows=[], issue_rows=[], purchase_split_rows=[])

        with (
            patch("src.shipment.job.SampleDataSource", return_value=FakeShipmentDataSource()),
            patch("src.shipment.job.apply_product_master_data", side_effect=RuntimeError("db down")),
            patch("src.shipment.job.build_customs_workbook_data", return_value=workbook_data) as build_rows,
            patch("src.shipment.job.export_customs_workbook") as export_excel,
            patch("src.shipment.job.report_shipment_customs_data_issues"),
        ):
            run_shipment_job(args)

        build_rows.assert_called_once()
        export_excel.assert_not_called()

    def test_all_sources_load_in_parallel_for_each_date(self) -> None:
        barrier = Barrier(2)
        sources = [BarrierShipmentDataSource("amazon", barrier), BarrierShipmentDataSource("overseas", barrier)]

        raw = _load_raw_data_for_dates(sources, ["2026-07-02"])

        self.assertEqual(raw.metadata["amazon"], "2026-07-02")
        self.assertEqual(raw.metadata["overseas"], "2026-07-02")

    def test_all_source_failure_does_not_build_or_write_rows(self) -> None:
        args = SimpleNamespace(
            clear_cache=False,
            use_sample_data=False,
            refresh_cache=False,
            shipment_source="all",
            shipment_time="2026-07-02",
            shipment_time_provided=True,
            output=None,
            db_preflight=False,
            write_db=True,
        )

        with (
            patch("src.shipment.job._shipment_data_sources", return_value=[FakeShipmentDataSource(), FailingShipmentDataSource()]),
            patch("src.shipment.job.build_customs_workbook_data") as build_rows,
            patch("src.shipment.job.export_customs_rows_to_mysql") as export_mysql,
        ):
            with self.assertRaises(RuntimeError):
                run_shipment_job(args)

        build_rows.assert_not_called()
        export_mysql.assert_not_called()


class FakeShipmentDataSource:
    def load(self, shipment_time):
        return RawCustomsData()


class FailingShipmentDataSource:
    def load(self, shipment_time):
        raise RuntimeError("source failed")


class BarrierShipmentDataSource:
    def __init__(self, name: str, barrier: Barrier) -> None:
        self.name = name
        self.barrier = barrier

    def load(self, shipment_time):
        self.barrier.wait(timeout=1)
        return RawCustomsData(metadata={self.name: shipment_time})

if __name__ == "__main__":
    unittest.main()
