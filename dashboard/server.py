from __future__ import annotations

import logging
import math
import os
import re
import secrets
import hashlib
import json
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from .analytics import dashboard_payload, pool_summaries, swap_page
from .chains import CHAINS, public_chain_rows
from .database import Database
from .ingestion import discover_pool_assets, effective_rpc_urls, validate_pool_input
from .logging_config import configure_logging
from .prices import validate_market
from .rpc import connect_rpc, validate_rpc_url, sanitize_error
from .scheduler import Scheduler


ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)


# ==== Request models ====

class RpcInput(BaseModel):
    url: str


class RpcTestInput(BaseModel):
    chain_key: str
    url: str | None = None


class EnvioSettingsInput(BaseModel):
    api_key: str | None = None
    rpm_limit: int = Field(default=30, ge=1)


class AssetInput(BaseModel):
    address: str
    price_kind: str = "none"
    market: str | None = None
    market_type: str = "spot"
    fixed_price: float | None = None


class PoolInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    chain_key: str
    address: str
    start_block: int | None = Field(default=None, ge=0)
    assets: list[AssetInput]
    mtm_mode: Literal['balanced', 'as_is'] = 'balanced'


class PoolDiscoveryInput(BaseModel):
    chain_key: str
    address: str


class PoolUpdateInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    start_block: int = Field(ge=0)
    assets: list[AssetInput]
    mtm_mode: Literal['balanced', 'as_is'] | None = None


class PauseInput(BaseModel):
    paused: bool


# ==== Application and routes ====

