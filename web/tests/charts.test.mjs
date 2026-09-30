import assert from "node:assert/strict";
import test from "node:test";

import {
  plotlyTimestamp, priceHoverText, pricePoints, renderBalanceChart, renderCumulativeVolumeChart,
  renderPnlChart, renderPriceChart, setChartTimeRange, swapMarkerSize, timeRangeFromRelayout,
} from "../charts.js";


test("chart rendering is optional when Plotly is unavailable", () => {
  globalThis.window = {};
  renderPnlChart([]);
  renderBalanceChart([], []);
  renderPriceChart([], []);
  renderCumulativeVolumeChart([]);
});


test("timezone-less Plotly axis values are interpreted as UTC", () => {
  assert.equal(plotlyTimestamp("2026-09-01 13:18:53"), Date.parse("2026-09-01T13:18:53Z"));
  assert.equal(plotlyTimestamp("2026-09-01T13:18:53-03:00"), Date.parse("2026-09-01T13:18:53-03:00"));
});


test("price history compares each pool execution with its contemporaneous CEX cross", () => {
  const swaps = [
    { tx_hash: "0xbuy", timestamp: "2026-01-01T00:01:00Z", asset0_delta: 1, asset1_delta: -90, asset0_price: 100, asset1_price: 1, volume_usd: 100 },
    { tx_hash: "0xsell", timestamp: "2026-01-01T00:01:00Z", asset0_delta: -2, asset1_delta: 220, asset0_price: 100, asset1_price: 1, volume_usd: 220 },
    { timestamp: "2026-01-01T00:03:00Z", asset0_delta: 1, asset1_delta: -95, asset0_price: null, asset1_price: 1 },
  ];
  const points = pricePoints(swaps);
  assert.deepEqual(points.map((point) => point.execution), [90, 110, 95]);
  assert.deepEqual(points.map((point) => point.reference), [100, 100, null]);
  assert.deepEqual(points.slice(0, 2).map((point) => Math.round(point.deviationBps)), [-1000, 1000]);

  const calls = [];
  globalThis.window = { Plotly: { react: (...args) => calls.push(args) } };
  renderPriceChart(swaps, [{ symbol: "WETH", exchange: "binance" }, { symbol: "USDC" }], { referencePoints: points });

  assert.equal(calls[0][0], "priceChart");
  assert.deepEqual(calls[0][1].map((trace) => trace.name), [undefined, "CEX REFERENCE", "POOL BUYS WETH", "POOL SELLS WETH"]);
  assert.equal(calls[0][1][2].marker.symbol, "triangle-up");
  assert.equal(calls[0][1][3].marker.symbol, "triangle-down");
  assert.deepEqual(calls[0][1][2].marker.line, { color: "#080a0d", width: 1 });
  assert.deepEqual(calls[0][1][2].customdata, ["0xbuy", undefined]);
  assert.deepEqual(calls[0][1][2].marker.size, [11, 5]);
  assert.equal(calls[0][2].hovermode, false);
  const hover = priceHoverText(points[0].timestamp, points.slice(0, 2), "WETH");
  assert.match(hover, /2 SWAPS/);
  assert.match(hover, /SWAP SIZE \$100\.00/);
  assert.match(hover, /────────────────/);
  assert.equal(calls[0][1][0].x.length, 6);

  renderPriceChart(swaps.slice(1), [{ symbol: "WETH", exchange: "binance" }, { symbol: "USDC" }], { referencePoints: points });
  assert.equal(calls[1][1][1].x.length, 3);
  assert.equal(calls[1][1][0].x.length, 3);
  assert.deepEqual(calls[1][1][2].customdata, [undefined]);
  assert.deepEqual(calls[1][1][3].customdata, ["0xsell"]);
});


test("swap marker sizes scale logarithmically and stay within readable bounds", () => {
  assert.equal(swapMarkerSize(null), 5);
  assert.equal(swapMarkerSize(1), 5);
  assert.equal(swapMarkerSize(10), 8);
  assert.equal(swapMarkerSize(100), 11);
  assert.equal(swapMarkerSize(1_000), 14);
  assert.equal(swapMarkerSize(100_000), 20);
  assert.equal(swapMarkerSize(10_000_000), 23);
});


