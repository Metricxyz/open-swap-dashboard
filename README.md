# Open Metric Dashboard

A small self-hosted dashboard for monitoring two-token EVM liquidity pools. It indexes ERC-20 transfers, reconstructs swaps and balances, and prices them against Binance to show volume, PnL, TVL, APR, and recent swaps.

Everything runs as one Python process with a local DuckDB database. Supported chains: Ethereum, Base, Arbitrum One, Robinhood Chain, BNB Smart Chain, Polygon PoS, and Avalanche C-Chain.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- An [Envio HyperSync API key](https://envio.dev/app/api-tokens). You need one, because all transfer data comes from HyperSync. The free tier is enough.

## Getting started

```bash
uv python install 3.13
uv sync
uv run python main.py
```

Open <http://127.0.0.1:8052>. The web UI and the indexer both run in this process. There is no frontend build step.

1. Open **Network Configuration** in the sidebar and paste your Envio API key.
2. Click **+ Add** in the pool list, and paste the pool address. The chain and both tokens are detected automatically.
3. Set a price source for each token:
   - **Fixed USD** for stablecoins, e.g. `1` for USDC
   - **Binance** with a USDT- or USDC-quoted market, e.g. `ETHUSDT`
   - **None** if the token has no price source
4. Save. Syncing starts in the background. If you leave the start block empty, the dashboard backfills about three days.

Every chain has default public RPC endpoints. If one is slow or rate-limited, set your own in **Network Configuration**.

## Configuration

The database, logs, and cached market data go in `data/`. Settings are optional environment variables:

| Variable | Default |
| --- | --- |
| `OPEN_METRIC_DASHBOARD_HOST` | `127.0.0.1` |
| `OPEN_METRIC_DASHBOARD_PORT` | `8052` |
| `OPEN_METRIC_DASHBOARD_DATA_DIR` | `./data` |
| `OPEN_METRIC_DASHBOARD_LOG_LEVEL` | `INFO` |

The dashboard has no authentication. If you expose it beyond localhost, put it behind a reverse proxy with auth and TLS. The data directory also stores your Envio key.

## Development

```bash
uv run pytest
node --test web/tests/*.test.mjs
```

## License

MIT. See [LICENSE](LICENSE).
