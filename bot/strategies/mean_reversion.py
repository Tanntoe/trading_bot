"""Strategy 1: Mean Reversion (SPY, QQQ).

15-minute candles. A 20-period SMA and rolling std-dev define a band,
computed from the bars prior to the current one (mirroring the
momentum-breakout strategy's convention) so the current bar's own move
doesn't widen the very band it's being tested against - without this, an
extreme bar inflates its own std-dev and can partially mask itself,
producing jumpy entries/exits. Go long when price is more than
`entry_std_dev` standard deviations below the mean (expecting a revert
up), go short when it's the same distance above the mean. Exit when
price returns to the mean.

Optional regime filter (`trend_ema_period`): a longer-period EMA used to
tell a genuine dip-in-an-uptrend from the start of a real breakdown (and
the mirror case for shorts). Backtesting against real data showed that
without this, "buy the dip" kept getting run over by price continuing to
trend down instead of reverting - a long is only taken while price is
still above this longer-term trend line, a short only while price is
still below it.
"""
import logging

import pandas as pd

from bot import indicators
from bot.strategies.base import Signal, OPEN_LONG, OPEN_SHORT, CLOSE, HOLD

logger = logging.getLogger(__name__)


def evaluate(df: pd.DataFrame, instrument, current_side: str = None) -> Signal:
    lookback = instrument.params["lookback"]
    entry_std_dev = instrument.params["entry_std_dev"]
    trend_ema_period = instrument.params.get("trend_ema_period")

    if len(df) < lookback + 1:
        return Signal(HOLD, reason="not enough bars")

    # Shift by 1 so the band is computed from the prior N bars, excluding
    # the bar we are currently evaluating (see module docstring).
    mean = indicators.sma(df["close"], lookback).shift(1)
    std = indicators.rolling_std(df["close"], lookback).shift(1)

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

    trend_allows_long, trend_allows_short = True, True
    if trend_ema_period and len(df) >= trend_ema_period:
        trend_ema_last = indicators.ema(df["close"], trend_ema_period).iloc[-1]
        if not pd.isna(trend_ema_last):
            trend_allows_long = price >= trend_ema_last
            trend_allows_short = price <= trend_ema_last

    if price <= lower_band and trend_allows_long:
        return Signal(OPEN_LONG, reason=f"price {price:.2f} <= lower band {lower_band:.2f}, above trend EMA", price=price)
    if price >= upper_band and trend_allows_short:
        return Signal(OPEN_SHORT, reason=f"price {price:.2f} >= upper band {upper_band:.2f}, below trend EMA", price=price)

    return Signal(HOLD, price=price)
