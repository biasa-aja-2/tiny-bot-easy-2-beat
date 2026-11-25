"""Tiny trend bot for make.fun's "Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana".

Strategy (long only, no leverage, no shorts; pure stdlib, deterministic):

Two legs, chosen by the market regime of the hour:

1. Base mode -- the patient leg. Most hours the universe drifts down, so the bot
   mostly sits in USDC and only trades when a real signal fires:
   - Core trend entries: EMA(8) crosses above EMA(21) on a symbol whose 12h
     return is not deeply negative. Take profit +1.5%, stop -3%.
   - Scalp entries (only while fewer than 2 core positions are held): a
     dip-bounce (12h return under -2% with the last 3h turning up) or a
     6h momentum burst over +1.25%. Small size (15% of equity), quick
     take profit +1.5%, tight stop -1.5%.
2. Rally mode -- when the median symbol is up more than +2% over 24h, the
   whole universe is moving, so the bot holds the top 4 symbols by 12h
   return (25% of equity each), rebalanced every 12 bars, riding them with
   a 3% trailing stop instead of a fixed take-profit.

Sells are always sent before buys; a sell's proceeds land at the next open,
so buys decided on a bar that sold anything wait one bar. Orders decided on
a bar fill at the next bar's open. Nothing here reads the clock, the
environment or unseeded randomness, so two runs decide the same orders.

Guardrails the bot is built to clear: at least 8 trades (the scalp leg
keeps it active even in quiet windows) and max drawdown far under 25%.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

# --- strategy parameters (all in basis points unless noted) ---
EMA_FAST = 8
EMA_SLOW = 21
CORE_LOOKBACK = 12          # bars for the core entry's 12h-return filter
CORE_MIN_12H_BPS = -200     # core entry needs 12h return above -2%
CORE_TP_BPS = 150           # core take profit +1.5%
CORE_SL_BPS = 300           # core stop loss -3%
CORE_SIZE_BPS = 2500        # core buy: at most 25% of equity
SCALP_SIZE_BPS = 1500       # scalp buy: 15% of equity
SCALP_TP_BPS = 150          # scalp take profit +1.5%
SCALP_SL_BPS = 150          # scalp stop loss -1.5%
SCALP_DIP_BPS = -200        # scalp dip: 12h return under -2% ...
SCALP_BOUNCE_BPS = 50       # ... with the last 3h up over +0.5%
SCALP_BURST_BPS = 125       # ... or a 6h burst over +1.25%
SCALP_LOOKBACK = 12
MAX_POSITIONS = 3           # base mode holds at most 3 symbols
SCALP_MAX_CORE = 2          # scalps only fire while fewer than 2 core held
RALLY_MEDIAN_BPS = 200      # rally mode when median 24h return tops +2%
RALLY_TOP = 4               # rally mode holds the top 4 by 12h return
RALLY_REBALANCE = 12        # rally mode rebalances every 12 bars
RALLY_TRAIL_BPS = 300       # rally mode trailing stop -3% off the peak
MAX_SLIPPAGE_BPS = 10000    # wide open: a fill at the next open is never refused for slippage


class Series:
    """One symbol's closes and EMAs, updated bar by bar."""

    def __init__(self):
        self.closes = []
        self.fast = None
        self.fast_warm = []
        self.slow = None
        self.slow_warm = []
        self.prev_fast_above = None

    def push(self, close):
        self.closes.append(close)
        self._step(EMA_FAST, close)
        self._step(EMA_SLOW, close)

    def _step(self, span, close):
        if span == EMA_FAST:
            value, warm = self.fast, self.fast_warm
        else:
            value, warm = self.slow, self.slow_warm
        if value is None:
            warm.append(close)
            if len(warm) == span:
                value = sum(warm) / span
        else:
            value += (2.0 / (span + 1)) * (close - value)
        if span == EMA_FAST:
            self.fast = value
        else:
            self.slow = value

    def ret_bps(self, lookback):
        """Trailing return over `lookback` bars, in bps (int math, exact)."""
        cs = self.closes
        if len(cs) <= lookback:
            return None
        now, then = cs[-1], cs[-1 - lookback]
        return (now - then) * 10000 // then

    def crossed_up(self):
        """EMA(fast) crossed above EMA(slow) on this bar."""
        if self.fast is None or self.slow is None:
            return False
        above = self.fast > self.slow
        crossed = above and self.prev_fast_above is False
        self.prev_fast_above = above
        return crossed


