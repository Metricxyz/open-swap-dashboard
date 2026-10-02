import { api, setCsrfToken } from "./api.js?v=4";
import { renderBalanceChart, renderCumulativeVolumeChart, renderPnlChart, renderPriceChart, setChartTimeRange } from "./charts.js?v=30";
import { bindAutomaticPoolDiscovery } from "./pool-discovery.js?v=3";

const PLOT_IDS = ["pnl", "price", "balance", "volume"];
const state = {
  bootstrap: null,
  status: null,
  statusError: null,
  refreshError: null,
  refreshStarted: null,
  lastRefresh: null,
  dashboardSequence: 0,
  bootstrapSequence: 0,
  dashboardFlight: null,
  bootstrapFlight: null,
  selectedPoolId: localStorage.getItem("openPoolId"),
  editingPoolId: null,
  discoveredKey: null,
  discoveryRequestKey: null,
  view: "dashboard",
  hours: storedWindow("openPoolWindowHours", 24),
  listHours: storedWindow("openPoolListWindowHours", 24),
  listPnlMode: localStorage.getItem("openPoolListPnlMode") === "mtm" ? "mtm" : "flow",
  plots: storedPlots(),
  hideSmallSwaps: localStorage.getItem("openPoolHideSmallSwaps") === "true",
  counterpartySide: localStorage.getItem("openPoolCounterpartySide") === "sender" ? "sender" : "recipient",
  addressLabels: JSON.parse(localStorage.getItem("openPoolAddressLabels") || "{}"),
  knownAddresses: {},
  minimumSwapUsd: storedMinimumSwapUsd(),
  currentDashboard: null,
  chartRange: null,
  chartRangeContext: null,
  swapPage: 0,
  uiLocked: false,
  metadataGroups: [],
  dashboardEtag: null,
  dashboardUrl: null,
  chartRenderKeys: {},
  statusTimer: null,
  statusFlight: false,
  zoomTimer: null,
  pageSequence: 0,
  resizingColumns: false,
};
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

// ==== Startup and events ====

if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", initialize);

async function initialize() {
  bindEvents();
  pollStatus();
  loadKnownAddresses();
  await Promise.all([refreshBootstrap({ initializeControls: true }), state.selectedPoolId ? loadDashboard() : Promise.resolve()]);
  // Also covers no pools at all: loadDashboard shows the empty state instead of waiting for the next refresh.
  if (!state.selectedPoolId || state.currentDashboard?.pool.id !== state.selectedPoolId) await loadDashboard();
  setInterval(refreshRuntime, 10000);
}

function bindEvents() {
  if (!window.Plotly) $("#chartAvailability").textContent = "Charts loading — waiting for Plotly.";
  if ($("#plotlyScript").dataset.failed) $("#chartAvailability").textContent = "Charts unavailable — Plotly failed to load. Reload to retry.";
  $("#plotlyScript").addEventListener("error", () => { $("#chartAvailability").textContent = "Charts unavailable — Plotly failed to load. Reload to retry."; });
  $("#plotlyScript").addEventListener("load", () => {
    $("#chartAvailability").textContent = "";
    if (!state.currentDashboard || state.view !== "dashboard") return;
    state.chartRenderKeys = {};
    renderDashboardCharts(state.currentDashboard);
  });
  $("#sidebarToggle").addEventListener("click", toggleSidebar);
  $("#sidebarScrim").addEventListener("click", closeSidebar);
  $("#addPoolButton").addEventListener("click", () => showPoolEditor());
  $("#emptyAddButton").addEventListener("click", () => showPoolEditor());
  $("#emptyEnvioButton").addEventListener("click", showRpcSettings);
  $("#editPoolButton").addEventListener("click", () => editPool(state.selectedPoolId));
  $("#removePoolButton").addEventListener("click", () => removePool(state.selectedPoolId));
  $("#rpcConfigButton").addEventListener("click", showRpcSettings);
  $("#closeRpcSettings").addEventListener("click", showDashboard);
  $("#cancelEdit").addEventListener("click", () => { resetPoolForm(); showDashboard(); });
  $("#windowControls").addEventListener("click", (event) => selectWindow(
    event, "hours", "openPoolWindowHours", loadDashboard,
  ));
  $("#listWindowControls").addEventListener("click", (event) => selectWindow(
    event, "listHours", "openPoolListWindowHours", refreshBootstrap,
  ));
  $("#listPnlFlow").addEventListener("click", () => selectListPnlMode("flow"));
  $("#listPnlMtm").addEventListener("click", () => selectListPnlMode("mtm"));
  $("#counterpartySide").addEventListener("click", toggleCounterpartySide);
  $("#counterpartiesBody").addEventListener("click", editAddressLabel);
  $("#swapsBody").addEventListener("click", editAddressLabel);
  $("#previewPool").addEventListener("click", discoverPool);
  $("#metadataButton").addEventListener("click", loadMetadataPools);
  $("#metadataPools").addEventListener("click", selectMetadataPool);
  $("#metadataSearch").addEventListener("input", renderMetadataPools);
  bindAutomaticPoolDiscovery($("#poolAddressInput"), $("#poolChainInput"), invalidateDiscovery, discoverPool);
  $("#poolForm").addEventListener("submit", savePool);
  $("#poolForm").addEventListener("change", (event) => {
    const source = event.target.closest(".price-kind");
    if (source) syncPriceInputs(source.closest(".asset-config"));
  });
  $("#rpcRows").addEventListener("click", handleRpcAction);
  $("#saveEnvioSettings").addEventListener("click", saveEnvioSettings);
  $("#clearEnvioKey").addEventListener("click", clearEnvioKey);
  $("#previousSwaps").addEventListener("click", () => changeSwapPage(-1));
  $("#nextSwaps").addEventListener("click", () => changeSwapPage(1));
  $("#plotConfiguration").addEventListener("change", configurePlots);
  $("#chartGrid").addEventListener("click", toggleChartFullscreen);
  document.addEventListener("fullscreenchange", handleFullscreenChange);
  window.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSidebar(); });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(state.statusTimer);
    if (document.hidden) {
      state.dashboardController?.abort();
      return;
    }
    pollStatus();
    refreshRuntime();
  });
  window.addEventListener("resize", () => { if (window.innerWidth > 980) closeSidebar(); });
}

async function refreshRuntime() {
  if (state.uiLocked || document.hidden) return;
  await Promise.all([state.bootstrapFlight ? null : refreshBootstrap(), state.view === "dashboard" ? loadDashboard() : Promise.resolve()]);
}

async function refreshBootstrap({ initializeControls = false } = {}) {
  // Every call fetches fresh state and only the newest response renders, so a poll that
  // started before a pool mutation cannot overwrite the mutated list or selection.
  const hours = state.listHours;
  const sequence = ++state.bootstrapSequence;
  const flight = state.bootstrapFlight = api(`/api/bootstrap?hours=${hours}`);
  try {
    const bootstrap = await flight;
    if (sequence !== state.bootstrapSequence || hours !== state.listHours) return;
    state.bootstrap = bootstrap;
    setCsrfToken(state.bootstrap.csrf_token);
    if (initializeControls) populateChainSelects();
    renderWindowControls("#windowControls", state.hours);
    renderWindowControls("#listWindowControls", state.listHours);
    normalizeSelectedPool();
    renderPoolManagement();
    renderSystemStatus();
  } catch (error) {
    if (sequence !== state.bootstrapSequence) return;
    notice(`Pool data unavailable: ${error.message}`, true);
    renderSystemStatus();
  } finally { if (state.bootstrapFlight === flight) state.bootstrapFlight = null; }
}

// ==== Pool navigation and dashboard ====

function normalizeSelectedPool() {
  const pools = state.bootstrap.pools;
  if (!pools.some((pool) => pool.id === state.selectedPoolId)) state.selectedPoolId = pools[0]?.id || null;
  if (state.selectedPoolId) localStorage.setItem("openPoolId", state.selectedPoolId);
  else localStorage.removeItem("openPoolId");
}

function populateChainSelects() {
  const options = state.bootstrap.chains.map((chain) => `<option value="${escapeHtml(chain.key)}">${escapeHtml(chain.name)}</option>`).join("");
  $("#poolChainInput").innerHTML = options;
}

