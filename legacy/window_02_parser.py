import json
import re
import os
import time

# Konfigurasi File
RAW_SIGNAL_FILE = "raw_signal.txt"
PARSED_SIGNAL_FILE = "signal_queue.json"

def clean_text(text):
    """Membersihkan emoji dan karakter aneh agar regex lebih akurat."""
    # Menghapus emoji/karakter non-alphanumeric kecuali titik dan spasi
    return re.sub(r'[^\w\s\.]', ' ', text)

def parse_telegram_text(text):
    original_text = text.upper()
    text = clean_text(original_text)
    print(f"[*] Parsing Teks: {text[:60]}...")

    # 1. Identifikasi Action & Layering (More Buy/Sell)
    action = None
    is_layering = False
    
    if "MORE BUY" in text or "ADD BUY" in text:
        action = "BUY"
        is_layering = True
    elif "MORE SELL" in text or "ADD SELL" in text:
        action = "SELL"
        is_layering = True
    elif "BUY LIMIT" in text: action = "BUY_LIMIT"
    elif "SELL LIMIT" in text: action = "SELL_LIMIT"
    elif "BUY" in text: action = "BUY"
    elif "SELL" in text: action = "SELL"

    # 2. Identifikasi Symbol
    symbol = "XAUUSD" if any(x in text for x in ["XAUUSD", "GOLD"]) else None

    # 3. Ekstraksi Harga (Regex yang lebih kuat)
    # Mencari angka 4 digit (khas Gold)
    prices = re.findall(r"(\d{4}(?:\.\d+)?)", text)
    
    if not action or not symbol or not prices:
        return None

    data = {
        "symbol": symbol,
        "action": action,
        "is_layering": is_layering,
        "entry": None,
        "sl": None,
        "tp": None,
        "raw_msg": original_text[:100]
    }

    # Logika Range Entry (Contoh: 2040 - 2042)
    # Jika ada dua angka berdekatan di awal, kita ambil rata-ratanya
    range_match = re.search(r"(\d{4})\s*-\s*(\d{4})", text)
    if range_match:
        p1, p2 = float(range_match.group(1)), float(range_match.group(2))
        data["entry"] = (p1 + p2) / 2
    else:
        data["entry"] = float(prices[0])

    # Cari SL (Mencari angka setelah keyword SL)
    sl_match = re.search(r"SL\s*(\d{4}(?:\.\d+)?)", text)
    if sl_match:
        data["sl"] = float(sl_match.group(1))

    # Cari TP (Mencari TP1 atau TP pertama yang ditemukan)
    tp_match = re.search(r"TP\s*(?:1)?\s*(\d{4}(?:\.\d+)?)", text)
    if tp_match:
        data["tp"] = float(tp_match.group(1))

    return data

def main():
    print("🧠 Window 02 (The Parser) Active... Monitoring raw_signal.txt")
    while True:
        if os.path.exists(RAW_SIGNAL_FILE):
            try:
                with open(RAW_SIGNAL_FILE, "r") as f:
                    raw_content = f.read()
                
                # Hapus file mentah agar tidak diproses ulang
                os.remove(RAW_SIGNAL_FILE)
                
                parsed_result = parse_telegram_text(raw_content)
                
                # VALIDASI KRUSIAL: No SL, No Trade
                if parsed_result and parsed_result["sl"]:
                    with open(PARSED_SIGNAL_FILE, "w") as f:
                        json.dump(parsed_result, f, indent=4)
                    print(f"✅ Parsed: {parsed_result['action']} {parsed_result['symbol']} @{parsed_result['entry']} SL: {parsed_result['sl']}")
                else:
                    reason = "SL tidak ditemukan" if parsed_result else "Format tidak dikenali"
                    print(f"❌ Sinyal Ditolak: {reason}")
                    
            except Exception as e:
                print(f"⚠️ Error: {e}")
        
        time.sleep(0.5) # Respon lebih cepat

if __name__ == "__main__":
    main()
