"""
tasi/sweep.py
=============
مسح معاملات الأنماط - searching parameter space honestly.

الفخ الذي يبطل أغلب عمليات المسح: تجربة مئات التركيبات على نفس البيانات
تُنتج رابحاً بالصدفة وحدها. لو جرّبت ٢٠٠ تركيبة عشوائية تماماً، ستجد
عشرة منها «رابحة» عند مستوى ثقة ٥٪. من يعرض هذه العشرة كاكتشاف يخدع
نفسه قبل أن يخدع غيره.

ثلاثة قيود مطبّقة هنا في مواجهة ذلك:

    1. كل تركيبة تمر ببوابة التحقق خارج العيّنة، لا بالأداء الكلي.
    2. عدد التركيبات المجرَّبة يُعرض في كل تقرير، لأن معناه أن الناجي
       الواحد من مئتين قد يكون ضجيجاً.
    3. يُحسب عدد الناجين المتوقع بالصدفة، فيُقارَن بعدد الناجين الفعلي.
       ناجٍ واحد من مئتين ليس اكتشافاً بل هو ما تتوقعه الصدفة.

المسح يبني الخصائص مرة واحدة لكل سهم ثم يعيد استخدامها لكل تركيبة، وهو
الفرق بين مسح يستغرق دقائق وآخر يستغرق ساعات.
"""

from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Type

from . import backtest as bt
from . import features as F
from . import quality as ql
from . import regime as rg
from . import setups as S


@dataclass
class SweepResult:
    """نتيجة تركيبة معاملات واحدة."""

    setup: str
    params: Dict[str, float]
    regime: str
    train_trades: int
    train_expectancy: Optional[float]
    test_trades: int
    test_expectancy: Optional[float]
    survived: bool

    def to_row(self) -> Dict[str, object]:
        return {
            "النمط": S.SETUP_NAMES_AR.get(self.setup, self.setup),
            "الحالة": rg.REGIME_AR.get(self.regime, self.regime),
            "المعاملات": ", ".join(f"{k}={v:g}" for k, v in self.params.items()),
            "صفقات تدريب": self.train_trades,
            "توقع تدريب": self.train_expectancy,
            "صفقات تحقق": self.test_trades,
            "توقع تحقق": self.test_expectancy,
            "صمد": "نعم" if self.survived else "لا",
        }



def _neighbours(a: "SweepResult", b: "SweepResult") -> bool:
    """هل التركيبتان متجاورتان؟ تتفقان في الحالة وتختلفان في معامل واحد."""
    if a.regime != b.regime or a.setup != b.setup:
        return False
    keys = set(a.params) | set(b.params)
    differing = sum(1 for k in keys if a.params.get(k) != b.params.get(k))
    return differing <= 1


