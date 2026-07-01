"""Entry point: continuous multi-strategy trading loop.

Every POLL_INTERVAL_SECONDS the loop wakes up and, per instrument:
  1. Skips equities when the market is closed (crypto is always checked).
  2. Manages the hard/trailing stop of any open position using the latest
     trade price (fast, no bars call).
  3. If that instrument's candle interval has elapsed since its last full
     evaluation, fetches fresh bars and asks the relevant strategy module
     for a signal, then sizes/opens/closes/flips positions accordingly.

Run with: python -m bot.main
"""
import datetime as dt
import logging
import time

import config
from bot.broker import AlpacaBroker, BrokerError
from bot.portfolio import Portfolio
from bot.risk_manager import RiskManager
from bot.strategies import mean_reversion, momentum_breakout, trend_following
from bot.strategies.base import OPEN_LONG, OPEN_SHORT, CLOSE, HOLD
from bot import indicators

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(config.BOT_LOG_FILE)],
)
logger = logging.getLogger("bot.main")

STRATEGY_EVALUATORS = {
    config.MEAN_REVERSION: mean_reversion.evaluate,
    config.MOMENTUM_BREAKOUT: momentum_breakout.evaluate,
    config.TREND_FOLLOWING: trend_following.evaluate,
}

_UNIT_SECONDS = {"Minute": 60, "Hour": 3600, "Day": 86400}


def timeframe_seconds(instrument: config.InstrumentConfig) -> int:
    return instrument.timeframe_amount * _UNIT_SECONDS[instrument.timeframe_unit]


class TradingBot:
    def __init__(self):
        self.broker = AlpacaBroker()
        self.portfolio = Portfolio(self.broker)
        self.risk_manager = RiskManager(self.broker, self.portfolio)
        self._last_checked: dict = {}
        self._market_closed_logged: set = set()

    def start(self):
        logger.info("Starting trading bot for instruments: %s", ", ".join(config.INSTRUMENTS))
        self.portfolio.sync_from_broker(config.INSTRUMENTS)
        self._run_forever()

    def _run_forever(self):
        while True:
            loop_start = time.monotonic()
            try:
                self._tick()
            except Exception:
                logger.exception("Unhandled error in main loop tick; continuing.")
            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.0, config.POLL_INTERVAL_SECONDS - elapsed))

    def _tick(self):
        self.portfolio.maybe_roll_day()
        for symbol, instrument in config.INSTRUMENTS.items():
            try:
                self._process_instrument(symbol, instrument)
            except BrokerError as exc:
                logger.error("Broker error processing %s: %s", symbol, exc)
            except Exception:
                logger.exception("Error processing %s; skipping this tick.", symbol)

    def _process_instrument(self, symbol: str, instrument: config.InstrumentConfig):
        if not self._market_available(symbol, instrument):
            return

        self._manage_open_position_stops(symbol, instrument)

        now = dt.datetime.now(dt.timezone.utc)
        last = self._last_checked.get(symbol)
        due = last is None or (now - last).total_seconds() >= timeframe_seconds(instrument)
        if not due:
            return
        self._last_checked[symbol] = now
        self._evaluate_and_trade(symbol, instrument)

    def _market_available(self, symbol: str, instrument: config.InstrumentConfig) -> bool:
        if instrument.asset_class == "crypto":
            return True
        is_open = self.broker.is_market_open("equity")
        if not is_open and symbol not in self._market_closed_logged:
            logger.info("Market closed; skipping equity instrument %s until it reopens.", symbol)
            self._market_closed_logged.add(symbol)
        if is_open:
            self._market_closed_logged.discard(symbol)
        return is_open

    # -- Stop management (every tick, using latest trade price) ---------- #

    def _manage_open_position_stops(self, symbol: str, instrument: config.InstrumentConfig):
        position = self.portfolio.get_position(symbol)
        if position is None:
            return

        price = self.broker.get_latest_trade_price(instrument)
        if price is None:
            return

        self.portfolio.update_extreme(symbol, price)

        if position.atr <= 0:
            return  # ATR not sampled yet (e.g. adopted from broker on startup)

        trailing_stop = self.risk_manager.trailing_stop_price(
            position.extreme_price, position.atr, position.trail_mult, position.side
        )
        effective_stop = self.risk_manager.effective_stop_price(
            position.side, position.hard_stop, trailing_stop
        )

        hit = (
            (position.side == "long" and price <= effective_stop)
            or (position.side == "short" and price >= effective_stop)
        )
        if hit:
            logger.info("Stop hit for %s: price=%.4f effective_stop=%.4f", symbol, price, effective_stop)
            self._close(symbol, instrument, price)

    # -- Signal evaluation (once per candle interval) --------------------- #

    def _evaluate_and_trade(self, symbol: str, instrument: config.InstrumentConfig):
        df = self.broker.get_bars(instrument, limit=max(300, instrument.params.get("slow_ema", 0) + 20))
        if df.empty:
            logger.warning("No bar data returned for %s; skipping.", symbol)
            return

        atr_series = indicators.atr(df, config.ATR_PERIOD)
        atr_last = atr_series.iloc[-1]
        price = df["close"].iloc[-1]

        if self.portfolio.has_position(symbol):
            self.portfolio.update_atr(symbol, atr_last)

        position = self.portfolio.get_position(symbol)
        current_side = position.side if position else None

        evaluator = STRATEGY_EVALUATORS[instrument.strategy]
        signal = evaluator(df, instrument, current_side)

        if signal.action == HOLD:
            return

        exec_price = signal.price if signal.price is not None else price

        if signal.action == CLOSE:
            if position is not None:
                self._close(symbol, instrument, exec_price)
            return

        desired_side = "long" if signal.action == OPEN_LONG else "short"

        if position is not None and position.side == desired_side:
            return  # already positioned correctly

        if position is not None and position.side != desired_side:
            self._close(symbol, instrument, exec_price)

        if desired_side == "long" and self.risk_manager.correlation_filter_blocks_long(symbol):
            return

        self._open(symbol, instrument, desired_side, exec_price, atr_last)

    # -- Order execution --------------------------------------------------- #

    def _open(self, symbol: str, instrument: config.InstrumentConfig, side: str, price: float, atr: float):
        qty = self.risk_manager.position_size(symbol, atr, price, instrument.asset_class)
        if qty <= 0:
            logger.info("Skipping %s %s: computed position size is zero.", symbol, side)
            return

        order_side = "buy" if side == "long" else "sell"
        try:
            self.broker.submit_market_order(instrument.symbol, qty, order_side, instrument.asset_class)
        except BrokerError as exc:
            logger.error("Failed to submit %s order for %s: %s", side, symbol, exc)
            return

        hard_stop = self.risk_manager.hard_stop_price(price, atr, side)
        trail_mult = instrument.params.get("trail_atr_mult", config.HARD_STOP_ATR_MULT)
        self.portfolio.open_position(symbol, side, qty, price, hard_stop, trail_mult, instrument.strategy, atr)

    def _close(self, symbol: str, instrument: config.InstrumentConfig, price: float):
        if not self.portfolio.has_position(symbol):
            return
        try:
            self.broker.close_position(instrument.symbol)
        except BrokerError as exc:
            logger.error("Failed to close position for %s on broker: %s", symbol, exc)
            return
        self.portfolio.close_position(symbol, price)


def main():
    bot = TradingBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        logger.info("Shutdown requested; flushing daily P&L and exiting.")
        bot.portfolio.flush_daily_pnl()


if __name__ == "__main__":
    main()
