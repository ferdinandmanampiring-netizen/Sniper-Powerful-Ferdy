import pandas as pd

class LogicAgent:
    """
    Refactored from window_02_parser & window_03_intel.
    Tugas: Menganalisa kelayakan sinyal Telegram menggunakan sistem Voting:
    - Telegram signal (trigger)
    - RSI
    - MA
    - ATR
    - Bollinger Bands
    """
    def __init__(self, cfg: dict | None = None, rsi_period: int = 14, ma_period: int = 200):
        cfg = cfg or {}
        ind = (cfg.get("indicators") or {})
        voting = (cfg.get("voting") or {})

        self.rsi_period = int(ind.get("rsi_period", rsi_period))
        self.ma_period = int(ind.get("ma_period", ma_period))
        self.rsi_overbought = float(ind.get("rsi_overbought", 70))
        self.rsi_oversold = float(ind.get("rsi_oversold", 30))

        self.bb_period = int(voting.get("bb_period", 20))
        self.bb_stddev = float(voting.get("bb_stddev", 2.0))
        self.atr_min_pct = float(voting.get("atr_min_pct", 0.0))

        self.min_score_to_execute = int(voting.get("min_score_to_execute", 4))
        # Owner rule: score 3 = rejected
        self.reject_score_equals = int(voting.get("reject_score_equals", 3))

        # Toggles: default ON. If disabled => vote YES (no blocking).
        self.enable_rsi = bool(voting.get("enable_rsi", True))
        self.enable_ma = bool(voting.get("enable_ma", True))
        self.enable_atr = bool(voting.get("enable_atr", True))
        self.enable_bb = bool(voting.get("enable_bb", True))

    def calculate_indicators(self, df):
        work = df.copy()

        # RSI (simple rolling mean method)
        delta = work["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=self.rsi_period).mean()
        rs = gain / loss
        work["rsi"] = 100 - (100 / (1 + rs))

        # MA
        work["ma_200"] = work["close"].rolling(window=self.ma_period).mean()

        # ATR (simple moving average of true range)
        prev_close = work["close"].shift(1)
        tr1 = (work["high"] - work["low"]).abs()
        tr2 = (work["high"] - prev_close).abs()
        tr3 = (work["low"] - prev_close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        work["atr"] = true_range.rolling(window=14).mean()

        # Bollinger Bands
        mid = work["close"].rolling(window=self.bb_period).mean()
        std = work["close"].rolling(window=self.bb_period).std(ddof=0)
        work["bb_mid"] = mid
        work["bb_upper"] = mid + (self.bb_stddev * std)
        work["bb_lower"] = mid - (self.bb_stddev * std)

        return work

    def validate_signal(self, telegram_signal, market_data):
        """
        Validasi apakah sinyal Telegram layak eksekusi (Refactor Intel Logic).
        """
        action = (telegram_signal.get("action") or "").upper()
        if action not in ("BUY", "SELL"):
            return False, "Action tidak dikenal"

        required_cols = {"close", "high", "low"}
        if not required_cols.issubset(set(market_data.columns)):
            return False, "Market data tidak lengkap (butuh close/high/low)."

        df = self.calculate_indicators(market_data)

        last = df.iloc[-1]
        price = float(last["close"])
        rsi = float(last.get("rsi")) if pd.notna(last.get("rsi")) else None
        ma = float(last.get("ma_200")) if pd.notna(last.get("ma_200")) else None
        atr = float(last.get("atr")) if pd.notna(last.get("atr")) else None
        bb_mid = float(last.get("bb_mid")) if pd.notna(last.get("bb_mid")) else None

        votes: list[tuple[str, bool, str]] = []

        # 1) Telegram trigger always votes YES once parsed
        votes.append(("TG", True, "Signal parsed"))

        # 2) RSI vote
        if not self.enable_rsi:
            votes.append(("RSI", True, "RSI disabled"))
        elif rsi is None:
            votes.append(("RSI", False, "RSI belum siap"))
        else:
            if action == "BUY":
                ok = rsi < self.rsi_overbought
                votes.append(("RSI", ok, f"rsi={rsi:.2f} < {self.rsi_overbought:.2f}"))
            else:
                ok = rsi > self.rsi_oversold
                votes.append(("RSI", ok, f"rsi={rsi:.2f} > {self.rsi_oversold:.2f}"))

        # 3) MA vote (trend)
        if not self.enable_ma:
            votes.append(("MA", True, "MA disabled"))
        elif ma is None:
            votes.append(("MA", False, "MA belum siap"))
        else:
            if action == "BUY":
                ok = price > ma
                votes.append(("MA", ok, f"price={price:.5f} > ma={ma:.5f}"))
            else:
                ok = price < ma
                votes.append(("MA", ok, f"price={price:.5f} < ma={ma:.5f}"))

        # 4) ATR vote (volatility)
        if not self.enable_atr:
            votes.append(("ATR", True, "ATR disabled"))
        elif self.atr_min_pct <= 0:
            votes.append(("ATR", True, "ATR filter off"))
        else:
            if atr is None or price <= 0:
                votes.append(("ATR", False, "ATR belum siap"))
            else:
                atr_pct = (atr / price) * 100.0
                ok = atr_pct >= self.atr_min_pct
                votes.append(("ATR", ok, f"atr%={atr_pct:.3f} >= {self.atr_min_pct:.3f}"))

        # 5) Bollinger vote (position vs midline)
        if not self.enable_bb:
            votes.append(("BB", True, "BB disabled"))
        elif bb_mid is None:
            votes.append(("BB", False, "BB belum siap"))
        else:
            if action == "BUY":
                ok = price <= bb_mid
                votes.append(("BB", ok, f"price={price:.5f} <= mid={bb_mid:.5f}"))
            else:
                ok = price >= bb_mid
                votes.append(("BB", ok, f"price={price:.5f} >= mid={bb_mid:.5f}"))

        score = sum(1 for _, ok, _ in votes if ok)
        detail = " | ".join([f"{name}:{'Y' if ok else 'N'}" for name, ok, _ in votes])

        if score == self.reject_score_equals:
            return False, f"Voting REJECT (score={score}/5). {detail}"
        if score >= self.min_score_to_execute:
            return True, f"Voting OK (score={score}/5). {detail}"
        return False, f"Voting REJECT (score={score}/5). {detail}"