@dataclass
class SweepReport:
    """حصيلة المسح كاملاً، مع ما يلزم للحكم على معناها."""

    combinations_tested: int = 0
    results: List[SweepResult] = field(default_factory=list)
    symbols_used: int = 0
    total_trades: int = 0

    @property
    def survivors(self) -> List[SweepResult]:
        return [r for r in self.results if r.survived]

    @property
    def expected_by_chance(self) -> float:
        """كم ناجياً نتوقعه بالصدفة وحدها؟

        الفرضية الصفرية تُقدَّر من البيانات نفسها لا من رقم مفترض: نحسب
        نسبة التركيبات التي ربحت في التدريب، ونسبة التي ربحت في التحقق،
        ثم نضربهما بافتراض الاستقلال بين النافذتين.

        هذا أدق بكثير من افتراض ٥٠٪ لكل نافذة، لأن هذه الأنماط تميل إلى
        الخسارة بعد التكاليف، فمعدلها الأساسي أقل من ذلك بكثير.
        """
        if not self.results:
            return 0.0
        n = len(self.results)
        train_positive = sum(1 for r in self.results
                             if (r.train_expectancy or 0) > 0) / n
        test_positive = sum(1 for r in self.results
                            if (r.test_expectancy or 0) > 0) / n
        return n * train_positive * test_positive

    # هامش أمان فوق التقدير الصفري. سببه أن التقدير يفترض استقلال
    # نافذتي التدريب والتحقق، وهو افتراض متفائل: الانحيازات المستمرة
    # (قطاع مهيمن، فترة سوق واحدة) ترفع الارتباط فيقلّ المتوقع زوراً.
    CHANCE_MARGIN = 1.5

    @property
    def null_rates(self) -> Tuple[float, float]:
        """نسبتا الربح في نافذتي التدريب والتحقق - أساس الفرضية الصفرية."""
        if not self.results:
            return 0.0, 0.0
        n = len(self.results)
        return (sum(1 for r in self.results if (r.train_expectancy or 0) > 0) / n,
                sum(1 for r in self.results if (r.test_expectancy or 0) > 0) / n)

    @property
    def survivor_clusters(self) -> int:
        """كم مجموعة متجاورة يشكّلها الناجون في فضاء المعاملات؟

        ناجٍ منفرد وسط جيران خاسرين غالباً ضجيج. مجموعة متجاورة من
        الناجين أقوى دلالة، لأن الأفضلية الحقيقية لا تختفي عند تغيير
        معامل واحد تغييراً طفيفاً.
        """
        survivors = self.survivors
        if not survivors:
            return 0
        groups: List[List[SweepResult]] = []
        for candidate in survivors:
            for group in groups:
                if any(_neighbours(candidate, member) for member in group):
                    group.append(candidate)
                    break
            else:
                groups.append([candidate])
        return len(groups)

    def verdict_ar(self) -> str:
        """حكم صريح: هل الناجون اكتشاف أم ضجيج متوقع؟"""
        found = len(self.survivors)
        expected = self.expected_by_chance
        train_rate, test_rate = self.null_rates

        if not self.results:
            return "لم تُنتج أي تركيبة صفقات كافية للحكم."
        if found == 0:
            return (f"لم تصمد أي تركيبة من {self.combinations_tested}. "
                    "لا توجد أفضلية في فضاء المعاملات المجرَّب.")

        basis = (f"ربحت {train_rate:.0%} من التركيبات في التدريب و"
                 f"{test_rate:.0%} في التحقق، فالمتوقع بالصدفة "
                 f"{expected:.1f} ناجٍ")

        threshold = expected * self.CHANCE_MARGIN
        if found <= threshold:
            return (f"صمدت {found} من {self.combinations_tested}. {basis}، "
                    f"وعتبة القبول {threshold:.1f} بعد هامش الأمان. "
                    "الناجون ضمن الضجيج، ولا يصلحون للتشغيل.")

        clusters = self.survivor_clusters
        cluster_note = (
            f" الناجون يشكّلون {clusters} مجموعة متجاورة في فضاء المعاملات"
            if clusters else "")
        return (f"صمدت {found} من {self.combinations_tested}. {basis}."
                f"{cluster_note}. يتجاوز الصدفة، لكنه مرشّح لا اكتشاف: "
                "يحتاج تحققاً متدحرجاً على فترات متعددة قبل أي تشغيل.")


# ---------------------------------------------------------------------------
def expand_grid(grid: Dict[str, Sequence[float]]) -> List[Dict[str, float]]:
    """حوّل شبكة معاملات إلى قائمة تركيبات."""
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, combo))
            for combo in itertools.product(*(grid[k] for k in keys))]


def load_series(conn: sqlite3.Connection, symbol: str,
                interval: str = "1day") -> Optional[Dict[str, List]]:
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM bars"
        " WHERE symbol = ? AND interval = ? ORDER BY ts ASC",
        (symbol, interval)).fetchall()
    if not rows:
        return None
    return {
        "ts": [r["ts"] for r in rows],
        "open": [float(r["open"] or r["close"]) for r in rows],
        "high": [float(r["high"] or r["close"]) for r in rows],
        "low": [float(r["low"] or r["close"]) for r in rows],
        "close": [float(r["close"]) for r in rows],
        "volume": [float(r["volume"] or 0) for r in rows],
    }


def prepare(
    conn: sqlite3.Connection,
    symbols: Sequence[str],
    benchmark: str = "TASI",
    start: int = 250,
    min_bars: int = 300,
) -> Tuple[Dict[str, dict], Dict[str, str]]:
    """ابنِ الخصائص وحالات السوق مرة واحدة لإعادة استخدامها في كل تركيبة."""
    bench = load_series(conn, benchmark)
    regimes: Dict[str, str] = {}
    bench_closes = None
    if bench:
        bench_closes = bench["close"]
        for i in range(60, len(bench_closes)):
            regimes[bench["ts"][i]] = rg.classify(
                bench_closes[:i + 1], as_of=bench["ts"][i]).regime

    prepared: Dict[str, dict] = {}
    for symbol in symbols:
        if symbol == benchmark:
            continue
        bars = load_series(conn, symbol)
        if not bars or len(bars["close"]) < min_bars:
            continue
        if not ql.check_bars(symbol, bars).ok:
            continue

        bench_arg = (bench_closes
                     if bench_closes and len(bench_closes) == len(bars["close"])
                     else None)
        snapshots = F.build_series(
            symbol, bars["open"], bars["high"], bars["low"], bars["close"],
            bars["volume"], bars["ts"], bench_arg, start=start)
        for snap in snapshots:
            snap.regime = regimes.get(snap.ts)
        prepared[symbol] = {"bars": bars, "snapshots": snapshots, "start": start}
    return prepared, regimes


