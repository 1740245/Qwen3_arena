from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import Settings


logger = logging.getLogger(__name__)


class BitgetClient:
    """Lightweight asynchronous Bitget REST client."""

    def __init__(self, settings: Settings):
        self._settings = settings
        base_url = settings.bitget_base_url.rstrip("/")
        self._auth_client = httpx.AsyncClient(
            base_url=settings.bitget_base_url.rstrip("/"),
            timeout=10.0,
        )
        self._public_client = httpx.AsyncClient(
            base_url=base_url,
            timeout=10.0,
        )
        demo_url = settings.bitget_demo_base_url.rstrip("/") if settings.bitget_demo_base_url else base_url
        self._demo_client = httpx.AsyncClient(
            base_url=demo_url,
            timeout=10.0,
        )
        self._position_mode: Optional[str] = None
        self._position_mode_cached_at: float = 0.0
        self._position_mode_ttl: float = 60.0
        self._last_logged_position_mode: Optional[str] = None
        self._order_tap = deque(maxlen=10)

    async def close(self) -> None:
        await self._auth_client.aclose()
        await self._public_client.aclose()
        await self._demo_client.aclose()

    async def post(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Debug helper: emit logs around Bitget POST calls."""
        body_data = params or {}
        body = json.dumps(body_data, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        api_key = self._settings.bitget_api_key
        api_secret = getattr(self._settings, "bitget_api_secret", None)
        if not api_secret:
            api_secret = getattr(self._settings, "bitget_secret_key", None)
        passphrase = self._settings.bitget_passphrase
        if not all([api_key, api_secret, passphrase]):
            raise RuntimeError("Bitget API credentials are not configured.")

        sign_target = f"{timestamp}POST{path}{body}"
        signature = base64.b64encode(
            hmac.new(api_secret.encode("utf-8"), sign_target.encode("utf-8"), hashlib.sha256).digest()
        ).decode()

        headers = {
            "ACCESS-KEY": api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

        tag = kwargs.get("tag")
        tap_entry = {
            "path": path,
            "body": body_data,
            "tag": tag,
            "timestamp": time.time(),
            "headers": {
                key: (value if key != "ACCESS-KEY" else f"***{value[-4:]}")
                for key, value in headers.items()
                if key != "ACCESS-SIGN"
            },
        }
        logger.debug("Bitget POST %s body=%s headers=%s", path, body, headers)
        client = self._select_client(authenticated=True, use_demo=False)
        try:
            response = await client.post(path, content=body, headers=headers)
            logger.debug("Bitget POST %s status=%s", path, response.status_code)
            response.raise_for_status()
            result = response.json()
            logger.debug("Bitget POST %s response=%s", path, result)
            tap_entry["status"] = response.status_code
            if isinstance(result, dict):
                tap_entry["code"] = result.get("code")
                tap_entry["msg"] = result.get("msg")
            self._order_tap.appendleft(tap_entry)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Bitget POST %s failed status=%s text=%s",
                path,
                exc.response.status_code if exc.response else None,
                exc.response.text if exc.response else None,
            )
            tap_entry["status"] = exc.response.status_code if exc.response else None
            tap_entry["error"] = exc.response.text if exc.response else str(exc)
            self._order_tap.appendleft(tap_entry)
            raise

        if isinstance(result, dict) and result.get("code") not in (None, "00000", "0", "success", "SUCCESS"):
            raise RuntimeError(f"API Error: {result.get('msg', 'Unknown error')}")

        return result

    def get_recent_order_tap(self) -> List[Dict[str, Any]]:
        return list(self._order_tap)

    async def __aenter__(self) -> "BitgetClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def place_spot_order(self, payload: Dict[str, Any], *, demo_mode: bool = False) -> Dict[str, Any]:
        if demo_mode and not self._settings.has_api_credentials():
            return self._simulate_order(payload, route="spot")
        return await self._request(
            "POST",
            "/api/v2/spot/trade/place-order",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def cancel_spot_order(self, order_id: str, symbol: str, *, demo_mode: bool = False) -> Dict[str, Any]:
        if demo_mode and not self._settings.has_api_credentials():
            return self._wrap_data({"orderId": order_id, "status": "cancelled"})
        body = {"orderId": order_id, "symbol": symbol}
        return await self._request(
            "POST",
            "/api/v2/spot/trade/cancel-order",
            json_payload=body,
            use_demo=demo_mode,
        )

    async def list_open_spot_orders(self, symbol: Optional[str] = None, *, demo_mode: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        if demo_mode and not self._settings.has_api_credentials():
            return {
                "ok": True,
                "code": "00000",
                "msg": "",
                "raw": {"data": []},
                "data_obj": None,
                "data_list": [],
                "data": [],
            }
        return await self._request(
            "GET",
            "/api/v2/spot/trade/open-orders",
            params=params,
            use_demo=demo_mode,
        )

    async def list_balances(self) -> Dict[str, Any]:
        return await self.fetch_energy_usdt()

    async def list_fills(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"symbol": symbol} if symbol else {}
        return await self._request(
            "GET",
            "/api/v2/spot/trade/fills",
            params=params,
        )

    async def list_perp_fills(self, symbol: Optional[str] = None, *, demo_mode: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {"productType": "usdt-futures"}
        if symbol:
            params["symbol"] = symbol
        return await self._request(
            "GET",
            "/api/v2/mix/order/fills",
            params=params,
            use_demo=demo_mode,
        )

    async def list_perp_positions(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        """Fetch all perpetual positions for the given product type."""
        params = {"productType": product_type}
        return await self._request(
            "GET",
            "/api/v2/mix/position/all-position",
            params=params,
            use_demo=demo_mode,
        )

    async def get_position_single(
        self,
        symbol: str,
        *,
        product_type: str = "USDT-FUTURES",
        margin_coin: str = "USDT",
    ) -> Dict[str, Any]:
        params = {
            "productType": product_type,
            "symbol": symbol.upper(),
            "marginCoin": margin_coin,
        }
        payload = await self._request(
            "GET",
            "/api/v2/mix/position/single-position",
            params=params,
        )
        return payload.get("raw", payload)

    async def get_position_all(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        margin_coin: str = "USDT",
    ) -> Dict[str, Any]:
        params = {
            "productType": product_type,
            "marginCoin": margin_coin,
        }
        payload = await self._request(
            "GET",
            "/api/v2/mix/position/all-position",
            params=params,
        )
        return payload.get("raw", payload)

    async def read_single_position(
        self,
        symbol: str,
        *,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        if not self._settings.has_api_credentials():
            return {
                "ok": False,
                "status": None,
                "error": "Bitget API credentials are not configured.",
                "entries": [],
                "params": {},
            }

        try:
            payload = await self.get_position_single(symbol, product_type=product_type)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            status = response.status_code if response else None
            text = response.text if response else str(exc)
            return {
                "ok": False,
                "status": status,
                "error": text,
                "entries": [],
                "payload": None,
                "params": {
                    "productType": product_type,
                    "symbol": symbol.upper(),
                    "marginCoin": "USDT",
                },
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": None,
                "error": str(exc),
                "entries": [],
                "params": {
                    "productType": product_type,
                    "symbol": symbol.upper(),
                    "marginCoin": "USDT",
                },
            }

        entries = self._extract_position_entries(payload)
        return {
            "ok": True,
            "status": 200,
            "entries": entries,
            "payload": payload,
            "params": {
                "productType": product_type,
                "symbol": symbol.upper(),
                "marginCoin": "USDT",
            },
        }

    async def read_all_positions(
        self,
        *,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        if not self._settings.has_api_credentials():
            return {
                "ok": False,
                "status": None,
                "error": "Bitget API credentials are not configured.",
                "entries": [],
                "params": {},
            }

        try:
            payload = await self.get_position_all(product_type=product_type)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            status = response.status_code if response else None
            text = response.text if response else str(exc)
            return {
                "ok": False,
                "status": status,
                "error": text,
                "entries": [],
                "payload": None,
                "params": {
                    "productType": product_type,
                    "marginCoin": "USDT",
                },
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": None,
                "error": str(exc),
                "entries": [],
                "params": {
                    "productType": product_type,
                    "marginCoin": "USDT",
                },
            }

        entries = self._extract_position_entries(payload)
        return {
            "ok": True,
            "status": 200,
            "entries": entries,
            "payload": payload,
            "params": {
                "productType": product_type,
                "marginCoin": "USDT",
            },
        }

    async def list_open_perp_orders(
        self,
        symbol: Optional[str] = None,
        *,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"productType": "USDT-FUTURES"}
        if symbol:
            params["symbol"] = symbol
        if demo_mode and not self._settings.has_api_credentials():
            return {
                "ok": True,
                "code": "00000",
                "msg": "",
                "raw": {"data": []},
                "data_obj": None,
                "data_list": [],
                "data": [],
            }
        return await self._request(
            "GET",
            "/api/v2/mix/order/current",
            params=params,
            use_demo=demo_mode,
        )

    async def list_pending_perp_orders(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"productType": product_type}
        return await self._request(
            "GET",
            "/api/v2/mix/order/orders-pending",
            params=params,
            use_demo=demo_mode,
        )

    async def list_pending_perp_plan_orders(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"productType": product_type, "pageSize": 100, "pageNo": 1}
        return await self._request(
            "GET",
            "/api/v2/mix/order/orders-plan-pending",
            params=params,
            use_demo=demo_mode,
        )

    async def fetch_pending_perp_orders_raw(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        demo_mode: bool = False,
    ) -> Any:
        params: Dict[str, Any] = {"productType": product_type}
        try:
            payload = await self._request(
                "GET",
                "/api/v2/mix/order/orders-pending",
                params=params,
                use_demo=demo_mode,
            )
            return payload.get("raw", payload)
        except Exception as exc:
            return {"error": str(exc)}

    async def get_mix_orders_pending(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        symbol: Optional[str] = None,
        page_size: int = 100,
        page_no: int = 1,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "productType": product_type,
            "pageSize": page_size,
            "pageNo": page_no,
        }
        if symbol:
            params["symbol"] = symbol.upper()

        payload = await self._request(
            "GET",
            "/api/v2/mix/order/orders-pending",
            params=params,
            use_demo=demo_mode,
        )
        return payload.get("raw", payload)

    async def fetch_working_orders_v2(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        symbol: Optional[str] = None,
        page_size: int = 100,
        page_no: int = 1,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        if not self._settings.has_api_credentials():
            params = {
                "productType": product_type,
                "pageSize": page_size,
                "pageNo": page_no,
            }
            if symbol:
                params["symbol"] = symbol.upper()
            return {"error": "Bitget API credentials are not configured.", "params": params}

        try:
            return await self.get_mix_orders_pending(
                product_type=product_type,
                symbol=symbol,
                page_size=page_size,
                page_no=page_no,
                demo_mode=demo_mode,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response else None
            text = exc.response.text if exc.response else ""
            message = f"HTTP {status}: {text}".strip()
            params: Dict[str, Any] = {
                "productType": product_type,
                "pageSize": page_size,
                "pageNo": page_no,
            }
            if symbol:
                params["symbol"] = symbol.upper()
            return {"error": message, "params": params}
        except Exception as exc:
            params = {
                "productType": product_type,
                "pageSize": page_size,
                "pageNo": page_no,
            }
            if symbol:
                params["symbol"] = symbol.upper()
            return {"error": str(exc), "params": params}

    async def probe_working_orders(self, symbol: Optional[str]) -> List[Dict[str, Any]]:
        attempts = [
            {"label": "v2:orders-pending", "path": "/api/v2/mix/order/orders-pending"},
            {"label": "v2:current", "path": "/api/v2/mix/order/current"},
            {"label": "v2:pending-orders", "path": "/api/v2/mix/order/pending-orders"},
            {"label": "v1:orders-pending", "path": "/api/mix/v1/order/orders-pending"},
            {"label": "v1:pending-orders", "path": "/api/mix/v1/order/pending-orders"},
        ]

        normalized_symbol = symbol.upper() if isinstance(symbol, str) else ""
        filter_targets = set()
        if normalized_symbol:
            stripped = normalized_symbol.split("_", 1)[0]
            base = stripped[:-4] if stripped.endswith("USDT") else stripped
            if base:
                filter_targets.add(f"{base}USDT")
                filter_targets.add(f"{base}USDT_UMCBL")

        def build_params(include_symbol: bool) -> Dict[str, Any]:
            params: Dict[str, Any] = {
                "productType": "USDT-FUTURES",
                "pageSize": 100,
                "pageNo": 1,
            }
            if include_symbol and normalized_symbol:
                params["symbol"] = normalized_symbol
            return params

        if not self._settings.has_api_credentials():
            results: List[Dict[str, Any]] = []
            for attempt in attempts:
                params = build_params(include_symbol=True)
                results.append(
                    {
                        "label": attempt["label"],
                        "path": attempt["path"],
                        "params": params,
                        "status": None,
                        "topKeys": [],
                        "listName": "none",
                        "listLen": 0,
                        "firstKeys": [],
                        "error": "Bitget API credentials are not configured.",
                    }
                )
            return results

        async def perform(path: str, params: Dict[str, Any]) -> Tuple[Optional[Any], Optional[int], Optional[str]]:
            try:
                payload = await self._request(
                    "GET",
                    path,
                    params=params,
                )
                payload_raw = payload.get("raw", payload)
                return payload_raw, 200, None
            except httpx.HTTPStatusError as exc:
                response = exc.response
                status = response.status_code if response else None
                text = response.text if response else str(exc)
                try:
                    payload = response.json() if response is not None else None
                except ValueError:
                    payload = None
                return payload, status, text
            except Exception as exc:
                return None, None, str(exc)

        results: List[Dict[str, Any]] = []
        for attempt in attempts:
            label = attempt["label"]
            path = attempt["path"]
            initial_params = build_params(include_symbol=True)
            summary: Dict[str, Any] = {
                "label": label,
                "path": path,
                "params": dict(initial_params),
                "status": None,
                "topKeys": [],
                "listName": "none",
                "listLen": 0,
                "firstKeys": [],
            }

            payload, status_code, error_message = await perform(path, initial_params)
            summary["params"] = dict(initial_params)
            summary["status"] = status_code

            fallback_used = False
            if (
                normalized_symbol
                and status_code == 400
            ):
                fallback_params = build_params(include_symbol=False)
                fallback_payload, fallback_status, fallback_error = await perform(path, fallback_params)
                fallback_used = True
                summary["initialStatus"] = status_code
                summary["initialParams"] = dict(initial_params)
                summary["params"] = dict(fallback_params)
                summary["status"] = fallback_status
                payload = fallback_payload
                error_message = fallback_error

            if status_code is None and error_message:
                summary["error"] = error_message
                results.append(summary)
                continue

            if summary.get("status") and summary["status"] >= 400:
                if error_message:
                    summary["error"] = error_message
                results.append(summary)
                continue

            if error_message and payload is None:
                summary.setdefault("error", error_message)

            if isinstance(payload, dict):
                summary["topKeys"] = list(payload.keys())[:15]
            elif isinstance(payload, list):
                summary["topKeys"] = []

            list_name = "none"
            entries: List[Dict[str, Any]] = []
            if isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, dict):
                    entrusted = data.get("entrustedList")
                    if isinstance(entrusted, list):
                        list_name = "entrustedList"
                        entries = [row for row in entrusted if isinstance(row, dict)]
                    else:
                        nested_list = data.get("list")
                        if isinstance(nested_list, list):
                            list_name = "list"
                            entries = [row for row in nested_list if isinstance(row, dict)]
                elif isinstance(data, list):
                    list_name = "data"
                    entries = [row for row in data if isinstance(row, dict)]
            elif isinstance(payload, list):
                list_name = "list"
                entries = [row for row in payload if isinstance(row, dict)]

            if not entries and payload is not None:
                entries = self._parse_mix_entries(payload)

            if fallback_used and filter_targets:
                entries = [
                    row
                    for row in entries
                    if isinstance(row.get("symbol"), str)
                    and row["symbol"].upper() in filter_targets
                ]

            summary["listName"] = list_name
            summary["listLen"] = len(entries)
            if entries:
                first_entry = entries[0]
                if isinstance(first_entry, dict):
                    summary["firstKeys"] = list(first_entry.keys())[:15]
                else:
                    summary["firstKeys"] = []
            else:
                summary["firstKeys"] = []

            results.append(summary)

        return results

    async def _mix_orders_pending_v2(
        self,
        params: Dict[str, Any],
        *,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        try:
            payload = await self._request(
                "GET",
                "/api/v2/mix/order/orders-pending",
                params=params,
                use_demo=demo_mode,
            )
            return payload.get("raw", payload)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response else None
            text = exc.response.text if exc.response else str(exc)
            return {"error": text, "status": status, "params": params}
        except Exception as exc:
            return {"error": str(exc), "params": params}

    async def _mix_orders_pending_v2_probe(
        self,
        attempts: List[Dict[str, Any]],
        *,
        demo_mode: bool = False,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for attempt in attempts:
            params = attempt.get("params", {}) or {}
            path = attempt.get("path") or "/api/v2/mix/order/orders-pending"
            label = attempt.get("label") or "unknown"
            summary: Dict[str, Any] = {
                "label": label,
                "params": params,
                "status": None,
                "topKeys": [],
                "listName": "none",
                "listLen": 0,
                "firstKeys": [],
            }
            try:
                payload = await self._request(
                    "GET",
                    path,
                    params=params,
                    use_demo=demo_mode,
                )
                summary["status"] = 200
            except httpx.HTTPStatusError as exc:
                response = exc.response
                status = response.status_code if response else None
                summary["status"] = status
                summary["error"] = response.text if response else str(exc)
                try:
                    payload = response.json() if response is not None else None
                except ValueError:
                    payload = None
                results.append(summary)
                continue
            except Exception as exc:
                summary["error"] = str(exc)
                results.append(summary)
                continue

            if isinstance(payload, dict):
                summary["topKeys"] = sorted(payload.keys())
            entries = self._parse_mix_entries(payload)
            list_name = "none"
            if isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, dict):
                    for candidate in ("entrustedList", "orderInfoList", "list"):
                        block = data.get(candidate)
                        if isinstance(block, list):
                            list_name = candidate
                            break
                elif isinstance(data, list):
                    list_name = "data"
            elif isinstance(payload, list):
                list_name = "root"

            summary["listName"] = list_name
            summary["listLen"] = len(entries)
            first = entries[0] if entries else None
            if isinstance(first, dict):
                summary["firstKeys"] = list(first.keys())[:15]
                summary["firstRow"] = {k: first.get(k) for k in summary["firstKeys"]}
            results.append(summary)

        return results

    async def get_mix_orders_plan_pending(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        symbol: Optional[str] = None,
        plan_type: Optional[str] = None,
        margin_coin: Optional[str] = None,
        page_size: int = 100,
        page_no: int = 1,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "productType": product_type,
            "pageSize": page_size,
            "pageNo": page_no,
        }
        if symbol:
            params["symbol"] = symbol.upper()
        if plan_type:
            params["planType"] = plan_type
        if margin_coin:
            params["marginCoin"] = margin_coin
        try:
            payload = await self._request(
                "GET",
                "/api/v2/mix/order/orders-plan-pending",
                params=params,
                use_demo=demo_mode,
            )
            return payload.get("raw", payload)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            if response is not None and response.status_code == 400:
                results: Dict[str, Any] = {
                    "ok": True,
                    "code": "",
                    "msg": "",
                    "data": [],
                }
                plan_types = ["normal_plan", "profit_plan", "loss_plan"]
                for plan_type in plan_types:
                    params_with_plan = dict(params)
                    params_with_plan["planType"] = plan_type
                    try:
                        partial = await self._request(
                            "GET",
                            "/api/v2/mix/order/orders-plan-pending",
                            params=params_with_plan,
                            use_demo=demo_mode,
                        )
                    except httpx.HTTPStatusError:
                        continue
                    partial_raw = partial.get("raw", partial) if isinstance(partial, dict) else partial
                    data = partial_raw.get("data") if isinstance(partial_raw, dict) else None
                    if isinstance(data, list):
                        results.setdefault("data", []).extend(data)
                return results
            raise

    async def list_spot_tickers(self) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v2/spot/market/tickers",
            authenticated=False,
        )

    async def list_perp_tickers(self) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v2/mix/market/tickers",
            params={"productType": "usdt-futures"},
            authenticated=False,
        )

    async def list_perp_contracts(self) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v2/mix/market/contracts",
            params={"productType": "usdt-futures"},
            authenticated=False,
        )

    async def get_perp_contract(self, symbol: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v2/mix/market/contracts",
            params={"productType": "usdt-futures", "symbol": symbol},
            authenticated=False,
        )

    async def get_perp_account_raw(self) -> Dict[str, Any]:
        """Return raw Bitget v2 mix account payload for USDT-FUTURES."""
        return await self._request(
            "GET",
            "/api/v2/mix/account/account",
            params={"productType": "USDT-FUTURES", "marginCoin": "USDT"},
        )

    async def get_perp_accounts_raw(self) -> Dict[str, Any]:
        """Return raw Bitget v2 mix accounts payload for USDT-FUTURES."""
        return await self._request(
            "GET",
            "/api/v2/mix/account/accounts",
            params={"productType": "USDT-FUTURES"},
        )

    @property
    def position_mode(self) -> Optional[str]:
        return self._position_mode

    async def get_position_mode(self, product_type: str = "USDT-FUTURES") -> Optional[str]:
        now = time.time()
        if self._position_mode_cached_at and (now - self._position_mode_cached_at) < self._position_mode_ttl:
            return self._position_mode

        if not self._settings.has_api_credentials():
            self._position_mode = None
            self._position_mode_cached_at = now
            return None

        params = {"productType": product_type}
        mode: Optional[str] = None
        try:
            mode = await self._request_position_mode("/api/v2/mix/account/accounts", params)
            if mode is None:
                mode = await self._request_position_mode("/api/mix/v1/account/accounts", params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            mode = await self._request_position_mode("/api/mix/v1/account/accounts", params)

        self._position_mode = mode
        self._position_mode_cached_at = now
        if mode and mode != self._last_logged_position_mode:
            logger.info("Perp position mode: %s", mode)
            self._last_logged_position_mode = mode
        return mode

    async def place_perp_order(self, payload: Dict[str, Any], *, demo_mode: bool = False) -> Dict[str, Any]:

        if demo_mode and not self._settings.has_api_credentials():
            return self._simulate_order(payload, route="perp")
        return await self._request(
            "POST",
            "/api/v2/mix/order/place-order",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def place_perp_stop_loss(
        self, payload: Dict[str, Any], *, demo_mode: bool = False
    ) -> Dict[str, Any]:
        if demo_mode and not self._settings.has_api_credentials():
            return self._wrap_data(
                {
                    "tpslId": str(uuid.uuid4()),
                    "status": "success",
                }
            )
        return await self._request(
            "POST",
            "/api/v2/mix/order/place-pos-tpsl",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def close_perp_positions(
        self, payload: Dict[str, Any], *, demo_mode: bool = False
    ) -> Dict[str, Any]:
        if demo_mode and not self._settings.has_api_credentials():
            return self._wrap_data({"status": "success", "symbol": payload.get("symbol")})
        return await self._request(
            "POST",
            "/api/v2/mix/order/close-positions",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def cancel_perp_plan_order(
        self, payload: Dict[str, Any], *, demo_mode: bool = False
    ) -> Dict[str, Any]:
        if demo_mode and not self._settings.has_api_credentials():
            return self._wrap_data({"status": "cancelled", "symbol": payload.get("symbol")})
        return await self._request(
            "POST",
            "/api/v2/mix/order/cancel-plan-order",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def cancel_plan_order_v2(
        self,
        *,
        symbol: str,
        order_id: str,
        plan_type: Optional[str] = None,
        product_type: str = "USDT-FUTURES",
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "productType": product_type,
            "symbol": symbol,
            "planOrderId": order_id,
        }
        if plan_type:
            payload["planType"] = plan_type
        return await self._request(
            "POST",
            "/api/v2/mix/order/cancel-plan-order",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def cancel_mix_order(
        self,
        *,
        symbol: str,
        order_id: str,
        product_type: str = "USDT-FUTURES",
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        normalized_symbol = symbol.upper()
        payload: Dict[str, Any] = {
            "productType": product_type,
            "symbol": normalized_symbol,
            "orderId": order_id,
        }
        response = await self._request(
            "POST",
            "/api/v2/mix/order/cancel-order",
            json_payload=payload,
            use_demo=demo_mode,
        )
        ok = bool(response.get("ok"))
        if not ok:
            code = response.get("code")
            if code is not None and str(code).strip().lower() in {"00000", "0", "success", "true"}:
                ok = True
            elif isinstance(response.get("raw"), dict):
                raw_code = response["raw"].get("code")
                if raw_code is not None and str(raw_code).strip().lower() in {"00000", "0", "success", "true"}:
                    ok = True
        response["ok"] = ok
        response["symbol"] = normalized_symbol
        response["orderId"] = order_id
        return response

    async def cancel_perp_stop_loss(
        self, payload: Dict[str, Any], *, demo_mode: bool = False
    ) -> Dict[str, Any]:
        if demo_mode and not self._settings.has_api_credentials():
            return self._wrap_data({"status": "cancelled", "symbol": payload.get("symbol")})
        return await self._request(
            "POST",
            "/api/v2/mix/order/cancel-pos-tpsl",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def cancel_tpsl_order(
        self,
        *,
        symbol: str,
        order_id: str,
        product_type: str = "USDT-FUTURES",
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        payload = {
            "productType": product_type,
            "symbol": symbol,
            "orderId": order_id,
        }
        return await self._request(
            "POST",
            "/api/v2/mix/order/cancel-tpsl-order",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def cancel_tpsl_order_v1(
        self,
        *,
        symbol: str,
        order_id: str,
        product_type: str = "USDT-FUTURES",
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        payload = {
            "productType": product_type,
            "symbol": symbol,
            "orderId": order_id,
        }
        return await self._request(
            "POST",
            "/api/mix/v1/order/cancel-tpsl-order",
            json_payload=payload,
            use_demo=demo_mode,
        )

    @staticmethod
    def _parse_mix_entries(payload: Any) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                entrusted = data.get("entrustedList")
                if isinstance(entrusted, list):
                    entries.extend([item for item in entrusted if isinstance(item, dict)])
                list_field = data.get("list")
                if isinstance(list_field, list):
                    entries.extend([item for item in list_field if isinstance(item, dict)])
            elif isinstance(data, list):
                entries.extend([item for item in data if isinstance(item, dict)])
            data_list = payload.get("data_list")
            if isinstance(data_list, list):
                entries.extend([item for item in data_list if isinstance(item, dict)])
            list_root = payload.get("list")
            if isinstance(list_root, list):
                entries.extend([item for item in list_root if isinstance(item, dict)])
        elif isinstance(payload, list):
            entries.extend([item for item in payload if isinstance(item, dict)])
        return entries

    async def list_symbol_plan_orders_safe(
        self,
        symbol: str,
        *,
        product_type: str = "USDT-FUTURES",
        page_size: int = 100,
        page_no: int = 1,
        plan_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_symbol = symbol.upper()
        filter_symbol = False

        try:
            payload = await self.get_mix_orders_plan_pending(
                product_type=product_type,
                symbol=normalized_symbol,
                plan_type=plan_type,
                margin_coin="USDT",
                page_size=page_size,
                page_no=page_no,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response else None
            text = exc.response.text if exc.response else ""
            if status == 400 or (isinstance(text, str) and "parameter" in text.lower()):
                try:
                    payload = await self.get_mix_orders_plan_pending(
                        product_type=product_type,
                        symbol=None,
                        plan_type=plan_type,
                        margin_coin="USDT",
                        page_size=page_size,
                        page_no=page_no,
                    )
                    filter_symbol = True
                except Exception:
                    return []
            else:
                return []
        except Exception:
            return []

        entries = self._parse_mix_entries(payload)
        stripped_symbol = normalized_symbol.split("_", 1)[0]
        if filter_symbol or stripped_symbol:
            entries = [
                item
                for item in entries
                if isinstance(item.get("symbol"), str)
                and item.get("symbol").upper().split("_", 1)[0] == stripped_symbol
            ]
        return entries

    async def get_mix_tpsl_open(
        self,
        symbol: Optional[str],
        *,
        product_type: str = "USDT-FUTURES",
        margin_coin: str = "USDT",
    ) -> Tuple[Any, bool]:
        symbol_upper = symbol.upper() if isinstance(symbol, str) else None
        base_params = {"productType": product_type, "marginCoin": margin_coin}

        async def attempt(path: str, include_symbol: bool) -> Dict[str, Any]:
            params = dict(base_params)
            if include_symbol and symbol_upper:
                params["symbol"] = symbol_upper
            payload = await self._request("GET", path, params=params)
            return payload.get("raw", payload)

        last_error: Optional[Exception] = None
        for path in ("/api/v2/mix/order/tpsl-order-list", "/api/v2/mix/order/tpsl-open-orders"):
            include_options = [True, False] if symbol_upper else [False]
            for include_symbol in include_options:
                try:
                    payload = await attempt(path, include_symbol)
                    needs_filter = include_symbol is False and symbol_upper is not None
                    return payload, needs_filter
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response else None
                    if include_symbol and status in (400, 404):
                        last_error = exc
                        continue
                    if status in (400, 404):
                        last_error = exc
                        break
                    raise

        for include_symbol in ([True, False] if symbol_upper else [False]):
            try:
                payload = await attempt("/api/mix/v1/order/orders-tpsl-open", include_symbol)
                needs_filter = include_symbol is False and symbol_upper is not None
                return payload, needs_filter
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else None
                if include_symbol and status in (400, 404):
                    last_error = exc
                    continue
                raise

        if last_error:
            raise last_error
        raise RuntimeError("Unable to fetch TPSL orders.")

    async def list_symbol_tpsl_orders_safe(
        self,
        symbol: str,
        *,
        product_type: str = "USDT-FUTURES",
    ) -> List[Dict[str, Any]]:
        normalized_symbol = symbol.upper()

        try:
            payload, filter_symbol = await self.get_mix_tpsl_open(
                normalized_symbol,
                product_type=product_type,
                margin_coin="USDT",
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response else None
            if status in (400, 404):
                return []
            return []
        except Exception:
            return []

        entries = self._parse_mix_entries(payload)
        stripped_symbol = normalized_symbol.split("_", 1)[0]
        if filter_symbol or stripped_symbol:
            entries = [
                item
                for item in entries
                if isinstance(item.get("symbol"), str)
                and (
                    item.get("symbol").upper() == normalized_symbol
                    or item.get("symbol").upper().split("_", 1)[0] == stripped_symbol
                )
            ]
        return entries

    async def list_symbol_tpsl_orders_safe_v1(
        self,
        symbol: str,
        *,
        product_type: str = "USDT-FUTURES",
    ) -> List[Dict[str, Any]]:
        normalized_symbol = symbol.upper()
        params_symbol = {
            "productType": product_type,
            "symbol": normalized_symbol,
            "marginCoin": "USDT",
        }
        params_no_symbol = {
            "productType": product_type,
            "marginCoin": "USDT",
        }
        try:
            wrapper = await self._request(
                "GET",
                "/api/mix/v1/order/orders-tpsl-open",
                params=params_symbol,
            )
            payload = wrapper.get("raw", wrapper)
            filter_symbol = False
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response else None
            if status not in (400, 404):
                return []
            try:
                wrapper = await self._request(
                    "GET",
                    "/api/mix/v1/order/orders-tpsl-open",
                    params=params_no_symbol,
                )
                payload = wrapper.get("raw", wrapper)
                filter_symbol = True
            except Exception:
                return []
        except Exception:
            return []

        entries = self._parse_mix_entries(payload)
        stripped_symbol = normalized_symbol.split("_", 1)[0]
        if filter_symbol or stripped_symbol:
            entries = [
                item
                for item in entries
                if isinstance(item.get("symbol"), str)
                and item.get("symbol").upper().split("_", 1)[0] == stripped_symbol
            ]
        return entries

    @staticmethod
    def _extract_position_entries(payload: Any) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                entries.append(data)
            elif isinstance(data, list):
                entries.extend([item for item in data if isinstance(item, dict)])
        elif isinstance(payload, list):
            entries.extend([item for item in payload if isinstance(item, dict)])
        return entries

    async def cancel_all_orders_by_symbol(
        self,
        symbol: str,
        *,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        normalized = (symbol or "").upper()
        if demo_mode and not self._settings.has_api_credentials():
            return {
                "ok": True,
                "code": "00000",
                "msg": "",
                "symbol": normalized,
                "attemptedSymbols": [normalized],
            }

        candidates: List[str] = []
        if normalized:
            candidates.append(normalized)
            if normalized.endswith("_UMCBL"):
                base = normalized[:-6]
                if base:
                    candidates.append(base)
            else:
                candidates.append(f"{normalized}_UMCBL")
            if "_" in normalized:
                base_prefix = normalized.split("_", 1)[0]
                candidates.append(base_prefix)

        # Remove duplicates while preserving order
        seen = set()
        ordered_candidates: List[str] = []
        for candidate in candidates:
            if candidate and candidate not in seen:
                ordered_candidates.append(candidate)
                seen.add(candidate)

        attempted: List[str] = []
        last_response: Optional[Dict[str, Any]] = None

        for candidate in ordered_candidates or [normalized]:
            payload = {"productType": "USDT-FUTURES", "symbol": candidate}
            response = await self._request(
                "POST",
                "/api/v2/mix/order/cancel-all-orders",
                json_payload=payload,
                use_demo=demo_mode,
            )
            attempted.append(candidate)

            ok = bool(response.get("ok"))
            if not ok:
                code = response.get("code")
                if code is not None and str(code).strip().lower() in {"00000", "0", "success", "success"}:
                    ok = True
                elif isinstance(response.get("raw"), dict):
                    raw_code = response["raw"].get("code")
                    if raw_code is not None and str(raw_code).strip().lower() in {"00000", "0", "success", "success"}:
                        ok = True
            response["ok"] = ok
            response["symbol"] = candidate
            response["attemptedSymbols"] = list(attempted)

            if ok:
                return response

            last_response = response

        if last_response is None:
            last_response = {
                "ok": False,
                "code": None,
                "msg": "",
                "symbol": normalized,
                "attemptedSymbols": list(attempted) or [normalized],
            }

        return last_response

    async def cancel_all_working_orders(
        self,
        symbol: str,
        *,
        product_type: str = "USDT-FUTURES",
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        payload = {"productType": product_type, "symbol": symbol}
        return await self._request(
            "POST",
            "/api/v2/mix/order/cancel-all-orders",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def place_spot_stop_loss(
        self, payload: Dict[str, Any], *, demo_mode: bool = False
    ) -> Dict[str, Any]:
        if demo_mode and not self._settings.has_api_credentials():
            return self._wrap_data(
                {
                    "planOrderId": str(uuid.uuid4()),
                    "status": "created",
                }
            )
        return await self._request(
            "POST",
            "/api/v2/spot/trade/place-plan-order",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def cancel_spot_plan_order(
        self, payload: Dict[str, Any], *, demo_mode: bool = False
    ) -> Dict[str, Any]:
        if demo_mode and not self._settings.has_api_credentials():
            return self._wrap_data({"status": "cancelled", "symbol": payload.get("symbol")})
        return await self._request(
            "POST",
            "/api/v2/spot/trade/cancel-plan-order",
            json_payload=payload,
            use_demo=demo_mode,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_payload: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
        use_demo: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        client = self._select_client(authenticated=authenticated, use_demo=use_demo)
        params, json_payload = self._ensure_mix_product_type(path, params, json_payload)

        body = json.dumps(json_payload) if json_payload else ""
        query = str(httpx.QueryParams(params)) if params else ""
        path_with_query = f"{path}?{query}" if query else path

        headers: Dict[str, str] = {}
        content = body if body else None

        if authenticated:
            if not self._settings.has_api_credentials():
                raise RuntimeError("Bitget API credentials are not configured.")
            timestamp = str(int(time.time() * 1000))
            sign_target = f"{timestamp}{method.upper()}{path_with_query}{body}"
            signature = base64.b64encode(
                hmac.new(
                    self._settings.bitget_api_secret.encode(),
                    sign_target.encode(),
                    hashlib.sha256,
                ).digest()
            ).decode()
            headers = {
                "ACCESS-KEY": self._settings.bitget_api_key,
                "ACCESS-SIGN": signature,
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-PASSPHRASE": self._settings.bitget_passphrase,
            }
            if body:
                headers["Content-Type"] = "application/json"

        normalized_path = path.lower()
        if "/mix/order" in normalized_path:
            if isinstance(json_payload, dict):
                logger.info(
                    "Mix order request %s keys: %s",
                    path,
                    ", ".join(sorted(json_payload.keys())),
                )
            elif isinstance(params, dict):
                logger.info(
                    "Mix order request %s keys: %s",
                    path,
                    ", ".join(sorted(params.keys())),
                )

        response = await client.request(
            method,
            path,
            params=params or None,
            content=content,
            headers=headers or None,
            timeout=timeout,
        )
        response.raise_for_status()
        return self._parse_json(response)


    def _select_client(self, *, authenticated: bool, use_demo: bool) -> httpx.AsyncClient:
        if not authenticated:
            return self._public_client
        if use_demo:
            return self._demo_client
        return self._auth_client

    @staticmethod
    def _simulate_order(payload: Dict[str, Any], *, route: str) -> Dict[str, Any]:
        data = {
            "orderId": str(uuid.uuid4()),
            "status": "filled",
            "symbol": payload.get("symbol"),
            "route": route,
            "price": payload.get("price"),
            "size": payload.get("size"),
            "holdSide": payload.get("holdSide"),
        }
        response = BitgetClient._wrap_data(data)
        response["msg"] = "Simulated order."
        response["code"] = "00000"
        return response

    @staticmethod
    def _wrap_data(data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ok": True,
            "code": "00000",
            "msg": "",
            "raw": {"data": data},
            "data_obj": data,
            "data_list": [data],
            "data": data,
        }

    async def fetch_energy_usdt(self) -> Dict[str, Any]:
        result = self._empty_energy_summary()
        available_components: List[float] = []
        total_components: List[float] = []

        if not self._settings.has_api_credentials():
            return result

        try:
            perp_available, perp_total, perp_source = await self._probe_perp_energy()
            if perp_available is not None:
                result["perp"] = float(perp_available)
                available_components.append(float(perp_available))
                result["sources"]["perp"] = perp_source
            if perp_total is not None:
                result["perp_total"] = float(perp_total)
                total_components.append(float(perp_total))
        except Exception as exc:  # pragma: no cover - network safeguards
            logger.warning("Energy perp probe failed: %s", exc)

        try:
            spot_available, spot_total, spot_source = await self._probe_spot_energy()
            if spot_available is not None:
                result["spot"] = float(spot_available)
                available_components.append(float(spot_available))
                result["sources"]["spot"] = spot_source
            if spot_total is not None:
                result["spot_total"] = float(spot_total)
                total_components.append(float(spot_total))
        except Exception as exc:  # pragma: no cover - network safeguards
            logger.warning("Energy spot probe failed: %s", exc)

        if total_components:
            result["total"] = sum(total_components)
        elif available_components:
            result["total"] = sum(available_components)

        if available_components:
            result["available"] = sum(available_components)
        elif result.get("total") is not None:
            result["available"] = float(result["total"])
        else:
            result["available"] = None

        source_label = self._combined_source_label(result)
        if result["total"] is not None:
            logger.info(
                "Energy fetch ok (perp=%.2f spot=%.2f source=%s)",
                result["perp"] if result["perp"] is not None else 0.0,
                result["spot"] if result["spot"] is not None else 0.0,
                source_label,
            )
        return result

    async def get_usdtm_energy(self) -> Dict[str, float]:
        if not self._settings.has_api_credentials():
            return {"available": 0.0, "total": 0.0, "source": "none"}

        strategies = [
            (self._fetch_usdtm_account_detail, "mix.account"),
            (self._fetch_usdtm_account_list, "mix.accounts"),
            (self._fetch_usdtm_funding_assets, "funding"),
        ]

        total_hint: Optional[float] = None

        for fetcher, label in strategies:
            try:
                available_raw, total_raw = await fetcher()
            except Exception as exc:  # pragma: no cover - network guard
                logger.debug("USDT-M energy %s failed: %s", label, exc)
                continue

            if total_raw is not None:
                total_hint = total_raw

            if available_raw is None:
                continue

            total_value = total_raw if total_raw is not None else total_hint
            normalized = self._normalize_balance_pair(available_raw, total_value)
            if normalized is None:
                continue

            available, total = normalized
            rounded_available = round(available, 2)
            if total is not None:
                rounded_available = min(rounded_available, total)
            rounded_available = max(0.0, rounded_available)

            endpoint_label = "account" if label == "mix.account" else "accounts" if label == "mix.accounts" else label
            if label in {"mix.account", "mix.accounts"}:
                logger.info("HP available fix -> available=%.2f (src=%s)", rounded_available, endpoint_label)

            return {"available": rounded_available, "total": total, "source": label}

        fallback_total = total_hint if total_hint is not None else 0.0
        if fallback_total < 0:
            fallback_total = 0.0
        fallback_total = round(fallback_total, 2)
        return {"available": 0.0, "total": fallback_total, "source": "none"}

    async def get_perp_available_usdt(self) -> Optional[float]:
        if not self._settings.has_api_credentials():
            return None
        payload = await self.get_perp_account_raw()
        entry = self._first_data_obj(payload)
        available = self._extract_perp_available(entry)
        if available is None:
            return None
        try:
            numeric = float(available)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        if numeric < 0:
            return 0.0
        return numeric

    async def _fetch_usdtm_account_detail(self) -> Tuple[Optional[float], Optional[float]]:
        payload = await self._request(
            "GET",
            "/api/v2/mix/account/account",
            params={"productType": "USDT-FUTURES", "marginCoin": "USDT"},
        )
        entry = self._first_data_obj(payload)
        if not isinstance(entry, dict):
            return None, None
        available = self._extract_perp_available(entry)
        total = BitgetClient._extract_first(
            entry,
            ["usdtEquity", "accountEquity", "equity", "balance", "totalEquity"],
        )
        return available, total

    async def _fetch_usdtm_account_list(self) -> Tuple[Optional[float], Optional[float]]:
        payload = await self._request(
            "GET",
            "/api/v2/mix/account/accounts",
            params={"productType": "USDT-FUTURES"},
        )
        entries = self._collect_entries(payload)
        preferred: Optional[Tuple[Optional[float], Optional[float]]] = None
        fallback: Optional[Tuple[Optional[float], Optional[float]]] = None
        for entry in entries:
            margin_coin = entry.get("marginCoin") or entry.get("currency")
            available = self._extract_perp_available(entry)
            total = BitgetClient._extract_first(
                entry,
                ["usdtEquity", "accountEquity", "equity", "balance", "totalEquity"],
            )
            if available is None and total is None:
                continue
            margin_upper = str(margin_coin).upper() if margin_coin is not None else ""
            if margin_upper == "USDT" and available is not None:
                return available, total
            if margin_upper == "USDT" and preferred is None:
                preferred = (available, total)
            elif fallback is None:
                fallback = (available, total)
        if preferred is not None:
            return preferred
        if fallback is not None:
            return fallback
        return None, None

    async def _fetch_usdtm_funding_assets(self) -> Tuple[Optional[float], Optional[float]]:
        payload = await self._request(
            "GET",
            "/api/v2/account/funding-assets",
            params={"coin": "USDT"},
        )
        entries = self._collect_entries(payload)
        for entry in entries:
            coin = entry.get("coin") or entry.get("symbol")
            if coin and str(coin).upper() != "USDT":
                continue
            return self._extract_energy_fields(
                entry,
                total_keys=["usdtValue", "balance", "equity", "available"],
                available_keys=["available", "availableAmount", "free", "balance"],
            )
        return None, None

    async def _probe_perp_energy(self) -> Tuple[Optional[float], Optional[float], str]:
        endpoints = (
            ("v2", "/api/v2/mix/account/accounts", {"productType": "USDT-FUTURES"}),
            ("v1", "/api/mix/v1/account/accounts", {"productType": "USDT-FUTURES"}),
        )
        for label, path, params in endpoints:
            payload = await self._request_with_retries(
                "GET",
                path,
                params=params,
                timeout=2.0,
                max_retries=2,
            )
            if not payload:
                continue
            available, total = self._extract_perp_balances(payload)
            if available is not None or total is not None:
                return available, total, label
        return None, None, "none"

    async def _probe_spot_energy(self) -> Tuple[Optional[float], Optional[float], str]:
        endpoints = (
            ("v2", "/api/v2/spot/account/assets", {"coin": "USDT"}),
            ("v1", "/api/spot/v1/account/assets", {"coin": "USDT"}),
        )
        for label, path, params in endpoints:
            payload = await self._request_with_retries(
                "GET",
                path,
                params=params,
                timeout=2.0,
                max_retries=2,
            )
            if not payload:
                continue
            available, total = self._extract_spot_balances(payload)
            if available is not None or total is not None:
                return available, total, label
        return None, None, "none"

    async def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_payload: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
        use_demo: bool = False,
        timeout: Optional[float] = None,
        max_retries: int = 2,
    ) -> Optional[Dict[str, Any]]:
        attempts = 0
        while attempts <= max_retries:
            try:
                return await self._request(
                    method,
                    path,
                    params=params,
                    json_payload=json_payload,
                    authenticated=authenticated,
                    use_demo=use_demo,
                    timeout=timeout,
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in {429, 500, 502, 503, 504} or attempts >= max_retries:
                    logger.debug("Energy probe HTTP error (%s): %s", status, exc)
                    return None
            except (httpx.RequestError, asyncio.TimeoutError) as exc:
                if attempts >= max_retries:
                    logger.debug("Energy probe network error: %s", exc)
                    return None
            await asyncio.sleep(0.3 * (attempts + 1))
            attempts += 1
        return None

    @staticmethod
    def _extract_perp_balances(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Dict[str, object]]:
        entries: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            data_list = payload.get("data_list")
            if isinstance(data_list, list):
                for item in data_list:
                    if not isinstance(item, dict):
                        continue
                    inner_list = item.get("list")
                    if isinstance(inner_list, list):
                        entries.extend([row for row in inner_list if isinstance(row, dict)])
                    else:
                        entries.append(item)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            margin_coin = entry.get("marginCoin") or entry.get("marginCoin".lower())
            if margin_coin and str(margin_coin).upper() != "USDT":
                continue
            available = BitgetClient._coerce_float(
                entry,
                "available",
                "availableEq",
                "availBal",
                "usdtAvailable",
            )
            total = BitgetClient._coerce_float(
                entry,
                "equity",
                "usdtEquity",
                "totalEq",
                "marginEquity",
            )
            if available is not None or total is not None:
                return available, total
        return None, None

    async def _request_position_mode(self, path: str, params: Dict[str, Any]) -> Optional[str]:
        payload = await self._request("GET", path, params=params)
        return self._extract_position_mode(payload)

    @staticmethod
    def _extract_position_mode(payload: Dict[str, Any]) -> Optional[str]:
        entries: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            data_list = payload.get("data_list")
            if isinstance(data_list, list):
                for item in data_list:
                    if not isinstance(item, dict):
                        continue
                    inner_list = item.get("list")
                    if isinstance(inner_list, list):
                        entries.extend([row for row in inner_list if isinstance(row, dict)])
                    else:
                        entries.append(item)
        for entry in entries:
            for key in ("positionMode", "posMode", "holdMode"):
                if key not in entry:
                    continue
                mode = BitgetClient._normalize_position_mode(entry[key])
                if mode:
                    return mode
        return None

    @staticmethod
    def _normalize_position_mode(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                numeric = int(value)
            except (TypeError, ValueError):
                numeric = None
            if numeric == 1:
                return "one_way"
            if numeric == 2:
                return "hedge"
        text = str(value).strip().lower()
        if not text:
            return None
        normalized = text.replace("-", "_").replace(" ", "_")
        if normalized in {"oneway", "one_way", "onewaymode", "single", "one_way_mode"}:
            return "one_way"
        if normalized in {"hedge", "hedging", "hedge_mode", "two_way", "dual"}:
            return "hedge"
        if "hedge" in normalized:
            return "hedge"
        if "one" in normalized and "way" in normalized:
            return "one_way"
        return None

    @staticmethod
    def _extract_spot_balances(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Dict[str, object]]:
        entries: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            data_list = payload.get("data_list")
            if isinstance(data_list, list):
                for item in data_list:
                    if not isinstance(item, dict):
                        continue
                    assets = item.get("assetsList")
                    if isinstance(assets, list):
                        entries.extend([row for row in assets if isinstance(row, dict)])
                    else:
                        entries.append(item)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            coin = entry.get("coin") or entry.get("coinName")
            if coin and str(coin).upper() != "USDT":
                continue
            available = BitgetClient._coerce_float(
                entry,
                "available",
                "availableBalance",
                "availableForTrade",
                "free",
            )
            total = BitgetClient._coerce_float(
                entry,
                "equity",
                "usdtEquity",
                "total",
                "balance",
            )
            if available is not None or total is not None:
                return available, total
        return None, None

    @staticmethod
    def _collect_entries(payload: Any) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            for key in ("data_list", "data", "list"):
                value = payload.get(key)
                if isinstance(value, list):
                    entries.extend([item for item in value if isinstance(item, dict)])
                elif isinstance(value, dict):
                    entries.append(value)
            if not entries:
                entries.append(payload)
        elif isinstance(payload, list):
            entries.extend([item for item in payload if isinstance(item, dict)])
        return entries

    @staticmethod
    def _preview_entries(entries: List[Dict[str, Any]]) -> Dict[str, object]:
        if not entries:
            return {"keys": [], "type": "none", "count": 0}
        preview = BitgetClient._preview_from_entry(entries[0])
        preview["count"] = len(entries)
        return preview

    @staticmethod
    def _preview_from_entry(entry: Dict[str, Any]) -> Dict[str, object]:
        if not isinstance(entry, dict):
            return {"keys": [], "type": type(entry).__name__}
        keys = list(entry.keys())
        return {"keys": keys[:8], "type": "dict"}

    @staticmethod
    def _extract_perp_available(entry: Dict[str, Any]) -> Optional[float]:
        if not isinstance(entry, dict):
            return None

        candidate_keys = [
            "crossMaxAvailable",
            "unionAvailable",
            "maxTransferOut",
            "available",
            "availableBalance",
            "availableEq",
            "marginAvailable",
        ]

        primary: Optional[float] = None
        cross_cap = BitgetClient._extract_first(entry, ["crossMaxAvailable"])

        for key in candidate_keys:
            value = BitgetClient._extract_first(entry, [key])
            if value is not None:
                primary = value
                break

        if primary is not None and cross_cap is not None:
            primary = min(primary, cross_cap)

        if primary is None:
            total_value = BitgetClient._extract_first(
                entry,
                ["usdtEquity", "equity", "accountEquity", "balance"],
            )
            locked = BitgetClient._extract_first(
                entry,
                ["crossedMarginLocked", "crossMarginLocked"],
            ) or 0.0
            open_margin = BitgetClient._extract_first(
                entry,
                ["openOrderMargin", "crossedOpenOrderMargin"],
            ) or 0.0
            if total_value is not None:
                derived = total_value - locked - open_margin
                primary = derived

        if primary is None:
            return None

        if not math.isfinite(primary):
            return None

        if primary < 0:
            return 0.0

        return primary

    @staticmethod
    def _extract_energy_fields(
        entry: Dict[str, Any],
        *,
        total_keys: List[str],
        available_keys: List[str],
    ) -> Tuple[Optional[float], Optional[float]]:
        if not isinstance(entry, dict):
            return None, None
        available = BitgetClient._extract_first(entry, available_keys)
        total = BitgetClient._extract_first(entry, total_keys)
        return available, total

    @staticmethod
    def _extract_balance_fields(entry: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        if not isinstance(entry, dict):
            return None, None
        available = BitgetClient._extract_first(
            entry,
            ["available", "availableEquity", "marginAvailable", "availableBalance", "cash", "free"],
        )
        total = BitgetClient._extract_first(
            entry,
            ["accountEquity", "usdtEquity", "equity", "balance", "total"],
        )
        return available, total

    @staticmethod
    def _extract_first(entry: Dict[str, Any], keys: List[str]) -> Optional[float]:
        for key in keys:
            for variant in BitgetClient._key_variants(key):
                if variant in entry and entry[variant] not in (None, ""):
                    try:
                        return float(entry[variant])
                    except (TypeError, ValueError):
                        continue
        return None

    @staticmethod
    def _key_variants(key: str) -> List[str]:
        variants = [key]
        if "_" in key:
            parts = key.split("_")
            camel = parts[0] + ''.join(part.capitalize() for part in parts[1:])
            variants.append(camel)
        else:
            variants.append(key[:1].lower() + key[1:])
        return list(dict.fromkeys(variants))

    @staticmethod
    def _normalize_balance_pair(
        available: Optional[float],
        total: Optional[float],
    ) -> Optional[Tuple[float, float]]:
        avail = BitgetClient._ensure_non_negative(available)
        total_val = BitgetClient._ensure_non_negative(total)

        if total_val is None and avail is not None:
            total_val = avail
        if avail is None and total_val is not None:
            avail = 0.0

        if avail is None or total_val is None:
            return None

        if total_val <= 0:
            return (0.0, 0.0)

        if avail < 0:
            avail = 0.0
        if avail > total_val:
            avail = total_val

        return (avail, total_val)

    @staticmethod
    def _coerce_float(entry: Dict[str, Any], *keys: str) -> Optional[float]:
        for key in keys:
            value = entry.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _ensure_non_negative(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return max(0.0, numeric)

    @staticmethod
    def _combined_source_label(summary: Dict[str, Any]) -> str:
        has_perp = summary.get("perp") is not None
        has_spot = summary.get("spot") is not None
        if has_perp and has_spot:
            return "both"
        if has_perp:
            return "perp"
        if has_spot:
            return "spot"
        return "none"

    @staticmethod
    def _empty_energy_summary() -> Dict[str, Any]:
        return {
            "perp": None,
            "spot": None,
            "total": None,
            "available": None,
            "perp_total": None,
            "spot_total": None,
            "sources": {"perp": "none", "spot": "none"},
        }

    @staticmethod
    def _parse_json(response: httpx.Response) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError(
                "Prof. Oak: the exchange sent unreadable data. Please confirm again."
            ) from exc

        payload: Dict[str, Any] = {
            "ok": True,
            "code": "",
            "msg": "",
            "raw": data,
            "data_obj": None,
            "data_list": [],
        }

        if isinstance(data, dict):
            for key in ("code", "status", "errorCode"):
                value = data.get(key)
                if value not in (None, ""):
                    payload["code"] = value
                    break
            for key in ("msg", "message", "errorMsg", "detail"):
                value = data.get(key)
                if value:
                    payload["msg"] = value
                    break
            raw_data = data.get("data")
            if isinstance(raw_data, list):
                payload["data_list"] = raw_data
            elif isinstance(raw_data, dict):
                payload["data_obj"] = raw_data
                payload["data_list"] = [raw_data]
            payload["data"] = raw_data
            code_str = str(payload["code"]) if payload["code"] is not None else ""
            payload["ok"] = code_str in {"", "0", "00000", "None"}
            return payload

        if isinstance(data, list):
            payload["data_list"] = data
            return payload

        return payload

    @staticmethod
    def _first_data_obj(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        data_obj = payload.get("data_obj")
        if isinstance(data_obj, dict):
            return data_obj
        data_list = payload.get("data_list")
        if isinstance(data_list, list):
            for item in data_list:
                if isinstance(item, dict):
                    return item
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    return item
        return {}

    def first_data(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._first_data_obj(payload)

    @staticmethod
    def _ensure_mix_product_type(
        path: str,
        params: Optional[Dict[str, Any]],
        json_payload: Optional[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        normalized = path.lower()
        if not normalized.startswith("/api/") or "/mix/" not in normalized:
            return params, json_payload

        product_type = "USDT-FUTURES"

        if isinstance(json_payload, dict) and "productType" not in json_payload:
            json_payload["productType"] = product_type

        if params is None:
            params = {"productType": product_type}
        elif isinstance(params, dict) and "productType" not in params:
            params["productType"] = product_type

        return params, json_payload