function renderPoolManagement() {
  if (state.resizingColumns) return;
  const container = $("#poolManagement");
  const pnlKey = `${state.listPnlMode}_pnl_usd`;
  const aprKey = `${state.listPnlMode}_apr_pct`;
  const pnlLabel = state.listPnlMode.toUpperCase();
  for (const [mode, id] of [["flow", "listPnlFlow"], ["mtm", "listPnlMtm"]]) {
    const button = $(`#${id}`);
    button.classList.toggle("selected", state.listPnlMode === mode);
    button.setAttribute("aria-pressed", String(state.listPnlMode === mode));
  }
  const groups = state.bootstrap.chains.map((chain) => ({
    chain,
    pools: state.bootstrap.pools.filter((pool) => pool.chain_key === chain.key),
  })).filter((group) => group.pools.length);
  container.innerHTML = groups.length ? `<table class="pool-table mono">
    <colgroup><col class="pool-column"><col><col><col><col><col class="last-swap-column"><col class="status-column"></colgroup>
    <thead><tr><th>POOL</th><th>${formatWindowLabel(state.listHours)} VOL</th><th>${pnlLabel} PNL</th><th>${pnlLabel} APR</th><th>TVL</th><th>LAST SWAP<br>HRS AGO</th><th title="Indexing, then pricing: the stage still in progress, or UP TO DATE once both are done">DATA</th></tr></thead>
    <tbody>${groups.map(({ chain, pools }) => `<tr class="chain-heading"><td colspan="7">CHAIN: ${escapeHtml(chain.name)}</td></tr>
    ${pools.map((pool) => {
      const status = poolStatus(pool);
      const selected = pool.id === state.selectedPoolId;
      const summary = pool.summary || {};
      return `<tr class="pool-row ${selected ? "selected" : ""}" data-id="${escapeHtml(pool.id)}" tabindex="0" role="button" aria-current="${selected}">
        <td><span class="status-lamp ${status.tone}"></span><span class="pool-name">${escapeHtml(pool.name)}</span></td>
        <td>${compactUsd(summary.volume_usd)}${summary.volume_usd != null && summary.volume_complete === false ? " PARTIAL" : ""}</td>
        <td class="${signedClass(summary[pnlKey])}" title="${state.listPnlMode === "mtm" ? (pool.mtm_mode === "balanced" ? "MTM: 50/50 opening benchmark" : "MTM: actual starting mix") : "Flow PnL"}">${summary[pnlKey] == null ? missingPnlHint(pool) : compactUsd(summary[pnlKey], true)}${summary[pnlKey] != null && summary[`${state.listPnlMode}_complete`] === false ? " PARTIAL" : ""}</td>
        <td class="${signedClass(summary[aprKey])}" title="${summary.apr_tvl_usd != null ? `On peak window TVL ${compactUsd(summary.apr_tvl_usd)}` : ""}">${percent(summary[aprKey])}</td>
        <td>${compactUsd(summary.tvl_usd)}</td>
        <td>${formatHoursAgo(summary.last_swap_at)}</td>
        <td class="pool-status ${status.tone}" title="${escapeHtml(status.title || "")}">${escapeHtml(status.label)}</td>
      </tr>`;
    }).join("")}`).join("")}${summaryRow("TOTAL", state.bootstrap.total_summary, pnlKey, aprKey)}</tbody>
  </table>` : `<div class="pool-empty">NO POOLS CONFIGURED</div>`;
  bindColumnResizing(container);
  container.querySelectorAll(".pool-row").forEach((row) => {
    row.addEventListener("click", () => selectPool(row.dataset.id));
    row.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      selectPool(row.dataset.id);
    });
  });
}

function bindColumnResizing(container) {
  const columns = [...container.querySelectorAll("col")];
  const saved = (localStorage.getItem("openPoolColumnWidths") || "").split(",").map(Number);
  if (saved.length === columns.length && saved.every((width) => Number.isFinite(width) && width > 0) && Math.abs(saved.reduce((a, b) => a + b, 0) - 100) < 0.01) {
    columns.forEach((column, index) => { column.style.width = `${saved[index]}%`; });
  }
  container.querySelectorAll("th").forEach((header, index, headers) => {
    if (index === headers.length - 1) return;
    const handle = document.createElement("span");
    handle.className = "column-resizer";
    handle.tabIndex = 0;
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    handle.setAttribute("aria-label", `Resize ${header.textContent} column`);
    handle.title = "Drag to resize; use arrow keys when focused";
    header.append(handle);
    let startX, widths;
    const resize = (delta) => {
      const total = widths.reduce((a, b) => a + b, 0);
      const pair = widths[index] + widths[index + 1];
      const minimum = Math.min(40, pair / 2);
      const left = Math.max(minimum, Math.min(pair - minimum, widths[index] + delta));
      const adjusted = widths.map((width, i) => (i === index ? left : i === index + 1 ? pair - left : width) / total * 100);
      columns.forEach((column, i) => { column.style.width = `${adjusted[i]}%`; });
      localStorage.setItem("openPoolColumnWidths", adjusted.join(","));
    };
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      widths = [...headers].map((cell) => cell.getBoundingClientRect().width);
      startX = event.clientX;
      state.resizingColumns = true;
      handle.setPointerCapture(event.pointerId);
    });
    handle.addEventListener("pointermove", (event) => {
      if (handle.hasPointerCapture(event.pointerId)) resize(event.clientX - startX);
    });
    handle.addEventListener("lostpointercapture", () => {
      state.resizingColumns = false;
      renderPoolManagement();
    });
    handle.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      widths = [...headers].map((cell) => cell.getBoundingClientRect().width);
      resize(event.key === "ArrowLeft" ? -10 : 10);
    });
  });
}

function renderSystemStatus() {
  const pools = Object.values(state.status?.pools || {});
  const activity = state.status?.operation || { phase: "idle", message: "IDLE" };
  const errors = pools.filter((pool) => pool.ingestion.error || pool.pricing.error).length;
  const working = pools.filter((pool) => [pool.ingestion, pool.pricing].some((stage) => ["working", "waiting", "queued"].includes(stage.state))).length;
  $("#systemPoolCount").textContent = String(pools.length);
  $("#systemErrorCount").textContent = String(errors);
  $("#systemStatus").textContent = errors ? "ATTENTION" : working ? `${working} ACTIVE` : pools.length ? "NOMINAL" : "IDLE";
  $("#systemLamp").className = `status-lamp ${errors ? "bad" : working ? "warning" : pools.length ? "good" : "idle"}`;
  if (state.statusError || !state.status) {
    $("#systemStatus").textContent = state.statusError ? "CONNECTION LOST" : "CHECKING";
    $("#systemLamp").className = "status-lamp warning";
  } else if (!state.status.worker_alive) {
    $("#systemStatus").textContent = "STOPPED";
    $("#systemLamp").className = "status-lamp bad";
  }
  const activityTone = activity.phase === "error" ? "bad" : activity.phase === "idle" ? "idle" : "working";
  $("#backendActivity").textContent = activity.message;
  $("#backendActivity").className = activityTone;
  $("#backendActivity").title = activity.updated_at ? `Updated ${formatCompactTime(activity.updated_at)}` : "";
  $("#footerActivity").textContent = activity.message;
  $("#footerActivity").title = $("#backendActivity").title;
  $("#systemActivity").textContent = (activity.phase || "idle").toUpperCase();
}

function poolStatus(pool) {
  // One answer to "is this pool's data complete?": the stage still holding it back, with its progress.
  const { ingestion, pricing } = state.status?.pools?.[pool.id] || {};
  if (!ingestion) return { label: "CHECKING", tone: "warning" };
  if (ingestion.state === "paused") return { label: "PAUSED", tone: "idle" };
  if (ingestion.error) return { label: "INGESTION ERROR", tone: "bad", title: ingestion.error };
  if (pricing?.error) return { label: "PRICE ERROR", tone: "bad", title: pricing.error };
  if (ingestion.state !== "caught_up") {
    // Share of the last 3 days of blocks (or since the pool's start block, if later) that ingestion has committed.
    const chain = state.bootstrap?.chains.find((row) => row.key === pool.chain_key);
    const target = ingestion.target_block;
    const note = ingestion.wait_reason || ingestion.action || "";
    // A target at or below the indexed block is the head saved by the previous sync; the new head isn't known yet.
    if (target == null || target <= ingestion.indexed_block || !chain?.seconds_per_block) return { label: "INDEXING", tone: "working", title: note };
    const from = Math.max(pool.start_block ?? 0, target - Math.round(3 * 86400 / chain.seconds_per_block));
    const pct = Math.floor(Math.min(1, Math.max(0, (ingestion.indexed_block - from + 1) / Math.max(1, target - from + 1))) * 100);
    return { label: `INDEXING ${pct}%`, tone: "working",
      title: `Indexed through block ${ingestion.indexed_block.toLocaleString()} of ${target.toLocaleString()} (last 3 days)${note ? ` · ${note}` : ""}` };
  }
  const { pending_prices: pending, price_targets: targets } = pricing || {};
  if (pending || pricing?.state === "working") {
    if (!targets) return { label: "PRICING", tone: "working" };
    const pct = Math.floor((1 - pending / targets) * 100);
    return { label: `PRICING ${pct}%`, tone: "working", title: `${(targets - pending).toLocaleString()} of ${targets.toLocaleString()} prices fetched` };
  }
  return { label: "UP TO DATE", tone: "good" };
}

function missingPnlHint(pool) {
  const summary = pool.summary || {};
  const stages = state.status?.pools?.[pool.id];
  const flow = state.listPnlMode === "flow";
  const unpriced = (pool.assets || []).filter((asset) => asset.price_kind === "none").map((asset) => asset.symbol);
  const reasons = [
    !summary.swap_count && `No swaps in the last ${formatWindowLabel(state.listHours)}.`,
    !summary.swap_count && stages?.ingestion && stages.ingestion.state !== "caught_up" && "Ingestion hasn't caught up yet.",
    summary.swap_count && flow && unpriced.length && `Flow PnL needs a price source for both assets; ${unpriced.join(" and ")} has none.`,
    summary.swap_count && !flow && unpriced.length === 2 && "MTM PnL needs a price source for at least one asset.",
    summary.swap_count && stages?.pricing?.pending_prices && `${stages.pricing.pending_prices} prices are still being fetched.`,
    summary.swap_count && !flow && summary.mtm_baseline_available === false && "No opening balance and price to set the 50/50 benchmark.",
  ].filter(Boolean);
  const text = reasons.join(" ") || "No swap in this window could be priced.";
  return `<span class="missing-hint" title="${escapeHtml(text)}" aria-label="${escapeHtml(text)}">?</span>`;
}

