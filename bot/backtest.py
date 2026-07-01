"""Historical backtester for all three strategies across all five instruments.

Reuses the exact same decision logic as live trading (bot/main.py) so the
backtest is a faithful simulation, not a parallel reimplementation:
  - the strategy modules' evaluate() functions decide entries/exits/flips
  - bot.risk_manager.RiskManager decides position size and stop prices
  - bot.main.evaluation_window_size() bounds the trailing window fed into
    each evaluate() call, exactly like the live polling loop

Two backtests are run:
  1. Standalone, per instrument: each instrument trades alone against its
     own starting capital, ignoring the correlation filter (there is
     nothing else in the portfolio to filter against).
  2. Combined portfolio: all five instruments trade simultaneously out of
     one shared account, in strict chronological order across their mixed
     timeframes, with the SPY/QQQ-vs-BTC/USD correlation filter active.

Costs modeled: 0.05% slippage against the trader on every fill (entries and
exits), $0 commission (Alpaca is commission-free).

Run with: python -m bot.backtest [--months N] [--output PATH]
"""
import argparse
import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from dateutil.relativedelta import relativedelta

import config
from bot import indicators
from bot.broker import AlpacaBroker, BrokerError
from bot.main import evaluation_window_size
from bot.risk_manager import RiskManager
from bot.strategies import mean_reversion, momentum_breakout, trend_following
from bot.strategies.base import OPEN_LONG, OPEN_SHORT, CLOSE, HOLD

logger = logging.getLogger("bot.backtest")

STRATEGY_EVALUATORS = {
    config.MEAN_REVERSION: mean_reversion.evaluate,
    config.MOMENTUM_BREAKOUT: momentum_breakout.evaluate,
    config.TREND_FOLLOWING: trend_following.evaluate,
}

DEFAULT_STARTING_EQUITY = 100_000.0
SLIPPAGE_PCT = 0.0005   # 0.05%, applied against the trader on every fill
COMMISSION = 0.0        # Alpaca is commission-free
TRADING_DAYS_PER_YEAR = 252
COMBINED_LABEL = "COMBINED"


# --------------------------------------------------------------------------- #
# Historical data fetch
# --------------------------------------------------------------------------- #

def _hours_per_bar(instrument: config.InstrumentConfig) -> float:
    if instrument.timeframe_unit == "Minute":
        return instrument.timeframe_amount / 60.0
    if instrument.timeframe_unit == "Hour":
        return instrument.timeframe_amount
    return instrument.timeframe_amount * 24.0


def estimate_buffer_days(instrument: config.InstrumentConfig) -> int:
    """Calendar days of history to fetch *before* the test window starts, so
    the first evaluation at the test start already has a full trailing
    window (see evaluation_window_size) to compute indicators from -
    mirroring what a live bot with a running history would already have."""
    bars_needed = evaluation_window_size(instrument)
    hours_needed = bars_needed * _hours_per_bar(instrument)
    if instrument.asset_class == "crypto":
        calendar_days = hours_needed / 24.0
    else:
        trading_hours_per_day = 6.5
        trading_days = hours_needed / trading_hours_per_day
        calendar_days = trading_days * (7 / 5) * 1.08  # weekends + holiday margin
    return math.ceil(calendar_days) + 10  # extra safety margin


def fetch_historical_data(broker: AlpacaBroker, instrument: config.InstrumentConfig,
                           test_start: dt.datetime, test_end: dt.datetime) -> pd.DataFrame:
    buffer_days = estimate_buffer_days(instrument)
    fetch_start = test_start - dt.timedelta(days=buffer_days)
    logger.info("Fetching %s bars for %s from %s to %s (buffer=%dd)",
                instrument.timeframe_unit, instrument.symbol, fetch_start.date(), test_end.date(), buffer_days)
    return broker.get_historical_bars(instrument, fetch_start, test_end)


# --------------------------------------------------------------------------- #
# Backtest position/portfolio bookkeeping (mirrors bot/portfolio.py's
# interface but with no CSV side effects, since this isn't live trading)
# --------------------------------------------------------------------------- #

