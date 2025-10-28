from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, TYPE_CHECKING

from ..schemas import BattleAction, EncounterOrder, OrderStyle

if TYPE_CHECKING:  # pragma: no cover - typing guard
    from .contract_meta import ContractMeta


@dataclass(frozen=True)
class SpeciesProfile:
    """Metadata describing how a Pokémon species maps to an exchange market."""

    display_name: str
    spot_symbol: str
    element: str = ""
    region: str = ""
    sprite: str = ""
    perp_symbol: Optional[str] = None
    pip_precision: int = 2
    size_precision: int = 4
    perp_pip_precision: Optional[int] = None
    perp_size_precision: Optional[int] = None
    max_leverage: int = 50
    hp_scale: float = 100.0

    @property
    def base_token(self) -> str:
        # Hyperliquid symbols are already base tokens (BTC, ETH, etc.)
        return self.spot_symbol


@dataclass
class ExchangePreparation:
    """Container holding the translated exchange order payload."""

    route: str  # "spot" | "perp"
    direction: str  # "spot_long" | "long" | "short"
    payload: Dict[str, object]
    profile: SpeciesProfile
    client_oid: str
    hold_side: Optional[str] = None
    position_mode: Optional[str] = None
    contract_meta: Optional["ContractMeta"] = None


class PokemonTranslator:
    """Translates themed species into concrete exchange payloads."""

    def __init__(
        self,
        profiles: Optional[Iterable[SpeciesProfile]] = None,
        *,
        margin_mode: str = "crossed",
    ) -> None:
        self._profiles: Dict[str, SpeciesProfile] = {}
        self._symbol_map: Dict[str, SpeciesProfile] = {}
        self._margin_mode = self._normalize_margin_mode(margin_mode)
        if profiles:
            self.replace_profiles(profiles)

    def replace_profiles(self, profiles: Iterable[SpeciesProfile]) -> None:
        profile_map: Dict[str, SpeciesProfile] = {}
        symbol_map: Dict[str, SpeciesProfile] = {}
        for profile in profiles:
            profile_map[profile.display_name] = profile
            symbol_map[profile.spot_symbol.upper()] = profile
            if profile.perp_symbol:
                symbol_map[profile.perp_symbol.upper()] = profile
        self._profiles = profile_map
        self._symbol_map = symbol_map

    def supported_species(self) -> Dict[str, Dict[str, object]]:
        listing: Dict[str, Dict[str, object]] = {}
        for name in sorted(self._profiles.keys()):
            profile = self._profiles[name]
            listing[name] = {
                "element": profile.element,
                "region": profile.region,
                "sprite": profile.sprite,
                "level_caps": {"spot": 1, "perp": profile.max_leverage},
            }
        return listing

    def species_to_profile(self, species: str) -> SpeciesProfile:
        profile = self._profiles.get(species)
        if not profile:
            raise ValueError(f"Unknown species: {species}")
        return profile

    def symbol_to_profile(self, symbol: str) -> Optional[SpeciesProfile]:
        if not isinstance(symbol, str) or not symbol:
            return None
        return self._symbol_map.get(symbol.upper())

    def to_exchange_payload(self, order: EncounterOrder) -> ExchangePreparation:
        profile = self.species_to_profile(order.species)

        # Force perp routing if stop-loss is present (for embedded stop-loss like test-order-with-sl)
        has_stop_loss = (
            order.stop_loss_mode is not None and
            order.stop_loss_value is not None and
            profile.perp_symbol
        )

        if has_stop_loss:
            route = "perp"  # Force perp for embedded stop-loss
        else:
            route = "perp" if order.level >= 2 and profile.perp_symbol else "spot"

        if route == "perp" and not profile.perp_symbol:
            route = "spot"

        direction = "spot_long"
        hold_side: Optional[str] = None
        if route == "perp":
            if order.action == BattleAction.RELEASE:
                direction = "short"
                hold_side = "short"
            else:
                direction = "long"
                hold_side = "long"
        elif order.action == BattleAction.RELEASE:
            direction = "spot_long"

        client_oid = str(uuid.uuid4())
        if route == "perp":
            payload: Dict[str, object] = {
                "symbol": profile.perp_symbol,
                "marginMode": self._margin_mode,
                "marginCoin": "USDT",
                "clientOid": client_oid,
                "productType": "USDT-FUTURES",
                "side": "buy" if direction == "long" else "sell",
                "orderType": order.order_style.value,
                "timeInForceValue": "normal",
                "size": self._format_size(profile, route, order.pokeball_strength),
                "posSide": "long" if direction == "long" else "short",
            }
            if order.order_style == OrderStyle.LIMIT and order.limit_price is not None:
                payload["price"] = self._format_price(profile, route, float(order.limit_price))
        else:
            payload = {
                "symbol": profile.spot_symbol,
                "clientOid": client_oid,
                "side": "buy" if order.action != BattleAction.RELEASE else "sell",
                "orderType": order.order_style.value,
                "force": "gtc",
                "size": self._format_size(profile, route, order.pokeball_strength),
            }
            if order.order_style == OrderStyle.LIMIT and order.limit_price is not None:
                payload["price"] = self._format_price(profile, route, float(order.limit_price))

        return ExchangePreparation(
            route=route,
            direction=direction,
            payload=payload,
            profile=profile,
            client_oid=client_oid,
            hold_side=hold_side,
            position_mode=None,
        )

    def resolve_species(self, token: str) -> SpeciesProfile:
        normalized = (token or "").strip()
        if not normalized:
            raise ValueError("Species lookup value is empty.")

        cleaned = normalized.lower().replace("-", "").replace(" ", "")

        for species_name, profile in self._profiles.items():
            canonical = species_name.lower().replace("-", "").replace(" ", "")
            if canonical == cleaned:
                return profile

        for symbol, profile in self._symbol_map.items():
            canonical = symbol.lower().replace("-", "")
            if canonical == cleaned:
                return profile

        raise ValueError(
            "Unknown species token '{}'. Try a roster species name, base token, or market symbol.".format(
                token
            )
        )

    @staticmethod
    def _normalize_margin_mode(value: str) -> str:
        normalized = str(value or "crossed").lower()
        if normalized not in {"crossed", "isolated"}:
            return "crossed"
        return normalized

    def describe_balance(self, *, symbol: str, amount: float) -> Dict[str, object]:
        profile = self._symbol_map.get(symbol.upper())
        if not profile:
            raise ValueError(f"Unsupported balance symbol: {symbol}")
        scale = profile.hp_scale if profile.hp_scale > 0 else 100.0
        hp = max(0.0, min(1.0, amount / scale))
        return {
            "species": profile.display_name,
            "hp": hp,
            "symbol": symbol,
            "element": profile.element,
            "sprite": profile.sprite,
            "amount": amount,
        }

    @staticmethod
    def _format_price(profile: SpeciesProfile, route: str, price: float) -> str:
        precision = (
            profile.perp_pip_precision
            if route == "perp" and profile.perp_pip_precision is not None
            else profile.pip_precision
        )
        precision = max(0, precision)
        return f"{price:.{precision}f}"

    @staticmethod
    def _format_size(profile: SpeciesProfile, route: str, size: float) -> str:
        precision = (
            profile.perp_size_precision
            if route == "perp" and profile.perp_size_precision is not None
            else profile.size_precision
        )
        precision = max(0, precision)
        return f"{size:.{precision}f}"


