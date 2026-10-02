from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .chains import CHAINS
from .database import Database

MAX_CHART_POINTS = 2000
PAGE_SIZE = 100

# ==== One valuation rule for metrics, chart observations, and swap pages ====

VALUED_SQL = """
WITH priced AS (
    SELECT s.*, CASE WHEN a0.price_kind = 'fixed' OR a0.quote_verified THEN r0.price END AS asset0_price,
           CASE WHEN a1.price_kind = 'fixed' OR a1.quote_verified THEN r1.price END AS asset1_price,
           greatest(CASE WHEN a0.price_kind = 'fixed' OR a0.quote_verified THEN r0.lag_ms END,
                    CASE WHEN a1.price_kind = 'fixed' OR a1.quote_verified THEN r1.lag_ms END) AS price_lag_ms,
           ((a0.price_kind != 'none')::INTEGER + (a1.price_kind != 'none')::INTEGER = 1) AS implied,
           -- No CEX trade close enough before the swap: excluded from flow PnL instead of pending forever.
           coalesce(isinf(e0.retry_after) OR isinf(e1.retry_after), FALSE) AS thin_market
    FROM swaps s JOIN pools p ON p.id = s.pool_id
    JOIN pool_assets a0 ON a0.pool_id = s.pool_id AND a0.position = 0
    JOIN pool_assets a1 ON a1.pool_id = s.pool_id AND a1.position = 1
    LEFT JOIN reference_prices r0 ON r0.pool_id = s.pool_id AND r0.tx_hash = s.tx_hash
      AND r0.asset_position = 0 AND r0.config_revision = p.config_revision
      AND r0.target_timestamp BETWEEN ? AND ? AND isfinite(r0.price) AND r0.price > 0
    LEFT JOIN reference_prices r1 ON r1.pool_id = s.pool_id AND r1.tx_hash = s.tx_hash
      AND r1.asset_position = 1 AND r1.config_revision = p.config_revision
      AND r1.target_timestamp BETWEEN ? AND ? AND isfinite(r1.price) AND r1.price > 0
    LEFT JOIN price_enrichment_attempts e0 ON e0.pool_id = s.pool_id AND e0.tx_hash = s.tx_hash
      AND e0.asset_position = 0 AND e0.config_revision = p.config_revision
    LEFT JOIN price_enrichment_attempts e1 ON e1.pool_id = s.pool_id AND e1.tx_hash = s.tx_hash
      AND e1.asset_position = 1 AND e1.config_revision = p.config_revision
    WHERE {where}
), marked AS (
    SELECT *, CASE WHEN tx_index IS NOT NULL THEN coalesce(asset0_price,
        CASE WHEN implied THEN asset1_price * abs(asset1_delta / nullif(asset0_delta, 0)) END) END AS price0,
        CASE WHEN tx_index IS NOT NULL THEN coalesce(asset1_price,
        CASE WHEN implied THEN asset0_price * abs(asset0_delta / nullif(asset1_delta, 0)) END) END AS price1,
        struct_pack(block := block_number, tx := tx_index) AS ordering
    FROM priced
), valued AS (
    SELECT *, CASE WHEN price0 IS NOT NULL AND price1 IS NOT NULL THEN
        CASE WHEN asset0_price IS NOT NULL AND asset1_price IS NOT NULL THEN 'external' ELSE 'pool_implied' END END AS mtm_mark,
        -- MTM values inventory at the latest mark, so thin-market swaps still move inventory.
        tx_index IS NOT NULL AND (price0 IS NOT NULL AND price1 IS NOT NULL OR thin_market) AS in_inventory,
        CASE WHEN tx_index IS NOT NULL THEN asset0_delta * asset0_price + asset1_delta * asset1_price END AS flow_pnl,
        CASE WHEN price0 IS NOT NULL AND price1 IS NOT NULL THEN
            greatest(abs(asset0_delta * price0), abs(asset1_delta * price1))
            WHEN thin_market AND tx_index IS NOT NULL THEN coalesce(abs(asset0_delta * asset0_price), abs(asset1_delta * asset1_price)) END AS volume_usd,
        CASE WHEN tx_index IS NOT NULL THEN
            (CASE WHEN asset0_delta < 0 THEN 1 ELSE -1 END)
            * (abs(asset1_delta / nullif(asset0_delta, 0)) / (asset0_price / asset1_price) - 1) * 10000 END AS edge_bps
    FROM marked
)
"""

