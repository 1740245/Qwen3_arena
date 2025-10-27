# Johto Adventure Desk

Pokemon Gold-inspired adventure wrapper for Bitget. The backend exposes a FastAPI service that speaks to Bitget, while the front-end is a Game Boy-styled console for placing orders without seeing intimidating tickers.

## Project structure

- `backend/` – FastAPI application, Bitget client wrapper, translator, and adventure service.
- `frontend/public/` – Static GBA-inspired UI served at `/playground` when the backend is running.
- `pyproject.toml` – Python dependencies (`fastapi`, `httpx`, `uvicorn`, `pydantic-settings`).

## Setup

1. **Environment variables** – create a `.env` file in the project root:
   ```env
   BITGET_API_KEY=your_key
   BITGET_SECRET_KEY=your_secret
   BITGET_PASSPHRASE=your_passphrase
   # Gatekeeper lock
   # GATE_PHRASE=Silver Wing
   # SESSION_SECRET=generate_a_long_random_string
   # Optional overrides
   # BITGET_BASE_URL=https://api.bitget.com
   # PORTFOLIO_BASE_SPECIES=Rare Candy
   ```

2. **Install Python dependencies** (Python 3.10+):
   ```bash
   pip install -e .
   ```

3. **Run the backend**:
   ```bash
   uvicorn backend.app.main:app --reload
   ```

4. Visit `http://127.0.0.1:8000/playground` for the kid-friendly adventure UI.

Set `GATE_PHRASE` to the shared passphrase that unlocks the console and `SESSION_SECRET` to a long, random string used for signing the session cookie—both values must be present or logins will be blocked.

### Protecting your instance

The built-in gate is meant as a lightweight speed bump. Configure `GATE_PHRASE` with a secret phrase that only authorized trainers know, and set `SESSION_SECRET` to a long, randomly generated string so session cookies are tamper-resistant. For public-facing deployments, layer this behind a stronger perimeter such as Cloudflare Access, an OAuth proxy, or your hosting provider’s authentication controls—treat the gate as a second factor, not the primary fortress.

## Adventure concepts

- **Species** – Pokémon names stand in for symbols (e.g. `Dragonite = BTCUSDT`).
- **Throw Poké Ball** – place a buy order; **Release Pokémon** – place a sell order; **Use Potion** – reserves slot for hedging (mapped to `close`, expand as needed).
- **Poké Ball Strength** – order size, sized per-species precision.
- **Sparkle Price** – optional limit price when choosing a careful approach (`order_style = limit`).
- **Adventure Journal** – logs the most recent exchange responses, keeping the theme immersive.

## Extending the experience

- Add more species to `backend/app/services/translators.py` with their own emoji sprites, regions, and precision.
- Connect TradingView alerts to `/api/adventure/encounter` for automated challenges.
- Expand `AdventureOrderService` to differentiate spot vs futures products, add risk guards (maximum party size, potion reserves).
- Enhance front-end sprites by dropping actual pixel art into `frontend/public/assets/` and referencing them in the species roster.

## Safety notes

- API credentials are required for live trading; use Bitget demo keys for testing.
- The current build issues direct spot orders. Double-check translator mappings and limits before deploying.
- Logging currently keeps responses in-memory; wire this into durable storage if you need full audit trails.
