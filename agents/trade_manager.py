import csv
import json
import os
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5


@dataclass
class TradeSource:
    chat_id: int | None = None
    chat_title: str | None = None
    fwd_sender_id: int | None = None
    fwd_sender_name: str | None = None

    def label(self) -> str:
        parts = []
        if self.chat_title:
            parts.append(str(self.chat_title))
        if self.chat_id is not None:
            parts.append(str(self.chat_id))
        if self.fwd_sender_name:
            parts.append(str(self.fwd_sender_name))
        if self.fwd_sender_id is not None:
            parts.append(str(self.fwd_sender_id))
        return " | ".join(parts) if parts else "unknown"


class TradeManager:
    """
    BEP + Trailing + Journal
    - Aktif saat orchestrator start (START_ROBOT.bat).
    - Anti-SPAM: throttle per ticket + delta minimum + stoplevel check.
    """

    def __init__(
        self,
        *,
        magic_number: int,
        risk_agent=None,
        state_path: str = "runtime/trade_state.json",
        journal_path: str = "runtime/journal_trades.csv",
        loop_interval_sec: float = 1.0,
        min_modify_interval_sec: float = 10.0,
        min_sl_change_pips: float = 5.0,
    ):
        self.magic_number = int(magic_number)
        self.risk_agent = risk_agent
        self.state_path = state_path
        self.journal_path = journal_path
        self.loop_interval_sec = float(loop_interval_sec)
        self.min_modify_interval_sec = float(min_modify_interval_sec)
        self.min_sl_change_pips = float(min_sl_change_pips)

        self._state = self._load_state()
        self._last_modify_ts: dict[int, float] = {}
        self._hi_water: dict[int, float] = {}  # BUY
        self._lo_water: dict[int, float] = {}  # SELL
        self._known_positions: set[int] = set()
        self._last_report_ts: float = 0.0
        self._watch: list[dict] = []

    # -----------------
    # Public hooks
    # -----------------
    def remember_source_for_ticket(self, ticket: int, source: TradeSource):
        if ticket <= 0:
            return
        m = self._state.setdefault("ticket_source", {})
        m[str(int(ticket))] = {
            "chat_id": source.chat_id,
            "chat_title": source.chat_title,
            "fwd_sender_id": source.fwd_sender_id,
            "fwd_sender_name": source.fwd_sender_name,
        }
        self._save_state()

    def add_touch_watch(
        self,
        *,
        created_ts: float,
        symbol: str,
        action: str,
        sl: float,
        tp: float | None,
        policy_name: str,
        risk_cap_override_cents: float | None,
        levels: list[float],
        tolerance_pips: float,
        expiry_seconds: int,
        source: TradeSource,
    ):
        item = {
            "created_ts": float(created_ts),
            "symbol": str(symbol),
            "action": str(action).upper(),
            "sl": float(sl),
            "tp": float(tp) if tp is not None else None,
            "policy_name": str(policy_name),
            "risk_cap_override_cents": float(risk_cap_override_cents) if risk_cap_override_cents is not None else None,
            "levels": [float(x) for x in levels],
            "tolerance_pips": float(tolerance_pips),
            "expiry_seconds": int(expiry_seconds),
            "source": {
                "chat_id": source.chat_id,
                "chat_title": source.chat_title,
                "fwd_sender_id": source.fwd_sender_id,
                "fwd_sender_name": source.fwd_sender_name,
            },
            "done_levels": [],
        }
        self._watch.append(item)
        logging.info(
            f"[WATCH] Added {symbol} {action} levels={levels} tol={tolerance_pips}p expiry={expiry_seconds}s policy={policy_name}"
        )

    def run_forever(self):
        self._ensure_journal_header()
        self._ensure_report_headers()
        while True:
            try:
                self._tick()
            except Exception as e:
                logging.warning(f"TradeManager error: {e}")
            time.sleep(self.loop_interval_sec)

    # -----------------
    # Core loop
    # -----------------
    def _tick(self):
        self._process_watchlist()
        positions = mt5.positions_get()
        current = {int(p.ticket) for p in positions} if positions else set()

        # Detect closing (positions disappeared)
        closed = self._known_positions - current
        if closed:
            for ticket in list(closed):
                self._on_position_closed(ticket)

        self._known_positions = current

        if not positions:
            return

        # Light UI status (remaining daily limit)
        self._maybe_log_status()

        for p in positions:
            if int(getattr(p, "magic", 0)) != self.magic_number:
                continue

            symbol = str(getattr(p, "symbol", ""))
            pos_type = int(getattr(p, "type", 0))  # 0 buy, 1 sell
            ticket = int(getattr(p, "ticket", 0))
            vol = float(getattr(p, "volume", 0.0) or 0.0)
            entry = float(getattr(p, "price_open", 0.0) or 0.0)
            sl_now = float(getattr(p, "sl", 0.0) or 0.0)
            tp_now = float(getattr(p, "tp", 0.0) or 0.0)

            if ticket <= 0 or vol <= 0 or entry <= 0:
                continue

            info = mt5.symbol_info(symbol)
            tick = mt5.symbol_info_tick(symbol)
            if info is None or tick is None:
                continue

            point = float(info.point)
            digits = int(info.digits)
            pip = point * 10.0  # konsisten dengan legacy

            # Current price for profit calc
            cur = float(tick.bid) if pos_type == 0 else float(tick.ask)
            profit_pips = (cur - entry) / pip if pos_type == 0 else (entry - cur) / pip

            # 1) BEP
            bepth = self._bep_threshold_pips(symbol)
            if profit_pips >= bepth:
                bepsl = self._bep_sl(entry=entry, pos_type=pos_type, pip=pip)
                self._try_modify_sl(
                    ticket=ticket,
                    symbol=symbol,
                    pos_type=pos_type,
                    sl_now=sl_now,
                    tp_now=tp_now,
                    sl_target=bepsl,
                    digits=digits,
                    info=info,
                    tick=tick,
                    reason=f"BEP hit ({profit_pips:.1f}pips)",
                )

    def _process_watchlist(self):
        if not self._watch:
            return
        now = time.time()
        remaining: list[dict] = []
        for w in self._watch:
            age = now - float(w.get("created_ts", now))
            expiry = int(w.get("expiry_seconds", 80))
            if age > expiry:
                logging.info(
                    f"[REJECTED] Watch expired {w.get('symbol')} {w.get('action')} policy={w.get('policy_name')} age={int(age)}s"
                )
                continue

            symbol = str(w.get("symbol"))
            action = str(w.get("action", "")).upper()
            sl = float(w.get("sl", 0.0) or 0.0)
            tp = w.get("tp", None)
            tol_pips = float(w.get("tolerance_pips", 2.0))
            levels = [float(x) for x in (w.get("levels") or [])]
            done = set(float(x) for x in (w.get("done_levels") or []))

            info = mt5.symbol_info(symbol)
            tick = mt5.symbol_info_tick(symbol)
            if info is None or tick is None:
                remaining.append(w)
                continue

            pip = self._pip_size(info)
            tol = tol_pips * pip
            cur = float(tick.ask) if action == "BUY" else float(tick.bid)

            touched_level = None
            for lvl in levels:
                if lvl in done:
                    continue
                if abs(cur - lvl) <= tol:
                    touched_level = lvl
                    break

            if touched_level is None:
                remaining.append(w)
                continue

            # Execute market order on touch
            src_dict = (w.get("source") or {})
            src = TradeSource(
                chat_id=src_dict.get("chat_id"),
                chat_title=src_dict.get("chat_title"),
                fwd_sender_id=src_dict.get("fwd_sender_id"),
                fwd_sender_name=src_dict.get("fwd_sender_name"),
            )

            decision = None
            try:
                if self.risk_agent is not None:
                    decision = self.risk_agent.assess_entry(
                        symbol=symbol,
                        action=action,
                        order_kind="MARKET",
                        entry_price=None,
                        sl=float(sl),
                        risk_cap_override_cents=w.get("risk_cap_override_cents"),
                    )
            except Exception:
                decision = None

            if decision is not None and not decision.ok:
                logging.info(f"[REJECTED] Watch touch but risk blocked: {decision.reason}")
                remaining.append(w)
                continue

            lot = float(getattr(decision, "lot", None) or 0.01)
            # Spread sanity (reject abnormal spread)
            spread_ok, spread_msg = self._spread_ok(symbol)
            if not spread_ok:
                logging.info(f"[REJECTED] Watch blocked: {spread_msg}")
                remaining.append(w)
                continue

            res, err = self._send_market_order(symbol=symbol, action=action, lot=lot, sl=sl, tp=tp)
            if err or res is None or getattr(res, "retcode", None) not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                logging.info(f"[REJECTED] Watch execute failed symbol={symbol} err={err or getattr(res,'comment','')}")
                remaining.append(w)
                continue

            ticket = int(getattr(res, "order", 0) or 0)
            if ticket > 0:
                self.remember_source_for_ticket(ticket, src)
            logging.info(f"[EXECUTE] Watch touched {symbol} {action} level={touched_level} lot={lot}")

            done.add(float(touched_level))
            w["done_levels"] = list(done)
            # keep watching other levels until expiry
            remaining.append(w)

        self._watch = remaining

    def _spread_ok(self, symbol: str) -> tuple[bool, str]:
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            return False, "Tick/info kosong"
        pip = self._pip_size(info)
        if pip <= 0:
            return False, "Pip size invalid"
        spread_pips = (float(tick.ask) - float(tick.bid)) / pip
        sym = (symbol or "").upper()
        # Conservative defaults
        if sym.startswith("XAU") or sym.startswith("XAG"):
            max_pips = 50.0
        elif sym.startswith("BTC") or sym.startswith("ETH"):
            max_pips = 200.0
        else:
            max_pips = 5.0
        if spread_pips > max_pips:
            return False, f"Spread abnormal {spread_pips:.1f}pips > {max_pips:.1f}"
        return True, f"Spread OK {spread_pips:.1f}pips <= {max_pips:.1f}"

    def _pip_size(self, info) -> float:
        point = float(getattr(info, "point", 0.0) or 0.0)
        digits = int(getattr(info, "digits", 0) or 0)
        if digits in (3, 5):
            return point * 10.0
        return point

    def _send_market_order(self, *, symbol: str, action: str, lot: float, sl: float, tp: float | None):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None, "No tick data"

        if action.upper() == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = float(tick.ask)
        elif action.upper() == "SELL":
            order_type = mt5.ORDER_TYPE_SELL
            price = float(tick.bid)
        else:
            return None, "Unknown action"

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lot),
            "type": order_type,
            "price": float(price),
            "deviation": 20,
            "magic": int(self.magic_number),
            "comment": "SniperPowerfulFerdy",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if sl is not None:
            req["sl"] = float(sl)
        if tp is not None:
            req["tp"] = float(tp)

        res = mt5.order_send(req)
        return res, None

    # -----------------
    # Modify SL (anti-spam)
    # -----------------
    def _try_modify_sl(
        self,
        *,
        ticket: int,
        symbol: str,
        pos_type: int,
        sl_now: float,
        tp_now: float,
        sl_target: float,
        digits: int,
        info,
        tick,
        reason: str,
    ):
        now = time.time()
        last = self._last_modify_ts.get(ticket, 0.0)
        if (now - last) < self.min_modify_interval_sec:
            return

        pip = float(info.point) * 10.0
        if sl_now > 0 and abs(sl_target - sl_now) < (self.min_sl_change_pips * pip):
            return

        # Only improve SL directionally
        if pos_type == 0:  # BUY -> SL must go up
            if sl_now > 0 and sl_target <= sl_now:
                return
        else:  # SELL -> SL must go down
            if sl_now > 0 and sl_target >= sl_now:
                return

        # Broker stoplevel safety buffer
        stoplevel_points = float(getattr(info, "stoplevel", 0.0) or 0.0)
        stoplevel_aman = (stoplevel_points + 30.0) * float(info.point)

        if pos_type == 0:
            # SL must be below bid with buffer
            if sl_target >= (float(tick.bid) - stoplevel_aman):
                return
        else:
            # SL must be above ask with buffer
            if sl_target <= (float(tick.ask) + stoplevel_aman):
                return

        sl_final = round(float(sl_target), digits)
        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "sl": float(sl_final),
            "tp": float(tp_now) if tp_now else 0.0,
            "position": int(ticket),
        }
        res = mt5.order_send(req)
        if res is None or getattr(res, "retcode", None) not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            return

        self._last_modify_ts[ticket] = now
        logging.info(f"[MANAGE] {symbol} ticket={ticket} {reason} -> SL={sl_final}")

    # -----------------
    # BEP helpers
    # -----------------
    def _bep_threshold_pips(self, symbol: str) -> float:
        sym = (symbol or "").upper()
        if sym in ("XAUUSDC", "XAGUSDC", "BTCUSDC"):
            return 100.0
        # assume forex
        return 50.0

    def _bep_sl(self, *, entry: float, pos_type: int, pip: float) -> float:
        # Entry +/- 15 pips to cover biaya
        if pos_type == 0:  # BUY
            return float(entry) + (15.0 * pip)
        return float(entry) - (15.0 * pip)

    # -----------------
    # Journal + status
    # -----------------
    def _on_position_closed(self, ticket: int):
        try:
            deals = mt5.history_deals_get(ticket=int(ticket))
        except Exception:
            deals = None
        if not deals:
            return
        deal = deals[-1]
        if getattr(deal, "entry", None) != mt5.DEAL_ENTRY_OUT:
            return
        if int(getattr(deal, "magic", 0)) != self.magic_number:
            return

        symbol = str(getattr(deal, "symbol", ""))
        profit = float(getattr(deal, "profit", 0.0) or 0.0)
        volume = float(getattr(deal, "volume", 0.0) or 0.0)

        src = self._get_source_for_ticket(ticket)
        self._append_journal_row(
            {
                "time_close": int(getattr(deal, "time", int(time.time()))),
                "ticket": int(ticket),
                "symbol": symbol,
                "volume": volume,
                "profit_cents": profit,
                "result": "PROFIT" if profit > 0 else ("LOSS" if profit < 0 else "BEP"),
                "source": src.label(),
            }
        )
        self._maybe_generate_reports()

    # -----------------
    # Reports (daily/weekly/monthly by source)
    # -----------------
    def _ensure_report_headers(self):
        os.makedirs("runtime/reports", exist_ok=True)
        for p in ("runtime/reports/report_daily.csv", "runtime/reports/report_weekly.csv", "runtime/reports/report_monthly.csv"):
            if os.path.exists(p):
                continue
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=[
                        "period",
                        "source",
                        "trades",
                        "wins",
                        "losses",
                        "beps",
                        "winrate_pct",
                        "net_profit_cents",
                    ],
                )
                w.writeheader()

    def _maybe_generate_reports(self):
        # throttle: regenerate at most once per 30 seconds
        now = time.time()
        if (now - self._last_report_ts) < 30.0:
            return
        self._last_report_ts = now
        try:
            rows = self._read_journal_rows()
            self._write_report(rows, mode="daily", out_path="runtime/reports/report_daily.csv")
            self._write_report(rows, mode="weekly", out_path="runtime/reports/report_weekly.csv")
            self._write_report(rows, mode="monthly", out_path="runtime/reports/report_monthly.csv")
            logging.info("[REPORT] Updated daily/weekly/monthly reports.")
        except Exception as e:
            logging.warning(f"Gagal generate report: {e}")

    def _read_journal_rows(self) -> list[dict]:
        if not os.path.exists(self.journal_path):
            return []
        out: list[dict] = []
        with open(self.journal_path, "r", newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    out.append(
                        {
                            "time_close": int(float(row.get("time_close") or 0)),
                            "source": row.get("source") or "unknown",
                            "profit_cents": float(row.get("profit_cents") or 0.0),
                            "result": (row.get("result") or "").upper(),
                        }
                    )
                except Exception:
                    continue
        return out

    def _period_key(self, ts: int, mode: str) -> str:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        if mode == "daily":
            return dt.strftime("%Y-%m-%d")
        if mode == "monthly":
            return dt.strftime("%Y-%m")
        # weekly: ISO week
        iso_year, iso_week, _ = dt.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    def _write_report(self, rows: list[dict], *, mode: str, out_path: str):
        # group by period + source
        agg: dict[tuple[str, str], dict] = {}
        for row in rows:
            ts = int(row["time_close"])
            period = self._period_key(ts, mode)
            src = row["source"]
            key = (period, src)
            a = agg.get(key)
            if a is None:
                a = {
                    "period": period,
                    "source": src,
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "beps": 0,
                    "net_profit_cents": 0.0,
                }
                agg[key] = a

            a["trades"] += 1
            res = row["result"]
            if res == "PROFIT":
                a["wins"] += 1
            elif res == "LOSS":
                a["losses"] += 1
            else:
                a["beps"] += 1
            a["net_profit_cents"] += float(row["profit_cents"])

        # sort newest period first
        items = list(agg.values())
        items.sort(key=lambda x: (x["period"], x["source"]), reverse=True)

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "period",
                    "source",
                    "trades",
                    "wins",
                    "losses",
                    "beps",
                    "winrate_pct",
                    "net_profit_cents",
                ],
            )
            w.writeheader()
            for a in items:
                trades = int(a["trades"])
                wins = int(a["wins"])
                winrate = (wins / trades * 100.0) if trades > 0 else 0.0
                w.writerow(
                    {
                        "period": a["period"],
                        "source": a["source"],
                        "trades": trades,
                        "wins": wins,
                        "losses": int(a["losses"]),
                        "beps": int(a["beps"]),
                        "winrate_pct": round(winrate, 2),
                        "net_profit_cents": round(float(a["net_profit_cents"]), 2),
                    }
                )

    def _get_source_for_ticket(self, ticket: int) -> TradeSource:
        m = (self._state.get("ticket_source") or {}).get(str(int(ticket))) or {}
        return TradeSource(
            chat_id=m.get("chat_id"),
            chat_title=m.get("chat_title"),
            fwd_sender_id=m.get("fwd_sender_id"),
            fwd_sender_name=m.get("fwd_sender_name"),
        )

    def _ensure_journal_header(self):
        os.makedirs(os.path.dirname(self.journal_path), exist_ok=True)
        if os.path.exists(self.journal_path):
            return
        with open(self.journal_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["time_close", "ticket", "symbol", "volume", "profit_cents", "result", "source"],
            )
            w.writeheader()

    def _append_journal_row(self, row: dict):
        try:
            with open(self.journal_path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=["time_close", "ticket", "symbol", "volume", "profit_cents", "result", "source"],
                )
                w.writerow(row)
        except Exception as e:
            logging.warning(f"Gagal tulis journal: {e}")

    def _maybe_log_status(self):
        now = time.time()
        last = float(self._state.get("last_status_ts") or 0.0)
        if (now - last) < 30.0:
            return
        self._state["last_status_ts"] = now
        self._save_state()

        acc = mt5.account_info()
        if acc is None:
            return
        bal = float(getattr(acc, "balance", 0.0) or 0.0)
        eq = float(getattr(acc, "equity", 0.0) or 0.0)
        margin = float(getattr(acc, "margin", 0.0) or 0.0)
        name = getattr(acc, "name", None) or getattr(acc, "login", None)

        remaining = None
        try:
            if self.risk_agent is not None:
                # compute remaining: max_daily - booked - reserved
                risk_cfg = (getattr(self.risk_agent, "cfg", {}) or {}).get("risk") or {}
                max_daily = float(risk_cfg.get("max_daily_loss_cents", 300))
                booked = float(self.risk_agent._get_booked_loss_today_cents(magic=self.magic_number))
                reserved = float(self.risk_agent._get_reserved_risk_open_positions_cents(magic=self.magic_number))
                remaining = max_daily - booked - reserved
        except Exception:
            remaining = None

        if remaining is None:
            logging.info(f"[STATUS] {name} | Bal={bal:.2f}¢ Eq={eq:.2f}¢ Margin={margin:.2f}¢")
        else:
            logging.info(f"[STATUS] {name} | Bal={bal:.2f}¢ Eq={eq:.2f}¢ Margin={margin:.2f}¢ | DailyRemaining={remaining:.2f}¢")

    # -----------------
    # State IO
    # -----------------
    def _load_state(self) -> dict:
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
        except Exception:
            pass
        return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