METRICS_SQL = """
SELECT pool_id, count(*) AS swap_count, count(*) FILTER (WHERE tx_index IS NULL) AS ordering_pending_count,
       count(flow_pnl) AS priced_swap_count, count(mtm_mark) AS mtm_priced_swap_count,
       count(*) FILTER (WHERE thin_market AND tx_index IS NOT NULL) AS thin_market_count, count(volume_usd) AS volume_swap_count,
       sum(volume_usd) AS volume_usd, sum(flow_pnl) AS flow_pnl_usd,
       sum(asset0_delta) FILTER (WHERE in_inventory) * arg_max(price0, ordering) FILTER (WHERE mtm_mark IS NOT NULL)
       + sum(asset1_delta) FILTER (WHERE in_inventory) * arg_max(price1, ordering) FILTER (WHERE mtm_mark IS NOT NULL) AS mtm_pnl_usd,
       arg_max(timestamp, ordering) FILTER (WHERE mtm_mark IS NOT NULL) AS mtm_as_of,
       arg_max(mtm_mark, ordering) AS mtm_mark,
       arg_max(price0, ordering) FILTER (WHERE mtm_mark IS NOT NULL) AS mtm_price0,
       arg_max(price1, ordering) FILTER (WHERE mtm_mark IS NOT NULL) AS mtm_price1
FROM valued GROUP BY pool_id
"""

SWAP_COLUMNS = "tx_hash, counterparty, timestamp, block_number, tx_index, asset0_delta, asset1_delta, asset0_price, asset1_price, flow_pnl, edge_bps, volume_usd, price_lag_ms, thin_market"


def _metrics(row: dict, baseline=None) -> dict:
    result = dict(row)
    prices = [result.pop('mtm_price0', None), result.pop('mtm_price1', None)]
    result.pop('pool_id', None)
    for name in ('swap_count', 'ordering_pending_count', 'priced_swap_count', 'mtm_priced_swap_count', 'thin_market_count', 'volume_swap_count'):
        result.setdefault(name, 0)
    n, priced, marked, thin = result['swap_count'], result['priced_swap_count'], result['mtm_priced_swap_count'], result['thin_market_count']
    # Thin-market swaps are settled: flow PnL excludes them, MTM keeps their inventory.
    result.update(flow_complete=bool(n) and priced + thin == n, mtm_complete=bool(n) and marked + thin == n,
                  volume_complete=bool(n) and result.pop('volume_swap_count') == n,
                  price_coverage_pct=round(100 * priced / n, 2) if n else None,
                  mtm_coverage_pct=round(100 * marked / n, 2) if n else None)
    if baseline is not None:
        available = baseline['offset0'] is not None
        result.update(mtm_baseline_available=available, mtm_baseline_as_of=_iso(baseline.get('as_of')),
                      mtm_baseline_mark=baseline.get('mark'))
        result['mtm_complete'] &= available
        result['mtm_pnl_usd'] = (result['mtm_pnl_usd'] + baseline['offset0'] * prices[0]
                                 + baseline['offset1'] * prices[1]) if available and result.get('mtm_pnl_usd') is not None else None
    for name in ('volume_usd', 'flow_pnl_usd', 'mtm_pnl_usd'):
        result[name] = round(result[name], 2) if result.get(name) is not None else None
    result['mtm_as_of'] = _iso(result.get('mtm_as_of'))
    result.setdefault('mtm_mark', None)
    return result


