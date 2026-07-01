"""Strategy 1: Mean Reversion (SPY, QQQ).

15-minute candles. A 20-period SMA and rolling std-dev define a band.
Go long when price is more than `entry_std_dev` standard deviations below
the mean (expecting a revert up), go short when it's the same distance
above the mean. Exit when price returns to the mean.
"""
import logging

import pandas as pd

from bot import indicators
from bot.strategies.base import Signal, OPEN_LONG, OPEN_SHORT, CLOSE, HOLD

logger = logging.getLogger(__name__)


def evaluate(df: pd.DataFrame, instrument, current_side: str = None) -> Signal:
    lookback = instrument.params["lookback"]
    entry_std_dev = instrument.params["entry_std_dev"]

    if len(df) < lookback + 1:
        return Signal(HOLD, reason="not enough bars")

    mean = indicators.sma(df["close"], lookback)
    std = indicators.rolling_std(df["close"], lookback)

    price = df["close"].iloc[-1]
    mean_last = mean.iloc[-1]
    std_last = std.iloc[-1]

    if pd.isna(mean_last) or pd.isna(std_last) or std_last == 0:
        return Signal(HOLD, reason="indicators not ready", price=price)

    upper_band = mean_last + entry_std_dev * std_last
    lower_band = mean_last - entry_std_dev * std_last

    if current_side == "long":
        if price >= mean_last:
            return Signal(CLOSE, reason="reverted to mean", price=price)
        return Signal(HOLD, price=price)

    if current_side == "short":
        if price <= mean_last:
            return Signal(CLOSE, reason="reverted to mean", price=price)
        return Signal(HOLD, price=price)

    if price <= lower_band:
        return Signal(OPEN_LONG, reason=f"price {price:.2f} <= lower band {lower_band:.2f}", price=price)
    if price >= upper_band:
        return Signal(OPEN_SHORT, reason=f"price {price:.2f} >= upper band {upper_band:.2f}", price=price)

    return Signal(HOLD, price=price)
