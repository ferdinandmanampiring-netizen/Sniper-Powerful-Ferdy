import os
import MetaTrader5 as mt5
from dotenv import dotenv_values
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.request import HTTPXRequest
import sniper_main as sm

config = dotenv_values(".env")
TOKEN_BOT = config.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = config.get("TELEGRAM_CHAT_ID")

# Tracking posisi untuk laporan Closing
posisi_aktif = set()

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not mt5.initialize(): return
    acc = mt5.account_info()
    data = sm.get_market_data()
    keyboard = [[InlineKeyboardButton("🚀 BUY", callback_data='DOR_BUY'), InlineKeyboardButton("📉 SELL", callback_data='DOR_SELL')]]
    msg = f"📊 *STATUS*\nBalance: `{acc.balance}`\nRSI: `{data['rsi']:.2f}`"
    if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def monitor_closing_otomatis(context: ContextTypes.DEFAULT_TYPE):
    """MEI LAPOR SAAT POSISI TUTUP (HANYA TELEGRAM, TIDAK KE EXNESS)"""
    global posisi_aktif
    if not mt5.initialize(): return
    
    positions = mt5.positions_get()
    current_tickets = {p.ticket for p in positions} if positions else set()
    
    # Deteksi Closing
    for ticket in list(posisi_aktif):
        if ticket not in current_tickets:
            import datetime
            # Ambil history transaksi terakhir
            history = mt5.history_deals_get(ticket=ticket)
            profit_msg = ""
            if history:
                deal = history[-1]
                p_cent = deal.profit
                emoji = "💰 PROFIT" if p_cent > 0 else "💀 LOSS/BEP"
                profit_msg = f"\nHasil: `{emoji} ({p_cent} Cent)`"

            await context.bot.send_message(chat_id=CHAT_ID, text=f"🏁 *POSISI CLOSED, FERDY!*{profit_msg}\nTicket: `{ticket}`", parse_mode='Markdown')
            posisi_aktif.remove(ticket)

    # Sinkronisasi ticket aktif
    for ticket in current_tickets:
        posisi_aktif.add(ticket)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("DOR_"):
        tipe = query.data.split("_")[1]
        res = sm.kirim_order(tipe)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            await query.message.reply_text(f"🚀 *PELURU MELUNCUR!*\nBerhasil {tipe} di {res.price}\nTicket: `{res.order}`")
        else:
            await query.message.reply_text("❌ Gagal Eksekusi!")

def main():
    print("🚀 MEI ONLINE - MODE RAMAH BROKER")
    t_request = HTTPXRequest(connect_timeout=30, read_timeout=30)
    app = ApplicationBuilder().token(TOKEN_BOT).request(t_request).build()

    # Cek closing setiap 10 detik (Hanya baca data, tidak kirim order)
    app.job_queue.run_repeating(monitor_closing_otomatis, interval=10, first=5)

    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
