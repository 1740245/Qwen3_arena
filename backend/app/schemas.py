from __future__ import annotations

from datetime import datetime
from enum import Enum
import math
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BattleAction(str, Enum):
    CATCH = "throw_pokeball"  # buy / open long
    RELEASE = "release_pokemon"  # sell / reduce long
    HEAL = "use_potion"  # reserve for future hedging features
    RUN = "run_away"  # skip encounter with a friendly log entry


class OrderStyle(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class StopLossMode(str, Enum):
    PRICE = "price"
    PERCENT = "percent"


class TriggerSource(str, Enum):
    MARK = "mark_price"
    LAST = "last_price"


class AdventureBaseModel(BaseModel):
    pass


class EncounterOrder(AdventureBaseModel):
    ANCHOR_INVALID_MESSAGE: ClassVar[str] = "That anchor isn’t a valid number. Try plain digits like 104409.94."

    species: str = Field(..., description="Pokemon display name representing a market symbol.")
    action: BattleAction = Field(
        BattleAction.CATCH,
        description="Adventure action to perform (maps to exchange side).",
    )
    order_style: OrderStyle = Field(
        OrderStyle.MARKET,
        description="Order execution style available to the trainer.",
    )
    STRENGTH_REQUIRED_MESSAGE: ClassVar[str] = (
        "Professor Elm: choose a Poké Ball strength before confirming."
    )

    pokeball_strength: float = Field(
        0.0, ge=0.0, description="Order size abstracted as Pokeball strength.")
    quote_hp: Optional[float] = Field(
        None,
        ge=0.0,
        description="Optional quote HP amount used to derive Pokeball strength.",
    )
    limit_price: Optional[float] = Field(
        None,
        ge=0.0,
        description="Optional target price when using limit orders (in base currency).",
    )
    level: int = Field(
        1,
        ge=1,
        description="Adventure level: 1 for spot encounters, 2+ for USDT-M adventures.",
    )
    lv: Optional[int] = Field(
        None,
        ge=1,
        description="Optional shorthand LV field supplied by clients for leverage controls.",
    )
    demo_mode: bool = Field(
        False,
        description="When true, the encounter is routed to demo endpoints or simulated.",
    )
    size_preset: Optional[str] = Field(
        None,
        description="Optional size preset identifier chosen by the UI.",
    )
    stop_loss: Optional[float] = Field(
        None,
        description="Legacy stop-loss anchor value supplied directly by the UI.",
    )
    stop_loss_mode: Optional[StopLossMode] = Field(
        None,
        description="Stop-loss declaration mode (price or percent).",
    )
    stop_loss_value: Optional[float] = Field(
        None,
        ge=0.0,
        description="Stop-loss numeric value (price or percent depending on mode).",
    )
    stop_loss_trigger: TriggerSource = Field(
        TriggerSource.MARK,
        description="Trigger source for stop-loss automation.",
    )
    client_adventure_id: Optional[str] = Field(
        None,
        description="UI-provided identifier for deduplication and logging.",
    )

    @model_validator(mode="after")
    def _validate_strength_for_action(cls, order: "EncounterOrder") -> "EncounterOrder":
        if order.action != BattleAction.RUN and order.pokeball_strength <= 0:
            raise ValueError(cls.STRENGTH_REQUIRED_MESSAGE)
        return order

    @model_validator(mode="after")
    def _propagate_stop_loss(cls, order: "EncounterOrder") -> "EncounterOrder":
        updates: Dict[str, Any] = {}
        if order.stop_loss is not None and order.stop_loss_value is None:
            updates["stop_loss_value"] = order.stop_loss
            if order.stop_loss_mode is None:
                updates["stop_loss_mode"] = StopLossMode.PRICE
        if order.stop_loss is None and order.stop_loss_value is not None:
            updates["stop_loss"] = order.stop_loss_value
        if updates:
            return order.model_copy(update=updates)
        return order

    @field_validator("limit_price", "stop_loss_value", "stop_loss", mode="before")
    @classmethod
    def _sanitize_price(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            numeric = float(value)
        elif isinstance(value, str):
            cleaned = value.replace(",", "").strip()
            if not cleaned:
                return None
            try:
                numeric = float(cleaned)
            except ValueError as exc:
                raise ValueError(cls.ANCHOR_INVALID_MESSAGE) from exc
        else:
            raise ValueError(cls.ANCHOR_INVALID_MESSAGE)

        if not math.isfinite(numeric) or numeric <= 0:
            raise ValueError(cls.ANCHOR_INVALID_MESSAGE)
        return numeric


class EnergyStatus(AdventureBaseModel):
    """Encapsulates the trainer's Energy bar details."""

    present: bool = Field(
        False,
        description="Indicates whether live Energy data is currently available.",
    )
    fill: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="Normalized Energy fill from 0.0 to 1.0.",
    )
    source: str = Field(
        "none",
        description="Origin of the Energy reading: none, perp, spot, or both.",
    )
    unit: str = Field(
        "USDT",
        description="Display unit for the Energy caption.",
    )
    value: Optional[float] = Field(
        None,
        description="Optional numeric value for the energy caption when available.",
    )


class GuardrailStatus(AdventureBaseModel):
    cooldown_seconds: int = Field(
        0,
        description="Cooldown duration enforced between encounters.",
    )
    cooldown_remaining: float = Field(
        0.0,
        description="Seconds remaining on the active cooldown (0 when ready).",
    )
    max_party_size: int = Field(
        6,
        description="Maximum simultaneous open encounters allowed.",
    )
    minimum_energy: float = Field(
        0.0,
        description="Minimum Energy reserve required for new captures.",
    )


class SpeciesRosterSlot(AdventureBaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slot: int
    status: str = Field(..., description="occupied, mystery, or empty")
    species: Optional[str] = None
    sprite: Optional[str] = None
    element: Optional[str] = None
    region: Optional[str] = None
    base_token: Optional[str] = None
    spot_symbol: Optional[str] = Field(
        default=None,
        alias="spotSymbol",
        serialization_alias="spotSymbol",
        description="Internal spot market symbol (e.g., BTCUSDT).",
    )
    perp_symbol: Optional[str] = Field(
        default=None,
        alias="perpSymbol",
        serialization_alias="perpSymbol",
        description="Internal perp market symbol (e.g., BTCUSDT).",
    )
    price_usd: Optional[float] = None
    price_source: Optional[str] = None
    weight_kg: Optional[float] = Field(
        default=None,
        alias="weightKg",
        serialization_alias="weightKg",
        description="Latest converted weight derived from USD price.",
    )
    level_caps: Optional[Dict[str, int]] = Field(
        default=None,
        description="Advertised level caps per mode (e.g., {'spot': 1, 'perp': 50}).",
    )


class RosterResponse(AdventureBaseModel):
    roster: List[SpeciesRosterSlot] = Field(default_factory=list)
    last_updated: datetime = Field(default_factory=datetime.utcnow)


class TrainerStatus(AdventureBaseModel):
    trainer_name: str
    badges: List[str] = Field(default_factory=list)
    party: List[Dict[str, Any]] = Field(default_factory=list)
    pokedollar_balance: float = 0.0
    last_sync: datetime = Field(default_factory=datetime.utcnow)
    energy: EnergyStatus = Field(default_factory=EnergyStatus)
    guardrails: GuardrailStatus = Field(default_factory=GuardrailStatus)
    demo_mode: bool = False
    position_mode: Optional[str] = Field(
        default=None,
        alias="positionMode",
        serialization_alias="positionMode",
    )
    link_shell: Optional[str] = Field(
        default=None,
        alias="linkShell",
        serialization_alias="linkShell",
        description="Indicates whether the Bitget link-shell is online or offline.",
    )


class AdventureOrderReceipt(AdventureBaseModel):
    adventure_id: str
    species: str
    action: BattleAction
    filled: bool
    fill_price: Optional[float]
    fill_size: Optional[float]
    pokedollar_delta: Optional[float] = None
    level_used: int = 1
    leverage_applied: Optional[int] = None
    demo_mode: bool = False
    badge: Optional[str] = None
    narration: Optional[str] = None
    stop_loss_reference: Optional[str] = None
    raw_response: Dict[str, Any] = Field(default_factory=dict)
    normalized_price: Optional[str] = Field(
        default=None,
        alias="normalizedPrice",
        serialization_alias="normalizedPrice",
    )
    normalized_trigger_price: Optional[str] = Field(
        default=None,
        alias="normalizedTriggerPrice",
        serialization_alias="normalizedTriggerPrice",
    )
    price_scale: Optional[int] = Field(
        default=None,
        alias="priceScale",
        serialization_alias="priceScale",
    )
    price_tick_formatted: Optional[str] = Field(
        default=None,
        alias="priceTickFormatted",
        serialization_alias="priceTickFormatted",
    )


class AdventureLogEntry(AdventureBaseModel):
    event_id: str
    timestamp: datetime
    message: str
    badge: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class PriceQuoteResponse(AdventureBaseModel):
    base: str
    price: Optional[float]
    source: Optional[str]
    updated_at: Optional[datetime]
    weight_kg: Optional[float]
