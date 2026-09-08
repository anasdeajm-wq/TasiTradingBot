"""
tasi/momentum.py
================
الزخم المقطعي - cross-sectional momentum on liquid Saudi shares.

الفكرة: رتّب الأسهم بأدائها النسبي مقابل المؤشر خلال نافذة ماضية، اشترِ
الأقوى، وأعد التوازن شهرياً. لا رسوم بيانية ولا أشكال، بل ترتيب بحت.

لماذا نجح هذا حيث فشل غيره في هذا المشروع:

    - يعمل على الأسهم السائلة فقط. الأفضلية التي وجدناها في التوزيعات
      ماتت لأنها كانت تعيش في أسهم رخيصة يأكلها فرق السبريد. هنا مرشّح
      السيولة شرط أساسي لا إضافة.
    - أفقه شهر لا أيام، فتكلفة التداول تُوزَّع على حركة أكبر.
    - الزخم المقطعي من أكثر الظواهر توثيقاً في أبحاث التمويل عبر عشرات
      الأسواق وعقود. وجوده في السوق السعودي تأكيد لأثر معروف لا اكتشاف
      نمط عشوائي، وهذا فرق جوهري في مستوى الثقة.

**تحيّز البقاء - قيد لا يمكن إصلاحه ببياناتنا الحالية:** الكون يضم
الشركات المدرجة اليوم فقط. الشركات التي شُطبت أو اندمجت غائبة، وغيابها
يجمّل النتيجة. أثره على الزخم أقل منه على استراتيجيات أخرى لأن الزخم
يشتري الرابحين لا الخاسرين، لكنه موجود ويجب خصمه ذهنياً من أي رقم هنا.

**التحيّز أشد بكثير في مؤشرات مثل S&P 500:** عضوية المؤشر نفسها اختيار
قائم على الأداء. الشركة التي تراجعت تُخرَج منه، فقائمة اليوم لا تحوي
خاسري الأمس أصلاً. استراتيجية الزخم في اختبار كهذا لا تُمنح فرصة اختيار
الخاسرين الذين كانت ستشتريهم فعلاً. الأدبيات على بيانات نظيفة من هذا
التحيّز تجد فائضاً سنوياً في حدود ٥ إلى ١٠٪ للأسهم الأمريكية الكبيرة،
فأي رقم يتجاوز ذلك بكثير في اختبارنا يجب أن يُنسب للتحيّز لا للأفضلية.
"""

from __future__ import annotations

import sqlite3
import statistics as st
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_LOOKBACK = 120          # جلسات النظر للخلف (~ستة أشهر)
DEFAULT_HOLD = 10               # عدد الأسهم المحتفظ بها
DEFAULT_MIN_TURNOVER = 1e7      # حد السيولة اليومية بالريال
DEFAULT_SLIPPAGE = 0.002        # انزلاق متحفظ
DEFAULT_COMMISSION = 0.00155


@dataclass
class Holding:
    symbol: str
    score: float                # القوة النسبية مقابل المؤشر
    turnover: float


@dataclass
class Rebalance:
    date: str
    picks: List[Holding]
    period_return: Optional[float] = None
    benchmark_return: Optional[float] = None


@dataclass
class MomentumResult:
    lookback: int
    hold: int
    min_turnover: float
    slippage: float
    rebalances: List[Rebalance] = field(default_factory=list)
    equity: float = 1.0
    benchmark_equity: float = 1.0

    @property
    def total_return_pct(self) -> float:
        return (self.equity - 1) * 100

    @property
    def benchmark_return_pct(self) -> float:
        return (self.benchmark_equity - 1) * 100

    @property
    def excess_pct(self) -> float:
        return self.total_return_pct - self.benchmark_return_pct

    @property
    def periods(self) -> int:
        return sum(1 for r in self.rebalances if r.period_return is not None)

    def annualised(self, months_per_year: int = 12) -> Optional[float]:
        if self.periods < months_per_year:
            return None
        years = self.periods / months_per_year
        return ((self.equity ** (1 / years)) - 1) * 100

    def max_drawdown_pct(self) -> float:
        peak = equity = 1.0
        worst = 0.0
        for rebalance in self.rebalances:
            if rebalance.period_return is None:
                continue
            equity *= 1 + rebalance.period_return
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak * 100)
        return worst

    def win_rate(self) -> Optional[float]:
        returns = [r.period_return for r in self.rebalances
                   if r.period_return is not None]
        if not returns:
            return None
        return sum(1 for r in returns if r > 0) / len(returns)

    def worst_period(self) -> Optional[Tuple[str, float]]:
        returns = [(r.date, r.period_return) for r in self.rebalances
                   if r.period_return is not None]
        return min(returns, key=lambda x: x[1]) if returns else None

    def by_year(self) -> Dict[str, float]:
        buckets: Dict[str, float] = {}
        for rebalance in self.rebalances:
            if rebalance.period_return is None:
                continue
            year = rebalance.date[:4]
            buckets[year] = buckets.get(year, 1.0) * (1 + rebalance.period_return)
        return {y: (v - 1) * 100 for y, v in buckets.items()}


# ---------------------------------------------------------------------------
def _prepare(conn: sqlite3.Connection, market: str = "MAIN",
             min_bars: int = 400) -> Dict[str, dict]:
    """حضّر الأسهم الصالحة في سوق معيّن. ``market`` يقبل MAIN أو NOMU أو US."""
    from .quality import check_bars
    from .sweep import load_series

    rows = conn.execute(
        "SELECT symbol FROM companies WHERE market = ?", (market,)).fetchall()
    prepared: Dict[str, dict] = {}
    for row in rows:
        symbol = row["symbol"]
        if symbol == "TASI":
            continue
        bars = load_series(conn, symbol)
        if not bars or len(bars["close"]) < min_bars:
            continue
        if not check_bars(symbol, bars).ok:
            continue
        prepared[symbol] = {
            "index": {ts: i for i, ts in enumerate(bars["ts"])},
            "bars": bars,
        }
    return prepared


