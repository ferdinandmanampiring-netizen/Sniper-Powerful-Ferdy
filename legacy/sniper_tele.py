import os
import requests
from dotenv import load_dotenv

# WAJIB ADA AGAR .ENV TERBACA
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def kirim_pesan(pesan):
    if not TOKEN or not CHAT_ID:
        print("⚠️ Data di .env belum lengkap, Ferdy!")
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": pesan,
        "parse_mode": "Markdown"
    }
    
    try:
        r = requests.post(url, json=payload)
        if r.status_code == 200:
            print("✅ BERHASIL! Cek HP kamu sekarang!")
        else:
            print(f"❌ Telegram Error: {r.text}")
    except Exception as e:
        print(f"❌ Koneksi Gagal: {e}")

if __name__ == "__main__":
    kirim_pesan("Halo Bos Ferdy! Laporan Sniper Powerful siap dilaksanakan! 🚀")
