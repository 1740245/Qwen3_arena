from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import uuid
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from starlette.middleware.base import BaseHTTPMiddleware

from .adapters.bitget_client import BitgetClient
from .config import get_settings
from .schemas import AdventureOrderReceipt, EncounterOrder, RosterResponse, TrainerStatus
from .services.orders import AdventureOrderService
from .services.price_feed import PriceFeed
from .services.roster import PokemonRosterService
from .services.translators import PokemonTranslator, default_translator
from .utils.branding import sanitize_vendor_terms

settings = get_settings()
bitget = BitgetClient(settings)

async def fixed_post(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    import base64 as _base64
    import hashlib as _hashlib
    import hmac as _hmac
    import json as _json
    import time as _time

    api_key = settings.bitget_api_key
    api_secret = getattr(settings, "bitget_api_secret", None) or getattr(settings, "bitget_secret_key", None)
    passphrase = settings.bitget_passphrase
    base_url = settings.bitget_base_url.rstrip("/")
    if not all([api_key, api_secret, passphrase]):
        raise RuntimeError("Bitget API credentials are not configured.")

    timestamp = str(int(_time.time() * 1000))
    body = _json.dumps(params or {}, separators=(",", ":"))
    sign_payload = f"{timestamp}POST{path}{body}"
    signature = _base64.b64encode(
        _hmac.new(api_secret.encode("utf-8"), sign_payload.encode("utf-8"), _hashlib.sha256).digest()
    ).decode()

    headers = {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        response = await http_client.post(
            f"{base_url}{path}",
            headers=headers,
            content=body,
        )
        response.raise_for_status()
        return response.json()

bitget.post = fixed_post
translator: PokemonTranslator = default_translator(settings.adventure_margin_mode)
bitget_client = bitget
price_feed = PriceFeed(bitget_client, settings.pinned_perp_bases)
order_service = AdventureOrderService(bitget_client, translator, settings, price_feed)
roster_service = PokemonRosterService(
    bitget_client,
    translator,
    price_feed,
    settings.pinned_perp_bases,
)

SESSION_COOKIE_NAME = "adventure_session"
SESSION_TTL_SECONDS = 8 * 60 * 60  # 8 hours
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 5 * 60

class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._failures: Dict[str, List[float]] = {}

    def can_attempt(self, ip: str) -> bool:
        now = time.time()
        attempts = [ts for ts in self._failures.get(ip, []) if now - ts < self._window]
        self._failures[ip] = attempts
        return len(attempts) < self._max_attempts

    def record_failure(self, ip: str) -> None:
        now = time.time()
        attempts = [ts for ts in self._failures.get(ip, []) if now - ts < self._window]
        attempts.append(now)
        self._failures[ip] = attempts

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)

login_rate_limiter = LoginRateLimiter(LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS)

OPEN_ORDERS_TTL_SECONDS = 4.0
_open_orders_cache: Dict[str, object] = {
    "expires": 0.0,
    "payload": None,
    "ts": "",
}

def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")

def _b64decode(data: str) -> bytes:
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))

def _create_session_token() -> str:
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured.")
    now_ms = int(time.time() * 1000)
    payload = {"iat": now_ms, "exp": now_ms + SESSION_TTL_SECONDS * 1000}
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    data_b64 = _b64encode(payload_bytes)
    signature = hmac.new(
        settings.session_secret.encode("utf-8"),
        data_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    sig_b64 = _b64encode(signature)
    return f"{data_b64}.{sig_b64}"

def _verify_session_token(token: str) -> bool:
    secret = settings.session_secret
    if not secret:
        return False
    try:
        data_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        return False

    expected_sig = _b64encode(
        hmac.new(secret.encode("utf-8"), data_b64.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected_sig, sig_b64):
        return False

    try:
        payload_bytes = _b64decode(data_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return False

    exp = payload.get("exp")
    if not isinstance(exp, int):
        return False
    now_ms = int(time.time() * 1000)
    if exp <= now_ms:
        return False
    return True

def _is_secure_request(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return str(proto).lower() == "https"

def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        expires=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_is_secure_request(request),
        path="/",
    )

def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")

async def proper_post(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    api_key = settings.bitget_api_key
    api_secret = settings.bitget_api_secret
    passphrase = settings.bitget_passphrase
    base_url = settings.bitget_base_url.rstrip("/")

    if not all([api_key, api_secret, passphrase]):
        raise RuntimeError("Bitget API credentials are not configured.")

    timestamp = str(int(time.time() * 1000))
    body = json.dumps(data, separators=(",", ":"))
    prehash = f"{timestamp}POST{path}{body}"
    signature = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode()

    headers = {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "locale": "en-US",
    }

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        response = await http_client.post(
            f"{base_url}{path}",
            headers=headers,
            content=body,
        )
        response.raise_for_status()
        return response.json()

def _has_valid_session(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    return _verify_session_token(token)

class GatekeeperMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, **kwargs) -> None:
        super().__init__(app)
        self._settings = (
            kwargs.pop("settings_obj", None)
            or kwargs.pop("settings", None)
        )
        if self._settings is None:
            from .config import get_settings as _get_settings

            self._settings = _get_settings()

        self._exempt_paths = {
            "/api/session/login",
            "/api/session/login/",
            "/api/session/logout",
            "/api/session/logout/",
            "/gate",
            "/gate/",
            "/api/health",
        }

    def _requires_guard(self, path: str) -> bool:
        if path in self._exempt_paths:
            return False
        if path.startswith("/api/"):
            return True
        if path.startswith("/playground"):
            return True
        if path.startswith("/gate"):
            return False
        if path in {"/docs", "/docs/", "/redoc", "/redoc/", "/openapi.json"}:
            return True
        return False

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        if not self._requires_guard(path):
            return await call_next(request)

        if _has_valid_session(request):
            return await call_next(request)

        if request.method.upper() == "GET":
            return RedirectResponse(url="/gate/", status_code=302)

        return JSONResponse(
            {"ok": False, "msg": "Locked. Visit /gate/ to enter."},
            status_code=401,
        )

app = FastAPI(
    title="Johto Adventure Desk",
    description="Pokemon-themed Bitget execution assistant.",
    version="0.1.0",
)

logger = logging.getLogger(__name__)

app.add_middleware(GatekeeperMiddleware, settings=settings)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    for error in exc.errors():
        msg = error.get("msg")
        if isinstance(msg, str) and msg == EncounterOrder.ANCHOR_INVALID_MESSAGE:
            return JSONResponse(status_code=400, content={"detail": msg})
    return await request_validation_exception_handler(request, exc)

STARTUP_TS_MS = int(time.time() * 1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"]
,
    allow_headers=["*"],
)

public_dir = Path(__file__).resolve().parent.parent / ".." / "frontend" / "public"

def branded_detail(text: str | None) -> str:
    sanitized = sanitize_vendor_terms(text) if text is not None else None
    if sanitized:
        return sanitized
    return "Prof. Oak encountered an unknown error."

@app.get("/gate", include_in_schema=False)
async def gate_redirect() -> RedirectResponse:
    return RedirectResponse(url="/gate/", status_code=302)

@app.get("/gate/", include_in_schema=False)
async def gate_page() -> FileResponse:
    gate_path = public_dir / "gate.html"
    if not gate_path.exists():
        raise HTTPException(status_code=404, detail="Gate is still under construction.")
    return FileResponse(gate_path)

@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/gate/", status_code=302)

def _extract_phrase(payload: Any) -> str:
    if isinstance(payload, dict):
        candidate = payload.get("phrase")
        if candidate is None:
            candidate = payload.get("name")
    else:
        candidate = None
    if candidate is None:
        return ""
    if isinstance(candidate, (int, float)):
        candidate = str(candidate)
    if not isinstance(candidate, str):
        return ""
    return candidate.strip()

@app.post("/api/session/login")
async def session_login(request: Request) -> JSONResponse:
    gate_phrase = settings.gate_phrase
    if not gate_phrase:
        return JSONResponse(
            {"ok": False, "msg": "Professor Elm hasn't set the gate phrase yet. Try again later."},
            status_code=503,
        )

    if not settings.session_secret:
        return JSONResponse(
            {"ok": False, "msg": "Professor Elm mislaid the session secret. Please alert the lab."},
            status_code=503,
        )

    client_host = request.client.host if request.client else "unknown"
    if not login_rate_limiter.can_attempt(client_host):
        return JSONResponse(
            {"ok": False, "msg": "Hold up, Trainer! Too many tries. Take a five‑minute breather."},
            status_code=429,
        )

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    provided = _extract_phrase(payload)
    expected = gate_phrase.strip()

    if not provided or not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        login_rate_limiter.record_failure(client_host)
        return JSONResponse(
            {"ok": False, "msg": "Professor Elm squints: that name isn't in the Johto registry."},
            status_code=403,
        )

    login_rate_limiter.reset(client_host)
    token = _create_session_token()
    response = JSONResponse({"ok": True, "msg": "Gate unlocked. Welcome back, Trainer!"})
    _set_session_cookie(response, request, token)
    return response

@app.post("/api/session/logout")
async def session_logout(request: Request) -> JSONResponse:
    response = JSONResponse({"ok": True, "msg": "Session closed."})
    _clear_session_cookie(response)
    return response

@app.get("/api/session/status")
async def session_status(request: Request) -> Dict[str, bool]:
    return {"authed": _has_valid_session(request)}

@app.on_event("startup")
async def startup_event() -> None:
    await roster_service.refresh(force=True)
    price_feed.start()

@app.on_event("shutdown")
async def shutdown_event() -> None:
    await price_feed.stop()
    await bitget.close()

@app.get("/api/atlas/species")
def list_species() -> Dict[str, Dict[str, object]]:
    return translator.supported_species()

@app.get("/api/adventure/species")
async def adventure_species_mapping() -> Dict[str, object]:
    snapshot = roster_service.mapping_snapshot()
    entries = snapshot.get("entries", [])
    timestamp = snapshot.get("ts")
    if not timestamp:
        timestamp = datetime.now(timezone.utc).isoformat()
    return {"ok": True, "species": entries, "ts": timestamp}

@app.get("/api/atlas/roster", response_model=RosterResponse)
async def current_roster() -> RosterResponse:
    return await roster_service.current_roster()

@app.post("/api/atlas/refresh", response_model=RosterResponse)
async def refresh_roster() -> RosterResponse:
    try:
        return await roster_service.refresh(force=True)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=branded_detail(exc.response.text),
        )

@app.get("/api/atlas/prices")
async def atlas_prices() -> Dict[str, object]:
    return await price_feed.snapshot()

@app.get("/api/atlas/health")
async def atlas_health() -> Dict[str, object]:
    return {
        "ok": True,
        "mode": settings.runtime_mode,
        "keys": settings.credential_status,
        "startupTs": STARTUP_TS_MS,
        "linkShell": order_service.link_shell_state(),
        "energyPresent": order_service.energy_present(),
    }

@app.get("/api/debug/perp-account")
async def debug_perp_account() -> Dict[str, object]:
    try:
        raw = await bitget_client.get_perp_account_raw()
        if isinstance(raw, dict):
            data_candidate = raw.get("data") or raw.get("data_obj")
            data = data_candidate if isinstance(data_candidate, dict) else {}
        else:
            data = {}
        keys = sorted(list(data.keys())) if data else []
        return {
            "ok": True,
            "source": "/api/v2/mix/account/account",
            "keys": keys,
            "data": data,
            "payload": raw,
        }
    except httpx.HTTPStatusError as exc:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "kind": "http",
                "status_code": getattr(exc.response, "status_code", None),
                "text": getattr(exc.response, "text", None),
                "detail": str(exc),
            },
        )
    except Exception as exc:  # pragma: no cover - debug safety
        import traceback

        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "kind": "other",
                "detail": str(exc),
                "trace": traceback.format_exc(),
            },
        )