@dataclass
class BacktestPosition:
    symbol: str
    side: str
    qty: float
    entry_price: float
    entry_time: pd.Timestamp
    hard_stop: float
    trail_mult: float
    strategy: str
    extreme_price: float
    atr: float = 0.0


@dataclass
class Trade:
    symbol: str
    strategy: str
    side: str
    qty: float
    entry_price: float
    exit_price: float
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    pnl: float


class FakeBroker:
    """Stands in for AlpacaBroker's get_account_equity() during simulation."""

    def __init__(self, starting_equity: float):
        self.equity = starting_equity

    def get_account_equity(self) -> float:
        return self.equity


class BacktestPortfolio:
    """Stands in for bot.portfolio.Portfolio: same shape RiskManager expects
    (get_position), but trades are collected in memory instead of CSV."""

    def __init__(self, broker: FakeBroker, trades: List[Trade]):
        self.broker = broker
        self.trades = trades
        self.positions: Dict[str, BacktestPosition] = {}

    def get_position(self, symbol: str) -> Optional[BacktestPosition]:
        return self.positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def open_position(self, symbol, side, qty, entry_price, hard_stop, trail_mult, strategy, atr):
        position = BacktestPosition(
            symbol=symbol, side=side, qty=qty, entry_price=entry_price,
            entry_time=None, hard_stop=hard_stop, trail_mult=trail_mult,
            strategy=strategy, extreme_price=entry_price, atr=atr,
        )
        self.positions[symbol] = position
        return position

    def update_extreme_with_bar(self, symbol: str, high: float, low: float):
        position = self.positions.get(symbol)
        if position is None:
            return
        if position.side == "long":
            position.extreme_price = max(position.extreme_price, high)
        else:
            position.extreme_price = min(position.extreme_price, low)

    def update_atr(self, symbol: str, atr: float):
        position = self.positions.get(symbol)
        if position is not None and atr > 0:
            position.atr = atr

    def close_position(self, symbol: str, exit_price: float, exit_time: pd.Timestamp) -> float:
        position = self.positions.pop(symbol, None)
        if position is None:
            return 0.0
        if position.side == "long":
            pnl = (exit_price - position.entry_price) * position.qty
        else:
            pnl = (position.entry_price - exit_price) * position.qty
        self.broker.equity += pnl
        self.trades.append(Trade(
            symbol=symbol, strategy=position.strategy, side=position.side, qty=position.qty,
            entry_price=position.entry_price, exit_price=exit_price,
            entry_time=position.entry_time, exit_time=exit_time, pnl=pnl,
        ))
        return pnl


def apply_slippage(price: float, side: str, is_entry: bool, slippage_pct: float = SLIPPAGE_PCT) -> float:
    """Slippage always works against the trader: worse fills on both entry
    and exit, in both directions."""
    disadvantaged_up = (side == "long") == is_entry  # buying: long entry or short exit
    return price * (1 + slippage_pct) if disadvantaged_up else price * (1 - slippage_pct)


# --------------------------------------------------------------------------- #
# Core per-bar simulation step, shared by standalone and combined runs
# --------------------------------------------------------------------------- #

