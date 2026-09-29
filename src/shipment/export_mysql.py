from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from src.common.lingxing_client import _load_dotenv
from src.shipment.models import CustomsRow, CustomsWorkbookData


MYSQL_COLUMNS = [
    ("id", "id"),
    ("shipment_date", "confirm_shipment_month"),
    ("shipment_day", "confirm_shipment"),
    ("shipment_no", "tran_id"),
    ("seller_name", "seller_name"),
    ("final_customer", "final_cust"),
    ("dest_country", "dest_country"),
    ("purchase_entity", "working_corp_name"),
    ("supplier", "supplier_name"),
    ("domestic_source", "supplier_addr"),
    ("trade_term", "price_term"),
    ("payment_method_name", "pay_term"),
    ("currency", "currency"),
    ("sku", "item_code"),
    ("pieces", "copies"),
    ("product_name", "name"),
    ("customs_name_cn", "chinese_customs_name"),
    ("customs_name_en", "english_customs_name"),
    ("unit", "unit"),
    ("shipment_quantity", "quantity"),
    ("purchase_unit_price", "purchase_price"),
    ("logistics_provider", "logistics_name"),
    ("logistics_channel", "logistics_channel"),
    ("transport_method", "tran_way"),
    ("logistics_center_code", "center_id"),
    ("logistics_center_region", "centerid_region"),
    ("package_type", "package"),
    ("box_no", "box_no"),
    ("box_count", "packing_carton_num"),
    ("total_gross_weight", "item_total_gross_weight"),
    ("total_net_weight", "item_total_net_weight"),
    ("outer_box_size", "measure"),
    ("volume", "cube"),
    ("updated_at", "update_time"),
]

DEFAULT_PROTECTED_CUSTOMS_ROW_IDS = {
    "65d284a91e0aa7be",
    "0ee5728c8df7367d",
    "4c40401ebad6e1e3",
    "b189c38afb73f32b",
    "bf4d648b1f8d060f",
    "3072064fbc50df68",
    "3a37da1af1226c48",
    "d7cd6b63329be7a4",
    "6c562ac919fcf1d8",
    "975c2fae71951d78",
    "7b1f201d151196c6",
    "9600277cb0180225",
    "b40d400610f291c2",
    "0fb955b8706bf888",
    "34db600b0f34bf41",
    "cc9faa15be1d306d",
    "d509b3e4397061b9",
    "eb4c649e3c103b21",
    "35ab01a363836307",
    "efa5d1b1c74dbdce",
    "c5cdb832082e9f17",
    "cc8a6af55665aee1",
    "01a876971c505eab",
    "7e5279937368b778",
    "b77d717b95bfeac1",
    "712fa122f2c123a3",
    "f1be791fe86dd2ae",
    "784634f73beb9815",
    "9138dfd37ce68238",
    "23f80b76a4b0a790",
    "5178d4127e453c79",
    "250222378bd13932",
    "fd6b62cf0ecda2f9",
    # Supplier and source overrides from customs_bill_parcels.xlsx, 2026-09-28.
    # SP260928085
    "0122daa6126af36c",
    "39395e8dc9c6c0cb",
    "3bc2c0dff6dce55e",
    "4650489d08e854a3",
    "52f5a4f540718167",
    "5dc4c6ff2e529ba4",
    "6d05ab561650a0b8",
    # SP260928106
    "bfddedfbcdc7c3c1",
    # SP260928108
    "0329b175b6e8c678",
    "0a6acdaa0f1b2553",
    "837a0767d6945c62",
    "93201227aa7475e6",
    "a0c5d5f4a27d28ac",
    "c204ab883b262a2f",
    # SP260928109
    "2e634ec7acfe2df1",
    "30d67a16b42637da",
    "aaf157553e9518ee",
    "cf491617c67c6a9f",
    # SP260928089: customs_bill_parcels 02.xlsx, 2026-09-28.
    "02d6793472d4de96",
    "079abccda02ec1e9",
    "7a70bde29c2344da",
    "94250830be00ef23",
    "9c25722c32aa5a18",
    "9c7fbe63e435fece",
    "e2aa7b8b78c0f62e",
    "f679947bd902bb06",
}

DEFAULT_PROTECTED_CUSTOMS_SHIPMENT_NOS = {
    "SP260821045",
    "SP260821051",
    "SP260821052",
    "SP260821053",
    "SP260821054",
}

