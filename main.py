#!/usr/bin/env python3
"""Breakout trend bot for make.fun btc-race-24h-20261123-0511.

Speaks agent-fun/bars-replay/v1: newline-delimited JSON on stdin/stdout.
Standard library only. Fully deterministic: integer arithmetic throughout,
no randomness, no clock, no environment reads; every decision is a pure
function of the bars received.

Strategy (long-only, one asset CBBTC, at most one open position):
- Enter with all cash when the close makes a new 60-bar high, the trailing
  60-bar return is at least +0.5%, and the close is above the 120-bar EMA.
- Exit the whole position on a 2% trailing stop from the highest close
  since entry, or when the close falls below the lowest close of the
  30 bars before entry -- the breakdown level for the trade, fixed when
  the buy is decided. (A trailing 30-bar low re-read every bar fires on
  microscopic new lows in sideways chop: on the dev window it exits every
  trade on a ~0.1% dip and whipsaws the bot to -$26; the entry-time level
  only breaks on a real reversal.)
- Orders decided on a bar fill at the next bar's open; max slippage 300 bps.

The first 120 bars only build history (the EMA needs to settle); no trade
is decided before then. A buy or sell decided on one batch is filled on the
next, so a pending flag stops a second order for the same move while the
first is still in flight; the flag clears when the position appears or
vanishes, when the fill comes back rejected, or after two batches at most.
"""

import json
import sys

SYMBOL = "CBBTC"
MAX_SLIPPAGE_BPS = 300
LOOKBACK_HIGH = 60          # bars for the new-high test and the return test
LOOKBACK_LOW = 30           # bars for the breakdown test
EMA_BARS = 120              # bars for the trend filter
MIN_HISTORY = 120           # bars of history before the first trade
RETURN_MIN_NUM = 1005       # trailing return >= +0.5%: close * 1000 >= ref * 1005
RETURN_MIN_DEN = 1000
STOP_NUM = 98               # 2% trailing stop: close * 100 <= peak * 98
STOP_DEN = 100


def ema_step(prev_ema, close):
    """One integer EMA step, alpha = 2 / (EMA_BARS + 1)."""
    n = EMA_BARS + 1
    return (prev_ema * (n - 2) + close * 2) // n


class Bot:
    """One run of the strategy: bar history, EMA, position and peak."""

    def __init__(self, hello):
        self.hello = hello
        self.warmup_bars = hello.get("warmup_bars", 0)
        self.seq = -1
        self.closes = []            # closes seen so far, oldest first
        self.ema = None             # integer EMA of the closes
        self.cash = int(hello["capital"]["quote_units"])
        self.qty = 0                # CBBTC held, at qty_scale
        self.peak = None            # highest close since entry
        self.support = None         # lowest close of the 30 bars before entry
        self.pending_buy = None     # (client_id, seq) of a buy in flight
        self.pending_sell = None    # (client_id, seq) of a sell in flight
        self.buy_n = 0
        self.sell_n = 0

    def warming_up(self):
        return self.warmup_bars > 0 and self.seq < self.warmup_bars

    def apply_account(self, account):
        self.cash = int(account["cash_quote_units"])
        qty = 0
        for pos in account.get("positions", []):
            if pos.get("symbol") == SYMBOL:
                qty = int(pos["qty"])
        was_flat = self.qty == 0
        self.qty = qty
        if was_flat and qty > 0:
            # A buy just landed: start the trailing peak at this close.
            self.peak = self.closes[-1] if self.closes else None
            self.pending_buy = None
        if qty == 0:
            self.peak = None
            self.support = None
            self.pending_sell = None

    def apply_fills(self, fills):
        """Clear a pending flag when its order came back rejected."""
        for fill in fills or []:
            cid = fill.get("client_id")
            status = fill.get("status")
            if status == "rejected":
                if self.pending_buy and cid == self.pending_buy[0]:
                    self.pending_buy = None
                if self.pending_sell and cid == self.pending_sell[0]:
                    self.pending_sell = None

    def clear_stale_pending(self):
        """A fill always lands on the very next batch; older pendings are dead."""
        if self.pending_buy and self.seq - self.pending_buy[1] > 2:
            self.pending_buy = None
        if self.pending_sell and self.seq - self.pending_sell[1] > 2:
            self.pending_sell = None

    def on_bar(self, bar):
        close = int(bar["c"])
        self.closes.append(close)
        if self.ema is None:
            self.ema = close
        else:
            self.ema = ema_step(self.ema, close)
        if self.qty > 0 and (self.peak is None or close > self.peak):
            self.peak = close

    def entry_signal(self):
        """True when the close is a new 60-bar high, up >= 0.5% over 60 bars,
        and above the 120-bar EMA."""
        if len(self.closes) < MIN_HISTORY + 1:
            return False
        close = self.closes[-1]
        prior = self.closes[-LOOKBACK_HIGH - 1:-1]
        if close <= max(prior):
            return False
        ref = self.closes[-LOOKBACK_HIGH - 1]
        if close * RETURN_MIN_DEN < ref * RETURN_MIN_NUM:
            return False
        return close > self.ema

    def exit_signal(self):
        """True on a 2% trailing stop from the peak, or a close below the
        breakdown level fixed at entry (the lowest close of the 30 bars
        before the buy was decided)."""
        if self.qty == 0 or self.peak is None:
            return False
        close = self.closes[-1]
        if close * STOP_DEN <= self.peak * STOP_NUM:
            return True
        if self.support is not None and close < self.support:
            return True
        return False

    def decide(self):
        """The orders for this batch: at most one, buy all cash or sell all."""
        if self.warming_up():
            return []
        if self.qty > 0:
            if self.pending_sell is None and self.exit_signal():
                self.sell_n += 1
                cid = "s%d" % self.sell_n
                self.pending_sell = (cid, self.seq)
                return [{
                    "side": "sell",
                    "client_id": cid,
                    "symbol": SYMBOL,
                    "qty": str(self.qty),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            return []
        if self.pending_buy is None and self.cash > 0 and self.entry_signal():
            self.buy_n += 1
            cid = "b%d" % self.buy_n
            self.pending_buy = (cid, self.seq)
            # Fix the breakdown level for this trade: the lowest close of
            # the 30 bars before the buy is decided.
            if len(self.closes) > LOOKBACK_LOW:
                self.support = min(self.closes[-LOOKBACK_LOW - 1:-1])
            return [{
                "side": "buy",
                "client_id": cid,
                "symbol": SYMBOL,
                "notional_quote_units": str(self.cash),
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []


def run():
    bot = None
    out = sys.stdout
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            if msg.get("protocol") != "agent-fun/bars-replay/v1":
                raise SystemExit("unexpected protocol: %r" % (msg.get("protocol"),))
            bot = Bot(msg)
        elif kind == "batch":
            bot.seq = msg["seq"]
            for bar in msg.get("bars", []):
                if bar.get("symbol") == SYMBOL:
                    bot.on_bar(bar)
            bot.apply_account(msg["account"])
            bot.apply_fills(msg.get("fills", []))
            bot.clear_stale_pending()
            orders = bot.decide()
            out.write(json.dumps(
                {"type": "reply", "seq": msg["seq"], "orders": orders},
                separators=(",", ":"),
            ) + "\n")
            out.flush()
        elif kind == "done":
            return 0
        else:
            raise SystemExit("unknown message type: %r" % (kind,))
    raise SystemExit("input ended before done")


if __name__ == "__main__":
    sys.exit(run())