test("ctrl-clicking the hovered price-history trade opens its transaction explorer", () => {
  const handlers = {};
  let opened;
  const plotArea = { getBoundingClientRect: () => ({ left: 0, right: 1000, top: 0, bottom: 500, width: 1000, height: 500 }) };
  const chart = {
    _fullLayout: { xaxis: { range: ["2025-12-31T23:59:00Z", "2026-01-01T00:01:00Z"] }, yaxis: { range: [90, 110] } },
    on() {},
    removeListener() {},
    addEventListener: (event, handler) => { handlers[event] = handler; },
    removeEventListener() {},
    appendChild() {},
    querySelector: (selector) => selector === ".nsewdrag" ? plotArea : null,
    querySelectorAll: () => [],
  };
  const tooltip = { hidden: true, style: {}, getBoundingClientRect: () => ({ width: 200, height: 120 }) };
  globalThis.document = { createElement: () => tooltip, getElementById: () => chart, querySelector: () => chart };
  globalThis.window = { Plotly: { react() {} }, innerWidth: 1200, innerHeight: 800, open: (...args) => { opened = args; } };
  renderPriceChart(
    [{ tx_hash: "0xabc", timestamp: "2026-01-01T00:00:00Z", asset0_delta: 1, asset1_delta: -100, asset0_price: 100, asset1_price: 1, volume_usd: 100 }],
    [{ symbol: "WETH", exchange: "binance" }, { symbol: "USDC" }],
    { explorerTxUrl: "https://scan.example/tx/{tx_hash}", referencePoints: [] },
  );
  handlers.click({ ctrlKey: true });
  assert.equal(opened, undefined, "nothing is hovered yet");
  handlers.mousemove({ clientX: 500, clientY: 250 });
  handlers.click({ ctrlKey: false });
  assert.equal(opened, undefined);
  handlers.click({ ctrlKey: true });
  assert.deepEqual(opened, ["https://scan.example/tx/0xabc", "_blank", "noopener,noreferrer"]);
  delete globalThis.document;
});


test("price hover reuses one tooltip and tolerates transient unhover events", async () => {
  let moveHandler;
  let leaveHandler;
  const tooltip = {
    hidden: false, style: {}, textContent: "",
    getBoundingClientRect: () => ({ width: 200, height: 120 }),
  };
  const selectedClasses = new Set();
  const buyMarker = { classList: { add: (value) => selectedClasses.add(value), remove: (value) => selectedClasses.delete(value) } };
  const appended = [];
  const hoverTarget = {
    getBoundingClientRect: () => ({ left: 0, right: 1000, top: 0, bottom: 500, width: 1000, height: 500 }),
  };
  const chart = {
    _fullLayout: { xaxis: { range: ["2025-12-31T23:59:00Z", "2026-01-01T00:01:00Z"] } },
    _fullData: [{}, {}, { uid: "buy" }, { uid: "sell" }],
    on() {},
    removeListener() {},
    addEventListener(event, handler) {
      if (event === "mousemove") moveHandler = handler;
      if (event === "mouseleave") leaveHandler = handler;
    },
    removeEventListener() {},
    appendChild(child) { appended.push(child); },
    querySelector: (selector) => selector === ".nsewdrag" ? hoverTarget : null,
    querySelectorAll: (selector) => selector === ".tracebuy .point" ? [buyMarker] : [],
  };
  globalThis.document = {
    createElement: () => tooltip,
    getElementById: () => chart,
    querySelector: () => chart,
  };
  globalThis.window = { Plotly: { react() {} }, innerWidth: 1200, innerHeight: 800 };
  const timestamp = "2026-01-01T00:00:00Z";
  renderPriceChart([
    { tx_hash: "0x1", timestamp, asset0_delta: 1, asset1_delta: -100, asset0_price: 100, asset1_price: 1, volume_usd: 100 },
    { tx_hash: "0x2", timestamp, asset0_delta: -1, asset1_delta: 101, asset0_price: 100, asset1_price: 1, volume_usd: 101 },
  ], [{ symbol: "WETH", exchange: "binance" }, { symbol: "USDC" }], { referencePoints: [] });

  assert.equal(appended.length, 1);
  assert.equal(tooltip.hidden, true);
  chart._fullLayout.yaxis = { range: [90, 110] };
  moveHandler({ clientX: 500, clientY: 250 });
  assert.match(tooltip.textContent, /2 SWAPS/);
  assert.equal(tooltip.hidden, false);
  assert.equal(tooltip.style.left, "512px");
  assert.equal(selectedClasses.has("price-selected-swap"), true);
  leaveHandler();
  assert.equal(tooltip.hidden, false);
  moveHandler({ clientX: 500, clientY: 250 });
  await new Promise((resolve) => setTimeout(resolve, 130));
  assert.equal(tooltip.hidden, false);
  leaveHandler();
  await new Promise((resolve) => setTimeout(resolve, 130));
  assert.equal(tooltip.hidden, true);
  assert.equal(selectedClasses.has("price-selected-swap"), false);
  delete globalThis.document;
});


