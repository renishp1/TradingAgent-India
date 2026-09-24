/* TradingAgent-India paper dashboard — read-only client. */

const state = {
  dashboard: null,
};

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmt(value) {
  if (value === null || value === undefined || value === "") return "NOT AVAILABLE";
  if (typeof value === "number") {
    return Number.isInteger(value)
      ? value.toLocaleString("en-IN")
      : value.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  }
  return String(value);
}

async function getJson(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

function kvHtml(rows) {
  return rows
    .map(
      ([k, v]) =>
        `<div class="k">${esc(k)}</div><div class="v">${esc(fmt(v))}</div>`
    )
    .join("");
}

function renderMarket(market) {
  const indices = market.indices || {};
  const nifty = indices.NIFTY || {};
  const bank = indices.BANKNIFTY || {};
  document.getElementById("market-overview").innerHTML = `
    <div class="kpi">
      <div class="label">NIFTY</div>
      <div class="value">${esc(fmt(nifty.price))}</div>
      <div class="tag">${esc(nifty.label || market.provenance || market.data_label || "NOT AVAILABLE")}</div>
    </div>
    <div class="kpi">
      <div class="label">BANK NIFTY</div>
      <div class="value">${esc(fmt(bank.price))}</div>
      <div class="tag">${esc(bank.label || market.provenance || market.data_label || "NOT AVAILABLE")}</div>
    </div>
    <div class="kpi">
      <div class="label">Market Status</div>
      <div class="value">${esc(fmt(market.session_state))}</div>
      <div class="tag">Data: ${esc(fmt(market.market_data))} · Zerodha: ${esc(fmt(market.zerodha_status))}</div>
    </div>`;
  const noteBits = [
    market.note,
    market.provenance ? `Provenance: ${market.provenance}` : null,
    market.freshness ? `Freshness: ${market.freshness}` : null,
    nifty.timestamp ? `NIFTY ts: ${nifty.timestamp}` : null,
    market.quote_age_seconds != null && market.quote_age_seconds !== "NOT AVAILABLE"
      ? `Quote age (s): ${market.quote_age_seconds}`
      : null,
  ].filter(Boolean);
  document.getElementById("market-data-note").textContent =
    noteBits.join(" · ") || "Market state from session calendar + Zerodha / snapshot artifacts.";
  document.getElementById("market-status-pill").textContent =
    `Market: ${fmt(market.session_state)} · Data: ${fmt(market.market_data)} · Zerodha: ${fmt(market.zerodha_status)}`;
}

function renderSystem(system) {
  document.getElementById("system-panel").innerHTML = kvHtml([
    ["Trading mode", system.trading_mode],
    ["Live trading status", system.live_trading_status],
    ["Broker order path", system.broker_order_path],
    ["Risk Guard status", system.risk_guard_status],
    ["Market data status", system.market_data_status],
    ["Zerodha status", system.zerodha_status],
    ["Provenance", system.provenance],
    ["Freshness", system.freshness],
    ["LIVE_TRADING_COMPILED", system.live_trading_compiled],
  ]);
}

function renderCapital(capital) {
  document.getElementById("capital-panel").innerHTML = kvHtml([
    ["Paper capital", capital.paper_capital],
    ["Available capital", capital.available_capital],
    ["Today's P&L", capital.today_pnl],
    ["Daily loss limit", capital.daily_loss_limit],
    ["Max risk / trade", capital.max_per_trade_risk],
    ["Max open positions", capital.max_open_positions],
    ["Remaining daily risk", capital.remaining_daily_risk],
    ["Capital profile", capital.capital_profile],
    ["Currency", capital.currency],
    ["Data label", capital.data_label],
  ]);
}

function positionsTable(positionsPayload) {
  const rows = positionsPayload.positions || [];
  if (!rows.length) {
    return `<div class="empty">${esc(positionsPayload.message || "No open paper positions")}</div>`;
  }
  const body = rows
    .map(
      (p) => `<tr>
        <td>${esc(fmt(p.symbol))}</td>
        <td>${esc(fmt(p.contract))}</td>
        <td>${esc(fmt(p.quantity))}</td>
        <td>${esc(fmt(p.entry_price))}</td>
        <td>${esc(fmt(p.current_price))}</td>
        <td>${esc(fmt(p.pnl))}</td>
        <td>${esc(fmt(p.status))}</td>
      </tr>`
    )
    .join("");
  return `<table class="data">
    <thead>
      <tr>
        <th>Symbol</th><th>Contract</th><th>Qty</th>
        <th>Entry</th><th>LTP</th><th>P&amp;L</th><th>Status</th>
      </tr>
    </thead>
    <tbody>${body}</tbody>
  </table>`;
}

function renderCeo(agents) {
  const el = document.getElementById("ceo-panel");
  if (!agents.available || !(agents.decisions || []).length) {
    el.innerHTML = `<div class="empty">${esc(agents.message || "AI decision data not available")}</div>`;
    return;
  }
  const d = agents.decisions[0];
  el.innerHTML = `<div class="decision-grid">
    <div><div class="label">Cycle ID</div><div class="value">${esc(fmt(d.cycle_id))}</div></div>
    <div><div class="label">Snapshot ID</div><div class="value">${esc(fmt(d.snapshot_id))}</div></div>
    <div><div class="label">Candidate</div><div class="value">${esc(fmt(d.candidate))}</div></div>
    <div><div class="label">CE/PE</div><div class="value">${esc(fmt(d.ce_pe || d.option_type))}</div></div>
    <div><div class="label">Strike</div><div class="value">${esc(fmt(d.strike))}</div></div>
    <div><div class="label">Expiry</div><div class="value">${esc(fmt(d.expiry))}</div></div>
    <div><div class="label">DTE</div><div class="value">${esc(fmt(d.dte))}</div></div>
    <div><div class="label">Score</div><div class="value">${esc(fmt(d.score))}</div></div>
    <div><div class="label">CEO Gate</div><div class="value">${esc(fmt(d.ceo_gate))}</div></div>
    <div><div class="label">RiskGuard</div><div class="value">${esc(fmt(d.risk_guard))}</div></div>
    <div><div class="label">Final action</div><div class="value">${esc(fmt(d.final_action || d.decision))}</div></div>
    <div><div class="label">Provenance</div><div class="value">${esc(fmt(d.provenance))}</div></div>
    <div style="grid-column:1/-1"><div class="label">Reason</div><div class="value">${esc(fmt(d.reason))}</div></div>
  </div>`;
}

function optionChainHtml(chain) {
  if (!chain.available) {
    return `<div class="empty">${esc(chain.message || "NOT AVAILABLE")}</div>
       <p class="muted">${esc(chain.label || "")}</p>`;
  }
  const rows = chain.rows || [];
  if (!rows.length) {
    return kvHtml([
      ["CE", chain.ce],
      ["Strike", chain.strike],
      ["PE", chain.pe],
      ["Provenance", chain.provenance],
    ]);
  }
  const body = rows
    .map(
      (r) => `<tr>
        <td>${esc(fmt(r.expiry))}</td>
        <td>${esc(fmt(r.dte))}</td>
        <td>${esc(fmt(r.strike))}</td>
        <td>${esc(fmt(r.ce_ltp))}</td>
        <td>${esc(fmt(r.ce_bid))}</td>
        <td>${esc(fmt(r.ce_ask))}</td>
        <td>${esc(fmt(r.pe_ltp))}</td>
        <td>${esc(fmt(r.pe_bid))}</td>
        <td>${esc(fmt(r.pe_ask))}</td>
        <td>${esc(fmt(r.score))}</td>
        <td>${esc(fmt(r.eligibility))}</td>
        <td>${esc(fmt(r.rejection_reason))}</td>
      </tr>`
    )
    .join("");
  const selected = chain.selected
    ? `<p class="muted">Selected: ${esc(fmt(chain.selected.instrument))} · ${esc(
        fmt(chain.selected.option_type)
      )} ${esc(fmt(chain.selected.strike))} ${esc(fmt(chain.selected.expiry))}</p>`
    : "";
  return `${selected}<div class="kv" style="margin-bottom:0.75rem">${kvHtml([
    ["Provenance", chain.provenance],
    ["Freshness", chain.freshness],
    ["Snapshot", chain.snapshot_id],
  ])}</div>
  <table class="data">
    <thead>
      <tr>
        <th>Expiry</th><th>DTE</th><th>Strike</th>
        <th>CE LTP</th><th>CE bid</th><th>CE ask</th>
        <th>PE LTP</th><th>PE bid</th><th>PE ask</th>
        <th>Score</th><th>Eligibility</th><th>Rejection</th>
      </tr>
    </thead>
    <tbody>${body}</tbody>
  </table>`;
}

function renderOption(chain) {
  const html = optionChainHtml(chain);
  document.getElementById("option-panel-dash").innerHTML = html;
  document.getElementById("option-page").innerHTML = html;
}

function renderRisk(risk) {
  const status = `<span class="lamp" aria-hidden="true"></span> 🟢 Risk Guard ${esc(risk.status || "ACTIVE")}`;
  document.getElementById("risk-status").innerHTML = status;
  document.getElementById("risk-status-page").innerHTML = status;
  const rows = [
    ["Starting capital", risk.starting_capital ?? risk.paper_capital],
    ["Available capital", risk.available_capital],
    ["Daily P&L", risk.daily_pnl ?? risk.today_pnl],
    ["Max daily loss", risk.max_daily_loss],
    ["Remaining daily risk", risk.remaining_daily_risk],
    ["Max risk / trade", risk.max_per_trade_risk],
    ["Current exposure", risk.current_exposure],
    ["Open positions", risk.open_positions],
    ["Max positions", risk.max_positions ?? risk.max_open_positions],
    ["RiskGuard status", risk.status_label || risk.status],
    ["Capital profile", risk.capital_profile],
    ["Ruleset", risk.ruleset],
  ];
  const html = kvHtml(rows);
  document.getElementById("risk-panel").innerHTML = html;
  document.getElementById("risk-page").innerHTML = html +
    `<p class="muted" style="margin-top:0.75rem">${esc(risk.note || "")}</p>`;
}

function renderAgentsPage(agents) {
  const el = document.getElementById("agents-page");
  if (!agents.available && !(agents.decisions || []).length) {
    el.innerHTML = `<div class="empty">${esc(agents.message || "AI decision data not available")}</div>
      <div class="kv" style="margin-top:0.75rem">${kvHtml([
        ["Model provider", agents.model_provider],
        ["TradingAgents enabled", agents.tradingagents_enabled],
      ])}</div>`;
    return;
  }
  el.innerHTML = document.getElementById("ceo-panel").innerHTML;
}

function renderPlaceholder(id, payload) {
  const el = document.getElementById(id);
  el.innerHTML = `<div class="empty">${esc(payload.message || "NOT AVAILABLE")}</div>
    <p class="muted">${esc(payload.note || payload.label || "")}</p>`;
}

const ZERODHA_LS_KEY = "grow.zerodha.creds.v1";

function loadBrowserCreds() {
  try {
    const raw = localStorage.getItem(ZERODHA_LS_KEY);
    if (!raw) return { api_key: "", api_secret: "" };
    const parsed = JSON.parse(raw);
    return {
      api_key: String(parsed.api_key || ""),
      api_secret: String(parsed.api_secret || ""),
    };
  } catch (_) {
    return { api_key: "", api_secret: "" };
  }
}

function saveBrowserCreds(api_key, api_secret) {
  const prev = loadBrowserCreds();
  const next = {
    api_key: api_key || prev.api_key || "",
    api_secret: api_secret || prev.api_secret || "",
  };
  if (!next.api_key && !next.api_secret) {
    localStorage.removeItem(ZERODHA_LS_KEY);
    return next;
  }
  localStorage.setItem(ZERODHA_LS_KEY, JSON.stringify(next));
  return next;
}

function fillCredInputsFromBrowser() {
  const creds = loadBrowserCreds();
  const keyInput = document.getElementById("zerodha-api-key");
  const secretInput = document.getElementById("zerodha-api-secret");
  if (!keyInput || !secretInput) return;
  if (!keyInput.value && creds.api_key) keyInput.value = creds.api_key;
  if (!secretInput.value && creds.api_secret) secretInput.value = creds.api_secret;
}

async function postJson(path, body) {
  const res = await fetch(path, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `${path} → ${res.status}`);
    err.payload = data;
    throw err;
  }
  return data;
}

