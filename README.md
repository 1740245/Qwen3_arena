# Qwen3 Arena Trading System

A real-time cryptocurrency perpetual futures trading interface with AI-powered market insights. Built on **Hyperliquid DEX**, featuring automated price updates every 5 minutes, stop-loss management, and a sleek AI-themed UI.

## Features

- **Real-time Price Tracking**: Automatic Hyperliquid price updates every 5 minutes
- **10 Perpetual Markets**: BTC, ETH, SOL, XRP, DOGE, HYPE, AVAX, SUI, BNB, WLD
- **AI Market Insights**: Real-time market analysis and price monitoring
- **Position Management**: Open, close, and manage perpetual positions
- **Stop Loss Automation**: Automated position protection with price or percentage triggers
- **Trade History**: Complete logging of all Hyperliquid trading activities
- **Countdown Timer**: Visual display showing time until next price update
- **Leverage Trading**: Up to 50x leverage on all supported markets

## Project Structure

- `backend/` – FastAPI application with Hyperliquid client integration
- `frontend/public/` – Modern AI-themed trading UI served at `/playground`
- `pyproject.toml` – Python dependencies (`fastapi`, `hyperliquid-python-sdk`, etc.)

## Setup

### 1. Create Hyperliquid API Wallet (Recommended)

**Why API Wallet?** API wallets can trade on your behalf but **cannot withdraw funds**, making them safer for automated trading.

**Steps:**
1. Visit https://app.hyperliquid.xyz/API
2. Click **"Generate API Wallet"**
3. Name it (e.g., "Qwen3 Arena Bot")
4. Choose expiration (up to 180 days)
5. **Save the private key securely** (shown only once!)
6. Click **"Authorize"** and sign the transaction
7. Copy your **main wallet address** (top-right corner)

### 2. Environment Variables

Create a `.env` file in the project root:

```env
# ========== HYPERLIQUID CREDENTIALS ==========
# Your main wallet address
HYPERLIQUID_WALLET_ADDRESS=0xYourMainWalletAddressHere

# API wallet private key (from step 1)
HYPERLIQUID_PRIVATE_KEY=0xYourAPIWalletPrivateKeyHere

# Use testnet for testing (default: false)
HYPERLIQUID_TESTNET=false

# ========== TRADING SETTINGS ==========
# Price refresh interval (5 minutes)
ADVENTURE_COOLDOWN_SECONDS=300

# Base currency display
PORTFOLIO_BASE_SPECIES=USDT

# Maximum positions
MAX_TEAM_SIZE=6

# Minimum balance reserve
ADVENTURE_MIN_QUOTE_RESERVE=25.0

# ========== ACCESS CONTROL (Optional) ==========
# Passphrase to unlock trading interface
GATE_PHRASE=your_secret_phrase

# Session secret for cookie signing
SESSION_SECRET=generate_a_long_random_string_here
```

**Security Notes:**
- ⚠️ Never commit your `.env` file to version control
- ✅ Use API wallets (cannot withdraw)
- ✅ Test on testnet first (`HYPERLIQUID_TESTNET=true`)
- ✅ Store private keys securely

### 3. Install Dependencies

**Requirements:** Python 3.10 or higher

```bash
# Install project dependencies
pip install -e .

# Or install manually
pip install fastapi uvicorn httpx pydantic pydantic-settings hyperliquid-python-sdk eth-account
```

### 4. Run the Backend

```bash
# Start the FastAPI server
uvicorn backend.app.main:app --reload
```

You should see:
```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Hyperliquid exchange client initialized for wallet: 0x1234567...
```

### 5. Access the Trading Interface

Open your browser to: **http://127.0.0.1:8000/playground**

If you set a `GATE_PHRASE`, you'll need to enter it first.

## Trading on Hyperliquid

### Key Concepts

- **Perpetual Futures Only** – Hyperliquid is a perps DEX (no spot trading)
- **Leverage** – All positions support up to 50x leverage
- **Margin Mode** – Cross margin (account-wide collateral)
- **Hedge Mode** – Can hold both long and short positions simultaneously
- **Stop Loss** – Automated position protection
- **No Gas Fees** – Trading on Hyperliquid's L1 (low fees)

### Trading Flow

1. **Select Asset** – Choose from 10 supported perpetual markets
2. **Choose Action** – OPEN (long/short) or CLOSE existing position
3. **Set Size** – Amount in USDT to trade
4. **Order Type** – Market (instant) or Limit (specific price)
5. **Stop Loss** – Optional protective exit (price or percentage)
6. **Confirm** – Execute trade on Hyperliquid

