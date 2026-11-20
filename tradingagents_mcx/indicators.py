"""Technical indicators in plain pandas.

Mirrors the indicator set of the original enhanced_professional_technical_analysis.py
(MA5/20/60, EMA20, RSI14, MACD, KDJ, Bollinger, ATR14, OBV, volume ratio, VWAP,
open interest) without the TA-Lib C dependency.
"""
import numpy as np
import pandas as pd


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """df needs columns: open, high, low, close, volume (open_interest optional)."""
    out = df.copy()
    c, h, l, v = out["close"], out["high"], out["low"], out["volume"]

    for n in (5, 20, 60):
        out[f"ma{n}"] = c.rolling(n).mean()
    out["ema20"] = c.ewm(span=20, adjust=False).mean()

    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    out["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    ll9, hh9 = l.rolling(9).min(), h.rolling(9).max()
    rsv = 100 * (c - ll9) / (hh9 - ll9).replace(0, np.nan)
    out["kdj_k"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    out["kdj_d"] = out["kdj_k"].ewm(alpha=1 / 3, adjust=False).mean()
    out["kdj_j"] = 3 * out["kdj_k"] - 2 * out["kdj_d"]

    mid = c.rolling(20).mean()
    std = c.rolling(20).std()
    out["bb_middle"], out["bb_upper"], out["bb_lower"] = mid, mid + 2 * std, mid - 2 * std

    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    out["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    out["obv"] = (np.sign(c.diff()).fillna(0) * v).cumsum()
    out["volume_ma20"] = v.rolling(20).mean()
    out["volume_ratio"] = v / out["volume_ma20"]
    tp = (h + l + c) / 3
    out["vwap20"] = (tp * v).rolling(20).sum() / v.rolling(20).sum()
    return out


def _ma_alignment(row) -> str:
    ma5, ma20, ma60 = row["ma5"], row["ma20"], row["ma60"]
    if ma5 > ma20 > ma60:
        return "bullish alignment (MA5 > MA20 > MA60)"
    if ma5 < ma20 < ma60:
        return "bearish alignment (MA5 < MA20 < MA60)"
    return "mixed / entangled moving averages"


def support_resistance(df: pd.DataFrame, lookback: int = 60) -> tuple[list, list]:
    """Simple swing-point S/R: recent lows below price, highs above, plus round numbers."""
    recent = df.tail(lookback)
    price = float(df["close"].iloc[-1])
    highs = recent["high"].rolling(5, center=True).max()
    lows = recent["low"].rolling(5, center=True).min()
    swing_high = sorted({round(float(x)) for x, m in zip(recent["high"], recent["high"] == highs) if m and x > price})
    swing_low = sorted({round(float(x)) for x, m in zip(recent["low"], recent["low"] == lows) if m and x < price}, reverse=True)
    return swing_low[:3], swing_high[:3]


def technical_snapshot(df: pd.DataFrame) -> dict:
    """Latest-bar snapshot + 0-100 trend/momentum scores, as the original computed."""
    ind = compute_indicators(df).dropna(subset=["ma60"])
    last = ind.iloc[-1]
    price = float(last["close"])

    trend = 50.0
    trend += 15 if price > last["ma20"] else -15
    trend += 15 if price > last["ma60"] else -15
    trend += 10 if last["ma5"] > last["ma20"] else -10
    trend += 10 if last["macd"] > 0 else -10
    trend = max(0.0, min(100.0, trend))

    momentum = 50.0
    momentum += (last["rsi14"] - 50) * 0.6
    momentum += 10 if last["macd_hist"] > 0 else -10
    momentum += 5 if last["volume_ratio"] > 1.2 else (-5 if last["volume_ratio"] < 0.8 else 0)
    momentum = max(0.0, min(100.0, momentum))

    bb_range = last["bb_upper"] - last["bb_lower"]
    bb_pos = (price - last["bb_lower"]) / bb_range if bb_range else 0.5
    supports, resistances = support_resistance(ind)

    def pct_change(n):
        return round(100 * (price / float(ind["close"].iloc[-1 - n]) - 1), 2) if len(ind) > n else None

    return {
        "price": price,
        "change_5d_pct": pct_change(5),
        "change_20d_pct": pct_change(20),
        "rsi14": round(float(last["rsi14"]), 1),
        "macd": round(float(last["macd"]), 2),
        "macd_signal": round(float(last["macd_signal"]), 2),
        "macd_hist": round(float(last["macd_hist"]), 2),
        "ma5": round(float(last["ma5"]), 1),
        "ma20": round(float(last["ma20"]), 1),
        "ma60": round(float(last["ma60"]), 1),
        "ma_alignment": _ma_alignment(last),
        "bb_upper": round(float(last["bb_upper"]), 1),
        "bb_middle": round(float(last["bb_middle"]), 1),
        "bb_lower": round(float(last["bb_lower"]), 1),
        "bb_position": round(float(bb_pos), 2),
        "atr14": round(float(last["atr14"]), 2),
        "kdj_k": round(float(last["kdj_k"]), 1),
        "kdj_d": round(float(last["kdj_d"]), 1),
        "volume": int(last["volume"]),
        "volume_ratio": round(float(last["volume_ratio"]), 2),
        "open_interest": int(last["open_interest"]) if "open_interest" in ind.columns and pd.notna(last.get("open_interest")) else None,
        "trend_score": round(trend),
        "momentum_score": round(momentum),
        "supports": supports,
        "resistances": resistances,
    }


if __name__ == "__main__":
    rng = np.random.default_rng(7)
    n = 200
    close = 6000 + np.cumsum(rng.normal(0, 30, n))
    df = pd.DataFrame({
        "open": close + rng.normal(0, 10, n),
        "high": close + abs(rng.normal(0, 25, n)),
        "low": close - abs(rng.normal(0, 25, n)),
        "close": close,
        "volume": rng.integers(1000, 9000, n).astype(float),
    })
    snap = technical_snapshot(df)
    assert 0 <= snap["trend_score"] <= 100 and 0 <= snap["momentum_score"] <= 100
    assert 0 < snap["rsi14"] < 100 and snap["bb_lower"] < snap["bb_middle"] < snap["bb_upper"]
    print("indicators self-check OK:", {k: snap[k] for k in ("price", "rsi14", "trend_score", "momentum_score")})
