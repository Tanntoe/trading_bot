"""Strategy 3: Trend Following (GLD, USO).

4-hour candles. 50/200-period EMA crossover. Go long when the 50 EMA
crosses above the 200 EMA; exit/flip short when the 50 EMA crosses below
the 200 EMA. The 3x ATR trailing stop is managed centrally in
bot/main.py, same as the momentum strategy.
"""
import logging

import pandas as pd

from bot import indicators
from bot.strategies.base import Signal, OPEN_LONG, OPEN_SHORT, HOLD

logger = logging.getLogger(__name__)


def evaluate(df: pd.DataFrame, instrument, current_side: str = None) -> Signal:
    fast_period = instrument.params["fast_ema"]
    slow_period = instrument.params["slow_ema"]

    if len(df) < slow_period + 1:
        return Signal(HOLD, reason="not enough bars")

    fast = indicators.ema(df["close"], fast_period)
    slow = indicators.ema(df["close"], slow_period)
    price = df["close"].iloc[-1]

    if indicators.crossed_above(fast, slow) and current_side != "long":
        return Signal(OPEN_LONG, reason="50 EMA crossed above 200 EMA", price=price)

    if indicators.crossed_below(fast, slow) and current_side != "short":
        return Signal(OPEN_SHORT, reason="50 EMA crossed below 200 EMA", price=price)

    return Signal(HOLD, price=price)
