from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional

from ..adapters.bitget_client import BitgetClient
from ..schemas import RosterResponse, SpeciesRosterSlot
from .price_feed import PriceFeed
from .translators import PokemonTranslator, SpeciesProfile


@dataclass(frozen=True)
class PokemonSkin:
    name: str
    element: str
    sprite: str


PINNED_SKINS: List[PokemonSkin] = [
    PokemonSkin("Dragonite", "Dragon", "dragonite"),
    PokemonSkin("Lapras", "Water", "lapras"),
    PokemonSkin("Typhlosion", "Fire", "typhlosion"),
    PokemonSkin("Ampharos", "Electric", "ampharos"),
    PokemonSkin("Umbreon", "Dark", "umbreon"),
    PokemonSkin("Gengar", "Ghost", "gengar"),
    PokemonSkin("Espeon", "Psychic", "espeon"),
    PokemonSkin("Scizor", "Steel", "scizor"),
    PokemonSkin("Snorlax", "Normal", "snorlax"),
    PokemonSkin("Heracross", "Bug", "heracross"),
]


class PokemonRosterService:
    """Builds and caches the 15-slot roster, keeping the translator in sync."""

    def __init__(
        self,
        client: BitgetClient,
        translator: PokemonTranslator,
        price_feed: PriceFeed,
        pinned_bases: Iterable[str],
        *,
        roster_size: int = 15,
        mystery_slots: int = 5,
    ) -> None:
        self._client = client
        self._translator = translator
        self._price_feed = price_feed
        self._pinned_bases = [base.upper() for base in pinned_bases]
        self._mystery_slots = mystery_slots
        self._roster_size = len(self._pinned_bases) + self._mystery_slots
        self._lock = asyncio.Lock()
        self._cached_response: Optional[RosterResponse] = None
        self._species_map: Dict[str, Dict[str, str]] = {}
        self._base_map: Dict[str, str] = {}
        self._symbol_map: Dict[str, str] = {}
        self._last_refresh_ts: Optional[datetime] = None

    async def current_roster(self) -> RosterResponse:
        if self._cached_response is None:
            await self.refresh(force=True)
        assert self._cached_response is not None
        return self._cached_response

    async def refresh(self, *, force: bool = False) -> RosterResponse:
        async with self._lock:
            if self._cached_response is not None and not force:
                return self._cached_response
            profiles = self._build_profiles()
            self._translator.replace_profiles(profiles)
            roster = self._as_roster_response(profiles)
            self._rebuild_maps(roster)
            self._cached_response = roster
            return roster

    def _build_profiles(self) -> List[SpeciesProfile]:
        profiles: List[SpeciesProfile] = []
        for idx, base in enumerate(self._pinned_bases):
            skin = PINNED_SKINS[idx % len(PINNED_SKINS)]
            symbol = f"{base}USDT"

            # Check if we have an existing profile for this species to preserve precision values
            existing_profile = self._translator._profiles.get(skin.name)

            profiles.append(
                SpeciesProfile(
                    display_name=skin.name,
                    spot_symbol=symbol,
                    element=skin.element,
                    region="",
                    sprite=skin.sprite,
                    perp_symbol=symbol,
                    max_leverage=125,
                    # Preserve precision values from existing profile if available
                    pip_precision=existing_profile.pip_precision if existing_profile else 2,
                    size_precision=existing_profile.size_precision if existing_profile else 4,
                    perp_pip_precision=existing_profile.perp_pip_precision if existing_profile else None,
                    perp_size_precision=existing_profile.perp_size_precision if existing_profile else None,
                    hp_scale=existing_profile.hp_scale if existing_profile else 100.0,
                )
            )
        return profiles

    def _as_roster_response(self, profiles: Iterable[SpeciesProfile]) -> RosterResponse:
        slots: List[SpeciesRosterSlot] = []
        for index, profile in enumerate(profiles, start=1):
            base_token = profile.spot_symbol[:-4].upper()
            quote = self._price_feed.get_price(base_token)
            slots.append(
                SpeciesRosterSlot(
                    slot=index,
                    status="occupied",
                    species=profile.display_name,
                    sprite=profile.sprite,
                    element=profile.element,
                    base_token=base_token,
                    perp_symbol=profile.perp_symbol,
                    spot_symbol=profile.spot_symbol,
                    price_usd=quote.price if quote else None,
                    price_source=quote.source if quote else None,
                    weight_kg=quote.weight_kg if quote else None,
                    level_caps={"spot": 1, "perp": profile.max_leverage},
                )
            )
        for slot_index in range(len(slots) + 1, self._roster_size + 1):
            slots.append(
                SpeciesRosterSlot(
                    slot=slot_index,
                    status="mystery",
                    species="Mystery Egg",
                )
            )
        return RosterResponse(roster=slots, last_updated=datetime.utcnow())

    def _rebuild_maps(self, roster: RosterResponse) -> None:
        species_map: Dict[str, Dict[str, str]] = {}
        base_map: Dict[str, str] = {}
        symbol_map: Dict[str, str] = {}
        for slot in roster.roster:
            if slot.status != "occupied" or not slot.species or not slot.base_token or not slot.spot_symbol:
                continue
            species_name = slot.species
            base = slot.base_token.upper()
            symbol = slot.spot_symbol.upper()
            species_map[species_name] = {"base": base, "symbol": symbol}
            base_map[base] = species_name
            symbol_map[symbol] = species_name

        self._species_map = species_map
        self._base_map = base_map
        self._symbol_map = symbol_map
        self._last_refresh_ts = datetime.utcnow()

    def resolve_species(self, token: str) -> str:
        normalized = (token or "").strip().lower().replace("-", "")
        if not normalized:
            raise ValueError("Parameter is empty.")

        for species in self._species_map.keys():
            if species.lower().replace("-", "") == normalized:
                return species

        for base, species in self._base_map.items():
            if base.lower().replace("-", "") == normalized:
                return species

        for symbol, species in self._symbol_map.items():
            if symbol.lower().replace("-", "") == normalized:
                return species

        examples = sorted(self._species_map.keys())
        raise ValueError(
            "Unknown species token. Try a species name ({}), base symbol (e.g., BTC), or market symbol (e.g., BTCUSDT).".format(
                ", ".join(examples)
            )
        )

    def species_mapping(self) -> Dict[str, Dict[str, str]]:
        return self._species_map

    def base_mapping(self) -> Dict[str, str]:
        return self._base_map

    def symbol_mapping(self) -> Dict[str, str]:
        return self._symbol_map

    def mapping_snapshot(self) -> Dict[str, object]:
        entries = [
            {"name": species, "base": meta.get("base"), "symbol": meta.get("symbol")}
            for species, meta in sorted(self._species_map.items())
        ]
        ts = self._last_refresh_ts.isoformat() if self._last_refresh_ts else None
        return {"entries": entries, "ts": ts}