def rebalance_dates(calendar: Sequence[str], warmup: int = 260
                    ) -> List[Tuple[int, str]]:
    """أول جلسة من كل شهر بعد فترة التسخين."""
    seen = set()
    out = []
    for i, ts in enumerate(calendar):
        month = ts[:7]
        # >= لا > : warmup صفر يعني بلا تسخين، فلا تُستبعد أول جلسة
        if month not in seen and i >= warmup:
            seen.add(month)
            out.append((i, ts))
    return out


def rank_at(
    prepared: Dict[str, dict],
    benchmark: Dict[str, List],
    bench_index: Dict[str, int],
    as_of: str,
    lookback: int,
    min_turnover: float,
) -> List[Holding]:
    """رتّب الأسهم بالقوة النسبية مقابل المؤشر عند تاريخ معيّن.

    القوة النسبية = عائد السهم − عائد المؤشر على نفس النافذة. طرح المؤشر
    ضروري وإلا كان الترتيب يعكس اتجاه السوق لا تميّز السهم.
    """
    bench_at = bench_index.get(as_of)
    if bench_at is None or bench_at - lookback < 0:
        return []
    bench_past = benchmark["close"][bench_at - lookback]
    if not bench_past:
        return []
    market_return = (benchmark["close"][bench_at] - bench_past) / bench_past

    out: List[Holding] = []
    for symbol, blob in prepared.items():
        j = blob["index"].get(as_of)
        if j is None or j - lookback < 0 or j < 20:
            continue
        bars = blob["bars"]
        turnover = st.mean(bars["close"][k] * bars["volume"][k]
                           for k in range(j - 20, j)) \
            if j >= 20 else 0.0
        if turnover < min_turnover:
            continue
        past = bars["close"][j - lookback]
        if not past or not bars["close"][j]:
            continue
        stock_return = (bars["close"][j] - past) / past
        out.append(Holding(symbol=symbol, score=stock_return - market_return,
                           turnover=turnover))
    out.sort(key=lambda h: h.score, reverse=True)
    return out


def run(
    conn: sqlite3.Connection,
    lookback: int = DEFAULT_LOOKBACK,
    hold: int = DEFAULT_HOLD,
    min_turnover: float = DEFAULT_MIN_TURNOVER,
    slippage: float = DEFAULT_SLIPPAGE,
    commission: float = DEFAULT_COMMISSION,
    market: str = "MAIN",
    benchmark_symbol: str = "TASI",
    start: int = 0,
    end: Optional[int] = None,
) -> MomentumResult:
    """شغّل الاستراتيجية عبر التاريخ المخزّن."""
    from .sweep import load_series

    benchmark = load_series(conn, benchmark_symbol)
    if not benchmark:
        raise ValueError(f"لا توجد بيانات للمؤشر {benchmark_symbol}")
    bench_index = {ts: i for i, ts in enumerate(benchmark["ts"])}

    prepared = _prepare(conn, market)
    if not prepared:
        raise ValueError("لا توجد أسهم صالحة بعد فحص الجودة.")

    points = rebalance_dates(benchmark["ts"])
    points = points[start:end] if end else points[start:]

    result = MomentumResult(lookback=lookback, hold=hold,
                            min_turnover=min_turnover, slippage=slippage)

    for k in range(len(points) - 1):
        _, ts0 = points[k]
        _, ts1 = points[k + 1]

        ranked = rank_at(prepared, benchmark, bench_index, ts0,
                         lookback, min_turnover)
        if len(ranked) < hold * 2:
            continue                      # كون ضيق جداً للترتيب

        picks = ranked[:hold]
        rebalance = Rebalance(date=ts0, picks=picks)

        returns = []
        for holding in picks:
            blob = prepared[holding.symbol]
            bars = blob["bars"]
            j0 = blob["index"].get(ts0)
            j1 = blob["index"].get(ts1)
            if j0 is None or j1 is None:
                continue
            entry = bars["close"][j0] * (1 + slippage)
            exit_price = bars["close"][j1] * (1 - slippage)
            returns.append((exit_price - entry) / entry - commission * 2)

        if not returns:
            continue
        rebalance.period_return = sum(returns) / len(returns)
        b0, b1 = bench_index[ts0], bench_index[ts1]
        rebalance.benchmark_return = (
            benchmark["close"][b1] / benchmark["close"][b0] - 1)

        result.equity *= 1 + rebalance.period_return
        result.benchmark_equity *= 1 + rebalance.benchmark_return
        result.rebalances.append(rebalance)

    return result


def current_picks(
    conn: sqlite3.Connection,
    lookback: int = DEFAULT_LOOKBACK,
    hold: int = DEFAULT_HOLD,
    min_turnover: float = DEFAULT_MIN_TURNOVER,
    market: str = "MAIN",
    benchmark_symbol: str = "TASI",
) -> List[Holding]:
    """الترتيب الحالي بآخر بيانات مخزّنة - ما تشتريه لو أعدت التوازن اليوم."""
    from .sweep import load_series

    benchmark = load_series(conn, benchmark_symbol)
    if not benchmark:
        raise ValueError(f"لا توجد بيانات للمؤشر {benchmark_symbol}")
    bench_index = {ts: i for i, ts in enumerate(benchmark["ts"])}
    prepared = _prepare(conn, market)
    latest = benchmark["ts"][-1]
    return rank_at(prepared, benchmark, bench_index, latest,
                   lookback, min_turnover)[:hold]
