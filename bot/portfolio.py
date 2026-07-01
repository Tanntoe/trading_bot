"""In-memory bookkeeping of open positions.

This is the bot's own view of state (entry price, stop levels, the
high/low watermark used for trailing stops, which strategy opened the
trade). It is reconciled against Alpaca's actual positions at startup so a
restart doesn't lose track of what's already open.
"""
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, Optional

import config
from bot import trade_logger

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    side: str            # "long" or "short"
    qty: float
    entry_price: float
    entry_time: dt.datetime
    hard_stop: float
    trail_mult: float
    strategy: str
    extreme_price: float  # highest price since entry (long) / lowest (short)
    atr: float = 0.0       # ATR sampled at the last strategy evaluation, used
                            # to compute the trailing stop between candle closes


class Portfolio:
    def __init__(self, broker):
        self.broker = broker
        self.positions: Dict[str, Position] = {}
        self._realized_pnl_today = 0.0
        self._pnl_date = dt.date.today()

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def open_position(self, symbol: str, side: str, qty: float, entry_price: float,
                       hard_stop: float, trail_mult: float, strategy: str, atr: float = 0.0) -> Position:
        position = Position(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            entry_time=dt.datetime.now(dt.timezone.utc),
            hard_stop=hard_stop,
            trail_mult=trail_mult,
            strategy=strategy,
            extreme_price=entry_price,
            atr=atr,
        )
        self.positions[symbol] = position
        logger.info("Opened %s %s qty=%s @ %.4f (hard_stop=%.4f)",
                    symbol, side, qty, entry_price, hard_stop)
        return position

    def update_extreme(self, symbol: str, price: float):
        position = self.positions.get(symbol)
        if position is None:
            return
        if position.side == "long":
            position.extreme_price = max(position.extreme_price, price)
        else:
            position.extreme_price = min(position.extreme_price, price)

    def update_atr(self, symbol: str, atr: float):
        position = self.positions.get(symbol)
        if position is not None and atr > 0:
            position.atr = atr

    def close_position(self, symbol: str, exit_price: float):
        position = self.positions.pop(symbol, None)
        if position is None:
            return None

        if position.side == "long":
            pnl = (exit_price - position.entry_price) * position.qty
        else:
            pnl = (position.entry_price - exit_price) * position.qty

        trade_logger.log_trade(
            instrument=symbol,
            direction=position.side,
            entry_price=position.entry_price,
            exit_price=exit_price,
            profit_loss=pnl,
            position_size=position.qty,
        )
        self._accumulate_daily_pnl(pnl)
        logger.info("Closed %s %s qty=%s entry=%.4f exit=%.4f pnl=%.2f",
                     symbol, position.side, position.qty, position.entry_price, exit_price, pnl)
        return pnl

    def _accumulate_daily_pnl(self, pnl: float):
        today = dt.date.today()
        if today != self._pnl_date:
            self._pnl_date = today
            self._realized_pnl_today = 0.0
        self._realized_pnl_today += pnl

    def flush_daily_pnl(self):
        """Call once at end-of-day (or on each poll; log_daily_pnl just
        appends a row, callers control cadence via main.py)."""
        equity = self.broker.get_account_equity()
        trade_logger.log_daily_pnl(self._pnl_date, self._realized_pnl_today, equity)

    def maybe_roll_day(self):
        """Called every main-loop tick. When the calendar date advances,
        flush the completed day's realized P&L row and reset the counter."""
        today = dt.date.today()
        if today != self._pnl_date:
            self.flush_daily_pnl()
            self._pnl_date = today
            self._realized_pnl_today = 0.0

    def sync_from_broker(self, instruments):
        """Reconcile local state with Alpaca's actual open positions on startup.
        Any position the bot doesn't know about is adopted with a hard stop
        computed at the current price (best-effort; a fresh ATR-based stop is
        recomputed on the next signal check per instrument)."""
        try:
            broker_positions = self.broker.list_positions()
        except Exception as exc:
            logger.warning("Could not sync positions from broker: %s", exc)
            return

        known_symbols = {inst.symbol for inst in instruments.values()}
        for bp in broker_positions:
            symbol = bp.symbol
            if symbol not in known_symbols or symbol in self.positions:
                continue
            side = "long" if float(bp.qty) > 0 else "short"
            entry_price = float(bp.avg_entry_price)
            qty = abs(float(bp.qty))
            instrument = instruments[symbol]
            trail_mult = instrument.params.get("trail_atr_mult", 2.0)
            # Conservative placeholder stop until the next ATR recalculation;
            # prevents an untracked position from having no risk control at all.
            hard_stop = entry_price * (0.99 if side == "long" else 1.01)
            self.positions[symbol] = Position(
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=entry_price,
                entry_time=dt.datetime.now(dt.timezone.utc),
                hard_stop=hard_stop,
                trail_mult=trail_mult,
                strategy=instrument.strategy,
                extreme_price=entry_price,
            )
            logger.info("Adopted pre-existing broker position: %s %s qty=%s @ %.4f",
                         symbol, side, qty, entry_price)
