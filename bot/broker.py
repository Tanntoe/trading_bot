"""Thin wrapper around alpaca_trade_api.REST.

Centralizes all direct calls to the Alpaca API so the rest of the bot never
talks to `alpaca_trade_api` directly. This is where connection errors,
rate limits and market-closed states are handled and retried.
"""
import logging
import time
from typing import List, Optional

import pandas as pd
import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import APIError, TimeFrame, TimeFrameUnit

import config

logger = logging.getLogger(__name__)

_UNIT_MAP = {
    "Minute": TimeFrameUnit.Minute,
    "Hour": TimeFrameUnit.Hour,
    "Day": TimeFrameUnit.Day,
}

# Alpaca's crypto symbol convention differs from equities (slash separator).
# The config uses the human-readable "BTC/USD" everywhere; the REST calls
# below pass it through as-is since alpaca-trade-api's crypto endpoints
# accept the slash form.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


class BrokerError(Exception):
    """Raised when a broker call fails after all retries are exhausted."""


class AlpacaBroker:
    def __init__(self):
        self.api = tradeapi.REST(
            key_id=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            base_url=config.ALPACA_BASE_URL,
            api_version=config.ALPACA_API_VERSION,
        )

    def _with_retry(self, description: str, fn, *args, **kwargs):
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except APIError as exc:
                last_exc = exc
                logger.warning("Alpaca API error during %s (attempt %d/%d): %s",
                                description, attempt, MAX_RETRIES, exc)
            except Exception as exc:  # network disconnects, timeouts, etc.
                last_exc = exc
                logger.warning("Connection error during %s (attempt %d/%d): %s",
                                description, attempt, MAX_RETRIES, exc)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        raise BrokerError(f"{description} failed after {MAX_RETRIES} attempts: {last_exc}")

    def timeframe(self, amount: int, unit: str) -> TimeFrame:
        return TimeFrame(amount, _UNIT_MAP[unit])

    def get_bars(self, instrument: "config.InstrumentConfig", limit: int = 300) -> pd.DataFrame:
        """Returns an OHLCV dataframe (columns lower-cased) for the instrument's
        configured timeframe, newest bar last."""
        tf = self.timeframe(instrument.timeframe_amount, instrument.timeframe_unit)

        if instrument.asset_class == "crypto":
            bars = self._with_retry(
                f"get_crypto_bars({instrument.symbol})",
                self.api.get_crypto_bars,
                instrument.symbol,
                tf,
                limit=limit,
            )
        else:
            bars = self._with_retry(
                f"get_bars({instrument.symbol})",
                self.api.get_bars,
                instrument.symbol,
                tf,
                limit=limit,
                adjustment="raw",
            )
        df = bars.df
        if df is None or df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        # Multi-symbol responses (SIP feed) sometimes include a 'symbol' column;
        # normalize to a single-symbol OHLCV frame.
        if "symbol" in df.columns:
            df = df[df["symbol"] == instrument.symbol]
        return df[["open", "high", "low", "close", "volume"]]

    def get_account_equity(self) -> float:
        account = self._with_retry("get_account", self.api.get_account)
        return float(account.equity)

    def list_positions(self) -> List:
        return self._with_retry("list_positions", self.api.list_positions)

    def get_position(self, symbol: str):
        try:
            return self.api.get_position(symbol)
        except APIError as exc:
            if "position does not exist" in str(exc).lower() or "404" in str(exc):
                return None
            raise

    def submit_market_order(self, symbol: str, qty: float, side: str, asset_class: str):
        tif = "gtc" if asset_class == "crypto" else "day"
        return self._with_retry(
            f"submit_order({symbol},{side},{qty})",
            self.api.submit_order,
            symbol=symbol,
            qty=qty,
            side=side,
            type="market",
            time_in_force=tif,
        )

    def close_position(self, symbol: str):
        return self._with_retry(f"close_position({symbol})", self.api.close_position, symbol)

    def is_market_open(self, asset_class: str) -> bool:
        """Crypto trades 24/7; equities respect the exchange calendar."""
        if asset_class == "crypto":
            return True
        clock = self._with_retry("get_clock", self.api.get_clock)
        return bool(clock.is_open)

    def get_latest_trade_price(self, instrument: "config.InstrumentConfig") -> Optional[float]:
        try:
            if instrument.asset_class == "crypto":
                trade = self._with_retry(
                    f"get_latest_crypto_trade({instrument.symbol})",
                    self.api.get_latest_crypto_trade,
                    instrument.symbol,
                )
            else:
                trade = self._with_retry(
                    f"get_latest_trade({instrument.symbol})",
                    self.api.get_latest_trade,
                    instrument.symbol,
                )
            return float(trade.price)
        except BrokerError:
            logger.warning("Could not fetch latest trade price for %s; falling back to last bar.",
                            instrument.symbol)
            return None