def _process_bar(symbol: str, instrument: config.InstrumentConfig, window_df: pd.DataFrame,
                  bar: pd.Series, ts: pd.Timestamp, portfolio: BacktestPortfolio,
                  risk_manager: RiskManager, apply_correlation_filter: bool):
    position = portfolio.get_position(symbol)

    # 1. Stop management using this bar's intrabar range.
    if position is not None:
        portfolio.update_extreme_with_bar(symbol, bar["high"], bar["low"])
        position = portfolio.get_position(symbol)
        if position.atr > 0:
            trailing_stop = risk_manager.trailing_stop_price(
                position.extreme_price, position.atr, position.trail_mult, position.side
            )
            effective_stop = risk_manager.effective_stop_price(position.side, position.hard_stop, trailing_stop)
            hit = (
                (position.side == "long" and bar["low"] <= effective_stop)
                or (position.side == "short" and bar["high"] >= effective_stop)
            )
            if hit:
                exit_price = apply_slippage(effective_stop, position.side, is_entry=False)
                portfolio.close_position(symbol, exit_price, ts)
                position = None

    # 2. Strategy signal off the trailing window (identical inputs to live).
    current_side = position.side if position else None
    evaluator = STRATEGY_EVALUATORS[instrument.strategy]
    signal = evaluator(window_df, instrument, current_side)

    if signal.action == HOLD:
        return

    exec_price = signal.price if signal.price is not None else bar["close"]

    if signal.action == CLOSE:
        if position is not None:
            fill = apply_slippage(exec_price, position.side, is_entry=False)
            portfolio.close_position(symbol, fill, ts)
        return

    desired_side = "long" if signal.action == OPEN_LONG else "short"

    if position is not None and position.side == desired_side:
        return

    if position is not None and position.side != desired_side:
        fill = apply_slippage(exec_price, position.side, is_entry=False)
        portfolio.close_position(symbol, fill, ts)

    if desired_side == "long" and apply_correlation_filter and risk_manager.correlation_filter_blocks_long(symbol):
        return

    atr_series_last = _atr_from_window(window_df)
    if atr_series_last is None or atr_series_last <= 0:
        return

    qty = risk_manager.position_size(symbol, atr_series_last, exec_price, instrument.asset_class)
    if qty <= 0:
        return

    fill_price = apply_slippage(exec_price, desired_side, is_entry=True)
    hard_stop = risk_manager.hard_stop_price(fill_price, atr_series_last, desired_side)
    trail_mult = instrument.params.get("trail_atr_mult", config.HARD_STOP_ATR_MULT)
    position = portfolio.open_position(symbol, desired_side, qty, fill_price, hard_stop,
                                        trail_mult, instrument.strategy, atr_series_last)
    position.entry_time = ts


def _atr_from_window(window_df: pd.DataFrame) -> Optional[float]:
    if len(window_df) < config.ATR_PERIOD + 1:
        return None
    value = indicators.atr(window_df, config.ATR_PERIOD).iloc[-1]
    return None if pd.isna(value) else float(value)


# --------------------------------------------------------------------------- #
# Standalone per-instrument backtest
# --------------------------------------------------------------------------- #

def simulate_instrument(df: pd.DataFrame, instrument: config.InstrumentConfig,
                         test_start: pd.Timestamp, starting_equity: float = DEFAULT_STARTING_EQUITY) -> dict:
    trades: List[Trade] = []
    broker = FakeBroker(starting_equity)
    portfolio = BacktestPortfolio(broker, trades)
    risk_manager = RiskManager(broker, portfolio)

    window_size = evaluation_window_size(instrument)
    equity_points = []

    test_rows = [i for i in range(len(df)) if df.index[i] >= test_start]
    for i in test_rows:
        ts = df.index[i]
        bar = df.iloc[i]
        window_df = df.iloc[max(0, i - window_size + 1): i + 1]

        _process_bar(instrument.symbol, instrument, window_df, bar, ts, portfolio,
                     risk_manager, apply_correlation_filter=False)

        equity_points.append((ts, _mark_to_market(broker.equity, portfolio.get_position(instrument.symbol), bar["close"])))

    equity_curve = pd.Series(dict(equity_points)) if equity_points else pd.Series(dtype=float)
    return {
        "symbol": instrument.symbol,
        "strategy": instrument.strategy,
        "trades": trades,
        "equity_curve": equity_curve,
        "starting_equity": starting_equity,
        "final_equity": broker.equity,
    }


def _mark_to_market(realized_equity: float, position: Optional[BacktestPosition], last_price: float) -> float:
    if position is None:
        return realized_equity
    if position.side == "long":
        unrealized = (last_price - position.entry_price) * position.qty
    else:
        unrealized = (position.entry_price - last_price) * position.qty
    return realized_equity + unrealized


# --------------------------------------------------------------------------- #
# Combined portfolio backtest (shared capital, correlation filter active)
# --------------------------------------------------------------------------- #

