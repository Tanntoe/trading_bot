"""Shared types for strategy modules.

Every strategy's `evaluate()` returns a Signal describing what the caller
(bot/main.py) should do next. Strategies never place orders or size
positions themselves - that is the job of risk_manager/portfolio/broker in
main.py. This keeps strategy logic pure and easy to test on a DataFrame.
"""
from dataclasses import dataclass
from typing import Optional

OPEN_LONG = "open_long"
OPEN_SHORT = "open_short"
CLOSE = "close"
HOLD = "hold"


@dataclass
class Signal:
    action: str            # one of OPEN_LONG, OPEN_SHORT, CLOSE, HOLD
    reason: str = ""
    price: Optional[float] = None
