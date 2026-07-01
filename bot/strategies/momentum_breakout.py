"""Strategy 2: Momentum Breakout (BTC/USD).

1-hour candles. Tracks the 20-period high/low (computed on the bars prior
to the current one). A close above the 20-period high on volume >= 1.5x
the 20-period average volume triggers a long entry; a close below the
20-period low on the same volume confirmation triggers a short entry (and
flips/exits an existing long). The 2x ATR trailing stop itself is managed
centrally in bot/main.py using risk_manager, since it applies to any open
position regardless of which bar triggered it.
"""
import logging

import pandas as pd

from bot import indicators
from bot.strategies.base import Signal, OPEN_LONG, OPEN_SHORT, CLOSE, HOLD

logger = logging.getLogger(__name__)


def evaluate(df: pd.DataFrame, instrument, current_side: str = None) -> Signal:
    lookback = instrument.params["lookback"]
    volume_mult = instrument.params["volume_mult"]

    if len(df) < lookback + 2:
        return Signal(HOLD, reason="not enough bars")

    # Shift by 1 so the breakout level is computed from the prior N bars,
    # excluding the bar we are currently evaluating.
    highest = indicators.rolling_high(df["high"], lookback).shift(1)
    lowest = indicators.rolling_low(df["low"], lookback).shift(1)
    avg_volume = indicators.sma(df["volume"], lookback).shift(1)

    price = df["close"].iloc[-1]
    volume = df["volume"].iloc[-1]
    high_level = highest.iloc[-1]
    low_level = lowest.iloc[-1]
    avg_vol_last = avg_volume.iloc[-1]

    if pd.isna(high_level) or pd.isna(low_level) or pd.isna(avg_vol_last) or avg_vol_last == 0:
        return Signal(HOLD, reason="indicators not ready", price=price)

    volume_confirmed = volume >= volume_mult * avg_vol_last
    breakout_up = price > high_level and volume_confirmed
    breakout_down = price < low_level and volume_confirmed

    if breakout_up and current_side != "long":
        return Signal(OPEN_LONG, reason=f"breakout above {high_level:.2f}, vol {volume:.0f}", price=price)

    if breakout_down and current_side != "short":
        if current_side == "long":
            return Signal(OPEN_SHORT, reason=f"breakdown below {low_level:.2f}, flipping short", price=price)
        return Signal(OPEN_SHORT, reason=f"breakdown below {low_level:.2f}, vol {volume:.0f}", price=price)

    return Signal(HOLD, price=price)
