# trading_bot

A multi-strategy algorithmic trading bot for Alpaca Markets that trades five
instruments simultaneously — SPY, QQQ, BTC/USD, GLD, USO — each with a
strategy suited to how that market behaves.

## Strategies

| Strategy | Instruments | Timeframe | Logic |
|---|---|---|---|
| Mean Reversion | SPY, QQQ | 15m | 20-period SMA/std-dev band. Long below `mean - 1.5σ` (SPY) / `1.8σ` (QQQ), short above the mirror band. Exit at the mean. |
| Momentum Breakout | BTC/USD | 1h | Long on a close above the 20-period high with volume ≥ 1.5x the 20-period average; short/flip on the mirror breakdown. 2x ATR trailing stop. |
| Trend Following | GLD, USO | 4h | Long when the 50 EMA crosses above the 200 EMA; exit/flip short on the opposite cross. 3x ATR trailing stop. |

## Risk management

- **ATR-based sizing**: every position is sized so a 1x (14-period) ATR
  adverse move costs exactly 1% of total account equity — quiet instruments
  get bigger size, volatile ones get smaller size, and dollar risk stays
  constant across all five instruments.
- **Hard stop**: placed exactly 1x ATR from entry, which by construction is a
  1%-of-equity stop loss on every trade, no exceptions.
- **Trailing stop**: layered on top of the hard stop (2x ATR for BTC/USD, 3x
  ATR for GLD/USO) and can only tighten the effective stop as a trade moves
  favorably — it never loosens past the hard stop.
- **Correlation filter**: if SPY and QQQ are both already long, new long
  entries on BTC/USD are blocked to avoid stacking risk-on exposure.

## Project layout

```
config.py                          instrument universe, risk & timing constants, loads .env
bot/
  broker.py                        Alpaca REST wrapper (bars, orders, account, clock) with retries
  indicators.py                    SMA/EMA/std-dev/ATR/rolling hi-lo/crossover helpers
  portfolio.py                     open-position bookkeeping, trailing-stop watermark, broker sync
  risk_manager.py                  ATR position sizing, stop pricing, correlation filter
  trade_logger.py                  trades.csv / daily_pnl.csv writers
  main.py                          continuous polling loop, order execution
  backtest.py                      historical backtester for all 3 strategies
  strategies/
    base.py                        shared Signal type
    mean_reversion.py              Strategy 1 (SPY, QQQ)
    momentum_breakout.py           Strategy 2 (BTC/USD)
    trend_following.py             Strategy 3 (GLD, USO)
.env.example                       template for API credentials (copy to .env)
requirements.txt
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your Alpaca paper (or live) API keys
```

## Run

```bash
python -m bot.main
```

The bot defaults to Alpaca's **paper trading** endpoint. Only point
`ALPACA_BASE_URL` at the live endpoint once you've validated behavior on
paper. Equity instruments (SPY, QQQ, GLD, USO) are automatically skipped
outside regular market hours; BTC/USD is checked continuously, 24/7.

## Backtesting

```bash
python -m bot.backtest                              # default: 6 months, $100k
python -m bot.backtest --months 3 --starting-equity 50000 --output backtest_results.png
```

Pulls historical bars from Alpaca (15m for SPY/QQQ, 1h for BTC/USD, 4h for
GLD/USO) plus a warm-up buffer so indicators are already primed at the start
of the reported test window, then replays the exact same strategy/risk logic
used live (same `evaluate()` functions, same `RiskManager` sizing/stops) bar
by bar. Applies 0.05% slippage against the trader on every fill and $0
commission. Reports, per instrument and for the combined portfolio (with the
SPY/QQQ-vs-BTC/USD correlation filter active): total trades, win rate,
average win/loss, profit factor, max drawdown, Sharpe ratio, and total
return, printed as a summary table and plotted to `backtest_results.png`.
Any instrument with a negative Sharpe ratio over the test period is flagged
explicitly at the end of the run.

## Logs

- `trades.csv` — one row per closed trade: timestamp, instrument, direction,
  entry price, exit price, P&L, position size.
- `daily_pnl.csv` — one row per calendar day: date, realized P&L, account
  equity.
- `bot.log` — runtime log (also echoed to stdout).

## Disclaimer

This is example/educational software. Trading involves substantial risk of
loss. Test thoroughly on paper trading before ever connecting a live
account, and review the strategy logic and risk parameters against your own
risk tolerance.
