from __future__ import annotations

import logging
import time
from dataclasses import replace
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .chains import CHAINS
from .database import Database, upsert_rows
from .envio import EnvioClient
from .prices import suggest_binance_market
from .rpc import RpcClient, RpcError, connect_rpc, normalize_address, sanitize_error, redact_rpc_url


logger = logging.getLogger(__name__)

# Soft turn limits: always finish the current Envio page before handing off.
MAX_NEW_SWAPS_PER_TURN = 10_000
MAX_PAGES_PER_TURN = 10
MAX_INGESTION_SECONDS_PER_TURN = 30


# ==== Swap reconstruction ====

INSERT_RECONSTRUCTED_SWAPS_SQL = """
    WITH transfer_rows AS (
        SELECT l.*, b.timestamp, r.tx_index, r.tx_from,
               CASE WHEN l.signed_amount < 0 THEN l.recipient ELSE l.sender END AS counterparty
        FROM pool_logs l
        JOIN blocks b ON b.chain_id = l.chain_id AND b.block_number = l.block_number
        JOIN raw_logs r ON r.chain_id = l.chain_id AND r.tx_hash = l.tx_hash AND r.log_index = l.log_index
        WHERE l.pool_id = ? AND l.block_number BETWEEN ? AND ?
    ), deltas AS (
        SELECT t.pool_id, t.tx_hash, min(block_number) AS block_number,
               min(timestamp) AS timestamp, min(tx_index) AS tx_index, any_value(tx_from) AS signer,
               any_value(n.asset0_delta) AS asset0_delta, any_value(n.asset1_delta) AS asset1_delta
        FROM transfer_rows t JOIN exact_nets n ON n.tx_hash = t.tx_hash
        GROUP BY t.pool_id, t.tx_hash
    ), candidates AS (
        SELECT pool_id, tx_hash, counterparty, count(*) AS candidate_count,
               count(*) FILTER (WHERE signed_amount < 0) AS outgoing_count
        FROM transfer_rows
        WHERE signed_amount != 0
        GROUP BY pool_id, tx_hash, counterparty
    ), ranked AS (
        SELECT *, row_number() OVER (
            PARTITION BY pool_id, tx_hash
            ORDER BY candidate_count DESC, outgoing_count DESC, counterparty
        ) AS candidate_rank
        FROM candidates
    ), peers AS (
        -- The most frequent address on each side: who sent tokens to the pool, and who received them.
        SELECT pool_id, tx_hash, signed_amount > 0 AS incoming,
               CASE WHEN signed_amount > 0 THEN sender ELSE recipient END AS peer, count(*) AS n
        FROM transfer_rows WHERE signed_amount != 0 GROUP BY ALL
    ), sides AS (
        SELECT pool_id, tx_hash,
               first(peer ORDER BY n DESC, peer) FILTER (WHERE incoming) AS sender,
               first(peer ORDER BY n DESC, peer) FILTER (WHERE NOT incoming) AS recipient
        FROM peers GROUP BY pool_id, tx_hash
    )
    INSERT INTO swaps (
        pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta, counterparty, tx_index,
        sender, recipient, signer
    )
    SELECT derived.pool_id, derived.tx_hash, block_number, timestamp,
           asset0_delta, asset1_delta, counterparty, tx_index, sides.sender, sides.recipient, signer
    FROM deltas derived
    LEFT JOIN ranked
      ON ranked.pool_id = derived.pool_id AND ranked.tx_hash = derived.tx_hash
     AND ranked.candidate_rank = 1
    LEFT JOIN sides ON sides.pool_id = derived.pool_id AND sides.tx_hash = derived.tx_hash
    WHERE asset0_delta != 0 AND asset1_delta != 0
      AND ((asset0_delta > 0) != (asset1_delta > 0))
"""