def default_translator(margin_mode: str = "crossed") -> PokemonTranslator:
    """Build a translator seeded with the default Johto roster."""

    # Hyperliquid uses native symbols (BTC, ETH) without "USDT" suffix
    defaults: List[SpeciesProfile] = [
        SpeciesProfile(
            display_name="Dragonite",
            spot_symbol="BTC",
            perp_symbol="BTC",
            element="Dragon",
            sprite="dragonite",
            max_leverage=50,
            pip_precision=1,
            size_precision=4,
            perp_pip_precision=1,
            perp_size_precision=3,
            hp_scale=100.0,
        ),
        SpeciesProfile(
            display_name="Lapras",
            spot_symbol="ETH",
            perp_symbol="ETH",
            element="Water",
            sprite="lapras",
            max_leverage=50,
            pip_precision=2,
            size_precision=4,
            perp_pip_precision=2,
            perp_size_precision=3,
            hp_scale=75.0,
        ),
        SpeciesProfile(
            display_name="Typhlosion",
            spot_symbol="SOL",
            perp_symbol="SOL",
            element="Fire",
            sprite="typhlosion",
            max_leverage=50,
            pip_precision=2,
            size_precision=3,
            perp_pip_precision=2,
            perp_size_precision=3,
            hp_scale=50.0,
        ),
        SpeciesProfile(
            display_name="Ampharos",
            spot_symbol="XRP",
            perp_symbol="XRP",
            element="Electric",
            sprite="ampharos",
            max_leverage=50,
            pip_precision=4,
            size_precision=2,
            perp_pip_precision=4,
            perp_size_precision=2,
            hp_scale=50.0,
        ),
        SpeciesProfile(
            display_name="Umbreon",
            spot_symbol="DOGE",
            perp_symbol="DOGE",
            element="Dark",
            sprite="umbreon",
            max_leverage=50,
            pip_precision=5,
            size_precision=2,
            perp_pip_precision=5,
            perp_size_precision=2,
            hp_scale=20.0,
        ),
        SpeciesProfile(
            display_name="Gengar",
            spot_symbol="HYPE",
            perp_symbol="HYPE",
            element="Ghost",
            sprite="gengar",
            max_leverage=50,
            pip_precision=3,
            size_precision=3,
            perp_pip_precision=3,
            perp_size_precision=3,
            hp_scale=20.0,
        ),
        SpeciesProfile(
            display_name="Espeon",
            spot_symbol="AVAX",
            perp_symbol="AVAX",
            element="Psychic",
            sprite="espeon",
            max_leverage=50,
            pip_precision=2,
            size_precision=3,
            perp_pip_precision=2,
            perp_size_precision=3,
            hp_scale=40.0,
        ),
        SpeciesProfile(
            display_name="Scizor",
            spot_symbol="SUI",
            perp_symbol="SUI",
            element="Steel",
            sprite="scizor",
            max_leverage=50,
            pip_precision=4,
            size_precision=3,
            perp_pip_precision=4,
            perp_size_precision=3,
            hp_scale=40.0,
        ),
        SpeciesProfile(
            display_name="Snorlax",
            spot_symbol="BNB",
            perp_symbol="BNB",
            element="Normal",
            sprite="snorlax",
            max_leverage=50,
            pip_precision=2,
            size_precision=4,
            perp_pip_precision=2,
            perp_size_precision=3,
            hp_scale=60.0,
        ),
        SpeciesProfile(
            display_name="Heracross",
            spot_symbol="WLD",
            perp_symbol="WLD",
            element="Bug",
            sprite="heracross",
            max_leverage=50,
            pip_precision=3,
            size_precision=2,
            perp_pip_precision=3,
            perp_size_precision=2,
            hp_scale=10.0,
        ),
    ]

    translator = PokemonTranslator(defaults, margin_mode=margin_mode)
    return translator
