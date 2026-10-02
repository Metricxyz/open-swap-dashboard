import assert from "node:assert/strict";
import test, { after, beforeEach, mock } from "node:test";


class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  contains(value) { return this.values.has(value); }
  toggle(value, force) {
    const enabled = force === undefined ? !this.values.has(value) : force;
    if (enabled) this.values.add(value); else this.values.delete(value);
    return enabled;
  }
}

class FakeNode {
  constructor() {
    this.classList = new FakeClassList();
    this.textContent = "";
    this.className = "";
    this.innerHTML = "";
    this.disabled = false;
    this.dataset = {};
  }
  addEventListener() {}
  focus() {}
  querySelectorAll() { return []; }
  setAttribute() {}
}

const nodes = new Map();
const node = (selector) => {
  if (!nodes.has(selector)) nodes.set(selector, new FakeNode());
  return nodes.get(selector);
};

globalThis.document = {
  body: new FakeNode(),
  addEventListener() {},
  querySelector: node,
  querySelectorAll: () => [],
};
globalThis.localStorage = {
  getItem: () => null,
  setItem() {},
  removeItem() {},
};
globalThis.window = { innerWidth: 1200, addEventListener() {} };

const {
  configurePlots, discoverPool, formatHoursAgo, formatWindowLabel, loadDashboard, notice, changeSwapPage, applyChartRange, pollStatus,
  editAddressLabel, loadKnownAddresses, loadMetadataPools, parseWindowHours, renderMetadataPools, selectMetadataPool, refreshRuntime, removePool, renderCounterparties, renderRpcSettings, renderSwaps,
  selectWindow, state, syncPriceInputs, toggleChartFullscreen, toggleCounterpartySide, selectListPnlMode,
} = await import("../app.js");


after(() => clearTimeout(state.noticeTimer));


function response(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    headers: { get: () => "application/json" },
    json: async () => payload,
  };
}

beforeEach(() => {
  nodes.clear();
  state.status = null;
  state.statusError = null;
  document.hidden = false;
  state.dashboardEtag = null;
  state.dashboardUrl = null;
  state.chartRenderKeys = {};
  state.pageFlight = false;
  state.bootstrap = {
    csrf_token: "csrf",
    chains: [],
    pools: [{ id: "pool-a", name: "Pool A", paused: false, status: "caught_up" }],
  };
  state.selectedPoolId = "pool-a";
  state.editingPoolId = null;
  state.view = "dashboard";
  state.hours = 24;
  state.listPnlMode = "flow";
  state.plots = ["pnl", "price"];
  state.hideSmallSwaps = false;
  state.minimumSwapUsd = 100;
  state.currentDashboard = null;
  state.chartRange = null;
  state.chartRangeContext = null;
  state.swapPage = 0;
  state.uiLocked = false;
  globalThis.confirm = () => true;
  globalThis.prompt = () => "pool-a";
  globalThis.localStorage.setItem = () => {};
});


test("live status owns pricing activity, elapsed time, errors, and disconnected display", async () => {
  const status = {worker_alive: true, pools: {'pool-a': {
    id: 'pool-a', name: 'Pool A',
    ingestion: {state: 'caught_up'},
    pricing: {state: 'working', action: 'Reading trades', elapsed_seconds: 75,
      taking_longer_than_expected: true, started_at: '2099-01-01T00:00:00Z'},
  }}, operation: {phase: 'pricing', pool_id: 'pool-a', message: 'Reading trades'}};
  globalThis.fetch = async () => response(status);
  await pollStatus();
  clearTimeout(state.statusTimer);
  assert.equal(node('#systemStatus').textContent, '1 ACTIVE');
  assert.match(node('#systemActivity').textContent, /PRICING/);
  assert.match(node('#activityStatus').innerHTML, /taking longer than expected \(75s\)/);
  status.pools['pool-a'].pricing.error = 'Provider unavailable';
  status.pools['pool-a'].pricing.state = 'error';
  status.pools['pool-a'].pricing.taking_longer_than_expected = false;
  await pollStatus();
  clearTimeout(state.statusTimer);
  assert.equal(node('#systemStatus').textContent, 'ATTENTION');
  assert.match(node('#activityStatus').innerHTML, /Provider unavailable/);
  globalThis.fetch = async () => { throw new Error('offline'); };
  await pollStatus();
  clearTimeout(state.statusTimer);
  assert.equal(node('#systemStatus').textContent, 'CONNECTION LOST');
  assert.match(node('#activityStatus').innerHTML, /Provider unavailable/);
});


