from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import src.shipment.export_mysql as export_mysql
from src.shipment.export_mysql import (
    MYSQL_COLUMNS,
    MySQLConfig,
    MySQLExportError,
    build_delete_retention_sql,
    build_delete_stale_sql,
    build_upsert_sql,
    export_customs_rows_to_mysql,
    mysql_row_values,
    preflight_customs_rows_mysql,
    validate_table_columns,
    validate_string_lengths,
    validate_unique_id_index,
    validate_unique_row_ids,
)
from src.shipment.models import CustomsRow, CustomsWorkbookData


def sample_row() -> CustomsRow:
    return CustomsRow(
        id="abc123",
        shipment_date="2026-06",
        shipment_day="2026-06-09",
        shipment_no="SP260609001",
        seller_name="YYOUNG-US",
        final_customer="最终客户",
        dest_country="美国",
        purchase_entity="采购方",
        supplier="供应商",
        domestic_source="供应商地址",
        sku="00123",
        pieces=Decimal("4"),
        product_name="品名",
        customs_name_cn="中文名",
        customs_name_en="",
        unit="件",
        shipment_quantity=Decimal("52"),
        purchase_unit_price=Decimal("8.75"),
        updated_at="2026-06-09 12:00:00",
        trade_term="FOB",
        payment_method_name="t/t",
        currency="美元",
        logistics_provider="物流商",
        logistics_channel="物流渠道",
        transport_method="快递",
        logistics_center_code="ABE8",
        logistics_center_region="美东",
        package_type="cnts",
        box_no="BOX001",
        box_count=Decimal("1"),
        total_gross_weight=Decimal("21.41"),
        total_net_weight=Decimal("20.41"),
        outer_box_size="55*43*31",
        volume=Decimal("0.073"),
    )


def overseas_row() -> CustomsRow:
    row = sample_row()
    row.id = "ows123"
    row.shipment_no = "OWS260609001"
    row.source = "overseas"
    return row