DEFAULT_PROTECTED_CUSTOMS_SKUS = {
    "PK-3-P-LGG-L9-F-H-D-L-XL-03",
    "PK-3-P-LGG-L9-F-H-D-L-XL-06",
    "PK-3-P-LGG-L9-F-H-D-S-M-02",
    "PK-3-P-LGG-L9-F-H-D-S-M-03",
    "PK-3-P-LGG-L9-F-H-D-S-M-05",
}

DEFAULT_PROTECTED_CUSTOMS_ROW_KEYS = {
    (shipment_no, sku)
    for shipment_no in DEFAULT_PROTECTED_CUSTOMS_SHIPMENT_NOS
    for sku in DEFAULT_PROTECTED_CUSTOMS_SKUS
}

DEFAULT_PROTECTED_CUSTOMS_SHIPMENT_BOXES = {
    ("SP260907023", "FBA19P1LKHYBU000005"),
    ("SP260907023", "FBA19P1LKHYBU000006"),
}

SSHTunnelForwarderFactory: Any | None = None
PyMySQLModule: Any | None = None


class MySQLExportError(RuntimeError):
    pass


@dataclass(frozen=True)
class MySQLPreflightResult:
    table: str
    row_count: int
    duplicate_id_count: int = 0


@dataclass(frozen=True)
class MySQLExportResult:
    upserted_rows: int
    stale_deleted_by_source: dict[str, int]
    retention_deleted_rows: int = 0
    protected_skipped_rows: int = 0


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    table: str = "customs_bill_parcels"
    charset: str = "utf8mb4"
    use_ssh_tunnel: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_password: str = ""

    @classmethod
    def from_env(cls) -> "MySQLConfig":
        _load_dotenv()
        config = cls(
            host=os.getenv("MYSQL_HOST", ""),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", ""),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", ""),
            table=os.getenv("MYSQL_TABLE", "customs_bill_parcels"),
            use_ssh_tunnel=_env_bool(os.getenv("MYSQL_USE_SSH_TUNNEL", "0")),
            ssh_host=os.getenv("SSH_HOST", ""),
            ssh_port=int(os.getenv("SSH_PORT", "22")),
            ssh_user=os.getenv("SSH_USER", ""),
            ssh_password=os.getenv("SSH_PASSWORD", ""),
        )
        missing = [
            name
            for name, value in (
                ("MYSQL_HOST", config.host),
                ("MYSQL_USER", config.user),
                ("MYSQL_PASSWORD", config.password),
                ("MYSQL_DATABASE", config.database),
                ("MYSQL_TABLE", config.table),
            )
            if not value
        ]
        if missing:
            raise MySQLExportError("Missing MySQL config in .env: " + ", ".join(missing))
        if config.use_ssh_tunnel:
            ssh_missing = [
                name
                for name, value in (
                    ("SSH_HOST", config.ssh_host),
                    ("SSH_USER", config.ssh_user),
                    ("SSH_PASSWORD", config.ssh_password),
                )
                if not value
            ]
            if ssh_missing:
                raise MySQLExportError("Missing SSH tunnel config in .env: " + ", ".join(ssh_missing))
        return config


def export_customs_rows_to_mysql(
    data: CustomsWorkbookData,
    config: MySQLConfig | None = None,
    *,
    delete_retention: bool = False,
) -> MySQLExportResult:
    config = config or MySQLConfig.from_env()
    protected_ids = _protected_customs_row_ids()
    protected_keys = _protected_customs_row_keys()
    protected_box_keys = _protected_customs_shipment_box_keys()
    writable_rows = [
        row
        for row in data.customs_rows
        if not _is_protected_customs_row(row, protected_ids, protected_keys, protected_box_keys)
    ]
    rows = [mysql_row_values(row) for row in writable_rows]
    protected_skipped_rows = len(data.customs_rows) - len(writable_rows)
    stale_deleted_by_source: dict[str, int] = {}
    retention_deleted_rows = 0

    connection = None
    tunnel = None
    try:
        connection, tunnel = _open_mysql_connection(config)
        with connection.cursor() as cursor:
            _validate_mysql_target(cursor, config.table, data)
            stale_deleted_by_source = _delete_stale_rows_for_current_batch(cursor, config.table, data)
            if rows:
                cursor.executemany(build_upsert_sql(config.table), rows)
            if delete_retention:
                retention_deleted_rows = _delete_rows_before_retention_cutoff(cursor, config.table, _retention_cutoff())
        connection.commit()
    except Exception:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()
        if tunnel is not None:
            tunnel.stop()
    return MySQLExportResult(
        upserted_rows=len(rows),
        stale_deleted_by_source=stale_deleted_by_source,
        retention_deleted_rows=retention_deleted_rows,
        protected_skipped_rows=protected_skipped_rows,
    )


