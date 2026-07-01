"""
Central configuration for the trading bot.

Loads secrets from a .env file (see .env.example) and defines the static
configuration for instruments, strategies, risk management and logging.
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------- #
# Alpaca API credentials / connection
# --------------------------------------------------------------------------- #
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
# Defaults to paper trading. Point this at the live URL only when you are
# certain you want to trade with real money.
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_API_VERSION = "v2"

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    raise RuntimeError(
        "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. "
        "Copy .env.example to .env and fill in your Alpaca credentials."
    )

# --------------------------------------------------------------------------- #
# Risk management
# --------------------------------------------------------------------------- #
RISK_PER_TRADE_PCT = 0.01       # 1% of account equity risked per 1x ATR move
HARD_STOP_ATR_MULT = 1.0        # hard stop sits exactly 1 ATR away -> 1% equity
ATR_PERIOD = 14

# --------------------------------------------------------------------------- #
# Strategy groups / instrument universe
# --------------------------------------------------------------------------- #
MEAN_REVERSION = "mean_reversion"
MOMENTUM_BREAKOUT = "momentum_breakout"
TREND_FOLLOWING = "trend_following"


@dataclass
class InstrumentConfig:
    symbol: str                 # Alpaca order/quote symbol, e.g. "SPY" or "BTC/USD"
    asset_class: str            # "equity" or "crypto"
    strategy: str               # one of the strategy group constants above
    timeframe_amount: int
    timeframe_unit: str         # "Minute" | "Hour"
    params: Dict = field(default_factory=dict)


INSTRUMENTS: Dict[str, InstrumentConfig] = {
    "SPY": InstrumentConfig(
        symbol="SPY",
        asset_class="equity",
        strategy=MEAN_REVERSION,
        timeframe_amount=15,
        timeframe_unit="Minute",
        params={"lookback": 50, "entry_std_dev": 2.5},
    ),
    "QQQ": InstrumentConfig(
        symbol="QQQ",
        asset_class="equity",
        strategy=MEAN_REVERSION,
        timeframe_amount=15,
        timeframe_unit="Minute",
        params={"lookback": 50, "entry_std_dev": 2.8},
    ),
    "BTC/USD": InstrumentConfig(
        symbol="BTC/USD",
        asset_class="crypto",
        strategy=MOMENTUM_BREAKOUT,
        timeframe_amount=1,
        timeframe_unit="Hour",
        params={"lookback": 20, "volume_mult": 1.5, "trail_atr_mult": 2.0},
    ),
    "GLD": InstrumentConfig(
        symbol="GLD",
        asset_class="equity",
        strategy=TREND_FOLLOWING,
        timeframe_amount=4,
        timeframe_unit="Hour",
        params={"fast_ema": 50, "slow_ema": 200, "trail_atr_mult": 3.0},
    ),
    "USO": InstrumentConfig(
        symbol="USO",
        asset_class="equity",
        strategy=TREND_FOLLOWING,
        timeframe_amount=4,
        timeframe_unit="Hour",
        params={"fast_ema": 50, "slow_ema": 200, "trail_atr_mult": 3.0},
    ),
}

# Instruments treated as "risk-on equity beta" for the correlation filter.
CORRELATION_GROUP_EQUITY_RISK_ON: List[str] = ["SPY", "QQQ"]
CORRELATION_GATED_SYMBOL = "BTC/USD"

# --------------------------------------------------------------------------- #
# Main loop timing
# --------------------------------------------------------------------------- #
# How often (seconds) the main loop wakes up to: refresh prices, manage
# trailing/hard stops on open positions, and check whether any strategy's
# candle interval has elapsed and a new signal check is due.
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# --------------------------------------------------------------------------- #
# Logging / CSV output
# --------------------------------------------------------------------------- #
LOG_DIR = os.getenv("LOG_DIR", os.path.dirname(os.path.abspath(__file__)))
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
DAILY_PNL_CSV = os.path.join(LOG_DIR, "daily_pnl.csv")
BOT_LOG_FILE = os.path.join(LOG_DIR, "bot.log")
