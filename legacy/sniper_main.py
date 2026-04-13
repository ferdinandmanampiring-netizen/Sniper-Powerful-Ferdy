import os
import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv
from openai import OpenAI
from sniper_logic import hitung_pagar
from sniper_tele import kirim_pesan

# 1. LOAD DATA RAHASIA
load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY")
MT5_LOGIN = int(os.getenv("MT5_LOGIN"))
MT5_PASS = os.getenv("MT5_PASSWORD")
MT5_SERV = os.getenv("MT5_SERVER")
SYMBOL = os.getenv("MT5_SYMBOL")

client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=API_KEY)

def login_mt5():
    if not mt5.initialize():
        return False
    authorized = mt5.login(MT5_LOGIN, password=MT5_PASS, server=MT5_SERV)
    return authorized

def get_market_data():
    if not login_mt5(): return None
    # Ambil 300 bar untuk akurasi MA200
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 300)
    if rates is None: return None
    
    df = pd.DataFrame(rates)
    
    # --- INDIKATOR TEKNIKAL ---
    df['RSI'] = ta.rsi(df['close'], length=14)
    df['MA50'] = ta.sma(df['close'], length=50)
    df['MA200'] = ta.sma(df['close'], length=200)
    
    last_bar = df.iloc[-1]
    prices_list = [round(p, 2) for p in df['close'].tail(10).tolist()]
    
    return {
        "prices": prices_list,
        "rsi": round(last_bar['RSI'], 2) if not pd.isna(last_bar['RSI']) else 0,
        "ma50": round(last_bar['MA50'], 2),
        "ma200": round(last_bar['MA200'], 2),
        "current_price": round(last_bar['close'], 2)
    }

def sniper_analysis(balance, symbol, tech_data):
    prompt = (f"Analisa {symbol} M15. Harga: {tech_data['prices']}. "
              f"Indikator: RSI={tech_data['rsi']}, MA50={tech_data['ma50']}, MA200={tech_data['ma200']}. "
              f"Saldo {balance} Cent. Tren? Saran WAIT/BUY/SELL.")
    try:
        completion = client.chat.completions.create(
            model="openrouter/auto",
            messages=[{"role": "user", "content": prompt}],
            timeout=15
        )
        return completion.choices[0].message.content
    except:
        return "Mei: Koneksi terputus, gunakan insting Sniper kamu!"

def kirim_order(tipe_order, lot=0.01):
    if not login_mt5(): return None
    symbol_info = mt5.symbol_info(SYMBOL)
    tick = mt5.symbol_info_tick(SYMBOL)
    point = symbol_info.point
    digits = symbol_info.digits
    
    harga = tick.bid if tipe_order == "SELL" else tick.ask
    sl, tp = hitung_pagar(tipe_order, harga, point, digits)
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": float(lot),
        "type": mt5.ORDER_TYPE_SELL if tipe_order == "SELL" else mt5.ORDER_TYPE_BUY,
        "price": harga,
        "sl": sl,
        "tp": tp,
        "magic": 20260408,
        "comment": "Sniper Modular Ferdy",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    return mt5.order_send(request)

# --- MAIN EXECUTION BLOCK (HANYA SATU) ---
if __name__ == "__main__":
    print(f"--- SNIPER INDUK: AKTIF (MODULAR MODE) ---")
    
    if not login_mt5():
        print("❌ LOGIN GAGAL! Periksa .env.")
    else:
        acc = mt5.account_info()
        data_intel = get_market_data()
        
    if data_intel:
        # --- TAMBAHKAN BARIS INI ---
        from sniper_logic import hitung_skor
        skor_final = hitung_skor(data_intel)
        
        if data_intel:
            print(f"✅ Akun: {acc.login} | Saldo: {acc.balance} ¢")
            print(f"📊 DATA: RSI: {data_intel['rsi']} | MA50: {data_intel['ma50']} | MA200: {data_intel['ma200']}")
            
            print("\n--- ANALISA MEI (INTELLIGENCE MODE) ---")
            print(sniper_analysis(acc.balance, SYMBOL, data_intel))
            
            lot_trial = 0.01
            print(f"\n🛡️ SIAP TEMBAK: {lot_trial} Lot Cent (Auto SL/TP Active)")
            pilihan = input("Ketik 'BUY' atau 'SELL' untuk eksekusi (atau 'X' untuk batal): ").upper()
            
            if pilihan in ["BUY", "SELL"]:
                print(f"🚀 Eksekusi {pilihan}...")
                res = kirim_order(pilihan, lot_trial)
                
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    print(f"🎯 BERHASIL! Harga: {res.price} | SL: {res.request.sl} | TP: {res.request.tp}")
                    msg = (f"🚀 *PELURU MELUNCUR!*\n━━━━━━━━━━━━━━━\n"
                           f"📍 Pair: `{SYMBOL}`\n🛠 Type: *{pilihan}*\n💰 Price: `{res.price}`\n"
                           f"🛡 SL: `{res.request.sl}`\n🎯 TP: `{res.request.tp}`\n━━━━━━━━━━━━━━━\n"
                           f"Status: *Posisi Terbuka Aman!*")
                    kirim_pesan(msg)
                else:
                    err_msg = res.comment if res else 'Error Koneksi'
                    print(f"❌ GAGAL: {err_msg}")
            else:
                print("Operation Cancelled.")
