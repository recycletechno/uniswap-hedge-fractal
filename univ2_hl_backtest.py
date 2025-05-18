from datetime import datetime, UTC
import os
from fractal.core.base import Observation
from fractal.loaders import HyperliquidFundingRatesLoader
from fractal.loaders.thegraph.uniswap_v2.uniswap_v2_pool import EthereumUniswapV2PoolDataLoader
from fractal.loaders.binance import BinanceHourPriceLoader
from fractal.loaders.structs import PriceHistory
from fractal.core.entities import UniswapV2LPGlobalState, HyperLiquidGlobalState
from fractal.loaders import LoaderType
from strategies.univ2_hl_hedge import UniV2HyperLiquidHedge, UniV2HLParams

POOL_ADDR = "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc"  # USDC-ETH 0.3 %
THEGRAPH_KEY = os.getenv("THEGRAPH_KEY")


def build_observations(start, end, api_key, with_run, interval):

    # Can not use HyperLiquidPerpsPricesLoader because it is not enough data there
    price: PriceHistory = BinanceHourPriceLoader(
        ticker="ETHUSDT", loader_type=LoaderType.CSV, start_time=start, end_time=end
    ).read(with_run=True)

    fund = HyperliquidFundingRatesLoader(ticker="ETH", loader_type=LoaderType.CSV, start_time=start, end_time=end).read(
        with_run=with_run
    )

    pool = EthereumUniswapV2PoolDataLoader(api_key, POOL_ADDR, 0.003).read(with_run=with_run)

    # Convert liquidity to int with 18 decimals
    pool["liquidity"] = (pool["liquidity"] * 10**18).astype(int)

    df = price.join(fund, how="inner").join(pool, how="inner").sort_index().dropna()

    obs = []
    for ts, row in df.iterrows():
        obs.append(
            Observation(
                timestamp=ts,
                states={
                    "LP": UniswapV2LPGlobalState(
                        price=row["price"],
                        tvl=row["tvl"],
                        volume=row["volume"],
                        fees=row["fees"],
                        liquidity=row["liquidity"],
                    ),
                    "HEDGE": HyperLiquidGlobalState(mark_price=row["price"], funding_rate=row["rate"]),
                },
            )
        )
    return obs


if __name__ == "__main__":
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2025, 5, 17, tzinfo=UTC)
    interval = "1h"
    with_run = False

    observations = build_observations(start, end, api_key=THEGRAPH_KEY, with_run=with_run, interval=interval)
    params = UniV2HLParams(INITIAL_NOTIONAL=1_000_000, HL_LEVERAGE=1.0, REBALANCE_THRESHOLD_PCT=0.1)
    strategy = UniV2HyperLiquidHedge(debug=True, params=params)

    result = strategy.run(observations)
    print(result.get_default_metrics())
    result.to_dataframe().to_csv("result_univ2_hl_hedge.csv")