test("cumulative volume renders server-calculated observations without resumming", () => {
  const points = [{ timestamp: "2026-01-01T00:01:00Z", volume: 10 }, { timestamp: "2026-01-01T00:02:00Z", volume: 35 }];
  const calls = [];
  globalThis.window = { Plotly: { react: (...args) => calls.push(args) } };
  renderCumulativeVolumeChart(points);
  assert.equal(calls[0][0], "volumeChart");
  assert.deepEqual(calls[0][1][0].y, [10, 35]);
});


test("PnL and balance charts send the expected series to Plotly", () => {
  const calls = [];
  globalThis.window = { Plotly: { react: (...args) => calls.push(args) } };
  const points = [{ timestamp: "2026-01-01T00:00:00Z", flow: 10, mtm: 12 }];
  renderPnlChart(points, { revision: "pool-a:24" });
  renderBalanceChart(
    [{ timestamp: points[0].timestamp, asset0: 2, asset1: 3 }],
    [{ symbol: "WETH" }, { symbol: "USDC" }],
  );

  assert.equal(calls[0][0], "pnlChart");
  assert.deepEqual(calls[0][1].map((trace) => [trace.name, trace.y]), [["FLOW", [10]], ["MTM AS IS", [12]]]);
  assert.equal(calls[0][2].uirevision, "pool-a:24");
  assert.equal(calls[1][0], "balanceChart");
  assert.deepEqual(calls[1][1].map((trace) => trace.name), ["WETH", "USDC"]);
  assert.equal(calls[1][1][1].yaxis, "y2");

  renderPnlChart([{ ...points[0], mtm_mark: "pool_implied" }]);
  assert.equal(calls[2][1][1].name, "MTM AS IS (POOL MARK)");
  renderPnlChart(points, { mtmMode: "balanced" });
  assert.equal(calls[3][1][1].name, "MTM 50/50");
  assert.deepEqual(calls[3][1][1].y, [12]);
});


test("chart relayout ranges are normalized and can be applied to another chart", () => {
  const calls = [];
  globalThis.window = { Plotly: { relayout: (...args) => calls.push(args) } };
  const range = ["2026-01-01T00:00:00.000Z", "2026-01-02T00:00:00.000Z"];

  assert.deepEqual(timeRangeFromRelayout({ "xaxis.range[0]": range[0], "xaxis.range[1]": range[1] }), {
    changed: true, range,
  });
  assert.deepEqual(timeRangeFromRelayout({ "yaxis.range[0]": 1 }), { changed: false, range: null });
  assert.deepEqual(timeRangeFromRelayout({ "xaxis.autorange": true }), { changed: true, range: null });

  setChartTimeRange("balanceChart", range);
  setChartTimeRange("balanceChart", null);
  assert.deepEqual(calls, [
    ["balanceChart", { "xaxis.range": range, "xaxis.autorange": false }],
    ["balanceChart", { "xaxis.autorange": true }],
  ]);
});

test("Plotly Y-axis mutations cannot leak between charts or renders", () => {
  const layouts = [];
  globalThis.window = { Plotly: { react: (_id, _data, layout) => { layouts.push(layout); } } };
  renderPnlChart([]);
  layouts[0].yaxis.range = [102, 108];
  layouts[0].yaxis.autorange = false;
  renderCumulativeVolumeChart([]);
  renderBalanceChart([], []);
  renderPriceChart([], [], { referencePoints: [] });
  renderPnlChart([]);
  for (const layout of layouts.slice(1)) {
    assert.notEqual(layout.yaxis, layouts[0].yaxis);
    assert.equal(layout.yaxis.range, undefined);
    assert.equal(layout.yaxis.autorange, undefined);
  }
});
