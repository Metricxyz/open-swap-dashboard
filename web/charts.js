const layoutBase = {
  margin: { l: 58, r: 22, t: 24, b: 42 },
  paper_bgcolor: "transparent",
  plot_bgcolor: "#101216",
  font: { color: "#858d96", family: "Consolas, Liberation Mono, monospace", size: 10 },
  xaxis: {
    gridcolor: "#30363d",
    linecolor: "#515862",
    mirror: true,
    ticks: "outside",
    tickcolor: "#515862",
    zeroline: false,
  },
  yaxis: {
    gridcolor: "#30363d",
    linecolor: "#515862",
    mirror: true,
    ticks: "outside",
    tickcolor: "#515862",
    zerolinecolor: "#707983",
  },
  hovermode: "x unified",
  hoverlabel: { bgcolor: "#24282d", bordercolor: "#737c86", font: { family: "Consolas, Liberation Mono, monospace", size: 11 } },
  legend: { orientation: "h", y: 1.12, x: 0, bgcolor: "transparent" },
};

export function plotlyTimestamp(value) {
  if (typeof value === "number") return value;
  const text = String(value);
  return Date.parse(/(?:z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text.replace(" ", "T")}Z`);
}

function chartLayout(range, extra = {}, revision = "dashboard") {
  return {
    ...layoutBase,
    ...extra,
    uirevision: revision,
    // Plotly mutates axis settings during zoom; each chart must own its axis.
    yaxis: { ...layoutBase.yaxis },
    xaxis: {
      ...layoutBase.xaxis,
      ...(range ? { range, autorange: false } : {}),
    },
  };
}

function bindTimeRange(chartId, callback, rendered) {
  if (!callback || typeof document === "undefined") return;
  const bind = () => {
    const chart = document.getElementById?.(chartId) || document.querySelector?.(`#${chartId}`);
    if (!chart?.on) return;
    if (chart.dashboardRangeHandler) chart.removeListener?.("plotly_relayout", chart.dashboardRangeHandler);
    chart.dashboardRangeHandler = (event) => {
      const change = timeRangeFromRelayout(event);
      if (change.changed) callback(change.range);
    };
    chart.on("plotly_relayout", chart.dashboardRangeHandler);
  };
  if (rendered?.then) rendered.then(bind); else bind();
}