def insert_reconstructed_swaps(connection, pool_id: str, from_block: int, to_block: int) -> None:
    # ERC-20 amounts are 256-bit, beyond DuckDB integers, and float sums leave dust or erase
    # small nets. Python ints net each leg exactly, and int/int division rounds once, so a
    # nonzero net never becomes 0.0 and the swap filter sees the exact signs.
    legs = connection.execute("""
        SELECT l.tx_hash, l.asset_position, a.decimals, list(l.signed_raw)
        FROM pool_logs l JOIN pool_assets a ON a.pool_id = l.pool_id AND a.position = l.asset_position
        WHERE l.pool_id = ? AND l.block_number BETWEEN ? AND ?
        GROUP BY ALL
    """, [pool_id, from_block, to_block]).fetchall()
    nets = {}
    for tx_hash, position, decimals, raws in legs:
        nets.setdefault(tx_hash, [None, None])[position] = sum(map(int, raws)) / 10 ** decimals
    # One delimited string binds far faster than large list parameters; hex hashes contain
    # no delimiters and float repr round-trips exactly.
    connection.execute("""
        CREATE OR REPLACE TEMP TABLE exact_nets AS SELECT split_part(row, ',', 1) AS tx_hash,
            nullif(split_part(row, ',', 2), '')::DOUBLE AS asset0_delta,
            nullif(split_part(row, ',', 3), '')::DOUBLE AS asset1_delta
        FROM unnest(string_split(?, ';')) t(row)
    """, [";".join(f"{tx_hash},{'' if d0 is None else repr(d0)},{'' if d1 is None else repr(d1)}"
                   for tx_hash, (d0, d1) in nets.items())])
    connection.execute(INSERT_RECONSTRUCTED_SWAPS_SQL, [pool_id, from_block, to_block])
    connection.execute("DROP TABLE exact_nets")


# ==== Pool validation and discovery ====

def effective_rpc_urls(db: Database, chain_key: str) -> list[str]:
    chain = CHAINS[chain_key]
    override = db.rpc_overrides().get(chain_key)
    return [override] if override else list(chain.public_rpcs)


def validate_pool_input(
    db: Database,
    *,
    chain_key: str,
    pool_address: str,
    token_addresses: list[str],
    start_block: int | None,
) -> dict:
    if chain_key not in CHAINS:
        raise ValueError("Unsupported chain")
    if len(token_addresses) != 2:
        raise ValueError("Exactly two token addresses are required")
    pool_address = normalize_address(pool_address)
    token_addresses = [normalize_address(value) for value in token_addresses]
    if token_addresses[0] == token_addresses[1]:
        raise ValueError("Token addresses must be different")
    chain = CHAINS[chain_key]
    client, status = connect_rpc(effective_rpc_urls(db, chain_key), chain.chain_id)
    try:
        if client.code(pool_address) in {"0x", "0x0", ""}:
            raise ValueError("Pool address has no contract code")
        assets = []
        for address in token_addresses:
            if client.code(address) in {"0x", "0x0", ""}:
                raise ValueError(f"Token address {address} has no contract code")
            symbol, decimals = client.token_metadata(address)
            assets.append({"address": address, "symbol": symbol, "decimals": decimals})
        finalized = max(0, status.latest_block - chain.confirmations)
        if start_block is None:
            history_blocks = int((3 * 24 * 60 * 60) / chain.seconds_per_block)
            start_block = max(0, finalized - history_blocks)
        if start_block < 0 or start_block > finalized:
            raise ValueError(f"Start block must be between 0 and confirmed block {finalized}")
        return {
            "pool_address": pool_address,
            "assets": assets,
            "start_block": int(start_block),
            "latest_block": status.latest_block,
            "finalized_block": finalized,
            "rpc": status.display_url,
        }
    finally:
        client.close()