def simulate_combined(dfs: Dict[str, pd.DataFrame], instruments: Dict[str, config.InstrumentConfig],
                       test_start: pd.Timestamp, starting_equity: float = DEFAULT_STARTING_EQUITY) -> dict:
    trades: List[Trade] = []
    broker = FakeBroker(starting_equity)
    portfolio = BacktestPortfolio(broker, trades)
    risk_manager = RiskManager(broker, portfolio)

    events = []  # (timestamp, symbol, row_index)
    for symbol, df in dfs.items():
        window_size = evaluation_window_size(instruments[symbol])
        for i in range(len(df)):
            if df.index[i] >= test_start:
                events.append((df.index[i], symbol, i, window_size))
    events.sort(key=lambda e: (e[0], e[1]))

    last_price: Dict[str, float] = {}
    equity_points = []

    for ts, symbol, i, window_size in events:
        instrument = instruments[symbol]
        df = dfs[symbol]
        bar = df.iloc[i]
        window_df = df.iloc[max(0, i - window_size + 1): i + 1]

        _process_bar(symbol, instrument, window_df, bar, ts, portfolio,
                     risk_manager, apply_correlation_filter=True)

        last_price[symbol] = bar["close"]
        mtm = broker.equity
        for sym, pos in portfolio.positions.items():
            px = last_price.get(sym, pos.entry_price)
            if pos.side == "long":
                mtm += (px - pos.entry_price) * pos.qty
            else:
                mtm += (pos.entry_price - px) * pos.qty
        equity_points.append((ts, mtm))

    equity_curve = pd.Series(dict(equity_points)) if equity_points else pd.Series(dtype=float)
    return {
        "symbol": COMBINED_LABEL,
        "strategy": "all",
        "trades": trades,
        "equity_curve": equity_curve,
        "starting_equity": starting_equity,
        "final_equity": broker.equity,
    }


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def compute_metrics(result: dict) -> dict:
    trades: List[Trade] = result["trades"]
    equity_curve: pd.Series = result["equity_curve"]
    starting_equity = result["starting_equity"]

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    total_trades = len(trades)
    win_rate = len(wins) / total_trades if total_trades else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    if len(equity_curve) >= 2:
        running_max = equity_curve.cummax()
        drawdown = (equity_curve - running_max) / running_max
        max_drawdown = abs(drawdown.min())
    else:
        max_drawdown = 0.0

    sharpe = _sharpe_ratio(equity_curve)

    final_equity = result["final_equity"]
    total_return = (final_equity / starting_equity) - 1.0

    return {
        "symbol": result["symbol"],
        "strategy": result["strategy"],
        "total_trades": total_trades,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "total_return": total_return,
        "final_equity": final_equity,
    }