function bindPriceInteractions(explorerTxUrl, groups, asset0, rendered) {
  if (typeof document === "undefined") return;
  const bind = () => {
    const chart = document.getElementById?.("priceChart") || document.querySelector?.("#priceChart");
    if (!chart?.on) return;
    if (!document.createElement || !chart.appendChild) return;
    const tooltip = chart.dashboardPriceTooltip || document.createElement("div");
    tooltip.className = "price-hover-tooltip";
    if (!chart.dashboardPriceTooltip) {
      tooltip.hidden = true;
      chart.appendChild(tooltip);
    }
    chart.dashboardPriceTooltip = tooltip;
    tooltip.dashboardTimestamp = null;
    tooltip.hidden = true;
    chart.dashboardSelectedMarker?.classList.remove("price-selected-swap");
    chart.dashboardSelectedMarker = null;
    chart.dashboardSelectedSwap = null;
    const markerNodes = {};
    for (const [side, traceIndex] of [["buy", 2], ["sell", 3]]) {
      const uid = chart._fullData?.[traceIndex]?.uid;
      markerNodes[side] = uid ? chart.querySelectorAll?.(`.trace${uid} .point`) : [];
    }
    if (chart.dashboardPriceHoverHandler) chart.removeListener?.("plotly_hover", chart.dashboardPriceHoverHandler);
    if (chart.dashboardPriceUnhoverHandler) chart.removeListener?.("plotly_unhover", chart.dashboardPriceUnhoverHandler);
    const plotArea = chart.querySelector?.(".nsewdrag") || chart;
    const hoverTarget = chart;
    chart.dashboardPriceHoverTarget?.removeEventListener?.("mousemove", chart.dashboardPriceMoveHandler);
    chart.dashboardPriceHoverTarget?.removeEventListener?.("mouseleave", chart.dashboardPriceLeaveHandler);
    chart.dashboardPriceHoverTarget?.removeEventListener?.("click", chart.dashboardPriceClickHandler);
    const timestamps = [...groups.keys()].sort((left, right) => left - right);
    chart.dashboardPriceHoverHandler = (event) => {
      clearTimeout(chart.dashboardPriceHideTimer);
      const bounds = plotArea.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right
          || event.clientY < bounds.top || event.clientY > bounds.bottom) {
        tooltip.hidden = true;
        chart.dashboardSelectedMarker?.classList.remove("price-selected-swap");
        chart.dashboardSelectedSwap = null;
        return;
      }
      const range = chart._fullLayout?.xaxis?.range;
      const start = plotlyTimestamp(range?.[0]);
      const end = plotlyTimestamp(range?.[1]);
      if (!bounds.width || !Number.isFinite(start) || !Number.isFinite(end) || !timestamps.length) return;
      const target = start + Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)) * (end - start);
      let low = 0;
      let high = timestamps.length;
      while (low < high) {
        const middle = (low + high) >> 1;
        if (timestamps[middle] < target) low = middle + 1; else high = middle;
      }
      const after = timestamps[Math.min(low, timestamps.length - 1)];
      const before = timestamps[Math.max(0, low - 1)];
      const timestamp = Math.abs(after - target) < Math.abs(target - before) ? after : before;
      const trades = groups.get(timestamp);
      if (!trades) return;
      const yRange = chart._fullLayout?.yaxis?.range?.map(Number);
      const yPosition = (point) => bounds.bottom
        - (point.execution - yRange[0]) / (yRange[1] - yRange[0]) * bounds.height;
      const selected = yRange?.every(Number.isFinite) && yRange[0] !== yRange[1]
        ? trades.reduce((nearest, point) => Math.abs(yPosition(point) - event.clientY) < Math.abs(yPosition(nearest) - event.clientY) ? point : nearest)
        : trades[0];
      chart.dashboardSelectedSwap = selected;
      const selectedMarker = markerNodes[selected.side]?.[selected.markerIndex];
      if (selectedMarker !== chart.dashboardSelectedMarker) {
        chart.dashboardSelectedMarker?.classList.remove("price-selected-swap");
        selectedMarker?.classList.add("price-selected-swap");
        chart.dashboardSelectedMarker = selectedMarker;
      }
      if (tooltip.dashboardTimestamp !== timestamp) {
        tooltip.textContent = priceHoverText(timestamp, trades, asset0);
        tooltip.dashboardTimestamp = timestamp;
      }
      tooltip.hidden = false;
      const tooltipBounds = tooltip.getBoundingClientRect();
      tooltip.style.left = `${Math.max(8, Math.min(event.clientX + 12, window.innerWidth - tooltipBounds.width - 8))}px`;
      tooltip.style.top = `${Math.max(8, Math.min(event.clientY + 12, window.innerHeight - tooltipBounds.height - 8))}px`;
    };
    chart.dashboardPriceUnhoverHandler = () => {
      clearTimeout(chart.dashboardPriceHideTimer);
      chart.dashboardPriceHideTimer = setTimeout(() => {
        tooltip.hidden = true;
        chart.dashboardSelectedMarker?.classList.remove("price-selected-swap");
        chart.dashboardSelectedMarker = null;
        chart.dashboardSelectedSwap = null;
      }, 120);
    };
    // Plotly emits no plotly_click with hovermode off, so Ctrl/Cmd+click opens the swap the custom hover selected.
    chart.dashboardPriceClickHandler = (event) => {
      const txHash = chart.dashboardSelectedSwap?.txHash;
      if (explorerTxUrl && txHash && (event.ctrlKey || event.metaKey)) {
        window.open(explorerTxUrl.replace("{tx_hash}", txHash), "_blank", "noopener,noreferrer");
      }
    };
    chart.dashboardPriceMoveHandler = chart.dashboardPriceHoverHandler;
    chart.dashboardPriceLeaveHandler = chart.dashboardPriceUnhoverHandler;
    chart.dashboardPriceHoverTarget = hoverTarget;
    hoverTarget.addEventListener("mousemove", chart.dashboardPriceMoveHandler);
    hoverTarget.addEventListener("mouseleave", chart.dashboardPriceLeaveHandler);
    hoverTarget.addEventListener("click", chart.dashboardPriceClickHandler);
  };
  if (rendered?.then) rendered.then(bind); else bind();
}