async function selectPool(poolId) {
  if (state.uiLocked) return;
  const poolName = state.bootstrap.pools.find((pool) => pool.id === poolId)?.name || "POOL";
  setUiLocked(true, `LOADING ${poolName}`);
  try {
    state.selectedPoolId = poolId;
    localStorage.setItem("openPoolId", poolId);
    renderPoolManagement();
    renderSystemStatus();
    closeSidebar();
    await showDashboard();
  } finally {
    setUiLocked(false);
  }
}

async function loadDashboard() {
  if (document.hidden) return;
  if (!state.selectedPoolId) return showEmptyState();
  const poolId = state.selectedPoolId;
  const hours = state.hours;
  const params = new URLSearchParams({ hours, plots: state.plots.join(","), minimum_swap_usd: state.hideSmallSwaps ? state.minimumSwapUsd : 0,
    counterparty_side: state.counterpartySide });
  if (state.chartRange && state.chartRangeContext === `${poolId}:${hours}`) {
    params.set("range_start", state.chartRange[0]);
    params.set("range_end", state.chartRange[1]);
  }
  const viewUrl = `/api/pools/${encodeURIComponent(poolId)}/dashboard?${params}`;
  // Refreshes of the same view keep the reader's swap page; any view change starts at page 1.
  if (viewUrl === state.dashboardUrl && state.swapPage) params.set("page", state.swapPage);
  const url = `/api/pools/${encodeURIComponent(poolId)}/dashboard?${params}`;
  const context = url;
  if (state.dashboardFlight && state.dashboardContext === context) {
    await state.dashboardFlight.catch(() => {});
    return;
  }
  state.dashboardController?.abort();
  state.dashboardContext = context;
  const sequence = ++state.dashboardSequence;
  ++state.pageSequence;
  state.refreshStarted = Date.now();
  renderActivity();
  try {
    if (state.dashboardFlight) {
      await state.dashboardFlight.catch(() => {});
      if (sequence !== state.dashboardSequence) return;
    }
    state.dashboardController = new AbortController();
    let nextEtag = null;
    state.dashboardFlight = api(url, {
      signal: state.dashboardController.signal,
      etag: state.dashboardUrl === viewUrl ? state.dashboardEtag : null,
      onEtag: (value) => { nextEtag = value; },
    });
    const data = await state.dashboardFlight;
    if (sequence !== state.dashboardSequence || poolId !== state.selectedPoolId || hours !== state.hours || state.view !== "dashboard") return;
    if (data) {
      state.dashboardUrl = viewUrl;
      state.dashboardEtag = nextEtag;
      ++state.pageSequence;
      renderDashboard(data);
    }
    state.lastRefresh = Date.now();
    state.refreshError = null;
  } catch (error) {
    if (sequence === state.dashboardSequence && error.name !== "AbortError") state.refreshError = error.message;
  } finally {
    if (sequence === state.dashboardSequence) {
      state.dashboardFlight = null;
      state.refreshStarted = null;
      renderActivity();
    }
  }
}

async function pollStatus() {
  if (document.hidden || state.statusFlight) return;
  state.statusFlight = true;
  clearTimeout(state.statusTimer);
  try {
    state.status = await api("/api/status");
    state.statusError = null;
  } catch (error) {
    state.statusError = `Lost connection — ${error.message}. Retaining last known data; retrying.`;
  }
  state.statusFlight = false;
  renderActivity();
  if (state.currentDashboard) renderFooter(state.currentDashboard);
  if (state.bootstrap) renderPoolManagement();
  renderSystemStatus();
  const active = Object.values(state.status?.pools || {}).some((p) => [p.ingestion, p.pricing].some((s) => ["working", "waiting", "queued", "initializing"].includes(s.state)));
  if (!document.hidden) state.statusTimer = setTimeout(pollStatus, active ? 2000 : 10000);
}

function renderActivity() {
  if (document.hidden) return;
  const box = $("#activityStatus");
  if (!box) return;
  const expanded = box.querySelector?.("details")?.open || false;
  const pool = state.status?.pools?.[state.selectedPoolId];
  const details = [];
  const labels = { initializing: "Starting", queued: "Queued", working: "Working", waiting: "Waiting", caught_up: "Up to date", paused: "Paused", error: "Needs attention" };
  const rows = ["ingestion", "pricing"].map((name) => {
    const stage = pool?.[name];
    const title = name === "ingestion" ? "Ingestion" : "Pricing";
    if (!stage) return `<div class="activity-row"><span>${title}</span><strong>Starting</strong><span class="activity-note">Checking status…</span></div>`;
    let note = stage.error || stage.wait_reason || stage.action || "";
    if (!stage.error && stage.state === "caught_up") {
      note = name === "pricing" ? "No prices pending" : stage.indexed_at ? `Data through ${new Date(stage.indexed_at).toLocaleString()}` : "Confirmed blocks indexed";
    } else if (!stage.error && stage.state === "queued") {
      note = stage.wait_reason || "Waiting for the worker";
    }
    if (stage.taking_longer_than_expected) note = `${note} · taking longer than expected (${stage.elapsed_seconds}s)`;
    const tone = stage.error ? "bad" : stage.state === "caught_up" ? "good" : "warning";
    details.push(`${title}: ${stage.action || labels[stage.state] || stage.state}`);
    if (stage.indexed_at && name === "ingestion") details.push(`Indexed through ${new Date(stage.indexed_at).toLocaleString()}`);
    if (name === "ingestion") details.push(`Block ${stage.indexed_block ?? "—"} / confirmed ${stage.target_block ?? "—"}`);
    if (stage.last_success_at) details.push(`${title} last completed: ${new Date(stage.last_success_at).toLocaleString()}`);
    if (stage.next_attempt_at) details.push(`${title} next eligible: ${new Date(stage.next_attempt_at).toLocaleString()} · starts when the worker is available`);
    return `<div class="activity-row"><span>${title}</span><strong class="${tone}">${escapeHtml(labels[stage.state] || stage.state)}</strong><span class="activity-note">${escapeHtml(note)}</span></div>`;
  });
  const old = state.currentDashboard;
  if (old) {
    details.push(`Showing ${old.pool.name} · ${formatWindowLabel(old.window_hours)}`);
    details.push(`Price coverage: ${old.metrics.price_coverage_pct ?? "—"}% external · ${old.metrics.mtm_coverage_pct ?? "—"}% MTM`);
    if (old.metrics.ordering_pending_count) details.push(`${old.metrics.ordering_pending_count} swaps awaiting transaction metadata`);
  }
  details.push(`Display last updated: ${state.lastRefresh ? new Date(state.lastRefresh).toLocaleString() : "never"} · checks about every 10 seconds`);
  const error = state.refreshError || state.statusError || state.status?.worker_error || (state.status && !state.status.worker_alive ? "Indexer stopped — restart the server to resume background work." : null);
  box.innerHTML = `${error ? `<div class="activity-error" role="status">${escapeHtml(error)}</div>` : ""}<div class="activity-columns">${rows.join("")}<details class="activity-details"${expanded ? " open" : ""}><summary>Details</summary><div>${details.map(escapeHtml).join("<br>")}</div></details></div>`;
}

