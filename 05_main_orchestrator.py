import asyncio
import os
import re
import logging
import logging.handlers
import time
import colorama
from colorama import Fore, Style
from dotenv import load_dotenv
from pathlib import Path
import yaml
from agents.telegram_agent import TelegramAgent
from agents.parser_agent import ParserAgent
from agents.logic_agent import LogicAgent
from agents.data_agent import DataAgent
from agents.risk_agent import RiskAgent
from agents.trade_manager import TradeManager, TradeSource
from agents.policy_agent import PolicyAgent
import MetaTrader5 as mt5

colorama.init()

# Anti-Locked: bersihkan journal file sesi Telethon jika ada konflik
if os.path.exists("sniper_session.session-journal"):
    try:
        os.remove("sniper_session.session-journal")
    except:
        pass

# Custom Formatter untuk tampilan lebih "Pro"
class SniperFormatter(logging.Formatter):
    def format(self, record):
        message = record.getMessage()
        log_fmt = f"{Fore.CYAN}%(asctime)s{Style.RESET_ALL} - "
        if "Parser OK" in message:
            log_fmt += f"{Fore.GREEN}[SUCCESS]{Style.RESET_ALL} "
        elif "REJECTED" in message:
            log_fmt += f"{Fore.RED}[REJECTED]{Style.RESET_ALL} "
        else:
            log_fmt += f"{Fore.YELLOW}[INFO]{Style.RESET_ALL} "

        log_fmt += "%(message)s"
        formatter = logging.Formatter(log_fmt, datefmt="%H:%M:%S")
        return formatter.format(record)