test("plot configuration persists choices and keeps at least one plot visible", () => {
  let stored;
  globalThis.localStorage.setItem = (key, value) => { stored = [key, value]; };
  const price = { dataset: { plot: "price" }, checked: false };
  configurePlots({ target: price });
  assert.deepEqual(state.plots, ["pnl"]);
  assert.deepEqual(stored, ["openPoolPlots", "pnl"]);

  const pnl = { dataset: { plot: "pnl" }, checked: false };
  configurePlots({ target: pnl });
  assert.equal(pnl.checked, true);
  assert.deepEqual(state.plots, ["pnl"]);

  configurePlots({ target: { id: "hideSmallSwaps", checked: true } });
  assert.equal(state.hideSmallSwaps, true);
  assert.deepEqual(stored, ["openPoolHideSmallSwaps", "true"]);

  configurePlots({ target: { id: "minimumSwapUsd", value: "250" } });
  assert.equal(state.minimumSwapUsd, 250);
  assert.deepEqual(stored, ["openPoolMinimumSwapUsd", "250"]);

  const invalid = { id: "minimumSwapUsd", value: "-1" };
  configurePlots({ target: invalid });
  assert.equal(invalid.value, 250);
  assert.equal(state.minimumSwapUsd, 250);
});


test("chart fullscreen control enters and exits its panel", async () => {
  let entered = 0;
  let exited = 0;
  const panel = { requestFullscreen: async () => { entered += 1; } };
  const button = { closest: (selector) => selector === ".chart-panel" ? panel : null };
  const event = { target: { closest: (selector) => selector === "[data-fullscreen-chart]" ? button : null } };

  document.fullscreenElement = null;
  document.exitFullscreen = async () => { exited += 1; };
  await toggleChartFullscreen(event);
  assert.equal(entered, 1);

  document.fullscreenElement = panel;
  await toggleChartFullscreen(event);
  assert.equal(exited, 1);
  document.fullscreenElement = null;
});


test("a stale dashboard response cannot overwrite a newly selected pool", async () => {
  let resolveFetch;
  globalThis.fetch = () => new Promise((resolve) => { resolveFetch = resolve; });

  const pending = loadDashboard();
  state.selectedPoolId = "pool-b";
  resolveFetch(response({ pool: { name: "STALE" } }));
  await pending;

  assert.equal(node("#workspaceTitle").textContent, "");
  assert.equal(state.currentDashboard, null);
});


test("runtime refresh pauses while locked and does not load a dashboard from another view", async () => {
  state.status = { worker_alive: true, pools: {}, operation: {
    phase: "syncing", message: "SYNCING POOL A — FETCHING LOGS — 42%",
    updated_at: "2026-08-20T12:00:00Z",
  }};
  let calls = 0;
  globalThis.fetch = async (path) => {
    calls += 1;
    assert.equal(path, "/api/bootstrap?hours=24");
    return response({
      csrf_token: "next", chains: [], pools: [], total_summary: {},
      backend_activity: {
        phase: "syncing", message: "SYNCING POOL A — FETCHING LOGS — 42%",
        updated_at: "2026-08-20T12:00:00Z",
      },
    });
  };

  state.uiLocked = true;
  await refreshRuntime();
  assert.equal(calls, 0);

  state.uiLocked = false;
  state.view = "pool-form";
  await refreshRuntime();
  assert.equal(calls, 1);
  assert.equal(state.view, "pool-form");
  assert.equal(node("#backendActivity").textContent, "SYNCING POOL A — FETCHING LOGS — 42%");
  assert.equal(node("#backendActivity").className, "working");
  assert.equal(node("#footerActivity").textContent, "SYNCING POOL A — FETCHING LOGS — 42%");
});


test("pool removal requires confirmation and reports failures", async () => {
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options, locked: state.uiLocked });
    return response({ detail: "offline" }, { ok: false, status: 503 });
  };

  globalThis.confirm = () => false;
  await removePool("pool-a");
  assert.equal(calls.length, 0);

  globalThis.confirm = () => true;
  await removePool("pool-a");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/pools/pool-a");
  assert.equal(calls[0].options.method, "DELETE");
  // The slow backend delete runs behind the lock overlay, which is released when it fails.
  assert.equal(calls[0].locked, true);
  assert.equal(state.uiLocked, false);
  assert.equal(node("#notice").textContent, "offline");
});


test("successful removal clears selection before a failed refresh", async () => {
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (calls.length === 1) return response({ ok: true });
    throw new Error("bootstrap unavailable");
  };

  await removePool("pool-a");

  assert.equal(calls[0].path, "/api/pools/pool-a");
  assert.equal(calls[0].options.method, "DELETE");
  assert.equal(state.selectedPoolId, null);
});


