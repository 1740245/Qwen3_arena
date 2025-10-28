const API_BASE = '/api';

const speciesSelect = document.getElementById('species-select');
const actionButtons = Array.from(document.querySelectorAll('[data-action]'));
const actionInput = document.getElementById('action-input');
const levelSlider = document.getElementById('level-slider');
const levelIndicator = document.getElementById('level-indicator');
const strengthInput = document.getElementById('strength-input');
const presetButtons = Array.from(document.querySelectorAll('.size-presets .size-btn'));
const orderStyleSelect = document.getElementById('order-style-select');
const priceInput = document.getElementById('price-input');
const encounterForm = document.getElementById('encounter-form');
const rosterGrid = document.getElementById('roster-grid');
const refreshRosterBtn = document.getElementById('refresh-roster');
const trainerName = document.getElementById('trainer-name');
const energyFill = document.getElementById('energy-fill');
const energyCaption = document.getElementById('energy-caption');
const sizeHelper = document.getElementById('size-helper');
const cooldownChip = document.getElementById('cooldown-chip');
const journalFeed = document.getElementById('journal-feed');
const toastLayer = document.getElementById('toast-layer');
const stopLossGroup = document.getElementById('stop-loss-group');
const stopLossInput = document.getElementById('stop-loss-input');
const stopLossModeButtons = Array.from(document.querySelectorAll('.mode-btn'));
const stopLossTriggerSelect = document.getElementById('stop-loss-trigger');
const stopLossHint = document.getElementById('stop-loss-hint');
const stopLossScale = document.getElementById('stop-loss-scale');
const confirmButton = encounterForm.querySelector('.btn-confirm');
const priceInputs = Array.from(document.querySelectorAll('[data-role="price-input"]'));
const priceInputPlaceholders = new Map(
  priceInputs.map((input) => [input, input.getAttribute('placeholder') || ''])
);
const strengthPlaceholder = strengthInput ? strengthInput.getAttribute('placeholder') || '' : '';

let speciesDex = {};
let rosterSlots = [];
let partyMembers = [];
let guardrails = {};
let selectedPreset = 'hp50';
let presetActive = true;
let quoteHp = 50;
let demoMode = false;
let stopLossMode = 'price';
let priceRefreshTimer = null;
let stopLossDirty = false;
let latestPriceSnapshot = { healthy: false, ts: 0 };
let latestPriceMap = new Map();
let linkShellToastShown = false;
let positionMode = null;
let openOrderSpecies = new Set();
let nextUpdateTime = null;
let countdownIntervalId = null;

const REFRESH_INTERVAL = 300000; // 5 minutes in milliseconds
const COUNTDOWN_INTERVAL = 1000; // Update countdown every second

const LOCKED_LEVEL = 20;

lockEncounterLevel(LOCKED_LEVEL);

const defaultTrainerStatus = {
  trainer_name: 'Trader',
  party: [],
  guardrails: {
    cooldown_remaining: 0,
    cooldown_seconds: 0,
    max_party_size: 6,
    minimum_energy: 0,
  },
  energy: {
    present: false,
    fill: 0,
    source: 'none',
    unit: 'USDT',
    value: null,
    showNumbers: true,
  },
  demo_mode: false,
  linkShell: 'offline',
  positionMode: null,
};

function startCountdownTimer() {
  nextUpdateTime = Date.now() + REFRESH_INTERVAL;

  if (countdownIntervalId) {
    clearInterval(countdownIntervalId);
  }

  countdownIntervalId = setInterval(() => {
    const now = Date.now();
    const remaining = Math.max(0, nextUpdateTime - now);
    const minutes = Math.floor(remaining / 60000);
    const seconds = Math.floor((remaining % 60000) / 1000);

    const cooldownChip = document.getElementById('cooldown-chip');
    if (cooldownChip) {
      if (remaining > 0) {
        cooldownChip.textContent = `Next Update: ${minutes}:${seconds.toString().padStart(2, '0')}`;
      } else {
        cooldownChip.textContent = 'Updating...';
      }
    }
  }, COUNTDOWN_INTERVAL);
}

if (stopLossInput) {
  stopLossInput.placeholder = 'Set Price';
  stopLossInput.step = '0.01';
}

if (presetButtons.length > 0) {
  setTimeout(() => setPreset(presetButtons[0]), 0);
}

function sanitizeNumberString(value) {
  if (value === null || value === undefined) {
    return '';
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    return String(value);
  }
  return String(value).replace(/[\s,]/g, '');
}

function parseInputNumber(value) {
  const raw = sanitizeNumberString(value);
  if (!raw) {
    return NaN;
  }
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : NaN;
}