handler = logging.StreamHandler()
handler.setFormatter(SniperFormatter())
os.makedirs("runtime", exist_ok=True)
file_handler = logging.handlers.RotatingFileHandler(
    filename=os.path.join("runtime", "sniper.log"),
    maxBytes=2_000_000,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

logging.getLogger().handlers = [handler, file_handler]
logging.getLogger().setLevel(logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

class SniperHub:
    def __init__(self):
        self.cfg = self._load_settings()
        self.trading_cfg = (self.cfg.get("trading_params") or {})
        self.parser = ParserAgent()
        self.logic = LogicAgent(self.cfg)
        self.policy_agent = PolicyAgent()
        self.risk: RiskAgent | None = None
        self.trade_manager: TradeManager | None = None
        self.tele: TelegramAgent | None = None
        self.data = DataAgent()
        self._mt5_ready = False
        self.risk = RiskAgent(self.cfg)
        self.trade_manager = TradeManager(
            magic_number=int((self.cfg.get("risk") or {}).get("magic_number", 260410)),
            risk_agent=self.risk,
        )
        self.magic_number = int((self.cfg.get("risk") or {}).get("magic_number", 260410))
        self._dedup: dict[str, float] = {}

    def _count_open_positions(self, symbol: str) -> int:
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            return 0
        count = 0
        for p in positions:
            try:
                if int(getattr(p, "magic", 0)) != self.magic_number:
                    continue
                count += 1
            except Exception:
                continue
        return count

    def _load_settings(self) -> dict:
        base_dir = Path(__file__).resolve().parent
        settings_path = base_dir / "config" / "settings.yaml"
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logging.warning(f"Gagal baca settings.yaml: {e}")
            return {}

    def _allowed_symbols_set(self) -> set[str] | None:
        """
        Manual symbol filter (human-controlled via settings.yaml).
        If config lists are missing/empty, returns None (no filtering).
        """
        mode = str(self.trading_cfg.get("mode", "normal")).strip().lower()
        if mode == "weekend":
            syms = self.trading_cfg.get("allowed_symbols_weekend") or []
        else:
            syms = self.trading_cfg.get("allowed_symbols_normal") or []
        syms = [str(s).strip() for s in syms if str(s).strip()]
        if not syms:
            return None
        return {s.upper() for s in syms}

    def _pip_size_for_symbol(self, symbol: str) -> float | None:
        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        point = float(getattr(info, "point", 0.0) or 0.0)
        digits = int(getattr(info, "digits", 0) or 0)
        if digits in (3, 5):
            return point * 10.0
        return point

    def _max_spread_pips(self, symbol: str) -> float:
        sym = (symbol or "").upper()
        if sym.startswith("XAU") or sym.startswith("XAG"):
            return float(self.trading_cfg.get("max_spread_pips_metals", 50))
        if sym.startswith("BTC") or sym.startswith("ETH"):
            return float(self.trading_cfg.get("max_spread_pips_crypto", 200))
        return float(self.trading_cfg.get("max_spread_pips_forex", 5))

    def _spread_ok(self, symbol: str) -> tuple[bool, str]:
        if not bool(self.trading_cfg.get("spread_filter_enabled", True)):
            return True, "Spread filter off"
        tick = self.data.get_tick(symbol)
        if tick is None:
            return False, "Tick kosong (spread unknown)"
        pip = self._pip_size_for_symbol(symbol)
        if pip is None or pip <= 0:
            return False, "Pip size unknown"
        spread_pips = (float(tick.ask) - float(tick.bid)) / pip
        max_pips = self._max_spread_pips(symbol)
        if spread_pips > max_pips:
            return False, f"Spread abnormal {spread_pips:.1f}pips > {max_pips:.1f}"
        return True, f"Spread OK {spread_pips:.1f}pips <= {max_pips:.1f}"

    def _dedup_key(self, *, source: TradeSource, raw_text: str) -> str:
        # Normalize whitespace + uppercase to reduce duplicates
        base = " ".join((raw_text or "").upper().split())
        return f"{source.chat_id}|{source.fwd_sender_id}|{base}"

    def _is_duplicate(self, *, source: TradeSource, raw_text: str) -> bool:
        window = float(self.trading_cfg.get("duplicate_window_seconds", 120))
        if window <= 0:
            return False
        now = time.time()
        # cleanup
        for k, ts in list(self._dedup.items()):
            if (now - ts) > window:
                self._dedup.pop(k, None)
        key = self._dedup_key(source=source, raw_text=raw_text)
        ts = self._dedup.get(key)
        if ts is not None and (now - ts) <= window:
            return True
        self._dedup[key] = now
        return False

    def _sanity_ok(self, *, symbol: str, action: str | None, sl: float | None, tp: float | None) -> tuple[bool, str]:
        if sl is None:
            return False, "SL kosong"
        # require TP to avoid 'TP aneh/kosong' execution
        if tp is None:
            return False, "TP kosong"

        tick = self.data.get_tick(symbol)
        if tick is None:
            return False, "Tick kosong (sanity unknown)"
        cur = float(tick.ask) if (action or "").upper() == "BUY" else float(tick.bid)
        if cur <= 0:
            return False, "Harga sekarang invalid"

        # Reject obviously wrong absolute values for metals
        sym = symbol.upper()
        if (sym.startswith("XAU") or sym.startswith("XAG")) and float(sl) < 1000.0:
            return False, f"SL aneh untuk metals: {sl}"

        # Reject too-far levels (>20% away) - catches extreme typos
        for name, lvl in (("SL", float(sl)), ("TP", float(tp))):
            if lvl <= 0:
                return False, f"{name} invalid"
            if abs(lvl - cur) / cur > 0.2:
                return False, f"{name} aneh (terlalu jauh dari harga sekarang)"

        # Direction check if action known
        act = (action or "").upper()
        if act == "BUY" and float(sl) >= cur:
            return False, "SL BUY harus di bawah harga"
        if act == "SELL" and float(sl) <= cur:
            return False, "SL SELL harus di atas harga"

        return True, "Sanity OK"

    def pre_flight_check(self):
        logging.info("Memulai Pre-Flight Check (Self-Learning)...")
        sample_path = 'knowledge/signal_samples.txt'
        
        if not os.path.exists(sample_path):
            logging.warning("File samples tidak ditemukan.")
            return
        

        with open(sample_path, 'r', encoding='utf-8') as f:
            content = f.read()
            # Split berdasarkan kata "ATAU" (case insensitive)
            samples = re.split(r'\bATAU\b', content, flags=re.IGNORECASE)
            
            success = 0
            fail = 0
            for s in samples:
                clean_sample = s.strip()
                if clean_sample and "ID:" not in clean_sample.upper():  # Abaikan header info
                    result = self.parser.parse_signal(clean_sample)
                    if result:
                        success += 1
                    else:
                        fail += 1
            
            logging.info(f"Check Selesai! Berhasil: {success} | Gagal: {fail}")

    def _ensure_mt5(self) -> bool:
        if self._mt5_ready:
            return True
        logging.info("Menghubungkan ke MT5...")
        try:
            self._mt5_ready = bool(self.data.connect())
        except Exception as e:
            logging.exception(f"Exception saat connect MT5: {e}")
            self._mt5_ready = False
        return self._mt5_ready

    async def handle_new_signal(self, payload):
        """Alur Kerja Utama saat ada pesan Telegram masuk"""
        logging.info("Menerima pesan baru dari Telegram.")

        if isinstance(payload, dict):
            raw_text = payload.get("raw_text") or ""
            received_ts = float(payload.get("received_ts") or 0.0) or time.time()
            source = TradeSource(
                chat_id=payload.get("chat_id"),
                chat_title=payload.get("chat_title"),
                fwd_sender_id=payload.get("fwd_sender_id"),
                fwd_sender_name=payload.get("fwd_sender_name"),
            )
        else:
            raw_text = str(payload or "")
            received_ts = time.time()
            source = TradeSource()
        
        policy = self.policy_agent.get_policy(chat_id=source.chat_id, fwd_sender_id=source.fwd_sender_id)

        # Split multi-signal if needed
        chunks = self.parser.split_multi_signals(raw_text) if policy.split_on_atau else [raw_text]

        # 2. Pastikan MT5 siap (sekali per pesan)
        if not self._ensure_mt5():
            logging.warning("MT5 belum siap. Sinyal ditunda/ditolak sementara.")
            return

        # Anti-duplicate (whole message level)
        if self._is_duplicate(source=source, raw_text=raw_text):
            logging.info("[REJECTED] Duplicate signal (recent).")
            return

        for idx, chunk in enumerate(chunks, start=1):
            # 3. Parsing
            parsed_data = self.parser.parse_signal(chunk)
            if not parsed_data:
                logging.info(f"[REJECTED] Chunk[{idx}/{len(chunks)}] tidak ter-parse sebagai sinyal.")
                continue

            # Apply TP policy
            if policy.allowed_tp_indices:
                tps = parsed_data.get("tps") or []
                chosen = None
                for tp_idx in sorted(policy.allowed_tp_indices):
                    if tp_idx - 1 < len(tps):
                        chosen = tps[tp_idx - 1]
                        break
                parsed_data["tp"] = chosen

            symbol = parsed_data["symbol"]
            action = parsed_data.get("action")
            sl = parsed_data.get("sl")
            tp = parsed_data.get("tp")

            logging.info(
                f"Sinyal ter-parse[{idx}/{len(chunks)}] policy={policy.name}: {symbol} {action} | "
                f"Entry: {parsed_data.get('entry')} | SL: {sl} | TP: {tp}"
            )

            allowed = self._allowed_symbols_set()
            if allowed is not None and symbol.upper() not in allowed:
                logging.info(
                    f"[REJECTED] Symbol disabled by config: {symbol}. mode={self.trading_cfg.get('mode','normal')} allowed={sorted(list(allowed))}"
                )
                continue

            # Symbol availability
            if not self.data.ensure_symbol(symbol):
                logging.warning(f"Symbol tidak tersedia/visible di MT5: {symbol}. Sinyal ditolak.")
                continue

            ok_spread, spread_msg = self._spread_ok(symbol)
            if not ok_spread:
                logging.info(f"[REJECTED] {spread_msg}")
                continue

            ok_sanity, sanity_msg = self._sanity_ok(symbol=symbol, action=action, sl=sl, tp=tp)
            if not ok_sanity:
                logging.info(f"[REJECTED] {sanity_msg}")
                continue

            # Slot gate: XAUUSDc max 2 open
            if symbol.upper() == "XAUUSDC":
                open_count = self._count_open_positions(symbol)
                if open_count >= 2:
                    logging.info(f"[REJECTED] Slot XAUUSDc penuh (open={open_count}/2). Tunggu ada posisi closing.")
                    continue

            # Ghost mode: jika entry berupa zone area, selalu pakai watch (no pending) untuk semua provider
            if parsed_data.get("entry_zone"):
                if not action or sl is None:
                    logging.info("[REJECTED] Policy touch-entry but missing action/SL.")
                    continue

                lo, hi = parsed_data["entry_zone"]
                lo = float(lo)
                hi = float(hi)
                mid = (lo + hi) / 2.0
                levels = [lo, mid, hi]

                if self.trade_manager is not None:
                    self.trade_manager.add_touch_watch(
                        created_ts=float(received_ts),
                        symbol=symbol,
                        action=action,
                        sl=float(sl),
                        tp=float(tp) if tp is not None else None,
                        policy_name=policy.name or "ghost_zone",
                        risk_cap_override_cents=policy.risk_cap_cents_override,
                        levels=levels,
                        tolerance_pips=float(getattr(policy, "touch_tolerance_pips", 2.0) or 2.0),
                        expiry_seconds=int(getattr(policy, "touch_expiry_seconds", 80) or 80),
                        source=source,
                    )
                    logging.info("[READY] Ghost watch queued (zone). No pending order will be placed.")
                else:
                    logging.info("[REJECTED] TradeManager belum siap untuk watchlist.")
                continue

            # Market data for voting
            market_df = self.data.get_data(symbol, mt5.TIMEFRAME_M5, count=1000)
            if market_df is None or market_df.empty:
                logging.warning("Gagal ambil market data dari MT5. Sinyal ditolak.")
                continue
            if "close" not in market_df.columns:
                logging.warning("Market data MT5 tidak memiliki kolom 'close'. Sinyal ditolak.")
                continue

            # Voting filter
            is_valid, message = self.logic.validate_signal(parsed_data, market_df)
            if not is_valid:
                logging.info(f"[REJECTED] {message}")
                continue

            logging.info(f"[READY TO EXECUTE] {message}")

            # SL required
            if sl is None:
                logging.info("[REJECTED] SL tidak ditemukan di pesan. Tidak dieksekusi demi keamanan.")
                continue

            dry_run = bool(self.trading_cfg.get("dry_run", True))
            entry = parsed_data.get("entry")
            order_kind = parsed_data.get("order_kind") or ("LIMIT" if entry is not None else "MARKET")

            # Risk gate + CalculateLot
            decision = self.risk.assess_entry(
                symbol=symbol,
                action=action,
                order_kind=order_kind,
                entry_price=entry,
                sl=float(sl),
                risk_cap_override_cents=policy.risk_cap_cents_override,
            )
            if not decision.ok:
                logging.info(f"[REJECTED] Risk: {decision.reason}")
                continue

            lot = float(decision.lot or float(self.trading_cfg.get("lot", 0.01)))

            if dry_run:
                logging.info(
                    f"[DRY_RUN] Akan eksekusi {order_kind} {symbol} {action} lot={lot} entry={entry} sl={sl} tp={tp} "
                    f"(risk={decision.risk_cents}¢ remaining={decision.remaining_cents}¢)"
                )
                continue

            # No pending order globally:
            # if an entry price exists (limit-style), queue a single-level watch and wait up to 80s.
            if entry is not None and order_kind == "LIMIT":
                if self.trade_manager is None:
                    logging.info("[REJECTED] TradeManager belum siap untuk ghost entry.")
                    continue
                self.trade_manager.add_touch_watch(
                    created_ts=float(received_ts),
                    symbol=symbol,
                    action=action,
                    sl=float(sl),
                    tp=float(tp) if tp is not None else None,
                    policy_name=f"{policy.name or 'ghost'}:single_entry",
                    risk_cap_override_cents=policy.risk_cap_cents_override,
                    levels=[float(entry)],
                    tolerance_pips=2.0,
                    expiry_seconds=80,
                    source=source,
                )
                logging.info("[READY] Ghost watch queued (single entry). No pending order will be placed.")
                continue

            # Execute order (market now)
            if order_kind == "LIMIT" and entry is not None:
                tick = self.data.get_tick(symbol)
                if tick is None:
                    logging.info("[REJECTED] Tick MT5 kosong. Tidak bisa eksekusi.")
                    continue

                ask = float(tick.ask)
                bid = float(tick.bid)
                if action == "BUY" and float(entry) >= ask:
                    order_kind = "MARKET"
                elif action == "SELL" and float(entry) <= bid:
                    order_kind = "MARKET"

            if order_kind == "LIMIT" and entry is not None:
                result, err = self.data.send_limit_order(symbol, action, lot, float(entry), sl, tp)
            else:
                result, err = self.data.send_market_order(symbol, action, lot, sl, tp)

            if err:
                logging.warning(f"Order gagal: {err}")
                continue
            if result is None:
                logging.warning("Order gagal: result kosong.")
                continue
            if getattr(result, "retcode", None) not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                logging.warning(f"Order ditolak MT5. retcode={result.retcode} comment={getattr(result, 'comment', '')}")
                continue

            logging.info(f"[ORDER_OK] retcode={result.retcode} order={getattr(result, 'order', None)} deal={getattr(result, 'deal', None)}")
            try:
                ticket = int(getattr(result, "order", 0) or 0)
                if ticket > 0 and self.trade_manager is not None:
                    self.trade_manager.remember_source_for_ticket(ticket, source)
            except Exception:
                pass

    async def start(self):
        self.pre_flight_check()
        logging.info("Menyalakan listener Telegram (live mode)...")
        # Inisialisasi Telegram setelah pre-flight (agar pre-flight tetap bisa jalan walau kredensial belum siap)
        try:
            self.tele = TelegramAgent()
        except ValueError as e:
            logging.error(str(e))
            logging.error("Pastikan TELEGRAM_API_ID dan TELEGRAM_API_HASH sudah terisi di file .env.")
            return
        # Jalankan Telegram Listener dengan callback ke handle_new_signal
        # Jalankan TradeManager loop (BEP, trailing, journal, UI status)
        try:
            # Ensure MT5 is up early so manager can read positions safely
            self._ensure_mt5()
            if self.trade_manager is not None:
                asyncio.create_task(asyncio.to_thread(self.trade_manager.run_forever))
        except Exception as e:
            logging.warning(f"TradeManager gagal start: {e}")

        await self.tele.start_listening(self.handle_new_signal)

if __name__ == "__main__":
    hub = SniperHub()
    try:
        asyncio.run(hub.start())
    except KeyboardInterrupt:
        logging.info("Sistem dimatikan secara aman oleh Creator.")