def create_app(
    *,
    database_path: str | Path | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    data_dir = Path(os.getenv("OPEN_METRIC_DASHBOARD_DATA_DIR", ROOT / "data"))
    log_data_dir = Path(database_path).parent if database_path is not None else data_dir
    log_path = configure_logging(log_data_dir)
    db = Database(database_path or data_dir / "dashboard.duckdb")
    scheduler = Scheduler(
        db,
        data_dir,
        refresh_seconds=int(os.getenv("OPEN_METRIC_DASHBOARD_REFRESH_SECONDS", "60")),
    )
    csrf_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_app):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()
        db.close()

    app = FastAPI(title="Open Metric Dashboard", version="0.1.0", lifespan=lifespan)
    app.state.db = db
    app.state.scheduler = scheduler
    app.state.csrf_token = csrf_token
    app.state.log_path = log_path

    allowed_hosts = [
        item.strip()
        for item in os.getenv(
            "OPEN_METRIC_DASHBOARD_ALLOWED_HOSTS",
            "127.0.0.1,localhost,[::1],testserver",
        ).split(",")
        if item.strip()
    ]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def protect_mutations(request: Request, call_next):
        rejection = _mutation_rejection(request, csrf_token)
        if rejection:
            return rejection
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/bootstrap")
    def bootstrap(hours: Annotated[float, Query(ge=1, le=720)] = 24.0):
        with db._lock:
            overrides = db.rpc_overrides()
            envio = db.envio_settings()
            pools = db.list_pools()
            summaries = pool_summaries(db, pools, hours)
        for pool in pools:
            pool["summary"] = summaries["pools"][pool["id"]]
        return {
            "csrf_token": csrf_token,
            "chains": public_chain_rows(overrides),
            "envio": {
                "configured": bool(envio["api_key"]),
                "rpm_limit": int(envio["rpm_limit"]),
            },
            "pools": [_json_pool(pool) for pool in pools],
            "summary_window_hours": float(hours),
            "total_summary": summaries["total"],
        }

    @app.get("/api/status")
    async def status():
        return scheduler.status()

    @app.get("/api/pools/{pool_id}/dashboard")
    def pool_dashboard(pool_id: str, hours: Annotated[float, Query(ge=1, le=720)] = 24,
                       request: Request = None, window_end: datetime | None = None,
                       range_start: datetime | None = None, range_end: datetime | None = None,
                       plots: str = 'pnl,price', minimum_swap_usd: float = 0,
                       page: Annotated[int, Query(ge=0)] = 0, counterparty_side: str = 'recipient'):
        try:
            payload = dashboard_payload(db, pool_id, hours, window_end=window_end,
                                        range_start=range_start, range_end=range_end,
                                        plots=plots.split(',') if plots else [], minimum_swap_usd=minimum_swap_usd,
                                        swap_page_index=page, counterparty_side=counterparty_side)
            body = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode()
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            headers = {'ETag': etag, 'Cache-Control': 'private, no-cache'}
            if request and request.headers.get('if-none-match') == etag:
                return Response(status_code=304, headers=headers)
            return Response(body, media_type='application/json', headers=headers)
        except KeyError:
            raise HTTPException(404, "Pool not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/pools/{pool_id}/swaps')
    def pool_swap_page(pool_id: str, window_end: datetime, revision: str,
                       hours: Annotated[float, Query(ge=1, le=720)] = 24,
                       page: Annotated[int, Query(ge=0)] = 0,
                       range_start: datetime | None = None, range_end: datetime | None = None):
        try:
            return swap_page(db, pool_id, hours, window_end=window_end, revision=revision,
                             page=page, range_start=range_start, range_end=range_end)
        except KeyError:
            raise HTTPException(404, 'Pool not found') from None
        except ValueError as exc:
            raise HTTPException(409 if str(exc).startswith('Dashboard data changed') else 422, str(exc)) from None

    @app.post("/api/rpc/test")
    def test_rpc(payload: RpcTestInput):
        chain = CHAINS.get(payload.chain_key)
        if not chain:
            raise HTTPException(400, "Unsupported chain")
        urls = [validate_rpc_url(payload.url)] if payload.url else effective_rpc_urls(db, chain.key)
        try:
            client, status = connect_rpc(urls, chain.chain_id)
            client.close()
            return {
                "ok": True,
                "chain_id": status.chain_id,
                "latest_block": status.latest_block,
                "rpc": status.display_url,
            }
        except Exception as exc:
            raise HTTPException(400, sanitize_error(exc)) from None

    @app.put("/api/chains/{chain_key}/rpc")
    def save_rpc(chain_key: str, payload: RpcInput):
        chain = CHAINS.get(chain_key)
        if not chain:
            raise HTTPException(404, "Unsupported chain")
        try:
            url = validate_rpc_url(payload.url)
            client, status = connect_rpc([url], chain.chain_id)
            client.close()
            db.set_rpc_override(chain_key, url)
            scheduler.wake()
            return {"ok": True, "rpc": status.display_url, "latest_block": status.latest_block}
        except Exception as exc:
            raise HTTPException(400, sanitize_error(exc)) from None

    @app.delete("/api/chains/{chain_key}/rpc")
    def clear_rpc(chain_key: str):
        if chain_key not in CHAINS:
            raise HTTPException(404, "Unsupported chain")
        db.clear_rpc_override(chain_key)
        scheduler.wake()
        return {"ok": True}

    @app.put("/api/envio/settings")
    def save_envio_settings(payload: EnvioSettingsInput):
        try:
            db.set_envio_settings(api_key=payload.api_key, rpm_limit=payload.rpm_limit)
            scheduler.wake()
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(400, sanitize_error(exc)) from None

    @app.delete("/api/envio/key")
    def clear_envio_key():
        db.clear_envio_key()
        scheduler.wake()
        return {"ok": True}

    @app.post("/api/pools/discover")
    def discover_pool(payload: PoolDiscoveryInput):
        try:
            return discover_pool_assets(
                db,
                chain_key=payload.chain_key,
                pool_address=payload.address,
            )
        except Exception as exc:
            raise HTTPException(400, sanitize_error(exc)) from None

    @app.post("/api/pools")
    def add_pool(payload: PoolInput):
        try:
            preview = validate_pool_input(
                db,
                chain_key=payload.chain_key,
                pool_address=payload.address,
                token_addresses=[asset.address for asset in payload.assets],
                start_block=payload.start_block,
            )
            normalized_assets = _normalize_assets(payload.assets, preview["assets"])
            pool_id = _unique_pool_id(db, payload.chain_key, payload.name, preview["pool_address"])
            db.create_pool(
                {
                    "id": pool_id,
                    "name": payload.name.strip(),
                    "chain_key": payload.chain_key,
                    "address": preview["pool_address"],
                    "start_block": preview["start_block"],
                    "contract_type": "two_asset_transfers",
                    "mtm_mode": payload.mtm_mode,
                },
                normalized_assets,
            )
            scheduler.wake()
            return {"ok": True, "pool_id": pool_id}
        except Exception as exc:
            raise HTTPException(400, sanitize_error(exc)) from None

    @app.put("/api/pools/{pool_id}")
    def update_pool(pool_id: str, payload: PoolUpdateInput):
        pool = db.get_pool(pool_id)
        if not pool:
            raise HTTPException(404, "Pool not found")
        if len(payload.assets) != 2:
            raise HTTPException(400, "Exactly two assets are required")
        try:
            saved_assets = [AssetInput(address=a['address'], price_kind=a['price_kind'], market=a.get('market'),
                                       market_type=a.get('market_type') or 'spot', fixed_price=a.get('fixed_price'))
                            for a in pool['assets']]
            if payload.start_block == pool['start_block'] and payload.assets == saved_assets:
                normalized_assets = pool['assets']  # Name/MTM-only edits need no RPC or market validation.
            else:
                preview = validate_pool_input(
                    db,
                    chain_key=pool["chain_key"],
                    pool_address=pool["address"],
                    token_addresses=[asset["address"] for asset in pool["assets"]],
                    start_block=payload.start_block,
                )
                detected = [{**old, **fresh} for old, fresh in zip(pool["assets"], preview["assets"], strict=True)]
                normalized_assets = _normalize_assets(payload.assets, detected)
            db.update_pool(pool_id, payload.name.strip(), payload.start_block, normalized_assets, mtm_mode=payload.mtm_mode)
            scheduler.wake()
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(400, sanitize_error(exc)) from None

    @app.post("/api/pools/{pool_id}/pause")
    def pause_pool(pool_id: str, payload: PauseInput):
        if not db.get_pool(pool_id):
            raise HTTPException(404, "Pool not found")
        db.set_paused(pool_id, payload.paused)
        scheduler.wake()
        return {"ok": True}

    @app.post("/api/pools/{pool_id}/rebuild")
    def rebuild_pool(pool_id: str):
        if not db.get_pool(pool_id):
            raise HTTPException(404, "Pool not found")
        db.request_rebuild(pool_id)
        scheduler.wake()
        return {"ok": True}

    @app.delete("/api/pools/{pool_id}")
    def remove_pool(pool_id: str):
        if not db.get_pool(pool_id):
            raise HTTPException(404, "Pool not found")
        db.delete_pool(pool_id)
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
    return app


# ==== Validation and serialization ====

def _mutation_rejection(request: Request, csrf_token: str) -> JSONResponse | None:
    if not request.url.path.startswith("/api/") or request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if request.headers.get("X-CSRF-Token") != csrf_token:
        return JSONResponse({"detail": "Invalid CSRF token"}, status_code=403)
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.headers.get("Host"):
        return JSONResponse({"detail": "Cross-origin mutation rejected"}, status_code=403)
    return None


def _normalize_assets(inputs: list[AssetInput], detected: list[dict]) -> list[dict]:
    if len(inputs) != 2 or len(detected) != 2:
        raise ValueError("Exactly two assets are required")
    normalized = []
    for requested, metadata in zip(inputs, detected, strict=True):
        kind = requested.price_kind.strip().lower()
        row = {**metadata, "price_kind": kind, "exchange": None, "market": None, "market_type": None,
               "fixed_price": None, "quote_asset": None, "quote_verified": False, "quote_error": None}
        if kind == "fixed":
            if requested.fixed_price is None or not math.isfinite(requested.fixed_price) or requested.fixed_price <= 0:
                raise ValueError(f"{metadata['symbol']} fixed price must be positive")
            row["fixed_price"] = float(requested.fixed_price)
        elif kind == "binance":
            market = str(requested.market or "").strip().upper()
            market_type = requested.market_type.strip().lower()
            if not market or market_type not in {"spot", "futures"}:
                raise ValueError(f"{metadata['symbol']} requires a market and spot/futures type")
            row["quote_asset"] = validate_market(kind, market, market_type)
            row["quote_verified"] = True
            row.update({"exchange": kind, "market": market, "market_type": market_type})
        elif (kind == metadata.get("price_kind") == "bybit"
              and requested.market == metadata.get("market") and requested.market_type == metadata.get("market_type")):
            row = dict(metadata)  # Name-only edits preserve retired mappings and history.
        elif kind != "none":
            raise ValueError("Price kind must be none, fixed, or binance")
        normalized.append(row)
    return normalized


def _unique_pool_id(db: Database, chain_key: str, name: str, address: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "pool"
    base = f"{chain_key}-{slug}-{address[2:10]}"
    candidate = base
    suffix = 2
    while db.get_pool(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _json_pool(pool: dict) -> dict:
    return {
        **{key: value for key, value in pool.items() if key != "assets"},
        "created_at": str(pool.get("created_at")) if pool.get("created_at") else None,
        "updated_at": str(pool.get("updated_at")) if pool.get("updated_at") else None,
        "last_success_at": str(pool.get("last_success_at")) if pool.get("last_success_at") else None,
        "assets": [
            {**asset, "fixed_price": float(asset["fixed_price"]) if asset.get("fixed_price") is not None and math.isfinite(asset["fixed_price"]) else None}
            for asset in pool["assets"]
        ],
    }




# ==== Server entrypoint ====

def run() -> None:
    host = os.getenv("OPEN_METRIC_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("OPEN_METRIC_DASHBOARD_PORT", "8052"))
    logger.info("dashboard starting url=http://%s:%s", host, port)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