test("pool navigator renders window metrics, TVL, and one grand total", async () => {
  state.view = "pool-form";
  globalThis.fetch = async () => response({
    csrf_token: "next",
    chains: [{ key: "base", name: "Base" }],
    pools: [{
      id: "pool-a", name: "WETH/USDC", chain_key: "base", status: "processing",
      summary: { volume_usd: 3_790_000, flow_pnl_usd: 518, flow_apr_pct: 610.2, mtm_pnl_usd: 321, mtm_apr_pct: 378.4, tvl_usd: 31_000, last_swap_at: "2026-08-31T11:00:00Z" },
    }],
    total_summary: { volume_usd: 3_790_000, flow_pnl_usd: 518, flow_apr_pct: 610.2, mtm_pnl_usd: 321, mtm_apr_pct: 378.4, tvl_usd: 31_000 },
  });

  await refreshRuntime();

  const markup = node("#poolManagement").innerHTML;
  assert.match(markup, /<table class="pool-table mono">/);
  assert.match(markup, /24H VOL/);
  assert.match(markup, /FLOW PNL/);
  assert.match(markup, /WETH\/USDC/);
  assert.match(markup, /CHECKING/);
  assert.match(markup, /\+\$518/);
  assert.match(markup, /610\.2%/);
  assert.match(markup, /TVL/);
  assert.match(markup, /LAST SWAP<br>HRS AGO/);
  assert.match(markup, />TOTAL</);
  assert.doesNotMatch(markup, /subtotal/);

  selectListPnlMode("mtm");
  const mtmMarkup = node("#poolManagement").innerHTML;
  assert.match(mtmMarkup, /MTM PNL/);
  assert.match(mtmMarkup, /MTM APR/);
  assert.match(mtmMarkup, /\+\$321/);
  assert.match(mtmMarkup, /378\.4%/);
  assert.equal(node("#listPnlMtm").classList.contains("selected"), true);
  assert.equal(node("#listPnlFlow").classList.contains("selected"), false);
  selectListPnlMode("mtm");
  assert.equal(state.listPnlMode, "mtm");
  selectListPnlMode("flow");
  assert.equal(node("#listPnlFlow").classList.contains("selected"), true);
  assert.equal(node("#listPnlMtm").classList.contains("selected"), false);
});


test("data column shows how much of the last 3 days ingestion has committed", async () => {
  state.view = "pool-form";
  globalThis.fetch = async () => response({
    csrf_token: "next",
    chains: [{ key: "base", name: "Base", seconds_per_block: 2 }],
    pools: [
      { id: "old", name: "OLD", chain_key: "base", start_block: 0, summary: {} },
      { id: "new", name: "NEW", chain_key: "base", start_block: 190_000, summary: {} },
      { id: "unsynced", name: "UNSYNCED", chain_key: "base", start_block: 0, summary: {} },
    ],
    total_summary: {},
  });
  state.status = { worker_alive: true, operation: {}, pools: {
    // 3 days of Base blocks = 129,600, so the window is blocks 70,400..200,000.
    // After downtime the saved target is the old head, so there's no honest percentage until the new head is fetched.
    old: { ingestion: { state: "queued", indexed_block: 200_000, target_block: 200_000 }, pricing: { state: "caught_up" } },
    // Started after the window opened, so only blocks 190,000..199,999 count.
    new: { ingestion: { state: "working", indexed_block: 194_999, target_block: 199_999 }, pricing: { state: "caught_up" } },
    unsynced: { ingestion: { state: "queued", indexed_block: -1, target_block: null }, pricing: { state: "queued" } },
  } };

  await refreshRuntime();

  const markup = node("#poolManagement").innerHTML;
  assert.match(markup, />DATA</);
  assert.doesNotMatch(markup, />SYNC<|>STATUS</);
  assert.match(markup, /data-id="old"[\s\S]*?>INDEXING<\/td>/);
  assert.match(markup, /data-id="new"[\s\S]*?title="Indexed through block 194,999 of 199,999 \(last 3 days\)">INDEXING 50%</);
  assert.match(markup, /data-id="unsynced"[\s\S]*?>INDEXING<\/td>/);
  state.status = null;
});