def discover_pool_assets(
    db: Database,
    *,
    chain_key: str,
    pool_address: str,
    end_block: int | None = None,
    max_blocks: int | None = None,
    scan_other_chains: bool = True,
) -> dict:
    if chain_key not in CHAINS:
        raise ValueError("Unsupported chain")
    display_address = str(pool_address).strip()
    pool_address = normalize_address(pool_address)
    chain = CHAINS[chain_key]
    client, status = connect_rpc(effective_rpc_urls(db, chain_key), chain.chain_id)
    try:
        if client.code(pool_address) in {"0x", "0x0", ""}:
            found = _chains_with_contract(db, pool_address, exclude=chain_key) if scan_other_chains else []
            if not found:
                raise ValueError(f"Pool address has no contract code on {chain.name} or any other supported chain")
            logger.info("pool discovery found contract on other chain pool=%s selected=%s found=%s",
                        pool_address, chain_key, ",".join(found))
            return discover_pool_assets(db, chain_key=found[0], pool_address=display_address,
                                        max_blocks=max_blocks, scan_other_chains=False)
        finalized = max(0, status.latest_block - chain.confirmations)
        search_end = finalized if end_block is None else int(end_block)
        if search_end < 0 or search_end > finalized:
            raise ValueError(f"Discovery block must be between 0 and confirmed block {finalized}")
        search_limit = max_blocks or int((7 * 24 * 60 * 60) / chain.seconds_per_block)
        remaining = max(1, int(search_limit))
        cursor = search_end
        counts = Counter()
        latest_blocks = {}
        ranked = client.pool_token_addresses(pool_address)
        detected = []
        if ranked:
            logger.info(
                "pool discovery read token addresses chain=%s pool=%s",
                chain.key, pool_address,
            )
            for token_address in ranked:
                try:
                    symbol, decimals = client.token_metadata(token_address)
                except Exception:
                    logger.debug(
                        "pool discovery token metadata failed chain=%s token=%s",
                        chain.key, token_address, exc_info=True,
                    )
                    break
                detected.append({
                    "address": token_address,
                    "symbol": symbol,
                    "decimals": decimals,
                    "transfer_count": None,
                })
            if len(detected) != 2:
                logger.info(
                    "pool discovery falling back to Envio chain=%s pool=%s reason=incomplete_metadata",
                    chain.key, pool_address,
                )
                detected = []
                ranked = None
        if not ranked:
            settings = db.envio_settings()
            envio = EnvioClient(settings["api_key"], settings["rpm_limit"])
            start = max(0, cursor - remaining + 1)
            try:
                for page in envio.transfer_pages(chain.chain_id, pool_address, start, cursor):
                    for log in page["logs"]:
                        try:
                            token_address = normalize_address(log["address"])
                            block_number = int(log["blockNumber"], 16)
                        except (KeyError, TypeError, ValueError):
                            continue
                        counts[token_address] += 1
                        latest_blocks[token_address] = max(latest_blocks.get(token_address, 0), block_number)
            finally:
                envio.close()

            ranked = sorted(
                counts,
                key=lambda address: (-counts[address], -latest_blocks[address], address),
            )
            for token_address in ranked:
                try:
                    symbol, decimals = client.token_metadata(token_address)
                except Exception:
                    continue
                detected.append({
                    "address": token_address,
                    "symbol": symbol,
                    "decimals": decimals,
                    "transfer_count": counts[token_address],
                })
                if len(detected) == 2:
                    break
        if len(detected) != 2:
            raise ValueError(
                "Could not detect two ERC-20 assets from recent pool transfers; "
                "try a custom RPC or a pool with recent activity"
            )

        preferences = db.price_preferences(chain_key, [asset["address"] for asset in detected])
        assets = []
        for asset in detected:
            # With no remembered setting, USD-named tokens (USDC, crvUSD, …) default to a fixed $1,
            # and other tokens to a matching Binance market when one exists.
            preference = preferences.get(asset["address"]) or db.latest_cross_chain_price_preference(
                chain_key, asset["symbol"], asset["decimals"]
            ) or ({"price_kind": "fixed", "fixed_price": 1.0} if "USD" in asset["symbol"].upper()
                  else suggest_binance_market(asset["symbol"]) or {})
            assets.append(
                {
                    **asset,
                    "price_kind": preference.get("price_kind", "none"),
                    "exchange": preference.get("exchange"),
                    "market": preference.get("market"),
                    "market_type": preference.get("market_type") or "spot",
                    "fixed_price": preference.get("fixed_price"),
                }
            )
        return {
            "pool_address": pool_address,
            "generated_name": (
                f"{assets[0]['symbol']}/{assets[1]['symbol']} "
                f"{display_address[:6]}...{display_address[-4:]}"
            ),
            "chain_key": chain_key,
            "assets": assets,
            "latest_block": status.latest_block,
            "finalized_block": finalized,
            "rpc": status.display_url,
        }
    finally:
        client.close()


