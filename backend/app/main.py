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

from .adapters.hyperliquid_client import HyperliquidClient
from .config import get_settings
from .schemas import AdventureOrderReceipt, EncounterOrder, RosterResponse, TrainerStatus
from .services.orders import AdventureOrderService
from .services.price_feed import PriceFeed
from .services.roster import PokemonRosterService
from .services.translators import PokemonTranslator, default_translator
from .utils.branding import sanitize_vendor_terms

settings = get_settings()

# Initialize Hyperliquid client
hyperliquid_client = HyperliquidClient(settings)

# Initialize services with Hyperliquid client
translator: PokemonTranslator = default_translator(settings.adventure_margin_mode)
price_feed = PriceFeed(hyperliquid_client, settings.pinned_perp_bases)
order_service = AdventureOrderService(hyperliquid_client, translator, settings, price_feed)
roster_service = PokemonRosterService(
    hyperliquid_client,
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
    await hyperliquid_client.close()

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

# ====================================================================================
# Frontend routes
# ====================================================================================

@app.get("/playground", include_in_schema=False)
async def playground_alias() -> FileResponse:
    return _frontend_index()

@app.get("/playground/", include_in_schema=False)
async def playground_alias_slash() -> FileResponse:
    return _frontend_index()

if public_dir.exists():
    app.mount("/", StaticFiles(directory=public_dir, html=True), name="frontend")