def _mtm_baselines(db, pools, start, end):
    """Opening inventory minus equal-value 50/50 HODL quantities, once per window.

    Prefer observations before the window. At the beginning of indexed history,
    price at its first swap and undo transfers in the first recorded balance block.
    An empty opening (the pool was created in the window) has an empty 50/50 hold,
    so only swaps count. Never substitute an as-is baseline when opening data is unavailable.
    """
    ids = [p['id'] for p in pools if p['mtm_mode'] == 'balanced']
    baselines = {p: dict(offset0=None, offset1=None) for p in ids}
    if not ids:
        return baselines
    marks = db.rows(VALUED_SQL.format(where="""(s.pool_id, s.tx_hash) IN (
        SELECT pool_id, coalesce(
            arg_max(tx_hash, struct_pack(block := block_number, tx := tx_index)) FILTER (WHERE timestamp < ?),
            arg_min(tx_hash, struct_pack(block := block_number, tx := tx_index)) FILTER (WHERE timestamp >= ?))
        FROM swaps WHERE pool_id IN (SELECT unnest(?)) AND timestamp <= ? AND tx_index IS NOT NULL GROUP BY pool_id
    )""") + """
        SELECT pool_id, timestamp,
            coalesce(asset0_price, asset1_price * abs(asset1_delta / nullif(asset0_delta, 0))) AS opening0,
            coalesce(asset1_price, asset0_price * abs(asset0_delta / nullif(asset1_delta, 0))) AS opening1,
            CASE WHEN asset0_price IS NOT NULL AND asset1_price IS NOT NULL THEN 'external' ELSE 'pool_implied' END AS mark
        FROM priced
    """, [datetime.min.replace(tzinfo=timezone.utc), end] * 2 + [start, start, ids, end])
    balances = {r['pool_id']: r for r in db.rows("""
        WITH points AS (
            SELECT pool_id, coalesce(
                arg_max(struct_pack(block := block_number, observed_at := timestamp, q0 := asset0_balance, q1 := asset1_balance), block_number) FILTER (WHERE timestamp < ?),
                arg_min(struct_pack(block := block_number, observed_at := timestamp, q0 := asset0_balance, q1 := asset1_balance), block_number) FILTER (WHERE timestamp >= ?)) AS point
            FROM balance_points WHERE pool_id IN (SELECT unnest(?)) AND timestamp <= ? GROUP BY pool_id
        )
        SELECT p.pool_id, p.point,
            coalesce(sum(l.signed_amount) FILTER (WHERE l.asset_position = 0 AND b.timestamp >= ?), 0) AS delta0,
            coalesce(sum(l.signed_amount) FILTER (WHERE l.asset_position = 1 AND b.timestamp >= ?), 0) AS delta1,
            count(l.log_index) AS logs, count(b.block_number) AS timed_logs
        FROM points p LEFT JOIN pool_logs l ON l.pool_id = p.pool_id
            AND p.point.observed_at >= ? AND l.block_number <= p.point.block
        LEFT JOIN blocks b ON b.chain_id = l.chain_id AND b.block_number = l.block_number
        GROUP BY p.pool_id, p.point
    """, [start, start, ids, end, start, start, start])}
    for mark in marks:
        balance = balances.get(mark['pool_id'])
        prices = [mark['opening0'], mark['opening1']]
        if not balance or any(p is None or not math.isfinite(p) or p <= 0 for p in prices):
            continue
        point = balance['point']
        if point['observed_at'] >= start and (not balance['logs'] or balance['logs'] != balance['timed_logs']):
            continue
        # Undoing the creation deposit leaves float dust around zero, not an exact 0.
        quantities = [0.0 if abs(held - delta) <= 1e-9 * abs(held) else held - delta
                      for held, delta in [(point['q0'], balance['delta0']), (point['q1'], balance['delta1'])]]
        if any(not math.isfinite(q) or q < 0 for q in quantities):
            continue
        value = sum(q * p for q, p in zip(quantities, prices))
        if not math.isfinite(value):
            continue
        baselines[mark['pool_id']] = dict(offset0=quantities[0] - value / (2 * prices[0]),
                                          offset1=quantities[1] - value / (2 * prices[1]),
                                          as_of=mark['timestamp'], mark=mark['mark'] if value else 'empty')
    return baselines