def _protected_customs_row_ids() -> set[str]:
    configured = set(_split_env_values(os.getenv("CUSTOMS_PROTECTED_ROW_IDS", "")))
    return DEFAULT_PROTECTED_CUSTOMS_ROW_IDS | configured


def _protected_customs_row_keys() -> set[tuple[str, str]]:
    configured = set(
        _split_env_pairs(
            os.getenv("CUSTOMS_PROTECTED_SHIPMENT_SKUS", ""),
            env_name="CUSTOMS_PROTECTED_SHIPMENT_SKUS",
            pair_label="发货单号|SKU",
        )
    )
    return DEFAULT_PROTECTED_CUSTOMS_ROW_KEYS | configured


def _protected_customs_shipment_box_keys() -> set[tuple[str, str]]:
    configured = set(
        _split_env_pairs(
            os.getenv("CUSTOMS_PROTECTED_SHIPMENT_BOXES", ""),
            env_name="CUSTOMS_PROTECTED_SHIPMENT_BOXES",
            pair_label="发货单号|箱号",
        )
    )
    return DEFAULT_PROTECTED_CUSTOMS_SHIPMENT_BOXES | configured


def _is_protected_customs_row(
    row: CustomsRow,
    protected_ids: set[str],
    protected_keys: set[tuple[str, str]],
    protected_box_keys: set[tuple[str, str]],
) -> bool:
    return (
        row.id in protected_ids
        or _customs_row_key(row) in protected_keys
        or _customs_shipment_box_key(row) in protected_box_keys
    )


def _customs_row_key(row: CustomsRow) -> tuple[str, str]:
    return (str(row.shipment_no or "").strip(), str(row.sku or "").strip())


def _customs_shipment_box_key(row: CustomsRow) -> tuple[str, str]:
    return (str(row.shipment_no or "").strip(), str(row.box_no or "").strip())


def _split_env_values(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\s,;]+", str(value or "")) if part.strip()]