export function timeRangeFromRelayout(event) {
  if (event?.["xaxis.autorange"] === true) return { changed: true, range: null };
  const range = event?.["xaxis.range"] || [event?.["xaxis.range[0]"], event?.["xaxis.range[1]"]];
  return range[0] == null || range[1] == null
    ? { changed: false, range: null }
    : { changed: true, range: range.map((value) => new Date(plotlyTimestamp(value)).toISOString()) };
}

export function setChartTimeRange(chartId, range) {
  if (!window.Plotly?.relayout) return;
  return window.Plotly.relayout(chartId, range
    ? { "xaxis.range": range, "xaxis.autorange": false }
    : { "xaxis.autorange": true });
}

export function renderPnlChart(points, { range = null, onRangeChange = null, revision = "dashboard", mtmMode = "as_is" } = {}) {
  if (!window.Plotly) return;
  const x = points.map((row) => row.timestamp);
  const mtmName = `${mtmMode === "balanced" ? "MTM 50/50" : "MTM AS IS"}${points.some((row) => row.mtm_mark === "pool_implied") ? " (POOL MARK)" : ""}`;
  const rendered = window.Plotly.react("pnlChart", [
    { x, y: points.map((row) => row.flow), name: "FLOW", mode: "lines", line: { color: "#78a387", width: 1.5 } },
    { x, y: points.map((row) => row.mtm), name: mtmName, mode: "lines", line: { color: "#9288bc", width: 1.5 } },
  ], chartLayout(range, {}, revision), { responsive: true, displayModeBar: false });
  bindTimeRange("pnlChart", onRangeChange, rendered);
}

export function renderBalanceChart(points, assets, { range = null, onRangeChange = null, revision = "dashboard" } = {}) {
  if (!window.Plotly) return;
  const x = points.map((row) => row.timestamp);
  const rendered = window.Plotly.react("balanceChart", [
    { x, y: points.map((row) => row.asset0), name: assets[0]?.symbol || "ASSET 1", mode: "lines", line: { color: "#78a387", width: 1.5 } },
    { x, y: points.map((row) => row.asset1), name: assets[1]?.symbol || "ASSET 2", mode: "lines", yaxis: "y2", line: { color: "#7896b2", width: 1.5 } },
  ], chartLayout(range, {
    yaxis2: { overlaying: "y", side: "right", showgrid: false, linecolor: "#515862", mirror: true, ticks: "outside", tickcolor: "#515862" },
  }, revision), { responsive: true, displayModeBar: false });
  bindTimeRange("balanceChart", onRangeChange, rendered);
}

export function pricePoints(swaps) {
  return swaps.flatMap((row) => {
    const delta0 = Number(row.asset0_delta);
    const delta1 = Number(row.asset1_delta);
    if (!delta0 || !delta1 || !Number.isFinite(delta0) || !Number.isFinite(delta1)) return [];
    const execution = Math.abs(delta1 / delta0);
    const price0 = Number(row.asset0_price);
    const price1 = Number(row.asset1_price);
    const reference = price0 > 0 && price1 > 0 ? price0 / price1 : null;
    const deviationBps = reference == null ? null : (execution / reference - 1) * 10_000;
    const volumeUsd = row.volume_usd == null ? null : Number(row.volume_usd);
    return [{
      timestamp: row.timestamp,
      execution,
      reference,
      deviationBps,
      side: delta0 > 0 ? "buy" : "sell",
      txHash: row.tx_hash,
      volumeUsd: Number.isFinite(volumeUsd) ? volumeUsd : null,
    }];
  }).sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp));
}

