import re
import logging

class ParserAgent:
    def __init__(self):
        # Regex diperkuat untuk menangani variasi symbol, action, dan emoji
        self._symbol_regex = re.compile(r"\b(XAUUSD|GOLD|BTCUSD|EURUSD|GBPJPY)\b", re.I)
        self._action_regex = re.compile(r"\b(BUY|SELL|LONG|SHORT)\b", re.I)
        
        # Regex khusus untuk menangkap angka di tengah gangguan karakter (.., emoji, dll)
        self._price_pattern = re.compile(r"([\d]+(?:\.[\d]+)?)")
        self._range_pattern = re.compile(r"([\d]+(?:\.[\d]+)?)\s*-\s*([\d]+(?:\.[\d]+)?)")

    def _safe_float(self, raw_string):
        """Membersihkan string dari karakter non-angka dan titik sebelum konversi."""
        if not raw_string: return None
        try:
            # Hanya ambil angka dan titik pertama yang ditemukan
            clean = re.sub(r"[^0-9.]", "", raw_string)
            if not clean: return None
            # Menangani kasus double dot '..'
            if "." in clean:
                parts = clean.split(".")
                clean = parts[0] + "." + "".join(parts[1:])
            return float(clean)
        except:
            return None

    def _apply_cents_suffix(self, symbol: str, suffix: str = "c") -> str:
        if not symbol:
            return symbol
        sym = symbol.strip()
        if suffix and not sym.upper().endswith(suffix.upper()):
            return f"{sym}{suffix}"
        return sym

    def parse_signal(self, text):
        text_upper = text.upper()
        # Normalisasi karakter superskrip TP ke angka biasa
        text_upper = text_upper.replace("¹", "1").replace("²", "2").replace("³", "3").replace("⁴", "4")
        
        data = {
            "symbol": "XAUUSDc",
            "action": None,
            "order_kind": None,   # "MARKET" | "LIMIT"
            "entry": None,
            "entry_zone": None,   # (low, high)
            "sl": None,
            "tp": None,           # tp pertama (untuk eksekusi simpel)
            "tps": [],            # list TP (float) sesuai urutan yang muncul
            "raw_upper": text_upper,
            "raw": text,
        }

        # 1. Deteksi Symbol (Default XAUUSDc jika tidak ada)
        sym_match = self._symbol_regex.search(text_upper)
        if sym_match:
            raw_sym = sym_match.group(1)
            if raw_sym == "GOLD":
                data["symbol"] = "XAUUSD"
            else:
                data["symbol"] = raw_sym

        # Apply suffix cents secara konsisten
        data["symbol"] = self._apply_cents_suffix(data["symbol"], suffix="c")

        # 2. Deteksi Action
        if "BUY" in text_upper: data["action"] = "BUY"
        elif "SELL" in text_upper: data["action"] = "SELL"

        # 3. Ekstraksi Harga (Mencari Label SL, TP, Entry/Entry Zone)
        lines = text_upper.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Abaikan baris "LIMIT LOT ..." agar tidak mengganggu parsing angka (policy Deity)
            # (Filter spesifik policy dilakukan di orchestrator, tapi ini aman untuk umum)
            if "LIMIT LOT" in line:
                continue

            # Entry Zone (Alpha Institute: "Entry Zone: A - B")
            if "ENTRY ZONE" in line or "NOW AT" in line:
                rm = self._range_pattern.search(line)
                if rm:
                    lo = self._safe_float(rm.group(1))
                    hi = self._safe_float(rm.group(2))
                    if lo is not None and hi is not None:
                        data["entry_zone"] = (min(lo, hi), max(lo, hi))
                        data["order_kind"] = "LIMIT"

            # Zone entry tanpa label (contoh: BUY 4444-4442, BUY LIMIT 4563-4564)
            if data["entry_zone"] is None and ("BUY" in line or "SELL" in line):
                rm2 = self._range_pattern.search(line)
                if rm2:
                    lo = self._safe_float(rm2.group(1))
                    hi = self._safe_float(rm2.group(2))
                    if lo is not None and hi is not None:
                        data["entry_zone"] = (min(lo, hi), max(lo, hi))
                        data["order_kind"] = "LIMIT"

            # Parsing SL (Menangani SL.., SL... atau emoji)
            if "SL" in line or "STOP LOSS" in line or "⛔" in line:
                nums = self._price_pattern.findall(line)
                if nums:
                    # ambil angka terakhir biasanya value SL
                    data["sl"] = self._safe_float(nums[-1])
            
            # Parsing TP (Mengambil TP1 atau TP pertama)
            if any(x in line for x in ["TP", "TAKE PROFIT", "🎯"]):
                if "PIPS" not in line:
                    prices = self._price_pattern.findall(line)
                    # Beberapa format menulis "TP1: 1234" atau "Take Profit 1 : 1234"
                    # sehingga angka pertama sering indeks. Ambil angka non-indeks pertama setelah label.
                    for i, p in enumerate(prices):
                        val = self._safe_float(p)
                        if val is None:
                            continue
                        is_small_index = val in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0)
                        label_has_index = bool(re.search(r"\b(TP|TAKE\s*PROFIT)\s*%s\b" % int(val), line))
                        if is_small_index and label_has_index:
                            # skip index, next number should be price
                            continue
                        # Jika angka pertama kecil dan line mengandung "TAKE PROFIT", biasanya itu index
                        if i == 0 and is_small_index and ("TAKE PROFIT" in line or re.search(r"\bTP\s*1\b", line)):
                            continue
                        if data["tp"] is None:
                            data["tp"] = val
                        # simpan semua TP yang berhasil ditemukan (urutan sesuai teks)
                        if val is not None:
                            data["tps"].append(val)
                            break

            # Parsing Entry
            if "ENTRY" in line or "@" in line:
                m = self._price_pattern.search(re.split(r"ENTRY|@", line)[-1])
                if m:
                    data["entry"] = self._safe_float(m.group(1))
                    if data["entry"] is not None:
                        data["order_kind"] = data["order_kind"] or "LIMIT"

        # Entry fallback: jika ada entry_zone tapi belum ada entry, ambil midpoint
        if data["entry"] is None and data["entry_zone"]:
            lo, hi = data["entry_zone"]
            data["entry"] = (lo + hi) / 2.0

        if data["action"]:
            logging.info(f"Parser OK: {data['symbol']} {data['action']} | Entry: {data['entry']} | SL: {data['sl']} | TP: {data['tp']}")
            return data
        
        return None

    def split_multi_signals(self, raw_text: str) -> list[str]:
        """
        Memecah pesan yang berisi beberapa signal dipisah 'ATAU'/'Atau' menjadi beberapa payload.
        Default: split berbasis kata 'ATAU' sebagai delimiter.
        """
        if not raw_text:
            return []
        parts = re.split(r"\bATAU\b", raw_text, flags=re.IGNORECASE)
        out = [p.strip() for p in parts if p and p.strip()]
        return out if out else [raw_text]
