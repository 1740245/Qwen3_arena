from __future__ import annotations

import asyncio
import logging
import time
import uuid  # BUG FIX #11: Move uuid import to module level
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
                # BUG FIX #25: Handle short wallet addresses safely
                wallet_display = (settings.hyperliquid_wallet_address[:10] + "...") if len(settings.hyperliquid_wallet_address) > 10 else settings.hyperliquid_wallet_address
                logger.info("Hyperliquid exchange client initialized for wallet: %s", wallet_display)
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

    # BUG FIX #7: No need for async since this just returns a constant
    def get_position_mode(self, product_type: str = "perp") -> Optional[str]:
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

                # BUG FIX #21: Handle None values before float conversion
                # Available balance (what can be used for new orders)
                account_value_raw = margin_summary.get("accountValue", 0)
                total_margin_used_raw = margin_summary.get("totalMarginUsed", 0)

                try:
                    account_value = float(account_value_raw) if account_value_raw is not None else 0.0
                    total_margin_used = float(total_margin_used_raw) if total_margin_used_raw is not None else 0.0
                except (TypeError, ValueError) as e:
                    logger.warning("Invalid balance data: accountValue=%s, totalMarginUsed=%s", account_value_raw, total_margin_used_raw)
                    account_value = 0.0
                    total_margin_used = 0.0

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

                    # BUG FIX #22: Handle None value before float conversion
                    # Only include positions with non-zero size
                    szi_raw = position_data.get("szi", 0)
                    try:
                        szi = float(szi_raw) if szi_raw is not None else 0.0
                    except (TypeError, ValueError):
                        logger.debug("Invalid szi value for position: %s", szi_raw)
                        continue

                    if szi == 0:
                        continue

                    # BUG FIX #5: Convert all numeric fields to strings for consistency
                    # BUG FIX #9: Optimize nested dict access by storing in variable
                    leverage_data = position_data.get("leverage", {})
                    positions.append({
                        "symbol": position_data.get("coin", ""),
                        "holdSide": "long" if szi > 0 else "short",
                        "size": str(abs(szi)),
                        "entryPrice": str(position_data.get("entryPx", "0")),
                        "markPrice": str(position_data.get("markPx", "0")),
                        "liquidationPrice": str(position_data.get("liquidationPx", "0")),
                        "unrealizedPL": str(position_data.get("unrealizedPnl", "0")),
                        "leverage": str(leverage_data.get("value", "1")),
                        "marginMode": leverage_data.get("type", "cross"),
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
            # BUG FIX #15: Validate required payload fields with clear error messages
            if "symbol" not in payload:
                raise ValueError("Missing required field: 'symbol'")
            if "side" not in payload:
                raise ValueError("Missing required field: 'side'")
            if "size" not in payload:
                raise ValueError("Missing required field: 'size'")

            symbol = payload["symbol"]
            side = payload["side"]
            if side not in ("buy", "sell"):
                raise ValueError(f"Invalid side value: {side} (must be 'buy' or 'sell')")
            is_buy = side == "buy"

            size = float(payload["size"])
            order_type = payload.get("orderType", "market")
            reduce_only = payload.get("reduceOnly", False)

            # Validate required fields
            if size <= 0:
                raise ValueError(f"Order size must be positive, got {size}")

            # BUG FIX #10: Verify None handling for market orders
            # For limit orders, price is required
            if order_type == "limit":
                if "price" not in payload or payload["price"] is None:
                    raise ValueError("Limit orders require 'price' field")
                limit_px = float(payload["price"])
                if limit_px <= 0:
                    raise ValueError(f"Limit price must be positive, got {limit_px}")
            else:
                # Market orders: SDK accepts None for limit_px when order_type="market"
                # The SDK will execute at current market price
                limit_px = None

            order_request = {
                "coin": symbol,
                "is_buy": is_buy,
                "sz": size,
                "limit_px": limit_px,
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

            # Hyperliquid SDK order() method signature:
            # order(coin, is_buy, sz, limit_px, order_type, reduce_only=False)
            result = await asyncio.to_thread(
                self._exchange.order,
                symbol,
                is_buy,
                size,
                order_request["limit_px"],
                order_request["order_type"],
                reduce_only
            )

            tap_entry["status"] = 200
            tap_entry["response"] = result
            self._order_tap.appendleft(tap_entry)

            logger.info("Placed perp order: %s %s %.4f @ %s",
                       "BUY" if is_buy else "SELL", symbol, size,
                       payload.get("price", "MARKET"))

            # BUG FIX #18: Use consistent response parsing (check "status" field for order responses)
            # Hyperliquid SDK returns {"status": "ok", "response": {"type": "order", "data": {...}}}
            if isinstance(result, dict) and result.get("status") == "ok":
                response_data = result.get("response", {})
                if isinstance(response_data, dict):
                    data = response_data.get("data", {})
                    return self._wrap_data(data if data else response_data)
            return self._wrap_data(result)

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
            # BUG FIX #16: Validate required symbol field
            if "symbol" not in payload:
                raise ValueError("Missing required field: 'symbol'")
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
            size = float(target_position.get("size", 0))

            # Validate size before attempting to close
            if size == 0:
                logger.warning("Position size is 0 for %s, nothing to close", symbol)
                return self._wrap_data({"status": "no_position", "symbol": symbol})

            size = abs(size)
            # BUG FIX #19: Removed unused variable is_buy (market_close determines side automatically)

            # Hyperliquid SDK market_close(coin, sz=None, px=None, slippage=0.05, cloid=None)
            result = await asyncio.to_thread(
                self._exchange.market_close,
                symbol,
                sz=size
            )

            logger.info("Closed position: %s size=%.4f", symbol, size)

            # BUG FIX #18: Use consistent response parsing (check "status" field for order responses)
            # Hyperliquid SDK returns {"status": "ok", "response": {"type": "order", "data": {...}}}
            if isinstance(result, dict) and result.get("status") == "ok":
                response_data = result.get("response", {})
                if isinstance(response_data, dict):
                    data = response_data.get("data", {})
                    return self._wrap_data(data if data else response_data)
            return self._wrap_data(result)

        except Exception as exc:
            logger.error("Failed to close position: %s", exc)
            raise RuntimeError(f"Close failed: {str(exc)}")

    async def place_perp_stop_loss(
        self, payload: Dict[str, Any], *, demo_mode: bool = False
    ) -> Dict[str, Any]:
        """Place a perpetual stop-loss order."""
        if demo_mode or not self._exchange:
            return self._simulate_order(payload, route="perp")

        try:
            # BUG FIX #17: Validate required symbol field
            if "symbol" not in payload:
                raise ValueError("Missing required field: 'symbol'")
            symbol = payload["symbol"]

            trigger_price = float(payload.get("triggerPrice", 0))
            size = float(payload.get("size", 0))

            # BUG FIX #2: Validate required fields
            if trigger_price <= 0:
                raise ValueError(f"Invalid triggerPrice: {trigger_price} (must be > 0)")
            if size <= 0:
                raise ValueError(f"Invalid size: {size} (must be > 0)")

            limit_price = float(payload.get("price", trigger_price))

            # BUG FIX #4: Determine order side for stop-loss with explicit validation
            # If 'side' is explicitly provided, use it
            # Otherwise, derive from 'holdSide': long position → sell to close, short position → buy to close
            if "side" in payload:
                side_value = payload["side"]
                if side_value not in ("buy", "sell"):
                    raise ValueError(f"Invalid side value: {side_value} (must be 'buy' or 'sell')")
                is_buy = side_value == "buy"
            elif "holdSide" in payload:
                hold_side = payload["holdSide"]
                if hold_side not in ("long", "short"):
                    raise ValueError(f"Invalid holdSide value: {hold_side} (must be 'long' or 'short')")
                # Stop-loss closes position: long→sell, short→buy
                is_buy = hold_side == "short"
            else:
                # No side information provided - cannot determine direction
                raise ValueError("Either 'side' or 'holdSide' is required for stop-loss orders")

            # BUG FIX #1: Hyperliquid trigger order format requires triggerPx as STRING
            # For stop-loss: trigger activates when price goes against position
            order_type = {
                "trigger": {
                    "triggerPx": str(trigger_price),  # MUST be string, not float
                    "isMarket": True,  # Execute as market order when triggered
                    "tpsl": "sl"  # Mark as stop-loss
                }
            }

            result = await asyncio.to_thread(
                self._exchange.order,
                symbol,
                is_buy,
                size,
                limit_price,  # Limit price (used if isMarket=False)
                order_type,
                reduce_only=True  # Stop-loss always reduces position
            )

            logger.info("Placed stop-loss: %s trigger=%.4f size=%.4f",
                       symbol, trigger_price, size)

            # BUG FIX #18: Use consistent response parsing (check "status" field for order responses)
            # Parse response
            if isinstance(result, dict) and result.get("status") == "ok":
                response_data = result.get("response", {})
                if isinstance(response_data, dict):
                    data = response_data.get("data", {})
                    return self._wrap_data(data if data else response_data)
            return self._wrap_data(result)

        except Exception as exc:
            logger.error("Failed to place stop-loss: %s", exc)
            raise RuntimeError(f"Stop-loss failed: {str(exc)}")

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
            # Use frontend_open_orders to get pending orders
            open_orders = await asyncio.to_thread(
                self._info.frontend_open_orders,
                self._settings.hyperliquid_wallet_address
            )

            orders = []
            if isinstance(open_orders, list):
                for order in open_orders:
                    if not isinstance(order, dict):
                        continue

                    order_symbol = order.get("coin", "")

                    # Filter by symbol if provided
                    if symbol and order_symbol != symbol:
                        continue

                    # BUG FIX #26: Improve side mapping with explicit checks
                    # Hyperliquid uses "B" for buy, "A" for ask/sell
                    order_side_raw = order.get("side", "")
                    if order_side_raw == "B":
                        order_side = "buy"
                    elif order_side_raw == "A":
                        order_side = "sell"
                    else:
                        # Default to sell for unknown values, log warning
                        logger.warning("Unknown order side value: %s, defaulting to 'sell'", order_side_raw)
                        order_side = "sell"

                    # Map Hyperliquid order format to expected format
                    orders.append({
                        "orderId": order.get("oid", ""),
                        "symbol": order_symbol,
                        "side": order_side,
                        "orderType": order.get("orderType", "limit"),
                        "price": order.get("limitPx", "0"),
                        "size": order.get("sz", "0"),
                        "filledSize": order.get("szFilled", "0"),
                        "status": "open",
                        "reduceOnly": order.get("reduceOnly", False),
                        "timestamp": order.get("timestamp", 0),
                    })

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

            # BUG FIX #20: Hyperliquid SDK returns {"status": "ok", "response": ...} for cancel_all_orders
            # But after SDK processing, the response structure uses "ok" field for consistency
            # BUG FIX #3: Check result.get("ok") not result.get("status")
            cancelled_count = 0
            if isinstance(result, dict):
                if result.get("status") == "ok":
                    response_data = result.get("response", {})
                    if isinstance(response_data, dict):
                        data = response_data.get("data", {})
                        if isinstance(data, dict):
                            statuses = data.get("statuses", [])
                            cancelled_count = len(statuses) if isinstance(statuses, list) else 0

            return {
                "ok": True,
                "code": "00000",
                "msg": "Orders cancelled",
                "symbol": symbol,
                "cancelled": cancelled_count,
            }

        except Exception as exc:
            logger.error("Failed to cancel orders: %s", exc)
            # BUG FIX #13: Use standard error code format
            return {
                "ok": False,
                "code": "50000",
                "msg": str(exc),
                "symbol": symbol,
            }

    async def cancel_perp_stop_loss(
        self,
        payload: Dict[str, Any],
        *,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        """Cancel a perpetual stop-loss order."""
        if demo_mode or not self._exchange:
            return {
                "ok": True,
                "code": "00000",
                "msg": "Stop-loss cancelled (demo)",
                "orderId": payload.get("orderId", ""),
            }

        try:
            # BUG FIX #23: Validate symbol field instead of defaulting to empty string
            symbol = payload.get("symbol", "")
            if not symbol:
                raise ValueError("Missing required field: 'symbol'")

            # Accept both "orderId" and "planId" for flexibility
            order_id_str = payload.get("orderId") or payload.get("planId")

            if not order_id_str:
                raise ValueError("orderId or planId is required to cancel stop-loss")

            # Convert to int as required by Hyperliquid SDK
            try:
                order_id = int(order_id_str)
            except (TypeError, ValueError):
                raise ValueError(f"Invalid order ID format: {order_id_str}")

            # Hyperliquid cancel order by ID: cancel(coin, oid)
            result = await asyncio.to_thread(
                self._exchange.cancel,
                symbol,
                order_id
            )

            logger.info("Cancelled stop-loss: %s order=%s", symbol, order_id)

            # BUG FIX #3: Parse response using result.get("ok") not result.get("status")
            if isinstance(result, dict) and result.get("ok"):
                return {
                    "ok": True,
                    "code": "00000",
                    "msg": "Stop-loss cancelled",
                    "orderId": str(order_id),
                }

            # BUG FIX #13: Use standard error code format
            return {
                "ok": False,
                "code": "50000",
                "msg": str(result),
                "orderId": str(order_id),
            }

        except Exception as exc:
            logger.error("Failed to cancel stop-loss: %s", exc)
            # BUG FIX #13: Use standard error code format
            return {
                "ok": False,
                "code": "50000",
                "msg": str(exc),
                "orderId": payload.get("orderId") or payload.get("planId", ""),
            }

    async def cancel_perp_plan_order(
        self,
        payload: Dict[str, Any],
        *,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        """Cancel a perpetual plan order, or all orders if no orderId specified."""
        # BUG FIX #24: Validate symbol field instead of defaulting to empty string
        symbol = payload.get("symbol", "")
        if not symbol:
            raise ValueError("Missing required field: 'symbol'")

        order_id = payload.get("orderId") or payload.get("planId")

        if demo_mode or not self._exchange:
            return {
                "ok": True,
                "code": "00000",
                "msg": "Plan order cancelled (demo)",
                "orderId": order_id or "",
            }

        try:
            # If no order_id provided, cancel all orders for the symbol
            if not order_id:
                logger.info("No orderId provided, cancelling all orders for %s", symbol)
                return await self.cancel_all_orders_by_symbol(symbol, demo_mode=demo_mode)

            # Convert to int as required by Hyperliquid SDK
            try:
                order_id_int = int(order_id)
            except (TypeError, ValueError):
                raise ValueError(f"Invalid order ID format: {order_id}")

            # Hyperliquid cancel order by ID: cancel(coin, oid)
            result = await asyncio.to_thread(
                self._exchange.cancel,
                symbol,
                order_id_int
            )

            logger.info("Cancelled plan order: %s order=%s", symbol, order_id_int)

            # BUG FIX #3: Parse response using result.get("ok") not result.get("status")
            if isinstance(result, dict) and result.get("ok"):
                return {
                    "ok": True,
                    "code": "00000",
                    "msg": "Plan order cancelled",
                    "orderId": str(order_id_int),
                }

            # BUG FIX #13: Use standard error code format
            return {
                "ok": False,
                "code": "50000",
                "msg": str(result),
                "orderId": str(order_id_int),
            }

        except Exception as exc:
            logger.error("Failed to cancel plan order: %s", exc)
            # BUG FIX #13: Use standard error code format
            return {
                "ok": False,
                "code": "50000",
                "msg": str(exc),
                "orderId": order_id or "",
            }

    async def list_perp_fills(
        self,
        symbol: str,
        *,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        """List perpetual order fills for a symbol."""
        if not self._settings.has_hyperliquid_credentials():
            return self._wrap_data([])

        try:
            # Use user_fills to get fill history
            fills = await asyncio.to_thread(
                self._info.user_fills,
                self._settings.hyperliquid_wallet_address
            )

            fill_list = []
            if isinstance(fills, list):
                for fill in fills:
                    if not isinstance(fill, dict):
                        continue

                    fill_symbol = fill.get("coin", "")

                    # Filter by symbol
                    if fill_symbol != symbol:
                        continue

                    # BUG FIX #26: Improve side mapping with explicit checks
                    # Hyperliquid uses "B" for buy, "A" for ask/sell
                    fill_side_raw = fill.get("side", "")
                    if fill_side_raw == "B":
                        fill_side = "buy"
                    elif fill_side_raw == "A":
                        fill_side = "sell"
                    else:
                        # Default to sell for unknown values, log warning
                        logger.warning("Unknown fill side value: %s, defaulting to 'sell'", fill_side_raw)
                        fill_side = "sell"

                    # Map Hyperliquid fill format to expected format
                    fill_list.append({
                        "orderId": fill.get("oid", ""),
                        "symbol": fill_symbol,
                        "side": fill_side,
                        "price": fill.get("px", "0"),
                        "size": fill.get("sz", "0"),
                        "fee": fill.get("fee", "0"),
                        "timestamp": fill.get("time", 0),
                        "tradeId": fill.get("tid", ""),
                    })

            return self._wrap_data(fill_list)

        except Exception as exc:
            logger.error("Failed to fetch fills: %s", exc)
            return self._wrap_data([])

    # ==================== HELPER METHODS ====================

    @staticmethod
    def _wrap_data(data: Any) -> Dict[str, Any]:
        """
        Wrap data in standard response format.

        BUG FIX #12: Document response structure.

        Returns:
            {
                "ok": True,           # Success flag
                "code": "00000",      # Error code (00000 = success)
                "msg": "",            # Message (empty on success)
                "raw": {"data": ...}, # Raw data wrapped
                "data_obj": {...},    # Data as dict (or None)
                "data_list": [...],   # Data as list (or [data])
                "data": ...           # Original data
            }
        """
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
        # BUG FIX #6: Use correct field name 'holdSide' not 'posSide'
        data = {
            "orderId": str(uuid.uuid4()),
            "status": "filled",
            "symbol": payload.get("symbol"),
            "route": route,
            "price": payload.get("price"),
            "size": payload.get("size"),
            "holdSide": payload.get("holdSide"),
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