export function priceHoverText(timestamp, trades, asset0) {
  const reference = trades.find((point) => point.reference != null)?.reference;
  const sections = trades.map((point) => [
    `${point.side === "buy" ? "▲ POOL BUYS" : "▼ POOL SELLS"} ${asset0} : ${point.execution.toLocaleString(undefined, { maximumSignificantDigits: 10 })}`,
    point.volumeUsd == null ? "SWAP SIZE UNVALUED" : `SWAP SIZE $${point.volumeUsd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
    point.deviationBps == null ? "NO CEX REFERENCE" : `${point.deviationBps >= 0 ? "+" : ""}${point.deviationBps.toFixed(2)} BPS VS CEX`,
  ].join("\n"));
  return [
    new Date(timestamp).toLocaleString(),
    `${trades.length} ${trades.length === 1 ? "SWAP" : "SWAPS"}`,
    reference == null ? "NO CEX REFERENCE" : `CEX REFERENCE : ${reference.toLocaleString(undefined, { maximumSignificantDigits: 10 })}`,
    ...sections,
  ].join("\n────────────────\n");
}

export function swapMarkerSize(volumeUsd) {
  if (!Number.isFinite(volumeUsd) || volumeUsd <= 0) return 5;
  return Math.max(5, Math.min(23, 5 + 3 * Math.log10(volumeUsd)));
}

export function renderPriceChart(swaps, assets, { range = null, onRangeChange = null, revision = "dashboard", explorerTxUrl = null, referencePoints } = {}) {
  if (!window.Plotly) return;
  const points = pricePoints(swaps);
  const connectorX = [];
  const connectorY = [];
  for (const point of points) {
    if (point.reference == null) continue;
    connectorX.push(point.timestamp, point.timestamp, null);
    connectorY.push(point.reference, point.execution, null);
  }
  const referenceName = assets.some((asset) => asset.exchange) ? "CEX REFERENCE" : "REFERENCE";
  const asset0 = assets[0]?.symbol || "ASSET 1";
  const tradeTrace = (side, name, color, symbol) => {
    const trades = points.filter((point) => point.side === side);
    trades.forEach((point, markerIndex) => { point.markerIndex = markerIndex; });
    return {
      x: trades.map((point) => point.timestamp),
      y: trades.map((point) => point.execution),
      name,
      mode: "markers",
      marker: {
        color, size: trades.map((point) => swapMarkerSize(point.volumeUsd)),
        symbol, opacity: .9, line: { color: "#080a0d", width: 1 },
      },
      customdata: trades.map((point) => point.txHash),
      hoverinfo: "skip",
    };
  };
  const grouped = new Map();
  for (const point of points) {
    const timestamp = Date.parse(point.timestamp);
    const trades = grouped.get(timestamp);
    if (trades) trades.push(point); else grouped.set(timestamp, [point]);
  }
  const traces = [
    {
      x: connectorX, y: connectorY, mode: "lines", showlegend: false, hoverinfo: "skip",
      line: { color: "#515862", width: .75 }, opacity: .35,
    },
    {
      x: referencePoints.map((point) => point.timestamp),
      y: referencePoints.map((point) => point.reference),
      name: referenceName,
      mode: "lines",
      line: { color: "#71a9d6", width: 2.25 },
      hoverinfo: "skip",
    },
    tradeTrace("buy", `POOL BUYS ${asset0}`, "#78a387", "triangle-up"),
    tradeTrace("sell", `POOL SELLS ${asset0}`, "#bd7e82", "triangle-down"),
  ];
  const rendered = window.Plotly.react("priceChart", traces, chartLayout(range, {
    hovermode: false,
    hoverlabel: { ...layoutBase.hoverlabel, align: "left" },
  }, revision), { responsive: true, displayModeBar: false });
  bindTimeRange("priceChart", onRangeChange, rendered);
  bindPriceInteractions(explorerTxUrl, grouped, asset0, rendered);
}

export function renderCumulativeVolumeChart(points, { range = null, onRangeChange = null, revision = "dashboard" } = {}) {
  if (!window.Plotly) return;
  const rendered = window.Plotly.react("volumeChart", [{
    x: points.map((point) => point.timestamp),
    y: points.map((point) => point.volume),
    name: "VOLUME",
    mode: "lines",
    line: { color: "#7896b2", width: 1.5, shape: "hv" },
    fill: "tozeroy",
    fillcolor: "rgba(120,150,178,.12)",
    hovertemplate: "$%{y:,.2f}<extra>CUMULATIVE VOLUME</extra>",
  }], chartLayout(range, {}, revision), { responsive: true, displayModeBar: false });
  bindTimeRange("volumeChart", onRangeChange, rendered);
}
