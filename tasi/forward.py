"""
tasi/forward.py
===============
اختبار تقدّمي بمحفظة حقيقية - walk-forward paper trading.

الفرق عن backtest.py: هناك نقيس كل صفقة معزولة بوحدات المخاطرة، وهنا
نشغّل محفظة واحدة يوماً بيوم برأس مال محدود وحدود خسارة فعلية. الفرق
عملي: الاختبار التاريخي قد يُظهر توقعاً موجباً بينما المحفظة تنهار، لأن
الصفقات تتزاحم على رأس المال نفسه وتتراكم خسائرها في وقت واحد.

ما يقيسه هذا الملف هو ما يهم قبل أي إطلاق:
    - منحنى رأس المال وأقصى تراجع
    - نسبة الصفقات الخاسرة وأطول سلسلة خسائر متتالية
    - أسوأ يوم وأسوأ أسبوع
    - كم مرة كانت حدود الخسارة ستوقف التداول
    - احتمال خسارة نسبة معيّنة من رأس المال

القاعدة هنا كما في كل الملفات: لا تسرّب من المستقبل. القرار في اليوم t
يُبنى على بيانات حتى t فقط، والتنفيذ عند افتتاح t+1.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from . import features as F
from . import quality as ql
from . import regime as rg
from . import setups as S
from .risk import RiskManager


@dataclass
class PaperPosition:
    symbol: str
    shares: int
    entry_price: float
    entry_ts: str
    stop_price: float
    target_price: Optional[float]
    setup: str
    regime: Optional[str]

    def value_at(self, price: float) -> float:
        return self.shares * price

    def pnl_at(self, price: float) -> float:
        return (price - self.entry_price) * self.shares


@dataclass
class ClosedTrade:
    symbol: str
    setup: str
    regime: Optional[str]
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    reason: str
    days_held: int


@dataclass
class ForwardReport:
    """حصيلة الاختبار التقدّمي - ما يلزم لقرار الإطلاق."""

    start_equity: float
    end_equity: float = 0.0
    peak_equity: float = 0.0
    sessions: int = 0
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    trades: List[ClosedTrade] = field(default_factory=list)
    daily_returns: List[Tuple[str, float]] = field(default_factory=list)
    halt_days: List[Tuple[str, str]] = field(default_factory=list)
    signals_generated: int = 0
    signals_rejected_by_risk: int = 0

    # -- نتائج -----------------------------------------------------
    @property
    def total_return_pct(self) -> float:
        if not self.start_equity:
            return 0.0
        return (self.end_equity - self.start_equity) / self.start_equity * 100

    @property
    def max_drawdown_pct(self) -> float:
        """أقصى تراجع عن قمة سابقة - أهم رقم في تقييم المخاطرة."""
        peak = self.start_equity
        worst = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak:
                worst = max(worst, (peak - equity) / peak * 100)
        return worst

    @property
    def win_rate(self) -> Optional[float]:
        if not self.trades:
            return None
        return sum(1 for t in self.trades if t.pnl > 0) / len(self.trades)

    @property
    def loss_rate(self) -> Optional[float]:
        rate = self.win_rate
        return None if rate is None else 1.0 - rate

    @property
    def longest_losing_streak(self) -> int:
        streak = worst = 0
        for trade in sorted(self.trades, key=lambda t: t.exit_ts):
            if trade.pnl <= 0:
                streak += 1
                worst = max(worst, streak)
            else:
                streak = 0
        return worst

    @property
    def worst_day(self) -> Optional[Tuple[str, float]]:
        return min(self.daily_returns, key=lambda x: x[1]) if self.daily_returns else None

    @property
    def best_day(self) -> Optional[Tuple[str, float]]:
        return max(self.daily_returns, key=lambda x: x[1]) if self.daily_returns else None

    @property
    def average_trade_pnl(self) -> Optional[float]:
        if not self.trades:
            return None
        return sum(t.pnl for t in self.trades) / len(self.trades)

    @property
    def profit_factor(self) -> Optional[float]:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = abs(sum(t.pnl for t in self.trades if t.pnl <= 0))
        return gains / losses if losses else None

    def loss_probability(self, threshold_pct: float) -> float:
        """نسبة الجلسات التي كان رأس المال فيها أدنى من العتبة.

        ليست احتمالاً مستقبلياً بل تكراراً تاريخياً مقاساً، والفرق مهم:
        الماضي لا يضمن المستقبل لكنه أصدق ما لدينا.
        """
        if not self.equity_curve:
            return 0.0
        floor = self.start_equity * (1 - threshold_pct / 100)
        below = sum(1 for _, equity in self.equity_curve if equity < floor)
        return below / len(self.equity_curve)

    def render_ar(self) -> str:
        lines: List[str] = []
        add = lines.append
        add("═" * 64)
        add("تقرير الاختبار التقدّمي")
        add("═" * 64)
        add("")
        add("رأس المال")
        add("─" * 64)
        add(f"  البداية        : {self.start_equity:>12,.2f} ريال")
        add(f"  النهاية        : {self.end_equity:>12,.2f} ريال")
        add(f"  القمة          : {self.peak_equity:>12,.2f} ريال")
        add(f"  العائد الكلي   : {self.total_return_pct:>12.2f}%")
        add(f"  أقصى تراجع     : {self.max_drawdown_pct:>12.2f}%")
        add(f"  عدد الجلسات    : {self.sessions:>12}")
        add("")
        add("الصفقات")
        add("─" * 64)
        add(f"  إشارات مولّدة       : {self.signals_generated}")
        add(f"  مرفوضة من المخاطر   : {self.signals_rejected_by_risk}")
        add(f"  صفقات منفّذة        : {len(self.trades)}")
        if self.trades:
            add(f"  نسبة الربح          : {self.win_rate:.1%}")
            add(f"  نسبة الخسارة        : {self.loss_rate:.1%}")
            add(f"  متوسط الصفقة        : {self.average_trade_pnl:+,.2f} ريال")
            factor = self.profit_factor
            add(f"  عامل الربح          : "
                f"{factor:.2f}" if factor else "  عامل الربح          : لا خسائر")
            add(f"  أطول سلسلة خسائر    : {self.longest_losing_streak} صفقة متتالية")
            reasons: Dict[str, int] = {}
            for trade in self.trades:
                reasons[trade.reason] = reasons.get(trade.reason, 0) + 1
            add(f"  أسباب الخروج        : "
                + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
        else:
            add("  لم تُنفَّذ أي صفقة.")
        add("")
        add("المخاطرة اليومية")
        add("─" * 64)
        worst = self.worst_day
        best = self.best_day
        if worst:
            add(f"  أسوأ يوم       : {worst[1]:+.2f}%  ({worst[0]})")
        if best:
            add(f"  أفضل يوم       : {best[1]:+.2f}%  ({best[0]})")
        add(f"  أيام توقّف التداول بحدود الخسارة : {len(self.halt_days)}")
        for day, reason in self.halt_days[:5]:
            add(f"     {day}: {reason[:60]}")
        add("")
        add("احتمال الخسارة (تكرار تاريخي مقاس)")
        add("─" * 64)
        for threshold in (5, 10, 20, 30):
            share = self.loss_probability(threshold)
            add(f"  رأس المال دون -{threshold:>2}% : "
                f"{share:>6.1%} من الجلسات")
        add("")
        add("═" * 64)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
def run_forward(
    conn: sqlite3.Connection,
    symbols: Sequence[str],
    capital: float = 100_000.0,
    benchmark: str = "TASI",
    registry: Optional[List[S.Setup]] = None,
    weights: Optional[Dict[Tuple[str, str], float]] = None,
    start_index: int = 250,
    max_hold_days: int = 15,
    min_score: float = 50.0,
    max_open: int = 5,
    commission_pct: float = 0.00155,
    slippage_pct: float = 0.001,
    risk_per_trade: float = 0.01,
    daily_loss_limit: float = 0.03,
    weekly_loss_limit: float = 0.06,
    allow_uncalibrated: bool = False,
    on_progress=None,
) -> ForwardReport:
    """شغّل محفظة واحدة يوماً بيوم عبر التاريخ المخزّن."""
    from .sweep import load_series

    bench = load_series(conn, benchmark)
    if not bench:
        raise ValueError(f"لا توجد بيانات للمؤشر {benchmark}")

    # حالة السوق لكل يوم، محسوبة من بيانات ذلك اليوم فما قبل
    regimes: Dict[str, str] = {}
    for i in range(60, len(bench["close"])):
        regimes[bench["ts"][i]] = rg.classify(
            bench["close"][:i + 1], as_of=bench["ts"][i]).regime

    # حضّر الخصائص مرة واحدة
    prepared: Dict[str, dict] = {}
    for symbol in symbols:
        if symbol == benchmark:
            continue
        bars = load_series(conn, symbol)
        if not bars or len(bars["close"]) < start_index + 30:
            continue
        if not ql.check_bars(symbol, bars).ok:
            continue
        bench_arg = (bench["close"]
                     if len(bench["close"]) == len(bars["close"]) else None)
        snaps = F.build_series(symbol, bars["open"], bars["high"], bars["low"],
                               bars["close"], bars["volume"], bars["ts"],
                               bench_arg, start=start_index)
        index_by_ts = {s.ts: start_index + k for k, s in enumerate(snaps)}
        prepared[symbol] = {"bars": bars, "snaps": snaps, "idx": index_by_ts}

    if not prepared:
        raise ValueError("لا توجد أسهم صالحة بعد فحص الجودة.")

    registry = registry if registry is not None else S.default_registry()
    report = ForwardReport(start_equity=capital, peak_equity=capital)

    # تقويم موحّد من المؤشر
    calendar = bench["ts"][start_index:]
    cash = capital
    open_positions: Dict[str, PaperPosition] = {}
    realized_by_day: Dict[str, float] = {}
    previous_equity = capital

    def weight_of(setup: str, regime: Optional[str]) -> float:
        if weights is None:
            return 1.0 if allow_uncalibrated else 0.0
        return weights.get((setup, regime or "UNKNOWN"), 0.0)

    for day_number, today in enumerate(calendar):
        # ── 1) سعّر المراكز المفتوحة وأغلق ما بلغ وقفه أو هدفه ──
        for symbol in list(open_positions):
            position = open_positions[symbol]
            blob = prepared.get(symbol)
            if not blob or today not in blob["idx"]:
                continue
            i = blob["idx"][today]
            bars = blob["bars"]
            low, high, close = bars["low"][i], bars["high"][i], bars["close"][i]

            exit_price = exit_reason = None
            if low <= position.stop_price:                 # الوقف أولاً
                exit_price = position.stop_price * (1 - slippage_pct)
                exit_reason = "STOP"
            elif position.target_price and high >= position.target_price:
                exit_price = position.target_price * (1 - slippage_pct)
                exit_reason = "TARGET"
            else:
                held = day_number - calendar.index(position.entry_ts) \
                    if position.entry_ts in calendar else 0
                if held >= max_hold_days:
                    exit_price = close
                    exit_reason = "TIME"

            if exit_price is not None:
                gross = position.shares * exit_price
                cost = gross * commission_pct
                cash += gross - cost
                pnl = position.pnl_at(exit_price) - cost
                report.trades.append(ClosedTrade(
                    symbol=symbol, setup=position.setup, regime=position.regime,
                    entry_ts=position.entry_ts, exit_ts=today,
                    entry_price=position.entry_price, exit_price=exit_price,
                    shares=position.shares, pnl=pnl,
                    pnl_pct=(exit_price - position.entry_price)
                    / position.entry_price * 100,
                    reason=exit_reason,
                    days_held=max(1, day_number - calendar.index(position.entry_ts))
                    if position.entry_ts in calendar else 1))
                realized_by_day[today] = realized_by_day.get(today, 0.0) + pnl
                open_positions.pop(symbol)

        # ── 2) احسب رأس المال الحالي ──
        market_value = 0.0
        for symbol, position in open_positions.items():
            blob = prepared.get(symbol)
            if blob and today in blob["idx"]:
                market_value += position.value_at(
                    blob["bars"]["close"][blob["idx"][today]])
            else:
                market_value += position.value_at(position.entry_price)
        equity = cash + market_value
        report.equity_curve.append((today, equity))
        report.peak_equity = max(report.peak_equity, equity)
        report.sessions += 1
        if previous_equity:
            report.daily_returns.append(
                (today, (equity - previous_equity) / previous_equity * 100))
        previous_equity = equity

        # ── 3) حدود الخسارة ──
        today_pnl = realized_by_day.get(today, 0.0)
        week_start = RiskManager.week_start(date.fromisoformat(today)).isoformat()
        week_pnl = sum(v for k, v in realized_by_day.items() if k >= week_start)

        halted = ""
        if week_pnl <= -capital * weekly_loss_limit:
            halted = (f"حد الخسارة الأسبوعي ({weekly_loss_limit:.0%}) "
                      f"عند {week_pnl:,.0f} ريال")
        elif today_pnl <= -capital * daily_loss_limit:
            halted = (f"حد الخسارة اليومي ({daily_loss_limit:.0%}) "
                      f"عند {today_pnl:,.0f} ريال")
        if halted:
            report.halt_days.append((today, halted))
            if on_progress and day_number % 250 == 0:
                on_progress(day_number, len(calendar), equity)
            continue

        # ── 4) ابحث عن دخول جديد ──
        if len(open_positions) >= max_open:
            if on_progress and day_number % 250 == 0:
                on_progress(day_number, len(calendar), equity)
            continue

        regime = regimes.get(today)
        candidates = []
        for symbol, blob in prepared.items():
            if symbol in open_positions or today not in blob["idx"]:
                continue
            i = blob["idx"][today]
            snap = blob["snaps"][i - start_index]
            snap.regime = regime
            if not snap.is_complete:
                continue
            for match in S.evaluate_all(snap, registry, respect_regime=True):
                if match.score < min_score:
                    continue
                if weight_of(match.setup, regime) <= 0:
                    continue
                candidates.append((match, symbol, i, snap))
                break

        candidates.sort(key=lambda c: c[0].score, reverse=True)
        risk_budget = equity * risk_per_trade

        for match, symbol, i, snap in candidates:
            if len(open_positions) >= max_open:
                break
            report.signals_generated += 1

            blob = prepared[symbol]
            if i + 1 >= len(blob["bars"]["open"]):
                continue
            entry_price = blob["bars"]["open"][i + 1] * (1 + slippage_pct)
            stop = match.stop_hint
            if stop is None or entry_price <= stop:
                report.signals_rejected_by_risk += 1
                continue

            shares = int(risk_budget / (entry_price - stop))
            cost_basis = shares * entry_price
            if shares < 1 or cost_basis > cash * 0.95:
                shares = int((cash * 0.95) / entry_price)
            if shares < 1:
                report.signals_rejected_by_risk += 1
                continue

            cost_basis = shares * entry_price
            commission = cost_basis * commission_pct
            if cost_basis + commission > cash:
                report.signals_rejected_by_risk += 1
                continue

            cash -= cost_basis + commission
            entry_ts = blob["bars"]["ts"][i + 1]
            open_positions[symbol] = PaperPosition(
                symbol=symbol, shares=shares, entry_price=entry_price,
                entry_ts=entry_ts, stop_price=stop,
                target_price=match.target_hint, setup=match.setup,
                regime=regime)

        if on_progress and day_number % 250 == 0:
            on_progress(day_number, len(calendar), equity)

    # ── إغلاق ما تبقّى بآخر سعر ──
    final_value = 0.0
    last_day = calendar[-1] if calendar else ""
    for symbol, position in open_positions.items():
        blob = prepared.get(symbol)
        price = (blob["bars"]["close"][blob["idx"][last_day]]
                 if blob and last_day in blob["idx"] else position.entry_price)
        final_value += position.value_at(price)
    report.end_equity = cash + final_value
    return report
