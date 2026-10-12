from __future__ import annotations

import hashlib
import json
import os
import time
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.common.cache import JsonCache
from src.common.lingxing_client import _load_dotenv
from src.shipment.models import RawCustomsData
from src.shipment.seller_department import purchase_entity_for_seller


DEFAULT_API_ID = "aOeOfPzADG"
DEFAULT_URL = "https://www.foresight.radiancewave.cn/api/assetOpenApi/queryData"
LOCK_TIMEOUT_SECONDS = 600
LOCK_STALE_SECONDS = 900
VALIDATION_VERSION = 1
SNAPSHOT_ATTEMPTS = 6
REFRESH_COOLDOWN_SECONDS = 300


class ShopMappingError(RuntimeError):
    pass


class ShopMappingRateLimitError(ShopMappingError):
    pass


@dataclass(frozen=True)
class ShopMappingConfig:
    url: str
    app_key: str
    app_secret: str
    api_id: str = DEFAULT_API_ID
    page_size: int = 200
    timeout_seconds: int = 30
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "ShopMappingConfig":
        _load_dotenv()
        config = cls(
            url=_mapping_url_from_env(os.getenv("CUSTOMS_SHOP_MAPPING_URL")),
            app_key=os.getenv("CUSTOMS_SHOP_MAPPING_APP_KEY", ""),
            app_secret=os.getenv("CUSTOMS_SHOP_MAPPING_APP_SECRET", ""),
            api_id=os.getenv("CUSTOMS_SHOP_MAPPING_API_ID") or DEFAULT_API_ID,
            page_size=_clamp_page_size(os.getenv("CUSTOMS_SHOP_MAPPING_PAGE_SIZE", "200")),
            timeout_seconds=int(os.getenv("CUSTOMS_SHOP_MAPPING_TIMEOUT_SECONDS", "30")),
            max_retries=max(1, int(os.getenv("CUSTOMS_SHOP_MAPPING_MAX_RETRIES", "3"))),
        )
        missing = [
            name
            for name, value in (
                ("CUSTOMS_SHOP_MAPPING_URL", config.url),
                ("CUSTOMS_SHOP_MAPPING_APP_KEY", config.app_key),
                ("CUSTOMS_SHOP_MAPPING_APP_SECRET", config.app_secret),
            )
            if not value
        ]
        if missing:
            raise ShopMappingError("Missing shop mapping API config in .env: " + ", ".join(missing))
        return config


@dataclass(frozen=True)
class ShopMappingRecord:
    shop_name: str
    department: str = ""
    purchase_entity: str = ""
    final_customer: str = ""


@dataclass(frozen=True)
class ShopMappingLoadResult:
    mapping: dict[str, ShopMappingRecord]
    loaded_rows: int
    slot_key: str
    source: str
    refreshed: bool = False
    warning: str | None = None


