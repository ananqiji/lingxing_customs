from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.common.cache import JsonCache
from src.shipment.build_rows import build_customs_workbook_data
from src.shipment.export_excel import export_customs_workbook
from src.shipment.export_mysql import export_customs_rows_to_mysql, preflight_customs_rows_mysql
from src.shipment.fetcher import LingxingApiDataSource
from src.shipment.issue_report import report_shipment_customs_data_issues
from src.shipment.models import CustomsWorkbookData, RawCustomsData
from src.shipment.mapping_recovery import MappingRecoveryStore
from src.shipment.overseas_fetcher import OverseasWarehouseApiDataSource, write_overseas_field_debug
from src.shipment.product_master import apply_product_master_data
from src.shipment.sample_data import SampleDataSource
from src.shipment.shop_mapping import (
    apply_shop_mapping, invalid_mapping_shipments,
    load_shop_mapping_for_current_slot, missing_mapping_value,
)
from src.shipment.warehouse_region import apply_warehouse_region_mapping


def run_shipment_job(args: Any) -> None:
    if args.clear_cache:
        JsonCache().clear()
        print("Lingxing master-data cache cleared.")
    recovery = MappingRecoveryStore() if args.write_db and not args.use_sample_data else None
    source_names = _shipment_source_names(args)
    pending = recovery.pending(_today(), source_names) if recovery else []
    pending_keys = {(entry["source"], entry["shipment_no"]) for entry in pending}
    shop_mapping = _load_shop_mapping(force_refresh=bool(pending))
    data_sources = _shipment_data_sources(args)
    shipment_times = _shipment_times(args)
    raw_data = _load_raw_data_for_dates(data_sources, shipment_times)
    normal_keys = {(item.source, item.shipment_no) for item in raw_data.shipment_items}
    cleanup_shipments = _load_pending_shipments(raw_data, pending, data_sources, source_names, shipment_times)
    _apply_product_master_data(raw_data)
    _apply_shop_mapping(raw_data, shop_mapping.mapping)
    if invalid_mapping_shipments(raw_data):
        shop_mapping = _load_shop_mapping(force_refresh=True)
        _apply_shop_mapping(raw_data, shop_mapping.mapping)
    skipped = invalid_mapping_shipments(raw_data)
    if recovery:
        recovery.record(raw_data, skipped)
    present = {(item.source, item.shipment_no) for item in raw_data.shipment_items}
    # Pending shipments absent from the API response must also survive same-day cleanup.
    excluded = skipped | (pending_keys - present)
    _apply_warehouse_region_mapping(raw_data)
    write_overseas_field_debug(raw_data)
    workbook_data = build_customs_workbook_data(raw_data)
    # The issue API replaces whole source/day snapshots. A partial historical
    # recovery must not mark unrelated issues on that day as resolved.
    normal_numbers = {number for _, number in normal_keys}
    report_shipment_customs_data_issues(
        RawCustomsData(
            shipment_items=[item for item in raw_data.shipment_items if (item.source, item.shipment_no) in normal_keys],
            sku_infos=raw_data.sku_infos,
            purchase_batches=[batch for batch in raw_data.purchase_batches if batch.shipment_no in normal_numbers],
            metadata=raw_data.metadata,
        ),
        CustomsWorkbookData(
            [row for row in workbook_data.customs_rows if (row.source, row.shipment_no) in normal_keys],
            [issue for issue in workbook_data.issue_rows if issue.shipment_no in normal_numbers],
            [row for row in workbook_data.purchase_split_rows if row.shipment_no in normal_numbers],
        ),
        shipment_times=shipment_times,
        source_names=_shipment_source_names(args),
    )
    output_path = _export_with_available_path(workbook_data, Path(args.output)) if args.output else None
    writable_data = CustomsWorkbookData(
        [row for row in workbook_data.customs_rows if (row.source, row.shipment_no) not in excluded],
        workbook_data.issue_rows, workbook_data.purchase_split_rows,
    )
    recovered = 0
    if args.db_preflight:
        result = preflight_customs_rows_mysql(writable_data)
        print("MySQL preflight: OK")
        print(f"MySQL target table: {result.table}")
        print(f"MySQL rows ready: {result.row_count}")
        print(f"MySQL duplicate ids in current batch: {result.duplicate_id_count}")
    if args.write_db:
        result = preflight_customs_rows_mysql(writable_data)
        print("MySQL preflight: OK")
        print(f"MySQL target table: {result.table}")
        print(f"MySQL rows ready: {result.row_count}")
        retention_cleanup = _retention_cleanup_for_this_run(args)
        db_result = export_customs_rows_to_mysql(
            writable_data, delete_retention=retention_cleanup,
            excluded_shipments=excluded, cleanup_shipments=cleanup_shipments,
        )
        if recovery:
            completed = {(row.source, row.shipment_no) for row in writable_data.customs_rows}
            recovery.complete(completed)
            recovered = len(completed & pending_keys)
        if retention_cleanup:
            _mark_retention_cleanup_done()
        for source, deleted_rows in sorted(db_result.stale_deleted_by_source.items()):
            print(f"MySQL stale {source} rows deleted: {deleted_rows}")
        print(f"MySQL retention rows deleted: {db_result.retention_deleted_rows}")
        protected_skipped_rows = getattr(db_result, "protected_skipped_rows", 0)
        if protected_skipped_rows:
            print(f"MySQL protected rows skipped: {protected_skipped_rows}")
        print(f"MySQL rows upserted: {db_result.upserted_rows}")

    if output_path:
        print(f"Generated customs workbook: {output_path.resolve()}")
    else:
        print("Generated customs workbook: skipped (--output not provided)")
    print("Shipment dates: " + ", ".join(shipment_times))
    print("Shipment sources: " + ", ".join(_shipment_source_names(args)))
    print(f"Customs rows: {len(workbook_data.customs_rows)}")
    print(f"Issue rows: {len(workbook_data.issue_rows)}")
    print(f"Purchase split rows: {len(workbook_data.purchase_split_rows)}")
    pending_purchase_entities = sum(missing_mapping_value(row.purchase_entity) for row in workbook_data.customs_rows)
    pending_customers = sum(missing_mapping_value(row.final_customer) for row in workbook_data.customs_rows)
    purchase_entities = sorted({row.purchase_entity for row in workbook_data.customs_rows if not missing_mapping_value(row.purchase_entity)})
    print(f"Pending purchase entity rows: {pending_purchase_entities}")
    print(f"Pending final customer rows: {pending_customers}")
    print(f"Mapping shipments skipped: {len(excluded)}; recovered: {recovered}")
    if purchase_entities:
        print("Purchase entities: " + ", ".join(purchase_entities[:10]))
    if not workbook_data.purchase_split_rows:
        print("Warning: no purchase split rows were generated; shipment detail did not provide purchase_items/purchase_sn.")
    _print_api_performance_summary()