function statusClass(status) {
  if (status === "CONNECTED") return "status-connected";
  if (status === "NOT_CONFIGURED") return "status-not-configured";
  return "status-disconnected";
}

function wireZerodhaForm() {
  const keyInput = document.getElementById("zerodha-api-key");
  const secretInput = document.getElementById("zerodha-api-secret");
  if (!keyInput || !secretInput || keyInput.dataset.wired === "1") return;
  keyInput.dataset.wired = "1";
  fillCredInputsFromBrowser();
  const persist = () => {
    saveBrowserCreds(keyInput.value.trim(), secretInput.value.trim());
    const msg = document.getElementById("zerodha-save-msg");
    if (msg && (keyInput.value.trim() || secretInput.value.trim())) {
      msg.textContent = "Remembered in this browser.";
    }
  };
  keyInput.addEventListener("change", persist);
  secretInput.addEventListener("change", persist);
  keyInput.addEventListener("blur", persist);
  secretInput.addEventListener("blur", persist);
}

async function connectZerodha(connectPath) {
  const msg = document.getElementById("zerodha-save-msg");
  const keyInput = document.getElementById("zerodha-api-key");
  const secretInput = document.getElementById("zerodha-api-secret");
  // Browser password managers often show dots while .value is still empty.
  // Focus/blur forces some managers to commit the real value.
  if (keyInput) {
    keyInput.focus();
    keyInput.dispatchEvent(new Event("input", { bubbles: true }));
  }
  if (secretInput) {
    secretInput.focus();
    secretInput.dispatchEvent(new Event("input", { bubbles: true }));
  }
  if (secretInput) secretInput.blur();

  const cached = loadBrowserCreds();
  let api_key = ((keyInput && keyInput.value.trim()) || cached.api_key || "").trim();
  let api_secret = ((secretInput && secretInput.value.trim()) || cached.api_secret || "").trim();
  if (!api_key || !api_secret) {
    if (msg) {
      msg.textContent =
        "Paste real API key and API secret (password autofill dots are not enough), then Connect.";
    }
    if (!api_key && keyInput) keyInput.focus();
    else if (secretInput) secretInput.focus();
    return;
  }
  saveBrowserCreds(api_key, api_secret);
  if (msg) msg.textContent = "Connecting…";
  try {
    await postJson("/api/zerodha/credentials", { api_key, api_secret });
    window.location.href = connectPath || "/api/zerodha/connect";
  } catch (err) {
    if (msg) msg.textContent = err.message || "Connect failed";
  }
}