def _sharpe_ratio(equity_curve: pd.Series, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    if len(equity_curve) < 2:
        return 0.0
    daily = equity_curve.resample("1D").last().ffill()
    returns = daily.pct_change().dropna()
    if len(returns) < 2 or returns.std() == 0:
        return 0.0
    return float((returns.mean() / returns.std()) * math.sqrt(periods_per_year))


# --------------------------------------------------------------------------- #
# Reporting: table + chart + negative-Sharpe flags
# --------------------------------------------------------------------------- #

def print_summary_table(metrics_list: List[dict]):
    headers = ["Instrument", "Strategy", "Trades", "Win%", "AvgWin", "AvgLoss",
               "ProfitFactor", "MaxDD%", "Sharpe", "TotalRet%"]
    rows = []
    for m in metrics_list:
        pf = "inf" if math.isinf(m["profit_factor"]) else f"{m['profit_factor']:.2f}"
        rows.append([
            m["symbol"],
            m["strategy"],
            str(m["total_trades"]),
            f"{m['win_rate'] * 100:.1f}",
            f"${m['avg_win']:.2f}",
            f"${m['avg_loss']:.2f}",
            pf,
            f"{m['max_drawdown'] * 100:.2f}",
            f"{m['sharpe']:.2f}",
            f"{m['total_return'] * 100:.2f}",
        ])

    widths = [max(len(h), *(len(r[c]) for r in rows)) for c, h in enumerate(headers)]
    line = "+".join("-" * (w + 2) for w in widths)

    def fmt_row(cells):
        return "|".join(f" {c:<{w}} " for c, w in zip(cells, widths))

    print("\n" + "=" * len(line))
    print("BACKTEST SUMMARY")
    print("=" * len(line))
    print(line)
    print(fmt_row(headers))
    print(line)
    for r in rows:
        print(fmt_row(r))
    print(line)


def flag_negative_sharpe(metrics_list: List[dict]):
    flagged = [m for m in metrics_list if m["sharpe"] < 0]
    if not flagged:
        print("\nNo instrument/strategy had a negative Sharpe ratio over the test period.")
        return
    print("\n" + "!" * 70)
    print("NEGATIVE SHARPE RATIO - parameters likely need adjustment:")
    for m in flagged:
        print(f"  - {m['symbol']} ({m['strategy']}): Sharpe={m['sharpe']:.2f}, "
              f"total_return={m['total_return'] * 100:.2f}%, max_drawdown={m['max_drawdown'] * 100:.2f}%, "
              f"win_rate={m['win_rate'] * 100:.1f}%, trades={m['total_trades']}")
    print("!" * 70)


def plot_equity_curves(results: List[dict], output_path: str):
    n = len(results)
    cols = 3
    rows = math.ceil(n / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 3.5 * rows), squeeze=False)

    for idx, result in enumerate(results):
        ax = axes[idx // cols][idx % cols]
        curve = result["equity_curve"]
        if len(curve) == 0:
            ax.set_title(f"{result['symbol']} (no data)")
            continue
        ax.plot(curve.index, curve.values, linewidth=1.2)
        ax.axhline(result["starting_equity"], color="gray", linestyle="--", linewidth=0.8)
        total_return = (result["final_equity"] / result["starting_equity"] - 1) * 100
        ax.set_title(f"{result['symbol']} ({result['strategy']})  {total_return:+.1f}%")
        ax.set_ylabel("Equity ($)")
        ax.tick_params(axis="x", rotation=30)

    for idx in range(n, rows * cols):
        axes[idx // cols][idx % cols].axis("off")

    fig.suptitle("Backtest Equity Curves (6-month historical simulation)", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    logger.info("Saved equity curve chart to %s", output_path)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run_backtest(months: int = 6, starting_equity: float = DEFAULT_STARTING_EQUITY,
                  output_path: str = "backtest_results.png"):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    broker = AlpacaBroker()
    test_end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    test_start = test_end - relativedelta(months=months)

    dfs: Dict[str, pd.DataFrame] = {}
    standalone_results = []
    for symbol, instrument in config.INSTRUMENTS.items():
        try:
            df = fetch_historical_data(broker, instrument, test_start, test_end)
        except BrokerError as exc:
            logger.error("Failed to fetch historical data for %s: %s", symbol, exc)
            continue
        if df.empty:
            logger.warning("No historical data returned for %s; skipping.", symbol)
            continue
        dfs[symbol] = df
        standalone_results.append(simulate_instrument(df, instrument, pd.Timestamp(test_start), starting_equity))

    combined_result = simulate_combined(dfs, config.INSTRUMENTS, pd.Timestamp(test_start), starting_equity)

    all_results = standalone_results + [combined_result]
    metrics_list = [compute_metrics(r) for r in all_results]

    print_summary_table(metrics_list)
    flag_negative_sharpe(metrics_list)
    plot_equity_curves(all_results, output_path)

    return metrics_list


def main():
    parser = argparse.ArgumentParser(description="Backtest all strategies against historical Alpaca data.")
    parser.add_argument("--months", type=int, default=6, help="Length of the test window in months.")
    parser.add_argument("--output", type=str, default="backtest_results.png", help="Equity curve chart output path.")
    parser.add_argument("--starting-equity", type=float, default=DEFAULT_STARTING_EQUITY)
    args = parser.parse_args()
    run_backtest(months=args.months, starting_equity=args.starting_equity, output_path=args.output)


if __name__ == "__main__":
    main()