@app.get("/api/debug/open-orders-raw")
async def debug_open_orders_raw() -> Dict[str, object]:
    try:
        payload = await bitget_client.fetch_pending_perp_orders_raw()
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}

    if isinstance(payload, dict) and "error" in payload and len(payload) == 1:
        return {"ok": False, "detail": str(payload.get("error"))}

    entries: List[Dict[str, Any]] = []

    if isinstance(payload, dict):
        data_field = payload.get("data")
        if isinstance(data_field, list):
            entries = [item for item in data_field if isinstance(item, dict)]
        elif isinstance(data_field, dict):
            entries = [data_field]
        data_list = payload.get("data_list")
        if isinstance(data_list, list):
            entries.extend([item for item in data_list if isinstance(item, dict)])
        list_field = payload.get("list")
        if isinstance(list_field, list):
            entries.extend([item for item in list_field if isinstance(item, dict)])
        if not entries and all(
            key not in payload for key in ("data", "data_list", "list")
        ) and isinstance(payload, dict):
            entries = [payload]
    elif isinstance(payload, list):
        entries = [item for item in payload if isinstance(item, dict)]

    symbols: List[str] = []
    for entry in entries:
        symbol = entry.get("symbol") or entry.get("symbolName") or entry.get("symbolId")
        if isinstance(symbol, str) and symbol:
            symbols.append(symbol)

    sample = entries[0] if entries else None

    return {
        "ok": True,
        "count": len(entries),
        "symbols": symbols,
        "sample": sample,
    }

def _collect_symbols(entries: List[Dict[str, Any]]) -> List[str]:
    symbols: List[str] = []
    for entry in entries:
        symbol = entry.get("symbol") or entry.get("symbolName") or entry.get("symbolId")
        if isinstance(symbol, str) and symbol:
            symbols.append(symbol.upper())
    return symbols

def _extract_entries(payload: Any) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        for key in ("data", "data_list", "list"):
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

@app.get("/api/debug/open-orders-raw-full")
async def debug_open_orders_raw_full() -> Dict[str, object]:
    try:
        working = await bitget_client.get_mix_orders_pending()
        plan = await bitget_client.get_mix_orders_plan_pending()
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}

    working_entries = _extract_entries(working)
    plan_entries = _extract_entries(plan)

    sample = working_entries[0] if working_entries else None
    if isinstance(sample, dict):
        for key in (
            "presetStopLossPrice",
            "presetStopLossExecutePrice",
            "presetStopLossTriggerType",
            "presetStopSurplusPrice",
            "presetStopSurplusExecutePrice",
            "presetStopSurplusTriggerType",
        ):
            sample.setdefault(key, sample.get(key) or "")

    working_payload = {
        "count": len(working_entries),
        "symbols": _collect_symbols(working_entries),
        "sample": sample,
    }
    plan_payload = {
        "count": len(plan_entries),
        "symbols": _collect_symbols(plan_entries),
        "sample": plan_entries[0] if plan_entries else None,
    }

    return {
        "ok": True,
        "working": working_payload,
        "plan": plan_payload,
    }

@app.get("/api/debug/working/{species}")
async def debug_working_orders(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    payload = await bitget_client.fetch_working_orders_v2()
    if isinstance(payload, dict) and "error" in payload and len(payload) == 2:
        return {"ok": False, "detail": payload.get("error")}

    entries = _collect_order_entries(payload)
    normalized_symbol = symbol.upper().split("_", 1)[0]
    filtered = [
        item
        for item in entries
        if isinstance(item.get("symbol"), str)
        and item.get("symbol").upper().split("_", 1)[0] == normalized_symbol
    ]

    sample = filtered[0] if filtered else None
    if isinstance(sample, dict):
        for key in (
            "presetStopLossPrice",
            "presetStopLossExecutePrice",
            "presetStopLossTriggerType",
            "presetStopSurplusPrice",
            "presetStopSurplusExecutePrice",
            "presetStopSurplusTriggerType",
        ):
            sample.setdefault(key, sample.get(key) or "")

    keys = list(sample.keys()) if isinstance(sample, dict) else []

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": symbol,
        "count": len(filtered),
        "keys": keys,
        "sample": sample,
    }

@app.get("/api/debug/working-full/{species}")
async def debug_working_full(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    normalized_symbol = symbol.upper()
    try:
        payload = await bitget_client.fetch_working_orders_v2(symbol=normalized_symbol)
    except Exception as exc:  # pragma: no cover - defensive debug guard
        logger.debug("Working-full fetch failed: %s", exc)
        return {"ok": False, "detail": str(exc)}

    if isinstance(payload, dict) and "error" in payload and len(payload) <= 2:
        detail = payload.get("error")
        detail_text = detail if isinstance(detail, str) else str(detail)
        return {"ok": False, "detail": detail_text}

    if not isinstance(payload, dict):
        return {"ok": False, "detail": "Unexpected payload structure."}

    data = payload.get("data") if isinstance(payload, dict) else None
    orders: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        entrusted = data.get("entrustedList")
        if isinstance(entrusted, list):
            orders = [row for row in entrusted if isinstance(row, dict)]

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": normalized_symbol,
        "count": len(orders),
        "orders": orders,
    }

