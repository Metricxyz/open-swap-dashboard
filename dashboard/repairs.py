"""One-time data repair, deliberately outside schema migration and normal ingestion."""
import math

from .database import Database
from .prices import provider_deadline, validate_market
from .rpc import sanitize_error


def repair_pool(db: Database, pool: dict) -> bool:
    repair = db.row("SELECT * FROM repairs WHERE pool_id = ?", [pool["id"]])
    if not repair or repair["reason"].startswith("Awaiting"):
        return False

    def operation(connection):
        connection.execute("""
            UPDATE pool_logs SET signed_raw = '0', signed_amount = 0
            WHERE pool_id = ? AND sender = recipient
        """, [pool["id"]])
        connection.execute("""
            UPDATE swaps SET tx_index = source.tx_index FROM (
                SELECT l.pool_id, l.tx_hash, min(r.tx_index) AS tx_index
                FROM pool_logs l JOIN raw_logs r
                  ON r.chain_id = l.chain_id AND r.tx_hash = l.tx_hash AND r.log_index = l.log_index
                WHERE l.pool_id = ? GROUP BY l.pool_id, l.tx_hash
            ) source WHERE swaps.pool_id = source.pool_id AND swaps.tx_hash = source.tx_hash
        """, [pool["id"]])
        missing = connection.execute("""
            SELECT count(*) FROM pool_logs l LEFT JOIN raw_logs r
                ON r.chain_id = l.chain_id AND r.tx_hash = l.tx_hash AND r.log_index = l.log_index
            WHERE l.pool_id = ? AND r.tx_index IS NULL
        """, [pool["id"]]).fetchone()[0]
        connection.execute("DELETE FROM reference_prices WHERE pool_id = ?", [pool["id"]])
        connection.execute("DELETE FROM price_enrichment_attempts WHERE pool_id = ?", [pool["id"]])
        connection.execute("""
            UPDATE sync_state SET rebuild_requested = TRUE, status = 'queued', scan_next_block = NULL,
                next_block = CASE WHEN ? THEN (SELECT start_block FROM pools WHERE id = ?) ELSE next_block END
            WHERE pool_id = ?
        """, [bool(missing), pool["id"], pool["id"]])
        connection.execute("UPDATE repairs SET reason = ? WHERE pool_id = ?", ["Awaiting reingestion of missing transaction metadata" if missing else "Awaiting derived history rebuild", pool["id"]])

    return db.write_if_current(pool["id"], pool["work_generation"], operation, incarnation=pool["incarnation"])


def repair_price_configuration(db: Database, pool: dict, report) -> None:
    for asset in pool["assets"]:
        if asset["price_kind"] == "fixed" and (asset["fixed_price"] is None or not math.isfinite(asset["fixed_price"]) or asset["fixed_price"] <= 0):
            raise ValueError(f"{asset['symbol']} fixed price must be finite and positive; edit the pool pricing")
        if asset.get("quote_error"):
            raise ValueError(asset["quote_error"])
        if asset["price_kind"] not in {"none", "fixed"} and not asset["quote_verified"]:
            report("waiting", "Verifying market quote: " + asset["market"], pool["id"])
            try:
                quote = validate_market(asset["exchange"], asset["market"], asset["market_type"])
            except ValueError as exc:
                if not provider_deadline(asset["exchange"]):
                    db.write_if_current(pool["id"], pool["work_generation"], lambda c: c.execute(
                        "UPDATE pool_assets SET quote_error = ? WHERE pool_id = ? AND position = ?",
                        [sanitize_error(exc), pool["id"], asset["position"]]), incarnation=pool["incarnation"])
                raise
            db.write_if_current(pool["id"], pool["work_generation"], lambda c: c.execute(
                "UPDATE pool_assets SET quote_asset = ?, quote_verified = TRUE WHERE pool_id = ? AND position = ?",
                [quote, pool["id"], asset["position"]]), incarnation=pool["incarnation"])
