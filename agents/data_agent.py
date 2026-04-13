import MetaTrader5 as mt5
import pandas as pd
import os
from dotenv import load_dotenv
import logging

# Ambil rahasia dari .env
load_dotenv()

class DataAgent:
    def __init__(self):
        self.login = int(os.getenv("MT5_LOGIN", 0))
        self.password = os.getenv("MT5_PASSWORD")
        self.server = os.getenv("MT5_SERVER")
        
    def connect(self):
        try:
            ok = mt5.initialize(login=self.login, password=self.password, server=self.server)
        except Exception as e:
            logging.exception(f"Exception saat initialize MT5: {e}")
            return False

        if not ok:
            logging.error(f"Gagal koneksi ke MT5: {mt5.last_error()}")
            return False

        logging.info("Terhubung ke MT5 melalui .env Security")
        return True

    def get_data(self, symbol, tf, count=1000):
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        return pd.DataFrame(rates) if rates is not None else None

    def ensure_symbol(self, symbol: str) -> bool:
        info = mt5.symbol_info(symbol)
        if info is None:
            return False
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                return False
        return True

    def get_tick(self, symbol: str):
        return mt5.symbol_info_tick(symbol)

    def send_market_order(self, symbol: str, action: str, lot: float, sl: float | None, tp: float | None, deviation: int = 20):
        tick = self.get_tick(symbol)
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

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lot),
            "type": order_type,
            "price": price,
            "deviation": int(deviation),
            "magic": 260410,
            "comment": "SniperPowerfulFerdy",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if sl is not None:
            request["sl"] = float(sl)
        if tp is not None:
            request["tp"] = float(tp)

        result = mt5.order_send(request)
        return result, None

    def send_limit_order(self, symbol: str, action: str, lot: float, price: float, sl: float | None, tp: float | None):
        if action.upper() == "BUY":
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
        elif action.upper() == "SELL":
            order_type = mt5.ORDER_TYPE_SELL_LIMIT
        else:
            return None, "Unknown action"

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": float(lot),
            "type": order_type,
            "price": float(price),
            "magic": 260410,
            "comment": "SniperPowerfulFerdy",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        if sl is not None:
            request["sl"] = float(sl)
        if tp is not None:
            request["tp"] = float(tp)

        result = mt5.order_send(request)
        return result, None