class ShopMappingClient:
    def __init__(self, config: ShopMappingConfig | None = None) -> None:
        self.config = config or ShopMappingConfig.from_env()

    def fetch_all(self) -> dict[str, ShopMappingRecord]:
        previous = None
        last_error = "snapshots did not agree"
        for attempt in range(SNAPSHOT_ATTEMPTS):
            if attempt:
                time.sleep(1)
            try:
                current = self._fetch_snapshot()
            except ShopMappingRateLimitError:
                raise
            except ShopMappingError as exc:
                previous = None
                last_error = str(exc)
                continue
            if current == previous:
                return current
            previous = current
            last_error = "two consecutive clean snapshots did not agree"
        raise ShopMappingError(f"Shop mapping snapshot validation failed: {last_error}")

    def _fetch_snapshot(self) -> dict[str, ShopMappingRecord]:
        mapping: dict[str, ShopMappingRecord] = {}
        page_index = 1
        while True:
            rows = self._fetch_page(page_index)
            for row in rows:
                record = _record_from_row(row)
                if record is None:
                    raise ShopMappingError("Shop mapping page contains a row without shop_name")
                if record.shop_name in mapping:
                    reason = "conflicting" if mapping[record.shop_name] != record else "duplicate"
                    raise ShopMappingError(f"Shop mapping {reason} shop on page {page_index}: {record.shop_name}")
                mapping[record.shop_name] = record
            if len(rows) < self.config.page_size:
                if not mapping:
                    raise ShopMappingError("Shop mapping snapshot is empty")
                return mapping
            page_index += 1
            if page_index > 1000:
                raise ShopMappingError("Shop mapping API pagination exceeded 1000 pages")
            time.sleep(1)

    def _fetch_page(self, page_index: int) -> list[dict[str, Any]]:
        payload = {
            "apiId": self.config.api_id,
            "pageIndex": page_index,
            "pageSize": self.config.page_size,
        }
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                data = self._post_json(payload)
                _raise_for_api_error(data)
                return _extract_rows(data)
            except (HTTPError, URLError, TimeoutError, ValueError, ShopMappingError) as exc:
                if isinstance(exc, ShopMappingRateLimitError):
                    raise
                if isinstance(exc, HTTPError) and exc.code == 429:
                    raise ShopMappingRateLimitError("Shop mapping API rate limited (HTTP 429)") from exc
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                time.sleep(min(2**attempt, 10))
        raise ShopMappingError(f"Shop mapping API request failed after retries: {last_error}") from last_error

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        timestamp = str(int(time.time() * 1000))
        headers = {
            "sign": generate_sign(self.config.app_secret, {"timestamp": timestamp}),
            "timestamp": timestamp,
            "appKey": self.config.app_key,
            "Content-Type": "application/json",
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(self.config.url, data=body, headers=headers, method="POST")
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ShopMappingError("Shop mapping API response is not a JSON object")
        return parsed


def generate_sign(app_secret: str, params: dict[str, str]) -> str:
    sign_text = "".join(key + str(params[key]) for key in sorted(params)).strip() + app_secret
    return hashlib.md5(sign_text.encode("utf-8")).hexdigest().upper()


def load_shop_mapping_for_current_slot(
    now: datetime | None = None,
    client: ShopMappingClient | None = None,
    cache: JsonCache | None = None,
    *,
    force_refresh: bool = False,
) -> ShopMappingLoadResult:
    now = now or datetime.now()
    cache = cache or JsonCache()
    slot_key = shop_mapping_slot_key(now)
    latest_payload = _cache_get_mapping_payload(cache, "latest")
    cached_mapping = _mapping_from_payload(latest_payload) if _payload_slot_key(latest_payload) == slot_key else None
    if cached_mapping is not None and not force_refresh:
        _prune_shop_mapping_cache(cache)
        return ShopMappingLoadResult(
            mapping=cached_mapping,
            loaded_rows=len(cached_mapping),
            slot_key=slot_key,
            source="latest-cache",
        )

    with _shop_mapping_lock(cache):
        latest_payload = _cache_get_mapping_payload(cache, "latest")
        cached_mapping = _mapping_from_payload(latest_payload) if _payload_slot_key(latest_payload) == slot_key else None
        if cached_mapping is not None and not force_refresh:
            _prune_shop_mapping_cache(cache)
            return ShopMappingLoadResult(
                mapping=cached_mapping,
                loaded_rows=len(cached_mapping),
                slot_key=slot_key,
                source="latest-cache",
            )

        attempt = cache.get("shop_mapping_attempt", slot_key, ttl_days=3650)
        if isinstance(attempt, dict):
            try:
                attempted_at = datetime.fromisoformat(attempt.get("attempted_at", ""))
                cooling_down = (now - attempted_at).total_seconds() < REFRESH_COOLDOWN_SECONDS
            except (ValueError, TypeError):
                cooling_down = False
            if cooling_down:
                return _latest_or_empty(cache, slot_key, "shop mapping refresh is in its 5 minute cooldown")

        try:
            api_client = client or ShopMappingClient()
            mapping = api_client.fetch_all()
        except ShopMappingError as exc:
            if _is_missing_config_error(exc):
                return _latest_or_empty(cache, slot_key, str(exc))
            cache.set(
                "shop_mapping_attempt",
                slot_key,
                {"success": False, "error": str(exc), "attempted_at": now.isoformat(timespec="seconds")},
            )
            return _latest_or_empty(cache, slot_key, str(exc))

        payload = {
            "validation_version": VALIDATION_VERSION,
            "slot_key": slot_key,
            "refreshed_at": now.isoformat(timespec="seconds"),
            "records": [asdict(record) for record in mapping.values()],
        }
        _save_validated_mapping(cache, payload)
        _prune_shop_mapping_cache(cache)
        cache.set(
            "shop_mapping_attempt",
            slot_key,
            {"success": True, "attempted_at": now.isoformat(timespec="seconds")},
        )
        return ShopMappingLoadResult(
            mapping=mapping,
            loaded_rows=len(mapping),
            slot_key=slot_key,
            source="api",
            refreshed=True,
        )


def apply_shop_mapping(raw_data: RawCustomsData, mapping: dict[str, ShopMappingRecord]) -> tuple[int, int]:
    exact_entities: dict[tuple[str, str, str], str] = {}
    sku_entities: dict[tuple[str, str], str] = {}
    updated_items = []
    applied_items = 0

    for item in raw_data.shipment_items:
        record = mapping.get(_normalize_shop_name(item.seller_name))
        purchase_entity = _purchase_entity_from_record(record) if record is not None else ""
        final_customer = record.final_customer if record is not None else ""
        if record is not None:
            applied_items += 1
        exact_entities[(item.shipment_no, item.sku, item.box_no or "")] = purchase_entity
        sku_entities[(item.shipment_no, item.sku)] = purchase_entity
        updated_items.append(
            _replace_dataclass(
                item,
                purchase_entity=purchase_entity,
                final_customer=final_customer,
            )
        )

    updated_batches = []
    for batch in raw_data.purchase_batches:
        key = (batch.shipment_no, batch.sku, batch.box_no or "")
        fallback_key = (batch.shipment_no, batch.sku)
        purchase_entity = exact_entities.get(key, sku_entities.get(fallback_key, ""))
        updated_batches.append(_replace_dataclass(batch, purchase_entity=purchase_entity))

    raw_data.shipment_items = updated_items
    raw_data.purchase_batches = updated_batches
    return len(mapping), applied_items


def _purchase_entity_from_record(record: ShopMappingRecord) -> str:
    if not missing_mapping_value(record.purchase_entity):
        return record.purchase_entity
    return purchase_entity_for_seller(record.shop_name, {record.shop_name: record.department})


def missing_mapping_value(value: Any) -> bool:
    return str(value or "").strip() in {"", "待确认"}


def invalid_mapping_shipments(raw_data: RawCustomsData) -> set[tuple[str, str]]:
    return {
        (item.source, item.shipment_no)
        for item in raw_data.shipment_items
        if missing_mapping_value(item.purchase_entity) or missing_mapping_value(item.final_customer)
    }


def _save_validated_mapping(cache: JsonCache, payload: dict[str, Any]) -> None:
    path = Path(cache.cache_dir) / "shop_mapping" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"saved_at": time.time(), "value": payload}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def shop_mapping_slot_key(now: datetime) -> str:
    slot_date = now.date()
    slot_hour = 14
    if now.hour >= 14:
        slot_hour = 14
    elif now.hour >= 10:
        slot_hour = 10
    else:
        slot_date = (now - timedelta(days=1)).date()
        slot_hour = 14
    return f"{slot_date.isoformat()}-{slot_hour:02d}00"


