import MetaTrader5 as mt5

def hitung_skor(data):
    skor = 50 
    if data['rsi'] < 30: skor += 25
    elif data['rsi'] > 70: skor -= 25
    if data['price'] > data['ma50'] > data['ma200']: skor += 25
    elif data['price'] < data['ma50'] < data['ma200']: skor -= 25
    return skor

def get_last_swing(symbol, timeframe=mt5.TIMEFRAME_M1, candles_back=5):
    """
    Mendapatkan swing terakhir dari beberapa candle terakhir.
    Mengembalikan (swing_high, swing_low) dari candle terakhir yang signifikan.
    """
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, candles_back)
    if rates is None or len(rates) < candles_back:
        return None, None
    
    # Ambil high dan low dari candle terakhir
    last_candle = rates[-1]
    swing_high = last_candle['high']
    swing_low = last_candle['low']
    
    return swing_high, swing_low

def auto_trailing_dan_bep(symbol, magic_number, trigger_bep=120):
    """
    MODE RAMAH BROKER - SL MENGIKUTI SWING TERAKHIR:
    - SL BUY: Mengikuti swing low terakhir
    - SL SELL: Mengikuti swing high terakhir
    - Jarak toleransi diperlebar (15 Pips) agar tidak spamming server.
    - Buffer Stoplevel dipertebal agar tidak ada [Invalid stops].
    """
    if not mt5.initialize(): return
    
    positions = mt5.positions_get(symbol=symbol, magic=magic_number)
    if not positions: return
    # Dapatkan swing terakhir
    swing_high, swing_low = get_last_swing(symbol)
    if swing_high is None or swing_low is None:
        return  # Tidak bisa dapat data swing
    symbol_info = mt5.symbol_info(symbol)
    point = symbol_info.point
    digits = symbol_info.digits
    # Jarak aman ekstra dari batas minimal broker
    stoplevel_aman = (symbol_info.stoplevel + 30) * point 

    for p in positions:
        tick = mt5.symbol_info_tick(symbol)
        current_price = tick.bid if p.type == 0 else tick.ask
        profit_pips = abs(current_price - p.price_open) / (point * 10)
        
        target_sl = 0
        # Hanya kirim request jika SL baru beda > 15 Pips dari SL lama
        pips_toleransi_keras = 15 * 10 * point 

        if p.type == 0: # BUY
            if profit_pips >= trigger_bep:
                # SL mengikuti swing low terakhir, tapi minimal BEP
                ideal_sl = swing_low
                limit_bep = p.price_open + (2 * 10 * point)
                final_ideal = max(ideal_sl, limit_bep)

                # Syarat Ketat: Harus naik signifikan & jarak aman dari bid
                if final_ideal > (p.sl + pips_toleransi_keras) and final_ideal < (tick.bid - stoplevel_aman):
                    target_sl = final_ideal

        elif p.type == 1: # SELL
            if profit_pips >= trigger_bep:
                # SL mengikuti swing high terakhir, tapi minimal BEP
                ideal_sl = swing_high
                limit_bep = p.price_open - (2 * 10 * point)
                final_ideal = min(ideal_sl, limit_bep)

                # Syarat Ketat: Harus turun signifikan & jarak aman dari ask
                if (p.sl == 0 or final_ideal < (p.sl - pips_toleransi_keras)) and final_ideal > (tick.ask + stoplevel_aman):
                    target_sl = final_ideal

        if target_sl > 0:
            sl_final = round(target_sl, digits)
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "sl": sl_final,
                "tp": p.tp,
                "position": p.ticket
            }
            # Kirim ke Exness
            mt5.order_send(request)
            # MEI TIDAK PRINT APA PUN KE TERMINAL ATAU TELEGRAM SAAT GESER SL