def _window(hours, window_end=None, range_start=None, range_end=None):
    hours = min(720.0, max(1.0, float(hours)))
    now = datetime.now(timezone.utc)
    end = window_end or now
    if end.tzinfo is None or end > now:
        raise ValueError('window_end must be an aware timestamp no later than now')
    start = end - timedelta(hours=hours)
    if (range_start is None) != (range_end is None):
        raise ValueError('range_start and range_end must be supplied together')
    if range_start is not None and (range_start.tzinfo is None or range_end.tzinfo is None or range_start >= range_end):
        raise ValueError('Chart range must contain increasing, timezone-aware timestamps')
    return hours, start, end, max(start, range_start or start), min(end, range_end or end)


def _expiry(db, pool_ids, start, end):
    # A future observation can enter the window; an included one can leave it.
    row = db.row("""
        SELECT min(CASE WHEN timestamp > ? THEN timestamp ELSE timestamp + (? - ?) + INTERVAL 1 MICROSECOND END) AS expiry
        FROM (SELECT timestamp FROM swaps WHERE pool_id IN (SELECT unnest(?))
              UNION ALL SELECT timestamp FROM balance_points WHERE pool_id IN (SELECT unnest(?))) t
        WHERE timestamp >= ?
    """, [end, end, start, pool_ids, pool_ids, start])
    return row['expiry'] or datetime.max.replace(tzinfo=timezone.utc)


# ==== Aggregate navigation summaries ====

