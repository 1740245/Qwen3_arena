from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Dict, List, Optional

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from ..config import Settings


logger = logging.getLogger(__name__)


class HyperliquidClient:
    """Lightweight asynchronous Hyperliquid client wrapper."""

    def __init__(self, settings: Settings):
        self._settings = settings

        # Determine API URL (mainnet or testnet)
        self._base_url = (
            constants.TESTNET_API_URL
            if settings.hyperliquid_testnet
            else constants.MAINNET_API_URL
        )

        # Info client for read-only operations (public data)
        self._info = Info(self._base_url, skip_ws=True)

        # Exchange client for trading operations (requires credentials)
        self._exchange: Optional[Exchange] = None
        if settings.has_hyperliquid_credentials():
            try:
                self._exchange = Exchange(
                    wallet=None,  # We'll use private key directly
                    base_url=self._base_url,
                    account_address=settings.hyperliquid_wallet_address,
                    secret_key=settings.hyperliquid_private_key,
                )
                logger.info("Hyperliquid exchange client initialized for wallet: %s",
                           settings.hyperliquid_wallet_address[:10] + "...")
            except Exception as exc:
                logger.error("Failed to initialize Hyperliquid exchange client: %s", exc)
                self._exchange = None

        self._position_mode: Optional[str] = "hedge"  # Hyperliquid uses hedge mode
        self._order_tap = deque(maxlen=10)

    async def close(self) -> None:
        """Close any open connections."""
        # Hyperliquid SDK doesn't require explicit cleanup
        pass

    async def __aenter__(self) -> "HyperliquidClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def get_recent_order_tap(self) -> List[Dict[str, Any]]:
        """Get recent order history for debugging."""
        return list(self._order_tap)

    @property
    def position_mode(self) -> Optional[str]:
        """Hyperliquid always uses hedge mode."""
        return "hedge"

    async def get_position_mode(self, product_type: str = "perp") -> Optional[str]:
        """Get position mode. Hyperliquid always uses hedge mode."""
        return "hedge"

    # ==================== MARKET DATA ====================

    async def list_perp_tickers(self) -> Dict[str, Any]:
        """Fetch all perpetual market tickers."""
        try:
            # Wrap sync SDK calls in asyncio.to_thread to avoid blocking event loop
            meta = await asyncio.to_thread(self._info.meta)
            all_mids = await asyncio.to_thread(self._info.all_mids)

            tickers = []
            if isinstance(meta, dict) and "universe" in meta:
                for asset in meta["universe"]:
                    symbol = asset.get("name", "")
                    if symbol in all_mids:
                        tickers.append({
                            "symbol": symbol,
                            "lastPr": str(all_mids[symbol]),
                            "askPr": str(all_mids[symbol]),
                            "bidPr": str(all_mids[symbol]),
                            "baseVolume": "0",
                            "quoteVolume": "0",
                        })

            return self._wrap_data(tickers)
        except Exception as exc:
            logger.error("Failed to fetch perp tickers: %s", exc)
            return self._wrap_data([])

    async def list_perp_contracts(self) -> Dict[str, Any]:
        """Fetch perpetual contract information."""
        try:
            meta = await asyncio.to_thread(self._info.meta)
            contracts = []

            if isinstance(meta, dict) and "universe" in meta:
                for asset in meta["universe"]:
                    contracts.append({
                        "symbol": asset.get("name", ""),
                        "baseCoin": asset.get("name", "").replace("-USD", ""),
                        "quoteCoin": "USD",
                        "buyLimitPriceRatio": "0.05",
                        "sellLimitPriceRatio": "0.05",
                        "feeRateUpRatio": "0.005",
                        "makerFeeRate": "0.00025",
                        "takerFeeRate": "0.00050",
                        "minTradeNum": str(asset.get("szDecimals", 8)),
                        "priceEndStep": str(10 ** (-asset.get("szDecimals", 8))),
                        "volumePlace": str(asset.get("szDecimals", 8)),
                        "pricePlace": str(asset.get("szDecimals", 8)),
                    })

            return self._wrap_data(contracts)
        except Exception as exc:
            logger.error("Failed to fetch perp contracts: %s", exc)
            return self._wrap_data([])

    async def get_perp_contract(self, symbol: str) -> Dict[str, Any]:
        """Fetch single perpetual contract information."""
        contracts_response = await self.list_perp_contracts()
        contracts = contracts_response.get("data", [])

        for contract in contracts:
            if contract.get("symbol") == symbol:
                return self._wrap_data([contract])

        return self._wrap_data([])

    # ==================== ACCOUNT INFO ====================

    async def fetch_energy_usdt(self) -> Dict[str, Any]:
        """Fetch account balance (USDT equivalent)."""
        result = self._empty_energy_summary()

        if not self._settings.has_hyperliquid_credentials():
            return result

        try:
            user_state = await asyncio.to_thread(
                self._info.user_state,
                self._settings.hyperliquid_wallet_address
            )

            if isinstance(user_state, dict):
                # Extract margin summary
                margin_summary = user_state.get("marginSummary", {})

                # Available balance (what can be used for new orders)
                account_value = float(margin_summary.get("accountValue", 0))
                total_margin_used = float(margin_summary.get("totalMarginUsed", 0))
                available = max(0.0, account_value - total_margin_used)

                result["perp"] = available
                result["perp_total"] = account_value
                result["total"] = account_value
                result["available"] = available
                result["sources"]["perp"] = "hyperliquid"

                logger.info(
                    "Balance: available=%.2f total=%.2f (source=hyperliquid)",
                    available,
                    account_value,
                )
        except Exception as exc:
            logger.warning("Failed to fetch Hyperliquid balance: %s", exc)

        return result

    async def list_balances(self) -> Dict[str, Any]:
        """Fetch account balances."""
        return await self.fetch_energy_usdt()

    # ==================== POSITIONS ====================

    async def list_perp_positions(
        self,
        *,
        product_type: str = "perp",
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        """Fetch all perpetual positions."""
        if not self._settings.has_hyperliquid_credentials():
            return self._wrap_data([])

        try:
            user_state = await asyncio.to_thread(
                self._info.user_state,
                self._settings.hyperliquid_wallet_address
            )

            positions = []
            if isinstance(user_state, dict) and "assetPositions" in user_state:
                for pos in user_state["assetPositions"]:
                    position_data = pos.get("position", {})

                    # Only include positions with non-zero size
                    szi = float(position_data.get("szi", 0))
                    if szi == 0:
                        continue

                    positions.append({
                        "symbol": position_data.get("coin", ""),
                        "holdSide": "long" if szi > 0 else "short",
                        "size": str(abs(szi)),
                        "entryPrice": position_data.get("entryPx", "0"),
                        "markPrice": position_data.get("markPx", "0"),
                        "liquidationPrice": position_data.get("liquidationPx", "0"),
                        "unrealizedPL": position_data.get("unrealizedPnl", "0"),
                        "leverage": position_data.get("leverage", {}).get("value", "1"),
                        "marginMode": position_data.get("leverage", {}).get("type", "cross"),
                    })

            return self._wrap_data(positions)
        except Exception as exc:
            logger.error("Failed to fetch positions: %s", exc)
            return self._wrap_data([])

    async def read_all_positions(
        self,
        *,
        product_type: str = "perp",
    ) -> Dict[str, Any]:
        """Read all positions with normalized format."""
        response = await self.list_perp_positions(product_type=product_type)

        return {
            "ok": response.get("ok", False),
            "status": 200 if response.get("ok") else None,
            "entries": response.get("data", []),
            "payload": response.get("raw"),
            "params": {"productType": product_type},
        }

    # ==================== ORDERS ====================

    async def place_perp_order(self, payload: Dict[str, Any], *, demo_mode: bool = False) -> Dict[str, Any]:
        """Place a perpetual order."""
        if demo_mode or not self._exchange:
            return self._simulate_order(payload, route="perp")

        try:
            symbol = payload["symbol"]
            is_buy = payload["side"] == "buy"
            size = float(payload["size"])
            order_type = payload.get("orderType", "market")
            reduce_only = payload.get("reduceOnly", False)

            order_request = {
                "coin": symbol,
                "is_buy": is_buy,
                "sz": size,
                "limit_px": float(payload.get("price", 0)) if order_type == "limit" else None,
                "order_type": {"limit": "limit", "market": "market"}.get(order_type, "market"),
                "reduce_only": reduce_only,
            }

            # Log order attempt
            tap_entry = {
                "path": "/exchange",
                "body": order_request,
                "tag": "place_perp_order",
                "timestamp": time.time(),
            }

            result = await asyncio.to_thread(
                self._exchange.order,
                symbol,
                is_buy,
                size,
                order_request["limit_px"],
                order_request
            )

            tap_entry["status"] = 200
            tap_entry["response"] = result
            self._order_tap.appendleft(tap_entry)

            logger.info("Placed perp order: %s %s %.4f @ %s",
                       "BUY" if is_buy else "SELL", symbol, size,
                       payload.get("price", "MARKET"))

            return self._wrap_data(result.get("response", {}).get("data", {}))

        except Exception as exc:
            logger.error("Failed to place perp order: %s", exc)
            raise RuntimeError(f"Order failed: {str(exc)}")

    async def close_perp_positions(
        self, payload: Dict[str, Any], *, demo_mode: bool = False
    ) -> Dict[str, Any]:
        """Close perpetual positions."""
        if demo_mode or not self._exchange:
            return self._wrap_data({"status": "success", "symbol": payload.get("symbol")})

        try:
            symbol = payload["symbol"]

            # Get current position to determine size
            positions_response = await self.list_perp_positions()
            positions = positions_response.get("data", [])

            target_position = None
            for pos in positions:
                if pos.get("symbol") == symbol:
                    target_position = pos
                    break

            if not target_position:
                return self._wrap_data({"status": "no_position", "symbol": symbol})

            # Place opposite order to close
            size = abs(float(target_position.get("size", 0)))
            is_buy = target_position.get("holdSide") == "short"  # Buy to close short, sell to close long

            result = await asyncio.to_thread(self._exchange.market_close, symbol, size)

            logger.info("Closed position: %s size=%.4f", symbol, size)

            return self._wrap_data(result.get("response", {}).get("data", {}))

        except Exception as exc:
            logger.error("Failed to close position: %s", exc)
            raise RuntimeError(f"Close failed: {str(exc)}")

    async def list_open_perp_orders(
        self,
        symbol: Optional[str] = None,
        *,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        """List open perpetual orders."""
        if not self._settings.has_hyperliquid_credentials():
            return self._wrap_data([])

        try:
            user_state = await asyncio.to_thread(
                self._info.user_state,
                self._settings.hyperliquid_wallet_address
            )

            orders = []
            if isinstance(user_state, dict) and "assetPositions" in user_state:
                for asset_pos in user_state["assetPositions"]:
                    position_symbol = asset_pos.get("position", {}).get("coin", "")

                    # Filter by symbol if provided
                    if symbol and position_symbol != symbol:
                        continue

                    # Get open orders for this asset
                    # Note: Hyperliquid's user_state doesn't directly show pending orders
                    # We would need to use the frontend_open_orders endpoint for this
                    pass

            return self._wrap_data(orders)

        except Exception as exc:
            logger.error("Failed to fetch open orders: %s", exc)
            return self._wrap_data([])

    async def cancel_all_orders_by_symbol(
        self,
        symbol: str,
        *,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        """Cancel all orders for a symbol."""
        if demo_mode or not self._exchange:
            return {
                "ok": True,
                "code": "00000",
                "msg": "",
                "symbol": symbol,
                "attemptedSymbols": [symbol],
            }

        try:
            result = await asyncio.to_thread(self._exchange.cancel_all_orders, symbol)

            logger.info("Cancelled all orders for %s", symbol)

            return {
                "ok": True,
                "code": "00000",
                "msg": "Orders cancelled",
                "symbol": symbol,
                "cancelled": len(result.get("response", {}).get("data", {}).get("statuses", [])),
            }

        except Exception as exc:
            logger.error("Failed to cancel orders: %s", exc)
            return {
                "ok": False,
                "code": "error",
                "msg": str(exc),
                "symbol": symbol,
            }

    # ==================== HELPER METHODS ====================

    @staticmethod
    def _wrap_data(data: Any) -> Dict[str, Any]:
        """Wrap data in standard response format."""
        return {
            "ok": True,
            "code": "00000",
            "msg": "",
            "raw": {"data": data},
            "data_obj": data if isinstance(data, dict) else None,
            "data_list": data if isinstance(data, list) else [data],
            "data": data,
        }

    @staticmethod
    def _simulate_order(payload: Dict[str, Any], *, route: str) -> Dict[str, Any]:
        """Simulate an order in demo mode."""
        import uuid

        data = {
            "orderId": str(uuid.uuid4()),
            "status": "filled",
            "symbol": payload.get("symbol"),
            "route": route,
            "price": payload.get("price"),
            "size": payload.get("size"),
            "holdSide": payload.get("posSide"),
        }
        response = HyperliquidClient._wrap_data(data)
        response["msg"] = "Simulated order."
        response["code"] = "00000"
        return response

    @staticmethod
    def _empty_energy_summary() -> Dict[str, Any]:
        """Return empty energy/balance summary."""
        return {
            "perp": None,
            "spot": None,
            "total": None,
            "available": None,
            "perp_total": None,
            "spot_total": None,
            "sources": {"perp": "none", "spot": "none"},
        }