function renderDashboard(data) {
  const chartContext = `${data.pool.id}:${data.window_hours}`;
  if (chartContext !== state.chartRangeContext) {
    state.chartRange = null;
    state.chartRangeContext = chartContext;
    state.swapPage = 0;
  }
  state.currentDashboard = data;
  showWorkspaceSection("dashboard");
  $("#workspaceTitle").textContent = data.pool.name.toUpperCase();
  $("#poolChain").textContent = `${data.pool.chain_name.toUpperCase()} · POOL MONITOR`;
  $("#poolTitle").textContent = data.pool.name;
  $("#poolAddress").textContent = data.pool.address;
  $("#poolAddress").removeAttribute("href");

  $("#balancesMetric").textContent = data.balances.length
    ? data.balances.map((row) => `${formatNumber(row.balance)} ${row.symbol}`).join("\n") : "—";
  $("#balanceBlock").textContent = data.balances[0] ? `CONFIRMED BLOCK ${formatInteger(data.balances[0].block_number)}` : "WAITING FOR ANCHOR";
  $("#volumeMetric").textContent = money(data.metrics.volume_usd);
  $("#swapCountMetric").textContent = `${data.metrics.swap_count} SWAPS${data.metrics.volume_complete === false && data.metrics.volume_usd != null ? " · PARTIAL VOLUME" : ""}`;
  setSignedMetric($("#flowMetric"), data.metrics.flow_pnl_usd);
  setSignedMetric($("#mtmMetric"), data.metrics.mtm_pnl_usd);
  for (const measure of ["flow", "mtm"]) {
    if (data.metrics[`${measure}_complete`] === false && data.metrics[`${measure}_pnl_usd`] != null) $("#" + measure + "Metric").textContent += " · PARTIAL";
  }
  const markLabel = data.metrics.mtm_mark === "pool_implied" ? "POOL MARK" : "EXTERNAL MARK";
  const mtmBasis = data.pool.mtm_mode === "balanced" ? "50/50" : "AS IS";
  $("#mtmAsOf").textContent = data.metrics.mtm_baseline_available === false
    ? "50/50 · OPENING DATA UNAVAILABLE"
    : `${mtmBasis} · ${data.metrics.mtm_as_of ? `${markLabel} · AS OF ${formatCompactTime(data.metrics.mtm_as_of)}` : "WAITING FOR SWAPS"}`;
  $("#mtmMetric").title = data.pool.mtm_mode === "balanced" && data.metrics.mtm_baseline_mark === "empty"
    ? "The pool opened empty, so the 50/50 hold is empty and PnL counts only swaps."
    : data.pool.mtm_mode === "balanced"
    ? `PnL versus a 50/50 opening-value hold. Opening mark: ${data.metrics.mtm_baseline_mark === "pool_implied" ? "swap price" : "external price"}${data.metrics.mtm_baseline_as_of ? ` at ${formatCompactTime(data.metrics.mtm_baseline_as_of)}` : " unavailable"}.`
    : "PnL versus holding the actual starting asset mix.";
  const externalCoverage = data.metrics.price_coverage_pct || 0;
  const mtmCoverage = data.metrics.mtm_coverage_pct || 0;
  $("#coverageBar").style.width = `${mtmCoverage}%`;
  $("#coverageText").textContent = data.metrics.mtm_coverage_pct == null
    ? "NO SWAPS"
    : mtmCoverage === externalCoverage
      ? `${mtmCoverage}% · ${data.metrics.mtm_priced_swap_count}/${data.metrics.swap_count}`
      : `${mtmCoverage}% MTM · ${externalCoverage}% EXTERNAL`;
  const thin = data.metrics.thin_market_count;
  if (thin) $("#coverageText").textContent += ` · ${thin} THIN MARKET`;
  $("#coverageText").title = thin
    ? `${thin} swaps had no CEX trade in the 5 minutes before them. Flow PnL leaves them out; MTM still counts their inventory.` : "";
  $("#asset0Heading").textContent = data.pool.assets[0]?.symbol || "ASSET 1";
  $("#asset1Heading").textContent = data.pool.assets[1]?.symbol || "ASSET 2";
  $("#priceChartUnit").textContent = `${data.pool.assets[1]?.symbol || "ASSET 2"} PER ${data.pool.assets[0]?.symbol || "ASSET 1"}`;
  renderDashboardCharts(data);
  renderCounterparties(data);
  renderSwaps(data);
  renderFooter(data);

}

function renderFooter(data) {
  const stage = state.status?.pools?.[data.pool.id]?.ingestion;
  const label = String(stage?.state || "queued").replaceAll("_", " ").toUpperCase();
  $("#footerStatus").textContent = label;
  $("#footerRpc").textContent = `RPC ${stage?.active_rpc || "—"}`;
  $("#footerBlock").textContent = `CONFIRMED ${stage?.target_block == null ? "—" : formatInteger(stage.target_block)}`;
  $("#footerSuccess").textContent = `LAST OK ${stage?.last_success_at ? formatCompactTime(stage.last_success_at) : "—"}`;
  $("#footerRefresh").textContent = `REFRESH ${new Date().toLocaleTimeString()}`;
}

function renderDashboardCharts(data) {
  $$('[data-plot]').forEach((input) => { input.checked = state.plots.includes(input.dataset.plot); });
  $$('[data-plot-panel]').forEach((panel) => panel.classList.toggle("hidden", !state.plots.includes(panel.dataset.plotPanel)));
  $("#hideSmallSwaps").checked = state.hideSmallSwaps;
  const minimumSwapInput = $("#minimumSwapUsd");
  minimumSwapInput.disabled = !state.hideSmallSwaps;
  if (document.activeElement !== minimumSwapInput) minimumSwapInput.value = state.minimumSwapUsd;
  const changed = new Set();
  for (const plot of state.plots) {
    const key = JSON.stringify([data.pool.id, data.window_hours, data[`${plot}_series`],
      plot === "price" ? data.reference_series : null, state.chartRange, data.pool.assets, data.pool.mtm_mode]);
    if (state.chartRenderKeys[plot] !== key) { changed.add(plot); state.chartRenderKeys[plot] = key; }
  }
  for (const plot of PLOT_IDS) if (!state.plots.includes(plot)) delete state.chartRenderKeys[plot];
  if (window.Plotly) $("#chartAvailability").textContent = "";
  if (changed.has("pnl")) renderPnlChart(data.pnl_series, {
    mtmMode: data.pool.mtm_mode,
    range: state.chartRange, revision: state.chartRangeContext,
    onRangeChange: (range) => applyChartRange(range, "pnlChart"),
  });
  if (changed.has("price")) renderPriceChart(data.price_series, data.pool.assets, {
    range: state.chartRange, revision: state.chartRangeContext, explorerTxUrl: data.explorer_tx_url,
    referencePoints: data.reference_series,
    onRangeChange: (range) => applyChartRange(range, "priceChart"),
  });
  if (changed.has("balance")) renderBalanceChart(data.balance_series, data.pool.assets, {
    range: state.chartRange, revision: state.chartRangeContext,
    onRangeChange: (range) => applyChartRange(range, "balanceChart"),
  });
  if (changed.has("volume")) renderCumulativeVolumeChart(data.volume_series, {
    range: state.chartRange, revision: state.chartRangeContext,
    onRangeChange: (range) => applyChartRange(range, "volumeChart"),
  });
}

function applyChartRange(range, sourceChartId) {
  if (sameTimeRange(range, state.chartRange)) return;
  state.chartRange = range;
  state.swapPage = 0;
  ++state.pageSequence;
  state.refreshStarted = Date.now();
  renderActivity();
  const chartIds = { pnl: "pnlChart", price: "priceChart", balance: "balanceChart", volume: "volumeChart" };
  state.plots.map((plot) => chartIds[plot]).filter((chartId) => chartId !== sourceChartId)
    .forEach((chartId) => setChartTimeRange(chartId, range));
  clearTimeout(state.zoomTimer);
  state.zoomTimer = setTimeout(loadDashboard, 150);
}

function configurePlots(event) {
  const checkbox = event.target;
  if (checkbox?.id === "hideSmallSwaps") {
    state.hideSmallSwaps = checkbox.checked;
    localStorage.setItem("openPoolHideSmallSwaps", String(state.hideSmallSwaps));
    if (state.currentDashboard) loadDashboard();
    return;
  }
  if (checkbox?.id === "minimumSwapUsd") {
    const value = Number(checkbox.value);
    if (!Number.isFinite(value) || value < 0) {
      checkbox.value = state.minimumSwapUsd;
      return;
    }
    state.minimumSwapUsd = value;
    localStorage.setItem("openPoolMinimumSwapUsd", String(value));
    if (state.currentDashboard) loadDashboard();
    return;
  }
  const plot = checkbox?.dataset?.plot;
  if (!PLOT_IDS.includes(plot)) return;
  const selected = new Set(state.plots);
  if (checkbox.checked) selected.add(plot); else selected.delete(plot);
  if (!selected.size) {
    checkbox.checked = true;
    return;
  }
  state.plots = PLOT_IDS.filter((id) => selected.has(id));
  localStorage.setItem("openPoolPlots", state.plots.join(","));
  if (state.currentDashboard) loadDashboard();
}

async function toggleChartFullscreen(event) {
  const button = event.target.closest("[data-fullscreen-chart]");
  if (!button) return;
  const panel = button.closest(".chart-panel");
  try {
    if (document.fullscreenElement === panel) await document.exitFullscreen();
    else await panel.requestFullscreen();
  } catch (error) {
    notice(`FULL SCREEN UNAVAILABLE: ${error.message}`, true);
  }
}

function handleFullscreenChange() {
  $$('[data-fullscreen-chart]').forEach((button) => {
    button.textContent = button.closest(".chart-panel") === document.fullscreenElement
      ? "EXIT FULL SCREEN" : "FULL SCREEN";
  });
  window.requestAnimationFrame?.(() => {
    $$('[data-plot-panel]:not(.hidden) .chart').forEach((chart) => window.Plotly?.Plots?.resize(chart));
  });
}

function sameTimeRange(left, right) {
  if (!left || !right) return left === right;
  return Date.parse(left[0]) === Date.parse(right[0]) && Date.parse(left[1]) === Date.parse(right[1]);
}