async function fetchJSON(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const contentType = response.headers.get('content-type') || '';
    let detail = 'Request failed';
    if (contentType.includes('application/json')) {
      const data = await response.json().catch(() => ({}));
      if (typeof data === 'string') {
        detail = data;
      } else if (data && typeof data === 'object') {
        detail = data.detail || data.message || detail;
      }
    } else {
      detail = (await response.text()) || detail;
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

async function cancelAllOrders(species) {
  const endpoint = `${API_BASE}/adventure/orders/cancel-all/${encodeURIComponent(species)}`;
  const response = await fetch(endpoint, { method: 'POST' });
  if (!response.ok) {
    let detail = 'Cancel request failed';
    try {
      const payload = await response.json();
      if (payload && typeof payload === 'object') {
        detail = payload.detail || payload.message || detail;
      }
    } catch (error) {
      const text = await response.text().catch(() => '');
      if (text) {
        detail = text;
      }
    }
    const err = new Error(detail);
    err.status = response.status;
    throw err;
  }
  const result = await response.json();
  if (result && typeof result.cancelled_count === 'number') {
    console.log(`Cancelled ${result.cancelled_count} orders for ${species}`);
  } else if (result && Array.isArray(result.cancelled)) {
    console.log(`Cancelled ${result.cancelled.length} orders for ${species}`);
  }
  return result;
}

function toast(message) {
  const note = document.createElement('div');
  note.className = 'toast';
  note.textContent = message;
  toastLayer.appendChild(note);
  setTimeout(() => note.remove(), 3000);
}

function emojiForSprite(sprite) {
  const sprites = {
    btc: '₿',
    eth: 'Ξ',
    sol: '◎',
    xrp: '✕',
    doge: '🐕',
    hype: '🌊',
    avax: '🔺',
    sui: '💧',
    bnb: '🟡',
    wld: '🌍'
  };
  return sprites[sprite] || '❔';
}

function renderSpeciesOptions(data) {
  const previous = speciesSelect.value;
  speciesSelect.innerHTML = '';
  Object.entries(data)
    .sort((a, b) => a[0].localeCompare(b[0]))
    .forEach(([species, info]) => {
      const option = document.createElement('option');
      option.value = species;
      option.textContent = `${species} · ${info.element}`;
      option.dataset.levelCaps = JSON.stringify(info.level_caps || {});
      speciesSelect.appendChild(option);
    });
  if (previous && data[previous]) {
    speciesSelect.value = previous;
  }
  updateLevelBounds();
}

function renderRoster(roster) {
  rosterGrid.innerHTML = '';
  const partyMap = new Map(partyMembers.map((member) => [member.species, member]));
  const selectedSpecies = speciesSelect.value;

  roster
    .filter(
      (slot) =>
        slot &&
        slot.status === 'occupied' &&
        typeof slot.species === 'string' &&
        slot.species &&
        slot.species !== '???'
    )
    .forEach((slot) => {
      const card = document.createElement('article');
      card.className = 'roster-slot occupied';
      if (slot.species === selectedSpecies) {
        card.classList.add('selected');
      }

      const sprite = document.createElement('div');
      sprite.className = 'sprite';
      sprite.textContent = emojiForSprite(slot.sprite);

      const title = document.createElement('h3');
      title.textContent = slot.species;

      const typeLine = document.createElement('p');
      typeLine.className = 'type-line';
      typeLine.textContent = slot.element || '';

      const priceWrapper = document.createElement('div');
      priceWrapper.className = 'price-wrapper';
      const priceLine = document.createElement('span');
      priceLine.className = 'price-line';
      if (slot.price_usd !== undefined && slot.price_usd !== null && Number.isFinite(Number(slot.price_usd))) {
        priceLine.textContent = `${formatPriceValue(slot.price_usd, slot.species)} KG`;
      } else {
        priceLine.textContent = '—';
        priceLine.classList.add('placeholder');
      }
      if (slot.price_source) {
        priceLine.dataset.source = slot.price_source;
      }
      priceWrapper.appendChild(priceLine);

      card.appendChild(sprite);
      card.appendChild(title);
      if (slot.element) {
        card.appendChild(typeLine);
      }
      card.appendChild(priceWrapper);

      const partyEntry = partyMap.get(slot.species);
      if (partyEntry) {
        const hpBar = document.createElement('div');
        hpBar.className = 'hp-bar';
        const hpFill = document.createElement('span');
        const hpPercent = Math.min(100, Math.max(5, Math.round((partyEntry.hp ?? 0) * 100)));
        hpFill.style.setProperty('--hp-percent', hpPercent);
        hpBar.appendChild(hpFill);
        hpBar.title = `Energy buddy reserve ${partyEntry.hp ?? 0}`;

        card.appendChild(hpBar);
      }

      const hasOpenOrders = openOrderSpecies.has(slot.species);

      // Always add order status element to maintain consistent card height
      const orderStatus = document.createElement('div');
      orderStatus.className = 'order-status';
      orderStatus.textContent = hasOpenOrders ? 'BATTLE AWAITS!' : '\u00A0';
      card.appendChild(orderStatus);

      if (hasOpenOrders) {
        const actions = document.createElement('div');
        actions.className = 'order-actions';

        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'order-cancel';
        cancelBtn.textContent = '❌';
        cancelBtn.setAttribute('aria-label', `Cancel all open orders for ${slot.species}`);

        cancelBtn.addEventListener('click', async (event) => {
          event.stopPropagation();
          cancelBtn.disabled = true;
          try {
            const response = await cancelAllOrders(slot.species);
            if (response?.ok) {
              const cancelledCount = typeof response.cancelled === 'number' ? response.cancelled : 0;
              const message =
                response.message ||
                `System: Cancelled ${cancelledCount} orders for ${slot.species}.`;
              toast(message);
              openOrderSpecies.delete(slot.species);
              renderRoster(rosterSlots);
            } else {
              throw new Error(
                (response && response.message) || "System: couldn't cancel orders."
              );
            }
          } catch (error) {
            console.error(error);
            const message =
              (error && typeof error.message === 'string' && error.message) ||
              "System: couldn't cancel orders.";
            toast(message);
          } finally {
            cancelBtn.disabled = false;
          }
        });

        actions.appendChild(cancelBtn);
        card.appendChild(actions);
      }

      card.addEventListener('click', () => {
        speciesSelect.value = slot.species;
        updateLevelBounds();
        renderRoster(rosterSlots);
      });

      rosterGrid.appendChild(card);
    });

  lockEncounterLevel(LOCKED_LEVEL);
}

function currentMarkPrice() {
  const species = speciesSelect.value;
  if (!species) {
    return null;
  }
  const base = speciesToBaseToken(species);
  if (!base) {
    const slot = rosterSlots.find((item) => item.species === species);
    return slot && Number.isFinite(Number(slot?.price_usd)) ? Number(slot.price_usd) : null;
  }
  const quote = latestPriceMap.get(String(base).toUpperCase());
  if (quote && Number.isFinite(Number(quote.price))) {
    return Number(quote.price);
  }
  const slot = rosterSlots.find((item) => item.base_token === base);
  return slot && Number.isFinite(Number(slot?.price_usd)) ? Number(slot.price_usd) : null;
}

function computeStopLossAnchor(rawValue) {
  if (!Number.isFinite(rawValue)) {
    return null;
  }
  if (stopLossMode === 'percent') {
    const baseFromLimit = currentLimitPrice();
    const reference = Number.isFinite(baseFromLimit) ? baseFromLimit : currentMarkPrice();
    if (!Number.isFinite(reference) || reference <= 0) {
      return null;
    }
    const ratio = rawValue / 100;
    const adjustment = reference * ratio;
    const computed = isLongEntry() ? reference - adjustment : reference + adjustment;
    return Number.isFinite(computed) && computed > 0 ? computed : null;
  }
  return rawValue > 0 ? rawValue : null;
}

function syncStrengthWithQuoteHp() {
  const lv = LOCKED_LEVEL;
  const markPrice = currentMarkPrice();
  if (!markPrice || markPrice <= 0) {
    return;
  }
  const qty = (quoteHp * lv) / markPrice;
  if (Number.isFinite(qty)) {
    strengthInput.value = qty.toFixed(6);
  }
}

function updateSizeHelper() {
  if (!sizeHelper) {
    return;
  }
  if (presetActive) {
    syncStrengthWithQuoteHp();
  }
  const lv = LOCKED_LEVEL;
  const notional = quoteHp * lv;
  const markPrice = currentMarkPrice();
  let qtyText = '—';
  if (markPrice && markPrice > 0) {
    const qty = (quoteHp * lv) / markPrice;
    if (Number.isFinite(qty)) {
      qtyText = qty.toFixed(6);
    }
  }
  const speciesName = speciesSelect.value || 'Pokémon';
  sizeHelper.textContent = `Planned: HP ${formatPriceValue(quoteHp, speciesName)} × LV${lv} = Notional ${formatPriceValue(notional, speciesName)}; Est. qty ≈ ${qtyText} ${speciesName}`;
}

function renderStatus(status) {
  trainerName.textContent = `Trader: ${status.trainer_name}`;
  partyMembers = status.party || [];
  guardrails = status.guardrails || {};
  const serverDemo = Boolean(status.demo_mode);
  demoMode = serverDemo;

  lockEncounterLevel(LOCKED_LEVEL);

  const energyState = status.energy || {};
  energyFill.style.width = '0%';
  energyFill.dataset.energy = energyState.present ? 'online' : 'offline';
  energyFill.dataset.source = energyState.source || 'none';
  energyFill.classList.toggle('offline', !energyState.present);

  updateEnergyCaption(status, energyState);

  const cooldownRemaining = Math.ceil(guardrails.cooldown_remaining ?? 0);
  if (cooldownRemaining > 0) {
    cooldownChip.textContent = `Cooldown: ${cooldownRemaining}s`;
  } else {
    cooldownChip.textContent = 'Cooldown: Ready';
  }

  const rawMode = status.positionMode ?? status.position_mode ?? null;
  const normalizedMode = typeof rawMode === 'string' ? rawMode.toLowerCase() : null;
  positionMode = normalizedMode;
}

function formatWeight(weight) {
  const numeric = Number(weight);
  if (!Number.isFinite(numeric)) {
    return { text: 'WT —', placeholder: true };
  }
  return { text: `WT ${numeric.toFixed(3)} kg`, placeholder: false };
}

function formatPriceValue(price, species = null) {
  const numeric = Number(price);
  if (!Number.isFinite(numeric)) {
    return '—';
  }
  let options;

  // Species-specific decimal place formatting based on cryptocurrency standards
  if (species === 'Bitcoin') {
    // Bitcoin: 1 decimal place
    options = { minimumFractionDigits: 1, maximumFractionDigits: 1 };
  } else if (species === 'Ethereum') {
    // Ethereum: 2 decimal places for display
    options = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
  } else if (species === 'Solana') {
    // Solana: 3 decimal places
    options = { minimumFractionDigits: 3, maximumFractionDigits: 3 };
  } else if (species === 'Ripple') {
    // XRP: 4 decimal places
    options = { minimumFractionDigits: 4, maximumFractionDigits: 4 };
  } else if (species === 'Dogecoin') {
    // Dogecoin: 5 decimal places
    options = { minimumFractionDigits: 5, maximumFractionDigits: 5 };
  } else if (species === 'Hyperliquid') {
    // HYPE: 3 decimal places
    options = { minimumFractionDigits: 3, maximumFractionDigits: 3 };
  } else if (species === 'Avalanche') {
    // AVAX: 3 decimal places
    options = { minimumFractionDigits: 3, maximumFractionDigits: 3 };
  } else if (species === 'Sui') {
    // SUI: 3 decimal places
    options = { minimumFractionDigits: 3, maximumFractionDigits: 3 };
  } else if (species === 'BNB') {
    // BNB: 2 decimal places
    options = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
  } else if (species === 'Worldcoin') {
    // WLD: 3 decimal places
    options = { minimumFractionDigits: 3, maximumFractionDigits: 3 };
  } else if (Math.abs(numeric) >= 1) {
    options = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
  } else if (Math.abs(numeric) >= 0.1) {
    options = { minimumFractionDigits: 4, maximumFractionDigits: 4 };
  } else {
    options = { minimumFractionDigits: 2, maximumFractionDigits: 6 };
  }
  const formatter = new Intl.NumberFormat(undefined, options);
  return formatter.format(numeric);
}

function deriveWeightKg(price) {
  const numeric = Number(price);
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return null;
  }
  const safe = Math.max(numeric, 1e-7);
  const raw = 50 * (Math.log10(safe) + 2);
  const clamped = Math.max(5, Math.min(999, raw));
  return Number(clamped.toFixed(3));
}

function normalizeWeightFields(slot) {
  if (!slot || typeof slot !== 'object') {
    return slot;
  }
  if (slot.weightKg !== undefined) {
    const numeric = Number(slot.weightKg);
    slot.weightKg = Number.isFinite(numeric) ? numeric : null;
  }
  if (slot.weight_kg !== undefined) {
    const numeric = Number(slot.weight_kg);
    slot.weight_kg = Number.isFinite(numeric) ? numeric : null;
  }
  if (slot.weightKg === undefined && slot.weight_kg !== undefined) {
    slot.weightKg = slot.weight_kg;
  }
  if (slot.weight_kg === undefined && slot.weightKg !== undefined) {
    slot.weight_kg = slot.weightKg;
  }
  if (slot.price_usd !== undefined && slot.price_usd !== null) {
    const numericPrice = Number(slot.price_usd);
    slot.price_usd = Number.isFinite(numericPrice) ? numericPrice : null;
  }
  if (slot.price_source === undefined) {
    slot.price_source = null;
  }
  return slot;
}

function normalizeRosterSlots(slots) {
  if (!Array.isArray(slots)) {
    return [];
  }
  return slots.map((slot) => normalizeWeightFields(slot));
}

function applyOpenOrdersSummary(bySpecies) {
  const next = new Set();
  if (bySpecies && typeof bySpecies === 'object') {
    Object.entries(bySpecies).forEach(([species, value]) => {
      if (species && value) {
        next.add(species);
      }
    });
  }
  openOrderSpecies = next;
}

async function refreshOpenOrders() {
  try {
    const response = await fetchJSON(`${API_BASE}/adventure/open-orders-summary`);
    applyOpenOrdersSummary(response?.bySpecies || {});
  } catch (error) {
    console.error('Open orders summary fetch failed', error);
    applyOpenOrdersSummary({});
  }
}

function getSlotWeightValue(slot) {
  if (!slot) {
    return null;
  }
  const candidate = slot.weightKg ?? slot.weight_kg;
  if (Number.isFinite(candidate)) {
    return Number(candidate);
  }
  if (slot.price_usd !== undefined && slot.price_usd !== null) {
    return deriveWeightKg(slot.price_usd);
  }
  return null;
}

function renderJournal(entries) {
  if (!journalFeed || !Array.isArray(entries)) {
    return;
  }

  journalFeed.innerHTML = '';
  const sorted = [...entries].reverse();
  sorted.forEach((entry) => {
    const li = document.createElement('li');
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = entry.badge || 'Adventure';

    const message = document.createElement('div');
    message.textContent = entry.message;

    const time = document.createElement('time');
    const date = new Date(entry.timestamp);
    time.textContent = date.toLocaleTimeString();

    li.appendChild(badge);
    li.appendChild(message);
    li.appendChild(time);
    journalFeed.appendChild(li);
  });
}

function updatePriceInputSteps() {
  const species = speciesSelect.value;
  let stepValue = '0.01'; // Default step

  // Set species-specific step values based on typical exchange tick sizes
  if (species === 'Bitcoin') {
    stepValue = '0.1'; // Bitcoin: 1 decimal place
  } else if (species === 'Ethereum') {
    stepValue = '0.01'; // ETH: 2 decimal places
  } else if (species === 'Solana') {
    stepValue = '0.001'; // SOL: 3 decimal places
  } else if (species === 'Ripple') {
    stepValue = '0.0001'; // XRP: 4 decimal places
  } else if (species === 'Dogecoin') {
    stepValue = '0.00001'; // DOGE: 5 decimal places
  } else if (species === 'Hyperliquid') {
    stepValue = '0.001'; // HYPE: 3 decimal places
  } else if (species === 'Avalanche') {
    stepValue = '0.001'; // AVAX: 3 decimal places
  } else if (species === 'Sui') {
    stepValue = '0.001'; // SUI: 3 decimal places
  } else if (species === 'BNB') {
    stepValue = '0.01'; // BNB: 2 decimal places
  } else if (species === 'Worldcoin') {
    stepValue = '0.001'; // WLD: 3 decimal places
  }

  if (priceInput) {
    priceInput.step = stepValue;
  }

  // Update stop loss input step only when in price mode
  if (stopLossInput && stopLossMode === 'price') {
    stopLossInput.step = stepValue;
  }
}

function updateLevelBounds() {
  lockEncounterLevel(LOCKED_LEVEL);
  renderRoster(rosterSlots);
  autoDefaultEscapeRope();
  updatePriceInputSteps();
}

function updateLevelIndicator() {
  if (levelIndicator) {
    levelIndicator.textContent = `LV${LOCKED_LEVEL}`;
  }
}

function isPerpLevel() {
  return LOCKED_LEVEL >= 2;
}

function isLongEntry() {
  return actionInput.value === 'throw_pokeball';
}

function isShortEntry() {
  return actionInput.value === 'close_position' && isPerpLevel();
}

function requiresStopLoss() {
  return isLongEntry() || isShortEntry();
}

function currentLimitPrice() {
  if (orderStyleSelect.value === 'limit' && priceInput.value) {
    const parsed = parseInputNumber(priceInput.value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function validateForm() {
  if (!confirmButton) {
    return true;
  }
  let valid = true;
  let message = '';

  const stopLossVisible = stopLossGroup ? !stopLossGroup.hidden : false;
  const needsStopLoss = requiresStopLoss() && stopLossVisible;

  if (needsStopLoss) {
    const untouched = !stopLossDirty && stopLossInput && (!stopLossInput.value || !stopLossInput.value.trim());
    if (untouched) {
      if (stopLossHint) {
        stopLossHint.textContent = '';
      }
      confirmButton.disabled = false;
      return true;
    }
    const ropeValue = parseInputNumber(stopLossInput.value);
    if (stopLossMode === 'percent') {
      if (!Number.isFinite(ropeValue) || ropeValue < 0.1 || ropeValue > 99.9) {
        valid = false;
        message = 'Distance must be between 0.1 and 99.9.';
      }
    } else if (!Number.isFinite(ropeValue) || ropeValue <= 0) {
      valid = false;
      message = 'Set your stop loss before confirming.';
    }

    if (valid && stopLossMode === 'price') {
      const limitPrice = currentLimitPrice();
      if (limitPrice !== null) {
        if (isLongEntry() && ropeValue >= limitPrice) {
          valid = false;
          message = 'Your stop loss must be set below your entry price for a long.';
        }
        if (isShortEntry() && ropeValue <= limitPrice) {
          valid = false;
          message = 'Your stop loss must be set above your entry price for a short.';
        }
      }
    }

    if (!message && stopLossMode === 'percent') {
      message = 'Stop loss will be set based on current price.';
    }
  } else {
    message = '';
    valid = true;
  }

  if (stopLossHint) {
    stopLossHint.textContent = message;
  }
  if (!message && stopLossScale && stopLossScale.dataset.tickNote) {
    stopLossScale.textContent = stopLossScale.dataset.tickNote;
  } else if (stopLossScale && message) {
    stopLossScale.textContent = '';
  }

  const isRun = actionInput.value === 'run_away';
  confirmButton.disabled = isRun ? false : !valid;
  return valid;
}

function firstDefined(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null) {
      return value;
    }
  }
  return undefined;
}

function formatEnergyNumber(value) {
  if (!Number.isFinite(value)) {
    return null;
  }
  return value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function clampFill(value) {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.max(0, Math.min(1, value));
}

function updateEnergyCaption(status, energyState = {}) {
  if (!energyCaption || !energyFill) {
    return;
  }

  const availableRaw = firstDefined(
    energyState.available,
    energyState.free,
    status?.balances?.USDT?.available,
    status?.balances?.usdt?.available
  );
  const availableProvided = availableRaw !== undefined && availableRaw !== null;
  const availableValue = availableProvided ? Number(availableRaw) : Number.NaN;

  const totalRaw = firstDefined(
    energyState.total,
    energyState.capacity,
    status?.balances?.USDT?.total,
    status?.balances?.usdt?.total
  );
  const totalProvided = totalRaw !== undefined && totalRaw !== null;
  const totalValue = totalProvided ? Number(totalRaw) : Number.NaN;

  const hasAvailable = availableProvided && Number.isFinite(availableValue);
  const hasTotal = totalProvided && Number.isFinite(totalValue) && totalValue > 0;

  let ratio = 0;
  if (hasAvailable && hasTotal) {
    const cappedTotal = Math.max(totalValue, 0);
    const safeAvailable = Math.min(Math.max(availableValue, 0), cappedTotal);
    ratio = cappedTotal > 0 ? clampFill(safeAvailable / cappedTotal) : 0;
  } else {
    ratio = clampFill(Number(energyState.fill));
  }
  energyFill.style.width = `${Math.round(ratio * 100)}%`;

  const labelText = typeof energyState.label === 'string' && energyState.label.trim().length > 0
    ? energyState.label.trim()
    : null;

  if (labelText) {
    energyCaption.textContent = `HP ${labelText}`;
    return;
  }

  if (hasAvailable && hasTotal) {
    const availableText = formatEnergyNumber(Math.min(Math.max(availableValue, 0), totalValue));
    const totalText = formatEnergyNumber(Math.max(totalValue, 0));
    if (availableText && totalText) {
      energyCaption.textContent = `HP ${availableText}/${totalText}`;
      return;
    }
  }

  if (hasTotal) {
    const totalText = formatEnergyNumber(Math.max(totalValue, 0));
    if (totalText) {
      energyCaption.textContent = `HP --/${totalText}`;
      return;
    }
  }

  energyCaption.textContent = 'HP --/--';
}

function applyRunModeUI(isRun) {
  if (!strengthInput) {
    return;
  }

  if (isRun) {
    if (!('prevValue' in strengthInput.dataset)) {
      strengthInput.dataset.prevValue = strengthInput.value;
    }
    if (!('prevPlaceholder' in strengthInput.dataset)) {
      strengthInput.dataset.prevPlaceholder = strengthInput.placeholder || strengthPlaceholder;
    }
    if (!('prevType' in strengthInput.dataset)) {
      strengthInput.dataset.prevType = strengthInput.type || 'number';
    }
    strengthInput.type = 'text';
    strengthInput.value = '-';
    strengthInput.placeholder = '-';
    strengthInput.readOnly = true;
    strengthInput.disabled = true;
    strengthInput.classList.add('is-disabled');
    strengthInput.setAttribute('aria-disabled', 'true');
  } else {
    const restoredType = strengthInput.dataset.prevType || 'number';
    strengthInput.type = restoredType;
    strengthInput.readOnly = false;
    strengthInput.disabled = false;
    strengthInput.classList.remove('is-disabled');
    strengthInput.removeAttribute('aria-disabled');
    const restoredPlaceholder = strengthInput.dataset.prevPlaceholder ?? strengthPlaceholder;
    strengthInput.placeholder = restoredPlaceholder;
    if (strengthInput.dataset.prevValue !== undefined) {
      strengthInput.value = strengthInput.dataset.prevValue;
      delete strengthInput.dataset.prevValue;
    }
    delete strengthInput.dataset.prevType;
    delete strengthInput.dataset.prevPlaceholder;
  }

  priceInputs.forEach((input) => {
    const originalPlaceholder = priceInputPlaceholders.get(input) || '';
    if (isRun) {
      if (!('prevValue' in input.dataset)) {
        input.dataset.prevValue = input.value;
      }
      if (!('prevPlaceholder' in input.dataset)) {
        input.dataset.prevPlaceholder = input.placeholder || originalPlaceholder;
      }
      input.value = '';
      input.placeholder = '-';
      input.readOnly = true;
      input.disabled = true;
      input.classList.add('is-disabled');
      input.setAttribute('aria-disabled', 'true');
    } else {
      input.readOnly = false;
      input.disabled = false;
      input.classList.remove('is-disabled');
      input.removeAttribute('aria-disabled');
      const restoredPlaceholder = input.dataset.prevPlaceholder ?? originalPlaceholder;
      input.placeholder = restoredPlaceholder;
      if (input.dataset.prevValue !== undefined) {
        input.value = input.dataset.prevValue;
        delete input.dataset.prevValue;
      }
      delete input.dataset.prevPlaceholder;
    }
  });

  presetButtons.forEach((btn) => {
    if (isRun) {
      btn.classList.add('is-disabled');
      btn.setAttribute('aria-disabled', 'true');
      btn.disabled = true;
    } else {
      btn.classList.remove('is-disabled');
      btn.removeAttribute('aria-disabled');
      btn.disabled = false;
    }
  });
}

async function fetchSensorPrice(species, sensor) {
  const base = speciesToBaseToken(species);
  if (!base) {
    return null;
  }
  const baseKey = String(base).toUpperCase();
  const cached = latestPriceMap.get(baseKey);
  if (cached && Number.isFinite(cached.price)) {
    return Number(cached.price);
  }
  try {
    const snapshot = await fetchJSON(`${API_BASE}/atlas/prices`);
    applyPriceSnapshot(snapshot);
    const updated = latestPriceMap.get(baseKey);
    return updated && Number.isFinite(updated.price) ? Number(updated.price) : null;
  } catch (error) {
    console.error(error);
    return null;
  }
}

function speciesToBaseToken(species) {
  const slot = rosterSlots.find((item) => item.species === species);
  return slot?.base_token;
}

function formatAutoAnchor(value, species = null) {
  if (!Number.isFinite(value)) {
    return '';
  }
  return formatPriceValue(value, species);
}

function toggleRunInputs(disabled) {
  const interactive = [
    ...presetButtons,
    strengthInput,
    orderStyleSelect,
    priceInput,
    stopLossInput,
    stopLossTriggerSelect,
    ...stopLossModeButtons,
  ];
  interactive.forEach((el) => {
    if (!el) return;
    el.disabled = disabled;
    if (disabled) {
      el.classList.add('disabled-field');
    } else {
      el.classList.remove('disabled-field');
    }
  });
  lockEncounterLevel(LOCKED_LEVEL);
  priceInput.disabled = disabled || orderStyleSelect.value !== 'limit';
}

function autoDefaultEscapeRope() {
  if (!requiresStopLoss() || stopLossDirty) {
    return;
  }
  if (stopLossMode === 'percent') {
    stopLossInput.value = '5.0';
    validateForm();
    return;
  }
  const species = speciesSelect.value;
  if (!species) {
    return;
  }
  const sensor = stopLossTriggerSelect.value;
  fetchSensorPrice(species, sensor)
    .then((price) => {
      if (price === null || price <= 0) {
        return;
      }
      const isShort = isShortEntry();
      const factor = isShort ? 1.05 : 0.95;
      const anchor = price * factor;
      stopLossInput.value = formatAutoAnchor(anchor, species);
      stopLossDirty = false;
      validateForm();
    })
    .catch(() => {});
}

function setAction(action) {
  actionInput.value = action;
  actionButtons.forEach((button) => {
    button.classList.toggle('active', button.dataset.action === action);
  });
  lockEncounterLevel(LOCKED_LEVEL);
  updateOrderControls();
  validateForm();
}

function setPreset(button) {
  presetButtons.forEach((btn) => btn.classList.remove('active'));
  if (!button) {
    selectedPreset = null;
    presetActive = false;
    return;
  }
  button.classList.add('active');
  selectedPreset = button.dataset.preset || null;
  const hpValue = Number(button.dataset.hp || 0);
  if (Number.isFinite(hpValue) && hpValue > 0) {
    quoteHp = hpValue;
    presetActive = true;
    syncStrengthWithQuoteHp();
  } else {
    presetActive = false;
  }
  updateSizeHelper();
}

async function refreshRoster() {
  try {
    const roster = await fetchJSON(`${API_BASE}/atlas/refresh`, { method: 'POST' });
    rosterSlots = normalizeRosterSlots(roster?.roster || []);
    await refreshOpenOrders();
    renderRoster(rosterSlots);
    toast('Prices updated!');
    loadAIInsights();
  } catch (error) {
    console.error(error);
    toast('System: Price refresh failed.');
  }
}

async function hydrate() {
  try {
    const statusPath = `${API_BASE}/trainer/status${demoMode ? '?demo=1' : ''}`;
    const speciesPromise = fetchJSON(`${API_BASE}/atlas/species`).then((data) => {
      speciesDex = data || {};
      renderSpeciesOptions(speciesDex);
      return data;
    });
    const rosterPromise = fetchJSON(`${API_BASE}/atlas/roster`);
    const statusPromise = fetchJSON(statusPath);
    const journalPromise = fetchJSON(`${API_BASE}/adventure/journal`);
    const openOrdersPromise = fetchJSON(`${API_BASE}/adventure/open-orders-summary`);

    const [speciesResult, rosterResult, statusResult, journalResult, ordersResult] = await Promise.allSettled([
      speciesPromise,
      rosterPromise,
      statusPromise,
      journalPromise,
      openOrdersPromise,
    ]);

    if (speciesResult.status === 'rejected') {
      console.error('Species load failed', speciesResult.reason);
      if (!speciesDex || Object.keys(speciesDex).length === 0) {
        renderSpeciesOptions({});
      }
    }

    if (rosterResult.status === 'fulfilled') {
      rosterSlots = normalizeRosterSlots(rosterResult.value?.roster || []);
    } else {
      console.error('Roster load failed', rosterResult.reason);
    }

    if (ordersResult.status === 'fulfilled') {
      applyOpenOrdersSummary(ordersResult.value?.bySpecies || {});
    } else {
      console.error('Open orders load failed', ordersResult.reason);
      applyOpenOrdersSummary({});
    }

    renderRoster(rosterSlots);

    let statusData = null;
    let linkShellState = 'online';
    if (statusResult.status === 'fulfilled') {
      statusData = statusResult.value || null;
      if (statusData && typeof statusData.linkShell === 'string') {
        linkShellState = statusData.linkShell.toLowerCase();
      }
    } else {
      console.error('Trainer status load failed', statusResult.reason);
      linkShellState = 'offline';
    }

    if (linkShellState === 'offline') {
      if (!linkShellToastShown) {
        toast('System: Exchange connection offline.');
        linkShellToastShown = true;
      }
    } else {
      linkShellToastShown = false;
    }

    if (statusData) {
      renderStatus(statusData);
    } else {
      renderStatus(defaultTrainerStatus);
    }

    if (journalResult.status === 'fulfilled') {
      renderJournal(journalResult.value || []);
    } else {
      console.error('Journal load failed', journalResult.reason);
    }

  lockEncounterLevel(LOCKED_LEVEL);
  updateOrderControls();
  validateForm();
    await refreshPrices();
    startCountdownTimer();
    if (!priceRefreshTimer) {
      priceRefreshTimer = setInterval(() => {
        refreshPrices().catch((error) => console.error(error));
        startCountdownTimer();
      }, REFRESH_INTERVAL);
    }
    updateSizeHelper();
  } catch (error) {
    console.error(error);
    if (!linkShellToastShown) {
      toast('System: Exchange connection offline.');
      linkShellToastShown = true;
    }
  }
}

function applyPriceSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') {
    return;
  }
  const tsNumeric = Number(snapshot.ts);
  const timestamp = Number.isFinite(tsNumeric) ? tsNumeric : Date.now();
  const items = Array.isArray(snapshot.items) ? snapshot.items : [];

  if (items.length > 0) {
    const nextMap = new Map(latestPriceMap);
    items.forEach((item) => {
      if (!item || !item.base) {
        return;
      }
      const baseKey = String(item.base).toUpperCase();
      if (!baseKey) {
        return;
      }
      const priceValueRaw = item.price !== undefined ? Number(item.price) : null;
      const priceValue = Number.isFinite(priceValueRaw) ? priceValueRaw : null;
      const weightValueRaw = item.weightKg !== undefined ? Number(item.weightKg) : null;
      const fallbackWeight = Number.isFinite(weightValueRaw)
        ? Number(weightValueRaw.toFixed(3))
        : null;
      const derivedWeight = priceValue !== null ? deriveWeightKg(priceValue) : null;
      const finalWeight = derivedWeight ?? fallbackWeight;
      const source = item.source || null;
      nextMap.set(baseKey, {
        price: priceValue,
        source,
        weightKg: finalWeight,
      });
    });
    if (nextMap.size > 0) {
      latestPriceMap = nextMap;
    }
  }

  latestPriceSnapshot = {
    healthy: Boolean(snapshot.healthy && latestPriceMap.size),
    ts: timestamp,
  };
  if (!latestPriceMap.size && items.length === 0) {
    latestPriceSnapshot.healthy = false;
  }
  updateSizeHelper();
}

async function refreshPrices() {
  if (!rosterSlots.length) {
    return;
  }
  try {
    const snapshot = await fetchJSON(`${API_BASE}/atlas/prices`);
    applyPriceSnapshot(snapshot);

    if (!latestPriceMap.size) {
      return;
    }

    let changed = false;
    rosterSlots.forEach((slot) => {
      if (
        !slot ||
        slot.status !== 'occupied' ||
        typeof slot.species !== 'string' ||
        !slot.species ||
        slot.species === '???' ||
        !slot.base_token
      ) {
        return;
      }
      const baseKey = String(slot.base_token).toUpperCase();
      if (!baseKey) {
        return;
      }
      const quote = latestPriceMap.get(baseKey);
      if (!quote) {
        return;
      }

      const prevPrice = Number.isFinite(slot.price_usd) ? Number(slot.price_usd) : null;
      const price = Number.isFinite(quote.price) ? Number(quote.price) : null;
      const prevSource = slot.price_source ?? null;
      const source = quote.source || null;
      const prevWeight = getSlotWeightValue(slot);
      const weight = quote.weightKg ?? (price !== null ? deriveWeightKg(price) : null);

      const priceChanged =
        prevPrice === null || price === null
          ? prevPrice !== price
          : Math.abs(price - prevPrice) >= 1e-8;
      const sourceChanged = prevSource !== source;
      const weightChanged =
        prevWeight === null || weight === null
          ? prevWeight !== weight
          : Math.abs(weight - prevWeight) >= 0.001;

      if (priceChanged || sourceChanged || weightChanged) {
        slot.price_usd = price;
        slot.price_source = source;
        slot.weightKg = weight;
        slot.weight_kg = weight;
        changed = true;
      }
    });

    if (changed) {
      renderRoster(rosterSlots);
    }
    updateSizeHelper();
  } catch (error) {
    console.error(error);
  }
}

// Event bindings
speciesSelect.addEventListener('change', () => {
  updateLevelBounds();
  if (presetActive) {
    syncStrengthWithQuoteHp();
  }
  updateSizeHelper();
  validateForm();
});

actionButtons.forEach((button) => {
  button.addEventListener('click', () => {
    setAction(button.dataset.action);
  });
});

if (levelSlider) {
  levelSlider.addEventListener('input', () => {
    lockEncounterLevel(LOCKED_LEVEL);
    updateOrderControls();
    stopLossDirty = false;
    autoDefaultEscapeRope();
    if (presetActive) {
      syncStrengthWithQuoteHp();
    }
    updateSizeHelper();
  });
}

presetButtons.forEach((button) => {
  button.addEventListener('click', () => {
    if (button.classList.contains('is-disabled')) {
      return;
    }
    setPreset(button);
    validateForm();
  });
});

strengthInput.addEventListener('input', () => {
  const qty = parseInputNumber(strengthInput.value);
  const markPrice = currentMarkPrice();
  const lv = LOCKED_LEVEL;
  if (Number.isFinite(qty) && qty > 0 && markPrice && markPrice > 0) {
    quoteHp = qty * markPrice / lv;
    presetActive = false;
    selectedPreset = null;
    presetButtons.forEach((btn) => btn.classList.remove('active'));
  }
  updateSizeHelper();
  validateForm();
});

refreshRosterBtn.addEventListener('click', () => {
  refreshRoster();
});

orderStyleSelect.addEventListener('change', () => {
  updateOrderControls();
});

priceInput.addEventListener('input', () => {
  validateForm();
});

stopLossInput.addEventListener('input', () => {
  stopLossDirty = true;
  validateForm();
});

stopLossTriggerSelect.addEventListener('change', () => {
  stopLossDirty = false;
  autoDefaultEscapeRope();
  validateForm();
});

stopLossModeButtons.forEach((button) => {
  button.addEventListener('click', () => {
    if (button.dataset.slMode === stopLossMode) {
      return;
    }
    stopLossModeButtons.forEach((btn) => btn.classList.remove('active'));
    button.classList.add('active');
    stopLossMode = button.dataset.slMode || 'price';
    stopLossInput.placeholder = stopLossMode === 'percent' ? 'Set Distance (%)' : 'Set Price';
    if (stopLossMode === 'percent') {
      stopLossInput.step = '0.1';
    } else {
      updatePriceInputSteps(); // This will set the correct step for the current species
    }
    stopLossDirty = false;
    validateForm();
  });
});

encounterForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const species = speciesSelect.value;
  if (!species) {
    toast('Select an asset first!');
    return;
  }

  if (actionInput.value !== 'run_away') {
    const ready = validateForm();
    if (!ready) {
      const helper = stopLossHint ? stopLossHint.textContent : '';
      toast(helper || 'Set stop loss before confirming.');
      return;
    }
    if (requiresStopLoss()) {
      toast('Setting stop loss...');
    }
  }

  const payload = {
    species,
    action: actionInput.value,
    level: LOCKED_LEVEL,
    order_style: orderStyleSelect.value,
    demo_mode: demoMode,
  };

  if (actionInput.value !== 'run_away') {
    const strengthValue = parseInputNumber(strengthInput.value);
    payload.pokeball_strength = Number.isFinite(strengthValue) ? strengthValue : 0;

    if (orderStyleSelect.value === 'limit' && priceInput.value) {
      const limitPrice = parseInputNumber(priceInput.value);
      if (Number.isFinite(limitPrice)) {
        payload.limit_price = limitPrice;
      }
    }

    if (selectedPreset) {
      payload.size_preset = selectedPreset;
    }
  }

  if (requiresStopLoss()) {
    const stopLossValue = parseInputNumber(stopLossInput.value);
    const anchor = computeStopLossAnchor(stopLossValue);
    if (Number.isFinite(anchor)) {
      payload.stop_loss = anchor;
    }
  }

  console.log('Sending order:', payload);

  try {
    const receipt = await fetchJSON(`${API_BASE}/adventure/encounter`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(payload)
    });
    toast(receipt.narration || `Trade executed for ${receipt.species}!`);
    await hydrate();
    applyNormalizedReceipt(receipt);
    loadAIInsights();
  } catch (error) {
    console.error(error);
    const message = typeof error?.message === 'string' ? error.message : '';
    if (positionMode === 'one_way' && message.toLowerCase().includes('need side')) {
      toast('Switch to Hedge mode to manage sides, or close from your exchange.');
      return;
    }
    toast(message || 'System: Trade aborted.');
  }
});

