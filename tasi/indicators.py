"""
tasi/indicators.py
==================
حساب المؤشرات الفنية للسوق السعودي (تداول).

Technical indicators implemented in pure Python (no pandas / numpy required),
so the bot runs on a bare Python 3.9+ install.

المؤشرات المتوفرة:
    - MA20 / MA50 / MA200   (Simple Moving Averages)
    - RSI                   (Wilder's smoothing, default period 14)
    - MACD                  (12, 26, 9)
    - Volume Ratio          (current volume / average volume of the PREVIOUS N bars)
    - ATR                   (Average True Range - تقلب السهم، أساس وقف الخسارة)
    - VWAP                  (متوسط السعر المرجح بالحجم - مرجع المضاربة اليومية)
    - Bollinger Bands       (نطاقات التقلب)
    - Momentum / ROC        (قوة الحركة)

كل الدوال تستقبل قائمة من الأرقام (الأقدم أولاً) وتُرجع قائمة بنفس الطول،
حيث تكون القيم غير المعرّفة (في بداية السلسلة) مساوية للقيمة None.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence

Number = Optional[float]


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def sma(values: Sequence[float], period: int) -> List[Number]:
    """المتوسط المتحرك البسيط - Simple Moving Average.

    Uses a rolling sum so the cost is O(n) rather than O(n * period).
    """
    if period <= 0:
        raise ValueError("period must be a positive integer")

    out: List[Number] = [None] * len(values)
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> List[Number]:
    """المتوسط المتحرك الأسي - Exponential Moving Average.

    Seeded with the SMA of the first `period` values, which is the standard
    convention used by charting platforms (and by MACD below).
    """
    if period <= 0:
        raise ValueError("period must be a positive integer")

    out: List[Number] = [None] * len(values)
    if len(values) < period:
        return out

    multiplier = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed

    previous = seed
    for i in range(period, len(values)):
        previous = (values[i] - previous) * multiplier + previous
        out[i] = previous
    return out


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------
def rsi(values: Sequence[float], period: int = 14) -> List[Number]:
    """مؤشر القوة النسبية - Relative Strength Index (Wilder's smoothing).

    Returns values in the 0..100 range. A flat series with no losses yields
    100.0, which matches the reference implementation.
    """
    if period <= 0:
        raise ValueError("period must be a positive integer")

    out: List[Number] = [None] * len(values)
    if len(values) <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change

    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)

    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------
def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Dict[str, List[Number]]:
    """مؤشر الماكد - Moving Average Convergence Divergence.

    Returns a dict with three equal-length series: ``macd``, ``signal``
    and ``hist`` (histogram = macd - signal).
    """
    if fast >= slow:
        raise ValueError("fast period must be smaller than slow period")

    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)

    macd_line: List[Number] = [
        None if (f is None or s is None) else f - s
        for f, s in zip(fast_ema, slow_ema)
    ]

    # The signal line is an EMA of the (shorter) defined part of the MACD line.
    defined = [v for v in macd_line if v is not None]
    offset = len(macd_line) - len(defined)
    signal_defined = ema(defined, signal) if defined else []
    signal_line: List[Number] = [None] * offset + list(signal_defined)
    signal_line += [None] * (len(macd_line) - len(signal_line))

    hist: List[Number] = [
        None if (m is None or s is None) else m - s
        for m, s in zip(macd_line, signal_line)
    ]

    return {"macd": macd_line, "signal": signal_line, "hist": hist}


# ---------------------------------------------------------------------------
# Volume ratio
# ---------------------------------------------------------------------------
def average_volume(volumes: Sequence[float], period: int = 20) -> List[Number]:
    """متوسط حجم التداول للشموع السابقة (بدون الشمعة الحالية).

    The current bar is deliberately excluded: a spike must be measured
    against the volume that came *before* it, otherwise a large bar inflates
    its own baseline and the ratio understates the spike.
    """
    if period <= 0:
        raise ValueError("period must be a positive integer")

    out: List[Number] = [None] * len(volumes)
    running = 0.0
    for i, volume in enumerate(volumes):
        if i >= period:
            out[i] = running / period
            running -= volumes[i - period]
        running += volume
    return out


def volume_ratio(volumes: Sequence[float], period: int = 20) -> List[Number]:
    """نسبة الحجم - حجم الشمعة الحالية مقسوماً على متوسط الشموع السابقة.

    A value of 1.5 means the bar traded 50% more than the average of the
    `period` bars preceding it. Bars whose baseline average is zero return
    None instead of dividing by zero (common for illiquid names).
    """
    averages = average_volume(volumes, period)
    out: List[Number] = []
    for volume, average in zip(volumes, averages):
        if average is None or average == 0:
            out.append(None)
        else:
            out.append(volume / average)
    return out


# ---------------------------------------------------------------------------
# Aggregate snapshot
# ---------------------------------------------------------------------------
@dataclass
class IndicatorSnapshot:
    """آخر قراءة لكل المؤشرات - the latest reading of every indicator."""

    symbol: str
    close: Optional[float] = None
    ma20: Optional[float] = None
    ma50: Optional[float] = None
    ma200: Optional[float] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    volume: Optional[float] = None
    avg_volume: Optional[float] = None
    volume_ratio: Optional[float] = None
    bars: int = 0

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @property
    def trend_up(self) -> Optional[bool]:
        """اتجاه صاعد: MA50 فوق MA200 (التقاطع الذهبي)."""
        if self.ma50 is None or self.ma200 is None:
            return None
        return self.ma50 > self.ma200

    @property
    def macd_bullish(self) -> Optional[bool]:
        if self.macd is None or self.macd_signal is None:
            return None
        return self.macd > self.macd_signal


def compute_all(
    symbol: str,
    closes: Sequence[float],
    volumes: Sequence[float],
    volume_period: int = 20,
    rsi_period: int = 14,
) -> IndicatorSnapshot:
    """احسب كل المؤشرات وأرجع آخر قيمة لكل مؤشر.

    ``closes`` and ``volumes`` must be ordered oldest -> newest. Indicators
    that do not have enough history yet come back as None instead of raising,
    so the caller can decide whether to trade or wait for more bars.
    """
    closes = list(closes)
    volumes = list(volumes)
    if len(volumes) < len(closes):
        volumes += [0.0] * (len(closes) - len(volumes))

    snapshot = IndicatorSnapshot(symbol=symbol, bars=len(closes))
    if not closes:
        return snapshot

    ma20 = sma(closes, 20)
    ma50 = sma(closes, 50)
    ma200 = sma(closes, 200)
    rsi_series = rsi(closes, rsi_period)
    macd_series = macd(closes)
    vol_avg = average_volume(volumes, volume_period)
    vol_ratio = volume_ratio(volumes, volume_period)

    last = len(closes) - 1
    snapshot.close = closes[last]
    snapshot.ma20 = ma20[last]
    snapshot.ma50 = ma50[last]
    snapshot.ma200 = ma200[last]
    snapshot.rsi = rsi_series[last]
    snapshot.macd = macd_series["macd"][last]
    snapshot.macd_signal = macd_series["signal"][last]
    snapshot.macd_hist = macd_series["hist"][last]
    snapshot.volume = volumes[last]
    snapshot.avg_volume = vol_avg[last]
    snapshot.volume_ratio = vol_ratio[last]
    return snapshot


# ---------------------------------------------------------------------------
# مؤشرات المضاربة - intraday / speculation indicators
# ---------------------------------------------------------------------------
def true_range(highs: Sequence[float], lows: Sequence[float],
               closes: Sequence[float]) -> List[Number]:
    """المدى الحقيقي لكل شمعة - the raw input to ATR.

    True range accounts for gaps: a stock that opened far from yesterday's
    close has moved more than its own high-low span suggests.
    """
    out: List[Number] = [None] * len(closes)
    for i in range(len(closes)):
        if i == 0:
            out[i] = highs[i] - lows[i]
            continue
        prev_close = closes[i - 1]
        out[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int = 14) -> List[Number]:
    """متوسط المدى الحقيقي - Average True Range (Wilder's smoothing).

    ATR is the honest way to place a stop: a fixed 2% stop is too tight for
    a volatile name and too wide for a quiet one. Sizing off ATR makes the
    risk per trade consistent across very different stocks.
    """
    tr = true_range(highs, lows, closes)
    out: List[Number] = [None] * len(closes)
    if len(closes) <= period:
        return out

    seed = sum(v for v in tr[1:period + 1] if v is not None) / period
    out[period] = seed
    previous = seed
    for i in range(period + 1, len(closes)):
        current = tr[i] or 0.0
        previous = (previous * (period - 1) + current) / period
        out[i] = previous
    return out


def vwap(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
         volumes: Sequence[float]) -> List[Number]:
    """السعر المتوسط المرجح بالحجم - cumulative VWAP over the given bars.

    Intended for a single session's intraday bars: pass one day's bars and
    the series resets naturally. Institutions benchmark fills against VWAP,
    so price relative to it is a genuine intraday reference.
    """
    out: List[Number] = []
    cum_pv = 0.0
    cum_vol = 0.0
    for high, low, close, volume in zip(highs, lows, closes, volumes):
        typical = (high + low + close) / 3.0
        cum_pv += typical * volume
        cum_vol += volume
        out.append(cum_pv / cum_vol if cum_vol else None)
    return out


def bollinger(values: Sequence[float], period: int = 20,
              num_std: float = 2.0) -> Dict[str, List[Number]]:
    """نطاقات بولنجر - middle band plus/minus `num_std` standard deviations."""
    middle = sma(values, period)
    upper: List[Number] = [None] * len(values)
    lower: List[Number] = [None] * len(values)
    width: List[Number] = [None] * len(values)

    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        mean = middle[i]
        if mean is None:
            continue
        variance = sum((v - mean) ** 2 for v in window) / period
        std = variance ** 0.5
        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std
        width[i] = (upper[i] - lower[i]) / mean if mean else None

    return {"middle": middle, "upper": upper, "lower": lower, "width": width}


def roc(values: Sequence[float], period: int = 10) -> List[Number]:
    """معدل التغير - Rate of Change as a percentage over `period` bars."""
    out: List[Number] = [None] * len(values)
    for i in range(period, len(values)):
        past = values[i - period]
        if past:
            out[i] = (values[i] - past) / past * 100.0
    return out


def relative_strength(values: Sequence[float], benchmark: Sequence[float],
                      period: int = 20) -> List[Number]:
    """القوة النسبية مقابل المؤشر - stock performance minus index performance.

    Positive means the stock outpaced TASI over the window. This is what
    separates a name that is merely rising with the market from one that is
    genuinely leading it.
    """
    stock_roc = roc(values, period)
    index_roc = roc(benchmark, period)
    return [
        None if (s is None or b is None) else s - b
        for s, b in zip(stock_roc, index_roc)
    ]


def realized_volatility(values: Sequence[float], period: int = 20) -> List[Number]:
    """التقلب المحقق - standard deviation of returns, annualised on 252 days."""
    returns: List[Number] = [None] * len(values)
    for i in range(1, len(values)):
        if values[i - 1]:
            returns[i] = (values[i] - values[i - 1]) / values[i - 1]

    out: List[Number] = [None] * len(values)
    for i in range(period, len(values)):
        window = [r for r in returns[i - period + 1:i + 1] if r is not None]
        if len(window) < period // 2:
            continue
        mean = sum(window) / len(window)
        variance = sum((r - mean) ** 2 for r in window) / len(window)
        out[i] = (variance ** 0.5) * (252 ** 0.5) * 100.0
    return out
