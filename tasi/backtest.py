"""
tasi/backtest.py
================
محرك الاختبار التاريخي - measures what each setup actually did.

هذا الملف هو الذي يحوّل المعاملات المخمّنة إلى أرقام مقيسة. بدونه تبقى
كل نسبة في النظام رأياً.

قواعد الصدق المطبّقة هنا، وكل واحدة منها تمنع نتيجة متفائلة كاذبة:

    1. الدخول عند افتتاح الشمعة **التالية** للإشارة، لا عند إغلاق شمعة
       الإشارة. لا يمكنك الشراء بسعر لم تكن تعرفه إلا بعد إغلاق الجلسة.
    2. وقف الخسارة يُفحص على أدنى سعر الشمعة، والهدف على أعلاها.
    3. إذا لامست الشمعة الوقف والهدف معاً، تُحتسب وقف خسارة. الافتراض
       المتحفظ هو الوحيد الأمين هنا لأن بيانات الشموع لا تخبرنا بالترتيب.
    4. النتيجة تُقاس بوحدات المخاطرة (R) لا بالنسبة المئوية، فتصبح
       الصفقات قابلة للمقارنة بين أسهم مختلفة التقلب.
    5. تكاليف التداول تُخصم من كل صفقة.

النتيجة النهائية: expectancy لكل نمط في كل حالة سوق. النمط الذي توقعه
الرياضي سالب يُستبعد، مهما بدا منطقياً.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from . import features as F
from . import regime as rg
from . import setups as S

# تكاليف التداول في السوق السعودي. عمولة الوسيط تختلف، وهذه قيمة متحفظة
# افتراضية تشمل العمولة ورسوم السوق. تُمرَّر صراحة عند معرفة الرقم الفعلي.
DEFAULT_COMMISSION_PCT = 0.155 / 100     # لكل جهة (شراء أو بيع)
DEFAULT_SLIPPAGE_PCT = 0.10 / 100        # انزلاق تنفيذ متحفظ


@dataclass
class Trade:
    """صفقة محاكاة واحدة."""

    setup: str
    symbol: str
    regime: Optional[str]
    entry_ts: str
    entry_price: float
    stop_price: float
    target_price: float
    exit_ts: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""             # STOP / TARGET / TIME / END_OF_DATA
    bars_held: int = 0
    r_multiple: float = 0.0           # الربح أو الخسارة بوحدات المخاطرة
    return_pct: float = 0.0
    score: float = 0.0

    @property
    def is_win(self) -> bool:
        return self.r_multiple > 0


@dataclass
class SetupStats:
    """أداء نمط داخل حالة سوق واحدة."""

    setup: str
    regime: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit: float = 0.0         # مجموع R الموجبة
    gross_loss: float = 0.0           # مجموع |R| السالبة
    avg_r: float = 0.0
    hit_rate: float = 0.0
    expectancy: float = 0.0           # متوسط R لكل صفقة
    profit_factor: Optional[float] = None
    max_drawdown_r: float = 0.0
    avg_bars_held: float = 0.0

    @property
    def is_viable(self) -> bool:
        """هل يستحق النمط التشغيل؟ توقع موجب على عيّنة كافية."""
        return self.expectancy > 0 and self.trades >= 20

    def to_dict(self) -> Dict[str, object]:
        return {
            "النمط": S.SETUP_NAMES_AR.get(self.setup, self.setup),
            "الحالة": rg.REGIME_AR.get(self.regime, self.regime),
            "صفقات": self.trades,
            "نسبة الإصابة": f"{self.hit_rate:.0%}",
            "متوسط R": round(self.avg_r, 3),
            "التوقع": round(self.expectancy, 3),
            "عامل الربح": (round(self.profit_factor, 2)
                           if self.profit_factor else None),
            "أقصى تراجع R": round(self.max_drawdown_r, 2),
            "متوسط الأيام": round(self.avg_bars_held, 1),
            "صالح": "نعم" if self.is_viable else "لا",
        }


# ---------------------------------------------------------------------------
def simulate_trade(
    match: S.SetupMatch,
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    timestamps: Sequence[str],
    signal_index: int,
    max_bars: int = 20,
    commission_pct: float = DEFAULT_COMMISSION_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    regime: Optional[str] = None,
) -> Optional[Trade]:
    """حاكِ صفقة واحدة من لحظة الإشارة حتى الخروج.

    الدخول عند افتتاح الشمعة التالية. يُرجع None إن لم تعد هناك شموع
    كافية، بدل افتراض دخول لم يكن ممكناً.
    """
    entry_index = signal_index + 1
    if entry_index >= len(opens):
        return None
    if match.stop_hint is None or match.target_hint is None:
        return None

    entry_price = opens[entry_index] * (1 + slippage_pct)
    risk_per_share = entry_price - match.stop_hint
    if risk_per_share <= 0:
        return None

    trade = Trade(
        setup=match.setup, symbol=match.symbol, regime=regime,
        entry_ts=timestamps[entry_index], entry_price=entry_price,
        stop_price=match.stop_hint, target_price=match.target_hint,
        score=match.score,
    )

    # max_bars يعني عدد الشموع المحتفظ بها، وشمعة الدخول أولاها.
    last_index = min(entry_index + max_bars - 1, len(closes) - 1)
    for i in range(entry_index, last_index + 1):
        trade.bars_held = i - entry_index + 1

        # الوقف يُفحص أولاً: الافتراض المتحفظ عند ملامسة الاثنين معاً.
        if lows[i] <= match.stop_hint:
            trade.exit_price = match.stop_hint * (1 - slippage_pct)
            trade.exit_reason = "STOP"
            trade.exit_ts = timestamps[i]
            break
        if highs[i] >= match.target_hint:
            trade.exit_price = match.target_hint * (1 - slippage_pct)
            trade.exit_reason = "TARGET"
            trade.exit_ts = timestamps[i]
            break
    else:
        trade.exit_price = closes[last_index]
        trade.exit_reason = "TIME" if last_index < len(closes) - 1 else "END_OF_DATA"
        trade.exit_ts = timestamps[last_index]

    cost = (entry_price + trade.exit_price) * commission_pct
    net_move = (trade.exit_price - entry_price) - cost
    trade.r_multiple = net_move / risk_per_share
    trade.return_pct = net_move / entry_price * 100.0
    return trade


def run_symbol(
    symbol: str,
    bars: Dict[str, Sequence],
    registry: Optional[List[S.Setup]] = None,
    benchmark_closes: Optional[Sequence[float]] = None,
    regimes_by_ts: Optional[Dict[str, str]] = None,
    max_bars: int = 20,
    start: int = 200,
    respect_regime: bool = True,
    **costs: float,
) -> List[Trade]:
    """شغّل الاختبار على سهم واحد وأرجع كل الصفقات المحاكاة.

    ``bars`` قاموس فيه open/high/low/close/volume/ts كقوائم متوازية.
    """
    opens, highs = list(bars["open"]), list(bars["high"])
    lows, closes = list(bars["low"]), list(bars["close"])
    volumes, timestamps = list(bars["volume"]), list(bars["ts"])

    snapshots = F.build_series(symbol, opens, highs, lows, closes, volumes,
                               timestamps, benchmark_closes, start=start)
    registry = registry if registry is not None else S.default_registry()

    trades: List[Trade] = []
    # منع الدخول المتكرر على نفس السهم بينما صفقة سابقة ما زالت مفتوحة.
    busy_until = -1

    for offset, snap in enumerate(snapshots):
        i = start + offset
        if i <= busy_until:
            continue
        if regimes_by_ts:
            snap.regime = regimes_by_ts.get(snap.ts)

        for match in S.evaluate_all(snap, registry, respect_regime):
            trade = simulate_trade(
                match, opens, highs, lows, closes, timestamps, i,
                max_bars=max_bars, regime=snap.regime,
                **{k: v for k, v in costs.items()
                   if k in ("commission_pct", "slippage_pct")},
            )
            if trade:
                trades.append(trade)
                busy_until = i + trade.bars_held
                break        # نمط واحد لكل إشارة: الأعلى درجة
    return trades


def aggregate(trades: Sequence[Trade]) -> Dict[Tuple[str, str], SetupStats]:
    """اجمع الصفقات إلى إحصاءات لكل (نمط، حالة سوق)."""
    buckets: Dict[Tuple[str, str], List[Trade]] = {}
    for trade in trades:
        key = (trade.setup, trade.regime or "UNKNOWN")
        buckets.setdefault(key, []).append(trade)

    out: Dict[Tuple[str, str], SetupStats] = {}
    for (setup, regime), group in buckets.items():
        stats = SetupStats(setup=setup, regime=regime, trades=len(group))
        r_values = [t.r_multiple for t in group]
        stats.wins = sum(1 for r in r_values if r > 0)
        stats.losses = sum(1 for r in r_values if r <= 0)
        stats.gross_profit = sum(r for r in r_values if r > 0)
        stats.gross_loss = abs(sum(r for r in r_values if r <= 0))
        stats.avg_r = sum(r_values) / len(r_values)
        stats.expectancy = stats.avg_r
        stats.hit_rate = stats.wins / len(group)
        stats.profit_factor = (stats.gross_profit / stats.gross_loss
                               if stats.gross_loss else None)
        stats.avg_bars_held = sum(t.bars_held for t in group) / len(group)

        # أقصى تراجع في منحنى R التراكمي
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in r_values:
            equity += r
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        stats.max_drawdown_r = max_dd

        out[(setup, regime)] = stats
    return out


def save_performance(conn: sqlite3.Connection,
                     stats: Dict[Tuple[str, str], SetupStats]) -> None:
    """احفظ نتائج القياس ليستخدمها محرك القرار في ترجيح الأنماط."""
    now = datetime.now().isoformat(timespec="seconds")
    for (setup, regime), s in stats.items():
        # الوزن من التوقع: النمط السالب يُصفَّر فلا يُنتج إشارات.
        weight = max(0.0, min(2.0, s.expectancy * 2.0)) if s.is_viable else 0.0
        conn.execute(
            """
            INSERT INTO setup_performance
                (setup, regime, trades, wins, losses, gross_profit, gross_loss,
                 avg_r, hit_rate, expectancy, weight, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(setup, regime) DO UPDATE SET
                trades = excluded.trades, wins = excluded.wins,
                losses = excluded.losses, gross_profit = excluded.gross_profit,
                gross_loss = excluded.gross_loss, avg_r = excluded.avg_r,
                hit_rate = excluded.hit_rate, expectancy = excluded.expectancy,
                weight = excluded.weight, updated_at = excluded.updated_at
            """,
            (setup, regime, s.trades, s.wins, s.losses, s.gross_profit,
             s.gross_loss, s.avg_r, s.hit_rate, s.expectancy, weight, now),
        )
    conn.commit()


def summary_report(stats: Dict[Tuple[str, str], SetupStats],
                   min_trades: int = 1) -> List[Dict[str, object]]:
    """تقرير مرتّب بالتوقع، مع إخفاء العيّنات الصغيرة جداً."""
    rows = [s.to_dict() for s in stats.values() if s.trades >= min_trades]
    return sorted(rows, key=lambda r: r["التوقع"], reverse=True)

# ---------------------------------------------------------------------------
# التحقق خارج العيّنة - out-of-sample validation
# ---------------------------------------------------------------------------
def split_by_time(trades: Sequence[Trade], train_fraction: float = 0.6
                  ) -> Tuple[List[Trade], List[Trade], str]:
    """اقسم الصفقات زمنياً إلى تدريب وتحقق. يُرجع (تدريب، تحقق، تاريخ القطع)."""
    ordered = sorted(trades, key=lambda t: t.entry_ts)
    if len(ordered) < 10:
        return list(ordered), [], ""
    cut_index = int(len(ordered) * train_fraction)
    cut_ts = ordered[cut_index].entry_ts
    return ([t for t in ordered if t.entry_ts < cut_ts],
            [t for t in ordered if t.entry_ts >= cut_ts], cut_ts)


def validate(trades: Sequence[Trade], train_fraction: float = 0.6,
             min_trades_each: int = 30) -> Dict[Tuple[str, str], Dict[str, object]]:
    """هل تصمد أفضلية كل نمط على بيانات لم يُقس عليها؟

    نمط يربح داخل العيّنة ويخسر خارجها ليس أفضلية بل ملاءمة زائدة. هذا
    الفحص هو الفرق بين اكتشاف أفضلية واكتشاف رقم، ولا يُعطى وزن موجب
    لأي نمط لم يجتزه.
    """
    train, test, cut = split_by_time(trades, train_fraction)
    stats_train = aggregate(train)
    stats_test = aggregate(test)

    out: Dict[Tuple[str, str], Dict[str, object]] = {}
    for key in set(stats_train) | set(stats_test):
        a = stats_train.get(key)
        b = stats_test.get(key)
        enough = bool(a and b and a.trades >= min_trades_each
                      and b.trades >= min_trades_each)
        survived = bool(enough and a.expectancy > 0 and b.expectancy > 0)
        out[key] = {
            "setup": key[0], "regime": key[1], "cut_ts": cut,
            "train_trades": a.trades if a else 0,
            "train_expectancy": round(a.expectancy, 4) if a else None,
            "test_trades": b.trades if b else 0,
            "test_expectancy": round(b.expectancy, 4) if b else None,
            "enough_data": enough,
            "survived": survived,
        }
    return out


def save_validated_performance(
    conn: sqlite3.Connection,
    trades: Sequence[Trade],
    train_fraction: float = 0.6,
    min_trades_each: int = 30,
) -> Dict[Tuple[str, str], Dict[str, object]]:
    """احفظ الأداء، لكن لا تمنح وزناً موجباً إلا لنمط صمد خارج العيّنة.

    هذا هو الحارس الذي يمنع نشر نظام مُلائم زيادة. الأرقام كلها تُحفظ
    للاطلاع، والوزن وحده هو ما يقرر إن كان النمط سيُنتج إشارات.
    """
    full = aggregate(trades)
    verdicts = validate(trades, train_fraction, min_trades_each)
    now = datetime.now().isoformat(timespec="seconds")

    for key, stats in full.items():
        verdict = verdicts.get(key, {})
        survived = bool(verdict.get("survived"))
        test_expectancy = verdict.get("test_expectancy")

        # الوزن يُشتق من الأداء خارج العيّنة لا من الأداء الكلي.
        weight = 0.0
        if survived and test_expectancy:
            weight = max(0.0, min(2.0, float(test_expectancy) * 2.0))

        conn.execute(
            """
            INSERT INTO setup_performance
                (setup, regime, trades, wins, losses, gross_profit, gross_loss,
                 avg_r, hit_rate, expectancy, weight, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(setup, regime) DO UPDATE SET
                trades = excluded.trades, wins = excluded.wins,
                losses = excluded.losses, gross_profit = excluded.gross_profit,
                gross_loss = excluded.gross_loss, avg_r = excluded.avg_r,
                hit_rate = excluded.hit_rate, expectancy = excluded.expectancy,
                weight = excluded.weight, updated_at = excluded.updated_at
            """,
            (key[0], key[1], stats.trades, stats.wins, stats.losses,
             stats.gross_profit, stats.gross_loss, stats.avg_r,
             stats.hit_rate, stats.expectancy, weight, now),
        )
    conn.commit()
    return verdicts


def validation_report(verdicts: Dict[Tuple[str, str], Dict[str, object]],
                      min_trades: int = 30) -> List[Dict[str, object]]:
    """تقرير التحقق مرتّباً بأداء خارج العيّنة."""
    rows = []
    for verdict in verdicts.values():
        if (verdict["train_trades"] or 0) < min_trades:
            continue
        rows.append({
            "النمط": S.SETUP_NAMES_AR.get(verdict["setup"], verdict["setup"]),
            "الحالة": rg.REGIME_AR.get(verdict["regime"], verdict["regime"]),
            "صفقات التدريب": verdict["train_trades"],
            "توقع التدريب": verdict["train_expectancy"],
            "صفقات التحقق": verdict["test_trades"],
            "توقع التحقق": verdict["test_expectancy"],
            "صمد": "نعم" if verdict["survived"] else "لا",
        })
    return sorted(rows, key=lambda r: (r["توقع التحقق"] is None,
                                       -(r["توقع التحقق"] or -99)))