hydrate();
setInterval(hydrate, 30000);
updateOrderControls();

function updateOrderControls() {
  const isRun = actionInput.value === 'run_away';
  orderStyleSelect.disabled = isRun;
  const limitOnly = !isRun && orderStyleSelect.value === 'limit';
  applyRunModeUI(isRun);
  priceInput.disabled = isRun || !limitOnly;
  const needsSL = requiresStopLoss();
  if (stopLossGroup) {
    stopLossGroup.hidden = !needsSL;
    if (!needsSL) {
      stopLossInput.value = '';
      if (stopLossHint) {
        stopLossHint.textContent = '';
      }
      if (stopLossScale) {
        stopLossScale.textContent = '';
        delete stopLossScale.dataset.tickNote;
      }
    }
  }
  validateForm();
}

function applyNormalizedReceipt(receipt) {
  if (!receipt || typeof receipt !== 'object') {
    return;
  }

  if (receipt.normalizedPrice && orderStyleSelect.value === 'limit') {
    priceInput.value = String(receipt.normalizedPrice);
  }

  if (receipt.normalizedTriggerPrice && stopLossMode === 'price') {
    stopLossInput.value = String(receipt.normalizedTriggerPrice);
  }

  if (stopLossScale) {
    const tickFormatted = receipt.priceTickFormatted || null;
    if (tickFormatted) {
      const note = `Using ${tickFormatted} increments.`;
      stopLossScale.textContent = note;
      stopLossScale.dataset.tickNote = note;
    } else {
      stopLossScale.textContent = '';
      delete stopLossScale.dataset.tickNote;
    }
  }

  validateForm();
}