class ExportMySQLTest(unittest.TestCase):
    def test_mysql_columns_use_required_order_and_transport_method(self) -> None:
        self.assertEqual([column for _, column in MYSQL_COLUMNS], [
            "id",
            "confirm_shipment_month",
            "confirm_shipment",
            "tran_id",
            "seller_name",
            "final_cust",
            "dest_country",
            "working_corp_name",
            "supplier_name",
            "supplier_addr",
            "price_term",
            "pay_term",
            "currency",
            "item_code",
            "copies",
            "name",
            "chinese_customs_name",
            "english_customs_name",
            "unit",
            "quantity",
            "purchase_price",
            "logistics_name",
            "logistics_channel",
            "tran_way",
            "center_id",
            "centerid_region",
            "package",
            "box_no",
            "packing_carton_num",
            "item_total_gross_weight",
            "item_total_net_weight",
            "measure",
            "cube",
            "update_time",
        ])

    def test_mysql_row_values_convert_decimals_and_keep_empty_english_name_blank(self) -> None:
        values = mysql_row_values(sample_row())

        self.assertEqual(values[0], "abc123")
        self.assertEqual(values[1], "2026-06")
        self.assertEqual(values[2], "2026-06-09")
        self.assertEqual(values[4], "YYOUNG-US")
        self.assertEqual(values[5], "最终客户")
        self.assertEqual(values[6], "美国")
        self.assertEqual(values[17], "")
        self.assertEqual(values[23], "快递")
        self.assertEqual(values[24], "ABE8")
        self.assertEqual(values[25], "美东")
        self.assertEqual(values[28], "1")
        self.assertEqual(values[29], "21.41")

    def test_mysql_row_values_convert_invalid_shipment_month_to_null(self) -> None:
        row = sample_row()
        row.shipment_date = "0"

        values = mysql_row_values(row)

        self.assertIsNone(values[1])

    def test_upsert_sql_updates_all_columns_except_id(self) -> None:
        sql = build_upsert_sql("customs_bill_parcels")

        self.assertIn("INSERT INTO `customs_bill_parcels`", sql)
        self.assertIn("`confirm_shipment`", sql)
        self.assertIn("`final_cust`", sql)
        self.assertIn("`tran_way`", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        update_clause = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
        self.assertNotIn("`id`=VALUES(`id`)", update_clause)
        self.assertIn("`update_time`=VALUES(`update_time`)", update_clause)

    def test_delete_stale_sql_limits_by_day_source_and_current_ids(self) -> None:
        sql = build_delete_stale_sql("customs_bill_parcels", 2)

        self.assertIn("DELETE FROM `customs_bill_parcels`", sql)
        self.assertIn("`confirm_shipment` = %s", sql)
        self.assertIn("`tran_id` LIKE %s", sql)
        self.assertIn("`id` NOT IN (%s, %s)", sql)

    def test_delete_retention_sql_deletes_rows_before_cutoff(self) -> None:
        sql = build_delete_retention_sql("customs_bill_parcels")

        self.assertEqual(sql, "DELETE FROM `customs_bill_parcels` WHERE `confirm_shipment` < %s")

    def test_retention_keeps_tenth_day_and_deletes_only_older_rows(self) -> None:
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE parcels (id TEXT PRIMARY KEY, confirm_shipment TEXT)")
            db.executemany("INSERT INTO parcels VALUES (?, ?)", [
                ("expired", "2026-10-07"), ("tenth_day", "2026-10-08"),
                ("eighth_day", "2026-10-10"), ("today", "2026-10-17"),
                ("future", "2026-10-18"), ("undated", None),
            ])

            class Cursor:
                rowcount = 0

                def execute(self, sql, params):
                    self.rowcount = db.execute(sql.replace("%s", "?"), params).rowcount

            deleted = export_mysql._delete_rows_before_retention_cutoff(
                Cursor(), "parcels", export_mysql._retention_cutoff(date(2026, 10, 17)),
            )
            self.assertEqual(deleted, 1)
            self.assertEqual({row[0] for row in db.execute("SELECT id FROM parcels")},
                             {"tenth_day", "eighth_day", "today", "future", "undated"})

    def test_ten_day_retention_crosses_year_boundary(self) -> None:
        self.assertEqual(export_mysql._retention_cutoff(date(2027, 1, 5)), date(2026, 12, 27))

    def test_validate_table_columns_reports_missing_columns(self) -> None:
        columns = [column for _, column in MYSQL_COLUMNS if column not in {"tran_way", "update_time"}]

        with self.assertRaises(MySQLExportError) as context:
            validate_table_columns(columns)

        message = str(context.exception)
        self.assertIn("tran_way", message)
        self.assertIn("update_time", message)

    def test_validate_string_lengths_reports_box_no_context(self) -> None:
        row = sample_row()
        row.box_no = "B" * 101

        with self.assertRaises(MySQLExportError) as context:
            validate_string_lengths(CustomsWorkbookData(customs_rows=[row], issue_rows=[], purchase_split_rows=[]), {"box_no": 100})

        message = str(context.exception)
        self.assertIn("column=box_no", message)
        self.assertIn("length=101", message)
        self.assertIn("shipment_no=SP260609001", message)
        self.assertIn("sku=00123", message)
        self.assertIn("box_no_length=101", message)

    def test_validate_unique_id_index_accepts_single_column_unique_id(self) -> None:
        validate_unique_id_index(
            [
                {"Key_name": "PRIMARY", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "id"},
            ]
        )

    def test_validate_unique_id_index_rejects_missing_unique_id(self) -> None:
        with self.assertRaises(MySQLExportError):
            validate_unique_id_index(
                [
                    {"Key_name": "idx_id", "Non_unique": 1, "Seq_in_index": 1, "Column_name": "id"},
                ]
            )

    def test_validate_unique_row_ids_reports_duplicates(self) -> None:
        row = sample_row()
        duplicate = sample_row()

        with self.assertRaises(MySQLExportError) as context:
            validate_unique_row_ids(CustomsWorkbookData(customs_rows=[row, duplicate], issue_rows=[], purchase_split_rows=[]))

        self.assertIn("duplicate id", str(context.exception))

    def test_open_mysql_connection_uses_direct_host_when_tunnel_disabled(self) -> None:
        fake_pymysql = FakePyMySQL()
        config = mysql_config(use_ssh_tunnel=False)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql):
            connection, tunnel = export_mysql._open_mysql_connection(config)

        self.assertIsNone(tunnel)
        self.assertIs(connection, fake_pymysql.connection)
        self.assertEqual(fake_pymysql.connect_kwargs["host"], "172.31.0.4")
        self.assertEqual(fake_pymysql.connect_kwargs["port"], 3306)

    def test_open_mysql_connection_uses_ssh_tunnel_local_port_when_enabled(self) -> None:
        fake_pymysql = FakePyMySQL()
        fake_tunnel_factory = FakeTunnelFactory(local_bind_port=43306)
        config = mysql_config(use_ssh_tunnel=True)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql), patch.object(
            export_mysql, "SSHTunnelForwarderFactory", fake_tunnel_factory
        ):
            connection, tunnel = export_mysql._open_mysql_connection(config)

        self.assertIs(connection, fake_pymysql.connection)
        self.assertIs(tunnel, fake_tunnel_factory.tunnel)
        self.assertTrue(tunnel.started)
        self.assertEqual(fake_pymysql.connect_kwargs["host"], "127.0.0.1")
        self.assertEqual(fake_pymysql.connect_kwargs["port"], 43306)
        self.assertEqual(fake_tunnel_factory.kwargs["remote_bind_address"], ("172.31.0.4", 3306))

    def test_open_mysql_connection_stops_tunnel_when_mysql_connect_fails(self) -> None:
        fake_pymysql = FakePyMySQL(connect_error=RuntimeError("mysql down"))
        fake_tunnel_factory = FakeTunnelFactory(local_bind_port=43306)
        config = mysql_config(use_ssh_tunnel=True)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql), patch.object(
            export_mysql, "SSHTunnelForwarderFactory", fake_tunnel_factory
        ):
            with self.assertRaises(MySQLExportError):
                export_mysql._open_mysql_connection(config)

        self.assertTrue(fake_tunnel_factory.tunnel.stopped)

    def test_preflight_checks_table_without_upsert(self) -> None:
        fake_pymysql = FakePyMySQL()
        fake_pymysql.connection = FakeConnection(
            columns=[column for _, column in MYSQL_COLUMNS],
            indexes=[{"Key_name": "PRIMARY", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "id"}],
        )
        config = mysql_config(use_ssh_tunnel=False)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql):
            result = preflight_customs_rows_mysql(
                CustomsWorkbookData(customs_rows=[sample_row()], issue_rows=[], purchase_split_rows=[]),
                config,
            )

        self.assertEqual(result.row_count, 1)
        self.assertEqual(result.table, "customs_bill_parcels")
        self.assertEqual(fake_pymysql.connection.cursor_obj.executemany_calls, [])
        self.assertFalse(any("DELETE FROM" in sql for sql, _ in fake_pymysql.connection.cursor_obj.execute_calls))

    def test_export_deletes_stale_amazon_rows_before_upsert(self) -> None:
        fake_pymysql = FakePyMySQL()
        fake_pymysql.connection = FakeConnection(
            columns=[column for _, column in MYSQL_COLUMNS],
            indexes=[{"Key_name": "PRIMARY", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "id"}],
            delete_rowcounts=[3],
        )
        config = mysql_config(use_ssh_tunnel=False)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql), patch.object(export_mysql, "_today", return_value=date(2026, 6, 17)):
            result = export_customs_rows_to_mysql(
                CustomsWorkbookData(customs_rows=[sample_row()], issue_rows=[], purchase_split_rows=[]),
                config,
            )

        self.assertEqual(result.upserted_rows, 1)
        self.assertEqual(result.stale_deleted_by_source, {"amazon": 3})
        self.assertEqual(result.retention_deleted_rows, 0)
        delete_calls = [(sql, params) for sql, params in fake_pymysql.connection.cursor_obj.execute_calls if sql.startswith("DELETE FROM")]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0][1], ["2026-06-09", "SP%", "abc123"])
        self.assertEqual(len(fake_pymysql.connection.cursor_obj.executemany_calls), 1)

    def test_export_deletes_stale_sources_independently(self) -> None:
        fake_pymysql = FakePyMySQL()
        fake_pymysql.connection = FakeConnection(
            columns=[column for _, column in MYSQL_COLUMNS],
            indexes=[{"Key_name": "PRIMARY", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "id"}],
            delete_rowcounts=[1, 1],
        )
        config = mysql_config(use_ssh_tunnel=False)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql), patch.object(export_mysql, "_today", return_value=date(2026, 6, 17)):
            result = export_customs_rows_to_mysql(
                CustomsWorkbookData(customs_rows=[sample_row(), overseas_row()], issue_rows=[], purchase_split_rows=[]),
                config,
            )

        delete_params = [
            params
            for sql, params in fake_pymysql.connection.cursor_obj.execute_calls
            if sql.startswith("DELETE FROM")
        ]
        self.assertEqual(result.upserted_rows, 2)
        self.assertEqual(result.stale_deleted_by_source, {"amazon": 1, "overseas": 1})
        self.assertEqual(result.retention_deleted_rows, 0)
        self.assertIn(["2026-06-09", "SP%", "abc123"], delete_params)
        self.assertIn(["2026-06-09", "OWS%", "ows123"], delete_params)

    def test_export_skips_protected_rows_without_deleting_them_as_stale(self) -> None:
        fake_pymysql = FakePyMySQL()
        fake_pymysql.connection = FakeConnection(
            columns=[column for _, column in MYSQL_COLUMNS],
            indexes=[{"Key_name": "PRIMARY", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "id"}],
            delete_rowcounts=[0],
        )
        protected = sample_row()
        protected.id = "65d284a91e0aa7be"
        protected.box_no = "FBA19LYY3F5GU000007"
        normal = sample_row()
        normal.id = "normal123"
        normal.box_no = "FBA19LYY3F5GU000009"
        config = mysql_config(use_ssh_tunnel=False)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql), patch.object(export_mysql, "_today", return_value=date(2026, 6, 17)):
            result = export_customs_rows_to_mysql(
                CustomsWorkbookData(customs_rows=[protected, normal], issue_rows=[], purchase_split_rows=[]),
                config,
            )

        self.assertEqual(result.upserted_rows, 1)
        self.assertEqual(result.protected_skipped_rows, 1)
        delete_calls = [(sql, params) for sql, params in fake_pymysql.connection.cursor_obj.execute_calls if sql.startswith("DELETE FROM")]
        self.assertEqual(delete_calls[0][1], ["2026-06-09", "SP%", "65d284a91e0aa7be", "normal123"])
        self.assertEqual(len(fake_pymysql.connection.cursor_obj.executemany_calls), 1)
        upsert_rows = fake_pymysql.connection.cursor_obj.executemany_calls[0][1]
        self.assertEqual([row[0] for row in upsert_rows], ["normal123"])

    def test_export_skips_protected_shipment_sku_rows_without_deleting_them_as_stale(self) -> None:
        fake_pymysql = FakePyMySQL()
        fake_pymysql.connection = FakeConnection(
            columns=[column for _, column in MYSQL_COLUMNS],
            indexes=[{"Key_name": "PRIMARY", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "id"}],
            delete_rowcounts=[0],
        )
        protected = sample_row()
        protected.id = "protected-by-key"
        protected.shipment_no = "SP260821045"
        protected.sku = "PK-3-P-LGG-L9-F-H-D-L-XL-03"
        normal = sample_row()
        normal.id = "normal123"
        normal.shipment_no = "SP260821045"
        normal.sku = "NOT-PROTECTED"
        config = mysql_config(use_ssh_tunnel=False)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql), patch.object(export_mysql, "_today", return_value=date(2026, 8, 22)):
            result = export_customs_rows_to_mysql(
                CustomsWorkbookData(customs_rows=[protected, normal], issue_rows=[], purchase_split_rows=[]),
                config,
            )

        self.assertEqual(result.upserted_rows, 1)
        self.assertEqual(result.protected_skipped_rows, 1)
        delete_calls = [(sql, params) for sql, params in fake_pymysql.connection.cursor_obj.execute_calls if sql.startswith("DELETE FROM")]
        self.assertEqual(delete_calls[0][1], ["2026-06-09", "SP%", "normal123", "protected-by-key"])
        self.assertEqual(len(fake_pymysql.connection.cursor_obj.executemany_calls), 1)
        upsert_rows = fake_pymysql.connection.cursor_obj.executemany_calls[0][1]
        self.assertEqual([row[0] for row in upsert_rows], ["normal123"])

    def test_export_skips_protected_shipment_box_rows_without_deleting_them_as_stale(self) -> None:
        fake_pymysql = FakePyMySQL()
        fake_pymysql.connection = FakeConnection(
            columns=[column for _, column in MYSQL_COLUMNS],
            indexes=[{"Key_name": "PRIMARY", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "id"}],
            delete_rowcounts=[0],
        )
        protected = sample_row()
        protected.id = "protected-by-box"
        protected.shipment_no = "SP260907023"
        protected.box_no = "FBA19P1LKHYBU000005"
        normal = sample_row()
        normal.id = "normal123"
        normal.shipment_no = "SP260907023"
        normal.box_no = "FBA19P1LKHYBU000007"
        config = mysql_config(use_ssh_tunnel=False)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql), patch.object(export_mysql, "_today", return_value=date(2026, 9, 11)):
            result = export_customs_rows_to_mysql(
                CustomsWorkbookData(customs_rows=[protected, normal], issue_rows=[], purchase_split_rows=[]),
                config,
            )

        self.assertEqual(result.upserted_rows, 1)
        self.assertEqual(result.protected_skipped_rows, 1)
        delete_calls = [(sql, params) for sql, params in fake_pymysql.connection.cursor_obj.execute_calls if sql.startswith("DELETE FROM")]
        self.assertEqual(delete_calls[0][1], ["2026-06-09", "SP%", "normal123", "protected-by-box"])
        self.assertEqual(len(fake_pymysql.connection.cursor_obj.executemany_calls), 1)
        upsert_rows = fake_pymysql.connection.cursor_obj.executemany_calls[0][1]
        self.assertEqual([row[0] for row in upsert_rows], ["normal123"])

    def test_export_empty_batch_skips_retention_by_default(self) -> None:
        fake_pymysql = FakePyMySQL()
        fake_pymysql.connection = FakeConnection(
            columns=[column for _, column in MYSQL_COLUMNS],
            indexes=[{"Key_name": "PRIMARY", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "id"}],
        )
        config = mysql_config(use_ssh_tunnel=False)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql), patch.object(export_mysql, "_today", return_value=date(2026, 6, 17)):
            result = export_customs_rows_to_mysql(
                CustomsWorkbookData(customs_rows=[], issue_rows=[], purchase_split_rows=[]),
                config,
            )

        self.assertEqual(result.upserted_rows, 0)
        self.assertEqual(result.stale_deleted_by_source, {})
        self.assertEqual(result.retention_deleted_rows, 0)
        delete_calls = [(sql, params) for sql, params in fake_pymysql.connection.cursor_obj.execute_calls if sql.startswith("DELETE FROM")]
        self.assertEqual(delete_calls, [])
        self.assertEqual(fake_pymysql.connection.cursor_obj.executemany_calls, [])

    def test_export_runs_retention_when_requested(self) -> None:
        fake_pymysql = FakePyMySQL()
        fake_pymysql.connection = FakeConnection(
            columns=[column for _, column in MYSQL_COLUMNS],
            indexes=[{"Key_name": "PRIMARY", "Non_unique": 0, "Seq_in_index": 1, "Column_name": "id"}],
            delete_rowcounts=[4],
        )
        config = mysql_config(use_ssh_tunnel=False)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql), patch.object(export_mysql, "_today", return_value=date(2026, 6, 17)):
            result = export_customs_rows_to_mysql(
                CustomsWorkbookData(customs_rows=[], issue_rows=[], purchase_split_rows=[]),
                config,
                delete_retention=True,
            )

        self.assertEqual(result.upserted_rows, 0)
        self.assertEqual(result.stale_deleted_by_source, {})
        self.assertEqual(result.retention_deleted_rows, 4)
        delete_calls = [(sql, params) for sql, params in fake_pymysql.connection.cursor_obj.execute_calls if sql.startswith("DELETE FROM")]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0][1], ["2026-06-08"])
        self.assertEqual(fake_pymysql.connection.cursor_obj.executemany_calls, [])

    def test_export_rolls_back_only_when_connection_exists(self) -> None:
        fake_pymysql = FakePyMySQL(connect_error=RuntimeError("mysql down"))
        config = mysql_config(use_ssh_tunnel=False)

        with patch.object(export_mysql, "PyMySQLModule", fake_pymysql):
            with self.assertRaises(MySQLExportError):
                export_mysql.export_customs_rows_to_mysql(
                    CustomsWorkbookData(customs_rows=[sample_row()], issue_rows=[], purchase_split_rows=[]),
                    config,
                )

    def test_mysql_config_requires_ssh_settings_when_tunnel_enabled(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MYSQL_HOST": "172.31.0.4",
                "MYSQL_PORT": "3306",
                "MYSQL_USER": "gmb001",
                "MYSQL_PASSWORD": "secret",
                "MYSQL_DATABASE": "dtw_gmb000053",
                "MYSQL_TABLE": "customs_bill_parcels",
                "MYSQL_USE_SSH_TUNNEL": "1",
                "SSH_HOST": "",
                "SSH_USER": "",
                "SSH_PASSWORD": "",
            },
            clear=True,
        ):
            with self.assertRaises(MySQLExportError) as context:
                MySQLConfig.from_env()

        self.assertIn("SSH_HOST", str(context.exception))