def _chains_with_contract(db: Database, address: str, *, exclude: str) -> list[str]:
    """Supported chains with code at the address, in chain order; one quick pass per chain."""
    urls = {key: effective_rpc_urls(db, key) for key in CHAINS if key != exclude}

    def has_code(key: str) -> bool:
        try:
            client = RpcClient(urls[key], timeout=10, expected_chain_id=CHAINS[key].chain_id)
        except RpcError:
            return False
        try:
            return client.code(address) not in {"0x", "0x0", ""}
        except (RpcError, ValueError, TypeError):
            return False
        finally:
            client.close()

    with ThreadPoolExecutor(len(urls)) as pool:
        return [key for key, found in zip(urls, pool.map(has_code, urls)) if found]


# ==== Incremental synchronization ====

def sync_pool(
    db: Database,
    pool: dict,
    *,
    report=None, stop=None,
) -> None:
    emit = report or (lambda *_args, **_kwargs: None)
    pool_name = pool.get("name") or pool["id"]
    work_generation = int(pool["work_generation"])
    chain = CHAINS[pool["chain_key"]]
    assets = db.rows("SELECT * FROM pool_assets WHERE pool_id = ? ORDER BY position", [pool["id"]])
    started = time.monotonic()
    client = None
    envio = None
    try:
        emit("waiting", f"SYNCING {pool_name} — WAITING FOR RPC", pool["id"])
        client, rpc_status = connect_rpc(
            effective_rpc_urls(db, chain.key), chain.chain_id, stop=stop,
            report=lambda message: emit("waiting", message, pool["id"]),
        )
        settings = db.envio_settings()
        envio = EnvioClient(settings["api_key"], settings["rpm_limit"])
        envio.stop = stop
        envio.report = lambda message: emit("waiting", message, pool["id"])
        finalized = max(0, rpc_status.latest_block - chain.confirmations)
        rebuild_requested = bool(pool.get("rebuild_requested"))
        if rebuild_requested:
            logger.info("pool sync rebuilding derived data pool_id=%s", pool["id"])

        next_block = int(pool["next_block"])
        # A persisted scan resumes both forward batches and interrupted tail replay.
        scan_next = pool.get("scan_next_block")
        if scan_next is not None and int(scan_next) <= finalized:
            from_block = int(scan_next)
        elif scan_next is None and (pool.get("finalized_block") is None or next_block <= int(pool["finalized_block"])):
            from_block = next_block
        else:
            from_block = max(int(pool["start_block"]), min(next_block, finalized + 1) - chain.overlap_blocks)
        if finalized < from_block:
            emit("syncing", f"SYNCING {pool_name} — CHECKING CONFIRMED HEAD", pool["id"])
            if rebuild_requested and not rebuild_derived(db, pool, assets, client, finalized):
                logger.warning("pool sync discarded stale rebuild result pool_id=%s", pool["id"])
                return
            rpc_status = replace(rpc_status, url=client.url, display_url=redact_rpc_url(client.url))
            if not _mark_caught_up(db, pool["id"], work_generation, rpc_status, finalized, incarnation=pool.get("incarnation")):
                logger.warning("pool sync discarded stale result pool_id=%s", pool["id"])
            else:
                emit("syncing", f"SYNC COMPLETE — {pool_name} IS UP TO DATE", pool["id"])
            return

        progress_denominator = max(1, finalized - int(pool["start_block"]) + 1)
        total_logs = 0
        total_swaps = 0
        new_swaps = 0
        pages = 0
        yielded = False
        to_block = from_block - 1
        anchor_header = client.block_headers([finalized])[0]
        for page in envio.transfer_pages(
            chain.chain_id,
            pool["address"],
            from_block,
            finalized,
            [asset["address"] for asset in assets],
        ):
            if stop and stop.is_set():
                return
            logs = page["logs"]
            to_block = int(page["to_block"])
            headers = page["headers"]
            if to_block == finalized:
                headers = [*headers, anchor_header]
            progress = min(100, max(0, round(
                (to_block - int(pool["start_block"]) + 1) / progress_denominator * 100
            )))
            emit("syncing", f"SYNCING {pool_name} — FETCHING ENVIO LOGS — {progress}%", pool["id"])
            logger.info(
                "pool sync committing page pool_id=%s blocks=%s-%s logs=%s",
                pool["id"], page["from_block"], to_block, len(logs),
            )
            emit("committing", f"SYNCING {pool_name} — COMMITTING DATA", pool["id"])
            counts = _commit_range(
                db, pool, assets, logs, headers, int(page["from_block"]), to_block,
                finalized, redact_rpc_url(client.url), new_from=next_block,
            )
            if counts is None:
                logger.warning("pool sync discarded stale result pool_id=%s", pool["id"])
                return
            swap_count, new_count = counts
            emit("progress", f"Committed through block {to_block:,} — {len(logs):,} transfers", pool["id"], indexed_block=to_block, target_block=finalized, committed_transfers=total_logs + len(logs), committed_swaps=total_swaps + swap_count)
            if stop and stop.is_set():
                return
            total_logs += len(logs)
            total_swaps += swap_count
            new_swaps += new_count
            pages += 1
            if to_block < finalized and (
                new_swaps >= MAX_NEW_SWAPS_PER_TURN or pages >= MAX_PAGES_PER_TURN
                or time.monotonic() - started >= MAX_INGESTION_SECONDS_PER_TURN
            ):
                yielded = True
                break

        if stop and stop.is_set():
            return
        caught_up = to_block >= finalized
        if caught_up:
            emit("rebuilding", f"SYNCING {pool_name} — REBUILDING BALANCES", pool["id"])
            if not rebuild_derived(
                db, pool, assets, client, finalized,
                rebuild_swaps=rebuild_requested,
            ):
                logger.warning("pool sync discarded stale rebuild result pool_id=%s", pool["id"])
                return
        rpc_status = replace(rpc_status, url=client.url, display_url=redact_rpc_url(client.url))
        progress_denominator = max(1, finalized - int(pool["start_block"]) + 1)
        progress = min(1.0, max(0.0, (to_block - int(pool["start_block"]) + 1) / progress_denominator))
        now = datetime.now(timezone.utc)
        committed = db.write_if_current(pool["id"], work_generation, lambda connection: connection.execute(
            """
            UPDATE sync_state
            SET next_block = greatest(next_block, ?), head_block = ?, finalized_block = ?,
                status = ?, progress = ?, error = NULL, active_rpc = ?,
                rebuild_requested = ?, scan_next_block = ?, last_success_at = ?, updated_at = ?
            WHERE pool_id = ?
            """,
            [
                to_block + 1, rpc_status.latest_block, finalized,
                "caught_up" if caught_up else "queued" if yielded else "waiting", progress, rpc_status.display_url,
                rebuild_requested and not caught_up, None if caught_up else max(from_block, to_block + 1),
                now, now, pool["id"],
            ],
        ), incarnation=pool.get("incarnation"), invalidate=False)
        if not committed:
            logger.warning("pool sync discarded stale completion pool_id=%s", pool["id"])
            return
        logger.info(
            "pool sync completed pool_id=%s logs=%s swaps=%s caught_up=%s next_block=%s",
            pool["id"], total_logs, total_swaps, caught_up, to_block + 1,
        )
        if caught_up:
            db.write_if_current(pool["id"], work_generation, lambda c: c.execute("DELETE FROM repairs WHERE pool_id = ?", [pool["id"]]), incarnation=pool.get("incarnation"), invalidate=False)
        emit("syncing", f"History batch complete — pricing downloaded swaps next ({new_swaps:,} new swaps)" if yielded else f"SYNC PASS COMPLETE — {pool_name}", pool["id"])
    except Exception as exc:
        if stop and stop.is_set():
            return
        now = datetime.now(timezone.utc)
        message = sanitize_error(exc)[:1000]
        recorded = db.write_if_current(pool["id"], work_generation, lambda connection: connection.execute(
            "UPDATE sync_state SET status = 'error', error = ?, updated_at = ? WHERE pool_id = ?",
            [message, now, pool["id"]],
        ), incarnation=pool.get("incarnation"), invalidate=False)
        if recorded:
            emit("error", f"SYNC FAILED — {pool_name}", pool["id"])
            logger.exception("pool sync failed pool_id=%s error=%s", pool["id"], message)
        else:
            logger.warning("pool sync discarded stale error pool_id=%s error=%s", pool["id"], message)
    finally:
        if envio:
            envio.close()
        if client:
            client.close()


