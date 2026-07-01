"""Shared technical indicator helpers used across strategy modules.

All functions take/return pandas Series (or a DataFrame with OHLCV columns
for ATR) and are pure functions with no side effects, so they are easy to
unit test independently of the broker/API layer.
"""
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rolling_std(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).std()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rolling_high(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).max()


def rolling_low(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).min()


def true_range(df: pd.DataFrame) -> pd.Series:
    """df must have columns: high, low, close."""
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's average true range."""
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def crossed_above(fast: pd.Series, slow: pd.Series) -> bool:
    """True if `fast` just crossed above `slow` on the most recent bar."""
    if len(fast) < 2 or len(slow) < 2:
        return False
    prev_fast, prev_slow = fast.iloc[-2], slow.iloc[-2]
    last_fast, last_slow = fast.iloc[-1], slow.iloc[-1]
    if pd.isna(prev_fast) or pd.isna(prev_slow) or pd.isna(last_fast) or pd.isna(last_slow):
        return False
    return prev_fast <= prev_slow and last_fast > last_slow


def crossed_below(fast: pd.Series, slow: pd.Series) -> bool:
    """True if `fast` just crossed below `slow` on the most recent bar."""
    if len(fast) < 2 or len(slow) < 2:
        return False
    prev_fast, prev_slow = fast.iloc[-2], slow.iloc[-2]
    last_fast, last_slow = fast.iloc[-1], slow.iloc[-1]
    if pd.isna(prev_fast) or pd.isna(prev_slow) or pd.isna(last_fast) or pd.isna(last_slow):
        return False
    return prev_fast >= prev_slow and last_fast < last_slow
