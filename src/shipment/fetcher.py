from __future__ import annotations

import json
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.common.cache import JsonCache
from src.common.lingxing_client import LingxingClient, LingxingClientError
from src.shipment.build_rows import _box_count_from_box_no, _display_box_no
from src.shipment.models import PurchaseBatch, RawCustomsData, ShipmentItem, SkuInfo, decimal_or_zero

from .base import CustomsDataSource


_AMAZON_CARTON_CODE_RE = re.compile(r"FBA[A-Z0-9]+U\d{6}", re.IGNORECASE)
_BOX_NO_KEYS = ("box_no", "boxNo", "carton_no", "case_no", "box_codes", "box_range")
_BOX_COUNT_KEYS = ("box_num", "box_count", "boxCount", "carton_count")


class LingxingApiDataSource(CustomsDataSource):
    def __init__(self, client: LingxingClient | None = None, refresh_cache: bool = False) -> None:
        self.client = client or LingxingClient()
        self.cache = JsonCache(refresh=refresh_cache)
        self.fba_shipment_list_endpoint = os.getenv(
            "LINGXING_FBA_SHIPMENT_LIST_ENDPOINT",
            "/erp/sc/routing/storage/shipment/getInboundShipmentList",
        )
        self.fba_shipment_detail_endpoint = os.getenv(
            "LINGXING_FBA_SHIPMENT_DETAIL_ENDPOINT",
            "/erp/sc/routing/storage/shipment/getInboundShipmentListMwsDetail",
        )
        self.sku_detail_endpoint = os.getenv("LINGXING_SKU_DETAIL_ENDPOINT", "/erp/sc/routing/data/local_inventory/productInfo")
        self.purchase_batch_endpoint = os.getenv("LINGXING_PURCHASE_BATCH_ENDPOINT", "")
        self.purchase_order_list_endpoint = os.getenv(
            "LINGXING_PURCHASE_ORDER_LIST_ENDPOINT",
            "/erp/sc/routing/data/local_inventory/purchaseOrderList",
        )
        self.purchaser_list_endpoint = os.getenv("LINGXING_PURCHASER_LIST_ENDPOINT", "/erp/sc/routing/data/purchaser/lists")
        self.supplier_list_endpoint = os.getenv("LINGXING_SUPPLIER_LIST_ENDPOINT", "/erp/sc/data/local_inventory/supplier")
        configured_status = os.getenv("LINGXING_SHIPMENT_STATUS", "1").strip()
        if configured_status not in ("", "1", "已发货", "shipped"):
            raise ValueError("LINGXING_SHIPMENT_STATUS must be 1 (已发货)")
        self.shipment_status = 1
        self.shipment_status_field = os.getenv("LINGXING_SHIPMENT_STATUS_FIELD", "status")
        self.shipment_time_type_field = os.getenv("LINGXING_SHIPMENT_TIME_TYPE_FIELD", "time_type")
        self.shipment_time_type = os.getenv("LINGXING_SHIPMENT_TIME_TYPE", "0")
        self.shipment_warehouse_field = os.getenv("LINGXING_SHIPMENT_WAREHOUSE_FIELD", "wname")
        self.shipment_warehouse = os.getenv("LINGXING_SHIPMENT_WAREHOUSE", "亚马逊义乌仓库")

    def load(self, shipment_time: str | None = None) -> RawCustomsData:
        self._validate_config()
        shipment_headers = self._fetch_shipment_headers(shipment_time)
        shipment_items: list[ShipmentItem] = []
        detail_purchase_batches: list[PurchaseBatch] = []
        sku_codes: set[str] = set()

        for header in shipment_headers:
            details = self._fetch_shipment_detail(header)
            for item_payload in details:
                item = _map_shipment_item(header, item_payload)
                shipment_items.append(item)
                detail_purchase_batches.extend(_map_purchase_items(item, item_payload))
                if item.sku:
                    sku_codes.add(item.sku)

        sku_infos = self._fetch_sku_infos(sku_codes)
        purchase_batches = self._enrich_purchase_batches(detail_purchase_batches)
        if not purchase_batches:
            purchase_batches = self._fetch_purchase_batches(shipment_items)
        return RawCustomsData(shipment_items=shipment_items, sku_infos=sku_infos, purchase_batches=purchase_batches)

    def _fetch_shipment_headers(self, shipment_time: str | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {}
        if shipment_time:
            payload["start_date"] = shipment_time
            payload["end_date"] = shipment_time
        if self.shipment_time_type != "":
            payload[self.shipment_time_type_field] = _coerce_int_string(self.shipment_time_type)
        if self.shipment_status:
            payload[self.shipment_status_field] = self.shipment_status

        rows, page_summaries = self._fetch_shipment_list_rows(payload, shipment_time)

        filtered_rows = [
            row
            for row in rows
            if _is_shipped(row, self.shipment_status)
            and _matches_shipment_time(row, shipment_time)
            and _matches_warehouse(row, self.shipment_warehouse_field, self.shipment_warehouse)
        ]
        _dump_header_keys(rows, filtered_rows, self.shipment_warehouse_field, self.shipment_warehouse, page_summaries)
        return filtered_rows

    def _fetch_shipment_list_rows(self, payload: dict[str, Any], shipment_time: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return self._fetch_offset_page_rows(payload, shipment_time)

    def _fetch_offset_page_rows(self, payload: dict[str, Any], shipment_time: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        seen_signatures: set[tuple[str, ...]] = set()
        seen_shipment_sns: set[str] = set()
        length = min(_client_page_size(self.client), 100)
        offset = 0
        while True:
            page_payload = dict(payload)
            page_payload["offset"] = offset
            page_payload["length"] = length
            data = self.client.post(self.fba_shipment_list_endpoint, page_payload)
            items = _extract_rows(data)
            summary = _page_summary("offset", page_payload, items)
            summaries.append(summary)
            if not items:
                return rows, summaries

            signature = tuple(_shipment_identity(item) for item in items)
            if signature in seen_signatures:
                raise LingxingClientError("Shipment list repeated a page; refusing to sync an incomplete batch")
            seen_signatures.add(signature)

            new_items = [item for item in items if _shipment_identity(item) not in seen_shipment_sns]
            if not new_items:
                raise LingxingClientError("Shipment list returned no new shipments; refusing to sync an incomplete batch")
            rows.extend(new_items)
            seen_shipment_sns.update(_shipment_identity(item) for item in new_items)

            # The service may cap page length. Advance by rows actually consumed.
            # Do not stop on pick_time: it can precede the actual shipping date.
            offset += len(items)
            if offset > 10000:
                raise LingxingClientError("Shipment list exceeded 10000 rows; narrow the date range before syncing")

    def _fetch_shipment_detail(self, header: dict[str, Any]) -> list[dict[str, Any]]:
        candidate_values = _detail_candidate_values(header)
        if not candidate_values:
            raise LingxingClientError("Shipment header has no identifier; refusing to sync an incomplete batch")

        last_error: Exception | None = None
        for request_body in _detail_request_bodies(candidate_values):
            try:
                data = self.client.post(self.fba_shipment_detail_endpoint, request_body)
            except LingxingClientError as exc:
                last_error = exc
                if _is_parameter_error(exc):
                    continue
                raise

            detail_data = data.get("data", data)
            items = _find_item_rows(detail_data)
            if items:
                expanded_items: list[dict[str, Any]] = []
                for item in items:
                    expanded_items.extend(_expand_item_by_boxes(item))
                return [dict(header, **item) for item in expanded_items]
            raise LingxingClientError(
                f"Shipment detail returned no items for {candidate_values[0]}; refusing to sync an incomplete batch"
            )

        if last_error is not None:
            raise LingxingClientError(f"Shipment detail parameter error after trying common parameter names. Last error: {last_error}") from last_error
        raise LingxingClientError("Shipment detail could not be loaded; refusing to sync an incomplete batch")

    def _fetch_sku_infos(self, sku_codes: set[str]) -> dict[str, SkuInfo]:
        sku_infos: dict[str, SkuInfo] = {}
        for sku in sorted(sku_codes):
            cached_payload = self.cache.get("sku_info", sku, ttl_days=30)
            if isinstance(cached_payload, dict):
                sku_info = _map_sku_info(sku, cached_payload)
                sku_infos[sku_info.sku] = sku_info
                continue
            try:
                data = self.client.post(self.sku_detail_endpoint, {"sku": sku})
            except LingxingClientError:
                sku_info = SkuInfo(sku=sku)
            else:
                payload = data.get("data", data)
                if _is_valid_payload(payload):
                    self.cache.set("sku_info", sku, payload)
                sku_info = _map_sku_info(sku, payload)
            sku_infos[sku_info.sku] = sku_info
        return sku_infos

    def _fetch_purchase_batches(self, shipment_items: list[ShipmentItem]) -> list[PurchaseBatch]:
        if not self.purchase_batch_endpoint:
            return []
        batches: list[PurchaseBatch] = []
        for item in shipment_items:
            data = self.client.post(
                self.purchase_batch_endpoint,
                {"shipment_no": item.shipment_no, "sku": item.sku, "box_no": item.box_no},
            )
            payload = data.get("data", data)
            rows = payload if isinstance(payload, list) else payload.get("list", [])
            for row in rows:
                if isinstance(row, dict):
                    batches.append(_map_purchase_batch(item, row))
        return batches

    def _enrich_purchase_batches(self, batches: list[PurchaseBatch]) -> list[PurchaseBatch]:
        if not batches:
            return []

        purchase_orders = self._fetch_purchase_orders({batch.purchase_sn or batch.purchase_order_no for batch in batches})
        purchaser_names = self._fetch_purchaser_names()
        purchaser_names.update(_purchaser_names_from_env())
        supplier_infos = self._fetch_supplier_infos()

        enriched: list[PurchaseBatch] = []
        for batch in batches:
            purchase_sn = batch.purchase_sn or batch.purchase_order_no
            purchase_order = purchase_orders.get(purchase_sn, {})
            purchaser_id = _first(purchase_order, {}, "purchaser_id", "purchaserId", "buyer_id", "buyerId")
            supplier_key = str(_first(purchase_order, {}, "supplier_name", "supplier", "supplierName") or batch.supplier)
            supplier_info = supplier_infos.get(supplier_key, {})
            supplier = str(supplier_info.get("account_name") or supplier_key)
            purchase_entity = str(
                _first(
                    purchase_order,
                    {},
                    "purchaser_name",
                    "purchaserName",
                    "purchase_entity",
                    "buyer_name",
                    "buyerName",
                    "subject_name",
                    "company_name",
                )
                or purchaser_names.get(str(purchaser_id), "")
                or batch.purchase_entity
            )
            domestic_source = str(supplier_info.get("url") or batch.domestic_source)
            enriched.append(
                PurchaseBatch(
                    shipment_no=batch.shipment_no,
                    sku=batch.sku,
                    box_no=batch.box_no,
                    quantity=batch.quantity,
                    purchase_entity=purchase_entity,
                    supplier=supplier,
                    domestic_source=domestic_source,
                    purchase_order_no=str(_first(purchase_order, {}, "purchase_sn", "purchase_order_no", "po_no") or purchase_sn),
                    purchase_sn=purchase_sn,
                    batch_no=batch.batch_no,
                    purchase_unit_price=batch.purchase_unit_price,
                    quantity_missing=batch.quantity_missing,
                )
            )
        _dump_purchase_enrichment_summary(batches, enriched, purchase_orders, purchaser_names, self.cache)
        return enriched

    def _fetch_purchase_orders(self, purchase_sns: set[str]) -> dict[str, dict[str, Any]]:
        orders: dict[str, dict[str, Any]] = {}
        if not self.purchase_order_list_endpoint:
            return orders
        order_keys = ("purchase_sn", "purchase_order_no", "po_no", "order_sn", "custom_order_sn", "alibaba_order_sn")
        pending_purchase_sns = sorted(sn for sn in purchase_sns if sn)
        for purchase_sn in pending_purchase_sns:
            cached_order = self.cache.get("purchase_order", purchase_sn, ttl_days=30)
            fallback_cached_order = None
            if isinstance(cached_order, dict) and _purchase_order_matches(cached_order, order_keys, purchase_sn):
                if not _purchase_order_has_placeholder_supplier(cached_order):
                    orders[purchase_sn] = cached_order
                    continue
                fallback_cached_order = cached_order
            if purchase_sn in orders:
                continue
            for request_body in _purchase_order_request_bodies(purchase_sn):
                try:
                    data = self.client.post(self.purchase_order_list_endpoint, request_body)
                except LingxingClientError as exc:
                    if _is_permission_or_whitelist_error(exc):
                        if fallback_cached_order is not None:
                            orders[purchase_sn] = fallback_cached_order
                        return orders
                    continue
                rows = _extract_rows(data)
                matched = _first_matching_any(rows, order_keys, purchase_sn)
                if matched:
                    orders[purchase_sn] = matched
                    self.cache.set("purchase_order", purchase_sn, matched)
                    break
                if purchase_sn in orders:
                    self.cache.set("purchase_order", purchase_sn, orders[purchase_sn])
                    break
            if purchase_sn not in orders and fallback_cached_order is not None:
                orders[purchase_sn] = fallback_cached_order
        return orders

    def _fetch_purchaser_names(self) -> dict[str, str]:
        if not self.purchaser_list_endpoint:
            return {}
        cached = self.cache.get("purchaser_names", "all", ttl_days=1)
        if isinstance(cached, dict):
            return {str(key): str(value) for key, value in cached.items()}
        try:
            rows = self._fetch_offset_rows(self.purchaser_list_endpoint)
        except LingxingClientError:
            return {}
        names: dict[str, str] = {}
        for row in rows:
            purchaser_id = _first(row, {}, "id", "purchaser_id", "purchaserId")
            name = _first(row, {}, "name", "purchaser_name", "purchaserName")
            if purchaser_id not in (None, "") and name not in (None, ""):
                names[str(purchaser_id)] = str(name)
        self.cache.set("purchaser_names", "all", names)
        return names

    def _fetch_supplier_infos(self) -> dict[str, dict[str, str]]:
        if not self.supplier_list_endpoint:
            return {}
        try:
            rows = self._fetch_offset_rows(self.supplier_list_endpoint)
        except LingxingClientError:
            return {}
        infos: dict[str, dict[str, str]] = {}
        for row in rows:
            account_name = _select_supplier_account_name(
                _first(row, {}, "account_name", "accountName", "account_names", "accountNames")
            )
            source = _first(row, {}, "url", "supplier_url", "website")
            supplier_info = {"account_name": account_name, "url": str(source or "")}
            for name in _supplier_match_names(row, account_name):
                infos[name] = supplier_info
        return infos

    def _fetch_offset_rows(self, endpoint: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        length = self.client.config.page_size if hasattr(self.client, "config") else 100
        while True:
            data = self.client.post(endpoint, {"offset": offset, "length": length})
            items = _extract_rows(data)
            rows.extend(items)
            if len(items) < length:
                return rows
            offset += length

    def _validate_config(self) -> None:
        missing = []
        if not self.fba_shipment_list_endpoint:
            missing.append("LINGXING_FBA_SHIPMENT_LIST_ENDPOINT")
        if not self.fba_shipment_detail_endpoint:
            missing.append("LINGXING_FBA_SHIPMENT_DETAIL_ENDPOINT")
        if not self.sku_detail_endpoint:
            missing.append("LINGXING_SKU_DETAIL_ENDPOINT")
        if missing:
            raise RuntimeError("Real Lingxing API endpoints are not configured: " + ", ".join(missing))


def _map_shipment_item(header: dict[str, Any], payload: dict[str, Any]) -> ShipmentItem:
    box_info = _box_info_for_item(payload)
    box_no = str(_first(payload, box_info, *_BOX_NO_KEYS) or "")
    return ShipmentItem(
        shipment_date=_date_text(
            _first(header, payload, "pick_time", "shipment_time_second", "shipment_time", "shipment_date", "shipping_date", "delivery_date", "shipDate", "shipped_at")
        ),
        shipment_no=str(_first(payload, header, "shipment_sn", "shipmentSn", "shipment_no", "shipmentNo", "shipOrderNo", "shipment_id", "id") or ""),
        sku=str(_first(payload, header, "sku", "seller_sku", "local_sku", "msku") or ""),
        quantity=decimal_or_zero(_first(payload, header, "quantity_shipped", "quantity", "qty", "ship_qty", "num")),
        seller_name=str(_first(payload, header, "sname", "seller_name", "sellerName", "shop_name", "shopName", "store_name", "storeName") or ""),
        dest_country=str(_first(payload, header, "nation") or ""),
        product_name=str(_first(payload, header, "product_name", "product_name_cn", "name", "productName") or ""),
        updated_at=_updated_at_from_auxs(payload),
        box_no=box_no,
        box_count=_carton_count_for_payload(payload, box_info, box_no),
        pieces=decimal_or_zero(_first(payload, header, "pieces", "copy_count", "copies") or 1),
        logistics_provider=str(_first(payload, header, "logistics_provider", "carrier", "logistics_provider_name", "logistics_company") or ""),
        logistics_channel=str(_first(payload, header, "logistics_channel", "logistics_channel_name", "channel", "shipping_channel") or ""),
        transport_method=_transport_method(payload),
        logistics_center_code=str(_first(payload, header, "logistics_center_code", "warehouse_code", "destination_fulfillment_center_id", "fba_warehouse_code") or ""),
        volume=_box_volume_cbm(payload, box_info) or _optional_decimal(_first(payload, header, "volume", "cbm")),
        total_gross_weight=_box_gross_weight(box_info) or _optional_decimal(_first(payload, header, "total_gw", "total_gross_weight")),
        outer_box_size=_outer_box_size_from_item(payload, box_info),
        purchase_unit_price=_optional_decimal(_first(payload, header, "fba_stock_cost", "purchase_price_unit", "purchase_unit_price", "stock_cost")),
        purchase_entity=str(_first(payload, header, "purchase_entity", "buyer", "purchaser") or ""),
        supplier=str(_first(payload, header, "supplier", "supplier_name") or ""),
        domestic_source=str(_first(payload, header, "domestic_source", "source_place") or ""),
    )


def _map_sku_info(sku: str, payload: dict[str, Any]) -> SkuInfo:
    product_logistics = payload.get("product_logistics_relation")
    if isinstance(product_logistics, list) and product_logistics:
        logistics_payload = product_logistics[0]
        if isinstance(logistics_payload, dict):
            payload = dict(payload, **logistics_payload)
    declaration = payload.get("declaration")
    if not isinstance(declaration, dict):
        declaration = {}

    length = _optional_decimal(payload.get("box_length_cm") or payload.get("length") or payload.get("outer_length"))
    width = _optional_decimal(payload.get("box_width_cm") or payload.get("width") or payload.get("outer_width"))
    height = _optional_decimal(payload.get("box_height_cm") or payload.get("height") or payload.get("outer_height"))
    outer_box_size = str(payload.get("outer_box_size") or payload.get("box_size") or "")
    if not outer_box_size and length and width and height:
        outer_box_size = f"{length}*{width}*{height}cm"

    return SkuInfo(
        sku=str(payload.get("sku") or payload.get("seller_sku") or sku),
        product_name=str(payload.get("product_name") or payload.get("product_name_cn") or payload.get("name") or payload.get("productName") or ""),
        customs_name_cn=str(
            payload.get("bg_customs_export_name")
            or declaration.get("customs_export_name")
            or payload.get("customs_name_cn")
            or payload.get("declare_name_cn")
            or payload.get("chinese_name")
            or payload.get("customs_name")
            or payload.get("declare_name")
            or ""
        ),
        customs_name_en=str(
            payload.get("bg_customs_import_name")
            or declaration.get("customs_import_name")
            or payload.get("customs_name_en")
            or payload.get("declare_name_en")
            or payload.get("english_name")
            or ""
        ),
        unit=str(payload.get("unit") or payload.get("declare_unit") or declaration.get("customs_declaration_unit") or ""),
        package_type=str(payload.get("package_type") or payload.get("packing") or payload.get("package") or ""),
        gross_weight=_optional_decimal(payload.get("gross_weight") or payload.get("weight_gross") or payload.get("gross_weight_kg")),
        net_weight=_optional_decimal(payload.get("net_weight") or payload.get("weight_net") or payload.get("net_weight_kg")),
        outer_box_size=outer_box_size,
        box_length_cm=length,
        box_width_cm=width,
        box_height_cm=height,
    )


def _outer_box_size_from_item(payload: dict[str, Any], box_info: dict[str, Any]) -> str:
    length = _first_nonzero(box_info, payload, "cg_box_length", "box_length_cm", "length", "outer_length")
    width = _first_nonzero(box_info, payload, "cg_box_width", "box_width_cm", "width", "outer_width")
    height = _first_nonzero(box_info, payload, "cg_box_height", "box_height_cm", "height", "outer_height")
    if length in (None, "") or width in (None, "") or height in (None, ""):
        return str(_first(box_info, payload, "outer_box_size", "box_size") or "")
    return f"{length}*{width}*{height}"


def _box_gross_weight(box_info: dict[str, Any]) -> Decimal | None:
    weight = _optional_decimal(_first(box_info, {}, "cg_box_weight", "box_weight", "weight"))
    box_num = _carton_count_from_actual_box_no(str(_first(box_info, {}, *_BOX_NO_KEYS) or "")) or decimal_or_zero(
        _first(box_info, {}, *_BOX_COUNT_KEYS) or 1
    )
    if weight is None:
        return None
    return weight * box_num


def _box_volume_cbm(payload: dict[str, Any], box_info: dict[str, Any]) -> Decimal | None:
    length = _optional_decimal(_first_nonzero(box_info, payload, "cg_box_length", "box_length_cm", "length", "outer_length"))
    width = _optional_decimal(_first_nonzero(box_info, payload, "cg_box_width", "box_width_cm", "width", "outer_width"))
    height = _optional_decimal(_first_nonzero(box_info, payload, "cg_box_height", "box_height_cm", "height", "outer_height"))
    box_num = _carton_count_for_payload(payload, box_info)
    if length is None or width is None or height is None:
        return None
    return (box_num * length * width * height) / Decimal("1000000")


def _updated_at_from_auxs(payload: dict[str, Any]) -> str:
    auxs = payload.get("auxs")
    if isinstance(auxs, dict):
        return str(auxs.get("gmt_modified") or auxs.get("update_time") or "")
    if isinstance(auxs, list):
        for aux in auxs:
            if isinstance(aux, dict):
                value = aux.get("gmt_modified") or aux.get("update_time")
                if value not in (None, ""):
                    return str(value)
    return str(_first(payload, {}, "gmt_modified", "update_time", "last_update_time") or "")


def _transport_method(payload: dict[str, Any]) -> str:
    return _transport_type_name(payload) or _normalized_transport_method_name(_first(payload, {}, "method_name", "shipping_method", "transport_mode"))


def _transport_type_name(payload: dict[str, Any]) -> str:
    logistics_lists = _as_list(payload.get("head_logistics_list") or payload.get("headLogisticsList"))
    for logistics in logistics_lists:
        if not isinstance(logistics, dict):
            continue
        tracks = _as_list(logistics.get("track_list") or logistics.get("trackList"))
        for track in tracks:
            if not isinstance(track, dict):
                continue
            value = track.get("transport_type_name") or track.get("transportTypeName")
            if value not in (None, ""):
                return str(value)
    return ""


def _normalized_transport_method_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("海") or "海运" in text:
        return "海运"
    if "陆" in text or "卡" in text or "汽运" in text:
        return "陆运"
    if "空" in text:
        return "空运"
    if "铁" in text:
        return "铁路"
    if "快递" in text:
        return "快递"
    return text


def _map_purchase_batch(item: ShipmentItem, payload: dict[str, Any]) -> PurchaseBatch:
    purchase_sn = str(payload.get("purchase_sn") or payload.get("purchase_order_no") or payload.get("po_no") or "")
    return PurchaseBatch(
        shipment_no=str(payload.get("shipment_no") or item.shipment_no),
        sku=str(payload.get("sku") or item.sku),
        box_no=str(payload.get("box_no") or item.box_no),
        quantity=decimal_or_zero(payload.get("quantity") or payload.get("qty")),
        purchase_entity=str(payload.get("purchase_entity") or payload.get("buyer") or payload.get("purchaser") or ""),
        supplier=str(payload.get("supplier") or payload.get("supplier_name") or ""),
        domestic_source=str(payload.get("domestic_source") or payload.get("source_place") or ""),
        purchase_order_no=purchase_sn,
        purchase_sn=purchase_sn,
        batch_no=str(payload.get("batch_no") or payload.get("stock_batch_no") or ""),
        purchase_unit_price=_optional_decimal(payload.get("purchase_unit_price") or payload.get("price")),
    )


def _map_purchase_items(item: ShipmentItem, payload: dict[str, Any]) -> list[PurchaseBatch]:
    purchase_items = payload.get("purchase_items") or payload.get("purchaseItems") or payload.get("purchase_list") or []
    if not isinstance(purchase_items, list):
        return []

    rows = [
        purchase_item
        for purchase_item in purchase_items
        if isinstance(purchase_item, dict) and _first(purchase_item, {}, "purchase_sn", "purchase_order_no", "po_no") not in (None, "")
    ]
    if not rows:
        return _map_outbound_batches(item, payload)

    quantity_keys = ("quantity", "qty", "purchase_qty", "purchaseQty", "num", "amount", "stock_num")
    has_any_quantity = any(_first(row, {}, *quantity_keys) not in (None, "") for row in rows)
    if not has_any_quantity:
        first = rows[0]
        purchase_sn = str(_first(first, {}, "purchase_sn", "purchase_order_no", "po_no") or "")
        return [
            PurchaseBatch(
                shipment_no=item.shipment_no,
                sku=item.sku,
                box_no=item.box_no,
                quantity=item.quantity,
                purchase_order_no=purchase_sn,
                purchase_sn=purchase_sn,
                purchase_unit_price=item.purchase_unit_price,
                quantity_missing=True,
            )
        ]

    batches: list[PurchaseBatch] = []
    for row in rows:
        purchase_sn = str(_first(row, {}, "purchase_sn", "purchase_order_no", "po_no") or "")
        quantity_value = _first(row, {}, *quantity_keys)
        batches.append(
            PurchaseBatch(
                shipment_no=item.shipment_no,
                sku=item.sku,
                box_no=str(_first(row, {}, "box_no", "boxNo") or item.box_no),
                quantity=decimal_or_zero(quantity_value),
                purchase_order_no=purchase_sn,
                purchase_sn=purchase_sn,
                batch_no=str(_first(row, {}, "batch_no", "stock_batch_no") or ""),
                purchase_unit_price=item.purchase_unit_price,
                quantity_missing=quantity_value in (None, ""),
            )
        )
    return batches


def _map_outbound_batches(item: ShipmentItem, payload: dict[str, Any]) -> list[PurchaseBatch]:
    outbound_batches = payload.get("outbound_batch") or payload.get("outboundBatch") or []
    if not isinstance(outbound_batches, list):
        return []

    batches: list[PurchaseBatch] = []
    for outbound_batch in outbound_batches:
        if not isinstance(outbound_batch, dict):
            continue
        batch_sku = _first(outbound_batch, {}, "sku", "seller_sku", "local_sku", "msku")
        if batch_sku not in (None, "") and str(batch_sku) != item.sku:
            continue
        records = outbound_batch.get("batch_record_list") or outbound_batch.get("batchRecordList") or []
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            purchase_sns = _as_list(
                record.get("purchase_order_sns")
                or record.get("custom_purchase_order_sns")
                or record.get("purchase_sns")
                or record.get("purchaseSn")
            )
            if not purchase_sns:
                continue
            supplier_names = _as_list(record.get("supplier_names") or record.get("supplier_name") or record.get("supplier"))
            quantity = decimal_or_zero(_first(record, {}, "outbound_num", "quantity", "qty", "num"))
            if quantity == 0:
                quantity = item.quantity
            if item.box_no and item.quantity and quantity > item.quantity:
                quantity = item.quantity
            unit_price = _money_decimal(
                _first(record, {}, "unit_purchase_price", "unit_storage_cost", "purchase_unit_price", "price")
            )
            for index, purchase_sn in enumerate(purchase_sns):
                batches.append(
                    PurchaseBatch(
                        shipment_no=item.shipment_no,
                        sku=item.sku,
                        box_no=item.box_no,
                        quantity=quantity if len(purchase_sns) == 1 else quantity / Decimal(len(purchase_sns)),
                        supplier=str(supplier_names[index] if index < len(supplier_names) else supplier_names[0] if supplier_names else ""),
                        purchase_order_no=str(purchase_sn),
                        purchase_sn=str(purchase_sn),
                        batch_no=str(_first(record, {}, "warehouse_batch_id", "batch_no", "stock_batch_no") or ""),
                        purchase_unit_price=unit_price or item.purchase_unit_price,
                    )
                )
    return batches


def _box_info_for_item(payload: dict[str, Any]) -> dict[str, Any]:
    matched_box_info = payload.get("_matched_box_info")
    if isinstance(matched_box_info, dict):
        return matched_box_info
    box_list = payload.get("box_list") or payload.get("boxList") or []
    if not isinstance(box_list, list):
        return {}
    item_sku_box_key = str(payload.get("sku_box_key") or "")
    item_sku = str(_first(payload, {}, "sku", "seller_sku", "local_sku", "msku") or "")
    item_shipment_id = str(_first(payload, {}, "shipment_id", "shipmentId") or "")
    item_msku = str(_first(payload, {}, "msku", "seller_sku") or "")

    fallback: dict[str, Any] = {}
    for box in box_list:
        if not isinstance(box, dict):
            continue
        box_skus = box.get("box_skus") or box.get("boxSkus") or []
        if not isinstance(box_skus, list):
            continue
        for box_sku in box_skus:
            if not isinstance(box_sku, dict):
                continue
            if _box_sku_matches_item(box_sku, item_sku_box_key, item_sku, item_shipment_id, item_msku):
                return box
            if item_sku and str(box_sku.get("sku") or "") == item_sku and not fallback:
                fallback = box
    return fallback


def _expand_item_by_boxes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    matched_boxes = _matching_box_infos_for_item(payload)
    if len(matched_boxes) <= 1:
        return [payload]

    expanded: list[dict[str, Any]] = []
    for box, box_sku in matched_boxes:
        row = dict(payload)
        row["_matched_box_info"] = box
        quantity_in_case = _first(box_sku, {}, "quantity_in_case", "quantity", "qty", "num")
        if quantity_in_case not in (None, ""):
            quantity = decimal_or_zero(quantity_in_case) * _packing_carton_count_for_payload(row)
            row["quantity_shipped"] = quantity
            row["quantity"] = quantity
            row["num"] = quantity
        expanded.append(row)
    return expanded


def _packing_carton_count_for_payload(payload: dict[str, Any]) -> Decimal:
    box_info = _box_info_for_item(payload)
    return _carton_count_for_payload(payload, box_info)


def _carton_count_for_payload(payload: dict[str, Any], box_info: dict[str, Any], box_no: str | None = None) -> Decimal:
    if box_no is None:
        box_no = str(_first(payload, box_info, *_BOX_NO_KEYS) or "")
    return _carton_count_from_actual_box_no(box_no) or decimal_or_zero(_first(box_info, payload, *_BOX_COUNT_KEYS) or 1)


def _carton_count_from_actual_box_no(box_no: str) -> Decimal | None:
    display_box_no = _display_box_no(str(box_no or ""))
    count = _box_count_from_box_no(display_box_no)
    if count is not None:
        return count
    if _AMAZON_CARTON_CODE_RE.fullmatch(display_box_no):
        return Decimal("1")
    return None


def _matching_box_infos_for_item(payload: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    box_list = payload.get("box_list") or payload.get("boxList") or []
    if not isinstance(box_list, list):
        return []
    item_sku_box_key = str(payload.get("sku_box_key") or "")
    item_sku = str(_first(payload, {}, "sku", "seller_sku", "local_sku", "msku") or "")
    item_shipment_id = str(_first(payload, {}, "shipment_id", "shipmentId") or "")
    item_msku = str(_first(payload, {}, "msku", "seller_sku") or "")

    exact_matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    sku_matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for box in box_list:
        if not isinstance(box, dict):
            continue
        box_skus = box.get("box_skus") or box.get("boxSkus") or []
        if not isinstance(box_skus, list):
            continue
        for box_sku in box_skus:
            if not isinstance(box_sku, dict):
                continue
            if _box_sku_matches_item(box_sku, item_sku_box_key, item_sku, item_shipment_id, item_msku):
                exact_matches.append((box, box_sku))
            elif item_sku and str(box_sku.get("sku") or "") == item_sku:
                sku_matches.append((box, box_sku))
    return exact_matches or sku_matches


def _box_sku_matches_item(
    box_sku: dict[str, Any],
    item_sku_box_key: str,
    item_sku: str,
    item_shipment_id: str,
    item_msku: str,
) -> bool:
    if item_sku_box_key and str(box_sku.get("sku_box_key") or "") == item_sku_box_key:
        return True
    if item_sku and str(box_sku.get("sku") or "") != item_sku:
        return False
    if item_shipment_id and str(box_sku.get("shipment_id") or "") not in ("", item_shipment_id):
        return False
    if item_msku and str(box_sku.get("msku") or "") not in ("", item_msku):
        return False
    return bool(item_sku)


def _first(primary: dict[str, Any], secondary: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in primary and primary[key] not in (None, ""):
            return primary[key]
        if key in secondary and secondary[key] not in (None, ""):
            return secondary[key]
    return None


def _first_nonzero(primary: dict[str, Any], secondary: dict[str, Any], *keys: str) -> Any:
    for source in (primary, secondary):
        for key in keys:
            value = source.get(key)
            if value in (None, ""):
                continue
            if _is_zero_value(value):
                continue
            return value
    return None


def _is_zero_value(value: Any) -> bool:
    try:
        return Decimal(str(value)) == 0
    except Exception:
        return False


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _money_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    text = str(value)
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
    if cleaned in ("", ".", "-", "-."):
        return None
    return Decimal(cleaned)


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [item for item in value if item not in (None, "")]
    return [value]


def _select_supplier_account_name(value: Any) -> str:
    names: list[str] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            item = _first(item, {}, "account_name", "accountName", "name")
        for part in re.split(r"[\r\n,;/，；]+", str(item or "")):
            text = part.strip()
            if text:
                names.append(text)
    if not names:
        return ""
    for name in names:
        if name.endswith("公司") or name.endswith("厂"):
            return name
    return names[0]


def _supplier_match_names(row: dict[str, Any], account_name: str) -> list[str]:
    names: list[str] = []
    for key in ("supplier_name", "supplier", "name", "supplierName", "code", "supplier_code", "supplier_id", "id"):
        value = row.get(key)
        if value not in (None, ""):
            names.append(str(value))
    if account_name:
        names.append(account_name)

    distinct: list[str] = []
    for name in names:
        if name and name not in distinct:
            distinct.append(name)
    return distinct


def _purchase_order_request_bodies(purchase_sn: str) -> list[dict[str, Any]]:
    dated_payload = _purchase_order_date_payload(purchase_sn)
    if not dated_payload:
        return []
    return [dict(dated_payload, purchase_sn=purchase_sn)]


def _purchase_order_date_payload(purchase_sn: str) -> dict[str, Any] | None:
    match = re.search(r"PO(\d{2})(\d{2})(\d{2})", str(purchase_sn or ""))
    if match:
        year = 2000 + int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        try:
            parsed = date(year, month, day).isoformat()
            return {"start_date": parsed, "end_date": parsed, "search_field_time": "create_time"}
        except ValueError:
            pass
    return None


def _probe_purchase_order_request_bodies(purchase_sn: str) -> list[dict[str, Any]]:
    dated_payload = _purchase_order_date_payload(purchase_sn)
    bodies: list[dict[str, Any]] = []
    if dated_payload:
        for key in ("order_sn", "custom_order_sn", "purchase_sn", "purchase_order_no", "po_no"):
            body = dict(dated_payload)
            body[key] = purchase_sn
            bodies.append(body)
        bodies.append(dict(dated_payload))
    bodies.append({"purchase_sn": purchase_sn})
    return bodies


def _purchaser_names_from_env() -> dict[str, str]:
    raw = os.getenv("LINGXING_PURCHASER_ID_NAME_MAP", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return {str(key): str(value) for key, value in parsed.items() if value not in (None, "")}

    names: dict[str, str] = {}
    for pair in raw.replace("；", ";").split(";"):
        if not pair.strip():
            continue
        if ":" in pair:
            key, value = pair.split(":", 1)
        elif "=" in pair:
            key, value = pair.split("=", 1)
        else:
            continue
        key = key.strip()
        value = value.strip()
        if key and value:
            names[key] = value
    return names


def _date_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    if text.isdigit() and len(text) >= 10:
        try:
            from datetime import datetime

            return datetime.fromtimestamp(int(text[:10])).date().isoformat()
        except Exception:
            return text
    return text


def _extract_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("data", "list", "items", "rows", "records"):
        value = data.get(key)
        rows = _extract_rows(value)
        if rows:
            return rows
    return []


def _is_valid_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if not payload:
        return False
    return any(value not in (None, "", [], {}) for value in payload.values())


def _first_matching(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get(key, "")) == value:
            return row
    return rows[0] if len(rows) == 1 else None


def _first_matching_any(rows: list[dict[str, Any]], keys: tuple[str, ...], value: str) -> dict[str, Any] | None:
    for key in keys:
        matched = _first_matching(rows, key, value)
        if matched:
            return matched
    return None


def _purchase_order_matches(order: dict[str, Any], keys: tuple[str, ...], purchase_sn: str) -> bool:
    return any(str(order.get(key) or "") == purchase_sn for key in keys)


def _purchase_order_has_placeholder_supplier(order: dict[str, Any]) -> bool:
    supplier_name = _first(order, {}, "supplier_name", "supplier", "supplierName")
    return str(supplier_name or "").strip().startswith("Supplier")


def _coerce_int_string(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _client_page_size(client: Any) -> int:
    config = getattr(client, "config", None)
    page_size = getattr(config, "page_size", 100)
    try:
        value = int(page_size)
    except (TypeError, ValueError):
        return 100
    return value if value > 0 else 100


def _is_parameter_error(exc: Exception) -> bool:
    text = str(exc)
    return "参数" in text or "parameter" in text.lower() or "code': 102" in text or '"code": 102' in text


def _is_permission_or_whitelist_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "ip not permit",
            "ip address is not allowed",
            "white list",
            "whitelist",
            "permission denied",
            "no permission",
        )
    )


def _detail_candidate_values(header: dict[str, Any]) -> list[Any]:
    keys = (
        "shipment_sn",
        "shipmentSn",
        "shipment_id",
        "shipmentId",
        "fba_shipment_id",
        "fbaShipmentId",
        "shipment_no",
        "shipmentNo",
        "shipOrderNo",
        "order_sn",
        "id",
    )
    values: list[Any] = []
    for key in keys:
        value = header.get(key)
        if value not in (None, "") and value not in values:
            values.append(value)
    return values


def _detail_request_bodies(values: list[Any]) -> list[dict[str, Any]]:
    param_names = (
        "shipment_sn",
        "shipmentSn",
        "shipment_id",
        "shipmentId",
        "fba_shipment_id",
        "fbaShipmentId",
        "shipment_no",
        "shipmentNo",
        "id",
    )
    bodies: list[dict[str, Any]] = []
    for value in values:
        for param_name in param_names:
            body = {param_name: value}
            if body not in bodies:
                bodies.append(body)
    return bodies


def _dump_header_keys(
    rows: list[dict[str, Any]],
    filtered_rows: list[dict[str, Any]],
    warehouse_field: str,
    warehouse_name: str,
    page_summaries: list[dict[str, Any]] | None = None,
) -> None:
    debug_dir = os.getenv("LINGXING_DEBUG_DIR", "")
    if not debug_dir or not rows:
        return
    path = Path(debug_dir)
    path.mkdir(parents=True, exist_ok=True)
    first = rows[0]
    payload = {
        "row_count": len(rows),
        "filtered_row_count": len(filtered_rows),
        "warehouse_field": warehouse_field,
        "warehouse_name": warehouse_name,
        "page_count": len(page_summaries or []),
        "pages": page_summaries or [],
        "shipment_time_counts": _count_by_date(rows, "shipment_time"),
        "shipment_time_second_counts": _count_by_date(rows, "shipment_time_second"),
        "pick_time_counts": _count_by_date(rows, "pick_time"),
        "warehouse_counts": _count_by_field(rows, warehouse_field),
        "status_counts": _count_by_field(rows, "status"),
        "first_row_keys": sorted(first.keys()),
        "first_row_preview": {key: first.get(key) for key in list(first.keys())[:30]},
    }
    (path / "shipment_list_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _dump_purchase_enrichment_summary(
    original_batches: list[PurchaseBatch],
    enriched_batches: list[PurchaseBatch],
    purchase_orders: dict[str, dict[str, Any]],
    purchaser_names: dict[str, str],
    cache: Any | None = None,
) -> None:
    debug_dir = os.getenv("LINGXING_DEBUG_DIR", "")
    if not debug_dir:
        return
    path = Path(debug_dir)
    path.mkdir(parents=True, exist_ok=True)
    purchaser_ids = {
        str(_first(order, {}, "purchaser_id", "purchaserId", "buyer_id", "buyerId"))
        for order in purchase_orders.values()
        if _first(order, {}, "purchaser_id", "purchaserId", "buyer_id", "buyerId") not in (None, "")
    }
    payload = {
        "purchase_batch_count": len(original_batches),
        "enriched_batch_count": len(enriched_batches),
        "unique_purchase_sn_count": len({batch.purchase_sn or batch.purchase_order_no for batch in original_batches if batch.purchase_sn or batch.purchase_order_no}),
        "purchase_order_found_count": len(purchase_orders),
        "purchaser_id_count": len(purchaser_ids),
        "purchaser_name_found_count": len([purchaser_id for purchaser_id in purchaser_ids if purchaser_id in purchaser_names]),
        "cache_dir": str(getattr(cache, "cache_dir", "")) if cache is not None else "",
        "purchase_order_cache_file_count": _cache_file_count(cache, "purchase_order"),
        "purchaser_names_cache_file_count": _cache_file_count(cache, "purchaser_names"),
        "supplier_infos_cache_file_count": _cache_file_count(cache, "supplier_infos"),
        "missing_purchase_entity_count": len([batch for batch in enriched_batches if not batch.purchase_entity]),
        "missing_purchase_entity_examples": [
            {
                "shipment_no": batch.shipment_no,
                "sku": batch.sku,
                "box_no": batch.box_no,
                "purchase_sn": batch.purchase_sn or batch.purchase_order_no,
                "purchase_order_found": bool(purchase_orders.get(batch.purchase_sn or batch.purchase_order_no)),
                "purchase_order_purchaser_id": str(
                    _first(purchase_orders.get(batch.purchase_sn or batch.purchase_order_no, {}), {}, "purchaser_id", "purchaserId", "buyer_id", "buyerId")
                    or ""
                ),
                "purchase_order_purchaser_name": str(
                    _first(
                        purchase_orders.get(batch.purchase_sn or batch.purchase_order_no, {}),
                        {},
                        "purchaser_name",
                        "purchaserName",
                        "purchase_entity",
                        "buyer_name",
                        "buyerName",
                        "subject_name",
                        "company_name",
                    )
                    or ""
                ),
                "purchaser_list_matched": str(
                    _first(purchase_orders.get(batch.purchase_sn or batch.purchase_order_no, {}), {}, "purchaser_id", "purchaserId", "buyer_id", "buyerId")
                    or ""
                )
                in purchaser_names,
            }
            for batch in enriched_batches
            if not batch.purchase_entity
        ][:20],
    }
    (path / "purchase_enrichment_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_file_count(cache: Any | None, namespace: str) -> int:
    cache_dir = getattr(cache, "cache_dir", None)
    if cache_dir is None:
        return 0
    path = Path(cache_dir) / namespace
    if not path.exists():
        return 0
    return len(list(path.glob("*.json")))


def _page_summary(mode: str, payload: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "mode": mode,
        "request": payload,
        "row_count": len(items),
        "first_shipment_sn": _shipment_identity(items[0]) if items else "",
        "last_shipment_sn": _shipment_identity(items[-1]) if items else "",
        "first_time": _row_time_text(items[0]) if items else "",
        "last_time": _row_time_text(items[-1]) if items else "",
        "stop_reason": "",
    }


def _count_by_date(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field) or "")[:10]
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _count_by_field(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _is_shipped(row: dict[str, Any], shipped_status: int | str) -> bool:
    status_value = _first(row, {}, "status", "status_name", "shipment_status", "state", "state_name")
    status_text_values = [
        str(value)
        for value in (
            row.get("status_name"),
            row.get("shipment_status"),
            row.get("state_name"),
            row.get("state"),
            row.get("status"),
        )
        if value not in (None, "")
    ]
    if any(_is_voided_status_text(value) for value in status_text_values):
        return False
    if status_value in (None, ""):
        return False
    status_text = str(status_value).strip()
    if status_text.lstrip("-").isdigit():
        return int(status_text) == 1
    return status_text in {"已发货", "shipped", "SHIPPED"}


def _is_voided_status_text(value: str) -> bool:
    return any(keyword in value for keyword in ("作废", "取消", "删除"))


def _matches_shipment_time(row: dict[str, Any], shipment_time: str | None) -> bool:
    if not shipment_time:
        return True
    row_times = _shipment_time_values(row)
    if not row_times:
        return False
    return any(_date_text(row_time) == shipment_time for row_time in row_times)


def _shipment_time_values(row: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    for key in ("pick_time", "shipment_time_second", "shipment_time", "shipmentTime", "actual_shipment_time", "delivery_date"):
        value = row.get(key)
        if value not in (None, ""):
            values.append(value)
    return values


def _row_time_text(row: dict[str, Any]) -> str:
    values = _shipment_time_values(row)
    return str(values[0]) if values else ""


def _is_before_target_date(items: list[dict[str, Any]], shipment_time: str | None) -> bool:
    if not shipment_time or not items:
        return False
    dates = []
    for item in items:
        text = _row_time_text(item)
        if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
            dates.append(text[:10])
    return bool(dates) and max(dates) < shipment_time


def _shipment_identity(row: dict[str, Any]) -> str:
    value = _first(row, {}, "shipment_sn", "shipmentSn", "shipment_no", "shipmentNo", "id")
    return str(value or "")


def _matches_warehouse(row: dict[str, Any], warehouse_field: str, warehouse_name: str) -> bool:
    if not warehouse_name:
        return True
    return str(row.get(warehouse_field, "")) == warehouse_name


def _find_item_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        direct = [item for item in payload if isinstance(item, dict) and _looks_like_shipment_item(item)]
        if direct:
            return direct
        rows: list[dict[str, Any]] = []
        for item in payload:
            rows.extend(_find_item_rows(item))
        return rows
    if not isinstance(payload, dict):
        return []

    for key in ("items", "products", "product_list", "box_items", "mws_detail", "mws_list", "list", "rows", "data"):
        value = payload.get(key)
        rows = _find_item_rows(value)
        if rows:
            parent_context = {
                parent_key: parent_value
                for parent_key, parent_value in payload.items()
                if parent_key != key and not _is_nested_collection(parent_value)
            }
            if "purchase_items" in payload:
                parent_context["purchase_items"] = payload["purchase_items"]
            if "purchaseItems" in payload:
                parent_context["purchaseItems"] = payload["purchaseItems"]
            if "outbound_batch" in payload:
                parent_context["outbound_batch"] = payload["outbound_batch"]
            if "outboundBatch" in payload:
                parent_context["outboundBatch"] = payload["outboundBatch"]
            if "box_list" in payload:
                parent_context["box_list"] = payload["box_list"]
            if "boxList" in payload:
                parent_context["boxList"] = payload["boxList"]
            if "auxs" in payload:
                parent_context["auxs"] = payload["auxs"]
            if "head_logistics_list" in payload:
                parent_context["head_logistics_list"] = payload["head_logistics_list"]
            if "headLogisticsList" in payload:
                parent_context["headLogisticsList"] = payload["headLogisticsList"]
            return [dict(parent_context, **row) for row in rows]
    return [payload] if _looks_like_shipment_item(payload) else []


def _looks_like_shipment_item(payload: dict[str, Any]) -> bool:
    has_sku = _first(payload, {}, "sku", "seller_sku", "local_sku", "msku") not in (None, "")
    has_quantity = _first(payload, {}, "quantity", "qty", "ship_qty", "quantity_shipped", "num") not in (None, "")
    return has_sku and has_quantity


def _is_nested_collection(value: Any) -> bool:
    return isinstance(value, (dict, list))