def _shipment_times(args: Any) -> list[str]:
    if getattr(args, "shipment_time_provided", False):
        return [args.shipment_time]
    today = _today()
    return [(today - timedelta(days=1)).isoformat(), today.isoformat()]


def _retention_cleanup_for_this_run(args: Any) -> bool:
    if getattr(args, "shipment_time_provided", False):
        return False
    if not _is_retention_cleanup_window(_now()):
        return False
    cache = JsonCache()
    key = _today().isoformat()
    return cache.get("shipment_retention_cleanup", key, ttl_days=30) is None


def _mark_retention_cleanup_done() -> None:
    key = _today().isoformat()
    JsonCache().set("shipment_retention_cleanup", key, {"date": key, "done_at": _now().isoformat(timespec="seconds")})


def _is_retention_cleanup_window(now: datetime) -> bool:
    return now.hour == 7 and now.minute < 5


def _now() -> datetime:
    return datetime.now()


def _today() -> date:
    return date.today()


def _shipment_data_sources(args: Any) -> list[Any]:
    if args.use_sample_data:
        return [SampleDataSource()]
    if args.shipment_source == "overseas":
        return [OverseasWarehouseApiDataSource(refresh_cache=args.refresh_cache)]
    if args.shipment_source == "all":
        return [
            LingxingApiDataSource(refresh_cache=args.refresh_cache),
            OverseasWarehouseApiDataSource(refresh_cache=args.refresh_cache),
        ]
    return [LingxingApiDataSource(refresh_cache=args.refresh_cache)]


def _shipment_source_names(args: Any) -> list[str]:
    if args.use_sample_data:
        return ["sample"]
    if args.shipment_source == "all":
        return ["amazon", "overseas"]
    return [args.shipment_source]


def _load_raw_data_for_dates(data_sources: list[Any], shipment_times: list[str]) -> RawCustomsData:
    combined = RawCustomsData()
    for shipment_time in shipment_times:
        if len(data_sources) > 1:
            raw_data_list = _load_raw_data_for_date_parallel(data_sources, shipment_time)
        else:
            raw_data_list = [data_sources[0].load(shipment_time=shipment_time)]
        for raw_data in raw_data_list:
            combined.shipment_items.extend(raw_data.shipment_items)
            combined.purchase_batches.extend(raw_data.purchase_batches)
            combined.sku_infos.update(raw_data.sku_infos)
            combined.metadata.update(raw_data.metadata)
    return combined