function renderZerodha(z) {
  const status = z.status || "DISCONNECTED";
  const browser = loadBrowserCreds();
  document.getElementById("zerodha-status").innerHTML = kvHtml([
    ["Provider", z.provider || "zerodha"],
    ["Purpose", z.purpose || "market_data_only"],
    ["Status", status],
    ["Browser cache", browser.api_key && browser.api_secret ? "yes" : "partial/no"],
    ["API key present", z.api_key_present ? "yes" : "no"],
    ["API secret present", z.api_secret_present ? "yes" : "no"],
    ["Access token present", z.access_token_present ? "yes" : "no"],
    ["Redirect URI", z.redirect_uri || ""],
  ]);
  const pill = `<span class="status-pill ${statusClass(status)}">${esc(status)}</span>`;
  document.getElementById("zerodha-message").innerHTML =
    `${pill} <span style="margin-left:0.5rem">${esc(z.message || "")}</span>`;

  const actions = document.getElementById("zerodha-actions");
  const connectPath = z.connect_path || "/api/zerodha/connect";
  const disconnectPath = z.disconnect_path || "/api/zerodha/disconnect";
  // Always enable Connect — credentials may only exist in the input/browser cache.
  actions.innerHTML = `
    <button type="button" class="btn btn-primary" id="btn-zerodha-connect">Connect / Authorise</button>
    <button type="button" class="btn btn-danger" id="btn-zerodha-disconnect">
      Disconnect
    </button>
    <button type="button" class="btn" id="btn-zerodha-refresh">Refresh status</button>`;

  document.getElementById("btn-zerodha-connect").addEventListener("click", () => {
    connectZerodha(connectPath);
  });
  document.getElementById("btn-zerodha-disconnect").addEventListener("click", async () => {
    await getJson(disconnectPath);
    await refresh({ probe: true });
  });
  document.getElementById("btn-zerodha-refresh").addEventListener("click", async () => {
    await refresh({ probe: true });
  });
  wireZerodhaForm();
}