def pool_summaries(db: Database, pools: list[dict], hours: float = 24.0) -> dict:
    hours, start, end, _, _ = _window(hours)
    ids = tuple(p['id'] for p in pools)
    with db._lock:
        key = ('summaries', ids, tuple(db.data_revision(p) for p in ids), hours)
        def build():
            baselines = _mtm_baselines(db, pools, start, end)
            metrics = {r['pool_id']: _metrics(r, baselines.get(r['pool_id'])) for r in db.rows(
                VALUED_SQL.format(where='s.timestamp BETWEEN ? AND ?') + METRICS_SQL, [start, end] * 3)}
            marks = {r['pool_id']: r for r in db.rows(VALUED_SQL.format(where='s.tx_index IS NOT NULL') + """
                SELECT pool_id, arg_max(timestamp, ordering) AS last_swap_at,
                    arg_max(CASE WHEN implied THEN CASE WHEN mtm_mark IS NOT NULL THEN price0 END ELSE asset0_price END, ordering) AS price0,
                    arg_max(CASE WHEN implied THEN CASE WHEN mtm_mark IS NOT NULL THEN price1 END ELSE asset1_price END, ordering) AS price1
                FROM valued GROUP BY pool_id
            """, [datetime.min.replace(tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc)] * 2)}
            snapshots = {}
            for row in db.rows('SELECT * FROM balance_snapshots'):
                snapshots.setdefault(row['pool_id'], []).append(row)
            summaries, marked = {}, {}
            for pool in pools:
                metric = metrics.get(pool['id'], _metrics({}))
                summary = {k: metric[k] for k in ('volume_usd', 'flow_pnl_usd', 'mtm_pnl_usd', 'flow_complete', 'mtm_complete', 'volume_complete')}
                summary.update(swap_count=metric['swap_count'], mtm_baseline_available=metric.get('mtm_baseline_available'))
                mark = marks.get(pool['id'], {})
                prices = [mark.get('price0'), mark.get('price1')]
                for asset in pool['assets']:
                    if asset['price_kind'] == 'fixed':
                        prices[int(asset['position'])] = asset['fixed_price']
                balances = snapshots.get(pool['id'], [])
                priced = all(p is not None and math.isfinite(p) and p > 0 for p in prices)
                if priced:
                    marked[pool['id']] = prices
                tvl = round(sum(abs(b['balance']) * prices[int(b['asset_position'])] for b in balances), 2) if len(balances) == 2 and priced else None
                summary.update(last_swap_at=_iso(mark.get('last_swap_at')), tvl_usd=tvl)
                summaries[pool['id']] = summary
            # APR divides by the window's peak TVL so a withdrawal doesn't inflate it: every balance
            # in effect during the window (including the one carried in at its start) at current marks.
            peaks = {r['pool_id']: r['tvl'] for r in db.rows("""
                WITH marks AS (SELECT unnest(?) AS pool_id, unnest(?) AS p0, unnest(?) AS p1)
                SELECT b.pool_id, max(abs(b.asset0_balance) * m.p0 + abs(b.asset1_balance) * m.p1) AS tvl
                FROM balance_points b JOIN marks m USING (pool_id)
                WHERE b.timestamp >= ? OR b.block_number = (
                    SELECT max(block_number) FROM balance_points x WHERE x.pool_id = b.pool_id AND x.timestamp < ?)
                GROUP BY b.pool_id
            """, [list(marked), [p[0] for p in marked.values()], [p[1] for p in marked.values()], start, start])} if marked else {}
            for pool_id, summary in summaries.items():
                basis = max(filter(None, (summary['tvl_usd'], peaks.get(pool_id))), default=None)
                summary['apr_tvl_usd'] = round(basis, 2) if basis else None
                for measure in ('flow', 'mtm'):
                    summary[f'{measure}_apr_pct'] = round(summary[f'{measure}_pnl_usd'] / basis * (24 * 365 / hours) * 100, 2) if summary[f'{measure}_complete'] and basis else None
            chains = {}
            groups = {key: [summaries[p['id']] for p in pools if p['chain_key'] == key] for key in {p['chain_key'] for p in pools}}
            groups[None] = list(summaries.values())
            for chain, included in groups.items():
                total = {}
                for metric, complete in (('volume_usd', 'volume_complete'), ('flow_pnl_usd', 'flow_complete'), ('mtm_pnl_usd', 'mtm_complete'), ('tvl_usd', 'tvl_complete'), ('apr_tvl_usd', 'apr_tvl_complete')):
                    known = [s[metric] for s in included if s[metric] is not None]
                    total[metric] = round(sum(known), 2) if known else None
                    total[complete] = bool(included) and all(s.get(complete, s[metric] is not None) for s in included)
                for measure in ('flow', 'mtm'):
                    total[f'{measure}_apr_pct'] = round(total[f'{measure}_pnl_usd'] / total['apr_tvl_usd'] * (24 * 365 / hours) * 100, 2) if total[f'{measure}_complete'] and total['apr_tvl_complete'] and total['apr_tvl_usd'] else None
                chains[chain] = total
            return {'pools': summaries, 'chains': {k: v for k, v in chains.items() if k is not None}, 'total': chains[None]}, _expiry(db, list(ids), start, end)
        return db.cached_analytics(key, build)


# ==== Bounded chart observations and exact visible-range details ====

def _sample(db, source, columns, extrema, *, side=False):
    """Select actual observations, preserving temporal endpoints and extrema in SQL."""
    count = db.row(f'SELECT count(*) AS n FROM ({source})')['n']
    if count <= MAX_CHART_POINTS:
        return db.rows(f'SELECT {columns} FROM ({source}) ORDER BY timestamp, ordering'), count
    selectors = ['min(point_id)', 'max(point_id)']
    for expression, direction in extrema:
        selectors.append(f'arg_{direction}(point_id, {expression})')
    buckets = MAX_CHART_POINTS // (len(selectors) * (2 if side else 1))
    rows = db.rows(f"""
        WITH observations AS (
            SELECT *, row_number() OVER (ORDER BY timestamp, ordering) AS point_id FROM ({source})
        ), bucketed AS (
            SELECT *, least({buckets - 1}, floor((epoch(timestamp) - min(epoch(timestamp)) OVER ())
                / greatest(max(epoch(timestamp)) OVER () - min(epoch(timestamp)) OVER (), 0.000001) * {buckets})) AS bucket
            FROM observations
        ), selected AS (
            SELECT unnest([{', '.join(selectors)}]) AS chosen FROM bucketed
            GROUP BY bucket {', asset0_delta > 0' if side else ''}
        ) SELECT {columns} FROM bucketed WHERE point_id IN (SELECT chosen FROM selected)
          ORDER BY timestamp, ordering
    """)
    return rows, count