def mysql_config(use_ssh_tunnel: bool) -> MySQLConfig:
    return MySQLConfig(
        host="172.31.0.4",
        port=3306,
        user="gmb001",
        password="secret",
        database="dtw_gmb000053",
        table="customs_bill_parcels",
        use_ssh_tunnel=use_ssh_tunnel,
        ssh_host="115.190.118.240",
        ssh_port=22,
        ssh_user="root",
        ssh_password="ssh-secret",
    )


class FakeCursor:
    def __init__(self, columns=None, indexes=None, delete_rowcount=0, delete_rowcounts=None) -> None:
        self.columns = columns or []
        self.indexes = indexes or []
        self.delete_rowcount = delete_rowcount
        self.delete_rowcounts = list(delete_rowcounts or [])
        self.last_sql = ""
        self.execute_calls = []
        self.executemany_calls = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.execute_calls.append((sql, params))
        if sql.startswith("DELETE FROM"):
            self.rowcount = self.delete_rowcounts.pop(0) if self.delete_rowcounts else self.delete_rowcount
        else:
            self.rowcount = 0

    def executemany(self, sql, rows):
        self.executemany_calls.append((sql, rows))

    def fetchall(self):
        if "SHOW COLUMNS" in self.last_sql:
            return [column if isinstance(column, tuple) else (column,) for column in self.columns]
        if "SHOW INDEX" in self.last_sql:
            return self.indexes
        return []


class FakeConnection:
    def __init__(self, columns=None, indexes=None, delete_rowcount=0, delete_rowcounts=None) -> None:
        self.cursor_obj = FakeCursor(
            columns=columns,
            indexes=indexes,
            delete_rowcount=delete_rowcount,
            delete_rowcounts=delete_rowcounts,
        )
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakePyMySQL:
    def __init__(self, connect_error: Exception | None = None) -> None:
        self.connect_error = connect_error
        self.connection = FakeConnection()
        self.connect_kwargs = {}

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error
        return self.connection


class FakeTunnel:
    def __init__(self, local_bind_port: int) -> None:
        self.local_bind_port = local_bind_port
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class FakeTunnelFactory:
    def __init__(self, local_bind_port: int) -> None:
        self.tunnel = FakeTunnel(local_bind_port)
        self.kwargs = {}

    def __call__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return self.tunnel


if __name__ == "__main__":
    unittest.main()
