from dataclasses import dataclass
from typing import List, cast
import math # for isnan

from fractal.core.base import BaseStrategy, BaseStrategyParams, Action, ActionToTake, NamedEntity
from fractal.core.base.entity import EntityException
from fractal.core.entities import (
    UniswapV2LPEntity, UniswapV2LPConfig,
    HyperliquidEntity
)
from fractal.core.entities.uniswap_v2_lp import UniswapV2LPGlobalState, UniswapV2LPInternalState

@dataclass
class UniV2HLParams(BaseStrategyParams):
    INITIAL_NOTIONAL: float
    HL_LEVERAGE: float
    REBALANCE_THRESHOLD_PCT: float = 0.10

class UniV2HyperLiquidHedge(BaseStrategy):
    _did_boot = False
    _params: UniV2HLParams 

    def __init__(self, *args, params: UniV2HLParams, debug: bool = False, **kwargs):
        super().__init__(params=params, debug=debug, *args, **kwargs)
        self._params = params 

    def set_up(self):
        # USDC(6) / WETH(18) pool, 0.3 % fees
        uni_cfg = UniswapV2LPConfig(
            fees_rate=0.003,
            token0_decimals=6,   # USDC
            token1_decimals=18,  # WETH
            trading_fee=0.003
        )
        self.register_entity(NamedEntity('LP', UniswapV2LPEntity(uni_cfg)))
        self.register_entity(NamedEntity('HEDGE', HyperliquidEntity()))
        self._price_on_boot = None

    def predict(self) -> List[ActionToTake]:
        actions: List[ActionToTake] = []
        
        lp_entity = cast(UniswapV2LPEntity, self.get_entity('LP'))
        hedge_entity = cast(HyperliquidEntity, self.get_entity('HEDGE'))

        if lp_entity.global_state is None or lp_entity.internal_state is None:
            self._debug("LP entity states are not initialized. Skipping.")
            return []
        
        lp_global_state = cast(UniswapV2LPGlobalState, lp_entity.global_state)
        lp_internal_state = cast(UniswapV2LPInternalState, lp_entity.internal_state)
        
        current_eth_price = lp_global_state.price

        if not self._did_boot:
            if current_eth_price is None or current_eth_price <= 0 or math.isnan(current_eth_price):
                self._debug(f"Initial price ({current_eth_price}) not available or invalid. Skipping boot.")
                return []

            leverage = self._params.HL_LEVERAGE
            initial_notional = self._params.INITIAL_NOTIONAL
            initial_lp_deposit_notional = initial_notional / (1 + 1 / (2 * leverage))
            initial_eth_value_in_lp = initial_lp_deposit_notional / 2             
            margin_for_initial_short = initial_eth_value_in_lp / leverage
            initial_eth_amount_to_short = initial_eth_value_in_lp / current_eth_price

            actions.extend([
                ActionToTake('LP', Action('deposit', {'amount_in_notional': initial_lp_deposit_notional})),
                ActionToTake('LP', Action('open_position', {'amount_in_notional': initial_lp_deposit_notional})),
                ActionToTake('HEDGE', Action('deposit', {'amount_in_notional': margin_for_initial_short})),
                ActionToTake('HEDGE', Action('open_position', {'amount_in_product': -initial_eth_amount_to_short})),
            ])
            self._price_on_boot = current_eth_price
            self._did_boot = True
            return actions

        # Check for HEDGE liquidation if already booted
        if hedge_entity.balance == 0 and lp_internal_state.token1_amount > 0:
            self._debug(f"CRITICAL: HEDGE account balance is zero ({hedge_entity.balance:.8f}) while LP holds assets"
                        f" ({lp_internal_state.token1_amount:.8f}). "
                        f"This may indicate the short position was liquidated.")
            
            # Raise exception when liquidation is detected (sanity check)
            # raise EntityException(
            #     f"Hedge position likely liquidated: HEDGE balance ({hedge_entity.balance:.8f}) is zero/near-zero "
            #     f"while LP token1 amount ({lp_internal_state.token1_amount:.8f}) > 0."
            # )

        # Rebalance short logic
        if current_eth_price is None or current_eth_price <= 0 or math.isnan(current_eth_price):
            self._debug(f"Invalid price ({current_eth_price}) for rebalancing. Skipping.")
            return []

        lp_eth_amount = lp_internal_state.token1_amount
        current_short_size_abs = abs(hedge_entity.size)

        if lp_eth_amount <= 1e-9: 
            self._debug(f"LP ETH amount ({lp_eth_amount}) is too small. Skipping rebalance.")
            return []

        diff_in_eth_amount = lp_eth_amount - current_short_size_abs
        
        if abs(diff_in_eth_amount) / lp_eth_amount > self._params.REBALANCE_THRESHOLD_PCT:
            self._debug(f"Rebalancing triggered. LP ETH: {lp_eth_amount}, Short ETH: {current_short_size_abs}, Diff: {diff_in_eth_amount}")
            
            desired_total_short_product = -lp_eth_amount
            amount_to_adjust_product = desired_total_short_product - hedge_entity.size

            if abs(amount_to_adjust_product * current_eth_price) < 0.01: # Minimal notional value for adjustment
                 self._debug(f"Adjustment amount too small ({amount_to_adjust_product} ETH, notional: {abs(amount_to_adjust_product * current_eth_price)}). Skipping.")
                 return actions

            if amount_to_adjust_product < 0: 
                notional_value_of_adjustment = abs(amount_to_adjust_product) * current_eth_price
                margin_needed_for_adjustment = notional_value_of_adjustment / self._params.HL_LEVERAGE
                
                available_margin = hedge_entity.balance 
                if available_margin < margin_needed_for_adjustment:
                    self._debug(f"Insufficient margin to increase short. Need: {margin_needed_for_adjustment}, Have: {available_margin}. Consider adding more capital to HEDGE or reducing leverage.")
                    raise ValueError(f"Insufficient margin for HEDGE. Needed: {margin_needed_for_adjustment}, Available: {available_margin}")
                else:
                    self._debug(f"Sufficient margin. Need: {margin_needed_for_adjustment}, Have: {available_margin}. Increasing short by {amount_to_adjust_product} ETH")
                    actions.append(ActionToTake('HEDGE', Action('open_position', {'amount_in_product': amount_to_adjust_product})))
            
            elif amount_to_adjust_product > 0: 
                self._debug(f"Decreasing short by {amount_to_adjust_product} ETH")
                actions.append(ActionToTake('HEDGE', Action('open_position', {'amount_in_product': amount_to_adjust_product})))
            else:
                self._debug("No adjustment needed after calculation (amount_to_adjust_product is zero).")
        
        return actions