def _latest_or_empty(cache: JsonCache, slot_key: str, warning: str) -> ShopMappingLoadResult:
    latest = _cache_get_mapping(cache, "latest")
    if latest is not None:
        _prune_shop_mapping_cache(cache)
        return ShopMappingLoadResult(
            mapping=latest,
            loaded_rows=len(latest),
            slot_key=slot_key,
            source="latest-cache",
            warning=f"failed to refresh shop mapping for slot {slot_key}; using latest cache: {warning}",
        )
    return ShopMappingLoadResult(
        mapping={},
        loaded_rows=0,
        slot_key=slot_key,
        source="empty",
        warning=f"failed to refresh shop mapping for slot {slot_key}; no cached mapping available: {warning}",
    )


def _cache_get_mapping(cache: JsonCache, key: str) -> dict[str, ShopMappingRecord] | None:
    payload = _cache_get_mapping_payload(cache, key)
    return _mapping_from_payload(payload)


def _cache_get_mapping_payload(cache: JsonCache, key: str) -> dict[str, Any] | None:
    payload = cache.get("shop_mapping", key, ttl_days=3650)
    if not isinstance(payload, dict):
        return None
    records = payload.get("records")
    if not isinstance(records, list):
        return None
    return payload


def _mapping_from_payload(payload: dict[str, Any] | None) -> dict[str, ShopMappingRecord] | None:
    if payload is None or payload.get("validation_version") != VALIDATION_VERSION:
        return None
    records = payload.get("records")
    if not isinstance(records, list):
        return None
    mapping: dict[str, ShopMappingRecord] = {}
    for item in records:
        if not isinstance(item, dict):
            return None
        record = _record_from_row(
            {
                "shop_name": item.get("shop_name"),
                "ywbm": item.get("department"),
                "cgzt": item.get("purchase_entity"),
                "zzkh": item.get("final_customer"),
            }
        )
        if record is None or record.shop_name in mapping:
            return None
        mapping[record.shop_name] = record
    return mapping or None