def _split_env_pairs(value: str, *, env_name: str, pair_label: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in _split_env_values(value):
        separator = next((candidate for candidate in ("|", ":", "=") if candidate in part), "")
        if not separator:
            raise MySQLExportError(
                f"Invalid {env_name} entry "
                f"{part!r}; use {pair_label}, separated by commas"
            )
        shipment_no, right_value = (piece.strip() for piece in part.split(separator, 1))
        if not shipment_no or not right_value:
            raise MySQLExportError(
                f"Invalid {env_name} entry "
                f"{part!r}; use {pair_label}, separated by commas"
            )
        pairs.append((shipment_no, right_value))
    return pairs


def preflight_customs_rows_mysql(
    data: CustomsWorkbookData,
    config: MySQLConfig | None = None,
) -> MySQLPreflightResult:
    config = config or MySQLConfig.from_env()
    connection = None
    tunnel = None
    try:
        connection, tunnel = _open_mysql_connection(config)
        with connection.cursor() as cursor:
            _validate_mysql_target(cursor, config.table, data)
    finally:
        if connection is not None:
            connection.close()
        if tunnel is not None:
            tunnel.stop()
    return MySQLPreflightResult(table=config.table, row_count=len(data.customs_rows), duplicate_id_count=0)


def _open_mysql_connection(config: MySQLConfig) -> tuple[Any, Any | None]:
    pymysql = _pymysql_module()

    host = config.host
    port = config.port
    tunnel = None
    if config.use_ssh_tunnel:
        tunnel = _open_ssh_tunnel(config)
        host = "127.0.0.1"
        port = int(tunnel.local_bind_port)

    try:
        connection = pymysql.connect(
            host=host,
            port=port,
            user=config.user,
            password=config.password,
            database=config.database,
            charset=config.charset,
            autocommit=False,
        )
    except Exception as exc:
        if tunnel is not None:
            tunnel.stop()
        target = f"{host}:{port}" if config.use_ssh_tunnel else f"{config.host}:{config.port}"
        raise MySQLExportError(f"Could not connect to MySQL via {target}: {exc}") from exc
    return connection, tunnel


def _open_ssh_tunnel(config: MySQLConfig) -> Any:
    tunnel_factory = _ssh_tunnel_factory()

    try:
        tunnel = tunnel_factory(
            (config.ssh_host, config.ssh_port),
            ssh_username=config.ssh_user,
            ssh_password=config.ssh_password,
            remote_bind_address=(config.host, config.port),
            local_bind_address=("127.0.0.1", 0),
        )
        tunnel.start()
    except Exception as exc:
        raise MySQLExportError(f"Could not open SSH tunnel {config.ssh_host}:{config.ssh_port} -> {config.host}:{config.port}: {exc}") from exc
    return tunnel


def _pymysql_module() -> Any:
    if PyMySQLModule is not None:
        return PyMySQLModule
    try:
        import pymysql
    except ImportError as exc:
        raise MySQLExportError("PyMySQL is not installed. Run: .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt") from exc
    return pymysql


def _ssh_tunnel_factory() -> Any:
    if SSHTunnelForwarderFactory is not None:
        return SSHTunnelForwarderFactory
    try:
        import paramiko

        if not hasattr(paramiko, "DSSKey"):
            paramiko.DSSKey = paramiko.RSAKey
        from sshtunnel import SSHTunnelForwarder
    except ImportError as exc:
        raise MySQLExportError("sshtunnel is not installed. Run: .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt") from exc
    return SSHTunnelForwarder


def mysql_row_values(row: CustomsRow) -> tuple[Any, ...]:
    return tuple(_mysql_row_value(attr, getattr(row, attr)) for attr, _ in MYSQL_COLUMNS)


def build_upsert_sql(table: str) -> str:
    columns = [column for _, column in MYSQL_COLUMNS]
    column_sql = ", ".join(_quote_identifier(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    update_sql = ", ".join(
        f"{_quote_identifier(column)}=VALUES({_quote_identifier(column)})"
        for column in columns
        if column != "id"
    )
    return f"INSERT INTO {_quote_identifier(table)} ({column_sql}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_sql}"


def build_delete_stale_sql(table: str, id_count: int) -> str:
    if id_count <= 0:
        raise MySQLExportError("Cannot build stale delete SQL without current id values")
    placeholders = ", ".join(["%s"] * id_count)
    return (
        f"DELETE FROM {_quote_identifier(table)} "
        "WHERE `confirm_shipment` = %s "
        "AND `tran_id` LIKE %s "
        f"AND `id` NOT IN ({placeholders})"
    )


def build_delete_retention_sql(table: str) -> str:
    return f"DELETE FROM {_quote_identifier(table)} WHERE `confirm_shipment` < %s"


def _delete_stale_rows_for_current_batch(cursor: Any, table: str, data: CustomsWorkbookData) -> dict[str, int]:
    grouped_ids: dict[tuple[str, str], set[str]] = {}
    for row in data.customs_rows:
        source = _cleanup_source(row)
        shipment_day = _mysql_shipment_day_value(row.shipment_day)
        if not source or not shipment_day or not row.id:
            continue
        grouped_ids.setdefault((source, shipment_day), set()).add(row.id)

    deleted_by_source: dict[str, int] = {}
    for (source, shipment_day), ids in sorted(grouped_ids.items()):
        if not ids:
            continue
        prefix = _source_tran_id_prefix(source)
        if not prefix:
            continue
        sorted_ids = sorted(ids)
        cursor.execute(build_delete_stale_sql(table, len(sorted_ids)), [shipment_day, prefix, *sorted_ids])
        deleted_by_source[source] = deleted_by_source.get(source, 0) + int(getattr(cursor, "rowcount", 0) or 0)
    return deleted_by_source


def _delete_rows_before_retention_cutoff(cursor: Any, table: str, cutoff: date) -> int:
    cursor.execute(build_delete_retention_sql(table), [cutoff.isoformat()])
    return int(getattr(cursor, "rowcount", 0) or 0)


def _retention_cutoff(today: date | None = None) -> date:
    return (today or _today()) - timedelta(days=6)


def _today() -> date:
    return date.today()


def _cleanup_source(row: CustomsRow) -> str:
    source = str(row.source or "").strip().lower()
    if source in {"amazon", "overseas"}:
        return source
    shipment_no = str(row.shipment_no or "").upper()
    if shipment_no.startswith("OWS"):
        return "overseas"
    if shipment_no.startswith("SP"):
        return "amazon"
    return ""


def _source_tran_id_prefix(source: str) -> str:
    if source == "amazon":
        return "SP%"
    if source == "overseas":
        return "OWS%"
    return ""


def validate_table_columns(table_columns: Iterable[str]) -> None:
    actual = {column.lower() for column in table_columns}
    required = [column for _, column in MYSQL_COLUMNS]
    missing = [column for column in required if column.lower() not in actual]
    if missing:
        raise MySQLExportError("customs_bill_parcels is missing columns: " + ", ".join(missing))


def validate_unique_id_index(index_rows: Iterable[Any]) -> None:
    indexes: dict[str, dict[str, Any]] = {}
    for row in index_rows:
        key_name = _row_value(row, "Key_name", 2)
        if not key_name:
            continue
        index = indexes.setdefault(str(key_name), {"non_unique": _row_value(row, "Non_unique", 1), "columns": []})
        index["columns"].append((int(_row_value(row, "Seq_in_index", 3) or 0), str(_row_value(row, "Column_name", 4) or "")))

    for index in indexes.values():
        if str(index["non_unique"]) not in {"0", "False", "false"}:
            continue
        columns = [column for _, column in sorted(index["columns"])]
        if columns == ["id"]:
            return
    raise MySQLExportError("customs_bill_parcels requires a PRIMARY KEY or UNIQUE index on id")


def validate_unique_row_ids(data: CustomsWorkbookData) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in data.customs_rows:
        if row.id in seen:
            duplicates.add(row.id)
        seen.add(row.id)
    if duplicates:
        preview = ", ".join(sorted(duplicates)[:10])
        raise MySQLExportError(f"Current batch contains duplicate id values: {preview}")


def validate_string_lengths(data: CustomsWorkbookData, column_lengths: dict[str, int]) -> None:
    issues: list[str] = []
    for row_index, row in enumerate(data.customs_rows, start=1):
        for attr, column in MYSQL_COLUMNS:
            max_length = column_lengths.get(column.lower())
            if not max_length:
                continue
            value = _mysql_row_value(attr, getattr(row, attr))
            if value is None:
                continue
            text = str(value)
            if len(text) <= max_length:
                continue
            preview = text[:120].replace("\n", "\\n")
            issues.append(
                f"row {row_index}: column={column}, max={max_length}, length={len(text)}, "
                f"id={row.id}, shipment_no={row.shipment_no}, sku={row.sku}, box_no_length={len(str(row.box_no or ''))}, "
                f"value_preview={preview}"
            )
            if len(issues) >= 10:
                break
        if len(issues) >= 10:
            break
    if issues:
        raise MySQLExportError("Current batch contains values longer than MySQL column limits:\n" + "\n".join(issues))


def _fetch_table_columns(cursor: Any, table: str) -> set[str]:
    cursor.execute(f"SHOW COLUMNS FROM {_quote_identifier(table)}")
    columns: set[str] = set()
    for row in cursor.fetchall():
        if isinstance(row, dict):
            field = row.get("Field")
        else:
            field = row[0] if row else None
        if field:
            columns.add(str(field))
    return columns


def _fetch_table_column_lengths(cursor: Any, table: str) -> dict[str, int]:
    cursor.execute(f"SHOW COLUMNS FROM {_quote_identifier(table)}")
    lengths: dict[str, int] = {}
    for row in cursor.fetchall():
        field = _row_value(row, "Field", 0)
        column_type = str(_row_value(row, "Type", 1) or "")
        if not field:
            continue
        match = re.match(r"^(?:var)?char\((\d+)\)", column_type, flags=re.IGNORECASE)
        if match:
            lengths[str(field).lower()] = int(match.group(1))
    return lengths


def _fetch_table_indexes(cursor: Any, table: str) -> list[Any]:
    cursor.execute(f"SHOW INDEX FROM {_quote_identifier(table)}")
    return list(cursor.fetchall())


def _validate_mysql_target(cursor: Any, table: str, data: CustomsWorkbookData) -> None:
    validate_unique_row_ids(data)
    validate_table_columns(_fetch_table_columns(cursor, table))
    validate_string_lengths(data, _fetch_table_column_lengths(cursor, table))
    validate_unique_id_index(_fetch_table_indexes(cursor, table))


def _row_value(row: Any, key: str, index: int) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[index] if len(row) > index else None


def _mysql_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if value is None:
        return ""
    return value


def _mysql_row_value(attr: str, value: Any) -> Any:
    if attr == "shipment_date":
        return _mysql_shipment_month_value(value)
    if attr == "shipment_day":
        return _mysql_shipment_day_value(value)
    return _mysql_value(value)


def _mysql_shipment_month_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) == 7 and text[4:5] == "-":
        return text
    elif len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        text = text[:7]
    else:
        return None
    try:
        datetime.strptime(text, "%Y-%m")
    except ValueError:
        return None
    return text


def _mysql_shipment_day_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        text = text[:10]
    else:
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def _quote_identifier(value: str) -> str:
    if not value or "`" in value:
        raise MySQLExportError(f"Invalid MySQL identifier: {value!r}")
    return f"`{value}`"


def _env_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