test("missing PnL shows a question mark that explains why", async () => {
  state.view = "pool-form";
  state.listPnlMode = "flow";
  const assets = (kind) => [{ symbol: "WETH", price_kind: "binance" }, { symbol: "TOKEN", price_kind: kind }];
  globalThis.fetch = async () => response({
    csrf_token: "next",
    chains: [{ key: "base", name: "Base" }],
    pools: [
      { id: "idle", name: "IDLE", chain_key: "base", assets: assets("fixed"), summary: { swap_count: 0 } },
      { id: "unpriced", name: "UNPRICED", chain_key: "base", assets: assets("none"), summary: { swap_count: 3 } },
      { id: "priced", name: "PRICED", chain_key: "base", assets: assets("fixed"), summary: { swap_count: 3, flow_pnl_usd: 5, flow_complete: true } },
    ],
    total_summary: {},
  });
  state.status = { worker_alive: true, operation: {}, pools: {
    idle: { ingestion: { state: "working", indexed_block: 1, target_block: 2 }, pricing: { state: "caught_up" } },
  } };

  await refreshRuntime();

  const row = (id) => node("#poolManagement").innerHTML.match(new RegExp(`data-id="${id}"[\\s\\S]*?</tr>`))[0];
  assert.match(row("idle"), /class="missing-hint" title="No swaps in the last 24H\. Ingestion hasn&#39;t caught up yet\."/);
  assert.match(row("unpriced"), /title="Flow PnL needs a price source for both assets; TOKEN has none\."/);
  assert.doesNotMatch(row("priced"), /missing-hint/);
  state.status = null;
});