# ==== Transactional range ingestion ====

def _commit_range(
    db: Database,
    pool: dict,
    assets: list[dict],
    logs: list[dict],
    headers: list[dict],
    from_block: int,
    to_block: int,
    finalized: int,
    active_rpc: str,
    *, new_from: int = 0,
) -> tuple[int, int] | None:
    transfers = _normalized_pool_transfers(logs, assets, pool["address"])
    progress = min(1.0, max(
        0.0,
        (to_block - int(pool["start_block"]) + 1)
        / max(1, finalized - int(pool["start_block"]) + 1),
    ))
    now = datetime.now(timezone.utc)

    counts = None

    def operation(connection):
        nonlocal counts
        connection.execute("CREATE OR REPLACE TEMP TABLE old_range_swaps AS SELECT * FROM swaps WHERE pool_id = ? AND block_number BETWEEN ? AND ?", [pool["id"], from_block, to_block])
        connection.execute(
            "DELETE FROM pool_logs WHERE pool_id = ? AND block_number BETWEEN ? AND ?",
            [pool["id"], from_block, to_block],
        )
        chain_id = CHAINS[pool["chain_key"]].chain_id
        upsert_rows(connection, "blocks", [
            [chain_id, h["number"], h["hash"], datetime.fromtimestamp(h["timestamp"], timezone.utc)]
            for h in {h["number"]: h for h in headers}.values()
        ])
        raw_rows, pool_rows = {}, {}
        for transfer in transfers:
            log = transfer["log"]
            key = (transfer["tx_hash"], transfer["log_index"])
            raw_rows[key] = [
                chain_id, transfer["tx_hash"], transfer["log_index"], transfer["block_number"],
                str(log["blockHash"]).lower(), int(log["transactionIndex"], 16), transfer["token_address"],
                *transfer["topics"], str(log.get("data") or "0x"), log.get("transactionFrom"),
            ]
            pool_rows[key] = [
                pool["id"], chain_id, transfer["tx_hash"], transfer["log_index"],
                transfer["block_number"], transfer["asset_position"],
                str(transfer["signed_raw"]), transfer["signed_amount"],
                transfer["sender"], transfer["recipient"],
            ]
        upsert_rows(connection, "raw_logs", list(raw_rows.values()))
        upsert_rows(connection, "pool_logs", list(pool_rows.values()))
        connection.execute(
            "DELETE FROM swaps WHERE pool_id = ? AND block_number BETWEEN ? AND ?",
            [pool["id"], from_block, to_block],
        )
        insert_reconstructed_swaps(connection, pool["id"], from_block, to_block)
        counts = connection.execute("""
            SELECT count(*), count(*) FILTER (WHERE block_number >= ?)
            FROM swaps WHERE pool_id = ? AND block_number BETWEEN ? AND ?
        """, [new_from, pool["id"], from_block, to_block]).fetchone()
        for table in ("reference_prices", "price_enrichment_attempts"):
            connection.execute(f"""
                DELETE FROM {table} WHERE pool_id = ? AND tx_hash IN (
                    SELECT o.tx_hash FROM old_range_swaps o LEFT JOIN swaps n
                    ON n.pool_id = o.pool_id AND n.tx_hash = o.tx_hash
                    WHERE n.tx_hash IS NULL OR n.timestamp IS DISTINCT FROM o.timestamp
                       OR n.asset0_delta IS DISTINCT FROM o.asset0_delta
                       OR n.asset1_delta IS DISTINCT FROM o.asset1_delta
                       OR n.block_number IS DISTINCT FROM o.block_number
                       OR n.tx_index IS DISTINCT FROM o.tx_index
                )
            """, [pool["id"]])
        connection.execute("DROP TABLE old_range_swaps")
        # Replayed points are rebuilt from the next anchor. The snapshot stays: it is an
        # RPC balance at its own block, so TVL remains available until the anchor advances.
        connection.execute("DELETE FROM balance_points WHERE pool_id = ? AND block_number >= ?", [pool["id"], from_block])
        connection.execute(
            """
            UPDATE sync_state SET next_block = greatest(next_block, ?), scan_next_block = ?, status = 'syncing', progress = ?, head_block = ?,
                finalized_block = ?, active_rpc = ?, error = NULL, updated_at = ? WHERE pool_id = ?
            """,
            [to_block + 1, to_block + 1, progress, finalized, finalized, active_rpc, now, pool["id"]],
        )

    db.write_if_current(pool["id"], int(pool["work_generation"]), operation, incarnation=pool.get("incarnation"))
    return counts


