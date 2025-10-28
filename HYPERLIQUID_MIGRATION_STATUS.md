# Hyperliquid Migration Status

## Critical Issues Identified

The code evaluation revealed **3 HIGH severity blockers** that need immediate attention before the Hyperliquid integration can work:

### ❌ BLOCKER 1: Debug Endpoints Reference Non-Existent `bitget` Client

**Problem:** 50+ debug endpoints in `main.py` still reference `bitget` and `bitget_client` which no longer exist after switching to HyperliquidClient.

**Impact:**
- App crashes on shutdown (`await bitget.close()`)
- All `/api/debug/*` endpoints throw `NameError`
- Large portions of API are dead

**Recommended Solution:**
Since debug endpoints are **Bitget-specific** and not needed for core trading:
- **Disable all `/api/debug/*` endpoints** (comment them out)
- Keep only core trading endpoints: `/api/atlas/*`, `/api/adventure/*`, `/api/trainer/*`
- Create new Hyperliquid-specific debug endpoints if needed later

**Alternative:** Revert entire codebase back to BitgetClient (not recommended)

---

### ❌ BLOCKER 2: Symbol Format Mismatch

**Problem:** Hyper liquid uses `BTC`, `ETH`, `SOL` while code expects `BTCUSDT`, `ETHUSDT`, `SOLUSDT`

**Impact:**
- Price feed gets zero quotes from Hyperliquid
- Orders fail with "symbol not found"
- Translator maps wrong symbols

**Files Affected:**
- `backend/app/services/translators.py` (lines 244-366) - hardcoded "USDT" suffix
- `backend/app/services/price_feed.py` (lines 112-272) - expects Bitget field names
- `backend/app/adapters/hyperliquid_client.py` - needs to normalize symbols

**Required Fix:**
1. Update translator to use Hyperliquid symbol format (`BTC` not `BTCUSDT`)
2. Update price feed to parse Hyperliquid ticker format
3. Add symbol translation layer in Hyperliquid client

---

### ❌ BLOCKER 3: AdventureOrderService Uses Bitget-Specific Methods

**Problem:** Order service calls methods like:
- `list_open_spot_orders()` - doesn't exist on HyperliquidClient
- `get_mix_orders_pending()` - Bitget-specific
- `cancel_mix_order()` - Bitget-specific

**Impact:**
- Open order summaries crash
- Cancel operations fail
- Core trading broken

**Files Affected:**
- `backend/app/services/orders.py` (lines 501-605)

**Required Fix:**
- Implement missing methods in HyperliquidClient
- OR update AdventureOrderService to use available Hyperliquid methods

---

## ⚠️ RISK: Blocking Async Event Loop

**Problem:** Hyperliquid SDK uses **synchronous** calls but we're calling them in async coroutines

**Impact:** Event loop blocks, hurting latency under load

**Files Affected:**
- `backend/app/adapters/hyperliquid_client.py` (all methods)

**Fix:** Wrap sync calls in `asyncio.to_thread()`:
```python
# BEFORE
result = self._exchange.order(...)

# AFTER
result = await asyncio.to_thread(self._exchange.order, ...)
```

---

## Recommended Migration Path

### Option A: Complete Hyperliquid Port (Recommended)

**Pros:** Modern DEX, no gas fees, better for users
**Cons:** More work upfront

**Steps:**
1. ✅ ~~Install hyperliquid-python-sdk~~ (done)
2. ✅ ~~Create HyperliquidClient adapter~~ (done)
3. ❌ **FIX SYMBOL FORMAT** - align translator with Hyperliquid
4. ❌ **FIX PRICE FEED** - parse Hyperliquid ticker format
5. ❌ **DISABLE DEBUG ENDPOINTS** - remove Bitget-specific code
6. ❌ **WRAP SYNC CALLS** - use asyncio.to_thread
7. ❌ **TEST END-TO-END** - verify trading works

**Estimated Time:** 2-4 hours

### Option B: Revert to BitgetClient

**Pros:** Fastest path to working state
**Cons:** Keeps old exchange, user wanted Hyperliquid

**Steps:**
1. Revert `main.py` changes
2. Use `BitgetClient` instead of `HyperliquidClient`
3. Keep existing code as-is

**Estimated Time:** 15 minutes

---

## Decision Required

**Which path should we take?**
1. Complete Hyperliquid migration (fix all blockers)
2. Revert to Bitget (user didn't want this)
3. Hybrid approach (support both exchanges)

**My Recommendation:** Option 1 - Complete the Hyperliquid migration properly. The core client is already built, we just need to fix symbol formats and disable Bitget debug endpoints.

---

## Next Steps (If Proceeding with Hyperliquid)

1. **Disable all debug endpoints** (comment out lines 418-3000+ in main.py)
2. **Fix translator symbol format** (remove "USDT" suffix, use "BTC", "ETH", etc.)
3. **Fix price feed parsing** (handle Hyperliquid ticker format)
4. **Wrap sync SDK calls** in asyncio.to_thread
5. **Test basic flow:** login → view prices → place order → close position

Would you like me to proceed with completing the Hyperliquid migration?