class Bot:
    def __init__(self):
        self.series = {}
        self.entry = {}   # symbol -> close (int) at the bar the buy was decided on
        self.peak = {}    # symbol -> highest close (int) since entry
        self.kind = {}    # symbol -> "core" | "scalp" | "rally"
        self.pending = None  # buys to send on the next bar, after sells fill

    def on_batch(self, state, batch):
        if state.warming_up:
            return []
        for symbol in state.hello["symbols"]:
            symbol = symbol["symbol"]
            close = state.close(symbol)
            if close is None:
                continue
            series = self.series.setdefault(symbol, Series())
            series.push(close)
        # Update every symbol's cross state on every bar, whatever mode runs,
        # so a cross is judged on consecutive bars only.
        self.crossed = {s: series.crossed_up()
                        for s, series in self.series.items()}
        if self.pending is not None:
            want, kind = self.pending
            self.pending = None
            # A rally rebalance's buys were decided while the universe was
            # rallying; if the regime has already flipped back, drop them
            # instead of buying into a cooling market.
            if kind == "rally" and not self._is_rally():
                return self._base(state)
            return self._buys(state, want, kind)
        rally = self._is_rally()
        exits = self._exits(state, rally)
        if exits:
            return exits
        if rally:
            return self._rally(state)
        return self._base(state)

    # -- exits, checked every bar -------------------------------------------
    # In rally mode every held position rides with a trailing stop only: a
    # quick take-profit would cut a rally winner short. In base mode each
    # position exits on its own take-profit / stop.
    def _exits(self, state, rally):
        orders = []
        for symbol, holding in list(state.positions.items()):
            close = state.close(symbol)
            entry = self.entry.get(symbol)
            if close is None or entry is None:
                continue
            self.peak[symbol] = max(self.peak.get(symbol, close), close)
            kind = self.kind.get(symbol)
            if rally or kind == "rally":
                if close * 10000 < self.peak[symbol] * (10000 - RALLY_TRAIL_BPS):
                    orders.append(self._sell(symbol, holding.qty))
            elif kind == "scalp":
                if (close * 10000 >= entry * (10000 + SCALP_TP_BPS)
                        or close * 10000 <= entry * (10000 - SCALP_SL_BPS)):
                    orders.append(self._sell(symbol, holding.qty))
            else:  # core
                if (close * 10000 >= entry * (10000 + CORE_TP_BPS)
                        or close * 10000 <= entry * (10000 - CORE_SL_BPS)):
                    orders.append(self._sell(symbol, holding.qty))
        return orders

    # -- regime ---------------------------------------------------------------
    def _is_rally(self):
        rets = []
        for symbol, series in self.series.items():
            ret = series.ret_bps(24)
            if ret is not None:
                rets.append(ret)
        if len(rets) < len(self.series) or not rets:
            return False
        rets.sort()
        return rets[len(rets) // 2] > RALLY_MEDIAN_BPS

    # -- rally mode -----------------------------------------------------------
    def _rally(self, state):
        if state.seq < RALLY_REBALANCE or state.seq % RALLY_REBALANCE != 0:
            return []
        ranked = []
        for symbol, series in self.series.items():
            ret = series.ret_bps(CORE_LOOKBACK)
            if ret is not None and ret > 0:
                ranked.append((ret, symbol))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        want = [symbol for _, symbol in ranked[:RALLY_TOP]]
        sells = [self._sell(s, h.qty) for s, h in state.positions.items()
                 if s not in want]
        if sells:
            self.pending = (want, "rally")
            return sells
        return self._buys(state, want, "rally")

    # -- base mode ------------------------------------------------------------
    def _base(self, state):
        buys = []
        for symbol, series in self.series.items():
            if symbol in state.positions:
                continue
            if not self.crossed.get(symbol):
                continue
            ret12 = series.ret_bps(CORE_LOOKBACK)
            if ret12 is not None and ret12 > CORE_MIN_12H_BPS:
                buys.append((symbol, "core"))
        n_core = sum(1 for s in state.positions if self.kind.get(s) == "core")
        n_core += sum(1 for _, kind in buys if kind == "core")
        if n_core < SCALP_MAX_CORE:
            for symbol, series in self.series.items():
                if symbol in state.positions:
                    continue
                if any(s == symbol for s, _ in buys):
                    continue
                ret12 = series.ret_bps(SCALP_LOOKBACK)
                ret3 = series.ret_bps(3)
                ret6 = series.ret_bps(6)
                dip_bounce = (ret12 is not None and ret3 is not None
                              and ret12 < SCALP_DIP_BPS and ret3 > SCALP_BOUNCE_BPS)
                burst = ret6 is not None and ret6 > SCALP_BURST_BPS
                if dip_bounce or burst:
                    buys.append((symbol, "scalp"))
        if not buys:
            return []
        room = MAX_POSITIONS - len(state.positions)
        buys = buys[:max(room, 0)]
        if not buys:
            return []
        return self._buys(state, [s for s, _ in buys],
                          {s: kind for s, kind in buys})

    # -- order helpers ----------------------------------------------------------
    def _sell(self, symbol, qty):
        return {"side": "sell", "symbol": symbol, "qty": qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def _buys(self, state, want, kinds):
        if isinstance(kinds, str):
            kinds = {symbol: kinds for symbol in want}
        new = [s for s in want if s not in state.positions]
        if not new or state.cash <= 0:
            return []
        orders = []
        spent = 0
        for symbol in new:
            kind = kinds.get(symbol, "core")
            if kind == "rally":
                size = state.cash // len(new)
            elif kind == "scalp":
                size = SCALP_SIZE_BPS * state.equity // 10000
            else:
                size = CORE_SIZE_BPS * state.equity // 10000
            size = min(size, state.weight_room(symbol),
                       state.cash - spent)
            if size < int(state.hello["fees"]["min_notional_quote_units"]):
                continue
            spent += size
            orders.append({"side": "buy", "symbol": symbol,
                           "notional_quote_units": size,
                           "max_slippage_bps": MAX_SLIPPAGE_BPS})
            self.entry[symbol] = state.close(symbol)
            self.peak[symbol] = state.close(symbol)
            self.kind[symbol] = kind
        return orders


def main():
    bot = Bot()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
