from __future__ import annotations

import threading
import json
import uuid
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import duckdb


SCHEMA_VERSION = 11


def upsert_rows(connection, table: str, rows: list) -> None:
    """Write a batch in one statement; callers supply internal table names only."""
    if not rows:
        return
    columns = connection.execute(f"DESCRIBE {table}").fetchall()
    values = ", ".join(f"unnest(?::{column[1]}[])" for column in columns)
    connection.execute(f"INSERT OR REPLACE INTO {table} SELECT {values}",
                       [list(column) for column in zip(*rows, strict=True)])


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._revision_epoch = uuid.uuid4().hex
        self._global_revision = 0
        self._pool_revisions = {}
        self._analytics_cache = OrderedDict()
        self._analytics_cache_bytes = 0
        self.connection = duckdb.connect(str(self.path), config={
            'threads': max(1, int(os.getenv('OPEN_METRIC_DASHBOARD_DB_THREADS', min(4, os.cpu_count() or 1)))),
            'memory_limit': os.getenv('OPEN_METRIC_DASHBOARD_DB_MEMORY_LIMIT', '512MiB'),
        })
        try:
            self._initialize()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    # ==== Schema initialization and migrations ====

    def _initialize(self) -> None:
        tables = {r[0] for r in self.connection.execute("SHOW TABLES").fetchall()}
        versions = self.connection.execute("SELECT version FROM schema_meta").fetchall() if 'schema_meta' in tables else []
        version = versions[0][0] if versions else 0
        if len(versions) > 1 or version not in range(SCHEMA_VERSION + 1):
            raise RuntimeError(f"Unsupported database schema {version}; expected {SCHEMA_VERSION}")
        if version == SCHEMA_VERSION:
            return
        # Fresh DDL and historical upgrades commit with their version, or not at all.
        statements = [
            "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)",
            """
            CREATE TABLE IF NOT EXISTS chain_rpc_overrides (
                chain_key VARCHAR PRIMARY KEY,
                rpc_url VARCHAR NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS envio_settings (
                singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                api_key VARCHAR,
                rpm_limit INTEGER NOT NULL DEFAULT 15 CHECK (rpm_limit > 0),
                updated_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS asset_price_preferences (
                chain_key VARCHAR NOT NULL,
                token_address VARCHAR NOT NULL,
                price_kind VARCHAR NOT NULL,
                exchange VARCHAR,
                market VARCHAR,
                market_type VARCHAR,
                fixed_price DOUBLE,
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (chain_key, token_address)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pools (
                id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                chain_key VARCHAR NOT NULL,
                address VARCHAR NOT NULL,
                start_block BIGINT NOT NULL,
                contract_type VARCHAR NOT NULL,
                paused BOOLEAN NOT NULL DEFAULT FALSE,
                config_revision INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                incarnation VARCHAR DEFAULT CAST(uuid() AS VARCHAR),
                mtm_mode VARCHAR NOT NULL DEFAULT 'balanced'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pool_assets (
                pool_id VARCHAR NOT NULL,
                position SMALLINT NOT NULL,
                symbol VARCHAR NOT NULL,
                address VARCHAR NOT NULL,
                decimals SMALLINT NOT NULL,
                price_kind VARCHAR NOT NULL,
                exchange VARCHAR,
                market VARCHAR,
                market_type VARCHAR,
                fixed_price DOUBLE,
                quote_asset VARCHAR,
                quote_verified BOOLEAN DEFAULT FALSE,
                quote_error VARCHAR,
                PRIMARY KEY (pool_id, position)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                pool_id VARCHAR PRIMARY KEY,
                next_block BIGINT NOT NULL,
                head_block BIGINT,
                finalized_block BIGINT,
                status VARCHAR NOT NULL,
                progress DOUBLE NOT NULL DEFAULT 0,
                error VARCHAR,
                active_rpc VARCHAR,
                rebuild_requested BOOLEAN NOT NULL DEFAULT FALSE,
                work_generation BIGINT NOT NULL DEFAULT 1,
                last_success_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL,
                scan_next_block BIGINT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS blocks (
                chain_id BIGINT NOT NULL,
                block_number BIGINT NOT NULL,
                block_hash VARCHAR NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (chain_id, block_number)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS raw_logs (
                chain_id BIGINT NOT NULL,
                tx_hash VARCHAR NOT NULL,
                log_index BIGINT NOT NULL,
                block_number BIGINT NOT NULL,
                block_hash VARCHAR NOT NULL,
                tx_index BIGINT NOT NULL,
                address VARCHAR NOT NULL,
                topic0 VARCHAR NOT NULL,
                topic1 VARCHAR,
                topic2 VARCHAR,
                topic3 VARCHAR,
                data VARCHAR NOT NULL,
                tx_from VARCHAR,
                PRIMARY KEY (chain_id, tx_hash, log_index)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pool_logs (
                pool_id VARCHAR NOT NULL,
                chain_id BIGINT NOT NULL,
                tx_hash VARCHAR NOT NULL,
                log_index BIGINT NOT NULL,
                block_number BIGINT NOT NULL,
                asset_position SMALLINT NOT NULL,
                signed_raw VARCHAR NOT NULL,
                signed_amount DOUBLE NOT NULL,
                sender VARCHAR NOT NULL,
                recipient VARCHAR NOT NULL,
                PRIMARY KEY (pool_id, chain_id, tx_hash, log_index)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS swaps (
                pool_id VARCHAR NOT NULL,
                tx_hash VARCHAR NOT NULL,
                block_number BIGINT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                asset0_delta DOUBLE NOT NULL,
                asset1_delta DOUBLE NOT NULL,
                counterparty VARCHAR,
                tx_index BIGINT,
                sender VARCHAR,
                recipient VARCHAR,
                signer VARCHAR,
                PRIMARY KEY (pool_id, tx_hash)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS reference_prices (
                pool_id VARCHAR NOT NULL,
                tx_hash VARCHAR NOT NULL,
                asset_position SMALLINT NOT NULL,
                config_revision INTEGER NOT NULL,
                provider VARCHAR NOT NULL,
                market VARCHAR,
                market_type VARCHAR,
                target_timestamp TIMESTAMPTZ NOT NULL,
                source_trade_id VARCHAR,
                source_timestamp TIMESTAMPTZ NOT NULL,
                price DOUBLE NOT NULL,
                lag_ms BIGINT NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (pool_id, tx_hash, asset_position)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS price_enrichment_attempts (
                pool_id VARCHAR NOT NULL,
                tx_hash VARCHAR NOT NULL,
                asset_position SMALLINT NOT NULL,
                config_revision INTEGER NOT NULL,
                attempted_at TIMESTAMPTZ NOT NULL,
                retry_after TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (pool_id, tx_hash, asset_position, config_revision)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                pool_id VARCHAR NOT NULL,
                asset_position SMALLINT NOT NULL,
                block_number BIGINT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                balance DOUBLE NOT NULL,
                PRIMARY KEY (pool_id, asset_position)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS balance_points (
                pool_id VARCHAR NOT NULL,
                block_number BIGINT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                asset0_balance DOUBLE NOT NULL,
                asset1_balance DOUBLE NOT NULL,
                PRIMARY KEY (pool_id, block_number)
            )
            """,
        ]

        def initialize(connection):
            for statement in statements:
                connection.execute(statement)
            connection.execute("""CREATE TABLE IF NOT EXISTS stage_status (
                pool_id VARCHAR, incarnation VARCHAR, stage VARCHAR, error VARCHAR, last_success_at TIMESTAMPTZ,
                PRIMARY KEY (pool_id, incarnation, stage))""")
            connection.execute("CREATE TABLE IF NOT EXISTS repairs (pool_id VARCHAR PRIMARY KEY, reason VARCHAR)")
            if 'pools' in tables:
                self._upgrade(connection, version)
            connection.execute("DELETE FROM schema_meta")
            connection.execute("INSERT INTO schema_meta VALUES (?)", [SCHEMA_VERSION])

        self.write(initialize, invalidate=False)

    def _upgrade(self, connection, version):
        connection.execute("ALTER TABLE pools ADD COLUMN IF NOT EXISTS mtm_mode VARCHAR DEFAULT 'balanced'")
        # Version 11 adds swap sides and transaction signers; older rows keep NULL until reindexed.
        connection.execute("ALTER TABLE raw_logs ADD COLUMN IF NOT EXISTS tx_from VARCHAR")
        for column in ("sender", "recipient", "signer"):
            connection.execute(f"ALTER TABLE swaps ADD COLUMN IF NOT EXISTS {column} VARCHAR")
        if version >= 9:
            return
        if version == 1:
            connection.execute("""
                INSERT OR REPLACE INTO asset_price_preferences
                SELECT chain_key, address, price_kind, exchange, market, market_type, fixed_price, updated_at
                FROM (SELECT p.chain_key, a.*, p.updated_at,
                    row_number() OVER (PARTITION BY p.chain_key, lower(a.address)
                        ORDER BY p.updated_at DESC, p.id) AS preference_rank
                    FROM pool_assets a JOIN pools p ON p.id = a.pool_id WHERE a.price_kind != 'none') remembered
                WHERE preference_rank = 1
            """)
        if version <= 3:
            connection.execute("ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS work_generation BIGINT DEFAULT 1")
            connection.execute("UPDATE sync_state SET work_generation = 1 WHERE work_generation IS NULL")
            connection.execute("ALTER TABLE sync_state ALTER COLUMN work_generation SET NOT NULL")
        if version <= 4:
            connection.execute("ALTER TABLE sync_state DROP COLUMN IF EXISTS query_blocks")
        # Version 8 could be recorded before fresh initialization finished. Repair
        # those missing columns here once; current-schema startup does no repairs.
        missing_order = not connection.execute("""SELECT 1 FROM information_schema.columns
            WHERE table_name = 'swaps' AND column_name = 'tx_index'""").fetchone()
        connection.execute("ALTER TABLE swaps ADD COLUMN IF NOT EXISTS counterparty VARCHAR")
        connection.execute("ALTER TABLE pools ADD COLUMN IF NOT EXISTS incarnation VARCHAR DEFAULT CAST(uuid() AS VARCHAR)")
        connection.execute("ALTER TABLE swaps ADD COLUMN IF NOT EXISTS tx_index BIGINT")
        connection.execute("ALTER TABLE pool_assets ADD COLUMN IF NOT EXISTS quote_asset VARCHAR")
        connection.execute("ALTER TABLE pool_assets ADD COLUMN IF NOT EXISTS quote_verified BOOLEAN DEFAULT FALSE")
        connection.execute("ALTER TABLE pool_assets ADD COLUMN IF NOT EXISTS quote_error VARCHAR")
        connection.execute("ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS scan_next_block BIGINT")
        if version <= 6 or missing_order:
            connection.execute("INSERT OR REPLACE INTO repairs SELECT id, 'Repairing transaction order and transfer accounting' FROM pools")
        connection.execute("""UPDATE pool_assets SET quote_verified = FALSE,
            quote_error = 'Bybit pricing is retired; edit the pool and choose Binance, Fixed USD, or None'
            WHERE price_kind = 'bybit' OR exchange = 'bybit'""")

    # ==== Query and transaction primitives ====

    def rows(self, sql: str, params: list | tuple | None = None) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self.connection.execute(sql, params or [])
            names = [item[0] for item in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def row(self, sql: str, params: list | tuple | None = None) -> dict[str, Any] | None:
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def data_revision(self, pool_id: str) -> str:
        return f"{self._revision_epoch}:{self._global_revision}:{self._pool_revisions.get(pool_id, 0)}"

    def cached_analytics(self, key, build):
        """Cache bounded JSON only; callers hold the database read lock."""
        now = datetime.now(timezone.utc)
        cached = self._analytics_cache.pop(key, None)
        if cached:
            expires, body = cached
            if expires > now:
                self._analytics_cache[key] = cached
                return json.loads(body)
            self._analytics_cache_bytes -= len(body)
        result, expires = build()
        body = json.dumps(result, separators=(",", ":"), allow_nan=False).encode()
        budget = 32 * 1024 * 1024
        if len(body) <= budget:
            while self._analytics_cache and self._analytics_cache_bytes + len(body) > budget:
                _, (_, removed) = self._analytics_cache.popitem(last=False)
                self._analytics_cache_bytes -= len(removed)
            self._analytics_cache[key] = (expires, body)
            self._analytics_cache_bytes += len(body)
        return result

    def write(self, callback: Callable[[duckdb.DuckDBPyConnection], Any], *, invalidate=True) -> Any:
        with self._lock:
            self.connection.execute("BEGIN TRANSACTION")
            try:
                result = callback(self.connection)
                self.connection.execute("COMMIT")
                if invalidate and result is not False:
                    if isinstance(invalidate, str):
                        self._pool_revisions[invalidate] = self._pool_revisions.get(invalidate, 0) + 1
                    else:
                        self._global_revision += 1
                    # Old revisions cannot be reused; discard them immediately.
                    for key in list(self._analytics_cache):
                        if not isinstance(invalidate, str) or invalidate in key[1]:
                            _, body = self._analytics_cache.pop(key)
                            self._analytics_cache_bytes -= len(body)
                return result
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def write_if_current(
        self,
        pool_id: str,
        work_generation: int,
        callback: Callable[[duckdb.DuckDBPyConnection], Any],
        *, incarnation: str | None = None,
        invalidate: bool = True,
    ) -> bool:
        def guarded(connection):
            current = connection.execute(
                "SELECT s.work_generation, p.incarnation, p.paused FROM sync_state s JOIN pools p ON p.id = s.pool_id WHERE pool_id = ?",
                [pool_id],
            ).fetchone()
            if (not current or int(current[0]) != int(work_generation) or current[2]
                    or (incarnation is not None and current[1] != incarnation)):
                return False
            callback(connection)
            return True

        return bool(self.write(guarded, invalidate=pool_id if invalidate else False))

    # ==== Configuration and pool lifecycle ====

    def rpc_overrides(self) -> dict[str, str]:
        return {
            row["chain_key"]: row["rpc_url"]
            for row in self.rows("SELECT chain_key, rpc_url FROM chain_rpc_overrides")
        }

    def set_rpc_override(self, chain_key: str, rpc_url: str) -> None:
        now = datetime.now(timezone.utc)

        def operation(connection):
            connection.execute("DELETE FROM chain_rpc_overrides WHERE chain_key = ?", [chain_key])
            connection.execute(
                "INSERT INTO chain_rpc_overrides VALUES (?, ?, ?)",
                [chain_key, rpc_url, now],
            )
            _queue_pools(connection, now, chain_key)

        self.write(operation)

    def clear_rpc_override(self, chain_key: str) -> None:
        self.write(lambda connection: connection.execute(
            "DELETE FROM chain_rpc_overrides WHERE chain_key = ?", [chain_key]
        ))

    def envio_settings(self) -> dict[str, Any]:
        row = self.row("SELECT api_key, rpm_limit FROM envio_settings WHERE singleton = TRUE")
        return row or {"api_key": None, "rpm_limit": 15}

    def set_envio_settings(self, *, api_key: str | None, rpm_limit: int) -> None:
        current = self.envio_settings()
        key = str(api_key).strip() if api_key is not None else current["api_key"]
        if not key:
            raise ValueError("Envio API key is required")
        if int(rpm_limit) <= 0:
            raise ValueError("Envio RPM limit must be positive")
        now = datetime.now(timezone.utc)

        def operation(connection):
            connection.execute("INSERT OR REPLACE INTO envio_settings VALUES (TRUE, ?, ?, ?)", [key, int(rpm_limit), now])
            _queue_pools(connection, now)

        self.write(operation)

    def clear_envio_key(self) -> None:
        current = self.envio_settings()
        self.write(lambda connection: connection.execute(
            "INSERT OR REPLACE INTO envio_settings VALUES (TRUE, NULL, ?, ?)",
            [int(current["rpm_limit"]), datetime.now(timezone.utc)],
        ))

    def price_preferences(
        self,
        chain_key: str | None = None,
        token_addresses: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, dict[str, Any]]:
        clauses = ["price_kind IN ('fixed', 'binance')"]
        params = []
        if chain_key:
            clauses.append("chain_key = ?")
            params.append(chain_key)
        if token_addresses:
            normalized = [address.lower() for address in token_addresses]
            clauses.append(f"lower(token_address) IN ({', '.join('?' for _ in normalized)})")
            params.extend(normalized)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return {
            row["token_address"].lower(): row
            for row in self.rows(
                f"SELECT * FROM asset_price_preferences {where} ORDER BY chain_key, token_address",
                params,
            )
        }

    def latest_cross_chain_price_preference(
        self, chain_key: str, symbol: str, decimals: int
    ) -> dict[str, Any] | None:
        return self.row(
            """
            SELECT a.price_kind, a.exchange, a.market, a.market_type, a.fixed_price
            FROM pool_assets a JOIN pools p ON p.id = a.pool_id
            WHERE p.chain_key != ? AND upper(a.symbol) = upper(?) AND a.decimals = ?
              AND a.price_kind IN ('fixed', 'binance')
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT 1
            """,
            [chain_key, symbol, decimals],
        )

    def list_pools(self) -> list[dict[str, Any]]:
        pools = self.rows(
            """
            SELECT p.*, s.next_block, s.head_block, s.finalized_block, s.status,
                   s.progress, s.error, s.active_rpc, s.last_success_at
            FROM pools p
            LEFT JOIN sync_state s ON s.pool_id = p.id
            ORDER BY lower(p.name), p.id
            """
        )
        assets = self.rows("SELECT * FROM pool_assets ORDER BY pool_id, position")
        by_pool: dict[str, list[dict]] = {}
        for asset in assets:
            by_pool.setdefault(asset["pool_id"], []).append(asset)
        for pool in pools:
            pool["assets"] = by_pool.get(pool["id"], [])
        return pools

    def get_pool(self, pool_id: str) -> dict[str, Any] | None:
        pool = self.row(
            """
            SELECT p.*, s.work_generation
            FROM pools p JOIN sync_state s ON s.pool_id = p.id
            WHERE p.id = ?
            """,
            [pool_id],
        )
        if not pool:
            return None
        pool["assets"] = self.rows(
            "SELECT * FROM pool_assets WHERE pool_id = ? ORDER BY position", [pool_id]
        )
        return pool

    def create_pool(self, pool: dict[str, Any], assets: list[dict[str, Any]]) -> None:
        mtm_mode = pool.get('mtm_mode', 'balanced')
        if mtm_mode not in {'balanced', 'as_is'}:
            raise ValueError('Invalid MTM mode')
        now = datetime.now(timezone.utc)

        def operation(connection):
            connection.execute(
                """
                INSERT INTO pools
                (id, name, chain_key, address, start_block, contract_type, paused,
                 config_revision, created_at, updated_at, mtm_mode)
                VALUES (?, ?, ?, ?, ?, ?, FALSE, 1, ?, ?, ?)
                """,
                [
                    pool["id"], pool["name"], pool["chain_key"], pool["address"],
                    pool["start_block"], pool["contract_type"], now, now, mtm_mode,
                ],
            )
            for position, asset in enumerate(assets):
                connection.execute(
                    """
                    INSERT INTO pool_assets
                    (pool_id, position, symbol, address, decimals, price_kind,
                     exchange, market, market_type, fixed_price, quote_asset, quote_verified, quote_error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        pool["id"], position, asset["symbol"], asset["address"],
                        asset["decimals"], asset["price_kind"], asset.get("exchange"),
                        asset.get("market"), asset.get("market_type"), asset.get("fixed_price"),
                        asset.get("quote_asset"), bool(asset.get("quote_verified")), asset.get("quote_error"),
                    ],
                )
            self._remember_price_preferences(connection, pool["chain_key"], assets, now)
            connection.execute(
                """
                INSERT INTO sync_state
                (pool_id, next_block, status, progress, updated_at)
                VALUES (?, ?, 'queued', 0, ?)
                """,
                [pool["id"], pool["start_block"], now],
            )

        self.write(operation, invalidate=pool["id"])

    def update_pool(self, pool_id: str, name: str, start_block: int, assets: list[dict[str, Any]], *, mtm_mode: str | None = None) -> None:
        existing = self.get_pool(pool_id)
        if not existing:
            raise KeyError(pool_id)
        mtm_mode = existing['mtm_mode'] if mtm_mode is None else mtm_mode
        if mtm_mode not in {'balanced', 'as_is'}:
            raise ValueError('Invalid MTM mode')
        start_changed = int(existing["start_block"]) != int(start_block)
        old_prices = [
            (a["price_kind"], a.get("exchange"), a.get("market"), a.get("market_type"), a.get("fixed_price"))
            for a in existing["assets"]
        ]
        new_prices = [
            (a["price_kind"], a.get("exchange"), a.get("market"), a.get("market_type"), a.get("fixed_price"))
            for a in assets
        ]
        price_changed = old_prices != new_prices
        now = datetime.now(timezone.utc)

        def operation(connection):
            if price_changed or start_changed:
                connection.execute(
                    "UPDATE sync_state SET work_generation = work_generation + 1 WHERE pool_id = ?",
                    [pool_id],
                )
            connection.execute(
                """
                UPDATE pools
                SET name = ?, start_block = ?,
                    config_revision = config_revision + ?, updated_at = ?, mtm_mode = ?
                WHERE id = ?
                """,
                [name, start_block, 1 if price_changed else 0, now, mtm_mode, pool_id],
            )
            for position, asset in enumerate(assets):
                connection.execute(
                    """
                    UPDATE pool_assets
                    SET price_kind = ?, exchange = ?, market = ?, market_type = ?, fixed_price = ?,
                        quote_asset = ?, quote_verified = ?, quote_error = ?
                    WHERE pool_id = ? AND position = ?
                    """,
                    [
                        asset["price_kind"], asset.get("exchange"), asset.get("market"),
                        asset.get("market_type"), asset.get("fixed_price"), asset.get("quote_asset"),
                        bool(asset.get("quote_verified")), asset.get("quote_error"), pool_id, position,
                    ],
                )
            self._remember_price_preferences(connection, existing["chain_key"], assets, now)
            if price_changed:
                connection.execute("DELETE FROM reference_prices WHERE pool_id = ?", [pool_id])
                connection.execute("DELETE FROM price_enrichment_attempts WHERE pool_id = ?", [pool_id])
                connection.execute(
                    "UPDATE sync_state SET status = 'queued', error = NULL, updated_at = ? WHERE pool_id = ?",
                    [now, pool_id],
                )
            if start_changed:
                self._delete_pool_data(connection, pool_id)
                connection.execute(
                    """
                    UPDATE sync_state
                    SET next_block = ?, scan_next_block = NULL, head_block = NULL, finalized_block = NULL,
                        status = 'queued', progress = 0, error = NULL,
                        rebuild_requested = FALSE, updated_at = ?
                    WHERE pool_id = ?
                    """,
                    [start_block, now, pool_id],
                )

        self.write(operation, invalidate=pool_id)

    def set_paused(self, pool_id: str, paused: bool) -> None:
        now = datetime.now(timezone.utc)

        def operation(connection):
            connection.execute(
                "UPDATE pools SET paused = ?, updated_at = ? WHERE id = ?",
                [paused, now, pool_id],
            )
            connection.execute(
                """
                UPDATE sync_state
                SET status = ?, work_generation = work_generation + 1, updated_at = ?
                WHERE pool_id = ?
                """,
                ["paused" if paused else "queued", now, pool_id],
            )

        self.write(operation, invalidate=pool_id)

    def request_rebuild(self, pool_id: str) -> None:
        now = datetime.now(timezone.utc)
        self.write(lambda connection: connection.execute(
            """
            UPDATE sync_state
            SET rebuild_requested = TRUE, status = 'queued', error = NULL,
                work_generation = work_generation + 1, updated_at = ?
            WHERE pool_id = ?
            """,
            [now, pool_id],
        ), invalidate=pool_id)

    def delete_pool(self, pool_id: str) -> None:
        def operation(connection):
            self._delete_pool_data(connection, pool_id)
            connection.execute("DELETE FROM sync_state WHERE pool_id = ?", [pool_id])
            connection.execute("DELETE FROM pool_assets WHERE pool_id = ?", [pool_id])
            connection.execute("DELETE FROM pools WHERE id = ?", [pool_id])
            connection.execute("DELETE FROM stage_status WHERE pool_id = ?", [pool_id])
            connection.execute("DELETE FROM repairs WHERE pool_id = ?", [pool_id])
            connection.execute(
                """
                DELETE FROM raw_logs r
                WHERE NOT EXISTS (
                    SELECT 1 FROM pool_logs p
                    WHERE p.chain_id = r.chain_id AND p.tx_hash = r.tx_hash AND p.log_index = r.log_index
                )
                """
            )

        self.write(operation, invalidate=pool_id)

    @staticmethod
    def _delete_pool_data(connection, pool_id: str) -> None:
        for table in (
            "pool_logs", "swaps", "reference_prices", "price_enrichment_attempts",
            "balance_snapshots", "balance_points"
        ):
            connection.execute(f"DELETE FROM {table} WHERE pool_id = ?", [pool_id])

    @staticmethod
    def _remember_price_preferences(connection, chain_key: str, assets: list[dict], now: datetime) -> None:
        for asset in assets:
            if asset["price_kind"] == "none":
                continue
            connection.execute(
                """
                INSERT OR REPLACE INTO asset_price_preferences
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    chain_key, asset["address"].lower(), asset["price_kind"],
                    asset.get("exchange"), asset.get("market"), asset.get("market_type"),
                    asset.get("fixed_price"), now,
                ],
            )

    def sync_candidates(self) -> list[dict[str, Any]]:
        return self.rows(
            """
            SELECT p.*, s.next_block, s.scan_next_block, s.head_block, s.finalized_block,
                   s.status, s.rebuild_requested, s.work_generation,
                   s.updated_at AS sync_updated_at
            FROM pools p JOIN sync_state s ON s.pool_id = p.id
            WHERE p.paused = FALSE
            ORDER BY CASE WHEN s.status = 'queued' THEN 0 ELSE 1 END, s.updated_at, p.id
            """
        )


def _queue_pools(connection, now: datetime, chain_key: str | None = None) -> None:
    """Make idle pools due right away after a connection setting changes, instead of after the refresh interval."""
    connection.execute("""
        UPDATE sync_state SET status = 'queued', error = NULL, updated_at = ?
        WHERE status NOT IN ('queued', 'syncing')
          AND pool_id IN (SELECT id FROM pools WHERE NOT paused AND (? IS NULL OR chain_key = ?))
    """, [now, chain_key, chain_key])
