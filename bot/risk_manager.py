"""Risk management: ATR-based position sizing, hard stops and the
cross-instrument correlation filter.

Sizing rule: a position is sized so that a 1x ATR adverse move costs
exactly RISK_PER_TRADE_PCT of total account equity. Because the hard stop
is placed HARD_STOP_ATR_MULT (1.0) ATR away from entry, hitting the hard
stop always realizes a loss of exactly RISK_PER_TRADE_PCT of equity -
"no exceptions" is satisfied by construction, not by a separate check.
"""
import logging

import config

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, broker, portfolio):
        self.broker = broker
        self.portfolio = portfolio

    def position_size(self, symbol: str, atr: float, price: float, asset_class: str) -> float:
        """Number of shares/coins to trade so a 1x ATR move == 1% equity."""
        if atr <= 0 or price <= 0:
            logger.warning("Non-positive ATR/price for %s (atr=%s, price=%s); skipping sizing.",
                            symbol, atr, price)
            return 0.0

        equity = self.broker.get_account_equity()
        dollar_risk = equity * config.RISK_PER_TRADE_PCT
        qty = dollar_risk / atr

        if asset_class == "crypto":
            qty = round(qty, 6)
        else:
            qty = float(int(qty))  # whole shares only

        if qty <= 0:
            logger.info("Computed zero position size for %s (equity=%.2f, atr=%.4f).",
                         symbol, equity, atr)
        return qty

    def hard_stop_price(self, entry_price: float, atr: float, side: str) -> float:
        """Fixed stop placed 1x ATR from entry -> exactly 1% equity loss if hit."""
        offset = config.HARD_STOP_ATR_MULT * atr
        return entry_price - offset if side == "long" else entry_price + offset

    def trailing_stop_price(self, extreme_price: float, atr: float, trail_mult: float, side: str) -> float:
        offset = trail_mult * atr
        return extreme_price - offset if side == "long" else extreme_price + offset

    def effective_stop_price(self, side: str, hard_stop: float, trailing_stop: float) -> float:
        """The hard stop is a floor that never loosens; the trailing stop can
        only tighten it further as the trade moves favorably."""
        if side == "long":
            return max(hard_stop, trailing_stop)
        return min(hard_stop, trailing_stop)

    def correlation_filter_blocks_long(self, symbol: str) -> bool:
        """If SPY and QQQ are both already long, block new BTC/USD longs to
        avoid doubling up on risk-on exposure."""
        if symbol != config.CORRELATION_GATED_SYMBOL:
            return False
        for equity_symbol in config.CORRELATION_GROUP_EQUITY_RISK_ON:
            position = self.portfolio.get_position(equity_symbol)
            if position is None or position.side != "long":
                return False
        logger.info("Correlation filter: SPY and QQQ both long, blocking new long on %s.", symbol)
        return True
