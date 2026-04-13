import time
import os
import MetaTrader5 as mt5
from dotenv import load_dotenv
from sniper_logic import auto_trailing_dan_bep
from sniper_tele import kirim_pesan

load_dotenv()
SYMBOL = os.getenv("MT5_SYMBOL")
MAGIC_NUMBER = 20260408

def cek_posisi_tertutup(tiket_list):
    """Mengecek history untuk posisi yang baru saja tertutup"""
    # Ambil history 10 menit terakhir agar tidak terlewat
    from_date = time.time() - 600 
    history = mt5.history_deals_get(from_date, time.time() + 100)
    
    if history:
        for deal in history:
            # Pastikan deal ini adalah penutupan (OUT) dari tiket yang kita pantau
            if deal.ticket in tiket_list and (deal.entry == mt5.DEAL_ENTRY_OUT):
                profit = deal.profit
                symbol = deal.symbol
                volume = deal.volume
                
                # RUMUS PIPS GOLD CENT: (Profit / (Volume * 100))
                # Contoh: Profit 2.00 Cent, Volume 0.01 -> 2.00 / (0.01 * 100) = 2.0 Pips
                pips = profit / (volume * 100) if volume > 0 else 0
                
                status = "💰 PROFIT HIT!" if profit > 0 else "🛡️ SL/BEP HIT"
                emoji = "✅" if profit > 0 else "⚠️"
                
                msg = (f"{emoji} *TRADE CLOSED REPORT*\n"
                       f"━━━━━━━━━━━━━━━\n"
                       f"📍 Pair: `{symbol}`\n"
                       f"🎫 Ticket: `{deal.ticket}`\n"
                       f"📈 Pips: `{pips:.1f} Pips`\n" # REVISI: Sekarang ada Pips
                       f"💵 Result: `{profit} Cent`\n"
                       f"📝 Status: *{status}*\n"
                       f"━━━━━━━━━━━━━━━\n"
                       f"Mei: Target selesai, Ferdy. Siap untuk instruksi selanjutnya!")
                kirim_pesan(msg)

def start_guardian():
    if not mt5.initialize():
        print("❌ Guardian Gagal Inisialisasi!")
        return

    print(f"🛡️ GUARDIAN AKTIF - Memantau {SYMBOL}...")
    
    # Inisialisasi daftar tiket yang ada saat startup agar tidak dianggap 'tertutup' saat baru jalan
    positions_start = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    tiket_terpantau = {p.ticket for p in positions_start} if positions_start else set()
    
    try:
        while True:
            # 1. Ambil posisi terbuka sekarang
            current_positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
            current_tickets = {p.ticket for p in current_positions} if current_positions else set()
            
            # 2. DETEKSI CLOSING
            tiket_tertutup = tiket_terpantau - current_tickets
            if tiket_tertutup:
                print(f"🔍 Mendeteksi {len(tiket_tertutup)} posisi tertutup. Mengirim laporan...")
                cek_posisi_tertutup(list(tiket_tertutup))
            
            # 3. UPDATE DAFTAR PANTAUAN (Sangat Penting!)
            # Sinkronkan agar tiket yang baru dibuka masuk ke daftar pantau, 
            # dan tiket yang sudah dilaporkan tertutup keluar dari daftar.
            tiket_terpantau = current_tickets

            # 4. JALANKAN TRAILING: Hanya jika ada posisi aktif
            if current_tickets:
                auto_trailing_dan_bep(SYMBOL, MAGIC_NUMBER, trigger_bep=100)
            
            time.sleep(1) # Cek setiap detik
            
    except KeyboardInterrupt:
        print("🛡️ Guardian dimatikan.")
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    start_guardian()
