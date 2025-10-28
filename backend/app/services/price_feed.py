from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set

import httpx

from ..adapters.bitget_client import BitgetClient

logger = logging.getLogger(__name__)


@dataclass
class PriceQuote:
    base: str
    price: float
    source: str  # "perp" | "spot"
    updated_at: datetime
    weight_kg: float


class PriceFeed:
    """Poll Bitget tickers and expose prices/weights keyed by base token."""

    def __init__(
        self,
        client: BitgetClient,
        bases: Iterable[str],
        *,
        interval_seconds: float = 3.0,
        timeout_seconds: float = 2.0,
        max_retries: int = 2,
    ) -> None:
        self._client = client
        self._bases = self._normalize_bases(bases)
        self._interval = float(interval_seconds)
        self._timeout = float(timeout_seconds)
        self._max_retries = max(0, int(max_retries))

        self._quotes: Dict[str, PriceQuote] = {}
        self._healthy: bool = False
        self._last_ts: Optional[datetime] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None

    @staticmethod
    def _normalize_bases(bases: Iterable[str]) -> List[str]:
        seen: Set[str] = set()
        ordered: List[str] = []
        for base in bases:
            base_str = str(base).strip().upper()
            if not base_str or base_str in seen:
                continue
            seen.add(base_str)
            ordered.append(base_str)
        return ordered

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._runner())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _runner(self) -> None:
        try:
            while True:
                await self._poll_once()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("PriceFeed background task stopped unexpectedly")
            raise

    async def _poll_once(self) -> None:
        try:
            updated = await self._refresh()
        except Exception as exc:
            await self._mark_failure(exc)
            return
        logger.debug("PriceFeed poll ok (%d items)", updated)

    async def _mark_failure(self, exc: Exception) -> None:
        error_label = self._format_error(exc)
        async with self._lock:
            if self._last_ts is None:
                self._last_ts = datetime.now(timezone.utc)
            self._healthy = False
        logger.error("PriceFeed poll error (%s): %s", exc.__class__.__name__, error_label)

    async def _refresh(self) -> int:
        now = datetime.now(timezone.utc)
        perp_payload = await self._with_retries(self._client.list_perp_tickers)
        perp_quotes, missing = self._extract_perp_quotes(perp_payload, now)

        quotes = dict(perp_quotes)
        # Hyperliquid only supports perpetuals, no spot markets

        if missing:
            logger.debug("PriceFeed: missing quotes for %s", missing)

        if not quotes:
            raise asyncio.TimeoutError("no quotes collected from ticker payloads")

        async with self._lock:
            self._quotes.update(quotes)
            self._healthy = True
            self._last_ts = now

        return len(quotes)

    async def _with_retries(self, func):
        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt <= self._max_retries:
            try:
                return await asyncio.wait_for(func(), timeout=self._timeout)
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                delay = self._retry_delay(attempt, exc.response.headers)
            except (httpx.RequestError, asyncio.TimeoutError, httpx.TimeoutException) as exc:
                last_exc = exc
                delay = self._retry_delay(attempt, None)
            attempt += 1
            if attempt > self._max_retries:
                break
            if delay > 0:
                await asyncio.sleep(delay)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("retry helper exited unexpectedly")

    @staticmethod
    def _retry_delay(attempt: int, headers) -> float:
        retry_after = None
        if headers is not None:
            retry_value = headers.get("Retry-After")
            if retry_value:
                try:
                    retry_after = float(retry_value)
                except ValueError:
                    retry_after = None
        if retry_after is not None and retry_after >= 0:
            return retry_after
        return 0.5 * (attempt + 1)

    @staticmethod
    def _format_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            return str(exc.response.status_code)
        if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, httpx.RequestError)):
            return "timeout"
        return str(exc)

    def get_price(self, base: str) -> Optional[PriceQuote]:
        base_key = base.strip().upper()
        return self._quotes.get(base_key)

    async def latest_prices(self) -> Dict[str, PriceQuote]:
        async with self._lock:
            return {base: self._quotes[base] for base in self._bases if base in self._quotes}

    async def snapshot(self) -> Dict[str, object]:
        async with self._lock:
            timestamp = int((self._last_ts or datetime.now(timezone.utc)).timestamp() * 1000)
            healthy = self._healthy and bool(self._quotes)
            items = []
            for base in self._bases:
                quote = self._quotes.get(base)
                if not quote:
                    continue
                items.append(
                    {
                        "base": base,
                        "price": quote.price,
                        "source": quote.source,
                        "weightKg": quote.weight_kg,
                    }
                )
        return {"healthy": healthy, "ts": timestamp, "items": items}

    def _extract_perp_quotes(
        self,
        payload: object,
        now: datetime,
    ) -> tuple[Dict[str, PriceQuote], Set[str]]:
        quotes: Dict[str, PriceQuote] = {}
        missing: Set[str] = set(self._bases)
        for entry in self._iter_entries(payload):
            symbol = entry.get("symbol")
            if not isinstance(symbol, str):
                continue
            base = self._base_from_symbol(symbol)
            if base not in missing:
                continue
            price = self._extract_price(entry, key="lastPr")
            if price is None:
                continue
            quotes[base] = self._build_quote(base, price, "perp", now)
            missing.discard(base)
            if not missing:
                break
        return quotes, missing

    @staticmethod
    def _iter_entries(payload: object):
        if not isinstance(payload, dict):
            return []
        entries = payload.get("data_list")
        if not isinstance(entries, list):
            data = payload.get("data")
            if isinstance(data, list):
                entries = data
            else:
                return []
        return [entry for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _base_from_symbol(symbol: str) -> Optional[str]:
        """
        Extract base symbol from market symbol.
        Hyperliquid returns symbols like BTC-USD, ETH-USD.
        Legacy Bitget used BTCUSDT, ETHUSDT format.
        """
        upper = symbol.upper()
        # Hyperliquid: strip -USD suffix (e.g., "BTC-USD" -> "BTC")
        if upper.endswith("-USD"):
            return upper[:-4]
        # Fallback: return as-is for symbols without suffix
        return upper if upper else None

    @staticmethod
    def _extract_price(entry: Dict[str, object], key: str = "lastPr") -> Optional[float]:
        """
        Extract price from ticker entry.
        Hyperliquid uses: lastPr, askPr, bidPr
        Legacy Bitget used: markPrice, close, last, price
        """
        for candidate in (key, "lastPr", "askPr", "bidPr", "markPrice", "close", "last", "price"):
            value = entry.get(candidate)
            if value is None:
                continue
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
        return None

    @staticmethod
    def _compute_weight(price: float) -> float:
        safe_price = max(price, 1e-7)
        raw = 50.0 * (math.log10(safe_price) + 2.0)
        return max(5.0, min(999.0, raw))

    def _build_quote(self, base: str, price: float, source: str, now: datetime) -> PriceQuote:
        weight = self._compute_weight(price)
        return PriceQuote(
            base=base,
            price=price,
            source=source,
            updated_at=now,
            weight_kg=weight,
        )
