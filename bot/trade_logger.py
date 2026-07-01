"""CSV trade and daily P&L logging.

trades.csv gets one row per closed trade.
daily_pnl.csv gets one row per calendar day summarizing realized P&L and
end-of-day account equity.
"""
import csv
import datetime as dt
import os
import threading

import config

_lock = threading.Lock()

TRADES_HEADER = [
    "timestamp",
    "instrument",
    "direction",
    "entry_price",
    "exit_price",
    "profit_loss",
    "position_size",
]

DAILY_PNL_HEADER = ["date", "realized_pnl", "account_equity"]


def _ensure_header(path: str, header):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)


def log_trade(instrument: str, direction: str, entry_price: float,
              exit_price: float, profit_loss: float, position_size: float,
              timestamp: dt.datetime = None):
    timestamp = timestamp or dt.datetime.now(dt.timezone.utc)
    with _lock:
        _ensure_header(config.TRADES_CSV, TRADES_HEADER)
        with open(config.TRADES_CSV, "a", newline="") as f:
            csv.writer(f).writerow([
                timestamp.isoformat(),
                instrument,
                direction,
                f"{entry_price:.4f}",
                f"{exit_price:.4f}",
                f"{profit_loss:.2f}",
                f"{position_size:.6f}",
            ])


def log_daily_pnl(date: dt.date, realized_pnl: float, account_equity: float):
    with _lock:
        _ensure_header(config.DAILY_PNL_CSV, DAILY_PNL_HEADER)
        with open(config.DAILY_PNL_CSV, "a", newline="") as f:
            csv.writer(f).writerow([
                date.isoformat(),
                f"{realized_pnl:.2f}",
                f"{account_equity:.2f}",
            ])