test("data column moves on to pricing progress, then up to date, and surfaces errors", async () => {
  state.view = "pool-form";
  globalThis.fetch = async () => response({
    csrf_token: "next",
    chains: [{ key: "base", name: "Base" }],
    pools: [
      { id: "backlog", name: "BACKLOG", chain_key: "base", summary: {} },
      { id: "done", name: "DONE", chain_key: "base", summary: {} },
      { id: "broken", name: "BROKEN", chain_key: "base", summary: {} },
      { id: "paused", name: "PAUSED", chain_key: "base", summary: {} },
    ],
    total_summary: {},
  });
  state.status = { worker_alive: true, operation: {}, pools: {
    backlog: { ingestion: { state: "caught_up" }, pricing: { state: "waiting", pending_prices: 64, price_targets: 100 } },
    done: { ingestion: { state: "caught_up" }, pricing: { state: "caught_up", pending_prices: 0, price_targets: 100 } },
    broken: { ingestion: { state: "error", error: "RPC unreachable" }, pricing: { state: "caught_up" } },
    paused: { ingestion: { state: "paused" }, pricing: { state: "paused" } },
  } };

  await refreshRuntime();

  const markup = node("#poolManagement").innerHTML;
  assert.match(markup, /data-id="backlog"[\s\S]*?pool-status working" title="36 of 100 prices fetched">PRICING 36%</);
  assert.match(markup, /data-id="done"[\s\S]*?pool-status good" title="">UP TO DATE</);
  assert.match(markup, /data-id="broken"[\s\S]*?pool-status bad" title="RPC unreachable">INGESTION ERROR</);
  assert.match(markup, /data-id="paused"[\s\S]*?pool-status idle" title="">PAUSED</);
  state.status = null;
});

test("last swap age is formatted in hours", () => {
  const now = Date.parse("2026-08-31T12:00:00Z");
  assert.equal(formatHoursAgo("2026-08-31T10:30:00Z", now), "1.5");
  assert.equal(formatHoursAgo("2026-08-31T11:59:00Z", now), "<0.1");
  assert.equal(formatHoursAgo(null, now), "—");
});


test("swap references show an available price when the other asset is unpriced", () => {
  renderSwaps({
    pool: { assets: [
      { symbol: "AI", price_kind: "none" },
      { symbol: "WETH", price_kind: "binance" },
    ] },
    explorer_tx_url: "https://explorer.test/tx/{tx_hash}",
    explorer_address_url: "https://explorer.test/address/{address}",
    swap_page: {total: 1, page: 0, page_size: 100, items: [{
      tx_hash: "0xtx", timestamp: "2026-08-31T12:00:00Z", block_number: 1,
      counterparty: "0x0000000000000000000000000000000000000011",
      asset0_delta: 100, asset1_delta: -1, asset0_price: null, asset1_price: 2406.95,
      flow_pnl: null, edge_bps: null, price_lag_ms: null,
    }]},
  });

  const markup = node("#swapsBody").innerHTML;
  assert.match(markup, /— \/ 2,406\.95/);
  assert.match(markup, /title="AI: no price source"/);
  assert.match(markup, /https:\/\/explorer\.test\/address\/0x0{38}11/);
  assert.doesNotMatch(markup, /UNPRICED/);
});


test("counterparty rendering follows the visible chart range", () => {
  const included = "0x0000000000000000000000000000000000000011";
  const excluded = "0x0000000000000000000000000000000000000022";
  state.chartRange = ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"];
  renderCounterparties({
    explorer_address_url: "https://explorer.test/address/{address}",
    counterparties: {total_swaps: 1, total_counterparties: 1, rows: [
      {address: included, swap_count: 1, volume_usd: 100, avg_edge_bps: 5, swap_share_pct: 100, volume_share_pct: 100},
    ]},
  });

  assert.equal(node("#counterpartyCount").textContent, "1 SWAPS · 1 COUNTERPARTY");
  assert.match(node("#counterpartiesBody").innerHTML, new RegExp(included));
  assert.doesNotMatch(node("#counterpartiesBody").innerHTML, new RegExp(excluded));
});


test("counterparty side toggle switches the grouping, persists it, and shows unique signers", () => {
  const stored = [];
  globalThis.localStorage.setItem = (...args) => stored.push(args);
  state.counterpartySide = "recipient";
  let prevented = false;
  toggleCounterpartySide({ preventDefault: () => { prevented = true; } });
  assert.ok(prevented, "the click must not also collapse the panel");
  assert.equal(state.counterpartySide, "sender");
  assert.deepEqual(stored.at(-1), ["openPoolCounterpartySide", "sender"]);

  renderCounterparties({
    explorer_address_url: "https://explorer.test/address/{address}",
    counterparties: { side: "sender", total_swaps: 3, total_counterparties: 1, rows: [
      { address: "0x" + "11".repeat(20), swap_count: 3, unique_signers: 2, volume_usd: 300, avg_edge_bps: null, swap_share_pct: 100, volume_share_pct: 100 },
    ] },
  });
  assert.equal(node("#counterpartySide").textContent, "SENT TO POOL");
  assert.match(node("#counterpartiesBody").innerHTML, /<td>3<\/td>\s*<td>2<\/td>/);
  globalThis.localStorage.setItem = () => {};
  state.counterpartySide = "recipient";
});


test("known aggregator addresses are tagged with their own style and user labels override them", async () => {
  const address = "0x" + "cd".repeat(20);
  globalThis.fetch = async (url) => {
    assert.equal(url, "/static/known_aggregators.json");
    return { ok: true, json: async () => ({ "0x API": [address.toUpperCase().replace("0X", "0x")] }) };
  };
  state.addressLabels = {};
  state.currentDashboard = {
    pool: { assets: [{ symbol: "A" }, { symbol: "B" }] },
    explorer_tx_url: "https://explorer.test/tx/{tx_hash}",
    explorer_address_url: "https://explorer.test/address/{address}",
    counterparties: { side: "recipient", total_swaps: 1, total_counterparties: 1, rows: [
      { address, swap_count: 1, volume_usd: 1, avg_edge_bps: null, swap_share_pct: 100, volume_share_pct: 100 },
    ] },
    swap_page: { total: 0, page: 0, page_size: 100, items: [] },
  };

  await loadKnownAddresses();
  assert.match(node("#counterpartiesBody").innerHTML, /class="address-label known-address"[^>]*>0x API ↗/);

  state.addressLabels = { [address]: "Mine" };
  renderCounterparties(state.currentDashboard);
  assert.match(node("#counterpartiesBody").innerHTML, /class="address-label"[^>]*>Mine ↗/);
  state.addressLabels = {};
  state.knownAddresses = {};
  delete globalThis.fetch;
});


test("address labels are saved in browser storage and replace the short address everywhere", () => {
  const address = "0x" + "ab".repeat(20);
  const stored = [];
  globalThis.localStorage.setItem = (...args) => stored.push(args);
  state.addressLabels = {};
  state.currentDashboard = {
    pool: { assets: [{ symbol: "A" }, { symbol: "B" }] },
    explorer_tx_url: "https://explorer.test/tx/{tx_hash}",
    explorer_address_url: "https://explorer.test/address/{address}",
    counterparties: { side: "recipient", total_swaps: 1, total_counterparties: 1, rows: [
      { address, swap_count: 1, volume_usd: 1, avg_edge_bps: null, swap_share_pct: 100, volume_share_pct: 100 },
    ] },
    swap_page: { total: 1, page: 0, page_size: 100, items: [{ tx_hash: "0xtx", timestamp: "2026-08-31T12:00:00Z",
      counterparty: address, counterparty_rank: 3, asset0_delta: 1, asset1_delta: -1, asset0_price: 1, asset1_price: 1 }] },
  };
  const click = { target: { closest: () => ({ dataset: { labelAddress: address } }) } };

  globalThis.prompt = () => "  <Arb bot>  ";
  editAddressLabel(click);
  assert.deepEqual(stored.at(-1), ["openPoolAddressLabels", JSON.stringify({ [address]: "<Arb bot>" })]);
  for (const body of ["#counterpartiesBody", "#swapsBody"]) {
    assert.match(node(body).innerHTML, /&lt;Arb bot&gt; ↗/);
    assert.match(node(body).innerHTML, new RegExp(`title="${address}"`));
  }

  assert.match(node("#swapsBody").innerHTML, /class="counterparty-rank mono"[^>]*>TOP #3<\/span>/);

  globalThis.prompt = () => "";
  editAddressLabel(click);
  assert.deepEqual(stored.at(-1), ["openPoolAddressLabels", "{}"]);
  assert.match(node("#swapsBody").innerHTML, /0xababab…ababab ↗/);
  globalThis.localStorage.setItem = () => {};
});


test("metadata pools are grouped by supported chain, searchable, and fill the form when selected", async () => {
  const address = "0x" + "12".repeat(20);
  const trackedAddress = "0x" + "34".repeat(20);
  state.bootstrap.chains = [{ key: "base", name: "Base", chain_id: 8453 }, { key: "ethereum", name: "Ethereum", chain_id: 1 }];
  state.bootstrap.pools = [{ id: "pool-a", chain_key: "base", address: trackedAddress }];
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push([url, options]);
    if (url.endsWith("/chains")) return response({ chains: [
      { type: "evm", chainId: 8453, name: "base" }, { type: "evm", chainId: 999, name: "hyperevm" }, { type: "evm", chainId: 1, name: "ethereum" },
    ] });
    if (url.includes("/evm/1/")) return response({ data: [] });
    if (url.includes("/evm/8453/")) return response({ data: [
      { pair: "WETH/USDC", poolAddress: address, createdBlockNumber: 123, aprBps7d: 3301, adminUsername: "Example Admin" },
      { pair: "cbBTC/USDC", poolAddress: trackedAddress, createdBlockNumber: 1, aprBps7d: null, admin: "0x" + "56".repeat(20) },
    ] });
    return response({ assets: [] });
  };

  await loadMetadataPools();
  assert.deepEqual(requests.map(([url]) => url), [
    "https://api.metric.xyz/public/v1/chains",
    "https://api.metric.xyz/public/v1/evm/8453/metadata?count=50&sortBy=tvl",
    "https://api.metric.xyz/public/v1/evm/1/metadata?count=50&sortBy=tvl",
  ]);
  const markup = node("#metadataPools").innerHTML;
  assert.match(markup, /<details><summary>Base <b>2 POOLS<\/b>/);
  assert.doesNotMatch(markup, /Ethereum/, "chains without pools are omitted");
  assert.doesNotMatch(markup, /APR/);
  assert.match(markup, /<i>ADMIN<\/i> Example Admin/);
  assert.match(markup, new RegExp(`data-address="${trackedAddress}"[^>]*disabled[\\s\\S]*TRACKED`));

  node("#metadataSearch").value = "EXAMPLE ADMIN";
  renderMetadataPools();
  assert.match(node("#metadataPools").innerHTML, /<details open><summary>Base <b>1 POOL<\/b>/);
  node("#metadataSearch").value = "nothing";
  renderMetadataPools();
  assert.match(node("#metadataPools").innerHTML, /NO MATCHING POOLS/);

  requests.length = 0;
  selectMetadataPool({ target: { closest: () => ({ disabled: false, dataset: { chainKey: "base", address, startBlock: "123" } }) } });
  assert.equal(node("#poolChainInput").value, "base");
  assert.equal(node("#poolAddressInput").value, address);
  assert.equal(node("#startBlockInput").value, "123");
  assert.ok(node("#metadataPanel").classList.contains("hidden"));
  assert.equal(requests[0][0], "/api/pools/discover");
  assert.deepEqual(JSON.parse(requests[0][1].body), { chain_key: "base", address });
});


test("custom windows accept hours and days within the supported range", () => {
  assert.equal(parseWindowHours("12h"), 12);
  assert.equal(parseWindowHours("3d"), 72);
  assert.equal(parseWindowHours("1.5d"), 36);
  assert.equal(parseWindowHours("31d"), null);
  assert.equal(parseWindowHours("nope"), null);
  assert.equal(formatWindowLabel(36), "36H");
  assert.equal(formatWindowLabel(168), "7D");
});


test("price inputs show only what the chosen source uses", () => {
  const input = () => ({ disabled: false, label: new FakeNode(), closest() { return this.label; } });
  const source = { value: "none" }, fixed = input(), market = input(), type = input();
  const field = {
    querySelector: (selector) => selector === ".price-kind" ? source : fixed,
    querySelectorAll: () => [market, type],
  };
  const visible = () => [fixed, market, type].map((item) => !item.label.classList.contains("hidden"));

  syncPriceInputs(field);
  assert.deepEqual(visible(), [false, false, false]);
  assert.equal(fixed.disabled, true);

  source.value = "fixed";
  syncPriceInputs(field);
  assert.deepEqual(visible(), [true, false, false]);
  assert.equal(fixed.disabled, false);

  source.value = "binance";
  syncPriceInputs(field);
  assert.deepEqual(visible(), [false, true, true]);
  assert.equal(fixed.disabled, true);
});


test("time-window changes show the quiet overlay and lock until refresh finishes", async () => {
  const runChange = async (stateKey, storageKey, controlsId, expectedLabel) => {
    let finish;
    const controls = { id: controlsId };
    const button = {
      dataset: { hours: "6" },
      hasAttribute: () => false,
      closest: () => controls,
    };
    const pending = selectWindow(
      { target: { closest: () => button } }, stateKey, storageKey,
      () => new Promise((resolve) => { finish = resolve; }),
    );

    assert.equal(state.uiLocked, true);
    assert.equal(node(".app-shell").inert, true);
    assert.equal(node("#uiLock").classList.contains("hidden"), false);
    finish();
    await pending;
    assert.equal(state.uiLocked, false);
  };

  await runChange("hours", "openPoolWindowHours", "windowControls", "LOADING POOL WINDOW");
  await runChange("listHours", "openPoolListWindowHours", "listWindowControls", "LOADING POOL SUMMARIES");
});


test("RPC settings show every chain without a selector", () => {
  state.bootstrap.envio = { configured: true, rpm_limit: 30 };
  state.bootstrap.chains = [
    { key: "ethereum", name: "Ethereum", public_rpcs: ["https://eth.test"], has_custom_rpc: false },
    { key: "base", name: "Base", public_rpcs: ["https://base.test"], has_custom_rpc: true, custom_rpc_display: "https://custom.test/…" },
  ];

  renderRpcSettings();

  const markup = node("#rpcRows").innerHTML;
  assert.match(markup, /Ethereum/);
  assert.match(markup, /Base/);
  assert.match(markup, /CUSTOM/);
  assert.match(markup, /ACTIVE ENDPOINT/);
  assert.doesNotMatch(markup, /<select/);
  assert.match(node("#envioKeyState").textContent, /configured/);
  assert.equal(node("#envioRpmLimit").value, 30);
  assert.equal(node("#clearEnvioKey").disabled, false);
});


test("failed initial pool load does not mark a healthy indexer unavailable", async () => {
  state.bootstrap = null;
  state.statusError = null;
  state.status = { worker_alive: true, pools: { "pool-a": { id: "pool-a", name: "Pool A", ingestion: {state: "working"}, pricing: {state: "waiting"} } }, operation: { phase: "committing", message: "Committing data" } };
  globalThis.fetch = async () => { throw new Error("Request timed out"); };
  await refreshRuntime();
  assert.equal(node("#systemStatus").textContent, "1 ACTIVE");
  assert.equal(node("#systemPoolCount").textContent, "1");
  assert.equal(node("#backendActivity").textContent, "Committing data");
  state.status = null;
});


test("time-window lock releases when refresh fails", async () => {
  const button = { dataset: { hours: "6" }, hasAttribute: () => false, closest: () => ({ id: "windowControls" }) };
  await assert.rejects(selectWindow(
    { target: { closest: () => button } }, "hours", "openPoolWindowHours",
    async () => { throw new Error("offline"); },
  ), /offline/);
  assert.equal(state.uiLocked, false);
  assert.equal(node(".app-shell").inert, false);
  assert.equal(node("#uiLock").classList.contains("hidden"), true);
});

test('hidden tabs do not refresh dashboard or bootstrap', async () => {
  document.hidden = true;
  let requests = 0;
  globalThis.fetch = async () => { requests++; throw new Error('hidden'); };
  await refreshRuntime();
  assert.equal(requests, 0);
});

test('unchanged dashboard checks retain the displayed page and avoid rendering', async () => {
  state.currentDashboard = {pool: {id: 'pool-a', name: 'Pool A'}, window_hours: 24, metrics: {}};
  state.dashboardFlight = null;
  state.dashboardUrl = '/api/pools/pool-a/dashboard?hours=24&plots=pnl%2Cprice&minimum_swap_usd=0&counterparty_side=recipient';
  state.dashboardEtag = '"same"';
  const retained = state.currentDashboard;
  let headers;
  globalThis.fetch = async (_url, options) => { headers = options.headers; return {status: 304}; };
  await loadDashboard();
  assert.equal(headers['If-None-Match'], '"same"');
  assert.equal(state.currentDashboard, retained);
  assert.equal(state.refreshError, null);
  assert.ok(state.lastRefresh);
});

test('page navigation requests only rows with the displayed revision and range', async () => {
  state.refreshStarted = null;
  state.swapPage = 0;
  state.currentDashboard = {pool: {id: 'pool-a', assets: []}, window_hours: 24,
    window_end: '2026-01-02T00:00:00Z', data_revision: 'r1', swap_page: {items: [], total: 101, page: 0, page_size: 100}};
  state.chartRange = ['2026-01-01T12:00:00Z', '2026-01-02T00:00:00Z'];
  let requested;
  globalThis.fetch = async (url) => { requested = new URL(url, 'http://localhost'); return response({items: [], total: 101, page: 1, page_size: 100}); };
  await changeSwapPage(1);
  assert.equal(requested.pathname, '/api/pools/pool-a/swaps');
  assert.equal(requested.searchParams.get('revision'), 'r1');
  assert.equal(requested.searchParams.get('range_start'), state.chartRange[0]);
  assert.equal(state.swapPage, 1);
});

test('dashboard refresh of the same view keeps the displayed swap page', async () => {
  const viewUrl = '/api/pools/pool-a/dashboard?hours=24&plots=pnl%2Cprice&minimum_swap_usd=0&counterparty_side=recipient';
  state.dashboardFlight = null;
  state.dashboardUrl = viewUrl;
  state.swapPage = 3;
  const requested = [];
  globalThis.fetch = async (url) => { requested.push(new URL(url, 'http://localhost')); return {status: 304}; };
  await loadDashboard();
  state.hours = 48;
  await loadDashboard();
  assert.equal(requested[0].searchParams.get('page'), '3');
  assert.equal(requested[1].searchParams.get('page'), null);
});

test("a poll started before a removal cannot restore the removed pool", async () => {
  const pool = (id) => ({ id, name: id, chain_key: "base", status: "caught_up", summary: {} });
  const bootstrap = (pools) => ({ csrf_token: "csrf", chains: [], pools: pools.map(pool), total_summary: {} });
  state.bootstrap = bootstrap(["pool-a", "pool-b"]);
  state.view = "pool-form";
  let releasePoll;
  let bootstraps = 0;
  globalThis.fetch = async (path, options = {}) => {
    if (options.method === "DELETE") return response({ ok: true });
    if (!path.startsWith("/api/bootstrap")) return { status: 304 };
    if (++bootstraps === 1) return new Promise((resolve) => { releasePoll = () => resolve(response(bootstrap(["pool-a", "pool-b"]))); });
    return response(bootstrap(["pool-b"]));
  };

  const poll = refreshRuntime();
  const removal = removePool("pool-a");
  await new Promise((resolve) => setImmediate(resolve));
  releasePoll();
  await Promise.all([poll, removal]);

  assert.equal(bootstraps, 2);
  assert.deepEqual(state.bootstrap.pools.map((row) => row.id), ["pool-b"]);
  assert.equal(state.selectedPoolId, "pool-b");
});

test("discovery selects the chain where the pool contract was found", async () => {
  state.bootstrap.chains = [{ key: "ethereum", name: "Ethereum" }, { key: "base", name: "Base" }];
  state.discoveredKey = null;
  state.discoveryRequestKey = null;
  node("#poolChainInput").value = "ethereum";
  node("#poolAddressInput").value = "0x00000000000000000000000000000000000000aA";
  node("#poolNameInput").placeholder = "Discover assets first";
  let body, placeholderDuringDiscovery;
  globalThis.fetch = async (_path, options) => {
    body = JSON.parse(options.body);
    placeholderDuringDiscovery = node("#poolNameInput").placeholder;
    return response({ chain_key: "base", generated_name: "WETH/USDC 0x0000...00aA", rpc: "rpc.test",
      assets: [{ symbol: "WETH" }, { symbol: "USDC" }] });
  };

  await discoverPool();

  assert.equal(body.chain_key, "ethereum");
  assert.equal(placeholderDuringDiscovery, "Discovering…");
  assert.equal(node("#poolNameInput").placeholder, "Discover assets first");
  assert.equal(node("#poolChainInput").value, "base");
  assert.equal(state.discoveredKey, "base:0x00000000000000000000000000000000000000aa");
  assert.match(node("#poolFormStatus").textContent, /^NO CONTRACT ON ETHEREUM; SWITCHED TO BASE\. DETECTED WETH \/ USDC/);
});


test("confirmation notices hide on their own while errors stay", () => {
  clearTimeout(state.noticeTimer); // a real timer from earlier tests; the mocked clearTimeout cannot cancel it
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    notice("POOL ADDED. INITIAL SYNCHRONIZATION IS QUEUED.");
    mock.timers.tick(5999);
    assert.ok(!node("#notice").classList.contains("hidden"));
    mock.timers.tick(1);
    assert.ok(node("#notice").classList.contains("hidden"));

    node("#notice").classList.remove("hidden"); // the fake node does not reset classList on className writes
    notice("offline", true);
    mock.timers.tick(60000);
    assert.equal(node("#notice").textContent, "offline");
    assert.ok(!node("#notice").classList.contains("hidden"));
  } finally { mock.timers.reset(); }
});
