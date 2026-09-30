from dataclasses import asdict, dataclass

from .rpc import redact_rpc_url


@dataclass(frozen=True)
class Chain:
    key: str
    name: str
    chain_id: int
    native_symbol: str
    explorer_tx_url: str
    explorer_address_url: str
    public_rpcs: tuple[str, ...]
    confirmations: int
    overlap_blocks: int
    seconds_per_block: float


CHAINS = {
    chain.key: chain
    for chain in (
        Chain(
            key="ethereum",
            name="Ethereum",
            chain_id=1,
            native_symbol="ETH",
            explorer_tx_url="https://etherscan.io/tx/{tx_hash}",
            explorer_address_url="https://etherscan.io/address/{address}",
            public_rpcs=(
                "https://eth-mainnet.public.blastapi.io",
                "https://ethereum-rpc.publicnode.com",
                "https://eth.llamarpc.com",
                "https://1rpc.io/eth",
            ),
            confirmations=16,
            overlap_blocks=24,
            seconds_per_block=12.0,
        ),
        Chain(
            key="base",
            name="Base",
            chain_id=8453,
            native_symbol="ETH",
            explorer_tx_url="https://basescan.org/tx/{tx_hash}",
            explorer_address_url="https://basescan.org/address/{address}",
            public_rpcs=(
                "https://mainnet.base.org",
                "https://base.gateway.tenderly.co",
            ),
            confirmations=20,
            overlap_blocks=40,
            seconds_per_block=2.0,
        ),
        Chain(
            key="arbitrum",
            name="Arbitrum One",
            chain_id=42161,
            native_symbol="ETH",
            explorer_tx_url="https://arbiscan.io/tx/{tx_hash}",
            explorer_address_url="https://arbiscan.io/address/{address}",
            public_rpcs=(
                "https://arbitrum-one-rpc.publicnode.com",
                "https://arb1.arbitrum.io/rpc",
            ),
            confirmations=64,
            overlap_blocks=128,
            seconds_per_block=0.25,
        ),
        Chain(
            key="robinhood",
            name="Robinhood Chain",
            chain_id=4663,
            native_symbol="ETH",
            explorer_tx_url="https://robin.etherscan.io/tx/{tx_hash}",
            explorer_address_url="https://robin.etherscan.io/address/{address}",
            public_rpcs=("https://rpc.mainnet.chain.robinhood.com",),
            confirmations=64,
            overlap_blocks=128,
            seconds_per_block=0.25,
        ),
        Chain(
            key="bsc",
            name="BNB Smart Chain",
            chain_id=56,
            native_symbol="BNB",
            explorer_tx_url="https://bscscan.com/tx/{tx_hash}",
            explorer_address_url="https://bscscan.com/address/{address}",
            public_rpcs=("https://bsc-rpc.publicnode.com",),
            confirmations=20,
            overlap_blocks=40,
            seconds_per_block=3.0,
        ),
        Chain(
            key="polygon",
            name="Polygon PoS",
            chain_id=137,
            native_symbol="POL",
            explorer_tx_url="https://polygonscan.com/tx/{tx_hash}",
            explorer_address_url="https://polygonscan.com/address/{address}",
            public_rpcs=(
                "https://polygon-bor-rpc.publicnode.com",
                "https://polygon-rpc.com",
            ),
            confirmations=64,
            overlap_blocks=128,
            seconds_per_block=2.0,
        ),
        Chain(
            key="avalanche",
            name="Avalanche C-Chain",
            chain_id=43114,
            native_symbol="AVAX",
            explorer_tx_url="https://snowtrace.io/tx/{tx_hash}",
            explorer_address_url="https://snowtrace.io/address/{address}",
            public_rpcs=(
                "https://avalanche-c-chain-rpc.publicnode.com",
                "https://api.avax.network/ext/bc/C/rpc",
            ),
            confirmations=12,
            overlap_blocks=24,
            seconds_per_block=1.0,
        ),
    )
}


def public_chain_rows(overrides: dict[str, str] | None = None) -> list[dict]:
    overrides = overrides or {}
    rows = []
    for chain in CHAINS.values():
        row = asdict(chain)
        row["public_rpcs"] = list(chain.public_rpcs)
        row["has_custom_rpc"] = chain.key in overrides
        row["custom_rpc_display"] = redact_rpc_url(overrides[chain.key]) if chain.key in overrides else None
        rows.append(row)
    return rows