def _load_pending_shipments(
    raw_data: RawCustomsData, pending: list[dict], data_sources: list[Any],
    source_names: list[str], shipment_times: list[str],
) -> dict[tuple[str, str], set[str]]:
    scopes: dict[tuple[str, str], set[str]] = {}
    for entry in pending:
        if entry["shipment_day"] not in shipment_times:
            scopes.setdefault((entry["source"], entry["shipment_day"]), set()).add(entry["shipment_no"])
    sources = dict(zip(source_names, data_sources))
    for (source, day), numbers in sorted(scopes.items()):
        try:
            recovered = sources[source].load(shipment_time=day)
        except Exception as exc:
            print(f"Warning: pending mapping recovery failed for {source} {day}: {exc}")
            continue
        selected = [item for item in recovered.shipment_items if item.source == source and item.shipment_no in numbers]
        if any(item.shipment_date[:10] != day for item in selected):
            print(f"Warning: pending mapping recovery returned a different shipment day for {source} {day}; preserving pending entries")
            continue
        raw_data.shipment_items.extend(selected)
        raw_data.purchase_batches.extend(batch for batch in recovered.purchase_batches if batch.shipment_no in numbers)
        raw_data.sku_infos.update(recovered.sku_infos)
        raw_data.metadata.update(recovered.metadata)
    return scopes


def _load_raw_data_for_date_parallel(data_sources: list[Any], shipment_time: str) -> list[RawCustomsData]:
    results: list[RawCustomsData | None] = [None] * len(data_sources)
    with ThreadPoolExecutor(max_workers=len(data_sources)) as executor:
        futures = {}
        for index, data_source in enumerate(data_sources):
            source_name = _data_source_name(data_source)
            print(f"Shipment source {source_name} started for {shipment_time}")
            started_at = time.perf_counter()
            future = executor.submit(data_source.load, shipment_time=shipment_time)
            futures[future] = (index, source_name, started_at)

        for future in as_completed(futures):
            index, source_name, started_at = futures[future]
            results[index] = future.result()
            print(f"Shipment source {source_name} finished for {shipment_time}: {time.perf_counter() - started_at:.2f}s")
    return [result for result in results if result is not None]


def _data_source_name(data_source: Any) -> str:
    if isinstance(data_source, OverseasWarehouseApiDataSource):
        return "overseas"
    if isinstance(data_source, LingxingApiDataSource):
        return "amazon"
    if isinstance(data_source, SampleDataSource):
        return "sample"
    return data_source.__class__.__name__


def _apply_product_master_data(raw_data: RawCustomsData) -> None:
    try:
        loaded_rows, applied_rows = apply_product_master_data(raw_data)
    except Exception as exc:
        print(f"Warning: failed to load product master data from customs_product: {exc}")
        return
    print(f"Product master rows loaded from MySQL: {loaded_rows}")
    print(f"Product master rows applied to shipment SKU info: {applied_rows}")


def _load_shop_mapping(*, force_refresh: bool = False):
    try:
        result = load_shop_mapping_for_current_slot(force_refresh=force_refresh)
    except Exception as exc:
        print(f"Warning: failed to load shop mapping: {exc}")
        from src.shipment.shop_mapping import ShopMappingLoadResult

        return ShopMappingLoadResult(mapping={}, loaded_rows=0, slot_key="", source="empty", warning=str(exc))
    if result.warning:
        print(f"Warning: {result.warning}")
    print(f"Shop mapping slot: {result.slot_key}")
    print(f"Shop mapping source: {result.source}")
    print(f"Shop mapping rows loaded: {result.loaded_rows}")
    return result


def _apply_shop_mapping(raw_data: RawCustomsData, mapping) -> None:
    loaded_rows, applied_rows = apply_shop_mapping(raw_data, mapping)
    print(f"Shop mapping rows available: {loaded_rows}")
    print(f"Shop mapping rows applied to shipment items: {applied_rows}")


def _apply_warehouse_region_mapping(raw_data: RawCustomsData) -> None:
    try:
        loaded_rows, applied_rows, warning = apply_warehouse_region_mapping(raw_data)
    except Exception as exc:
        print(f"Warning: failed to load warehouse region mapping: {exc}")
        return
    if warning:
        print(f"Warning: {warning}")
    print(f"Warehouse region rows loaded: {loaded_rows}")
    print(f"Warehouse region rows applied to shipment items: {applied_rows}")


def _export_with_available_path(workbook_data, output_path: Path) -> Path:
    try:
        export_customs_workbook(workbook_data, output_path)
        return output_path
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        fallback_path = output_path.with_name(f"{output_path.stem}-{timestamp}{output_path.suffix}")
        export_customs_workbook(workbook_data, fallback_path)
        print(f"Output file is in use, wrote a new file instead: {fallback_path}")
        return fallback_path


def _print_api_performance_summary() -> None:
    debug_dir = os.getenv("LINGXING_DEBUG_DIR", "")
    if not debug_dir:
        return
    summary_path = Path(debug_dir) / "performance_summary.json"
    if not summary_path.exists():
        return
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    print("Lingxing API performance summary:")
    for endpoint, item in sorted(summary.items(), key=lambda pair: float(pair[1].get("total_seconds", 0)), reverse=True)[:10]:
        print(
            f"  {endpoint}: calls={item.get('count', 0)}, "
            f"seconds={float(item.get('total_seconds', 0)):.2f}, "
            f"errors={item.get('error_count', 0)}"
        )
