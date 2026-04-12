import json
import os
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import MetaTrader5 as mt5


@dataclass(frozen=True)
class RiskDecision:
    ok: bool
    reason: str
    lot: float | None = None
    risk_cents: float | None = None
    remaining_cents: float | None = None


class RiskAgent:
    """
    Opsi 1:
    - Max daily loss dihitung dari booked loss + reserved risk (posisi berjalan dianggap loss via SL).
    - Jika posisi yang mengunci reserved risk berakhir PROFIT, reserved dilepas -> jatah kembali.
    - Tidak ada bonus tambahan +50 cents.
    """

    def __init__(self, cfg: dict | None = None, state_path: str = "runtime/risk_state.json"):
        self.cfg = cfg or {}
        self.state_path = state_path
        self._state = self._load_state()
        # Hard limits (baku) sesuai rule owner
        self.HARD_MIN_LOT = 0.01
        self.HARD_MAX_LOT = 0.7

    # -----------------------------
    # Public API
    # -----------------------------
    def assess_entry(
        self,
        *,
        symbol: str,
        action: str,
        order_kind: str,
        entry_price: float | None,
        sl: float,
        risk_cap_override_cents: float | None = None,
    ) -> RiskDecision:
        risk_cfg = (self.cfg.get("risk") or {})
        max_daily = float(risk_cfg.get("max_daily_loss_cents", 300))
        cooldown_seconds = int(risk_cfg.get("cooldown_seconds", 3600))
        losses_to_cooldown = int(risk_cfg.get("consecutive_losses_to_cooldown", 3))
        magic = int(risk_cfg.get("magic_number", 260410))

        self._rollover_day_if_needed()

        # 1) Cooldown check (loss streak)
        now_ts = int(time.time())
        cooldown_until = int(self._state.get("cooldown_until_ts") or 0)
        if now_ts < cooldown_until:
            remain = cooldown_until - now_ts
            return RiskDecision(False, f"Cooldown aktif ({remain}s tersisa).", None, None, None)

        streak = self._get_consecutive_loss_streak(magic=magic, n=losses_to_cooldown)
        if streak >= losses_to_cooldown:
            self._state["cooldown_until_ts"] = now_ts + cooldown_seconds
            self._save_state()
            return RiskDecision(False, f"Loss streak {streak}x. Robot diam {cooldown_seconds}s.", None, None, None)

        # 2) Compute booked loss today + reserved risk from open positions
        booked_loss = self._get_booked_loss_today_cents(magic=magic)
        reserved = self._get_reserved_risk_open_positions_cents(magic=magic)
        remaining_before = max_daily - booked_loss - reserved

        # 3) Risk cap per trade (20/40 cents) and lot sizing at SL
        risk_cap = float(risk_cap_override_cents) if risk_cap_override_cents is not None else self._risk_cap_for_symbol(symbol)
        if remaining_before < risk_cap - 1e-9:
            return RiskDecision(
                False,
                f"Daily limit tercapai. Remaining={remaining_before:.2f}¢, butuh={risk_cap:.2f}¢.",
                None,
                risk_cap,
                remaining_before,
            )

        if sl is None:
            return RiskDecision(False, "SL wajib untuk risk lock.", None, None, remaining_before)

        # Determine entry for market orders if absent
        if order_kind == "MARKET" or entry_price is None:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return RiskDecision(False, "Tick MT5 kosong. Tidak bisa hitung risk.", None, risk_cap, remaining_before)
            entry_price = float(tick.ask) if action.upper() == "BUY" else float(tick.bid)

        lot = self.calculate_lot_for_risk(symbol=symbol, action=action, entry_price=float(entry_price), sl=float(sl), risk_cents=risk_cap)
        if lot is None or lot <= 0:
            return RiskDecision(False, "Gagal menghitung lot dari SL/risk.", None, risk_cap, remaining_before)

        # Sanity: ensure calculated risk <= cap (tolerate tiny rounding errors)
        calc_risk = self._calc_loss_at_sl_cents(symbol=symbol, action=action, volume=float(lot), entry_price=float(entry_price), sl=float(sl))
        if calc_risk is None:
            return RiskDecision(False, "Gagal validasi risk (order_calc_profit).", None, risk_cap, remaining_before)
        if calc_risk > risk_cap + 0.5:  # allow small drift due to step rounding
            return RiskDecision(
                False,
                f"Lot rounding membuat risk {calc_risk:.2f}¢ > cap {risk_cap:.2f}¢.",
                None,
                risk_cap,
                remaining_before,
            )

        return RiskDecision(True, "Risk OK.", float(lot), risk_cap, remaining_before)

    def calculate_lot_for_risk(self, *, symbol: str, action: str, entry_price: float, sl: float, risk_cents: float) -> float | None:
        if entry_price is None or sl is None:
            return None
        if float(entry_price) == float(sl):
            return None

        # Profit at SL for 1.0 lot (usually negative for loss)
        order_type = mt5.ORDER_TYPE_BUY if action.upper() == "BUY" else mt5.ORDER_TYPE_SELL
        profit_at_sl = mt5.order_calc_profit(order_type, symbol, 1.0, float(entry_price), float(sl))
        if profit_at_sl is None:
            return None

        loss_per_lot = -float(profit_at_sl)
        if loss_per_lot <= 0:
            # SL placed on the "profit" side or calc failed to represent loss
            return None

        raw_lot = float(risk_cents) / loss_per_lot
        if raw_lot <= 0:
            return None

        info = mt5.symbol_info(symbol)
        if info is None:
            return None

        broker_min = float(info.volume_min)
        broker_max = float(info.volume_max)
        broker_step = float(info.volume_step)

        # Owner rules: lot min 0.01 (baku), lot max 0.7 (baku)
        vmin = max(broker_min, self.HARD_MIN_LOT)
        vmax = min(broker_max, self.HARD_MAX_LOT)
        if vmax < vmin:
            return None

        lot = self._round_volume_to_step(raw_lot, vmin, vmax, broker_step)
        if lot <= 0:
            return None
        return lot

    # -----------------------------
    # Internals
    # -----------------------------
    def _risk_cap_for_symbol(self, symbol: str) -> float:
        risk_cfg = (self.cfg.get("risk") or {})
        metals_cap = float(risk_cfg.get("max_loss_per_trade_metals_cents", 40))
        fx_cap = float(risk_cfg.get("max_loss_per_trade_forex_crypto_cents", 20))

        sym = (symbol or "").upper()
        if sym in ("XAUUSDC", "XAGUSDC"):
            return metals_cap
        return fx_cap

    def _get_booked_loss_today_cents(self, *, magic: int) -> float:
        start_ts, end_ts = self._today_window_server_time()
        deals = mt5.history_deals_get(start_ts, end_ts)
        if not deals:
            return 0.0

        loss_sum = 0.0
        for d in deals:
            try:
                if getattr(d, "magic", None) != magic:
                    continue
                if getattr(d, "entry", None) != mt5.DEAL_ENTRY_OUT:
                    continue
                p = float(getattr(d, "profit", 0.0) or 0.0)
                if p < 0:
                    loss_sum += (-p)
            except Exception:
                continue
        return float(loss_sum)

    def _get_reserved_risk_open_positions_cents(self, *, magic: int) -> float:
        positions = mt5.positions_get()
        if not positions:
            return 0.0

        reserved = 0.0
        for p in positions:
            try:
                if getattr(p, "magic", None) != magic:
                    continue
                sl = float(getattr(p, "sl", 0.0) or 0.0)
                if sl <= 0:
                    # If no SL, we cannot reserve deterministically -> treat as block
                    return float("inf")
                symbol = str(getattr(p, "symbol", ""))
                volume = float(getattr(p, "volume", 0.0) or 0.0)
                price_open = float(getattr(p, "price_open", 0.0) or 0.0)
                pos_type = int(getattr(p, "type", 0))
                action = "BUY" if pos_type == 0 else "SELL"
                loss = self._calc_loss_at_sl_cents(symbol=symbol, action=action, volume=volume, entry_price=price_open, sl=sl)
                if loss is None:
                    continue
                reserved += loss
            except Exception:
                continue
        return float(reserved)

    def _calc_loss_at_sl_cents(self, *, symbol: str, action: str, volume: float, entry_price: float, sl: float) -> float | None:
        order_type = mt5.ORDER_TYPE_BUY if action.upper() == "BUY" else mt5.ORDER_TYPE_SELL
        profit_at_sl = mt5.order_calc_profit(order_type, symbol, float(volume), float(entry_price), float(sl))
        if profit_at_sl is None:
            return None
        loss = -float(profit_at_sl)
        if loss < 0:
            # would be profit at SL (invalid SL direction)
            return None
        return float(loss)

    def _get_consecutive_loss_streak(self, *, magic: int, n: int) -> int:
        # Look back up to 7 days in server time to find last N closed trades for this magic
        now = datetime.now(timezone.utc)
        from_ts = int((now.timestamp()) - 7 * 86400)
        to_ts = int(now.timestamp()) + 5
        deals = mt5.history_deals_get(from_ts, to_ts)
        if not deals:
            return 0

        closed = []
        for d in deals:
            try:
                if getattr(d, "magic", None) != magic:
                    continue
                if getattr(d, "entry", None) != mt5.DEAL_ENTRY_OUT:
                    continue
                closed.append(d)
            except Exception:
                continue

        # Sort by time, newest first
        closed.sort(key=lambda x: getattr(x, "time", 0), reverse=True)
        streak = 0
        for d in closed[: max(n, 10)]:
            p = float(getattr(d, "profit", 0.0) or 0.0)
            if p < 0:
                streak += 1
                if streak >= n:
                    return streak
            else:
                break
        return streak

    def _today_window_server_time(self) -> tuple[int, int]:
        # MT5 times are in seconds; align "day" to broker/server time using mt5.time_current()
        server_now = mt5.time_current()
        if server_now is None or server_now <= 0:
            server_now = int(time.time())

        dt = datetime.fromtimestamp(int(server_now), tz=timezone.utc)
        start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        end = datetime(dt.year, dt.month, dt.day, 23, 59, 59, tzinfo=timezone.utc)
        return int(start.timestamp()), int(end.timestamp())

    def _rollover_day_if_needed(self):
        server_now = mt5.time_current()
        if server_now is None or server_now <= 0:
            server_now = int(time.time())
        dt = datetime.fromtimestamp(int(server_now), tz=timezone.utc)
        today = dt.strftime("%Y-%m-%d")
        if self._state.get("day") != today:
            self._state = {"day": today, "cooldown_until_ts": 0}
            self._save_state()

    def _round_volume_to_step(self, raw: float, vmin: float, vmax: float, step: float) -> float:
        if step <= 0:
            step = 0.01
        raw = max(vmin, min(vmax, raw))
        steps = int((raw - vmin) / step)
        vol = vmin + (steps * step)
        # ensure numeric stability
        return float(max(0.0, min(vmax, round(vol, 8))))

    def _load_state(self) -> dict:
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
        except Exception as e:
            logging.warning(f"Gagal load risk state: {e}")
        return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning(f"Gagal save risk state: {e}")