def _normalized_pool_transfers(logs: list[dict], assets: list[dict], pool_address: str) -> list[dict]:
    asset_by_address = {asset["address"].lower(): asset for asset in assets}
    pool_address = pool_address.lower()
    transfers = []
    for log in logs:
        try:
            topics = [str(topic).lower() for topic in (log.get("topics") or [])]
            token_address = str(log["address"]).lower()
            asset = asset_by_address.get(token_address)
            if len(topics) < 3 or not asset:
                continue
            sender = "0x" + topics[1][-40:]
            recipient = "0x" + topics[2][-40:]
            if sender != pool_address and recipient != pool_address:
                continue
            raw_amount = int(str(log.get("data") or "0x0"), 16)
            signed_raw = raw_amount * (int(recipient == pool_address) - int(sender == pool_address))
            transfers.append(
                {
                    "log": log,
                    "topics": topics + [None] * (4 - len(topics)),
                    "tx_hash": str(log["transactionHash"]).lower(),
                    "log_index": int(log["logIndex"], 16),
                    "block_number": int(log["blockNumber"], 16),
                    "token_address": token_address,
                    "asset_position": int(asset["position"]),
                    "signed_raw": signed_raw,
                    "signed_amount": signed_raw / (10 ** int(asset["decimals"])),
                    "sender": sender,
                    "recipient": recipient,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return transfers


# ==== Derived swaps and balances ====

def rebuild_derived(
    db: Database,
    pool: dict,
    assets: list[dict],
    client: RpcClient,
    anchor_block: int,
    *,
    rebuild_swaps: bool = True,
) -> bool:
    anchor_header = client.block_headers([anchor_block])[0]
    anchor_timestamp = datetime.fromtimestamp(anchor_header["timestamp"], timezone.utc)
    anchor_balances = [
        client.token_balance(asset["address"], pool["address"], anchor_block) / (10 ** int(asset["decimals"]))
        for asset in assets
    ]

    def operation(connection):
        checkpoint = None if rebuild_swaps else connection.execute(
            "SELECT block_number, asset0_balance, asset1_balance FROM balance_points "
            "WHERE pool_id = ? AND block_number <= ? ORDER BY block_number DESC LIMIT 1",
            [pool["id"], anchor_block],
        ).fetchone()
        lower = checkpoint[0] if checkpoint else int(pool["start_block"]) - 1
        if checkpoint:
            deltas = connection.execute(
                "SELECT coalesce(sum(signed_amount) FILTER (WHERE asset_position = 0), 0), "
                "coalesce(sum(signed_amount) FILTER (WHERE asset_position = 1), 0) "
                "FROM pool_logs WHERE pool_id = ? AND block_number > ? AND block_number <= ?",
                [pool["id"], lower, anchor_block],
            ).fetchone()
            if any(abs(anchor_balances[i] - deltas[i] - checkpoint[i + 1]) >
                   1e-12 * max(1, abs(anchor_balances[i] - deltas[i]), abs(checkpoint[i + 1])) for i in (0, 1)):
                logger.info("balance checkpoint disagrees with RPC anchor; reconciling full history pool_id=%s", pool["id"])
                lower = int(pool["start_block"]) - 1
        connection.execute("""
            CREATE OR REPLACE TEMP TABLE desired_balance_points AS
            WITH deltas AS (
                SELECT l.block_number, max(b.timestamp) AS timestamp,
                    coalesce(sum(l.signed_amount) FILTER (WHERE l.asset_position = 0), 0) AS delta0,
                    coalesce(sum(l.signed_amount) FILTER (WHERE l.asset_position = 1), 0) AS delta1
                FROM pool_logs l JOIN blocks b ON b.chain_id = l.chain_id AND b.block_number = l.block_number
                WHERE l.pool_id = ? AND l.block_number > ? AND l.block_number <= ? GROUP BY l.block_number
            ) SELECT ?::VARCHAR AS pool_id, block_number, timestamp,
                ? - coalesce(sum(delta0) OVER w, 0) AS asset0_balance,
                ? - coalesce(sum(delta1) OVER w, 0) AS asset1_balance
            FROM deltas WINDOW w AS (ORDER BY block_number DESC ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
        """, [pool["id"], lower, anchor_block, pool["id"], *anchor_balances])
        if rebuild_swaps:
            connection.execute("DELETE FROM swaps WHERE pool_id = ?", [pool["id"]])
            insert_reconstructed_swaps(connection, pool["id"], int(pool["start_block"]), anchor_block)
        for position, balance in enumerate(anchor_balances):
            connection.execute(
                """
                INSERT INTO balance_snapshots AS existing VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (pool_id, asset_position) DO UPDATE SET
                    block_number = excluded.block_number,
                    timestamp = excluded.timestamp,
                    balance = excluded.balance
                WHERE existing.block_number IS DISTINCT FROM excluded.block_number
                   OR existing.timestamp IS DISTINCT FROM excluded.timestamp
                   OR existing.balance IS DISTINCT FROM excluded.balance
                """,
                [pool["id"], position, anchor_block, anchor_timestamp, balance],
            )
        connection.execute(
            """
            DELETE FROM balance_points
            WHERE pool_id = ? AND block_number > ? AND NOT EXISTS (
                SELECT 1 FROM desired_balance_points desired
                WHERE desired.pool_id = balance_points.pool_id
                  AND desired.block_number = balance_points.block_number
                  AND desired.timestamp IS NOT DISTINCT FROM balance_points.timestamp
                  AND abs(desired.asset0_balance - balance_points.asset0_balance)
                      <= 1e-12 * greatest(
                          1, abs(desired.asset0_balance), abs(balance_points.asset0_balance)
                      )
                  AND abs(desired.asset1_balance - balance_points.asset1_balance)
                      <= 1e-12 * greatest(
                          1, abs(desired.asset1_balance), abs(balance_points.asset1_balance)
                      )
            )
            """,
            [pool["id"], lower],
        )
        connection.execute(
            """
            INSERT INTO balance_points
            SELECT desired.* FROM desired_balance_points desired
            WHERE NOT EXISTS (
                SELECT 1 FROM balance_points existing
                WHERE existing.pool_id = desired.pool_id
                  AND existing.block_number = desired.block_number
            )
            """
        )
        connection.execute("DROP TABLE desired_balance_points")
        if rebuild_swaps:
            connection.execute(
                "DELETE FROM reference_prices WHERE pool_id = ? AND tx_hash NOT IN "
                "(SELECT tx_hash FROM swaps WHERE pool_id = ?)",
                [pool["id"], pool["id"]],
            )
            connection.execute(
                "DELETE FROM price_enrichment_attempts WHERE pool_id = ? AND tx_hash NOT IN "
                "(SELECT tx_hash FROM swaps WHERE pool_id = ?)",
                [pool["id"], pool["id"]],
            )

    return db.write_if_current(pool["id"], int(pool["work_generation"]), operation, incarnation=pool.get("incarnation"))


def _mark_caught_up(
    db: Database,
    pool_id: str,
    work_generation: int,
    rpc_status,
    finalized: int,
    *, incarnation: str | None = None,
) -> bool:
    now = datetime.now(timezone.utc)
    return db.write_if_current(pool_id, work_generation, lambda connection: connection.execute(
        """
        UPDATE sync_state SET head_block = ?, finalized_block = ?, status = 'caught_up', scan_next_block = NULL,
            progress = 1, error = NULL, active_rpc = ?, last_success_at = ?, updated_at = ?
        WHERE pool_id = ?
        """,
        [rpc_status.latest_block, finalized, rpc_status.display_url, now, now, pool_id],
    ), incarnation=incarnation, invalidate=False)