### Position Management

- View **Open Positions** panel for active trades
- Monitor **unrealized P&L** in real-time
- **Close positions** with one click
- **Modify stop-loss** on active positions

## Configuration

### Price Update Interval
The default refresh interval is 5 minutes (300 seconds). Adjust in `.env`:
```env
ADVENTURE_COOLDOWN_SECONDS=300  # 5 minutes
```

### Supported Markets

The system supports these 10 Hyperliquid perpetual markets:
- **BTC** – Bitcoin Perpetual
- **ETH** – Ethereum Perpetual
- **SOL** – Solana Perpetual
- **XRP** – Ripple Perpetual
- **DOGE** – Dogecoin Perpetual
- **HYPE** – Hyperliquid Token Perpetual
- **AVAX** – Avalanche Perpetual
- **SUI** – Sui Perpetual
- **BNB** – Binance Coin Perpetual
- **WLD** – Worldcoin Perpetual

**Note:** Hyperliquid uses native ticker symbols (e.g., "BTC" not "BTCUSDT"). The backend handles symbol translation automatically.

To modify the market list, edit `pinned_perp_bases` in `backend/app/config.py`.

## Extending the System

- **Add more markets** – Update `backend/app/services/translators.py` with Hyperliquid perpetuals
- **Connect TradingView alerts** – POST to `/api/adventure/encounter` for automated trading
- **Enhance risk management** – Extend `backend/app/services/orders.py` with custom logic
- **Customize AI insights** – Modify `frontend/public/main.js` (loadAIInsights function)
- **Add notifications** – Integrate Telegram/Discord bots for trade alerts
- **Implement strategies** – Build custom trading algorithms using the API

## Safety & Best Practices

### 🔒 Security

- **Use API Wallets** – Cannot withdraw funds (safer for bots)
- **Test on Testnet First** – Set `HYPERLIQUID_TESTNET=true`
- **Never Share Private Keys** – Store in `.env`, never in code
- **One API Wallet Per Bot** – Prevents nonce conflicts
- **Monitor Your Account** – Check positions regularly at https://app.hyperliquid.xyz

### ⚠️ Risk Management

- **Start Small** – Test with minimal position sizes
- **Use Stop Losses** – Protect against adverse price movements
- **Set Reserve Balance** – Keep minimum USDT (`ADVENTURE_MIN_QUOTE_RESERVE`)
- **Leverage Carefully** – Higher leverage = higher risk
- **Monitor Liquidation Prices** – Avoid forced position closures

### 📊 Trading Tips

- 5-minute refresh interval prevents over-trading
- AI insights provide market context
- Countdown timer shows next price update
- Trade history tracks all activity
- Demo mode available for testing logic

## API Endpoints

- `GET /api/atlas/roster` - Get current asset roster with live Hyperliquid prices
- `POST /api/atlas/refresh` - Force refresh asset prices from Hyperliquid
- `GET /api/atlas/species` - Get all supported perpetual markets
- `GET /api/atlas/prices` - Get latest price snapshot
- `POST /api/adventure/encounter` - Execute trade order on Hyperliquid
- `GET /api/adventure/journal` - Get trade execution history
- `GET /api/adventure/open-orders-summary` - Get open orders summary
- `GET /api/trainer/status` - Get Hyperliquid account status and balance

## Development

```bash
# Backend development
cd backend
pip install -e ".[dev]"
uvicorn backend.app.main:app --reload

# Frontend development
# Frontend is static files served by the backend
# Edit files in frontend/public/ directory
```

## Hyperliquid Resources

- **Trading Interface**: https://app.hyperliquid.xyz
- **API Documentation**: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
- **Python SDK**: https://github.com/hyperliquid-dex/hyperliquid-python-sdk
- **Testnet**: https://app.hyperliquid-testnet.xyz

## Troubleshooting

### "Hyperliquid credentials are not configured"
- Check `.env` file exists and has correct wallet address and private key
- Ensure both values start with `0x`
- Restart the backend after updating `.env`

### "Nonce error" or "Transaction failed"
- Only use one API wallet per trading bot
- Multiple bots with same wallet cause nonce conflicts
- Create separate API wallets for each bot instance

### Positions not showing
- Verify wallet address matches your Hyperliquid account
- Check you're on correct network (mainnet vs testnet)
- Ensure you have open positions on Hyperliquid

### Price updates not working
- Check Hyperliquid API status
- Verify network connectivity
- Look for errors in backend logs

---

**Built with ❤️ for the Hyperliquid ecosystem**