def _payload_slot_key(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("slot_key") or "")


def _prune_shop_mapping_cache(cache: JsonCache) -> None:
    shop_mapping_dir = Path(cache.cache_dir) / "shop_mapping"
    if not shop_mapping_dir.exists():
        return
    for path in shop_mapping_dir.glob("*.json"):
        if path.name == "latest.json":
            continue
        try:
            path.unlink()
        except OSError:
            pass


@contextmanager
def _shop_mapping_lock(cache: JsonCache) -> Iterator[None]:
    lock_path = Path(cache.cache_dir) / "locks" / "shop_mapping_refresh.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    fd: int | None = None
    try:
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"pid={os.getpid()} time={time.time()}".encode("utf-8"))
                break
            except FileExistsError:
                if _is_stale_lock(lock_path):
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.time() - started_at > LOCK_TIMEOUT_SECONDS:
                    raise ShopMappingError(f"Timed out waiting for shop mapping refresh lock: {lock_path}")
                time.sleep(0.5)
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def _is_stale_lock(lock_path: Path) -> bool:
    try:
        return time.time() - lock_path.stat().st_mtime > LOCK_STALE_SECONDS
    except FileNotFoundError:
        return False

def _record_from_row(row: dict[str, Any]) -> ShopMappingRecord | None:
    shop_name = _normalize_shop_name(row.get("shop_name"))
    if not shop_name:
        return None
    return ShopMappingRecord(
        shop_name=shop_name,
        department=_cell_text(row.get("ywbm")),
        purchase_entity=_cell_text(row.get("cgzt")),
        final_customer=_cell_text(row.get("zzkh")),
    )


def _extract_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        if any(not isinstance(item, dict) for item in data):
            raise ShopMappingError("Shop mapping page contains invalid rows")
        return data
    if isinstance(data, dict):
        for key in ("data", "list", "items", "rows", "records"):
            if key in data:
                return _extract_rows(data[key])
    raise ShopMappingError("Shop mapping response has no row list")


def _raise_for_api_error(data: dict[str, Any]) -> None:
    success = data.get("success")
    code = data.get("code")
    if success is True or code in (200, "200", 0, "0"):
        return
    message = data.get("msg") or data.get("message") or data
    if code in (429, "429") or "超过接口最大调用次数" in str(message):
        raise ShopMappingRateLimitError(str(message))
    raise ShopMappingError(str(message))


def _replace_dataclass(instance: Any, **changes: Any) -> Any:
    from dataclasses import replace

    return replace(instance, **changes)


def _normalize_shop_name(value: Any) -> str:
    return str(value or "").strip()


def _cell_text(value: Any) -> str:
    return str(value or "").strip()


def _mapping_url_from_env(value: str | None) -> str:
    url = str(value or "").strip()
    if not url:
        return DEFAULT_URL
    if url in {
        "http://www.foresight.radiancewave.cn/api/assetOpenApi/queryData",
        "http://www.foresight.radiancewave.cn:80/api/assetOpenApi/queryData",
    }:
        return DEFAULT_URL
    return url


def _clamp_page_size(value: str) -> int:
    try:
        page_size = int(value)
    except (TypeError, ValueError):
        return 200
    return min(max(page_size, 1), 200)


def _is_missing_config_error(exc: Exception) -> bool:
    return "Missing shop mapping API config" in str(exc)