function renderSwaps(data) {
  const [asset0, asset1] = data.pool.assets;
  const rows = data.swap_page;
  const page = { items: rows.items, pageIndex: rows.page, pageCount: Math.ceil(rows.total / rows.page_size) };
  state.swapPage = rows.page;
  $("#recentCount").textContent = `${rows.total} MATCHING`;
  $("#swapPage").textContent = page.pageCount
    ? `PAGE ${page.pageIndex + 1} / ${page.pageCount}`
    : "PAGE 0 / 0";
  $("#previousSwaps").disabled = page.pageIndex === 0;
  $("#nextSwaps").disabled = page.pageIndex + 1 >= page.pageCount;
  $("#swapsBody").innerHTML = page.items.map((row) => {
    const txUrl = data.explorer_tx_url.replace("{tx_hash}", row.tx_hash);
    const prices = [row.asset0_price, row.asset1_price];
    const reference = row.thin_market ? "THIN MARKET" : prices.every((price) => price == null)
      ? "UNPRICED"
      : prices.map((price) => price == null ? "—" : formatNumber(price)).join(" / ");
    const missingPrices = prices.flatMap((price, index) => price == null
      ? [`${data.pool.assets[index].symbol}: ${data.pool.assets[index].price_kind === "none" ? "no price source" : row.thin_market ? "no CEX trade within 5 minutes (thin market)" : "price unavailable"}`]
      : []);
    const referenceTitle = [
      ...missingPrices,
      ...(row.price_lag_ms == null ? [] : [`${row.price_lag_ms}ms lag`]),
    ].join(" · ");
    return `<tr>
      <td>${escapeHtml(formatTime(row.timestamp))}</td>
      <td class="counterparty-column">${counterpartyLink(row.counterparty, data.explorer_address_url)}${row.counterparty_rank == null ? "" : `<span class="counterparty-rank mono" title="Rank by volume among all counterparties in this window">TOP #${row.counterparty_rank}</span>`}</td>
      <td class="${signedClass(row.asset0_delta)}">${signed(row.asset0_delta)} ${escapeHtml(asset0.symbol)}</td>
      <td class="${signedClass(row.asset1_delta)}">${signed(row.asset1_delta)} ${escapeHtml(asset1.symbol)}</td>
      <td title="${escapeHtml(referenceTitle)}">${escapeHtml(reference)}</td>
      <td class="${signedClass(row.flow_pnl)}">${row.flow_pnl == null ? "—" : money(row.flow_pnl)}</td>
      <td class="${signedClass(row.edge_bps)}">${bps(row.edge_bps)}</td>
      <td><a href="${escapeHtml(txUrl)}" target="_blank" rel="noreferrer">OPEN ↗</a></td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">NO SWAPS IN THIS ${state.chartRange ? "RANGE" : "WINDOW"}</td></tr>`;
}

function renderCounterparties(data) {
  const summary = data.counterparties;
  const counterpartyLabel = summary.total_counterparties === 1 ? "COUNTERPARTY" : "COUNTERPARTIES";
  $("#counterpartyCount").textContent = `${summary.total_swaps} SWAPS · ${summary.total_counterparties} ${counterpartyLabel}`;
  const received = summary.side === "recipient";
  $("#counterpartySide").textContent = received ? "RECEIVED FROM POOL" : "SENT TO POOL";
  $("#counterpartySide").title = `Grouped by the address that ${received ? "received tokens from" : "sent tokens to"} the pool; click to switch`;
  $("#counterpartiesBody").innerHTML = summary.rows.map((row) => `<tr>
    <td>${counterpartyLink(row.address, data.explorer_address_url)}</td>
    <td>${formatInteger(row.swap_count)}</td>
    <td>${row.unique_signers == null ? "—" : formatInteger(row.unique_signers)}</td>
    <td>${percent(row.swap_share_pct)}</td>
    <td>${money(row.volume_usd, 0)}</td>
    <td>${percent(row.volume_share_pct)}</td>
    <td class="${signedClass(row.avg_edge_bps)}">${bps(row.avg_edge_bps)}</td>
  </tr>`).join("") || `<tr><td colspan="7" class="muted">NO COUNTERPARTIES IN THIS ${state.chartRange ? "RANGE" : "WINDOW"}</td></tr>`;
}

async function changeSwapPage(delta) {
  const data = state.currentDashboard;
  if (!data || state.pageFlight || state.refreshStarted) return;
  const sequence = ++state.pageSequence;
  const params = new URLSearchParams({ hours: data.window_hours, window_end: data.window_end,
    revision: data.data_revision, page: Math.max(0, state.swapPage + delta) });
  if (state.chartRange) {
    params.set("range_start", state.chartRange[0]); params.set("range_end", state.chartRange[1]);
  }
  state.pageFlight = true;
  try {
    const page = await api(`/api/pools/${encodeURIComponent(data.pool.id)}/swaps?${params}`);
    if (sequence !== state.pageSequence || data !== state.currentDashboard) return;
    data.swap_page = page;
    renderSwaps(data);
  } catch (error) {
    if (sequence !== state.pageSequence) return;
    if (error.status === 409) {
      notice("Data updated · refreshing swaps.");
      state.swapPage = Math.max(0, state.swapPage + delta);
      state.dashboardEtag = null;
      await loadDashboard();
    } else notice(error.message, true);
  } finally { state.pageFlight = false; }
}

async function showDashboard() {
  state.view = "dashboard";
  closeSidebar();
  if (state.selectedPoolId) {
    showWorkspaceSection("dashboard");
    $("#workspaceTitle").textContent = selectedPool()?.name?.toUpperCase() || "POOL MONITOR";
    await loadDashboard();
  } else showEmptyState();
}

function showEmptyState() {
  state.view = "dashboard";
  state.currentDashboard = null;
  state.chartRange = null;
  state.chartRangeContext = null;
  state.swapPage = 0;
  showWorkspaceSection("emptyState");
  // Without an Envio key nothing can be indexed, so point to it first.
  const needsKey = state.bootstrap?.envio?.configured === false;
  $("#emptyEnvioHint").classList.toggle("hidden", !needsKey);
  $("#emptyEnvioButton").classList.toggle("hidden", !needsKey);
  $("#emptyAddButton").classList.toggle("primary", !needsKey);
  $("#workspaceTitle").textContent = "POOL MONITOR";
  $("#footerStatus").textContent = "IDLE";
  $("#footerRpc").textContent = "RPC —";
  $("#footerBlock").textContent = "CONFIRMED —";
  $("#footerSuccess").textContent = "LAST OK —";
}

function showWorkspaceSection(id) {
  ["dashboard", "emptyState", "poolEditor", "rpcSettings"].forEach((sectionId) => $(`#${sectionId}`).classList.toggle("hidden", sectionId !== id));
  $("#windowControls").classList.toggle("hidden", id !== "dashboard");
}

// ==== Pool configuration ====

function showPoolEditor(poolId = null) {
  resetPoolForm();
  state.view = "pool-form";
  showWorkspaceSection("poolEditor");
  closeSidebar();
  if (poolId) populatePoolForm(poolId);
  else {
    $("#workspaceTitle").textContent = "ADD POOL";
    $("#poolFormTitle").textContent = "ADD POOL";
    $("#savePool").textContent = "ADD POOL";
    $("#poolAddressInput").focus();
  }
}

function poolPayload() {
  return {
    name: $("#poolNameInput").value.trim(),
    chain_key: $("#poolChainInput").value,
    address: $("#poolAddressInput").value.trim(),
    start_block: $("#startBlockInput").value ? Number($("#startBlockInput").value) : null,
    mtm_mode: $("#mtmModeInput").value,
    assets: $$(".asset-config").map((field) => ({
      address: field.querySelector(".token-address").value.trim(),
      price_kind: field.querySelector(".price-kind").value,
      market: field.querySelector(".market").value.trim() || null,
      market_type: field.querySelector(".market-type").value,
      fixed_price: field.querySelector(".fixed-price").value ? Number(field.querySelector(".fixed-price").value) : null,
    })),
  };
}

async function discoverPool() {
  if (state.editingPoolId) return;
  const chainKey = $("#poolChainInput").value;
  const address = $("#poolAddressInput").value.trim();
  const discoveryKey = `${chainKey}:${address.toLowerCase()}`;
  if (state.discoveredKey === discoveryKey || state.discoveryRequestKey === discoveryKey) return;
  state.discoveryFeedback?.();
  state.discoveryRequestKey = discoveryKey;
  setFormStatus("poolFormStatus", "SCANNING RECENT POOL TRANSFERS AND DETECTING ASSETS…");
  $("#previewPool").disabled = true;
  setDiscovering(true);
  const finish = requestFeedback($("#poolFormStatus"), "Discovering pool assets");
  state.discoveryFeedback = finish;
  try {
    const result = await api("/api/pools/discover", { method: "POST", body: { chain_key: chainKey, address } });
    if (state.discoveryRequestKey !== discoveryKey) return;
    const chainName = (key) => state.bootstrap.chains.find((chain) => chain.key === key)?.name?.toUpperCase() || key;
    const switched = result.chain_key !== chainKey
      ? `NO CONTRACT ON ${chainName(chainKey)}; SWITCHED TO ${chainName(result.chain_key)}. ` : "";
    $("#poolChainInput").value = result.chain_key;
    $("#poolNameInput").value = result.generated_name;
    $$(".asset-config").forEach((field, index) => {
      const asset = result.assets[index];
      field.classList.remove("hidden");
      field.querySelector(".token-address").value = asset.address;
      field.querySelector(".price-kind").value = asset.price_kind || "none";
      field.querySelector(".market").value = asset.market || "";
      field.querySelector(".market-type").value = asset.market_type || "spot";
      field.querySelector(".fixed-price").value = asset.fixed_price ?? "";
      syncPriceInputs(field);
      const source = asset.transfer_count == null ? "CONTRACT METADATA" : `${asset.transfer_count} TRANSFERS`;
      field.querySelector(".detected").innerHTML = `<span class="token-symbol">${escapeHtml(asset.symbol)}</span> · ${asset.decimals} DECIMALS · ${source}`;
    });
    state.discoveredKey = `${result.chain_key}:${address.toLowerCase()}`;
    $("#savePool").disabled = false;
    setFormStatus("poolFormStatus", `${switched}DETECTED ${result.assets.map((asset) => asset.symbol).join(" / ")} THROUGH ${result.rpc}. PREVIOUS PRICE SETTINGS WERE REUSED WHERE AVAILABLE.`);
  } catch (error) {
    if (state.discoveryRequestKey === discoveryKey) {
      invalidateDiscovery();
      setFormStatus("poolFormStatus", error.message, true);
    }
  } finally {
    finish();
    if (state.discoveryRequestKey === discoveryKey) {
      setDiscovering(false);
      state.discoveryRequestKey = null;
      $("#previewPool").disabled = false;
    }
  }
}

// Metric's public API allows any origin, so the browser reads it directly.
const METRIC_API = "https://api.metric.xyz/public/v1";

async function loadMetadataPools() {
  $("#metadataButton").disabled = true;
  setFormStatus("poolFormStatus", "LOADING POOLS FROM METRIC METADATA…");
  try {
    const { chains } = await api(`${METRIC_API}/chains`);
    const supported = chains.flatMap((remote) => {
      const local = state.bootstrap.chains.find((chain) => chain.chain_id === remote.chainId);
      return remote.type === "evm" && local ? [local] : [];
    });
    const pages = await Promise.all(supported.map((chain) => api(`${METRIC_API}/evm/${chain.chain_id}/metadata?count=50&sortBy=tvl`)));
    state.metadataGroups = supported.map((chain, index) => ({ chain, pools: pages[index].data })).filter((group) => group.pools.length);
    $("#metadataSearch").value = "";
    renderMetadataPools();
    $("#metadataPanel").classList.remove("hidden");
    $("#metadataSearch").focus();
    const total = state.metadataGroups.reduce((sum, group) => sum + group.pools.length, 0);
    setFormStatus("poolFormStatus", `${total} METRIC POOLS ON ${state.metadataGroups.length} CHAINS · SELECT ONE TO FILL THE FORM`);
  } catch (error) {
    setFormStatus("poolFormStatus", `METRIC METADATA UNAVAILABLE: ${error.message}`, true);
  } finally {
    $("#metadataButton").disabled = false;
  }
}

function renderMetadataPools() {
  // One collapsible group per chain keeps the list short; a search opens every group with a match.
  const query = $("#metadataSearch").value.trim().toLowerCase();
  const tracked = new Set(state.bootstrap.pools.map((pool) => `${pool.chain_key}:${pool.address.toLowerCase()}`));
  const groups = state.metadataGroups.map(({ chain, pools }) => {
    const matches = pools.filter((pool) => !query || [chain.name, pool.pair, pool.poolAddress, pool.token0, pool.token1,
      pool.token0Name, pool.token1Name, pool.adminUsername, pool.admin].some((value) => String(value ?? "").toLowerCase().includes(query)));
    const rows = matches.map((pool) => {
      const isTracked = tracked.has(`${chain.key}:${pool.poolAddress.toLowerCase()}`);
      return `<button type="button" class="metadata-pool" data-chain-key="${escapeHtml(chain.key)}" data-address="${escapeHtml(pool.poolAddress)}"
        data-start-block="${escapeHtml(pool.createdBlockNumber ?? "")}" title="${escapeHtml(pool.poolAddress)}"${isTracked ? " disabled" : ""}>
        <b>${escapeHtml(pool.pair)}</b><span class="mono">${escapeHtml(compactAddress(pool.poolAddress))}</span>
        <span>${isTracked ? "TRACKED" : `<i>ADMIN</i> ${escapeHtml(pool.adminUsername || compactAddress(pool.admin))}`}</span>
      </button>`;
    });
    return rows.length ? `<details${query ? " open" : ""}><summary>${escapeHtml(chain.name)} <b>${rows.length} POOL${rows.length === 1 ? "" : "S"}</b></summary>${rows.join("")}</details>` : "";
  }).join("");
  $("#metadataPools").innerHTML = groups || `<p class="muted">${query ? "NO MATCHING POOLS" : "NO METRIC POOLS ON SUPPORTED CHAINS"}</p>`;
}

function selectMetadataPool(event) {
  const row = event.target.closest(".metadata-pool");
  if (!row || row.disabled) return;
  $("#metadataPanel").classList.add("hidden");
  $("#poolChainInput").value = row.dataset.chainKey;
  $("#poolAddressInput").value = row.dataset.address;
  invalidateDiscovery();
  $("#startBlockInput").value = row.dataset.startBlock;
  discoverPool();
}

async function savePool(event) {
  event.preventDefault();
  if ($("#savePool").disabled) return;
  $("#savePool").disabled = true;
  const payload = poolPayload();
  const poolId = state.editingPoolId;
  const finish = requestFeedback($("#poolFormStatus"), poolId ? "Saving pool" : "Validating and adding pool");
  try {
    if (poolId) {
      await api(`/api/pools/${encodeURIComponent(poolId)}`, { method: "PUT", body: { name: payload.name, start_block: payload.start_block, assets: payload.assets, mtm_mode: payload.mtm_mode } });
    } else {
      const result = await api("/api/pools", { method: "POST", body: payload });
      state.selectedPoolId = result.pool_id;
    }
    resetPoolForm();
    await refreshBootstrap();
    await showDashboard();
    notice(poolId ? "POOL UPDATED." : "POOL ADDED. INITIAL SYNCHRONIZATION IS QUEUED.");
  } catch (error) { setFormStatus("poolFormStatus", error.message, true); }
  finally { finish(); $("#savePool").disabled = false; }
}

function resetPoolForm() {
  state.discoveryFeedback?.();
  setDiscovering(false);
  state.editingPoolId = null;
  state.discoveredKey = null;
  state.discoveryRequestKey = null;
  $("#poolForm").reset();
  $("#mtmModeInput").value = "balanced";
  $("#editingPoolId").value = "";
  $("#poolFormTitle").textContent = "ADD POOL";
  $("#savePool").textContent = "ADD POOL";
  $("#poolChainInput").disabled = false;
  $("#poolAddressInput").disabled = false;
  $("#previewPool").classList.remove("hidden");
  $("#metadataPicker").classList.remove("hidden");
  $("#metadataPanel").classList.add("hidden");
  $$(".asset-config").forEach((field) => {
    field.classList.add("hidden");
    field.querySelector(".token-address").disabled = false;
    field.querySelector(".token-address").readOnly = true;
    field.querySelector(".detected").textContent = "";
    syncPriceInputs(field);
  });
  $("#savePool").disabled = true;
  setFormStatus("poolFormStatus", "");
}

function setDiscovering(on) {
  // The form locks while a lookup runs and empty discovered fields read "Discovering…"; placeholders come back after.
  $$("#poolForm fieldset").forEach((fieldset) => { fieldset.disabled = on; });
  const fields = $$(".asset-config");
  [$("#poolNameInput"), ...fields.flatMap((field) => [...field.querySelectorAll(".token-address, .market")])].forEach((input) => {
    input.dataset.placeholder ??= input.placeholder;
    input.placeholder = on ? "Discovering…" : input.dataset.placeholder;
  });
  if (on) fields.forEach((field) => {
    field.classList.remove("hidden");
    field.querySelector(".detected").textContent = "DISCOVERING…";
  });
}

function invalidateDiscovery() {
  if (state.editingPoolId) return;
  setDiscovering(false);
  state.discoveredKey = null;
  state.discoveryRequestKey = null;
  $("#previewPool").disabled = false;
  $("#poolNameInput").value = "";
  $("#savePool").disabled = true;
  $$(".asset-config").forEach((field) => {
    field.classList.add("hidden");
    field.querySelector(".token-address").value = "";
    field.querySelector(".detected").textContent = "";
  });
}

function editPool(poolId) {
  if (!poolId) return;
  showPoolEditor(poolId);
}

function populatePoolForm(poolId) {
  const pool = state.bootstrap.pools.find((row) => row.id === poolId);
  if (!pool) return showDashboard();
  state.editingPoolId = poolId;
  $("#editingPoolId").value = poolId;
  $("#workspaceTitle").textContent = "EDIT POOL";
  $("#poolFormTitle").textContent = `EDIT ${pool.name}`;
  $("#savePool").textContent = "SAVE CHANGES";
  $("#previewPool").classList.add("hidden");
  $("#metadataPicker").classList.add("hidden");
  $("#savePool").disabled = false;
  $("#poolNameInput").value = pool.name;
  $("#poolChainInput").value = pool.chain_key;
  $("#poolChainInput").disabled = true;
  $("#poolAddressInput").value = pool.address;
  $("#poolAddressInput").disabled = true;
  $("#startBlockInput").value = pool.start_block;
  $("#mtmModeInput").value = pool.mtm_mode;
  if (pool.assets.some((asset) => asset.price_kind === "bybit")) {
    setFormStatus("poolFormStatus", "Bybit pricing is retired. Choose Binance, Fixed USD, or None to resume pricing. Saving only the name preserves existing history.", true);
  }
  $$(".asset-config").forEach((field, index) => {
    const asset = pool.assets[index];
    field.classList.remove("hidden");
    field.querySelector(".token-address").value = asset.address;
    field.querySelector(".token-address").disabled = true;
    field.querySelector(".price-kind").value = asset.price_kind;
    field.querySelector(".market").value = asset.market || "";
    field.querySelector(".market-type").value = asset.market_type || "spot";
    field.querySelector(".fixed-price").value = asset.fixed_price ?? "";
    field.querySelector(".detected").innerHTML = `<span class="token-symbol">${escapeHtml(asset.symbol)}</span> · ${asset.decimals} DECIMALS`;
    syncPriceInputs(field);
  });
  $("#poolNameInput").focus();
}

async function removePool(poolId) {
  const pool = state.bootstrap?.pools.find((row) => row.id === poolId);
  if (!pool) return;
  try {
    if (!confirm(`Permanently remove ${pool.name} and its local data?`)) return;
    // Deleting a pool with long history takes several seconds on the backend.
    setUiLocked(true, `REMOVING ${pool.name.toUpperCase()}…`);
    await api(`/api/pools/${encodeURIComponent(poolId)}`, { method: "DELETE" });
    if (state.selectedPoolId === poolId) state.selectedPoolId = null;
    await refreshBootstrap();
    setUiLocked(false);
    await showDashboard();
    notice("POOL REMOVED.");
  } catch (error) {
    setUiLocked(false);
    notice(error.message, true);
  }
}

// ==== Network configuration ====

function showRpcSettings() {
  state.view = "rpc-form";
  showWorkspaceSection("rpcSettings");
  $("#workspaceTitle").textContent = "NETWORK CONFIGURATION";
  renderRpcSettings();
  closeSidebar();
  $("#envioApiKey").focus();
}

function renderRpcSettings() {
  const envio = state.bootstrap?.envio || { configured: false, rpm_limit: 15 };
  $("#envioKeyState").textContent = envio.configured ? "(configured; blank keeps current)" : "(required for ingestion)";
  $("#envioApiKey").value = "";
  $("#envioApiKey").placeholder = envio.configured ? "Configured" : "Enter key";
  $("#envioRpmLimit").value = envio.rpm_limit || 15;
  $("#clearEnvioKey").disabled = !envio.configured;
  $("#rpcRows").innerHTML = `<div class="rpc-table-heading"><span>CHAIN</span><span>ACTIVE ENDPOINT</span><span>CUSTOM OVERRIDE</span><span>ACTIONS</span></div>${(state.bootstrap?.chains || []).map((chain) => `
    <div class="rpc-row" data-chain="${escapeHtml(chain.key)}">
      <span class="rpc-chain"><strong>${escapeHtml(chain.name)}</strong><small>${chain.has_custom_rpc ? "CUSTOM" : "DEFAULT"}</small></span>
      <span class="rpc-active mono" title="Defaults: ${escapeHtml(chain.public_rpcs.join(" · "))}">${escapeHtml(chain.custom_rpc_display || chain.public_rpcs[0])}</span>
      <input type="url" class="rpc-url mono" aria-label="${escapeHtml(chain.name)} custom RPC URL" placeholder="${escapeHtml(chain.custom_rpc_display || "https://…")}">
      <span class="rpc-actions">
        <button type="button" data-rpc-action="test">TEST</button>
        <button type="button" data-rpc-action="save" class="primary">SAVE</button>
        <button type="button" data-rpc-action="clear" class="danger" ${chain.has_custom_rpc ? "" : "disabled"}>RESET</button>
      </span>
      <p class="rpc-result mono" aria-live="polite"></p>
    </div>`).join("")}`;
}

async function saveEnvioSettings() {
  const result = $("#envioResult");
  result.className = "rpc-result mono";
  try {
    await api("/api/envio/settings", {
      method: "PUT",
      body: {
        api_key: $("#envioApiKey").value || null,
        rpm_limit: Number($("#envioRpmLimit").value),
      },
    });
    await refreshBootstrap();
    renderRpcSettings();
    $("#envioResult").textContent = "ENVIO SETTINGS SAVED.";
  } catch (error) {
    result.textContent = error.message;
    result.className = "rpc-result mono error";
  }
}

async function clearEnvioKey() {
  if (!confirm("Clear the Envio API key? Log ingestion will stop until another key is saved.")) return;
  try {
    await api("/api/envio/key", { method: "DELETE" });
    await refreshBootstrap();
    renderRpcSettings();
    $("#envioResult").textContent = "ENVIO API KEY CLEARED.";
  } catch (error) {
    $("#envioResult").textContent = error.message;
    $("#envioResult").className = "rpc-result mono error";
  }
}

async function handleRpcAction(event) {
  const button = event.target.closest("button[data-rpc-action]");
  if (!button || button.disabled) return;
  const row = button.closest(".rpc-row");
  const chainKey = row.dataset.chain;
  const input = row.querySelector(".rpc-url");
  const resultNode = row.querySelector(".rpc-result");
  const action = button.dataset.rpcAction;
  resultNode.className = "rpc-result mono";
  const finish = requestFeedback(resultNode, `${action.toUpperCase()} RPC`, [...row.querySelectorAll("button")]);
  try {
    if (action === "test") {
      resultNode.textContent = "TESTING ENDPOINT…";
      const result = await api("/api/rpc/test", { method: "POST", body: { chain_key: chainKey, url: input.value || null } });
      resultNode.textContent = `${result.rpc} · CHAIN ${result.chain_id} · BLOCK ${formatInteger(result.latest_block)}`;
      return;
    }
    if (action === "save") {
      if (!input.value) throw new Error("Enter a custom RPC URL first.");
      await api(`/api/chains/${encodeURIComponent(chainKey)}/rpc`, { method: "PUT", body: { url: input.value } });
    } else {
      await api(`/api/chains/${encodeURIComponent(chainKey)}/rpc`, { method: "DELETE" });
    }
    await refreshBootstrap();
    renderRpcSettings();
    const refreshed = $(`.rpc-row[data-chain="${chainKey}"] .rpc-result`);
    refreshed.textContent = action === "save" ? "CUSTOM RPC SAVED AND SELECTED." : "CUSTOM OVERRIDE CLEARED; DEFAULTS ARE ACTIVE.";
  } catch (error) {
    resultNode.textContent = error.message;
    resultNode.className = "rpc-result mono error";
  } finally { finish(); }
}

function requestFeedback(node, action, buttons = []) {
  const started = Date.now();
  buttons.forEach((button) => { button.disabled = true; });
  const update = () => {
    const seconds = Math.floor((Date.now() - started) / 1000);
    node.textContent = `${action} · ${seconds}s${seconds >= 15 ? " · taking longer than expected; waiting for server response" : ""}`;
  };
  update();
  const timer = setInterval(update, 1000);
  return () => { clearInterval(timer); buttons.forEach((button) => { button.disabled = false; }); };
}

// ==== UI utilities ====

function setUiLocked(locked, message = "LOADING POOL") {
  state.uiLocked = locked;
  const shell = $(".app-shell");
  const overlay = $("#uiLock");
  shell.inert = locked;
  shell.setAttribute("aria-busy", String(locked));
  overlay.classList.toggle("hidden", !locked);
  overlay.setAttribute("aria-hidden", String(!locked));
  overlay.setAttribute("aria-label", message);
  if (locked) overlay.focus();
  else $(".pool-row.selected")?.focus();
}

function syncPriceInputs(field) {
  // Only the inputs the chosen source uses are shown: Market/Type for exchanges, the USD value for Fixed.
  const kind = field.querySelector(".price-kind").value;
  const exchange = kind === "binance" || kind === "bybit";
  const fixed = field.querySelector(".fixed-price");
  fixed.disabled = kind !== "fixed";
  fixed.closest("label").classList.toggle("hidden", kind !== "fixed");
  field.querySelectorAll(".market, .market-type").forEach((input) => input.closest("label").classList.toggle("hidden", !exchange));
}

function selectedPool() { return state.bootstrap?.pools.find((pool) => pool.id === state.selectedPoolId) || null; }

function summaryRow(label, summary = {}, pnlKey = "flow_pnl_usd", aprKey = "flow_apr_pct") {
  return `<tr class="chain-total">
    <td>${escapeHtml(label)}</td>
    <td>${compactUsd(summary?.volume_usd)}${summary?.volume_usd != null && summary?.volume_complete === false ? " PARTIAL" : ""}</td>
    <td class="${signedClass(summary?.[pnlKey])}">${compactUsd(summary?.[pnlKey], true)}${summary?.[pnlKey] != null && summary?.[pnlKey.startsWith("flow") ? "flow_complete" : "mtm_complete"] === false ? " PARTIAL" : ""}</td>
    <td class="${signedClass(summary?.[aprKey])}">${percent(summary?.[aprKey])}</td>
    <td>${compactUsd(summary?.tvl_usd)}</td>
    <td>—</td>
    <td>—</td>
  </tr>`;
}

function toggleCounterpartySide(event) {
  event.preventDefault(); // the button sits in the panel's <summary>; don't also collapse it
  state.counterpartySide = state.counterpartySide === "recipient" ? "sender" : "recipient";
  localStorage.setItem("openPoolCounterpartySide", state.counterpartySide);
  if (state.currentDashboard) loadDashboard();
}

async function loadKnownAddresses() {
  // Built-in tags for known aggregator routers; web/known_aggregators.json maps a name to its addresses.
  try {
    const response = await fetch("/static/known_aggregators.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const groups = await response.json();
    state.knownAddresses = Object.fromEntries(Object.entries(groups).flatMap(([name, addresses]) => addresses.map((address) => [address.toLowerCase(), name])));
  } catch (error) {
    console.warn("Known aggregator tags unavailable:", error);
    return;
  }
  if (!state.currentDashboard) return;
  renderCounterparties(state.currentDashboard);
  renderSwaps(state.currentDashboard);
}

function editAddressLabel(event) {
  // Labels are a per-browser convenience; they never reach the backend.
  const address = event.target.closest("[data-label-address]")?.dataset.labelAddress;
  if (!address) return;
  const entered = prompt(`Label for ${address} (leave empty to remove):`, state.addressLabels[address] || "");
  if (entered == null) return;
  if (entered.trim()) state.addressLabels[address] = entered.trim();
  else delete state.addressLabels[address];
  localStorage.setItem("openPoolAddressLabels", JSON.stringify(state.addressLabels));
  if (!state.currentDashboard) return;
  renderCounterparties(state.currentDashboard);
  renderSwaps(state.currentDashboard);
}

function selectListPnlMode(mode) {
  if (state.uiLocked || state.listPnlMode === mode) return;
  state.listPnlMode = mode;
  localStorage.setItem("openPoolListPnlMode", state.listPnlMode);
  renderPoolManagement();
}

async function selectWindow(event, stateKey, storageKey, refresh) {
  if (state.uiLocked) return;
  const button = event.target.closest("button");
  if (!button || !button.closest(".window-controls")) return;
  let hours = button.dataset.hours ? Number(button.dataset.hours) : null;
  if (button.hasAttribute("data-custom-window")) {
    const entered = prompt("Enter a window from 1h to 30d (examples: 12h, 3d):", formatWindowLabel(state[stateKey]));
    if (entered == null) return;
    hours = parseWindowHours(entered);
    if (hours == null) return notice("TIME WINDOW MUST BE BETWEEN 1H AND 30D.", true);
  }
  if (hours == null) return;
  state[stateKey] = hours;
  localStorage.setItem(storageKey, String(hours));
  renderWindowControls(`#${button.closest(".window-controls").id}`, hours);
  setUiLocked(true, stateKey === "hours" ? "LOADING POOL WINDOW" : "LOADING POOL SUMMARIES");
  try {
    await refresh();
  } finally {
    setUiLocked(false);
  }
}

function renderWindowControls(selector, hours) {
  const presets = [6, 24, 168, 720];
  $(`${selector} [data-custom-window]`).textContent = presets.includes(hours)
    ? "CUSTOM" : formatWindowLabel(hours);
  $$(`${selector} button`).forEach((button) => button.classList.toggle(
    "selected",
    button.dataset.hours ? Number(button.dataset.hours) === hours : !presets.includes(hours),
  ));
}

function storedWindow(key, fallback) {
  const value = Number(localStorage.getItem(key));
  return Number.isFinite(value) && value >= 1 && value <= 720 ? value : fallback;
}

function storedPlots() {
  const requested = new Set(String(localStorage.getItem("openPoolPlots") || "pnl,price").split(","));
  const plots = PLOT_IDS.filter((id) => requested.has(id));
  return plots.length ? plots : ["pnl", "price"];
}

function storedMinimumSwapUsd() {
  const stored = localStorage.getItem("openPoolMinimumSwapUsd");
  if (stored == null) return 100;
  const value = Number(stored);
  return Number.isFinite(value) && value >= 0 ? value : 100;
}

function parseWindowHours(value) {
  const match = String(value).trim().match(/^(\d+(?:\.\d+)?)\s*([hd])?$/i);
  if (!match) return null;
  const hours = Number(match[1]) * (match[2]?.toLowerCase() === "d" ? 24 : 1);
  return hours >= 1 && hours <= 720 ? hours : null;
}

function formatWindowLabel(value) {
  const hours = Number(value);
  if (hours === 24) return "24H";
  if (hours >= 24 && hours % 24 === 0) return `${formatCompact(hours / 24)}D`;
  return `${formatCompact(hours)}H`;
}

function toggleSidebar() {
  const open = !$("#sidebar").classList.contains("open");
  $("#sidebar").classList.toggle("open", open);
  $("#sidebarScrim").classList.toggle("hidden", !open);
  $("#sidebarToggle").setAttribute("aria-expanded", String(open));
}

function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#sidebarScrim").classList.add("hidden");
  $("#sidebarToggle").setAttribute("aria-expanded", "false");
}

function setSignedMetric(node, value) { node.textContent = money(value); node.className = signedClass(value); }
function signedClass(value) { return value > 0 ? "positive" : value < 0 ? "negative" : ""; }
function money(value, digits = 2) { return value == null ? "—" : new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: digits }).format(value); }
function formatNumber(value) { return new Intl.NumberFormat(undefined, { maximumSignificantDigits: 7 }).format(value); }
function formatInteger(value) { return new Intl.NumberFormat().format(value); }
function compactUsd(value, showSign = false) {
  if (value == null) return "—";
  const number = Number(value);
  const prefix = showSign && number > 0 ? "+" : number < 0 ? "−" : "";
  const absolute = Math.abs(number);
  const scaled = absolute >= 1e6 ? `${formatCompact(absolute / 1e6)}m` : absolute >= 1e3 ? `${formatCompact(absolute / 1e3)}k` : formatCompact(absolute);
  return `${prefix}$${scaled}`;
}
function formatCompact(value) { return new Intl.NumberFormat(undefined, { maximumFractionDigits: value >= 100 ? 0 : value >= 10 ? 1 : 2 }).format(value); }
function formatHoursAgo(value, now = Date.now()) {
  if (!value || !Number.isFinite(Date.parse(value))) return "—";
  const hours = Math.max(0, now - Date.parse(value)) / 3_600_000;
  return hours < 0.1 ? "<0.1" : new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(hours);
}
function percent(value) { return value == null ? "—" : `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(Number(value))}%`; }
function bps(value) { return value == null ? "—" : `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2, signDisplay: "exceptZero" }).format(value)} BPS`; }
function signed(value) { return `${value > 0 ? "+" : ""}${formatNumber(value)}`; }
function formatTime(value) { return new Date(value).toLocaleString(); }
function formatCompactTime(value) { return new Date(value).toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
function compactAddress(value) { return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : ""; }
function counterpartyLink(value, template) {
  const address = String(value || "").trim().toLowerCase();
  if (!address) return `<span class="muted">UNKNOWN</span>`;
  // A user label wins over the built-in aggregator tag, so a wrong or stale tag can be overridden.
  const custom = state.addressLabels[address];
  const known = state.knownAddresses[address];
  const label = escapeHtml(custom || known || compactAddress(address));
  const className = custom ? "address-label" : known ? "address-label known-address" : "mono";
  const editor = `<button class="label-button" type="button" data-label-address="${escapeHtml(address)}" title="${custom ? "Edit" : "Add"} label">✎</button>`;
  if (!template) return `<span class="${className}" title="${escapeHtml(address)}">${label}</span>${editor}`;
  const url = String(template).replace("{address}", address);
  return `<a class="${className}" href="${escapeHtml(url)}" title="${escapeHtml(address)}" target="_blank" rel="noreferrer">${label} ↗</a>${editor}`;
}
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }
function setFormStatus(id, message, error = false) { const node = $(`#${id}`); node.textContent = message; node.className = `form-status mono ${error ? "error" : ""}`; }
function notice(message, error = false) {
  const node = $("#notice");
  node.textContent = message;
  node.className = `notice ${error ? "error" : ""}`;
  // Confirmations fade out on their own (live progress is in the activity area); errors stay until replaced.
  clearTimeout(state.noticeTimer);
  if (!error) state.noticeTimer = setTimeout(() => node.classList.add("hidden"), 6000);
}

export { changeSwapPage, applyChartRange, discoverPool, editAddressLabel, loadKnownAddresses, loadMetadataPools, renderMetadataPools, selectMetadataPool, pollStatus, configurePlots, formatHoursAgo, formatWindowLabel, loadDashboard, notice, parseWindowHours, refreshRuntime, removePool, renderCounterparties, renderRpcSettings, renderSwaps, selectWindow, state, syncPriceInputs, toggleChartFullscreen, toggleCounterpartySide, selectListPnlMode };