def _swap_page(db, source, page):
    total = db.row(f'SELECT count(*) AS n FROM ({source})')['n']
    page = min(max(0, page), max(0, (total - 1) // PAGE_SIZE))
    # counterparty_rank: the counterparty's place by volume among all counterparties in the page's window,
    # ordered like the counterparties table.
    items = db.rows(f'''
        WITH source AS ({source}), ranks AS (
            SELECT nullif(lower(trim(counterparty)), '') AS address, row_number() OVER (
                ORDER BY coalesce(sum(volume_usd), 0) DESC, count(*) DESC, coalesce(nullif(lower(trim(counterparty)), ''), '')
            ) AS counterparty_rank
            FROM source GROUP BY 1
        )
        SELECT {SWAP_COLUMNS}, ranks.counterparty_rank FROM source
        LEFT JOIN ranks ON ranks.address = nullif(lower(trim(source.counterparty)), '')
        ORDER BY ordering DESC LIMIT {PAGE_SIZE} OFFSET ?
    ''', [page * PAGE_SIZE])
    for row in items:
        row['timestamp'] = _iso(row['timestamp'])
        for name in ('flow_pnl', 'edge_bps', 'volume_usd'):
            if row[name] is not None:
                row[name] = round(row[name], 6)
    return dict(items=items, total=total, page=page, page_size=PAGE_SIZE)


def dashboard_payload(db: Database, pool_id: str, hours: float = 24.0, *, window_end=None,
                      range_start=None, range_end=None, plots=('pnl', 'price'), minimum_swap_usd=0, swap_page_index=0,
                      counterparty_side='recipient') -> dict:
    hours, start, end, visible_start, visible_end = _window(hours, window_end, range_start, range_end)
    plots = tuple(sorted(set(plots)))
    if counterparty_side not in {'recipient', 'sender'}:
        raise ValueError('counterparty_side must be recipient or sender')
    if not set(plots) <= {'pnl', 'price', 'balance', 'volume'} or not math.isfinite(minimum_swap_usd) or minimum_swap_usd < 0:
        raise ValueError('Invalid plots or minimum swap size')
    with db._lock:
        pool = db.get_pool(pool_id)
        if not pool:
            raise KeyError(pool_id)
        revision = db.data_revision(pool_id) + ':' + pool['incarnation']
        key = ('dashboard', (pool_id,), revision, hours, _iso(window_end), _iso(range_start), _iso(range_end), plots, minimum_swap_usd, swap_page_index, counterparty_side)
        def build():
            baseline = _mtm_baselines(db, [pool], start, end).get(pool_id)
            # Native temporary tables keep large intermediate histories out of Python.
            db.connection.execute('CREATE OR REPLACE TEMP TABLE dashboard_valued AS ' + VALUED_SQL.format(
                where='s.pool_id = ? AND s.timestamp BETWEEN ? AND ?') + 'SELECT * FROM valued', [start, end, start, end, pool_id, start, end])
            try:
                metrics = _metrics(db.row(METRICS_SQL.replace('FROM valued', 'FROM dashboard_valued')) or {}, baseline)
                db.connection.execute("""CREATE OR REPLACE TEMP TABLE dashboard_visible AS
                    SELECT * FROM dashboard_valued WHERE tx_index IS NOT NULL AND timestamp BETWEEN ? AND ?
                """, [visible_start, visible_end])
                visible = 'SELECT * FROM dashboard_visible'
                page = _swap_page(db, visible, swap_page_index)
                # counterparty_side is validated above, so it is safe to use as a column name.
                counterparties = db.rows(f"""
                    WITH groups AS (
                        SELECT nullif(lower(trim({counterparty_side})), '') AS address, count(*) AS swap_count,
                            nullif(count(DISTINCT signer), 0) AS unique_signers,
                            sum(volume_usd) AS volume_usd,
                            sum(volume_usd * edge_bps) / nullif(sum(volume_usd) FILTER (WHERE edge_bps IS NOT NULL), 0) AS avg_edge_bps
                        FROM dashboard_visible GROUP BY 1
                    ) SELECT *, 100.0 * swap_count / sum(swap_count) OVER () AS swap_share_pct,
                        CASE WHEN volume_usd IS NOT NULL THEN coalesce(100.0 * volume_usd / nullif(sum(volume_usd) OVER (), 0), 0) END AS volume_share_pct,
                        count(*) OVER () AS total_counterparties
                    FROM groups ORDER BY coalesce(volume_usd, 0) DESC, swap_count DESC, coalesce(address, '') LIMIT 10
                """)
                counterparty_count = counterparties[0]['total_counterparties'] if counterparties else 0
                for row in counterparties:
                    row.pop('total_counterparties')
                chain = CHAINS[pool['chain_key']]
                result = dict(pool={**{k: pool[k] for k in ('id', 'name', 'chain_key', 'address', 'start_block', 'paused', 'mtm_mode')},
                                    'chain_name': chain.name, 'assets': [_public_asset(a) for a in pool['assets']]},
                              window_hours=hours, window_start=_iso(start), window_end=_iso(end), data_revision=revision,
                              chart_range=[_iso(range_start), _iso(range_end)] if range_start is not None else None,
                              metrics=metrics, swap_page=page,
                              counterparties=dict(rows=counterparties, total_swaps=page['total'], total_counterparties=counterparty_count,
                                                  side=counterparty_side),
                              explorer_tx_url=chain.explorer_tx_url, explorer_address_url=chain.explorer_address_url,
                              pnl_series=[], balance_series=[], price_series=[], reference_series=[], volume_series=[], series_counts={})
                result['balances'] = [dict(symbol=pool['assets'][int(r['asset_position'])]['symbol'], balance=r['balance'], block_number=r['block_number'], timestamp=_iso(r['timestamp']))
                                      for r in db.rows('SELECT * FROM balance_snapshots WHERE pool_id = ? ORDER BY asset_position', [pool_id])]
                if {'pnl', 'volume'} & set(plots):
                    db.connection.execute("""CREATE OR REPLACE TEMP TABLE dashboard_cumulative AS
                        SELECT *, sum(flow_pnl) OVER w AS flow,
                            (sum(asset0_delta) FILTER (WHERE in_inventory) OVER w + ?::DOUBLE) * price0
                            + (sum(asset1_delta) FILTER (WHERE in_inventory) OVER w + ?::DOUBLE) * price1 AS mtm,
                            sum(volume_usd) OVER w AS volume
                        FROM dashboard_valued WHERE tx_index IS NOT NULL
                        WINDOW w AS (ORDER BY ordering ROWS UNBOUNDED PRECEDING)
                    """, [baseline['offset0'], baseline['offset1']] if baseline is not None else [0.0, 0.0])
                for plot in plots:
                    if plot == 'pnl':
                        source = "SELECT * FROM dashboard_cumulative WHERE mtm_mark IS NOT NULL"
                        columns = 'timestamp, flow, mtm, price0, price1, mtm_mark'
                        extrema = [('flow', 'min'), ('flow', 'max'), ('mtm', 'min'), ('mtm', 'max')]
                    elif plot == 'volume':
                        source = 'SELECT * FROM dashboard_cumulative WHERE volume_usd IS NOT NULL'
                        columns, extrema = 'timestamp, volume', [('volume', 'min'), ('volume', 'max')]
                    elif plot == 'balance':
                        db.connection.execute("""CREATE OR REPLACE TEMP TABLE dashboard_balances AS
                            SELECT timestamp, asset0_balance AS asset0, asset1_balance AS asset1, block_number AS ordering
                            FROM balance_points WHERE pool_id = ?
                        """, [pool_id])
                        source, columns = 'SELECT * FROM dashboard_balances WHERE TRUE', 'timestamp, asset0, asset1'
                        extrema = [('asset0', 'min'), ('asset0', 'max'), ('asset1', 'min'), ('asset1', 'max')]
                    else:
                        source = f'SELECT * FROM dashboard_visible WHERE (volume_usd IS NULL OR volume_usd >= {float(minimum_swap_usd)})'
                        columns = SWAP_COLUMNS
                        extrema = [('abs(asset1_delta / nullif(asset0_delta, 0))', 'min'), ('abs(asset1_delta / nullif(asset0_delta, 0))', 'max'), ('abs(edge_bps)', 'max'), ('volume_usd', 'max')]
                    # Bounds originate from parsed datetime objects, never raw SQL inputs.
                    source += f" AND timestamp BETWEEN TIMESTAMPTZ '{visible_start.isoformat()}' AND TIMESTAMPTZ '{visible_end.isoformat()}'"
                    rows, count = _sample(db, source, columns, extrema, side=plot == 'price')
                    for row in rows:
                        row['timestamp'] = _iso(row['timestamp'])
                    result[f'{plot}_series'] = rows
                    result['series_counts'][plot] = dict(source=count, returned=len(rows))
                    if plot == 'price':
                        rows, count = _sample(db, visible, 'timestamp, asset0_price / asset1_price AS reference',
                                              [('asset0_price / asset1_price', 'min'), ('asset0_price / asset1_price', 'max')])
                        for row in rows:
                            row['timestamp'] = _iso(row['timestamp'])
                        result['reference_series'] = rows
                        result['series_counts']['reference'] = dict(source=count, returned=len(rows))
                return result, _expiry(db, [pool_id], start, end) if window_end is None else datetime.max.replace(tzinfo=timezone.utc)
            finally:
                for name in ('valued', 'visible', 'cumulative', 'balances'):
                    db.connection.execute(f'DROP TABLE IF EXISTS dashboard_{name}')
        return db.cached_analytics(key, build)


def swap_page(db: Database, pool_id: str, hours: float, *, window_end, revision, page=0, range_start=None, range_end=None):
    _, _, _, start, end = _window(hours, window_end, range_start, range_end)
    with db._lock:
        pool = db.get_pool(pool_id)
        if not pool:
            raise KeyError(pool_id)
        if revision != db.data_revision(pool_id) + ':' + pool['incarnation']:
            raise ValueError('Dashboard data changed; refresh before paging')
        db.connection.execute('CREATE OR REPLACE TEMP TABLE dashboard_page AS ' + VALUED_SQL.format(
            where='s.pool_id = ? AND s.timestamp BETWEEN ? AND ? AND s.tx_index IS NOT NULL') + 'SELECT * FROM valued', [start, end, start, end, pool_id, start, end])
        try:
            return _swap_page(db, 'SELECT * FROM dashboard_page', page)
        finally:
            db.connection.execute('DROP TABLE dashboard_page')


def _public_asset(asset: dict) -> dict:
    result = {key: asset.get(key) for key in ('position', 'symbol', 'address', 'decimals', 'price_kind', 'exchange', 'market', 'market_type', 'fixed_price', 'quote_asset', 'quote_verified', 'quote_error')}
    if result['fixed_price'] is not None and not math.isfinite(result['fixed_price']):
        result['fixed_price'] = None
    return result


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if isinstance(value, datetime) else str(value)
