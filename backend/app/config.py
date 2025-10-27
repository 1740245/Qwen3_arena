from __future__ import annotations

import logging
from functools import lru_cache
from typing import Dict, List

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    bitget_api_key: str = Field(
        default="",
        alias="BITGET_API_KEY",
        validation_alias=AliasChoices(
            "BITGET_API_KEY",
            "bitget_api_key",
            "BITGET_APIKEY",
            "bitget_apikey",
        ),
        description="API key for Bitget REST trading access.",
    )
    bitget_api_secret: str = Field(
        default="",
        alias="BITGET_API_SECRET",
        validation_alias=AliasChoices(
            "BITGET_API_SECRET",
            "bitget_api_secret",
            "BITGET_SECRET_KEY",
            "bitget_secret_key",
        ),
        description="API secret for Bitget HMAC signing.",
    )
    bitget_passphrase: str = Field(
        default="",
        alias="BITGET_PASSPHRASE",
        validation_alias=AliasChoices(
            "BITGET_PASSPHRASE",
            "bitget_passphrase",
            "BITGET_API_PASSPHRASE",
            "bitget_api_passphrase",
        ),
        description="Passphrase configured when creating the Bitget API key.",
    )
    bitget_base_url: str = Field(
        default="https://api.bitget.com",
        alias="BITGET_BASE_URL",
        validation_alias=AliasChoices("BITGET_BASE_URL", "bitget_base_url"),
        description="Base URL for Bitget REST API.",
    )
    bitget_demo_base_url: str = Field(
        default="https://demo-openapi.bitget.com",
        alias="BITGET_DEMO_BASE_URL",
        validation_alias=AliasChoices("BITGET_DEMO_BASE_URL", "bitget_demo_base_url"),
        description="Base URL for Bitget demo/paper trading REST API.",
    )
    portfolio_base_species: str = Field(
        default="Rare Candy",
        alias="PORTFOLIO_BASE_SPECIES",
        validation_alias=AliasChoices("PORTFOLIO_BASE_SPECIES", "portfolio_base_species"),
        description="Display name for the base currency (e.g., USD).",
    )
    environment: str = Field(
        default="development",
        alias="ENVIRONMENT",
        validation_alias=AliasChoices("ENVIRONMENT", "environment"),
        description="Runtime environment tag for logging and debugging.",
    )
    log_level: str = Field(
        default="INFO",
        alias="LOG_LEVEL",
        validation_alias=AliasChoices("LOG_LEVEL", "log_level"),
        description="Python logging level for the application.",
    )
    cooldown_seconds: int = Field(
        default=5,
        alias="ADVENTURE_COOLDOWN_SECONDS",
        validation_alias=AliasChoices(
            "ADVENTURE_COOLDOWN_SECONDS",
            "ORDER_COOLDOWN_SECONDS",
            "order_cooldown_seconds",
        ),
        description="Cooldown in seconds before the next Poké Ball throw.",
    )
    max_team_size: int = Field(
        default=6,
        alias="MAX_TEAM_SIZE",
        validation_alias=AliasChoices(
            "MAX_TEAM_SIZE",
            "max_team_size",
            "ADVENTURE_TEAM_CAP",
            "adventure_team_cap",
        ),
        description="Maximum simultaneous positions/orders allowed.",
    )
    minimum_energy_reserve: float = Field(
        default=25.0,
        alias="ADVENTURE_MIN_QUOTE_RESERVE",
        validation_alias=AliasChoices(
            "ADVENTURE_MIN_QUOTE_RESERVE",
            "MINIMUM_ENERGY_RESERVE",
            "adventure_min_quote_reserve",
            "minimum_energy_reserve",
        ),
        description="Minimum USDT balance to allow new captures.",
    )
    adventure_demo_mode: bool = Field(
        default=False,
        alias="ADVENTURE_DEMO_MODE",
        validation_alias=AliasChoices(
            "ADVENTURE_DEMO_MODE",
            "adventure_demo_mode",
            "DEMO_MODE",
            "demo_mode",
        ),
        description="Enable adventure-wide demo mode guardrails.",
    )
    adventure_demo_energy: float = Field(
        default=1000.0,
        alias="ADVENTURE_DEMO_ENERGY",
        description="Simulated Energy pool used when demo mode bypasses balances.",
    )
    adventure_energy_scale_usdt: float = Field(
        default=1000.0,
        alias="ADVENTURE_ENERGY_SCALE_USDT",
        validation_alias=AliasChoices(
            "ADVENTURE_ENERGY_SCALE_USDT",
            "adventure_energy_scale_usdt",
            "ENERGY_SCALE_USDT",
            "energy_scale_usdt",
        ),
        description="USDT amount that maps to a full Energy bar when trading live.",
    )
    adventure_energy_source: str = Field(
        default="perp",
        alias="ADVENTURE_ENERGY_SOURCE",
        validation_alias=AliasChoices(
            "ADVENTURE_ENERGY_SOURCE",
            "adventure_energy_source",
        ),
        description="Which energy bucket to show numerically (perp or total).",
    )
    adventure_margin_mode: str = Field(
        default="crossed",
        alias="ADVENTURE_MARGIN_MODE",
        validation_alias=AliasChoices(
            "ADVENTURE_MARGIN_MODE",
            "adventure_margin_mode",
        ),
        description="Margin mode to send on USDT-M orders (crossed or isolated).",
    )
    adventure_embed_sl: bool = Field(
        default=True,
        alias="ADVENTURE_EMBED_SL",
        validation_alias=AliasChoices(
            "ADVENTURE_EMBED_SL",
            "adventure_embed_sl",
        ),
        description="Embed stop loss parameters directly in perp place-order payloads.",
    )
    adventure_show_energy_numbers: bool = Field(
        default=True,
        alias="ADVENTURE_SHOW_ENERGY_NUMBERS",
        validation_alias=AliasChoices(
            "ADVENTURE_SHOW_ENERGY_NUMBERS",
            "adventure_show_energy_numbers",
        ),
        description="Toggle numeric energy captions in the UI.",
    )
    gate_phrase: str | None = Field(
        default=None,
        alias="GATE_PHRASE",
        validation_alias=AliasChoices("GATE_PHRASE", "gate_phrase"),
        description="Retro gate phrase required to unlock the adventure UI.",
    )
    session_secret: str | None = Field(
        default=None,
        alias="SESSION_SECRET",
        validation_alias=AliasChoices("SESSION_SECRET", "session_secret"),
        description="Secret key for signing adventure session cookies.",
    )
    adventure_default_level: int = Field(
        default=1,
        alias="ADVENTURE_DEFAULT_LV",
        validation_alias=AliasChoices(
            "ADVENTURE_DEFAULT_LV",
            "adventure_default_lv",
            "DEFAULT_ADVENTURE_LV",
            "default_adventure_lv",
        ),
        description="Baseline adventure level used when the UI does not specify one.",
    )
    pinned_perp_bases: List[str] = Field(
        default_factory=lambda: [
            "BTC",
            "ETH",
            "SOL",
            "XRP",
            "DOGE",
            "HYPE",
            "AVAX",
            "SUI",
            "BNB",
            "WLD",
        ],
        alias="PINNED_PERP_BASES",
        validation_alias=AliasChoices("PINNED_PERP_BASES", "pinned_perp_bases"),
        description="Comma-separated list of pinned USDT-M perp bases in roster order.",
    )

    def model_post_init(self, __context: object) -> None:
        self._credential_flags: Dict[str, bool] = {
            "api_key": bool(self.bitget_api_key),
            "secret": bool(self.bitget_api_secret),
            "passphrase": bool(self.bitget_passphrase),
        }
        if isinstance(self.pinned_perp_bases, str):
            bases = [item.strip().upper() for item in self.pinned_perp_bases.split(",") if item.strip()]
            if bases:
                self.pinned_perp_bases = bases
            else:
                self.pinned_perp_bases = [
                    "BTC",
                    "ETH",
                    "SOL",
                    "XRP",
                    "DOGE",
                    "HYPE",
                    "AVAX",
                    "SUI",
                    "BNB",
                    "PEPE",
                ]
        else:
            self.pinned_perp_bases = [base.upper() for base in self.pinned_perp_bases]

        try:
            scale = float(self.adventure_energy_scale_usdt)
        except (TypeError, ValueError):
            scale = 1000.0
        if scale <= 0:
            scale = 1000.0
        self.adventure_energy_scale_usdt = scale

        source = str(self.adventure_energy_source or "perp").lower()
        if source not in {"perp", "total"}:
            source = "perp"
        self.adventure_energy_source = source

        margin_mode = str(self.adventure_margin_mode or "crossed").lower()
        if margin_mode not in {"crossed", "isolated"}:
            margin_mode = "crossed"
        self.adventure_margin_mode = margin_mode

        creds_complete = all(self._credential_flags.values())
        self._runtime_mode = "live" if (not self.adventure_demo_mode and creds_complete) else "demo"
        self._trading_locked = (not self.adventure_demo_mode) and not creds_complete

        summary = ", ".join(
            f"{key}={'ok' if present else 'missing'}"
            for key, present in self._credential_flags.items()
        )
        if not getattr(self, "_startup_summary_logged", False):
            logger.info("Adventure boot mode=%s; credentials: %s", self._runtime_mode, summary)
            self._startup_summary_logged = True

        if self._trading_locked and not getattr(self, "_trading_warning_logged", False):
            missing = ", ".join(self.missing_credentials()) or "credentials"
            logger.warning(
                "Professor Elm waves: live adventures need Bitget credentials (%s). Trading endpoints are resting until keys arrive or demo mode is enabled.",
                missing,
            )
            self._trading_warning_logged = True

    def has_api_credentials(self) -> bool:
        """Return True if a full credential set is configured."""

        return all(self._credential_flags.values())

    @property
    def credential_status(self) -> Dict[str, bool]:
        return dict(self._credential_flags)

    def missing_credentials(self) -> List[str]:
        return [key for key, present in self._credential_flags.items() if not present]

    @property
    def runtime_mode(self) -> str:
        return self._runtime_mode

    @property
    def trading_locked(self) -> bool:
        return self._trading_locked


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Provide a cached Settings instance."""

    return Settings()  # type: ignore[arg-type]
