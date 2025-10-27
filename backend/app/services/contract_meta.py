from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Optional

from ..adapters.bitget_client import BitgetClient


DEFAULT_CONTRACT_META_DICT = {
    "priceScale": 1,
    "priceTick": Decimal("0.1"),
    "sizeScale": 6,
    "sizeTick": Decimal("0.000001"),
    "minNotional": None,
    "minSize": None,
}


@dataclass
class ContractMeta:
    symbol: str
    price_scale: int
    size_scale: int
    price_tick: float
    size_tick: float
    min_size: Optional[float] = None

    def quantize_price(self, value: float) -> float:
        tick = _meta_value(self, "priceTick")
        return _quantize_down(value, tick)

    def quantize_size(self, value: float) -> float:
        tick = _meta_value(self, "sizeTick")
        return _quantize_down(value, tick)

    def format_price(self, value: float) -> str:
        quantized = self.quantize_price(value)
        scale = _meta_value(self, "priceScale", numeric=True)
        return _format_with_scale(quantized, scale)

    def format_size(self, value: float) -> str:
        quantized = self.quantize_size(value)
        scale = _meta_value(self, "sizeScale", numeric=True)
        return _format_with_scale(quantized, scale)


def _meta_value(meta: object, key: str, *, numeric: bool = False):
    default_map = {
        "priceTick": DEFAULT_CONTRACT_META_DICT["priceTick"],
        "sizeTick": DEFAULT_CONTRACT_META_DICT["sizeTick"],
        "priceScale": DEFAULT_CONTRACT_META_DICT["priceScale"],
        "sizeScale": DEFAULT_CONTRACT_META_DICT["sizeScale"],
    }
    fallback = default_map.get(key)
    value = None

    if isinstance(meta, dict):
        value = meta.get(key) or meta.get(_to_camel(key))
    else:
        # First try the exact key
        value = getattr(meta, key, None)
        if value is None:
            # Try camelCase version
            value = getattr(meta, _to_camel(key), None)
        if value is None:
            # Try snake_case version for ContractMeta attributes
            snake_key = key.replace("Tick", "_tick").replace("Scale", "_scale")
            value = getattr(meta, snake_key, None)

    if value in (None, ""):
        value = fallback

    if numeric:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default_map.get(key, 0))

    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _quantize_down(value: float, tick: float) -> float:
    if not math.isfinite(value):
        return value
    if tick <= 0:
        return value
    value_dec = Decimal(str(value))
    tick_dec = Decimal(str(tick))
    if tick_dec == 0:
        return value
    steps = (value_dec / tick_dec).to_integral_value(rounding=ROUND_DOWN)
    quantized = steps * tick_dec
    return float(quantized)


def _format_with_scale(value: float, scale: int) -> str:
    decimals = max(0, int(scale))
    return f"{value:.{decimals}f}"


def _to_camel(key: str) -> str:
    if not key:
        return key
    return key[0].lower() + key[1:]


class ContractMetaCache:
    def __init__(self, client: BitgetClient, *, ttl_seconds: float = 60.0) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._meta: Dict[str, ContractMeta] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, symbol: str) -> Optional[ContractMeta]:
        symbol_key = symbol.upper()
        now = time.time()
        if (now - self._fetched_at) > self._ttl:
            await self._refresh(force=True)
        meta = self._meta.get(symbol_key)
        if meta is None:
            await self._refresh(force=True)
            meta = self._meta.get(symbol_key)
        return meta

    async def _refresh(self, *, force: bool = False) -> None:
        async with self._lock:
            now = time.time()
            if not force and (now - self._fetched_at) <= self._ttl:
                return
            try:
                payload = await self._client.list_perp_contracts()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            entries = payload.get("data_list")
            if not isinstance(entries, list):
                return
            meta_map: Dict[str, ContractMeta] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                symbol = entry.get("symbol") or entry.get("symbolName")
                if not isinstance(symbol, str):
                    continue
                symbol_key = symbol.upper()
                price_scale = _extract_scale(entry, ["priceScale", "pricePlace", "priceDigits"])
                size_scale = _extract_scale(entry, ["sizeScale", "sizePlace", "sizeDigits"])
                price_tick = _extract_tick(entry, ["priceTick", "priceStep"])
                size_tick = _extract_tick(entry, ["sizeTick", "sizeStep", "volumeTick"])
                if price_scale is not None and price_tick is None:
                    price_tick = 10 ** (-price_scale)
                if size_scale is not None and size_tick is None:
                    size_tick = 10 ** (-size_scale)
                price_scale = price_scale if price_scale is not None else DEFAULT_CONTRACT_META.price_scale
                size_scale = size_scale if size_scale is not None else DEFAULT_CONTRACT_META.size_scale
                price_tick = price_tick if price_tick is not None else 10 ** (-price_scale)
                size_tick = size_tick if size_tick is not None else 10 ** (-size_scale)
                min_size = (
                    entry.get("minTradeNum")
                    or entry.get("minOrderNum")
                    or entry.get("minTradeAmount")
                    or entry.get("minSize")
                )
                try:
                    min_size_value = float(min_size) if min_size is not None else None
                except (TypeError, ValueError):
                    min_size_value = None
                meta_map[symbol_key] = ContractMeta(
                    symbol=symbol_key,
                    price_scale=price_scale,
                    size_scale=size_scale,
                    price_tick=price_tick,
                    size_tick=size_tick,
                    min_size=min_size_value,
                )
            if meta_map:
                self._meta = meta_map
                self._fetched_at = now


def _extract_scale(entry: Dict[str, object], keys: list[str]) -> Optional[int]:
    for key in keys:
        value = entry.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_tick(entry: Dict[str, object], keys: list[str]) -> Optional[float]:
    for key in keys:
        value = entry.get(key)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            return numeric
    return None
DEFAULT_CONTRACT_META = ContractMeta(
    symbol="DEFAULT",
    price_scale=DEFAULT_CONTRACT_META_DICT["priceScale"],
    size_scale=DEFAULT_CONTRACT_META_DICT["sizeScale"],
    price_tick=float(DEFAULT_CONTRACT_META_DICT["priceTick"]),
    size_tick=float(DEFAULT_CONTRACT_META_DICT["sizeTick"]),
    min_size=DEFAULT_CONTRACT_META_DICT["minSize"],
)