function lockEncounterLevel(level) {
  const target = Number(level) || LOCKED_LEVEL;

  if (levelSlider) {
    levelSlider.value = String(target);
    levelSlider.disabled = true;
    levelSlider.readOnly = true;
    levelSlider.classList.add('lv-locked');
    levelSlider.setAttribute('aria-disabled', 'true');
  }

  const shadowInputs = [
    document.getElementById('lv'),
    document.getElementById('lvInput'),
    document.querySelector('input[name="lv"]'),
  ];
  shadowInputs.forEach((input) => {
    if (input) {
      input.value = String(target);
      input.disabled = true;
      input.readOnly = true;
      input.classList.add('lv-locked');
      input.setAttribute('aria-disabled', 'true');
    }
  });

  if (levelIndicator) {
    levelIndicator.textContent = `LV${target}`;
    levelIndicator.classList.add('lv-locked');
  }
}

// AI Chat Functionality
async function loadAIInsights() {
  const container = document.getElementById('ai-chat-container');
  if (!container) return;

  // Generate insights based on current market data
  const insights = [];

  // Get top 3 tokens from roster with prices
  const tokensWithPrices = rosterSlots
    .filter(slot => slot.species && slot.price_usd && slot.status === 'occupied')
    .slice(0, 3);

  if (tokensWithPrices.length > 0) {
    tokensWithPrices.forEach(slot => {
      const price = formatPriceValue(slot.price_usd, slot.species);
      insights.push({
        timestamp: new Date().toLocaleTimeString(),
        text: `${slot.species}: Current price at ${price} USDT. Market ${slot.element || 'stable'}.`
      });
    });
  } else {
    insights.push({
      timestamp: new Date().toLocaleTimeString(),
      text: 'Monitoring market conditions. Refresh prices to get latest data.'
    });
  }

  // Add system status
  insights.push({
    timestamp: new Date().toLocaleTimeString(),
    text: `System active. Next price update in ${Math.floor(REFRESH_INTERVAL / 60000)} minutes.`
  });

  container.innerHTML = insights.map(insight => `
    <div class="ai-chat-message">
      <span class="ai-avatar">🤖</span>
      <div class="ai-content">
        <p class="ai-timestamp">${insight.timestamp}</p>
        <p class="ai-text">${insight.text}</p>
      </div>
    </div>
  `).join('');
}

// Initialize AI chat
const refreshAIChatBtn = document.getElementById('refresh-ai-chat');
if (refreshAIChatBtn) {
  refreshAIChatBtn.addEventListener('click', loadAIInsights);
}

// Load AI insights after initial data load
setTimeout(() => {
  loadAIInsights();
}, 2000);