def run_combination(
    prepared: Dict[str, dict],
    setup: S.Setup,
    max_bars: int = 15,
) -> List[bt.Trade]:
    """شغّل تركيبة واحدة على الخصائص المحضّرة مسبقاً."""
    trades: List[bt.Trade] = []
    for symbol, blob in prepared.items():
        bars = blob["bars"]
        start = blob["start"]
        busy_until = -1
        for offset, snap in enumerate(blob["snapshots"]):
            i = start + offset
            if i <= busy_until:
                continue
            if not setup.allowed_in(snap.regime):
                continue
            match = setup.evaluate(snap)
            if not match:
                continue
            trade = bt.simulate_trade(
                match, bars["open"], bars["high"], bars["low"], bars["close"],
                bars["ts"], i, max_bars=max_bars, regime=snap.regime)
            if trade:
                trades.append(trade)
                busy_until = i + trade.bars_held
    return trades


def sweep(
    conn: sqlite3.Connection,
    setup_class: Type[S.Setup],
    grid: Dict[str, Sequence[float]],
    symbols: Sequence[str],
    benchmark: str = "TASI",
    max_bars: int = 15,
    min_trades_each: int = 30,
    train_fraction: float = 0.6,
    on_progress=None,
) -> SweepReport:
    """امسح شبكة معاملات لنمط واحد، وقيّم كل تركيبة خارج العيّنة."""
    prepared, _ = prepare(conn, symbols, benchmark)
    report = SweepReport(symbols_used=len(prepared))
    combos = expand_grid(grid)

    for index, params in enumerate(combos, 1):
        setup = setup_class(**params)
        trades = run_combination(prepared, setup, max_bars)
        report.total_trades += len(trades)

        verdicts = bt.validate(trades, train_fraction, min_trades_each)
        for (setup_name, regime), verdict in verdicts.items():
            if not verdict["enough_data"]:
                continue
            report.combinations_tested += 1
            report.results.append(SweepResult(
                setup=setup_name, params=params, regime=regime,
                train_trades=verdict["train_trades"],
                train_expectancy=verdict["train_expectancy"],
                test_trades=verdict["test_trades"],
                test_expectancy=verdict["test_expectancy"],
                survived=bool(verdict["survived"]),
            ))
        if on_progress:
            on_progress(index, len(combos), len(report.survivors))

    report.results.sort(
        key=lambda r: (not r.survived, -(r.test_expectancy or -99)))
    return report


# ---------------------------------------------------------------------------
# شبكات المعاملات الافتراضية - starting grids per setup
# ---------------------------------------------------------------------------
DEFAULT_GRIDS: Dict[str, Dict[str, Sequence[float]]] = {
    "momentum_breakout": {
        "min_volume_ratio": (1.2, 1.5, 2.0, 3.0),
        "max_rsi": (65.0, 75.0),
        "atr_stop_mult": (1.0, 1.5, 2.5),
        "atr_target_mult": (2.0, 3.0, 5.0),
    },
    "pullback_ma": {
        "ma_distance_pct": (1.0, 2.0, 3.5),
        "min_rsi": (35.0, 45.0),
        "max_rsi": (55.0, 65.0),
        "atr_stop_mult": (1.0, 1.5, 2.5),
        "atr_target_mult": (2.0, 3.0, 5.0),
    },
    "volume_spike": {
        "min_volume_ratio": (2.0, 3.0, 5.0),
        "min_price_move": (0.5, 1.5, 3.0),
        "atr_stop_mult": (1.5, 2.0, 3.0),
        "atr_target_mult": (2.0, 3.0, 5.0),
    },
    "mean_reversion": {
        "max_rsi": (25.0, 32.0, 38.0),
        "atr_stop_mult": (1.0, 1.5, 2.5),
        "atr_target_mult": (1.5, 2.0, 3.0),
    },
    "relative_leader": {
        "min_rel_strength": (3.0, 5.0, 10.0),
        "min_volume_ratio": (1.0, 1.2, 1.8),
        "atr_stop_mult": (1.5, 2.0, 3.0),
        "atr_target_mult": (2.5, 3.5, 5.0),
    },
}


def setup_class_by_name(name: str) -> Type[S.Setup]:
    for cls in S.ALL_SETUPS:
        if cls.name == name:
            return cls
    raise ValueError(f"نمط غير معروف: {name}")
