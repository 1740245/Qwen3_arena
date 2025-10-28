from __future__ import annotations

import asyncio
import logging
import math
from decimal import Decimal, ROUND_DOWN
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..adapters.bitget_client import BitgetClient
from ..config import Settings
from ..schemas import (
    AdventureLogEntry,
    AdventureOrderReceipt,
    BattleAction,
    EncounterOrder,
    GuardrailStatus,
    OrderStyle,
    StopLossMode,
    TriggerSource,
)
from ..utils.branding import sanitize_vendor_terms
from .contract_meta import ContractMeta, ContractMetaCache, DEFAULT_CONTRACT_META
from .price_feed import PriceFeed
from .translators import ExchangePreparation, PokemonTranslator, SpeciesProfile


logger = logging.getLogger(__name__)


@dataclass
class PendingEscapeRope:
    order: EncounterOrder
    prep: ExchangePreparation
    adventure_id: str
    client_oid: str
    stop_reference: str
    embedded: bool = False  # Track if stop-loss was embedded in main order
    sensor_price: Optional[float]
    demo_mode: bool
    created_at: datetime
    attempts: int = 0


class AdventureOrderService:
    """Coordinates between the translator and Bitget client."""

    def __init__(
        self,
        client: BitgetClient,
        translator: PokemonTranslator,
        settings: Settings,
        price_feed: PriceFeed,
    ):
        self._client = client
        self._translator = translator
        self._settings = settings
        self._price_feed = price_feed
        self._cooldown_seconds = settings.cooldown_seconds
        self._minimum_energy_base = settings.minimum_energy_reserve
        self._max_party_size = settings.max_team_size
        self._demo_mode_default = settings.adventure_demo_mode
        self._demo_energy_pool = settings.adventure_demo_energy
        self._energy_scale = settings.adventure_energy_scale_usdt
        self._adventure_log: List[AdventureLogEntry] = []
        self._last_encounter_at: Optional[datetime] = None
        self._last_energy_snapshot: float = 0.0
        self._last_energy_fill: float = 0.0
        self._last_energy_present: bool = False
        self._last_energy_source: str = "none"
        self._last_link_shell_state: str = "offline"
        self._last_demo_mode: bool = self._demo_mode_default
        self._last_guardrails = GuardrailStatus(
            cooldown_seconds=self._cooldown_seconds,
            cooldown_remaining=0.0,
            max_party_size=self._max_party_size,
            minimum_energy=self._minimum_energy_base,
        )
        self._pending_escape_tasks: Dict[str, asyncio.Task[None]] = {}
        self._pending_escape_meta: Dict[str, PendingEscapeRope] = {}
        self._position_mode: Optional[str] = None
        self._contract_meta_cache = ContractMetaCache(client)
        self._last_perp_sides: Dict[str, str] = {}

    async def execute_encounter(self, order: EncounterOrder) -> AdventureOrderReceipt:
        is_demo = self._resolve_demo_flag(order.demo_mode)

        if self._settings.trading_locked and order.action != BattleAction.RUN:
            missing = ", ".join(self._settings.missing_credentials()) or "credentials"
            raise ValueError(
                f"Prof. Oak: Pokégear is missing credentials ({missing}). Toggle demo mode or add keys before live adventures."
            )

        if order.action == BattleAction.RUN:
            return await self._execute_runaway(order, is_demo=is_demo)

        await self._check_cooldown()
        party_snapshot = await self.list_party_status(demo_mode=is_demo)
        party = party_snapshot.get("party", []) if isinstance(party_snapshot, dict) else []

        self._enforce_party_limit(order, party)
        self._enforce_energy_guard(order, self._last_energy_snapshot, is_demo=is_demo)

        adjustments: Dict[str, Any] = {}

        try:
            order = await self._prepare_order(order)

            prep = self._translator.to_exchange_payload(order)
            meta_symbol = prep.profile.perp_symbol if (prep.route == "perp" and prep.profile) else None
            meta = await self._get_contract_meta(meta_symbol)
            if not meta:
                meta = DEFAULT_CONTRACT_META
            order = await self._apply_contract_meta(order, prep, adjustments, meta_override=meta)
            position_mode = None
            if prep.route == "perp":
                position_mode = await self._resolve_position_mode()
            self._apply_position_mode(prep, position_mode, order)
            if prep.route == "perp":
                mode_label = prep.position_mode or position_mode or self._position_mode or "unknown"
                logger.info(
                    "Perp order payload keys (mode=%s): %s",
                    mode_label,
                    ", ".join(sorted(prep.payload.keys())),
                )
            leverage_note: Optional[str] = None

            if prep.route == "perp":
                leverage_applied, leverage_note = await self._clamp_leverage(
                    prep, requested_level=order.level, demo_mode=is_demo
                )
            else:
                leverage_applied = 1

            self._validate_stop_loss(order, prep)

            # EMBED STOP-LOSS BEFORE DISPATCHING ORDER
            stop_loss_reference: Optional[str] = None
            sensor_price: Optional[float] = None
            embedded_stop_loss = False

            if self._requires_stop_loss(order, prep):
                embed_allowed = (
                    prep.route == "perp"
                    and self._settings.adventure_embed_sl
                    and order.stop_loss_mode is not None
                    and order.stop_loss_value is not None
                )

                if embed_allowed:
                    stop_loss_reference = await self._embed_stop_loss(
                        order,
                        prep,
                        adjustments,
                        demo_mode=is_demo,
                    )
                    embedded_stop_loss = True

            # NOW DISPATCH ORDER WITH EMBEDDED STOP-LOSS
            try:
                raw_response = await self._dispatch_order(prep.payload, prep.route, is_demo)
            except (httpx.HTTPStatusError, httpx.RequestError, asyncio.TimeoutError) as exc:  # pragma: no cover - network guard
                self._handle_exchange_error(exc, context="place order", adjustments=adjustments)
            except RuntimeError as exc:  # pragma: no cover - credential guard
                self._handle_exchange_error(exc, context="place order", adjustments=adjustments)
            adventure_id = order.client_adventure_id or self._extract_adventure_id(raw_response)
            filled = self._extract_filled_status(raw_response)
            entry_price = self._resolve_entry_price(order, prep, raw_response)

            # HANDLE NON-EMBEDDED STOP-LOSS (separate orders)
            if self._requires_stop_loss(order, prep) and not embedded_stop_loss:
                if order.stop_loss_mode == StopLossMode.PRICE and order.stop_loss_value is not None:
                    try:
                        stop_loss_reference = await self._attach_stop_loss(
                            order,
                            prep,
                            stop_price=float(order.stop_loss_value),
                            demo_mode=is_demo,
                            adjustments=adjustments,
                        )
                    except (httpx.HTTPStatusError, httpx.RequestError, asyncio.TimeoutError) as exc:  # pragma: no cover - network guard
                        self._handle_exchange_error(exc, context="attach stop loss", adjustments=adjustments)
                    except RuntimeError as exc:  # pragma: no cover - credential guard
                        self._handle_exchange_error(exc, context="attach stop loss", adjustments=adjustments)
                elif order.stop_loss_mode == StopLossMode.PERCENT and order.stop_loss_value is not None:
                    try:
                        sensor_price = await self._fetch_sensor_price(prep, order.stop_loss_trigger, demo_mode=is_demo)
                    except (httpx.HTTPStatusError, httpx.RequestError, asyncio.TimeoutError) as exc:  # pragma: no cover - network guard
                        self._handle_exchange_error(exc, context="fetch stop sensor", adjustments=adjustments)
                    except RuntimeError as exc:  # pragma: no cover - credential guard
                        self._handle_exchange_error(exc, context="fetch stop sensor", adjustments=adjustments)
                    if sensor_price is None:
                        raise ValueError("Pokédex Sensor temporarily offline. Try again shortly.")
                    provisional_stop = self._compute_distance_stop_from_price(
                        prep,
                        order.stop_loss_value,
                        sensor_price,
                    )
                    try:
                        stop_loss_reference = await self._attach_stop_loss(
                            order,
                            prep,
                            stop_price=provisional_stop,
                            demo_mode=is_demo,
                            adjustments=adjustments,
                        )
                    except (httpx.HTTPStatusError, httpx.RequestError, asyncio.TimeoutError) as exc:  # pragma: no cover - network guard
                        self._handle_exchange_error(exc, context="attach stop loss", adjustments=adjustments)
                    except RuntimeError as exc:  # pragma: no cover - credential guard
                        self._handle_exchange_error(exc, context="attach stop loss", adjustments=adjustments)
                    self._append_log(
                        message="Escape Rope armed. Awaiting fine-tune...",
                        badge=None,
                        payload={
                            "species": order.species,
                            "mode": "distance",
                            "provisional": provisional_stop,
                        },
                    )
                    if not embedded_stop_loss:
                        order_clone = (
                            order.model_copy(deep=True)
                            if hasattr(order, "model_copy")
                            else order.copy(deep=True)  # type: ignore[attr-defined]
                        )
                        self._schedule_escape_rope_adjustment(
                            PendingEscapeRope(
                                order=order_clone,
                                prep=prep,
                                adventure_id=adventure_id,
                                client_oid=prep.client_oid,
                                stop_reference=stop_loss_reference,
                                embedded=embedded_stop_loss,
                                sensor_price=sensor_price,
                                demo_mode=is_demo,
                                created_at=datetime.now(timezone.utc),
                            )
                        )
            narration = self._friendly_message(
                species=order.species,
                action=order.action,
                route=prep.route,
                leverage=leverage_applied,
                direction=prep.direction,
                leverage_note=leverage_note,
                stop_loss_ref=stop_loss_reference,
                stop_loss_mode=order.stop_loss_mode,
                quote_hp=order.quote_hp,
                level=order.level,
            )

            receipt = AdventureOrderReceipt(
                adventure_id=adventure_id,
                species=order.species,
                action=order.action,
                filled=filled,
                fill_price=entry_price,
                fill_size=self._extract_fill_size(raw_response),
                level_used=order.level,
                leverage_applied=None if prep.route == "spot" else leverage_applied,
                demo_mode=is_demo,
                badge=self._badge_for_action(order.action),
                narration=narration,
                stop_loss_reference=stop_loss_reference,
                raw_response=raw_response,
                normalized_price=str(adjustments.get("rounded_price")) if adjustments.get("rounded_price") else None,
                normalized_trigger_price=str(adjustments.get("rounded_stop")) if adjustments.get("rounded_stop") else None,
                price_scale=adjustments.get("price_scale"),
                price_tick_formatted=str(adjustments.get("price_tick_formatted")) if adjustments.get("price_tick_formatted") else None,
            )
            self._append_log(
                message=narration,
                badge=receipt.badge,
                payload={
                    "payload": prep.payload,
                    "response": raw_response,
                    "route": prep.route,
                    "direction": prep.direction,
                    "stop_loss": stop_loss_reference,
                    "level": order.level,
                    "demo": is_demo,
                },
            )
            self._last_encounter_at = datetime.now(timezone.utc)
            return receipt
        except ValueError:
            raise
        except Exception as exc:
            logger.exception("Unexpected encounter error", exc_info=exc)
            sanitized = sanitize_vendor_terms(str(exc)) or ""
            hint = self._format_scale_step_hint(sanitized, adjustments)
            if hint:
                raise ValueError(hint) from None
            short_reason = sanitized or "Something went wrong."
            raise ValueError(f"Prof. Oak: couldn't throw that ball. {short_reason}") from None

    async def list_party_status(self, demo_mode: Optional[bool] = None) -> Dict[str, object]:
        is_demo = self._resolve_demo_flag(demo_mode)
        self._last_demo_mode = is_demo

        if is_demo or self._settings.trading_locked:
            reason = "demo" if is_demo else "credentials"
            logger.debug("Skipping live balance poll (reason=%s)", reason)
            self._position_mode = None
            return self._offline_status(reason)

        try:
            summary = await self._client.list_balances()
        except Exception as exc:
            logger.warning("Link shell energy fetch failed: %s", exc)
            return self._offline_status("offline")

        if not isinstance(summary, dict):
            return self._offline_status("offline")

        total = summary.get("total")
        if total is None:
            return self._offline_status("offline")

        try:
            total_value = float(total)
        except (TypeError, ValueError):
            return self._offline_status("offline")

        available_raw = summary.get("available")
        try:
            available_value = float(available_raw) if available_raw is not None else None
        except (TypeError, ValueError):
            available_value = None

        perp_value = summary.get("perp")
        spot_value = summary.get("spot")
        has_perp = isinstance(perp_value, (int, float))
        has_spot = isinstance(spot_value, (int, float))
        if has_perp and not isinstance(perp_value, float):
            perp_value = float(perp_value)
        if has_spot and not isinstance(spot_value, float):
            spot_value = float(spot_value)
        source_label = self._combined_energy_source(has_perp, has_spot)

        display_source = self._settings.adventure_energy_source
        if display_source == "perp":
            display_value = perp_value if has_perp else None
            display_origin = "perp" if has_perp else ("fallback" if has_spot else "none")
        else:
            display_value = total_value
            display_origin = source_label if source_label != "none" else "fallback"

        if display_value is None:
            return self._offline_status("offline")

        fill = 0.0
        if self._energy_scale > 0:
            fill = max(0.0, min(1.0, display_value / self._energy_scale))

        await self._refresh_position_mode()

        party_list, _ = await self.fetch_party_positions(demo_mode=is_demo, suppress_errors=True)

        return self._status_payload(
            link_shell="online",
            energy_present=True,
            fill=fill,
            source=display_origin,
            total=display_value,
            available=available_value,
            true_total=total_value,
            party=party_list,
        )

    def _offline_status(self, reason: str) -> Dict[str, object]:
        logger.debug("Producing offline trainer snapshot (reason=%s)", reason)
        return self._status_payload(
            link_shell="offline",
            energy_present=False,
            fill=0.0,
            source="none",
            total=None,
            available=None,
            true_total=None,
        )

    def _status_payload(
        self,
        *,
        link_shell: str,
        energy_present: bool,
        fill: float,
        source: str,
        total: Optional[float],
        available: Optional[float] = None,
        true_total: Optional[float] = None,
        party: Optional[List[Dict[str, object]]] = None,
    ) -> Dict[str, object]:
        party_list = party or []
        clamped_fill = max(0.0, min(1.0, float(fill))) if energy_present else 0.0
        self._last_energy_fill = clamped_fill
        self._last_energy_present = energy_present
        self._last_energy_source = source
        self._last_link_shell_state = link_shell
        actual_total = true_total if true_total is not None else total
        self._last_energy_snapshot = float(actual_total) if (energy_present and actual_total is not None) else 0.0
        self._update_guardrails(len(party_list))
        def _convert(value: Optional[float]) -> Optional[float]:
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return {
            "party": party_list,
            "raw": {},
            "linkShell": link_shell,
            "energy": {
                "present": energy_present,
                "fill": clamped_fill,
                "source": source,
                "unit": "USDT",
                "value": float(total) if (energy_present and total is not None) else None,
                "available": _convert(available) if energy_present else None,
                "total": _convert(actual_total) if energy_present else None,
                "showNumbers": self._settings.adventure_show_energy_numbers,
            },
            "guardrails": self.guardrails(),
            "positionMode": self._position_mode,
        }

    @staticmethod
    def _combined_energy_source(has_perp: bool, has_spot: bool) -> str:
        if has_perp and has_spot:
            return "both"
        if has_perp:
            return "perp"
        if has_spot:
            return "spot"
        return "none"

    def list_recent_events(self) -> List[AdventureLogEntry]:
        return self._adventure_log[-50:]

    def guardrails(self) -> GuardrailStatus:
        return self._last_guardrails

    def position_mode(self) -> Optional[str]:
        return self._position_mode

    def energy_percent(self) -> float:
        return self._last_energy_fill

    def last_energy_amount(self) -> float:
        return self._last_energy_snapshot

    def link_shell_state(self) -> str:
        return self._last_link_shell_state

    def energy_present(self) -> bool:
        return self._last_energy_present

    async def fetch_party_positions(
        self,
        *,
        demo_mode: bool = False,
        suppress_errors: bool = True,
    ) -> Tuple[List[Dict[str, Any]], Any]:
        """Return normalized party positions and raw entries."""

        if demo_mode or self._settings.trading_locked or not self._settings.has_api_credentials():
            return [], []

        try:
            payload = await self._client.list_perp_positions(demo_mode=demo_mode)
        except Exception as exc:
            if suppress_errors:
                logger.debug("Party positions fetch failed: %s", exc)
                return [], []
            raise

        return self._normalize_party_payload(payload)

    async def list_open_orders_by_species(
        self,
        demo_mode: Optional[bool] = None,
    ) -> Dict[str, Dict[str, Any]]:
        is_demo = self._resolve_demo_flag(demo_mode)
        if is_demo or self._settings.trading_locked or not self._settings.has_api_credentials():
            return {}

        entries: List[Dict[str, Any]] = []

        try:
            perp_payload = await self._client.list_open_perp_orders(demo_mode=is_demo)
        except Exception as exc:
            logger.debug("Perp open orders summary fetch failed: %s", exc)
            perp_payload = {}

        entries.extend(self._payload_entries(perp_payload))

        # Hyperliquid only supports perpetuals, no spot trading

        if not entries:
            return {}

        summary: Dict[str, Dict[str, Any]] = {}

        for entry in entries:
            normalized = self._normalize_open_order_entry(entry)
            if not normalized:
                continue

            species = normalized.get("species")
            if not species:
                continue

            symbol = normalized.get("symbol")
            element = normalized.get("element")
            sprite = normalized.get("sprite")
            entry_payload = {
                key: value
                for key, value in normalized.items()
                if key not in {"species", "symbol", "element", "sprite"}
            }

            bucket = summary.setdefault(
                species,
                {
                    "symbol": symbol,
                    "element": element,
                    "sprite": sprite,
                    "entries": [],
                },
            )

            if symbol and not bucket.get("symbol"):
                bucket["symbol"] = symbol
            if element and not bucket.get("element"):
                bucket["element"] = element
            if sprite and not bucket.get("sprite"):
                bucket["sprite"] = sprite

            bucket["entries"].append(entry_payload)

        for bucket in summary.values():
            bucket["entries"].sort(key=lambda item: item.get("updatedTs") or 0, reverse=True)
            bucket["entries"] = bucket["entries"][:2]

        return summary

    async def cancel_all_orders_for_species(
        self,
        species: str,
        *,
        demo_mode: Optional[bool] = None,
    ) -> Dict[str, Any]:
        is_demo = self._resolve_demo_flag(demo_mode)

        try:
            profile = self._translator.species_to_profile(species)
        except ValueError as exc:
            raise ValueError(f"Unknown species: {species}") from exc

        base_symbol = profile.perp_symbol or profile.spot_symbol
        if not base_symbol:
            raise ValueError(f"{species} does not have a tradable symbol configured.")

        # Hyperliquid uses cancel_all_orders_by_symbol instead of per-order cancellation
        cancelled_records: List[Dict[str, Any]] = []
        failed_records: List[Dict[str, Any]] = []

        try:
            response = await self._client.cancel_all_orders_by_symbol(
                symbol=base_symbol,
                demo_mode=is_demo,
            )

            # Parse Hyperliquid response format
            if isinstance(response, dict):
                # Hyperliquid returns {"ok": True, ...} not {"status": "ok"}
                if response.get("ok"):
                    # Successful cancellation
                    cancelled_records.append({
                        "symbol": base_symbol,
                        "ok": True,
                        "msg": "All orders cancelled",
                    })
                else:
                    # Failed cancellation
                    failed_records.append({
                        "symbol": base_symbol,
                        "ok": False,
                        "msg": response.get("response", str(response)),
                    })
        except Exception as exc:
            logger.warning(
                "Cancel all orders failed (species=%s symbol=%s): %s",
                species,
                base_symbol,
                exc,
            )
            failed_records.append({
                "symbol": base_symbol,
                "ok": False,
                "msg": str(exc),
            })

        success = bool(cancelled_records) and not failed_records
        if success:
            logger.info(
                "Cancelled all open orders for %s (%s)",
                species,
                base_symbol,
            )
        else:
            logger.warning(
                "Cancel orders incomplete (species=%s symbol=%s cancelled=%d failed=%d)",
                species,
                base_symbol,
                len(cancelled_records),
                len(failed_records),
            )

        return {
            "ok": success,
            "species": species,
            "symbol": base_symbol,
            "cancelled": cancelled_records,
            "failed": failed_records,
            "cancelled_count": len(cancelled_records),
        }

    @staticmethod
    def _symbol_candidates(symbol: str) -> List[str]:
        normalized = (symbol or "").upper()
        candidates: List[str] = []
        if normalized:
            candidates.append(normalized)
            if normalized.endswith("_UMCBL"):
                base = normalized[:-6]
                if base:
                    candidates.append(base)
            else:
                candidates.append(f"{normalized}_UMCBL")
        seen: set[str] = set()
        ordered: List[str] = []
        for candidate in candidates:
            if candidate and candidate not in seen:
                ordered.append(candidate)
                seen.add(candidate)
        return ordered

    @staticmethod
    def _extract_mix_order_entries(payload: Any) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []

        def extend(value: Optional[Any]) -> None:
            if isinstance(value, list):
                entries.extend([item for item in value if isinstance(item, dict)])

        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                extend(data.get("entrustedList"))
                extend(data.get("list"))
                extend(data.get("orderList"))
                if not entries and all(
                    key not in data for key in ("entrustedList", "list", "orderList")
                ):
                    entries.append(data)
            elif isinstance(data, list):
                extend(data)

            extend(payload.get("entrustedList"))
            extend(payload.get("list"))
        elif isinstance(payload, list):
            extend(payload)

        return entries

    def _normalize_party_payload(self, payload: Any) -> Tuple[List[Dict[str, Any]], Any]:
        if not isinstance(payload, dict):
            return [], payload

        entries_source: Any = payload.get("data")
        if isinstance(entries_source, dict):
            entries: List[Any] = [entries_source]
        elif isinstance(entries_source, list):
            entries = entries_source
        else:
            fallback = payload.get("data_list")
            entries = fallback if isinstance(fallback, list) else []

        normalized: List[Dict[str, Any]] = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            symbol = entry.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                continue
            symbol = symbol.upper()

            amount_value = self._pick_party_amount(entry)
            amount_usdt = abs(amount_value) if amount_value is not None else 0.0

            try:
                core = self._translator.describe_balance(symbol=symbol, amount=amount_usdt)
            except ValueError:
                core = {
                    "species": symbol,
                    "hp": 0.0,
                    "symbol": symbol,
                    "element": entry.get("marginCoin", ""),
                    "sprite": "",
                    "amount": amount_usdt,
                }

            core.update(
                {
                    "amount": amount_usdt,
                    "holdSide": entry.get("holdSide"),
                    "marginMode": entry.get("marginMode"),
                    "leverage": entry.get("leverage"),
                    "avgOpenPrice": self._to_float(entry.get("avgOpenPrice") or entry.get("openPriceAvg")),
                    "unrealizedPnL": self._to_float(entry.get("unrealizedPL")),
                    "usdtValue": self._to_float(entry.get("usdtValue")),
                    "equity": self._to_float(entry.get("equity")),
                    "margin": self._to_float(entry.get("margin")),
                    "positionMargin": self._to_float(entry.get("positionMargin")),
                    "cTime": entry.get("cTime"),
                    "uTime": entry.get("uTime"),
                    "productType": entry.get("productType"),
                    "symbol": symbol,
                }
            )

            normalized.append(core)

        normalized.sort(key=lambda item: item.get("amount", 0.0) or 0.0, reverse=True)

        return normalized, entries

    def _pick_party_amount(self, entry: Dict[str, Any]) -> Optional[float]:
        amount_candidates = (
            entry.get("usdtValue"),
            entry.get("equity"),
            entry.get("positionMargin"),
            entry.get("margin"),
            entry.get("quote"),
        )
        for candidate in amount_candidates:
            numeric = self._to_float(candidate)
            if numeric is not None:
                return numeric

        size_candidates = (
            entry.get("total"),
            entry.get("base"),
            entry.get("baseSize"),
            entry.get("available"),
        )
        for candidate in size_candidates:
            numeric = self._to_float(candidate)
            if numeric is not None:
                return numeric

        return None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return numeric

    async def _dispatch_order(self, payload: Dict[str, Any], route: str, demo_mode: bool) -> Dict[str, object]:
        if route == "perp":
            logger.info("Dispatching perp order with keys: %s", ", ".join(sorted(payload.keys())))


            return await self._client.place_perp_order(payload, demo_mode=demo_mode)
        # Hyperliquid only supports perpetual orders, no spot markets
        raise ValueError(f"Unsupported order route: {route}. Hyperliquid only supports 'perp' orders.")

    async def _get_contract_meta(self, symbol: Optional[str]) -> ContractMeta:
        candidates = self._symbol_candidates(symbol) if symbol else []
        for candidate in candidates:
            meta = await self._contract_meta_cache.get(candidate)
            if meta is not None:
                return meta

        fallback_symbol = candidates[0] if candidates else (symbol.upper() if symbol else "DEFAULT")

        profile = self._translator.symbol_to_profile(fallback_symbol)
        precision_price = None
        precision_size = None
        if profile:
            precision_price = (
                profile.perp_pip_precision
                if profile.perp_pip_precision is not None
                else profile.pip_precision
            )
            precision_size = (
                profile.perp_size_precision
                if profile.perp_size_precision is not None
                else profile.size_precision
            )

        price_scale = (
            int(precision_price)
            if isinstance(precision_price, int) and precision_price >= 0
            else DEFAULT_CONTRACT_META.price_scale
        )
        size_scale = (
            int(precision_size)
            if isinstance(precision_size, int) and precision_size >= 0
            else DEFAULT_CONTRACT_META.size_scale
        )

        price_tick = DEFAULT_CONTRACT_META.price_tick
        if isinstance(precision_price, int) and precision_price >= 0:
            price_tick = float(10 ** (-precision_price))

        size_tick = DEFAULT_CONTRACT_META.size_tick
        if isinstance(precision_size, int) and precision_size >= 0:
            size_tick = float(10 ** (-precision_size))

        return ContractMeta(
            symbol=fallback_symbol,
            price_scale=price_scale,
            size_scale=size_scale,
            price_tick=price_tick,
            size_tick=size_tick,
            min_size=DEFAULT_CONTRACT_META.min_size,
        )

    @staticmethod
    def _coerce_contract_meta_precision(meta: ContractMeta, profile: Optional[SpeciesProfile]) -> ContractMeta:
        if not profile or not isinstance(meta, ContractMeta):
            return meta

        price_precision = (
            profile.perp_pip_precision
            if profile.perp_pip_precision is not None
            else profile.pip_precision
        )
        size_precision = (
            profile.perp_size_precision
            if profile.perp_size_precision is not None
            else profile.size_precision
        )

        updated = meta
        if isinstance(price_precision, int) and price_precision >= 0:
            desired_tick = float(10 ** (-price_precision))
            if not math.isclose(meta.price_tick, desired_tick) or meta.price_scale != price_precision:
                updated = replace(updated, price_tick=desired_tick, price_scale=price_precision)

        if isinstance(size_precision, int) and size_precision >= 0:
            desired_size_tick = float(10 ** (-size_precision))
            if not math.isclose(updated.size_tick, desired_size_tick) or updated.size_scale != size_precision:
                updated = replace(updated, size_tick=desired_size_tick, size_scale=size_precision)

        return updated

    @staticmethod
    def _copy_order(order: EncounterOrder, updates: Dict[str, object]) -> EncounterOrder:
        if not updates:
            return order
        if hasattr(order, "model_copy"):
            return order.model_copy(update=updates)
        return order.copy(update=updates)  # type: ignore[attr-defined]

    async def _apply_contract_meta(
        self,
        order: EncounterOrder,
        prep: ExchangePreparation,
        adjustments: Dict[str, Any],
        *,
        meta_override: Optional[ContractMeta] = None,
    ) -> EncounterOrder:
        if prep.route != "perp":
            return order

        symbol = prep.profile.perp_symbol if prep.profile else None
        meta = meta_override or await self._get_contract_meta(symbol)
        meta = self._coerce_contract_meta_precision(meta, prep.profile)
        prep.contract_meta = meta

        updates: Dict[str, object] = {}
        adjustments.setdefault("price_scale", meta.price_scale)
        adjustments.setdefault("price_tick_formatted", meta.format_price(meta.price_tick))

        quant_size = meta.quantize_size(order.pokeball_strength)
        if not math.isclose(quant_size, order.pokeball_strength):
            updates["pokeball_strength"] = quant_size
        if quant_size <= 0:
            minimum_tick = meta.format_size(meta.size_tick)
            raise ValueError(
                f"Prof. Oak: contract size must be at least {minimum_tick}."
            )
        if meta.min_size and quant_size < meta.min_size:
            minimum_display = meta.format_size(meta.min_size)
            raise ValueError(
                f"Prof. Oak: minimum contract size is {minimum_display}."
            )
        size_formatted = meta.format_size(quant_size)
        prep.payload["size"] = size_formatted
        adjustments["rounded_qty"] = size_formatted

        # Helper function to quantize prices using profile precision when available
        def _quantize_with_profile_precision(value: float) -> Decimal:
            # Prioritize profile-based precision for high-precision symbols
            profile = prep.profile
            precision = None
            if profile:
                if prep.route == "perp" and getattr(profile, "perp_pip_precision", None) is not None:
                    precision = profile.perp_pip_precision
                elif getattr(profile, "pip_precision", None) is not None:
                    precision = profile.pip_precision

            if isinstance(precision, int) and precision >= 0:
                tick = Decimal("1").scaleb(-precision)
                base = Decimal(str(value))
                try:
                    return base.quantize(tick, rounding=ROUND_DOWN)
                except Exception:
                    pass

            # Fallback to contract metadata quantization
            return meta.quantize_price(value)

        if order.order_style == OrderStyle.LIMIT and order.limit_price is not None:
            quant_price = _quantize_with_profile_precision(order.limit_price)
            updates["limit_price"] = float(quant_price)
            rounded_price = self._format_price(prep.profile, prep.route, float(quant_price))
            prep.payload["price"] = rounded_price
            adjustments["rounded_price"] = rounded_price

        if order.stop_loss_mode == StopLossMode.PRICE and order.stop_loss_value is not None:
            quant_stop = _quantize_with_profile_precision(order.stop_loss_value)
            updates["stop_loss_value"] = float(quant_stop)
            adjustments["rounded_stop"] = self._format_price(prep.profile, prep.route, float(quant_stop))

        return self._copy_order(order, updates)

    async def _resolve_position_mode(self) -> Optional[str]:
        if not self._settings.has_api_credentials():
            self._position_mode = None
            return None
        try:
            mode = await self._client.get_position_mode()
            if mode:
                self._position_mode = mode
            return mode
        except RuntimeError:
            return self._position_mode
        except httpx.HTTPStatusError as exc:
            logger.debug("Position mode probe failed (%s): %s", exc.response.status_code, exc)
        except (httpx.RequestError, asyncio.TimeoutError) as exc:
            logger.debug("Position mode probe network error: %s", exc)
        return self._position_mode

    def _apply_position_mode(
        self,
        prep: ExchangePreparation,
        position_mode: Optional[str],
        order: EncounterOrder,
    ) -> None:
        if prep.route != "perp":
            return
        normalized = position_mode if position_mode in {"one_way", "hedge"} else None
        symbol = prep.profile.perp_symbol if prep.profile else None
        if normalized == "one_way":
            original = dict(prep.payload)
            prep.payload.clear()
            prep.hold_side = None
            prep.position_mode = "one_way"
            self._position_mode = "one_way"
            if symbol:
                self._last_perp_sides.pop(symbol, None)

            price = original.get("price")
            force_value = original.get("force")
            if not force_value:
                tif_value = original.get("timeInForceValue")
                if isinstance(tif_value, str):
                    force_value = "gtc" if tif_value.lower() in {"normal", "gtc"} else tif_value

            reduce_only_value: Optional[str] = None
            if order.action == BattleAction.RUN:
                reduce_only_value = "YES"

            cleaned_payload = {
                "symbol": original.get("symbol"),
                "productType": original.get("productType", "USDT-FUTURES"),
                "marginMode": original.get("marginMode"),
                "marginCoin": original.get("marginCoin"),
                "posSide": original.get("posSide"),
                "size": original.get("size"),
                "orderType": original.get("orderType"),
                "side": original.get("side"),
                "clientOid": original.get("clientOid"),
                "timeInForceValue": original.get("timeInForceValue"),
                "leverage": original.get("leverage"),
            }
            for optional_key in (
                "price",
                "presetStopLossPrice",
                "presetStopLossTriggerPrice",
                "presetStopLossTriggerType",
                "presetStopLossExecutePrice",
            ):
                if optional_key not in cleaned_payload:
                    value = original.get(optional_key)
                    if value is not None:
                        cleaned_payload[optional_key] = value
            if price is not None:
                cleaned_payload["price"] = price
            if force_value:
                cleaned_payload["force"] = force_value
            if reduce_only_value:
                cleaned_payload["reduceOnly"] = reduce_only_value

            for key, value in cleaned_payload.items():
                if value is not None:
                    prep.payload[key] = value
        else:
            trade_mode = normalized or "hedge"
            prep.position_mode = trade_mode
            if normalized in {"one_way", "hedge"}:
                self._position_mode = normalized
            prep.payload.pop("reduceOnly", None)
            prep.payload.pop("positionSide", None)
            if prep.hold_side:
                prep.payload["holdSide"] = prep.hold_side
                if symbol:
                    self._last_perp_sides[symbol] = prep.hold_side
            elif symbol:
                self._last_perp_sides.pop(symbol, None)
            trade_side = "close" if order.action == BattleAction.RUN else "open"
            prep.payload["tradeSide"] = trade_side

    async def _refresh_position_mode(self) -> Optional[str]:
        if self._settings.trading_locked or not self._settings.has_api_credentials():
            self._position_mode = None
            return None
        try:
            mode = await self._client.get_position_mode()
        except httpx.HTTPStatusError as exc:
            logger.debug("Position mode refresh failed (%s): %s", exc.response.status_code, exc)
            return self._position_mode
        except (httpx.RequestError, asyncio.TimeoutError) as exc:
            logger.debug("Position mode refresh network error: %s", exc)
            return self._position_mode
        if mode:
            self._position_mode = mode
        return self._position_mode

    async def _flash_close_perp(
        self,
        symbol: str,
        position_mode: Optional[str],
        hold_side: Optional[str],
        demo_mode: bool,
    ) -> bool:
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
        }
        if position_mode == "hedge" and hold_side:
            payload["holdSide"] = hold_side

        logger.info("Perp close request keys: %s", ", ".join(sorted(payload.keys())))

        try:
            await self._client.close_perp_positions(payload, demo_mode=demo_mode)
            return True
        except httpx.HTTPStatusError as exc:
            code, message = self._extract_exchange_error(exc)
            if (code and str(code) == "40774") or (message and "unilateral" in message.lower()):
                raise ValueError("Mode is ONE-WAY. I'll send one-way orders (no side field).") from exc
            logger.warning(
                "Flash close failed for %s (status=%s, code=%s): %s",
                symbol,
                exc.response.status_code,
                code,
                message,
            )
            return False
        except (httpx.RequestError, asyncio.TimeoutError) as exc:
            logger.warning("Flash close network error for %s: %s", symbol, exc)
            return False

    @staticmethod
    def _extract_exchange_error(exc: httpx.HTTPStatusError) -> Tuple[Optional[str], Optional[str]]:
        try:
            data = exc.response.json()
        except ValueError:
            text = exc.response.text
            return None, text
        if not isinstance(data, dict):
            return None, exc.response.text
        code = data.get("code") or data.get("errorCode") or data.get("status")
        message = (
            data.get("msg")
            or data.get("message")
            or data.get("detail")
            or exc.response.text
        )
        return (str(code) if code is not None else None, message)

    def _append_log(self, message: str, payload: Dict[str, object], badge: Optional[str] = None) -> None:
        message = sanitize_vendor_terms(message) or message
        self._adventure_log.append(
            AdventureLogEntry(
                event_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                message=message,
                badge=badge,
                payload=payload,
            )
        )

    def _friendly_message(
        self,
        species: str,
        action: BattleAction,
        route: str,
        leverage: int,
        direction: str,
        leverage_note: Optional[str],
        stop_loss_ref: Optional[str],
        stop_loss_mode: Optional[StopLossMode],
        quote_hp: Optional[float],
        level: int,
    ) -> str:
        prefix = ""
        if quote_hp is not None:
            hp_text = self._format_quote_amount(float(quote_hp))
            effective_level = max(1, int(level))
            notional = float(quote_hp) * float(effective_level)
            notional_text = self._format_quote_amount(notional)
            prefix = f"Trainer planned HP {hp_text} at LV{effective_level} (notional ~ {notional_text}). "

        if action == BattleAction.CATCH and direction == "long":
            base = f"Trainer opened a long adventure with {species} at LV{leverage}."
        elif action == BattleAction.RELEASE and direction == "short":
            base = f"Trainer launched a short ambush on {species} at LV{leverage}."
        elif route == "spot" and action == BattleAction.CATCH:
            base = f"Trainer threw a Poké Ball at {species}."
        elif route == "spot" and action == BattleAction.RELEASE:
            base = f"Trainer released {species} back to the wild."
        else:
            base = f"Trainer guided {species}."

        if leverage_note:
            base = f"{base} {leverage_note}"

        if stop_loss_ref and stop_loss_mode:
            mode_label = "Anchor" if stop_loss_mode == StopLossMode.PRICE else "Distance (%)"
            base = f"{base} Escape Rope armed ({mode_label})."
        elif stop_loss_ref:
            base = f"{base} Escape Rope armed."

        message = f"{prefix}{base}".strip()
        return message

    def _requires_stop_loss(self, order: EncounterOrder, prep: ExchangePreparation) -> bool:
        if order.action == BattleAction.CATCH:
            return True
        if order.action == BattleAction.RELEASE and prep.route == "perp":
            return True
        return False

    def _validate_stop_loss(self, order: EncounterOrder, prep: ExchangePreparation) -> None:
        if not self._requires_stop_loss(order, prep):
            return
        if order.stop_loss_mode is None or order.stop_loss_value is None:
            raise ValueError("Professor Elm insists you equip an Escape Rope before entering this encounter.")
        if order.stop_loss_mode == StopLossMode.PRICE and order.stop_loss_value <= 0:
            raise ValueError("Anchor must be greater than zero.")
        if order.stop_loss_mode == StopLossMode.PERCENT and order.stop_loss_value <= 0:
            raise ValueError("Distance must be a positive percentage.")

        if order.stop_loss_mode == StopLossMode.PRICE and order.order_style == OrderStyle.LIMIT and order.limit_price is not None:
            # Prioritize profile-based precision for high-precision symbols like Umbreon and Heracross
            tick: Optional[Decimal] = None

            # Get profile for precision calculation
            profile = getattr(prep, "profile", None)

            # First try to get precision from species profile
            if profile:
                precision = None
                if prep.route == "perp" and getattr(profile, "perp_pip_precision", None) is not None:
                    precision = profile.perp_pip_precision
                elif getattr(profile, "pip_precision", None) is not None:
                    precision = profile.pip_precision

                if isinstance(precision, int) and precision >= 0:
                    tick = Decimal("1").scaleb(-precision)

            # If no profile precision, try contract metadata
            if tick is None or tick <= 0:
                meta = getattr(prep, "contract_meta", None)
                if meta:
                    try:
                        tick_value = getattr(meta, "price_tick", None)
                        if tick_value:
                            tick = Decimal(str(tick_value))
                    except Exception:
                        pass

            # Final fallback to default
            if tick is None or tick <= 0:
                tick = Decimal(str(DEFAULT_CONTRACT_META.price_tick))

            def _quantize(value: float) -> Decimal:
                base = Decimal(str(value))
                try:
                    return base.quantize(tick, rounding=ROUND_DOWN)
                except Exception:
                    return base

            comparison_stop = _quantize(float(order.stop_loss_value))
            comparison_limit = _quantize(float(order.limit_price))

            if prep.direction in {"long", "spot_long"} and comparison_stop >= comparison_limit:
                raise ValueError("Your Escape Rope must be set below your anchor point for a long.")
            if prep.direction == "short" and comparison_stop <= comparison_limit:
                raise ValueError("Your Escape Rope must be set above your anchor point for a short.")

    async def _clamp_leverage(
        self,
        prep: ExchangePreparation,
        *,
        requested_level: int,
        demo_mode: bool,
    ) -> tuple[int, Optional[str]]:
        symbol = prep.profile.perp_symbol
        if not symbol:
            return requested_level, None
        try:
            response = await self._client.get_perp_contract(symbol)
        except Exception:
            return requested_level, None

        data = response.get("data")
        if isinstance(data, list) and data:
            contract = data[0]
        elif isinstance(data, dict):
            contract = data
        else:
            contract = {}
        max_leverage = contract.get("maxLever") or contract.get("maxLeverage")
        try:
            max_leverage = int(float(max_leverage)) if max_leverage is not None else requested_level
        except (TypeError, ValueError):
            max_leverage = requested_level

        applied = min(requested_level, max_leverage)
        prep.payload["leverage"] = str(applied)
        note = None
        if applied < requested_level:
            note = f"League cap set LV to {applied}."
        return applied, note

    def _resolve_entry_price(
        self,
        order: EncounterOrder,
        prep: ExchangePreparation,
        response: Dict[str, object],
    ) -> float | None:
        price = self._extract_fill_price(response)
        if price is None and order.limit_price is not None:
            price = float(order.limit_price)
        return float(price) if price is not None else None

    def _derive_stop_loss_price(
        self,
        order: EncounterOrder,
        prep: ExchangePreparation,
        entry_price: float,
    ) -> float:
        if not self._requires_stop_loss(order, prep):
            return 0.0
        assert order.stop_loss_value is not None
        if order.stop_loss_mode == StopLossMode.PRICE:
            target = float(order.stop_loss_value)
        else:
            ratio = float(order.stop_loss_value) / 100.0
            if prep.direction in {"long", "spot_long"}:
                target = entry_price * (1 - ratio)
            else:
                target = entry_price * (1 + ratio)

        if prep.direction in {"long", "spot_long"} and target >= entry_price:
            raise ValueError("Your Escape Rope must be set below your anchor point for a long.")
        if prep.direction == "short" and target <= entry_price:
            raise ValueError("Your Escape Rope must be set above your anchor point for a short.")
        if target <= 0:
            raise ValueError("Anchor must be greater than zero.")
        return float(target)

    async def _attach_stop_loss(
        self,
        order: EncounterOrder,
        prep: ExchangePreparation,
        stop_price: float,
        *,
        demo_mode: bool,
        adjustments: Dict[str, Any],
    ) -> str:
        if prep.route == "spot":
            # Hyperliquid only supports perpetual markets, no spot trading
            raise ValueError("Spot stop-loss orders are not supported on Hyperliquid. Only perpetual markets are available.")
        else:
            meta = prep.contract_meta or await self._get_contract_meta(prep.profile.perp_symbol)
            formatted_stop = self._format_price(prep.profile, prep.route, stop_price)
            adjustments.setdefault("price_scale", meta.price_scale)
            adjustments["rounded_stop"] = formatted_stop
            payload = {
                "symbol": prep.profile.perp_symbol,
                "marginCoin": "USDT",
                "planType": "sl",
                "stopLossTriggerPrice": formatted_stop,
                "stopLossTriggerType": order.stop_loss_trigger.value,
                "triggerPrice": formatted_stop,
                "size": meta.format_size(order.pokeball_strength),
            }
            if prep.hold_side:
                payload["holdSide"] = prep.hold_side
            response = await self._client.place_perp_stop_loss(payload, demo_mode=demo_mode)
            entry = self._first_payload_entry(response)
            reference = entry.get("tpslId") or entry.get("orderId")

        if not reference:
            raise ValueError("Escape Rope setup failed; the Professor suggests retrying.")
        return str(reference)

    async def _embed_stop_loss(
        self,
        order: EncounterOrder,
        prep: ExchangePreparation,
        adjustments: Dict[str, Any],
        *,
        demo_mode: bool,
    ) -> str:
        # Validate profile exists
        if not prep.profile:
            raise ValueError("Cannot embed stop-loss: profile is missing")

        meta = prep.contract_meta or await self._get_contract_meta(prep.profile.perp_symbol if prep.profile else None)
        if meta is None:
            meta = DEFAULT_CONTRACT_META

        if order.stop_loss_mode == StopLossMode.PRICE:
            target = float(order.stop_loss_value)
            entry_reference = (
                float(order.limit_price)
                if order.order_style == OrderStyle.LIMIT and order.limit_price is not None
                else None
            )
        else:
            if order.order_style == OrderStyle.LIMIT and order.limit_price is not None:
                entry_reference = float(order.limit_price)
            else:
                reference_price = await self._fetch_sensor_price(prep, order.stop_loss_trigger, demo_mode=demo_mode)
                if reference_price is None:
                    raise ValueError("Pokédex Sensor temporarily offline. Try again shortly.")
                entry_reference = reference_price
            target = self._compute_distance_stop_from_price(prep, order.stop_loss_value, entry_reference)

        if target <= 0:
            raise ValueError("Escape Rope calculation failed.")

        quant_price = meta.quantize_price(target)
        tick = getattr(meta, "price_tick", DEFAULT_CONTRACT_META.price_tick)

        entry_check = None
        if order.stop_loss_mode == StopLossMode.PRICE and order.order_style == OrderStyle.LIMIT and order.limit_price is not None:
            entry_check = float(order.limit_price)
        elif order.stop_loss_mode == StopLossMode.PERCENT:
            entry_check = entry_reference

        if entry_check is not None:
            if prep.direction in {"long", "spot_long"} and quant_price >= entry_check:
                quant_price = meta.quantize_price(max(entry_check - tick, 0.0))
                if quant_price <= 0 or quant_price >= entry_check:
                    raise ValueError("Escape Rope must be set below the entry reference for a long adventure.")
            if prep.direction == "short" and quant_price <= entry_check:
                quant_price = meta.quantize_price(entry_check + tick)
                if quant_price <= entry_check:
                    raise ValueError("Escape Rope must be set above the entry reference for a short adventure.")

        formatted_stop = self._format_price(prep.profile, prep.route, float(quant_price))
        adjustments.setdefault("price_scale", meta.price_scale)
        adjustments["rounded_stop"] = formatted_stop

        prep.payload["presetStopLossPrice"] = formatted_stop
        prep.payload["presetStopLossTriggerPrice"] = formatted_stop
        prep.payload["presetStopLossTriggerType"] = "mark_price"
        prep.payload["presetStopLossExecutePrice"] = formatted_stop

        return formatted_stop

    def _format_price(self, profile, route: str, price: float) -> str:
        if not profile:
            # Default to 2 decimal places if profile is missing
            return f"{price:.2f}"
        if route == "perp" and profile.perp_pip_precision is not None:
            precision = profile.perp_pip_precision
        else:
            precision = profile.pip_precision if profile.pip_precision is not None else 2
        return f"{price:.{precision}f}"

    def _format_size(self, route: str, profile, size: float) -> str:
        if not profile:
            # Default to 4 decimal places if profile is missing
            return f"{size:.4f}"
        if route == "perp" and profile.perp_size_precision is not None:
            precision = profile.perp_size_precision
        else:
            precision = profile.size_precision if profile.size_precision is not None else 4
        return f"{size:.{precision}f}"

    async def build_order_preview(self, data: Dict[str, Any]) -> Dict[str, Any]:
        action_raw = str(data.get("action", "throw")).lower()
        action = BattleAction.CATCH if action_raw in {"throw", "catch", "buy"} else BattleAction.RELEASE
        order_type_raw = str(data.get("orderType", "limit")).lower()
        try:
            order_style = OrderStyle(order_type_raw)
        except ValueError:
            order_style = OrderStyle.LIMIT

        rope = data.get("rope") or {}
        rope_value = rope.get("value", data.get("stopLoss") or data.get("stop_loss") or data.get("stop"))
        stop_loss = float(rope_value) if rope_value is not None else None
        mode_raw = str(rope.get("mode", "anchor")).lower()
        if stop_loss is not None:
            if mode_raw in {"percent", "distance"}:
                stop_mode = StopLossMode.PERCENT
            else:
                stop_mode = StopLossMode.PRICE
        else:
            stop_mode = None
        sensor_raw = str(rope.get("sensor", "mark")).lower()
        trigger = TriggerSource.MARK if sensor_raw.startswith("mark") else TriggerSource.LAST

        encounter_payload = {
            "species": data.get("species"),
            "action": action,
            "order_style": order_style,
            "pokeball_strength": float(data.get("size", 0.0) or 0.0),
            "limit_price": float(data.get("price")) if order_style == OrderStyle.LIMIT and data.get("price") is not None else None,
            "stop_loss": stop_loss,
            "stop_loss_mode": stop_mode,
            "stop_loss_trigger": trigger,
            "level": int(data.get("level") or data.get("lv") or max(2, int(data.get("leverage", 20)))),
        }

        order = EncounterOrder(**encounter_payload)
        adjustments: Dict[str, Any] = {}
        order = await self._prepare_order(order)
        prep = self._translator.to_exchange_payload(order)
        meta_symbol = prep.profile.perp_symbol if (prep.route == "perp" and prep.profile) else None
        meta = await self._get_contract_meta(meta_symbol)
        order = await self._apply_contract_meta(order, prep, adjustments, meta_override=meta)
        reasons: List[str] = []

        position_mode = None
        if prep.route == "perp":
            position_mode = await self._resolve_position_mode()
        self._apply_position_mode(prep, position_mode, order)

        embed_allowed = (
            prep.route == "perp"
            and self._settings.adventure_embed_sl
            and order.stop_loss_mode is not None
            and order.stop_loss_value is not None
        )
        if prep.route != "perp":
            reasons.append("not_perp")
        if not self._settings.adventure_embed_sl:
            reasons.append("flag_disabled")
        if order.stop_loss_mode is None:
            reasons.append("missing_stop_loss")

        embed_sl = False
        if embed_allowed:
            await self._embed_stop_loss(order, prep, adjustments, demo_mode=False)
            embed_sl = True

        if prep.route == "perp":
            await self._clamp_leverage(prep, requested_level=order.level, demo_mode=False)

        payload_snapshot = dict(prep.payload)
        return {
            "embedSL": embed_sl,
            "positionMode": prep.position_mode or position_mode or self._position_mode,
            "payload": payload_snapshot,
            "reasons": [] if embed_sl else reasons,
        }

    def _compute_distance_stop_from_price(
        self,
        prep: ExchangePreparation,
        distance_value: float,
        base_price: float,
    ) -> float:
        try:
            base = float(base_price)
        except (TypeError, ValueError):
            return 0.0
        ratio = float(distance_value) / 100.0
        if prep.direction in {"long", "spot_long"}:
            target = base * (1 - ratio)
        else:
            target = base * (1 + ratio)
        return max(0.0, target)

    async def _fetch_sensor_price(
        self,
        prep: ExchangePreparation,
        trigger_source: TriggerSource,
        *,
        demo_mode: bool,
    ) -> float | None:
        try:
            if prep.route == "spot":
                # Hyperliquid only supports perpetual markets, no spot trading
                raise ValueError("Spot tickers are not supported on Hyperliquid. Only perpetual markets are available.")
            else:
                ticker = await self._client.list_perp_tickers()
                data = self._payload_entries(ticker)
                symbol = prep.profile.perp_symbol
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if entry.get("symbol") == symbol:
                    return self._extract_ticker_price(entry, trigger_source)
        except Exception:
            if not demo_mode:
                raise
        return None

    @staticmethod
    def _extract_ticker_price(entry: Dict[str, Any], trigger_source: TriggerSource) -> float | None:
        keys = ["markPrice", "close", "last", "price"] if trigger_source == TriggerSource.MARK else ["last", "close", "price", "markPrice"]
        for key in keys:
            value = entry.get(key)
            if value is None:
                continue
            try:
                price = float(value)
                if price > 0:
                    return price
            except (TypeError, ValueError):
                continue
        return None

    def _schedule_escape_rope_adjustment(self, pending: PendingEscapeRope) -> None:
        if pending.order.stop_loss_mode != StopLossMode.PERCENT:
            return
        existing = self._pending_escape_tasks.pop(pending.client_oid, None)
        if existing:
            existing.cancel()
            # Wait for cancellation to complete to avoid race condition
            try:
                asyncio.create_task(self._wait_for_task_cancellation(existing))
            except Exception:
                pass  # Task may already be done
        self._pending_escape_meta[pending.client_oid] = pending
        task = asyncio.create_task(self._adjust_escape_rope(pending))
        self._pending_escape_tasks[pending.client_oid] = task

    async def _wait_for_task_cancellation(self, task: asyncio.Task) -> None:
        """Wait for a task to finish cancellation."""
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass  # Expected - task was cancelled or timed out

    async def _adjust_escape_rope(self, pending: PendingEscapeRope) -> None:
        client_oid = pending.client_oid
        try:
            for attempt in range(12):
                pending.attempts = attempt + 1
                entry_price = await self._fetch_average_entry_price(pending, pending.demo_mode)
                if entry_price is not None:
                    try:
                        final_stop = self._derive_stop_loss_price(pending.order, pending.prep, entry_price)
                    except ValueError as exc:
                        self._append_log(
                            message=str(exc),
                            badge=None,
                            payload={"clientOid": client_oid, "stage": "escape-fine-tune"},
                        )
                        return
                    if pending.sensor_price is not None:
                        delta = abs(final_stop - pending.sensor_price)
                        # Check if profile exists before accessing pip_precision
                        if pending.prep.profile is None:
                            logger.warning("Profile is None, skipping tick check")
                        else:
                            precision = pending.prep.profile.pip_precision
                            min_tick = 10 ** -precision if precision >= 0 else 0.0
                            if delta < min_tick:
                                self._append_log(
                                    message="Escape Rope set using sensor anchor.",
                                    badge=None,
                                    payload={"clientOid": client_oid, "stage": "escape-fine-tune"},
                                )
                                return
                    await self._replace_escape_rope(pending, final_stop)
                    self._append_log(
                        message="Escape Rope fine-tuned to your Distance (%) anchor.",
                        badge=None,
                        payload={"clientOid": client_oid, "stage": "escape-fine-tune", "final": final_stop},
                    )
                    return
                await asyncio.sleep(0.5)

            await self._cancel_escape_rope(pending)
            self._append_log(
                message="Escape Rope cancelled after entry stalled.",
                badge=None,
                payload={"clientOid": client_oid, "stage": "escape-timeout"},
            )
        except asyncio.CancelledError:
            pass
        finally:
            self._pending_escape_tasks.pop(client_oid, None)
            self._pending_escape_meta.pop(client_oid, None)

    async def _fetch_average_entry_price(
        self,
        pending: PendingEscapeRope,
        demo_mode: bool,
    ) -> float | None:
        symbol = (
            pending.prep.profile.spot_symbol
            if pending.prep.route == "spot"
            else pending.prep.profile.perp_symbol
        )
        try:
            if pending.prep.route == "spot":
                # Hyperliquid only supports perpetual markets, no spot trading
                raise ValueError("Spot fills are not supported on Hyperliquid. Only perpetual markets are available.")
            else:
                fills = await self._client.list_perp_fills(symbol, demo_mode=pending.demo_mode)
        except Exception:
            if not demo_mode:
                return None
            return pending.sensor_price

        data = self._payload_entries(fills)
        total_notional = 0.0
        total_size = 0.0
        for fill in data:
            if not isinstance(fill, dict):
                continue
            if fill.get("orderId") != pending.adventure_id and fill.get("clientOid") != pending.client_oid:
                continue
            try:
                price = float(fill.get("fillPrice") or fill.get("price"))
                size = float(fill.get("fillQuantity") or fill.get("size"))
            except (TypeError, ValueError):
                continue
            total_notional += price * size
            total_size += size
        if total_size <= 0:
            return None
        return total_notional / total_size

    async def _replace_escape_rope(self, pending: PendingEscapeRope, new_price: float) -> None:
        await self._cancel_escape_rope(pending)
        adjustments: Dict[str, Any] = {}
        new_reference = await self._attach_stop_loss(
            pending.order,
            pending.prep,
            stop_price=new_price,
            demo_mode=pending.demo_mode,
            adjustments=adjustments,
        )
        pending.stop_reference = new_reference

    async def _cancel_escape_rope(self, pending: PendingEscapeRope) -> None:
        if not pending.stop_reference:
            return
        # Embedded stop-losses can't be cancelled separately (they're part of main order)
        if pending.embedded:
            logger.debug("Skipping cancel for embedded stop-loss")
            return
        try:
            if pending.prep.route == "spot":
                # Hyperliquid only supports perpetual markets, no spot trading
                raise ValueError("Spot plan orders are not supported on Hyperliquid. Only perpetual markets are available.")
            else:
                payload = {
                    "symbol": pending.prep.profile.perp_symbol,
                    "marginCoin": "USDT",
                    "planId": pending.stop_reference,
                    "planType": "sl",
                }
                if pending.prep.hold_side:
                    payload["holdSide"] = pending.prep.hold_side
                await self._client.cancel_perp_stop_loss(payload, demo_mode=pending.demo_mode)
        except Exception:
            pass

    async def _cancel_pending_escape_ropes(self, species: str, demo_mode: bool) -> None:
        to_cancel = [key for key, meta in self._pending_escape_meta.items() if meta.order.species == species]
        for key in to_cancel:
            task = self._pending_escape_tasks.pop(key, None)
            if task:
                task.cancel()
            meta = self._pending_escape_meta.pop(key, None)
            if meta:
                await self._cancel_escape_rope(meta)

    async def _execute_runaway(self, order: EncounterOrder, *, is_demo: bool) -> AdventureOrderReceipt:
        profile = self._translator.species_to_profile(order.species)
        self._last_demo_mode = is_demo

        await self._cancel_pending_escape_ropes(order.species, is_demo)

        # Close perpetual positions first
        if profile.perp_symbol:
            position_mode = await self._refresh_position_mode()
            hold_side = None
            symbol = profile.perp_symbol
            if position_mode == "hedge":
                hold_side = self._last_perp_sides.get(symbol)
            try:
                flash_closed = await self._flash_close_perp(
                    symbol,
                    position_mode,
                    hold_side,
                    is_demo,
                )
            except ValueError:
                raise
            if not flash_closed:
                fallback_payload: Dict[str, Any] = {
                    "symbol": symbol,
                    "marginCoin": "USDT",
                    "productType": "USDT-FUTURES",
                }
                if hold_side:
                    fallback_payload["holdSide"] = hold_side
                try:
                    await self._client.close_perp_positions(
                        fallback_payload,
                        demo_mode=is_demo,
                    )
                    flash_closed = True
                except Exception:
                    flash_closed = False
            else:
                flash_closed = True

            if flash_closed and profile.perp_symbol:
                self._last_perp_sides.pop(profile.perp_symbol, None)

            try:
                await self._client.cancel_perp_plan_order(
                    {"symbol": profile.perp_symbol, "marginCoin": "USDT"},
                    demo_mode=is_demo,
                )
            except Exception:
                pass

        # Hyperliquid only supports perpetuals, no spot trading to cancel

        # Close any remaining perpetual positions for the species
        party_snapshot = await self.list_party_status(demo_mode=is_demo)
        # Note: Hyperliquid doesn't have spot markets, so skip spot balance closing

        # Refresh party snapshot post-closure to update guardrails
        await self.list_party_status(demo_mode=is_demo)

        message = f"Trainer ran safely—closed all spot and perp trails for {order.species}."
        badge = self._badge_for_action(BattleAction.RUN)
        self._append_log(
            message=message,
            badge=badge,
            payload={"species": order.species, "demo": is_demo},
        )
        # BUG FIX #8: Replace deprecated datetime.utcnow() with datetime.now(timezone.utc)
        self._last_encounter_at = datetime.now(timezone.utc)

        return AdventureOrderReceipt(
            adventure_id=str(uuid.uuid4()),
            species=order.species,
            action=BattleAction.RUN,
            filled=True,
            fill_price=None,
            fill_size=None,
            level_used=order.level,
            leverage_applied=None,
            demo_mode=is_demo,
            badge=badge,
            narration=message,
            raw_response={"message": message},
        )

    def _estimate_spot_balance(self, species: str, party_snapshot: Dict[str, object]) -> float:
        party = party_snapshot.get("party", []) if isinstance(party_snapshot, dict) else []
        for member in party:
            if not isinstance(member, dict):
                continue
            if member.get("species") == species:
                try:
                    return float(member.get("hp", 0.0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _badge_for_action(self, action: BattleAction) -> str:
        badge_map = {
            BattleAction.CATCH: "Capture Combo",
            BattleAction.RELEASE: "Tidy Trainer",
            BattleAction.HEAL: "Careful Tactician",
            BattleAction.RUN: "Safe Scout",
        }
        return badge_map.get(action, "Adventurer")

    def _update_guardrails(self, party_size: int) -> None:
        now = datetime.now(timezone.utc)
        cooldown_remaining = 0.0
        if self._last_encounter_at:
            elapsed = (now - self._last_encounter_at).total_seconds()
            cooldown_remaining = max(0.0, self._cooldown_seconds - elapsed)
        self._last_guardrails = GuardrailStatus(
            cooldown_seconds=self._cooldown_seconds,
            cooldown_remaining=cooldown_remaining,
            max_party_size=self._max_party_size,
            minimum_energy=0.0 if self._last_demo_mode else self._minimum_energy_base,
        )

    async def _check_cooldown(self) -> None:
        if not self._last_encounter_at:
            return
        elapsed = (datetime.now(timezone.utc) - self._last_encounter_at).total_seconds()
        if elapsed < self._cooldown_seconds:
            remaining = int(self._cooldown_seconds - elapsed)
            raise ValueError(f"Professor Elm says to rest for {remaining} more seconds.")

    def _enforce_party_limit(self, order: EncounterOrder, party: List[Dict[str, object]]) -> None:
        if order.action != BattleAction.CATCH:
            return
        if self._max_party_size and self._max_party_size > 0 and len(party) >= self._max_party_size:
            raise ValueError("Party is full! Release a partner before another throw.")

    def _enforce_energy_guard(self, order: EncounterOrder, energy_amount: float, *, is_demo: bool) -> None:
        if order.action != BattleAction.CATCH or is_demo:
            return
        if not self._last_energy_present:
            return
        if energy_amount < self._minimum_energy_base:
            raise ValueError("Energy reserves are low. Refill before throwing another Poké Ball.")

    def _resolve_demo_flag(self, override: Optional[bool]) -> bool:
        return bool(self._demo_mode_default if override is None else override)

    async def _prepare_order(self, order: EncounterOrder) -> EncounterOrder:
        effective_level = self._resolve_effective_level(order)
        updates: Dict[str, object] = {"level": effective_level}

        if order.quote_hp is not None:
            profile = self._translator.species_to_profile(order.species)
            route = "perp" if effective_level >= 2 and profile.perp_symbol else "spot"
            qty = await self._compute_size_from_quote(order, profile, route, effective_level)
            updates["pokeball_strength"] = qty

        if hasattr(order, "model_copy"):
            return order.model_copy(update=updates)
        return order.copy(update=updates)  # type: ignore[attr-defined]

    @staticmethod
    def _resolve_effective_level(order: EncounterOrder) -> int:
        source_level = order.lv if order.lv is not None else order.level
        try:
            resolved = int(source_level)
        except (TypeError, ValueError):
            resolved = order.level
        return max(1, resolved)

    async def _compute_size_from_quote(
        self,
        order: EncounterOrder,
        profile: SpeciesProfile,
        route: str,
        effective_level: int,
    ) -> float:
        quote_hp = float(order.quote_hp or 0.0)
        if quote_hp <= 0:
            raise ValueError("Encounter HP sizing requires a positive quote amount.")

        notional = quote_hp * float(max(1, effective_level))
        mark = await self._resolve_mark_price(profile, route)
        if mark <= 0:
            raise ValueError("Professor Elm: market sensors are offline. Try again shortly.")

        min_qty = self._minimum_quantity(profile, route)
        min_notional = mark * min_qty
        if notional < min_notional:
            formatted = self._format_quote_amount(min_notional)
            raise ValueError(f"Ace needs at least {formatted} HP at this level.")

        raw_qty = notional / mark
        precision = self._derive_size_precision(profile, route)
        qty = self._round_down(raw_qty, precision)
        if qty < min_qty:
            formatted = self._format_quote_amount(min_notional)
            raise ValueError(f"Ace needs at least {formatted} HP at this level.")
        if qty <= 0:
            raise ValueError("Encounter size is below the minimum tier after rounding.")
        return qty

    async def _resolve_mark_price(self, profile: SpeciesProfile, route: str) -> float:
        base = profile.base_token.strip().upper()
        if not base:
            raise ValueError("Encounter species is missing market metadata.")

        quote = self._price_feed.get_price(base)
        if quote and quote.price > 0:
            return float(quote.price)

        fallback = await self._fetch_mark_from_exchange(base, route)
        if fallback is not None and fallback > 0:
            return float(fallback)

        raise ValueError("Professor Elm: price data not available right now.")

    async def _fetch_mark_from_exchange(self, base: str, route: str) -> Optional[float]:
        # Hyperliquid only supports perpetual markets, no spot trading
        # Always use perp tickers regardless of route
        fetchers = [
            (self._client.list_perp_tickers, "markPrice"),
        ]

        for fetch, preferred_key in fetchers:
            try:
                payload = await fetch()
            except Exception:
                continue
            price = self._extract_price_from_payload(payload, base, preferred_key)
            if price is not None:
                return price
        return None

    @staticmethod
    def _extract_price_from_payload(payload: object, base: str, preferred_key: Optional[str]) -> Optional[float]:
        if not isinstance(payload, dict):
            return None
        data_list = payload.get("data_list")
        if not isinstance(data_list, list):
            data = payload.get("data")
            if isinstance(data, list):
                data_list = data
            else:
                return None
        base_key = base.upper()
        for entry in data_list:
            if not isinstance(entry, dict):
                continue
            symbol = entry.get("symbol")
            if not isinstance(symbol, str):
                continue
            entry_base = AdventureOrderService._base_from_symbol(symbol)
            if entry_base != base_key:
                continue
            price = AdventureOrderService._pick_price(entry, preferred_key)
            if price is not None:
                return price
        return None

    @staticmethod
    def _base_from_symbol(symbol: str) -> Optional[str]:
        upper = symbol.upper()
        if upper.endswith("USDT"):
            base = upper[:-4]
            return base or None
        return None

    @staticmethod
    def _pick_price(entry: Dict[str, object], preferred_key: Optional[str]) -> Optional[float]:
        keys: List[str] = []
        if preferred_key:
            keys.append(preferred_key)
        keys.extend(["markPrice", "last", "close", "price"])
        seen: set[str] = set()
        for key in keys:
            if not key or key in seen:
                continue
            seen.add(key)
            value = entry.get(key)
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
    def _derive_size_precision(profile: SpeciesProfile, route: str) -> Optional[int]:
        if route == "perp" and profile.perp_size_precision is not None:
            return profile.perp_size_precision
        return profile.size_precision

    @staticmethod
    def _round_down(value: float, precision: Optional[int]) -> float:
        decimals = 6 if precision is None else max(0, int(precision))
        factor = 10 ** decimals
        floored = math.floor(value * factor) / factor
        return float(f"{floored:.{decimals}f}")

    @staticmethod
    def _minimum_quantity(profile: SpeciesProfile, route: str) -> float:
        precision = (
            profile.perp_size_precision
            if route == "perp" and profile.perp_size_precision is not None
            else profile.size_precision
        )
        decimals = 6 if precision is None else max(0, int(precision))
        return 10 ** (-decimals)

    @staticmethod
    def _format_quote_amount(amount: float) -> str:
        value = float(amount)
        if value >= 1:
            text = f"{value:,.2f}"
        elif value >= 0.01:
            text = f"{value:,.4f}"
        else:
            text = f"{value:.6f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text

    def _handle_exchange_error(
        self,
        exc: Exception,
        *,
        context: str,
        adjustments: Optional[Dict[str, Any]] = None,
    ) -> None:
        logger.warning("Exchange error during %s: %s", context, exc)
        message = self._friendly_exchange_message(exc, adjustments=adjustments)
        raise ValueError(message) from None

    def _friendly_exchange_message(
        self,
        exc: Exception,
        *,
        adjustments: Optional[Dict[str, Any]] = None,
    ) -> str:
        if isinstance(exc, asyncio.TimeoutError):
            return "Prof. Oak is still waiting for a reply. Try again shortly."
        if isinstance(exc, httpx.HTTPStatusError):
            detail = self._parse_exchange_error(exc.response)
            if detail:
                detail = sanitize_vendor_terms(detail) or detail
                hint = self._format_scale_step_hint(detail, adjustments)
                if hint:
                    return hint
                return f"Prof. Oak relayed: {detail}"
            return "Prof. Oak can't reach the exchange right now. Try again in a moment."
        if isinstance(exc, httpx.RequestError):
            message = sanitize_vendor_terms(str(exc)) or ""
            hint = self._format_scale_step_hint(message, adjustments)
            if hint:
                return hint
            return "Prof. Oak can't reach the exchange right now. Try again in a moment."
        if isinstance(exc, RuntimeError):
            text = str(exc)
            if "credential" in text.lower():
                return (
                    "Prof. Oak: Pokégear is missing credentials. "
                    "Toggle demo mode or add keys before live adventures."
                )
        sanitized = sanitize_vendor_terms(str(exc)) or ""
        hint = self._format_scale_step_hint(sanitized, adjustments)
        if hint:
            return hint
        short_reason = sanitized or "Something went wrong."
        return f"Prof. Oak: couldn't throw that ball. {short_reason}"

    @staticmethod
    def _parse_exchange_error(response: httpx.Response) -> Optional[str]:
        try:
            payload = response.json()
        except ValueError:
            text = response.text.strip()
            return text if text else None
        if isinstance(payload, dict):
            for key in ("msg", "message", "error", "errMsg", "errorMsg", "detail"):
                value = payload.get(key)
                if isinstance(value, str):
                    trimmed = value.strip()
                    if trimmed:
                        sanitized = sanitize_vendor_terms(trimmed) or trimmed
                        return sanitized
        return None

    @staticmethod
    def _format_scale_step_hint(
        message: Optional[str],
        adjustments: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if not message:
            return None
        lower = message.lower()
        adjusted = adjustments or {}
        price_scale = adjusted.get("price_scale")
        rounded_price = adjusted.get("rounded_price") or adjusted.get("rounded_stop")
        rounded_qty = adjusted.get("rounded_qty")

        scale_keywords = [
            "checkscale",
            "checkbdscale",
            "price scale",
            "price precision",
            "checkprice",
        ]
        step_keywords = [
            "step",
            "size scale",
            "size precision",
            "quantity precision",
            "qty precision",
        ]

        if any(keyword in lower for keyword in scale_keywords) and rounded_price:
            decimals = price_scale if isinstance(price_scale, int) else DEFAULT_CONTRACT_META.price_scale
            return (
                f"Prof. Oak: your rope price needs at most {decimals} decimals. "
                f"I rounded it to {rounded_price}. Please confirm again."
            )

        if any(keyword in lower for keyword in step_keywords) and rounded_qty:
            return (
                f"Prof. Oak: quantity must follow the exchange step. "
                f"I adjusted it to {rounded_qty}."
            )

        return None

    def _normalize_open_order_entry(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(entry, dict):
            return None

        symbol_raw = entry.get("symbol")
        if not isinstance(symbol_raw, str) or not symbol_raw.strip():
            return None
        symbol = symbol_raw.upper().strip()

        size = self._to_float(
            entry.get("size")
            or entry.get("baseVolume")
            or entry.get("baseSize")
            or entry.get("total")
        )
        price = self._to_float(
            entry.get("price")
            or entry.get("triggerPrice")
            or entry.get("planPrice")
            or entry.get("px")
        )

        amount_for_descriptor = abs(size) if size is not None else 0.0
        try:
            descriptor = self._translator.describe_balance(
                symbol=symbol,
                amount=amount_for_descriptor,
            )
        except ValueError:
            return None

        side = self._normalize_order_side(entry)
        order_type_raw = entry.get("orderType") or entry.get("type")
        order_type = str(order_type_raw).lower() if order_type_raw else None

        timestamp = self._extract_order_timestamp(entry) or 0

        route = self._infer_order_route(entry)

        return {
            "species": descriptor.get("species", symbol),
            "symbol": symbol,
            "element": descriptor.get("element"),
            "sprite": descriptor.get("sprite"),
            "side": side,
            "orderType": order_type,
            "size": abs(size) if size is not None else None,
            "price": price,
            "route": route,
            "updatedTs": timestamp,
        }

    @staticmethod
    def _infer_order_route(entry: Dict[str, Any]) -> str:
        product_type = str(entry.get("productType") or "").strip().lower()
        if product_type:
            if "spot" in product_type:
                return "spot"
            if "future" in product_type or "mix" in product_type or "perp" in product_type:
                return "perp"

        margin_mode = entry.get("marginMode")
        has_perp_markers = any(
            key in entry
            for key in ("marginMode", "posSide", "holdSide", "leverage", "marginCoin")
        )
        if has_perp_markers:
            return "perp"

        return "spot"

    @staticmethod
    def _normalize_order_side(entry: Dict[str, Any]) -> Optional[str]:
        candidates = (
            entry.get("tradeSide"),
            entry.get("posSide"),
            entry.get("side"),
            entry.get("direction"),
        )
        for candidate in candidates:
            if candidate is None:
                continue
            text = str(candidate).strip().lower()
            if not text:
                continue
            if any(token in text for token in ("buy", "long", "open_long", "longside")):
                return "long"
            if any(token in text for token in ("sell", "short", "open_short", "shortside")):
                return "short"
        return None

    @staticmethod
    def _extract_order_timestamp(entry: Dict[str, Any]) -> Optional[int]:
        for key in ("uTime", "updateTime", "cTime", "createTime", "createdTime", "mtime"):
            value = entry.get(key)
            numeric = AdventureOrderService._to_float(value)
            if numeric is None:
                continue
            try:
                return int(numeric)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _payload_entries(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        if not isinstance(payload, dict):
            return entries
        data_list = payload.get("data_list")
        if isinstance(data_list, list):
            for item in data_list:
                if isinstance(item, dict):
                    entries.append(item)
        if entries:
            return entries
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    entries.append(item)
        elif isinstance(data, dict):
            entries.append(data)
        return entries

    @staticmethod
    def _first_payload_entry(payload: Dict[str, Any]) -> Dict[str, Any]:
        entries = AdventureOrderService._payload_entries(payload)
        return entries[0] if entries else {}

    @staticmethod
    def _extract_adventure_id(response: Dict[str, object]) -> str:
        entry = AdventureOrderService._first_payload_entry(response)
        for key in ("orderId", "clientOid", "clientOrderId"):
            value = entry.get(key)
            if isinstance(value, str):
                return value
        return str(uuid.uuid4())

    @staticmethod
    def _extract_filled_status(response: Dict[str, object]) -> bool:
        entry = AdventureOrderService._first_payload_entry(response)
        status = entry.get("status") if isinstance(entry, dict) else None
        if isinstance(status, str):
            return status.lower() in {"filled", "success", "full-fill"}
        return False

    @staticmethod
    def _extract_fill_price(response: Dict[str, object]) -> float | None:
        entry = AdventureOrderService._first_payload_entry(response)
        price = entry.get("price") if isinstance(entry, dict) else None
        if price is None:
            price = entry.get("fillPrice") if isinstance(entry, dict) else None
        if isinstance(price, (int, float)):
            return float(price)
        if isinstance(price, str) and price:
            try:
                return float(price)
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_fill_size(response: Dict[str, object]) -> float | None:
        entry = AdventureOrderService._first_payload_entry(response)
        size = entry.get("size") if isinstance(entry, dict) else None
        if size is None:
            size = entry.get("fillQuantity") if isinstance(entry, dict) else None
        if isinstance(size, (int, float)):
            return float(size)
        if isinstance(size, str) and size:
            try:
                return float(size)
            except ValueError:
                return None
        return None