function activateView(view) {
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("active", section.id === `view-${view}`);
  });
}

function wireNav() {
  const buttons = document.querySelectorAll(".nav-item");
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      activateView(btn.dataset.view);
      const url = new URL(window.location.href);
      url.searchParams.set("view", btn.dataset.view);
      window.history.replaceState({}, "", url);
    });
  });
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");
  if (view) activateView(view);
}

async function refresh(opts = {}) {
  const data = await getJson("/api/dashboard");
  state.dashboard = data;
  renderMarket(data.market || {});
  renderSystem(data.system || {});
  renderCapital(data.capital || {});
  renderCeo(data.agents || {});
  renderOption(data.option_chain || {});
  const posHtml = positionsTable(data.positions || {});
  document.getElementById("positions-panel").innerHTML = posHtml;
  document.getElementById("positions-page").innerHTML = posHtml;
  renderRisk(data.risk || {});
  renderAgentsPage(data.agents || {});
  renderPlaceholder("signals-page", data.signals || {});
  renderPlaceholder("research-page", data.research || {});
  // Prefer verified Zerodha block embedded in /api/dashboard market payload so
  // Settings cannot flip CONNECTED → DISCONNECTED on unprobed refresh.
  const zFromMarket = (data.market && data.market.zerodha) || {
    status: data.market && data.market.zerodha_status,
    access_token_present: undefined,
    purpose: "market_data_only",
  };
  const settings = await getJson("/api/settings");
  document.getElementById("settings-page").textContent = JSON.stringify(settings, null, 2);
  // Prefer dashboard market.zerodha (verified) when present so Settings never
  // paints an unverified DISCONNECTED over LIVE market status.
  const zMarket = data.market && data.market.zerodha;
  const zSettings = settings.zerodha_market_data;
  const zPick =
    zMarket && String(zMarket.status || "").toUpperCase() === "CONNECTED"
      ? zMarket
      : zSettings || zMarket || zFromMarket || {};
  renderZerodha(zPick);
}

wireNav();
refresh().catch((err) => {
  document.getElementById("ceo-panel").innerHTML =
    `<div class="empty">Failed to load dashboard: ${esc(err.message)}</div>`;
});

window.addEventListener("message", (ev) => {
  if (ev.origin !== window.location.origin) return;
  if (ev.data && ev.data.type === "zerodha-auth") {
    refresh().catch(() => {});
  }
});

setInterval(() => {
  refresh().catch(() => {});
}, 15000);