@app.get("/api/debug/all-stop-orders/{species}")
async def debug_all_stop_orders(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    normalized_symbol = symbol.upper()

    plan_types: List[Optional[str]] = [
        None,
        "normal_plan",
        "profit_plan",
        "loss_plan",
        "pos_profit",
        "pos_loss",
        "moving_plan",
    ]

    plan_entries: List[Dict[str, Any]] = []
    try:
        for plan_type in plan_types:
            rows = await bitget_client.list_symbol_plan_orders_safe(normalized_symbol, plan_type=plan_type)
            if rows:
                plan_entries.extend(rows)
    except Exception as exc:  # pragma: no cover - defensive debug guard
        logger.debug("Plan stop-order fetch failed: %s", exc)
        return {"ok": False, "detail": str(exc)}

    try:
        tpsl_entries = await bitget_client.list_symbol_tpsl_orders_safe(normalized_symbol)
    except Exception as exc:  # pragma: no cover - defensive debug guard
        logger.debug("TPSL stop-order fetch failed: %s", exc)
        return {"ok": False, "detail": str(exc)}

    if not tpsl_entries:
        try:
            tpsl_entries = await bitget_client.list_symbol_tpsl_orders_safe_v1(normalized_symbol)
        except Exception as exc:  # pragma: no cover - defensive debug guard
            logger.debug("TPSL v1 fallback failed: %s", exc)
            tpsl_entries = []

    def summarize(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        sample = entries[0] if entries else None
        first_keys = list(sample.keys())[:15] if isinstance(sample, dict) else []
        return {
            "count": len(entries),
            "firstKeys": first_keys,
            "sample": sample,
        }

    return {
        "ok": True,
        "symbol": normalized_symbol,
        "plan": summarize(plan_entries),
        "tpsl": summarize(tpsl_entries),
    }

@app.get("/api/debug/position/{species}")
async def debug_position(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    normalized_symbol = symbol.upper()
    base_token = species_meta.get("base")
    if isinstance(base_token, str) and base_token:
        base_upper = base_token.upper()
    elif normalized_symbol.endswith("USDT"):
        base_upper = normalized_symbol[:-4]
    else:
        base_upper = normalized_symbol.split("_", 1)[0]

    variants = {normalized_symbol, f"{normalized_symbol}_UMCBL"}
    if base_upper:
        variants.update(
            {
                base_upper,
                f"{base_upper}USDT",
                f"{base_upper}USDT_UMCBL",
            }
        )

    def extract_detail(result: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(result, dict):
            return None
        for key in ("error", "detail", "text"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        status_val = result.get("status")
        if isinstance(status_val, int):
            return f"HTTP {status_val}"
        return None

    single_result = await bitget_client.read_single_position(normalized_symbol)
    entries: List[Dict[str, Any]] = []
    single_entries = single_result.get("entries")
    if isinstance(single_entries, list):
        entries = [entry for entry in single_entries if isinstance(entry, dict)]

    fallback_result: Optional[Dict[str, Any]] = None
    if not entries:
        fallback_result = await bitget_client.read_all_positions()
        if not fallback_result.get("ok"):
            detail = extract_detail(fallback_result) or extract_detail(single_result) or "Unable to fetch positions."
            return {"ok": False, "detail": detail}

        fallback_entries = fallback_result.get("entries")
        if isinstance(fallback_entries, list):
            filtered: List[Dict[str, Any]] = []
            for entry in fallback_entries:
                if not isinstance(entry, dict):
                    continue
                symbols_to_check: List[str] = []
                raw_symbol = entry.get("symbol") if isinstance(entry, dict) else None
                if isinstance(raw_symbol, str):
                    symbols_to_check.append(raw_symbol.upper())
                raw_name = entry.get("symbolName") if isinstance(entry, dict) else None
                if isinstance(raw_name, str):
                    symbols_to_check.append(raw_name.upper())
                matched = False
                for candidate in symbols_to_check:
                    prefix = candidate.split("_", 1)[0]
                    if candidate in variants or prefix in variants:
                        matched = True
                        break
                if matched:
                    filtered.append(entry)
            entries = filtered

    sample_entry = entries[0] if entries else None
    keys = list(sample_entry.keys())[:15] if isinstance(sample_entry, dict) else []
    sample_trimmed = {
        key: sample_entry.get(key)
        for key in keys
    } if isinstance(sample_entry, dict) else {}

    def pick(entry: Optional[Dict[str, Any]], field: str) -> Any:
        if isinstance(entry, dict):
            return entry.get(field)
        return None

    sl_candidates = {
        "stopLossPrice": pick(sample_entry, "stopLossPrice"),
        "stopLossTriggerPrice": pick(sample_entry, "stopLossTriggerPrice"),
        "stopLossExecutePrice": pick(sample_entry, "stopLossExecutePrice"),
        "slTriggerType": pick(sample_entry, "slTriggerType"),
        "posLossPrice": pick(sample_entry, "posLossPrice"),
        "tpslId": pick(sample_entry, "tpslId"),
    }

    tp_candidates = {
        "takeProfitPrice": pick(sample_entry, "takeProfitPrice"),
        "tpTriggerPrice": pick(sample_entry, "tpTriggerPrice"),
        "tpExecutePrice": pick(sample_entry, "tpExecutePrice"),
        "tpTriggerType": pick(sample_entry, "tpTriggerType"),
        "posProfitPrice": pick(sample_entry, "posProfitPrice"),
    }

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": normalized_symbol,
        "count": len(entries),
        "keys": keys,
        "slCandidates": sl_candidates,
        "tpCandidates": tp_candidates,
        "sample": sample_trimmed,
    }

@app.get("/api/debug/tpsl-pending/{species}")
async def debug_tpsl_pending(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    normalized_symbol = symbol.upper()

    attempts = [
        {
            "label": "plan-pending-all",
            "path": "/api/v2/mix/order/orders-plan-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "symbol": normalized_symbol,
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "plan-pending-loss",
            "path": "/api/v2/mix/order/orders-plan-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "symbol": normalized_symbol,
                "planType": "loss_plan",
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "plan-pending-pos-loss",
            "path": "/api/v2/mix/order/orders-plan-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "symbol": normalized_symbol,
                "planType": "pos_loss",
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "trigger-orders",
            "path": "/api/v2/mix/order/trigger-orders",
            "params": {
                "productType": "USDT-FUTURES",
                "symbol": normalized_symbol,
                "isPlan": "plan",
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "plan-all-no-symbol",
            "path": "/api/v2/mix/order/orders-plan-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "pageSize": 100,
                "pageNo": 1,
            },
        },
    ]

    results: List[Dict[str, Any]] = []
    for attempt in attempts:
        params = dict(attempt.get("params", {}))
        try:
            wrapper = await bitget._request(
                "GET",
                attempt["path"],
                params=params,
            )
            raw_payload: Any = wrapper.get("raw") if isinstance(wrapper, dict) else wrapper

            entries: List[Dict[str, Any]] = []
            list_name = "none"
            if isinstance(raw_payload, dict):
                data_block = raw_payload.get("data")
                if isinstance(data_block, dict):
                    entrusted = data_block.get("entrustedList")
                    if isinstance(entrusted, list):
                        entries = [row for row in entrusted if isinstance(row, dict)]
                        list_name = "entrustedList"
                    else:
                        candidate_list = data_block.get("list")
                        if isinstance(candidate_list, list):
                            entries = [row for row in candidate_list if isinstance(row, dict)]
                            list_name = "list"
                elif isinstance(data_block, list):
                    entries = [row for row in data_block if isinstance(row, dict)]
                    list_name = "data"
                elif isinstance(raw_payload.get("entrustedList"), list):
                    entries = [row for row in raw_payload.get("entrustedList", []) if isinstance(row, dict)]
                    list_name = "entrustedList"
            elif isinstance(raw_payload, list):
                entries = [row for row in raw_payload if isinstance(row, dict)]
                list_name = "root"

            if "symbol" not in params and entries:
                entries = [
                    row
                    for row in entries
                    if isinstance(row.get("symbol"), str) and row["symbol"].upper() == normalized_symbol
                ]

            first_keys: List[str] = []
            sample: Dict[str, Any] = {}
            if entries:
                sample_entry = entries[0]
                if isinstance(sample_entry, dict):
                    first_keys = list(sample_entry.keys())[:15]
                    sample = {key: sample_entry.get(key) for key in first_keys}

            results.append(
                {
                    "label": attempt["label"],
                    "status": 200,
                    "count": len(entries),
                    "listName": list_name,
                    "firstKeys": first_keys,
                    "sample": sample,
                }
            )
        except Exception as exc:  # pragma: no cover - defensive debug guard
            results.append(
                {
                    "label": attempt["label"],
                    "error": str(exc),
                }
            )

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": normalized_symbol,
        "attempts": results,
    }

@app.get("/api/debug/position-tpsl/{species}")
async def debug_position_tpsl(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    normalized_symbol = symbol.upper()

    try:
        single_result = await bitget_client.read_single_position(normalized_symbol)
    except Exception as exc:  # pragma: no cover - defensive debug guard
        logger.debug("Position TPSL probe failed: %s", exc)
        return {"ok": False, "detail": str(exc)}

    if not isinstance(single_result, dict):
        return {"ok": False, "detail": "Unexpected position payload."}

    if not single_result.get("ok"):
        detail = single_result.get("error") or single_result.get("detail") or "Unable to fetch position."
        return {"ok": False, "detail": detail}

    entries = single_result.get("entries") if isinstance(single_result, dict) else None
    position_entry: Optional[Dict[str, Any]] = None
    if isinstance(entries, list):
        for candidate in entries:
            if isinstance(candidate, dict):
                position_entry = candidate
                break

    raw_payload = single_result.get("payload") if isinstance(single_result, dict) else None
    if not isinstance(raw_payload, dict):
        raw_payload = position_entry if isinstance(position_entry, dict) else None

    all_fields = list(position_entry.keys()) if isinstance(position_entry, dict) else []
    position_info = {
        "exists": bool(position_entry),
        "stopLossPrice": position_entry.get("stopLossPrice") if isinstance(position_entry, dict) else None,
        "takeProfitPrice": position_entry.get("takeProfitPrice") if isinstance(position_entry, dict) else None,
        "tpslMode": position_entry.get("tpslMode") if isinstance(position_entry, dict) else None,
        "allFields": all_fields,
        "raw": position_entry if isinstance(position_entry, dict) else raw_payload,
    }

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": normalized_symbol,
        "position": position_info,
    }

@app.get("/api/debug/find-sl/{species}")
async def debug_find_sl(species: str) -> Dict[str, object]:
    """Probe likely stop-loss sources for the requested symbol."""
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    normalized_symbol = symbol.upper()
    results: Dict[str, Any] = {}

    # 1. Position-level SL/TP
    try:
        position_result = await bitget_client.read_single_position(normalized_symbol)
        if isinstance(position_result, dict) and position_result.get("ok"):
            entries = position_result.get("entries")
            entry = next((e for e in entries if isinstance(e, dict)), None) if isinstance(entries, list) else None
            results["position"] = {
                "has_sl": bool(entry and entry.get("stopLossPrice")),
                "sl_price": entry.get("stopLossPrice") if isinstance(entry, dict) else None,
                "tp_price": entry.get("takeProfitPrice") if isinstance(entry, dict) else None,
            }
        else:
            detail = position_result.get("error") if isinstance(position_result, dict) else "Unable to fetch position."
            results["position"] = {"error": detail}
    except Exception as exc:  # pragma: no cover - defensive debug guard
        results["position"] = {"error": str(exc)}

    # 2. Pending plan orders (global fetch, symbol filtered client-side)
    try:
        plan_payload = await bitget_client.get_mix_orders_plan_pending(
            product_type="USDT-FUTURES",
            page_size=100,
            page_no=1,
        )
        plan_entries = _extract_entries(plan_payload)
        filtered_plans = [
            entry
            for entry in plan_entries
            if isinstance(entry.get("symbol"), str) and normalized_symbol in entry.get("symbol").upper()
        ]
        results["plan_orders"] = {
            "count": len(filtered_plans),
            "orders": filtered_plans,
        }
    except Exception as exc:  # pragma: no cover - defensive debug guard
        results["plan_orders"] = {"error": str(exc)}

    # 3. All positions snapshot (in case SL is stored elsewhere)
    try:
        all_positions_result = await bitget_client.read_all_positions()
        if isinstance(all_positions_result, dict) and all_positions_result.get("ok"):
            entries = all_positions_result.get("entries")
            filtered_positions = [
                entry
                for entry in entries
                if isinstance(entry, dict)
                and isinstance(entry.get("symbol"), str)
                and normalized_symbol in entry.get("symbol").upper()
            ] if isinstance(entries, list) else []
            results["all_positions"] = {
                "count": len(filtered_positions),
                "positions": filtered_positions,
            }
        else:
            detail = all_positions_result.get("error") if isinstance(all_positions_result, dict) else "Unable to fetch positions."
            results["all_positions"] = {"error": detail}
    except Exception as exc:  # pragma: no cover - defensive debug guard
        results["all_positions"] = {"error": str(exc)}

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": normalized_symbol,
        "results": results,
    }

@app.get("/api/debug/position-variations/{species}")
async def debug_position_variations(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    normalized_symbol = symbol.upper()
    variations: List[Dict[str, Any]] = [
        {"productType": "USDT-FUTURES", "symbol": normalized_symbol, "marginCoin": "USDT"},
        {"productType": "usdt-futures", "symbol": normalized_symbol, "marginCoin": "USDT"},
        {"productType": "USDT-FUTURES", "symbol": f"{normalized_symbol}_UMCBL", "marginCoin": "USDT"},
        {"productType": "UMCBL", "symbol": normalized_symbol, "marginCoin": "USDT"},
        {"symbol": normalized_symbol, "marginCoin": "USDT"},
        {"productType": "USDT-FUTURES", "marginCoin": "USDT"},
    ]

    results: List[Dict[str, Any]] = []

    for params in variations:
        single_payload: Optional[Any] = None
        all_payload: Optional[Any] = None
        try:
            single_payload = await bitget_client.get_position_single(
                params.get("symbol", normalized_symbol),
                product_type=params.get("productType", "USDT-FUTURES"),
                margin_coin=params.get("marginCoin", "USDT"),
            )
        except Exception as exc:  # pragma: no cover - defensive debug guard
            results.append({"endpoint": "single-position", "params": params, "error": str(exc)})
            single_payload = None

        if single_payload is not None:
            entries = BitgetClient._extract_position_entries(single_payload)  # type: ignore[attr-defined]
            sample = entries[0] if entries else None
            results.append(
                {
                    "endpoint": "single-position",
                    "params": params,
                    "hasData": bool(entries),
                    "dataType": type(single_payload).__name__,
                    "sample": sample,
                }
            )

        try:
            all_payload = await bitget_client.get_position_all(
                product_type=params.get("productType", "USDT-FUTURES"),
                margin_coin=params.get("marginCoin", "USDT"),
            )
        except Exception as exc:  # pragma: no cover - defensive debug guard
            results.append({"endpoint": "all-position", "params": params, "error": str(exc)})
            all_payload = None

        if all_payload is not None:
            entries = BitgetClient._extract_position_entries(all_payload)  # type: ignore[attr-defined]
            filtered = [
                entry
                for entry in entries
                if isinstance(entry, dict)
                and normalized_symbol in entry.get("symbol", "").upper()
            ]
            results.append(
                {
                    "endpoint": "all-position",
                    "params": params,
                    "count": len(entries),
                    "symbolMatches": filtered,
                }
            )

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": normalized_symbol,
        "results": results,
    }

@app.get("/api/debug/position-raw/{species}")
async def debug_position_raw(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    try:
        payload = await bitget_client.get_position_all(
            product_type="USDT-FUTURES",
            margin_coin="USDT",
        )
    except Exception as exc:  # pragma: no cover - defensive debug guard
        logger.debug("Position raw fetch failed: %s", exc)
        return {"ok": False, "detail": str(exc)}

    raw_payload: Any = payload
    if isinstance(payload, dict) and "raw" in payload:
        raw_candidate = payload.get("raw")
        raw_payload = raw_candidate if raw_candidate is not None else payload

    positions: List[Any] = []
    if isinstance(raw_payload, dict):
        data_block = raw_payload.get("data")
        if isinstance(data_block, list):
            positions = data_block
        elif isinstance(data_block, dict):
            positions = [data_block]
    elif isinstance(raw_payload, list):
        positions = raw_payload

    symbols = [
        entry.get("symbol")
        for entry in positions
        if isinstance(entry, dict)
    ] if positions else []

    return {
        "ok": True,
        "searchingFor": symbol,
        "totalPositions": len(positions),
        "allPositions": positions,
        "firstPosition": positions[0] if positions else None,
        "symbols": symbols,
    }

@app.get("/api/debug/all-data")
async def debug_all_data() -> Dict[str, object]:
    results: Dict[str, Any] = {}

    try:
        positions_payload = await bitget_client.get_position_all(
            product_type="USDT-FUTURES",
            margin_coin="USDT",
        )
        raw_positions = positions_payload.get("raw") if isinstance(positions_payload, dict) else positions_payload
        if isinstance(raw_positions, dict):
            data_block = raw_positions.get("data")
            if isinstance(data_block, list):
                results["positions"] = data_block
            elif isinstance(data_block, dict):
                results["positions"] = [data_block]
            else:
                results["positions"] = []
        elif isinstance(raw_positions, list):
            results["positions"] = raw_positions
        else:
            results["positions"] = []
    except Exception as exc:  # pragma: no cover - defensive debug guard
        results["positions"] = {"error": str(exc)}

    try:
        working_payload = await bitget_client.get_mix_orders_pending(
            product_type="USDT-FUTURES",
            page_size=100,
            page_no=1,
        )
        working_raw = working_payload.get("raw") if isinstance(working_payload, dict) else working_payload
        entries: List[Dict[str, Any]] = []
        if isinstance(working_raw, dict):
            data = working_raw.get("data")
            if isinstance(data, dict):
                entries = [row for row in data.get("entrustedList", []) if isinstance(row, dict)]
            elif isinstance(data, list):
                entries = [row for row in data if isinstance(row, dict)]
        elif isinstance(working_raw, list):
            entries = [row for row in working_raw if isinstance(row, dict)]
        results["working_orders"] = entries
    except Exception as exc:  # pragma: no cover - defensive debug guard
        results["working_orders"] = {"error": str(exc)}

    try:
        plan_payload = await bitget_client.get_mix_orders_plan_pending(
            product_type="USDT-FUTURES",
            page_size=100,
            page_no=1,
        )
        plan_raw = plan_payload.get("raw") if isinstance(plan_payload, dict) else plan_payload
        if isinstance(plan_raw, dict):
            data_block = plan_raw.get("data")
            if isinstance(data_block, dict):
                entries = data_block.get("entrustedList")
                if isinstance(entries, list):
                    results["plan_orders"] = [row for row in entries if isinstance(row, dict)]
                else:
                    results["plan_orders"] = []
            elif isinstance(data_block, list):
                results["plan_orders"] = [row for row in data_block if isinstance(row, dict)]
            else:
                results["plan_orders"] = []
        elif isinstance(plan_raw, list):
            results["plan_orders"] = [row for row in plan_raw if isinstance(row, dict)]
        else:
            results["plan_orders"] = []
    except Exception as exc:  # pragma: no cover - defensive debug guard
        results["plan_orders"] = {"error": str(exc)}

    return {
        "ok": True,
        "results": results,
    }

@app.get("/api/debug/find-btc-stop")
async def debug_find_btc_stop() -> Dict[str, object]:
    results: Dict[str, Any] = {}

    async def fetch_orders(path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        try:
            wrapper = await bitget._request("GET", path, params=params or {})
        except Exception as exc:  # pragma: no cover - defensive debug guard
            return [], str(exc)

        raw_payload: Any
        if isinstance(wrapper, dict) and "raw" in wrapper:
            raw_payload = wrapper.get("raw") if wrapper.get("raw") is not None else wrapper
        else:
            raw_payload = wrapper

        orders: List[Dict[str, Any]] = []
        if isinstance(raw_payload, dict):
            data_block = raw_payload.get("data")
            if isinstance(data_block, dict):
                entrusted = data_block.get("entrustedList")
                if isinstance(entrusted, list):
                    orders = [row for row in entrusted if isinstance(row, dict)]
                elif isinstance(data_block.get("list"), list):
                    orders = [row for row in data_block.get("list") if isinstance(row, dict)]
            elif isinstance(data_block, list):
                orders = [row for row in data_block if isinstance(row, dict)]
            elif isinstance(raw_payload.get("entrustedList"), list):
                orders = [row for row in raw_payload.get("entrustedList") if isinstance(row, dict)]
        elif isinstance(raw_payload, list):
            orders = [row for row in raw_payload if isinstance(row, dict)]

        return orders, None

    # Method 1: no params
    orders, error = await fetch_orders("/api/v2/mix/order/orders-plan-pending")
    if error:
        results["no_params"] = {"error": error}
    else:
        results["no_params"] = {
            "total": len(orders),
            "btc_orders": [o for o in orders if isinstance(o.get("symbol"), str) and "BTC" in o["symbol"].upper()],
        }

    # Method 2: marginCoin only
    orders_margin, error_margin = await fetch_orders(
        "/api/v2/mix/order/orders-plan-pending",
        {"marginCoin": "USDT"},
    )
    if error_margin:
        results["margin_only"] = {"error": error_margin}
    else:
        results["margin_only"] = {
            "total": len(orders_margin),
            "btc_orders": [o for o in orders_margin if isinstance(o.get("symbol"), str) and "BTC" in o["symbol"].upper()],
        }

    # Method 3: algo orders endpoint
    try:
        algo_wrapper = await bitget._request(
            "GET",
            "/api/v2/mix/order/orders-algo",
            params={"productType": "USDT-FUTURES", "symbol": "BTCUSDT"},
        )
        algo_raw = algo_wrapper.get("raw") if isinstance(algo_wrapper, dict) else algo_wrapper
        results["algo_orders"] = algo_raw.get("data") if isinstance(algo_raw, dict) else algo_raw
    except Exception as exc:  # pragma: no cover - defensive debug guard
        results["algo_orders"] = {"error": str(exc)}

    return {"ok": True, "results": results}

@app.get("/api/debug/plan-error-details")
async def debug_plan_error() -> Dict[str, object]:
    try:
        response = await bitget._request(
            "GET",
            "/api/v2/mix/order/orders-plan-pending",
            params={"productType": "USDT-FUTURES"},
        )
        raw_payload = response.get("raw") if isinstance(response, dict) else response
        return {
            "ok": True,
            "response": raw_payload,
        }
    except Exception as exc:  # pragma: no cover - defensive debug guard
        error_str = str(exc)
        return {
            "ok": False,
            "error": error_str,
            "hint": "Check for missing parameters or authentication issues.",
        }

@app.get("/api/debug/raw-error")
async def debug_raw_error() -> Dict[str, object]:
    params = {"productType": "USDT-FUTURES"}
    try:
        payload = await bitget._request(
            "GET",
            "/api/v2/mix/order/orders-plan-pending",
            params=params,
        )
        raw_payload = payload.get("raw") if isinstance(payload, dict) else payload
        return {
            "ok": True,
            "status": 200,
            "response": raw_payload,
        }
    except httpx.HTTPStatusError as exc:
        response = exc.response
        status = response.status_code if response else None
        text = response.text if response else None
        json_body: Optional[Any]
        try:
            json_body = response.json() if response is not None else None
        except Exception:
            json_body = None
        return {
            "ok": False,
            "status": status,
            "text": text,
            "json": json_body,
        }
    except Exception as exc:  # pragma: no cover - defensive debug guard
        return {"ok": False, "error": str(exc)}

@app.get("/api/debug/plan-simple")
async def debug_plan_simple() -> Dict[str, object]:
    param_sets: List[Dict[str, Any]] = [
        {},
        {"symbol": "BTCUSDT"},
        {"marginCoin": "USDT"},
        {"symbol": "BTCUSDT", "marginCoin": "USDT"},
    ]

    async def signed_get_without_product_type(path: str, params: Dict[str, Any]) -> Any:
        if not bitget._settings.has_api_credentials():  # type: ignore[attr-defined]
            raise RuntimeError("Bitget API credentials are not configured.")

        request_params = dict(params)
        query = str(httpx.QueryParams(request_params)) if request_params else ""
        path_with_query = f"{path}?{query}" if query else path
        timestamp = str(int(time.time() * 1000))
        sign_target = f"{timestamp}GET{path_with_query}"
        secret = bitget._settings.bitget_api_secret  # type: ignore[attr-defined]
        key = bitget._settings.bitget_api_key  # type: ignore[attr-defined]
        passphrase = bitget._settings.bitget_passphrase  # type: ignore[attr-defined]
        signature = base64.b64encode(
            hmac.new(secret.encode(), sign_target.encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            "ACCESS-KEY": key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": passphrase,
        }
        client = bitget._select_client(authenticated=True, use_demo=False)  # type: ignore[attr-defined]
        response = await client.request(
            "GET",
            path,
            params=request_params or None,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()

    attempts: List[Dict[str, Any]] = []
    for params in param_sets:
        try:
            payload = await signed_get_without_product_type(
                "/api/v2/mix/order/orders-plan-pending",
                params,
            )
            attempts.append(
                {
                    "params": params,
                    "success": True,
                    "data": payload,
                }
            )
        except Exception as exc:
            attempts.append(
                {
                    "params": params,
                    "error": str(exc)[:200],
                }
            )

    return {"ok": True, "attempts": attempts}

@app.get("/api/debug/working-endpoints")
async def debug_working_endpoints() -> Dict[str, object]:
    targets: List[Tuple[str, Dict[str, Any]]] = [
        ("/api/v2/mix/order/orders-pending", {"productType": "USDT-FUTURES", "symbol": "BTCUSDT"}),
        ("/api/v2/mix/order/detail", {"productType": "USDT-FUTURES", "symbol": "BTCUSDT"}),
        ("/api/v2/mix/order/current", {"productType": "USDT-FUTURES"}),
        ("/api/v2/mix/order/history", {"productType": "USDT-FUTURES"}),
        ("/api/v2/mix/order/fills", {"productType": "USDT-FUTURES"}),
    ]

    results: List[Dict[str, Any]] = []

    for endpoint, params in targets:
        try:
            wrapper = await bitget._request("GET", endpoint, params=params)
            raw_payload = wrapper.get("raw") if isinstance(wrapper, dict) else wrapper
            has_data = False
            if isinstance(raw_payload, dict):
                data_block = raw_payload.get("data")
                has_data = bool(data_block)
            elif isinstance(raw_payload, list):
                has_data = bool(raw_payload)
            results.append(
                {
                    "endpoint": endpoint,
                    "params": params,
                    "works": True,
                    "hasData": has_data,
                }
            )
        except Exception as exc:  # pragma: no cover - defensive debug guard
            text = str(exc)
            if "400" in text:
                error_code = "400"
            elif "404" in text:
                error_code = "404"
            else:
                error_code = "other"
            results.append(
                {
                    "endpoint": endpoint,
                    "params": params,
                    "works": False,
                    "error": error_code,
                }
            )

    return {"ok": True, "results": results}

@app.get("/api/debug/try-plan-variants")
async def debug_try_plan_variants() -> Dict[str, object]:
    paths = [
        "/api/v2/mix/order/plan-orders-pending",
        "/api/v2/mix/order/pending-plan-orders",
        "/api/v2/mix/order/trigger-pending",
        "/api/v2/mix/plan/pending-orders",
        "/api/v2/mix/order/stop-pending",
        "/api/mix/v2/order/orders-plan-pending",
    ]

    results: List[Dict[str, Any]] = []
    for path in paths:
        try:
            await bitget._request(
                "GET",
                path,
                params={"productType": "USDT-FUTURES"},
            )
            results.append({"path": path, "success": True})
        except Exception as exc:  # pragma: no cover - defensive debug guard
            text = str(exc)
            if "404" in text:
                error_type = "404"
            elif "400" in text:
                error_type = "400"
            else:
                error_type = "other"
            results.append({"path": path, "error": error_type})

    return {"ok": True, "results": results}

@app.get("/api/debug/working-structure/{species}")
async def debug_working_structure(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    payload = await bitget_client.fetch_working_orders_v2()
    if isinstance(payload, dict) and "error" in payload and len(payload) == 2:
        return {"ok": False, "detail": payload.get("error")}

    entries = _collect_order_entries(payload)
    normalized_symbol = symbol.upper().split("_", 1)[0]
    filtered = [
        item
        for item in entries
        if isinstance(item.get("symbol"), str)
        and item.get("symbol").upper().split("_", 1)[0] == normalized_symbol
    ]

    count = len(filtered)
    pos_side_counts = {"long": 0, "short": 0, "unknown": 0}
    for item in filtered:
        side = str(item.get("posSide") or item.get("positionSide") or "").lower()
        if side in {"long", "short"}:
            pos_side_counts[side] += 1
        else:
            pos_side_counts["unknown"] += 1

    field_list = [
        "size",
        "orderType",
        "posSide",
        "status",
        "presetStopLossPrice",
        "presetStopLossExecutePrice",
        "presetStopLossTriggerType",
        "presetStopSurplusPrice",
        "presetStopSurplusExecutePrice",
        "presetStopSurplusTriggerType",
    ]

    sample = filtered[0] if filtered else None
    keys = list(sample.keys()) if isinstance(sample, dict) else []
    field_presence = {
        field: bool(sample.get(field)) if isinstance(sample, dict) else False
        for field in field_list
    }

    def _extract_sample(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        fields = {
            "symbol": item.get("symbol"),
            "size": item.get("size"),
            "orderType": item.get("orderType"),
            "posSide": item.get("posSide") or item.get("positionSide"),
            "status": item.get("status"),
            "price": item.get("price"),
            "clientOid": item.get("clientOid"),
            "orderId": item.get("orderId"),
            "leverage": item.get("leverage"),
            "marginMode": item.get("marginMode"),
            "presetStopLossPrice": item.get("presetStopLossPrice"),
            "presetStopLossExecutePrice": item.get("presetStopLossExecutePrice"),
            "presetStopLossTriggerType": item.get("presetStopLossTriggerType"),
            "presetStopSurplusPrice": item.get("presetStopSurplusPrice"),
            "presetStopSurplusExecutePrice": item.get("presetStopSurplusExecutePrice"),
            "presetStopSurplusTriggerType": item.get("presetStopSurplusTriggerType"),
        }
        return fields

    sample_fields = _extract_sample(sample)

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": symbol,
        "count": count,
        "byPosSide": pos_side_counts,
        "keys": keys,
        "fieldPresence": field_presence,
        "sample": sample_fields,
    }

def _summarize_raw_pending(symbol: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    top_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    data = payload.get("data") if isinstance(payload, dict) else None
    data_type = "none"
    if isinstance(data, dict):
        data_type = "dict"
    elif isinstance(data, list):
        data_type = "list"

    entries: List[Dict[str, Any]] = []
    list_name = "none"
    if isinstance(data, dict):
        for candidate in ("entrustedList", "orderInfoList", "list"):
            block = data.get(candidate)
            if isinstance(block, list):
                list_name = candidate
                entries = [item for item in block if isinstance(item, dict)]
                break
    if not entries:
        if isinstance(data, list):
            list_name = "data"
            entries = [item for item in data if isinstance(item, dict)]
        elif isinstance(payload, list):
            list_name = "root"
            entries = [item for item in payload if isinstance(item, dict)]

    normalized_symbol = symbol.upper().split("_", 1)[0]
    entries = [
        item
        for item in entries
        if isinstance(item.get("symbol"), str)
        and item.get("symbol").upper().split("_", 1)[0] == normalized_symbol
    ]

    first = entries[0] if entries else None
    first_keys = list(first.keys()) if isinstance(first, dict) else []
    trimmed_first: Optional[Dict[str, Any]] = None
    if isinstance(first, dict):
        trimmed_first = {k: first.get(k) for k in list(first.keys())[:15]}

    return {
        "topKeys": top_keys,
        "dataType": data_type,
        "listName": list_name,
        "listLen": len(entries),
        "firstKeys": first_keys,
        "firstRow": trimmed_first,
    }

@app.get("/api/debug/working-raw/{species}")
async def debug_working_raw(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    params_with_symbol = {
        "productType": "USDT-FUTURES",
        "symbol": symbol,
        "pageSize": 100,
        "pageNo": 1,
    }
    params_no_symbol = {
        "productType": "USDT-FUTURES",
        "pageSize": 100,
        "pageNo": 1,
    }

    with_symbol = await bitget_client._mix_orders_pending_v2(params_with_symbol)
    no_symbol = await bitget_client._mix_orders_pending_v2(params_no_symbol)

    if isinstance(with_symbol, dict) and "error" in with_symbol and len(with_symbol) == 2:
        return {"ok": False, "detail": with_symbol.get("error")}

    summary_with = _summarize_raw_pending(symbol, with_symbol if isinstance(with_symbol, dict) else {})
    summary_with["params"] = params_with_symbol

    summary_no = _summarize_raw_pending(symbol, no_symbol if isinstance(no_symbol, dict) else {})
    summary_no["params"] = params_no_symbol

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": symbol,
        "withSymbol": summary_with,
        "noSymbol": summary_no,
    }

def _summarize_entries(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    sample = entries[0] if entries else None
    keys = list(sample.keys()) if isinstance(sample, dict) else []
    return {
        "count": len(entries),
        "keys": keys,
        "sample": sample,
    }

@app.get("/api/debug/working-probe/{species}")
async def debug_working_probe(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    base = species_meta.get("base")
    if not symbol or not base:
        return {"ok": False, "detail": "Symbol or base not available for requested species."}

    normalized_symbol = symbol.upper()
    symbol_umcbl = f"{normalized_symbol}_UMCBL"

    attempts = [
        {
            "label": "v2+symbol",
            "path": "/api/v2/mix/order/orders-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "symbol": normalized_symbol,
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "v2+symbol+margin",
            "path": "/api/v2/mix/order/orders-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "symbol": normalized_symbol,
                "marginCoin": "USDT",
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "v2+umcbl",
            "path": "/api/v2/mix/order/orders-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "symbol": symbol_umcbl,
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "v2+umcbl+margin",
            "path": "/api/v2/mix/order/orders-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "symbol": symbol_umcbl,
                "marginCoin": "USDT",
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "v2+noSymbol",
            "path": "/api/v2/mix/order/orders-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "v1+symbol",
            "path": "/api/mix/v1/order/orders-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "symbol": normalized_symbol,
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "v1+umcbl",
            "path": "/api/mix/v1/order/orders-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "symbol": symbol_umcbl,
                "pageSize": 100,
                "pageNo": 1,
            },
        },
        {
            "label": "v1+noSymbol",
            "path": "/api/mix/v1/order/orders-pending",
            "params": {
                "productType": "USDT-FUTURES",
                "pageSize": 100,
                "pageNo": 1,
            },
        },
    ]

    probe_results = await bitget_client._mix_orders_pending_v2_probe(attempts)

    normalized_base = normalized_symbol.split("_", 1)[0]
    for result in probe_results:
        list_len = result.get("listLen")
        if result.get("label") == "v2+noSymbol" and list_len:
            payload = await bitget_client.fetch_working_orders_v2()
            entries = bitget_client._parse_mix_entries(payload)
            filtered = [
                item
                for item in entries
                if isinstance(item.get("symbol"), str)
                and item.get("symbol").upper().split("_", 1)[0] in {normalized_base, symbol_umcbl}
            ]
            result["filteredLen"] = len(filtered)
            first = filtered[0] if filtered else None
            if isinstance(first, dict):
                keys = list(first.keys())[:15]
                result["filteredFirstKeys"] = keys
                result["filteredFirstRow"] = {k: first.get(k) for k in keys}

    return {
        "ok": True,
        "species": resolved_species,
        "symbolBase": normalized_symbol,
        "symbolUmcbl": symbol_umcbl,
        "attempts": probe_results,
    }

@app.get("/api/debug/orders-probe/{species}")
async def debug_orders_probe(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    try:
        attempts = await bitget_client.probe_working_orders(symbol)
    except Exception as exc:  # pragma: no cover - defensive debug guard
        logger.debug("Orders probe failed: %s", exc)
        return {"ok": False, "detail": str(exc)}

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": symbol.upper(),
        "attempts": attempts,
    }

@app.get("/api/debug/plan-tpsl/{species}")
async def debug_plan_tpsl(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    plan_types: List[Optional[str]] = [None, "normal_plan", "profit_plan", "loss_plan", "pos_profit", "pos_loss"]
    found: Dict[str, Dict[str, Any]] = {}

    for plan_type in plan_types:
        entries = await bitget_client.list_symbol_plan_orders_safe(symbol, plan_type=plan_type)
        label = "plan-pending" if plan_type is None else f"plan-pending:{plan_type}"
        found[label] = _summarize_entries(entries)

    tpsl_entries = await bitget_client.list_symbol_tpsl_orders_safe(symbol)
    found["tpsl-open-orders"] = _summarize_entries(tpsl_entries)

    tpsl_v1_entries = await bitget_client.list_symbol_tpsl_orders_safe_v1(symbol)
    found["v1:tpsl-open-orders"] = _summarize_entries(tpsl_v1_entries)

    return {
        "ok": True,
        "symbol": symbol,
        "species": resolved_species,
        "found": found,
    }

@app.get("/api/debug/perp-accounts")
async def debug_perp_accounts() -> Dict[str, object]:
    try:
        raw = await bitget_client.get_perp_accounts_raw()
        data = raw.get("data") or raw.get("data_list") or raw.get("data_obj") if isinstance(raw, dict) else []
        if isinstance(data, dict):
            data_iterable = [data]
        elif isinstance(data, list):
            data_iterable = data
        else:
            data_iterable = []

        rows: List[Dict[str, object]] = []
        for entry in data_iterable:
            if not isinstance(entry, dict):
                continue
            rows.append(
                {
                    "marginCoin": entry.get("marginCoin"),
                    "available": entry.get("available"),
                    "crossMaxAvailable": entry.get("crossMaxAvailable"),
                    "availableBalance": entry.get("availableBalance"),
                    "availableEq": entry.get("availableEq"),
                    "marginAvailable": entry.get("marginAvailable"),
                    "usdtEquity": entry.get("usdtEquity"),
                    "equity": entry.get("equity"),
                    "crossedMarginLocked": entry.get("crossedMarginLocked"),
                    "openOrderMargin": entry.get("openOrderMargin"),
                }
            )

        return {"ok": True, "rows": rows, "raw": raw}
    except Exception as exc:  # pragma: no cover - debug safety
        import traceback

        return JSONResponse(
            status_code=200,
            content={"ok": False, "error": str(exc), "trace": traceback.format_exc()},
        )

@app.get("/api/party")
async def party_positions(demo: bool = False) -> Dict[str, Any]:
    is_demo = bool(demo)

    try:
        party, raw = await order_service.fetch_party_positions(
            demo_mode=is_demo,
            suppress_errors=False,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=branded_detail(exc.response.text),
        )
    except Exception as exc:  # pragma: no cover - network/credential guard
        raise HTTPException(status_code=502, detail=branded_detail(str(exc))) from exc

    return {"party": party, "raw": raw}

@app.get("/api/trainer/status", response_model=TrainerStatus)
async def trainer_status(demo: bool = False) -> TrainerStatus:
    is_demo = bool(demo or settings.adventure_demo_mode)
    party_info = await order_service.list_party_status(demo_mode=is_demo)
    party = party_info.get("party", [])
    link_shell = party_info.get("linkShell")
    guardrails = party_info.get("guardrails") or order_service.guardrails()
    energy_info = party_info.get("energy") or {}
    position_mode = party_info.get("positionMode") or order_service.position_mode()

    try:
        balances = await bitget_client.get_usdtm_energy()
    except Exception as exc:  # pragma: no cover - safety
        logger.warning("HP balance fetch failed: %s", exc)
        balances = {"available": 0.0, "total": 0.0, "source": "none"}

    available_raw = balances.get("available")
    total_raw = balances.get("total")
    src = balances.get("source", "none")

    available_known = False
    total_known = False

    available_hp = 0.0
    if available_raw is not None:
        try:
            candidate = float(available_raw)
            if math.isfinite(candidate):
                available_hp = candidate
                available_known = True
        except (TypeError, ValueError):
            available_hp = 0.0

    total_hp = 0.0
    if total_raw is not None:
        try:
            candidate_total = float(total_raw)
            if math.isfinite(candidate_total):
                total_hp = candidate_total
                total_known = True
        except (TypeError, ValueError):
            total_hp = 0.0

    direct_available = None
    if not available_known:
        try:
            direct_available = await bitget_client.get_perp_available_usdt()
        except Exception as exc:  # pragma: no cover - diagnostic safety
            logger.debug("HP available probe failed: %s", exc)

        if direct_available is not None:
            try:
                numeric_available = float(direct_available)
                if math.isfinite(numeric_available):
                    available_hp = numeric_available
                    available_known = True
            except (TypeError, ValueError):
                pass

    if total_hp < 0:
        total_hp = 0.0
    if available_known and available_hp < 0:
        available_hp = 0.0
    if total_hp > 0 and available_known and available_hp > total_hp:
        available_hp = total_hp

    fill_ratio = (available_hp / total_hp) if total_hp > 0 else 0.0
    fill_ratio = max(0.0, min(1.0, fill_ratio))
    if total_hp > 0 and not available_known:
        try:
            fallback_fill = float(energy_info.get("fill", fill_ratio))
        except (TypeError, ValueError):
            fallback_fill = fill_ratio
        if math.isfinite(fallback_fill):
            fill_ratio = max(0.0, min(1.0, fallback_fill))

    if not total_known:
        total_hp = 0.0

    available_display = round(available_hp, 2) if available_known else None
    energy_payload = {
        "present": bool(total_hp > 0),
        "fill": fill_ratio,
        "source": "perp",
        "unit": "USDT",
        "value": total_hp,
        "displayLabel": "HP",
        "showNumbers": energy_info.get("showNumbers", True),
        "available": available_display,
        "total": round(total_hp, 2),
        "label": (
            f"{available_hp:,.2f}/{total_hp:,.2f}" if total_hp > 0 and available_known else
            f"--/{total_hp:,.2f}" if total_hp > 0 else
            "--/--"
        ),
    }

    if not link_shell:
        link_shell = "online" if energy_payload["present"] else "offline"

    logger.info(
        "Energy HP → available=%.2f total=%.2f (src=%s)",
        round(available_hp, 2),
        energy_payload["total"],
        src,
    )

    core_energy = {
        "present": energy_payload["present"],
        "fill": energy_payload["fill"],
        "source": energy_payload["source"],
        "unit": energy_payload["unit"],
        "value": energy_payload["value"],
    }

    if guardrails is not None:
        try:
            guardrails = guardrails.model_copy(update={"cooldown_seconds": settings.cooldown_seconds})
        except AttributeError:
            try:
                guardrails.cooldown_seconds = settings.cooldown_seconds  # type: ignore[attr-defined]
            except AttributeError:
                pass

    trainer = TrainerStatus(
        trainer_name="Trainer Gold",
        badges=["Zephyr Badge", "Hive Badge"],
        party=party,
        pokedollar_balance=0.0,
        energy=core_energy,
        guardrails=guardrails,
        demo_mode=is_demo,
        position_mode=position_mode,
        link_shell=link_shell,
    )

    payload = trainer.model_dump(mode="json")
    payload["energy"] = energy_payload
    return JSONResponse(payload)

@app.post("/api/adventure/encounter", response_model=AdventureOrderReceipt)
async def adventure_encounter(order: EncounterOrder) -> AdventureOrderReceipt:
    try:
        # Convert UI's legacy stop_loss field to proper backend format for embedded stop-loss
        if order.stop_loss and order.stop_loss > 0:
            if order.stop_loss_mode is None and order.stop_loss_value is None:
                try:
                    # UI sends stop_loss: 107200, convert to backend format
                    if hasattr(order, 'model_copy'):
                        order = order.model_copy(update={
                            'stop_loss_mode': StopLossMode.PRICE,
                            'stop_loss_value': order.stop_loss,
                            'stop_loss_trigger': TriggerSource.MARK
                        })
                    else:
                        # Fallback for older pydantic
                        order = order.copy(update={
                            'stop_loss_mode': StopLossMode.PRICE,
                            'stop_loss_value': order.stop_loss,
                            'stop_loss_trigger': TriggerSource.MARK
                        })
                except Exception:
                    # If conversion fails, let the order proceed without conversion
                    pass

        return await order_service.execute_encounter(order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=branded_detail(str(exc)))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=branded_detail(exc.response.text),
        )

@app.get("/api/adventure/journal")
def adventure_journal():
    return [entry.dict() for entry in order_service.list_recent_events()]

def _strip_symbol_suffix(symbol: str) -> str:
    core = symbol.split("_", 1)[0]
    return core.upper()

def _collect_order_entries(payload: Any) -> List[Dict[str, Any]]:
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

def _extract_entrusted_entries(payload: Any) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            entrusted = data.get("entrustedList")
            if isinstance(entrusted, list):
                entries.extend([item for item in entrusted if isinstance(item, dict)])
            elif isinstance(data.get("list"), list):
                entries.extend([item for item in data.get("list") if isinstance(item, dict)])
        elif isinstance(data, list):
            entries.extend([item for item in data if isinstance(item, dict)])
        elif isinstance(payload.get("entrustedList"), list):
            entries.extend([item for item in payload.get("entrustedList") if isinstance(item, dict)])
        elif isinstance(payload.get("list"), list):
            entries.extend([item for item in payload.get("list") if isinstance(item, dict)])
    elif isinstance(payload, list):
        entries.extend([item for item in payload if isinstance(item, dict)])
    return entries

async def _compute_open_orders_summary() -> Dict[str, bool]:
    species_catalog = roster_service.species_mapping()
    summary: Dict[str, bool] = {species: False for species in species_catalog.keys()}
    symbol_map = roster_service.symbol_mapping()

    try:
        pending = await bitget_client.fetch_working_orders_v2()
    except Exception as exc:
        logger.debug("Pending orders fetch failed: %s", exc)
        pending = {}

    for entry in _extract_entrusted_entries(pending):
        symbol_field = entry.get("symbol") or entry.get("symbolName") or entry.get("symbolId")
        if not isinstance(symbol_field, str) or not symbol_field:
            continue
        normalized_symbol = _strip_symbol_suffix(symbol_field.upper())
        species = symbol_map.get(normalized_symbol)
        if not species:
            continue
        summary[species] = True

    return summary

def _cached_open_orders_summary() -> Dict[str, Any]:
    payload = _open_orders_cache.get("payload")
    ts = _open_orders_cache.get("ts")
    if isinstance(payload, dict) and isinstance(ts, str):
        return {"payload": payload, "ts": ts}
    return {"payload": None, "ts": ""}

def _update_open_orders_cache(payload: Dict[str, bool], ts_iso: str) -> None:
    _open_orders_cache["payload"] = payload
    _open_orders_cache["ts"] = ts_iso
    _open_orders_cache["expires"] = time.time() + OPEN_ORDERS_TTL_SECONDS

@app.get("/api/adventure/orders/open")
async def adventure_open_orders(demo: bool = False) -> Dict[str, object]:
    summary = await order_service.list_open_orders_by_species(demo_mode=bool(demo))
    return {"ok": True, "orders": summary}

@app.get("/api/adventure/open-orders-summary")
async def adventure_open_orders_summary() -> Dict[str, object]:
    now = time.time()
    expires = _open_orders_cache.get("expires", 0.0)
    cached = _cached_open_orders_summary()
    if now < float(expires or 0.0) and cached["payload"]:
        return {"ok": True, "bySpecies": cached["payload"], "ts": cached["ts"]}

    summary = await _compute_open_orders_summary()
    timestamp = datetime.now(timezone.utc).isoformat()
    _update_open_orders_cache(summary, timestamp)

    return {"ok": True, "bySpecies": summary, "ts": timestamp}

@app.post("/api/adventure/orders/cancel-all/{species}")
async def cancel_all_orders(species: str, request: Request) -> Dict[str, Any]:
    client_ip = request.client.host if request.client else "unknown"
    logger.debug("cancel-all request", extra={"species": species, "ip": client_ip})

    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    try:
        result = await order_service.cancel_all_orders_for_species(resolved_species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    if result.get("ok"):
        return {"ok": True, **result}

    error_msg = result.get("error")
    if not error_msg:
        failed = result.get("failed")
        if isinstance(failed, list) and failed:
            msgs = [item.get("msg") for item in failed if isinstance(item, dict) and item.get("msg")]
            error_msg = msgs[0] if msgs else "Cancel request failed"
        else:
            error_msg = "Cancel request failed"

    return {"ok": False, **result, "error": error_msg}

@app.get("/api/debug/client-info")
async def debug_client_info() -> Dict[str, object]:
    return {
        "client_type": type(bitget_client).__name__,
        "has_post": hasattr(bitget_client, "cancel_all_orders_by_symbol"),
        "has_request": hasattr(bitget, "_request"),
        "has_private_request": hasattr(bitget, "_request"),
        "available_methods": sorted(
            name for name in dir(bitget_client) if not name.startswith("__")
        ),
    }

@app.post("/api/debug/raw-cancel")
async def debug_raw_cancel() -> Dict[str, Any]:
    api_key = settings.bitget_api_key
    api_secret = settings.bitget_api_secret
    passphrase = settings.bitget_passphrase
    if not all([api_key, api_secret, passphrase]):
        return {"ok": False, "detail": "Bitget API credentials are not configured."}

    timestamp = str(int(time.time() * 1000))
    request_path = "/api/v2/mix/order/cancel-all-orders"
    method = "POST"
    body_dict = {"productType": "USDT-FUTURES", "symbol": "BTCUSDT"}
    body = json.dumps(body_dict, separators=(",", ":"))
    sign_target = f"{timestamp}{method}{request_path}{body}"
    signature = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), sign_target.encode("utf-8"), hashlib.sha256).digest()
    ).decode()

    headers = {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as raw_client:
        try:
            response = await raw_client.post(
                f"{settings.bitget_base_url.rstrip('/')}{request_path}",
                headers=headers,
                content=body,
                timeout=10.0,
            )
        except Exception as exc:  # pragma: no cover - network guard
            return {"ok": False, "detail": str(exc)}

    try:
        parsed = response.json()
    except ValueError:
        parsed = None

    return {
        "ok": response.status_code == 200,
        "status": response.status_code,
        "json": parsed,
        "text": response.text if parsed is None else None,
    }

@app.post("/api/debug/test-cancel-raw")
async def test_cancel_raw() -> Dict[str, Any]:
    api_key = settings.bitget_api_key
    api_secret = getattr(settings, "bitget_api_secret", None) or getattr(settings, "bitget_secret_key", None)
    passphrase = settings.bitget_passphrase
    if not all([api_key, api_secret, passphrase]):
        return {"ok": False, "detail": "Bitget API credentials are not configured."}

    timestamp = str(int(time.time() * 1000))
    path = "/api/v2/mix/order/cancel-all-orders"
    body = json.dumps({"productType": "USDT-FUTURES", "symbol": "BTCUSDT"}, separators=(",", ":"))
    prehash = f"{timestamp}POST{path}{body}"
    signature = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode()

    headers = {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "locale": "en-US",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(
                f"https://api.bitget.com{path}",
                content=body,
                headers=headers,
            )
        except Exception as exc:  # pragma: no cover - defensive debug guard
            return {"ok": False, "error": str(exc), "type": type(exc).__name__}

    try:
        response_json = response.json()
    except ValueError:
        response_json = None

    return {
        "ok": response.status_code == 200,
        "status": response.status_code,
        "headers_sent": headers,
        "body_sent": body,
        "response_text": response.text,
        "response_json": response_json,
    }

@app.post("/api/adventure/orders/create")
async def debug_create_order(order_data: Dict[str, Any]) -> Dict[str, Any]:
    logger.debug("UI order create debug", extra={"payload": order_data})

    # Check all possible stop-loss field variations
    stop_loss_fields = {
        'stop_loss': order_data.get("stop_loss"),
        'stopLoss': order_data.get("stopLoss"),
        'sl': order_data.get("sl"),
        'stop_price': order_data.get("stop_price"),
        'stopPrice': order_data.get("stopPrice")
    }
    logger.debug("Stop-loss field values", extra={"fields": stop_loss_fields})

    try:
        resolved_species = roster_service.resolve_species(str(order_data.get("species", "")))
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    side = "buy" if str(order_data.get("side", "buy")).lower() != "sell" else "sell"
    pos_side = "long" if side == "buy" else "short"

    bitget_order = {
        "productType": "USDT-FUTURES",
        "symbol": symbol,
        "side": side,
        "orderType": str(order_data.get("orderType", "limit")),
        "price": str(order_data.get("price")),
        "size": str(order_data.get("size")),
        "marginCoin": "USDT",
        "posSide": pos_side,
    }

    stop_loss = (
        order_data.get("stopLoss")
        or order_data.get("stop_loss")
        or order_data.get("sl")
    )
    if stop_loss not in (None, ""):
        bitget_order["presetStopLossPrice"] = str(stop_loss)
        bitget_order["presetStopLossTriggerPrice"] = str(stop_loss)
        bitget_order["presetStopLossTriggerType"] = "mark_price"
        bitget_order["presetStopLossExecutePrice"] = str(stop_loss)
        logger.debug("Embedded stop-loss added", extra={"stop_loss": stop_loss})
    else:
        logger.debug("No stop-loss detected for debug order")

    try:
        response = await bitget.post("/api/v2/mix/order/place-order", bitget_order)
    except Exception as exc:  # pragma: no cover - defensive debug guard
        return {"ok": False, "error": str(exc)}

    logger.debug("Bitget order response", extra={"response": response})
    return response

@app.get("/api/debug/orderflow/tap")
async def debug_orderflow_tap() -> Dict[str, object]:
    recent = bitget_client.get_recent_order_tap()
    return {"ok": True, "recent": recent}



@app.post("/api/debug/orderflow/build")
async def debug_orderflow_build(payload: Dict[str, Any]) -> Dict[str, object]:
    try:
        preview = await order_service.build_order_preview(payload)
    except Exception as exc:  # pragma: no cover - defensive debug guard
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, **preview}


@app.post("/api/debug/orderflow/diff")
async def debug_orderflow_diff(payload: Dict[str, Any]) -> Dict[str, object]:
    try:
        main_preview = await order_service.build_order_preview(payload)
    except Exception as exc:  # pragma: no cover - defensive debug guard
        return {"ok": False, "detail": f"main builder failed: {exc}"}

    main_payload = dict(main_preview.get("payload", {}))
    symbol = main_payload.get("symbol")
    if not symbol:
        return {"ok": False, "detail": "Main builder did not produce a symbol."}

    string_fields = {
        "price",
        "size",
        "presetStopLossPrice",
        "presetStopLossTriggerPrice",
        "presetStopLossExecutePrice",
        "leverage",
    }

    def stringify(key: str, value: Any) -> Any:
        if value is None:
            return value
        return str(value) if key in string_fields else value

    test_payload: Dict[str, Any] = {}
    for key in (
        "productType",
        "symbol",
        "side",
        "orderType",
        "price",
        "size",
        "marginCoin",
        "marginMode",
        "posSide",
        "holdSide",
        "timeInForceValue",
        "clientOid",
        "tradeSide",
        "presetStopLossPrice",
        "presetStopLossTriggerPrice",
        "presetStopLossTriggerType",
        "presetStopLossExecutePrice",
        "leverage",
    ):
        if key in main_payload:
            test_payload[key] = stringify(key, main_payload.get(key))

    only_in_main = sorted(set(main_payload.keys()) - set(test_payload.keys()))
    only_in_test = sorted(set(test_payload.keys()) - set(main_payload.keys()))
    different = []
    for key in sorted(set(main_payload.keys()) & set(test_payload.keys())):
        if str(main_payload.get(key)) != str(test_payload.get(key)):
            different.append(
                {
                    "key": key,
                    "main": main_payload.get(key),
                    "test": test_payload.get(key),
                }
            )

    return {
        "ok": True,
        "embedSL": main_preview.get("embedSL"),
        "positionMode": main_preview.get("positionMode"),
        "reasons": main_preview.get("reasons", []),
        "onlyInMain": only_in_main,
        "onlyInTest": only_in_test,
        "different": different,
        "mainPayload": main_payload,
        "testPayload": test_payload,
    }


@app.post("/api/debug/test-order-with-sl")
async def debug_test_order_with_sl() -> Dict[str, Any]:
    params = {
        "productType": "USDT-FUTURES",
        "symbol": "BTCUSDT",
        "side": "buy",
        "orderType": "limit",
        "price": "107000",
        "size": "0.0029",
        "marginCoin": "USDT",
        "marginMode": settings.adventure_margin_mode,
        "posSide": "long",
        "holdSide": "long",
        "timeInForceValue": "normal",
        "clientOid": str(uuid.uuid4()),
        "tradeSide": "open",
        "presetStopLossPrice": "105930",
        "presetStopLossTriggerPrice": "105930",
        "presetStopLossTriggerType": "mark_price",
        "presetStopLossExecutePrice": "105930",
        "leverage": "1",
    }
    logger.debug("test-order-with-sl params", extra={"params": params})
    try:
        response = await bitget.post("/api/v2/mix/order/place-order", params)
        return {"ok": True, "params_sent": params, "response": response}
    except httpx.HTTPStatusError as exc:  # pragma: no cover - defensive debug guard
        body = exc.response.text if exc.response else str(exc)
        return {
            "ok": False,
            "status": exc.response.status_code if exc.response else None,
            "error": body,
            "params_sent": params,
        }
    except Exception as exc:  # pragma: no cover - defensive debug guard
        return {"ok": False, "error": str(exc), "params_sent": params}

@app.post("/api/adventure/place-order-with-embedded-sl")
async def place_order_with_embedded_sl(
    species: str = "bitcoin",
    side: str = "buy",
    price: float = 107000,
    size: float = 0.0029,
    stop_loss_price: float = 105930,
    leverage: int = 2,  # Force >= 2 for perp routing
    demo_mode: bool = True
) -> Dict[str, Any]:
    """Simplified production endpoint that forces embedded stop-loss like test-order-with-sl"""
    logger.debug("place-order-with-embedded-sl invoked", extra={
        "species": species,
        "side": side,
        "price": price,
        "size": size,
        "stop_loss_price": stop_loss_price,
        "leverage": leverage,
        "demo_mode": demo_mode,
    })

    try:
        # Force enable embedded stop-loss
        original_embed_setting = order_service._settings.adventure_embed_sl
        order_service._settings.adventure_embed_sl = True

        try:
            # Create order with forced level >= 2 for perp routing
            order = EncounterOrder(
                species=species,
                action=BattleAction.CATCH,
                order_style=OrderStyle.LIMIT,
                pokeball_strength=size,
                limit_price=price,
                stop_loss_value=stop_loss_price,
                stop_loss_mode=StopLossMode.PRICE,
                stop_loss_trigger=TriggerSource.MARK,
                level=max(2, leverage),  # Ensure >= 2 for perp routing
                demo_mode=demo_mode
            )

            # Execute through the production order service
            receipt = await order_service.execute_encounter(order)

            return {
                "ok": True,
                "adventure_id": receipt.adventure_id,
                "species": receipt.species,
                "filled": receipt.filled,
                "fill_price": receipt.fill_price,
                "stop_loss_reference": receipt.stop_loss_reference,
                "narration": receipt.narration,
                "embedded_sl": receipt.stop_loss_reference is not None,
                "raw_response": receipt.raw_response
            }

        finally:
            # Restore original setting
            order_service._settings.adventure_embed_sl = original_embed_setting

    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Unexpected error: {str(exc)}"}

@app.post("/api/adventure/place-order-with-sl")
async def place_order_with_sl(
    species: str = "bitcoin",
    side: str = "buy",
    price: float = 107000,
    size: float = 0.0029,
    stop_loss_price: float = 105930,
    leverage: int = 1,
    demo_mode: bool = False
) -> Dict[str, Any]:
    """Production endpoint for placing orders with embedded stop-loss,
    replicating the successful debug test-order-with-sl functionality."""

    try:
        # Create an EncounterOrder that matches the debug parameters
        order = EncounterOrder(
            species=species,
            action=BattleAction.CATCH,
            order_style=OrderStyle.LIMIT,
            pokeball_strength=size,
            limit_price=price,
            stop_loss_value=stop_loss_price,
            stop_loss_mode=StopLossMode.PRICE,
            stop_loss_trigger=TriggerSource.MARK,
            level=leverage,
            demo_mode=demo_mode
        )

        # Execute through the production order service
        receipt = await order_service.execute_encounter(order)

        return {
            "ok": True,
            "adventure_id": receipt.adventure_id,
            "species": receipt.species,
            "filled": receipt.filled,
            "fill_price": receipt.fill_price,
            "stop_loss_reference": receipt.stop_loss_reference,
            "narration": receipt.narration,
            "raw_response": receipt.raw_response
        }

    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Unexpected error: {str(exc)}"}

@app.post("/api/debug/compare-order-processing")
async def debug_compare_order_processing(order: EncounterOrder) -> Dict[str, Any]:
    """Debug endpoint to see exactly how an order gets processed vs debug test"""

    try:
        # Step 1: Show the original order
        original_order = {
            "species": order.species,
            "action": order.action.value if order.action else None,
            "order_style": order.order_style.value if order.order_style else None,
            "pokeball_strength": order.pokeball_strength,
            "limit_price": order.limit_price,
            "stop_loss_mode": order.stop_loss_mode.value if order.stop_loss_mode else None,
            "stop_loss_value": order.stop_loss_value,
            "stop_loss_trigger": order.stop_loss_trigger.value if order.stop_loss_trigger else None,
            "level": order.level,
            "demo_mode": order.demo_mode
        }

        # Step 2: Get the translation
        prep = order_service._translator.to_exchange_payload(order)

        # Step 3: Check embed conditions
        embed_conditions = {
            "prep_route": prep.route,
            "adventure_embed_sl_setting": order_service._settings.adventure_embed_sl,
            "has_stop_loss_mode": order.stop_loss_mode is not None,
            "has_stop_loss_value": order.stop_loss_value is not None,
            "requires_stop_loss": order_service._requires_stop_loss(order, prep),
        }

        embed_allowed = (
            prep.route == "perp"
            and order_service._settings.adventure_embed_sl
            and order.stop_loss_mode is not None
            and order.stop_loss_value is not None
        )

        # Step 4: Show what would be in the payload
        payload_before_embed = dict(prep.payload)

        # Step 5: Apply contract meta and position mode (like in real processing)
        order_processed = await order_service._prepare_order(order)
        meta_symbol = prep.profile.perp_symbol if (prep.route == "perp" and prep.profile) else None
        meta = await order_service._get_contract_meta(meta_symbol)
        adjustments = {}
        order_processed = await order_service._apply_contract_meta(order_processed, prep, adjustments, meta_override=meta)

        position_mode = None
        if prep.route == "perp":
            position_mode = await order_service._resolve_position_mode()
        order_service._apply_position_mode(prep, position_mode, order_processed)

        # Step 6: Show final payload
        payload_after_processing = dict(prep.payload)

        # Step 7: Check if embed would happen
        would_embed = False
        if embed_allowed and order_service._requires_stop_loss(order_processed, prep):
            would_embed = True
            # Simulate the embed
            try:
                await order_service._embed_stop_loss(order_processed, prep, adjustments, demo_mode=True)
                payload_with_embed = dict(prep.payload)
            except Exception as e:
                payload_with_embed = {"error": str(e)}
        else:
            payload_with_embed = payload_after_processing

        return {
            "ok": True,
            "original_order": original_order,
            "embed_conditions": embed_conditions,
            "embed_allowed": embed_allowed,
            "would_embed": would_embed,
            "route": prep.route,
            "payload_before_embed": payload_before_embed,
            "payload_after_processing": payload_after_processing,
            "payload_with_embed": payload_with_embed,
            "debug_test_expected": {
                "presetStopLossPrice": "105930",
                "presetStopLossTriggerPrice": "105930",
                "presetStopLossTriggerType": "mark_price",
                "presetStopLossExecutePrice": "105930"
            }
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}

@app.get("/api/debug/test-embed-debug")
async def test_embed_debug() -> Dict[str, Any]:
    """Test if server has the embed debugging code"""
    return {
        "ok": True,
        "message": "Server has embedded stop-loss debugging active",
        "check_server_console": "You should see 🚀 and 🎯 messages in server console"
    }

@app.get("/api/debug/server-updated")
async def debug_server_updated() -> Dict[str, Any]:
    """Check if server has the updated code"""
    import datetime
    return {
        "ok": True,
        "message": "Server has updated code with debug logging",
        "timestamp": str(datetime.datetime.now()),
        "embed_sl_setting": order_service._settings.adventure_embed_sl
    }

@app.get("/api/debug/routing-test")
async def debug_routing_test() -> Dict[str, Any]:
    """Test if routing logic works for stop-loss orders"""

    # Create the same order as your successful debug test
    test_order = EncounterOrder(
        species="bitcoin",
        action=BattleAction.CATCH,
        order_style=OrderStyle.LIMIT,
        pokeball_strength=0.0029,
        limit_price=107000,
        stop_loss_value=105930,
        stop_loss_mode=StopLossMode.PRICE,
        stop_loss_trigger=TriggerSource.MARK,
        level=1,  # This should now route to perp because of stop-loss
        demo_mode=True
    )

    # Test the routing
    prep = order_service._translator.to_exchange_payload(test_order)

    return {
        "ok": True,
        "order_level": test_order.level,
        "has_stop_loss": test_order.stop_loss_mode is not None and test_order.stop_loss_value is not None,
        "routed_to": prep.route,
        "should_be_perp": True,
        "routing_fixed": prep.route == "perp",
        "embed_sl_setting": order_service._settings.adventure_embed_sl,
        "payload_keys": list(prep.payload.keys()),
        "debug_comparison": {
            "debug_test_works": "level=1 with stop-loss goes to perp",
            "main_ui_issue": "probably not sending stop_loss_mode/stop_loss_value correctly"
        }
    }

@app.get("/api/debug/embed-sl-check")
async def debug_embed_sl_check() -> Dict[str, Any]:
    """Debug endpoint to check why embedded stop-loss might not be working"""

    # Check settings
    embed_sl_setting = settings.adventure_embed_sl

    # Test with the same parameters as your successful debug test
    test_order = EncounterOrder(
        species="bitcoin",
        action=BattleAction.CATCH,
        order_style=OrderStyle.LIMIT,
        pokeball_strength=0.0029,
        limit_price=107000,
        stop_loss_value=105930,
        stop_loss_mode=StopLossMode.PRICE,
        stop_loss_trigger=TriggerSource.MARK,
        level=1,
        demo_mode=True
    )

    # Get the translation without executing
    prep = order_service._translator.to_exchange_payload(test_order)

    # Check embed conditions
    embed_conditions = {
        "prep_route_is_perp": prep.route == "perp",
        "adventure_embed_sl_setting": embed_sl_setting,
        "has_stop_loss_mode": test_order.stop_loss_mode is not None,
        "has_stop_loss_value": test_order.stop_loss_value is not None,
    }

    embed_allowed = (
        prep.route == "perp"
        and embed_sl_setting
        and test_order.stop_loss_mode is not None
        and test_order.stop_loss_value is not None
    )

    return {
        "ok": True,
        "embed_sl_setting": embed_sl_setting,
        "embed_conditions": embed_conditions,
        "embed_allowed": embed_allowed,
        "prep_route": prep.route,
        "prep_payload_keys": list(prep.payload.keys()),
        "test_order_dict": {
            "species": test_order.species,
            "stop_loss_mode": test_order.stop_loss_mode.value if test_order.stop_loss_mode else None,
            "stop_loss_value": test_order.stop_loss_value,
            "level": test_order.level,
        }
    }

@app.get("/api/debug/plan-error-detail")
async def plan_error_detail() -> Dict[str, Any]:
    api_key = settings.bitget_api_key
    api_secret = getattr(settings, "bitget_api_secret", None) or getattr(settings, "bitget_secret_key", None)
    passphrase = settings.bitget_passphrase
    if not all([api_key, api_secret, passphrase]):
        return {"ok": False, "detail": "Bitget API credentials are not configured."}

    timestamp = str(int(time.time() * 1000))
    path = "/api/v2/mix/order/orders-plan-pending"
    params = {"productType": "USDT-FUTURES"}
    query = "&".join(f"{k}={v}" for k, v in params.items())
    full_path = f"{path}?{query}" if query else path
    sign_target = f"{timestamp}GET{full_path}"
    signature = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), sign_target.encode("utf-8"), hashlib.sha256).digest()
    ).decode()

    headers = {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"https://api.bitget.com{full_path}", headers=headers)

    try:
        response_json = response.json()
    except ValueError:
        response_json = None

    return {
        "ok": response.status_code == 200,
        "status": response.status_code,
        "response": response_json if response_json is not None else response.text,
    }

@app.get("/api/debug/tpsl-safe/{species}")
async def debug_tpsl_safe(species: str) -> Dict[str, object]:
    try:
        resolved_species = roster_service.resolve_species(species)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    species_meta = roster_service.species_mapping().get(resolved_species, {})
    symbol = species_meta.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return {"ok": False, "detail": "Symbol not available for requested species."}

    normalized_symbol = symbol.upper()

    try:
        rows_tpsl = await bitget.list_symbol_tpsl_orders_safe(normalized_symbol)
    except Exception as exc:  # pragma: no cover - defensive debug guard
        return {"ok": False, "detail": f"TPSL fetch failed: {exc}"}

    rows_tpsl_v1: List[Dict[str, Any]] = []
    if not rows_tpsl:
        try:
            rows_tpsl_v1 = await bitget.list_symbol_tpsl_orders_safe_v1(normalized_symbol)
        except Exception as exc:  # pragma: no cover - defensive debug guard
            return {"ok": False, "detail": f"TPSL v1 fetch failed: {exc}"}
    else:
        try:
            rows_tpsl_v1 = await bitget.list_symbol_tpsl_orders_safe_v1(normalized_symbol)
        except Exception:
            rows_tpsl_v1 = []

    try:
        rows_plan = await bitget.list_symbol_plan_orders_safe(normalized_symbol)
    except Exception as exc:  # pragma: no cover - defensive debug guard
        return {"ok": False, "detail": f"Plan fetch failed: {exc}"}

    def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        first = rows[0] if rows else None
        keys = list(first.keys())[:15] if isinstance(first, dict) else []
        return {"count": len(rows), "firstKeys": keys}

    sample = {
        "tpsl": rows_tpsl[0] if rows_tpsl else (rows_tpsl_v1[0] if rows_tpsl_v1 else None),
        "plan": rows_plan[0] if rows_plan else None,
    }

    return {
        "ok": True,
        "species": resolved_species,
        "symbol": normalized_symbol,
        "tpsl": summarize(rows_tpsl),
        "tpsl_v1": summarize(rows_tpsl_v1),
        "plan": summarize(rows_plan),
        "sample": sample,
    }

@app.get("/api/debug/find-orphan-sl")
async def debug_find_orphan_sl() -> Dict[str, object]:
    results: Dict[str, Any] = {}

    async def fetch(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            payload = await bitget._request("GET", path, params=params or {})
            raw = payload.get("raw") if isinstance(payload, dict) else payload
            return {"ok": True, "raw": raw}
        except Exception as exc:  # pragma: no cover - defensive debug guard
            return {"ok": False, "error": str(exc)[:100]}

    results["no_filter"] = await fetch("/api/v2/mix/order/orders-plan-pending")
    results["symbol_only"] = await fetch(
        "/api/v2/mix/order/orders-plan-pending",
        {"symbol": "BTCUSDT"},
    )
    results["lowercase"] = await fetch(
        "/api/v2/mix/order/orders-plan-pending",
        {"productType": "usdt-futures"},
    )

    history = await fetch(
        "/api/v2/mix/order/orders-history",
        {"productType": "USDT-FUTURES", "symbol": "BTCUSDT"},
    )
    if history.get("ok"):
        raw = history.get("raw")
        data_block = raw.get("data") if isinstance(raw, dict) else None
        entrusted = []
        if isinstance(data_block, dict):
            entry_list = data_block.get("entrustedList")
            if isinstance(entry_list, list):
                entrusted = [item for item in entry_list if isinstance(item, dict)]
        history = {
            "ok": True,
            "count": len(entrusted),
            "sample": entrusted[:2],
        }
    results["history"] = history

    return {"ok": True, "results": results}

@app.post("/api/debug/force-cancel-plan")
async def debug_force_cancel_plan() -> Dict[str, object]:
    bodies = [
        {"productType": "USDT-FUTURES", "symbol": "BTCUSDT"},
        {"symbol": "BTCUSDT"},
        {"productType": "USDT-FUTURES"},
        {"marginCoin": "USDT", "symbol": "BTCUSDT"},
    ]

    attempts: List[Dict[str, Any]] = []
    for body in bodies:
        try:
            response = await bitget.post("/api/v2/mix/order/cancel-all-plan-orders", body)
            attempts.append({"body": body, "success": True, "response": response})
        except Exception as exc:  # pragma: no cover - defensive debug guard
            attempts.append({"body": body, "error": str(exc)[:100]})

    return {"ok": True, "attempts": attempts}

def _frontend_index() -> FileResponse:
    if not public_dir.exists():
        raise HTTPException(status_code=404, detail="Playground is still under construction.")
    index_path = public_dir / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_path)

@app.get("/playground", include_in_schema=False)
async def playground_alias() -> FileResponse:
    return _frontend_index()

@app.get("/playground/", include_in_schema=False)
async def playground_alias_slash() -> FileResponse:
    return _frontend_index()

if public_dir.exists():
    app.mount("/", StaticFiles(directory=public_dir, html=True), name="frontend")
