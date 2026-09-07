"""
tasi/engine.py
==============
منسّق الجلسة - the live session orchestrator.

يربط كل الطبقات في دورة واحدة:

    المزوّد ← الجودة ← الخصائص ← حالة السوق ← الأنماط ← الأوزان المقيسة
            ← المخاطر ← اقتراح الأمر ← تيليجرام ← التسجيل

ثلاثة حرّاس مبنية في المسار، وكل واحد منها يمنع نوعاً من الفشل الصامت:

    1. حارس القدرات : لا يعمل وضع لحظي على بيانات مؤجلة ربع ساعة.
    2. حارس الجودة  : لا تُحسب مؤشرات على سلسلة فاسدة.
    3. حارس الأوزان : النمط غير المعاير أو سالب التوقع لا يُنتج إشارة.

الحارس الثالث هو الأهم: بدونه يتحوّل النظام إلى مولّد إشارات بمعاملات
مخمّنة، وهو بالضبط ما نتجنبه. الوضع الافتراضي يرفض العمل بأنماط غير
معايرة، ويحتاج ``allow_uncalibrated=True`` صراحة للتجربة.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from . import features as F
from . import quality as ql
from . import regime as rg
from . import setups as S
from . import universe as un
from .providers.base import (MarketDataProvider, ProviderCapabilityError,
                             ProviderError, Quote)
from .risk import PositionSize, RiskManager
from .notifier import TelegramNotifier

log = logging.getLogger("tasi.engine")

# جلسة تداول: الأحد إلى الخميس، 10:00 إلى 15:00 بتوقيت الرياض
RIYADH = timezone(timedelta(hours=3))
SESSION_OPEN = dtime(10, 0)
SESSION_CLOSE = dtime(15, 0)
TRADING_WEEKDAYS = {6, 0, 1, 2, 3}      # الأحد=6 .. الخميس=3

MIN_HISTORY_BARS = 60                   # أقل تاريخ يسمح ببناء خصائص ذات معنى


def now_riyadh() -> datetime:
    return datetime.now(RIYADH)


def session_is_open(moment: Optional[datetime] = None) -> bool:
    moment = moment or now_riyadh()
    if moment.weekday() not in TRADING_WEEKDAYS:
        return False
    return SESSION_OPEN <= moment.time() <= SESSION_CLOSE


@dataclass
class Proposal:
    """اقتراح أمر جاهز للعرض على المستخدم لينفّذه."""

    symbol: str
    name_ar: str
    action: str                     # BUY / SELL
    setup: str
    price: float
    shares: int
    stop_price: float
    target_price: Optional[float]
    score: float
    weight: float                   # وزن النمط المقيس
    regime: Optional[str]
    reason_ar: str
    risk_amount: float
    position_value: float
    calibrated: bool
    signal_id: Optional[int] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol, "name_ar": self.name_ar,
            "action": self.action, "setup": self.setup,
            "price": round(self.price, 4), "shares": self.shares,
            "stop_price": round(self.stop_price, 4),
            "target_price": round(self.target_price, 4) if self.target_price else None,
            "score": round(self.score, 1), "weight": round(self.weight, 3),
            "regime": self.regime, "reason_ar": self.reason_ar,
            "risk_amount": round(self.risk_amount, 2),
            "position_value": round(self.position_value, 2),
            "calibrated": self.calibrated,
        }


@dataclass
class CycleResult:
    """حصيلة دورة واحدة."""

    ts: str
    regime: Optional[str] = None
    scanned: int = 0
    quotes_received: int = 0
    proposals: List[Proposal] = field(default_factory=list)
    rejected: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    skipped_quality: List[str] = field(default_factory=list)

    def summary_ar(self) -> str:
        parts = [
            f"فُحص {self.scanned} سهماً",
            f"أسعار {self.quotes_received}",
            f"اقتراحات {len(self.proposals)}",
        ]
        if self.regime:
            parts.insert(0, rg.REGIME_AR.get(self.regime, self.regime))
        if self.skipped_quality:
            parts.append(f"مرفوض للجودة {len(self.skipped_quality)}")
        if self.errors:
            parts.append(f"أخطاء {len(self.errors)}")
        return " | ".join(parts)


class Engine:
    """محرك الجلسة."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: MarketDataProvider,
        risk: Optional[RiskManager] = None,
        notifier: Optional[TelegramNotifier] = None,
        registry: Optional[List[S.Setup]] = None,
        shariah_allow: Optional[Sequence[str]] = None,
        shariah_source: Optional[str] = None,
        benchmark: str = "TASI",
        allow_uncalibrated: bool = False,
        min_score: float = 50.0,
    ) -> None:
        self.conn = conn
        self.provider = provider
        self.risk = risk or RiskManager(conn=conn)
        self.notifier = notifier or TelegramNotifier()
        self.registry = registry if registry is not None else S.default_registry()
        self.shariah_allow = list(shariah_allow) if shariah_allow else None
        self.shariah_source = shariah_source
        self.benchmark = benchmark
        self.allow_uncalibrated = allow_uncalibrated
        self.min_score = min_score
        self._weights: Optional[Dict[tuple, float]] = None

    # ------------------------------------------------------------------
    def preflight(self, require_realtime: bool = False) -> Dict[str, object]:
        """افحص الجاهزية قبل بدء الجلسة.

        الفشل هنا أرخص بكثير من اكتشافه بعد فتح السوق، فكل شرط يُفحص
        صراحة ويُرجع سبباً مقروءاً بدل استثناء غامض لاحقاً.
        """
        checks: Dict[str, object] = {"ok": True, "problems": []}

        health = self.provider.health_check()
        checks["provider"] = health
        if not health.get("ok"):
            checks["ok"] = False
            checks["problems"].append(
                f"المزوّد لا يستجيب: {health.get('error', 'سبب غير معروف')}")

        if require_realtime:
            try:
                self.provider.capabilities.require(realtime=True, websocket=True)
            except ProviderCapabilityError as exc:
                checks["ok"] = False
                checks["problems"].append(str(exc))

        universe = self.tradable_symbols()
        checks["tradable_symbols"] = len(universe)
        if not universe:
            checks["ok"] = False
            checks["problems"].append(
                "لا توجد أسهم مسموح تداولها. تحقق من استيراد التصنيف الشرعي.")

        weights = self.setup_weights()
        active = {k: v for k, v in weights.items() if v > 0}
        checks["calibrated_setups"] = len(active)
        if not active and not self.allow_uncalibrated:
            checks["ok"] = False
            checks["problems"].append(
                "لا يوجد نمط معاير بوزن موجب. شغّل الاختبار التاريخي أولاً "
                "(backtest --save)، أو مرّر allow_uncalibrated=True للتجربة.")

        status = self.risk.status()
        checks["risk"] = status.to_dict()
        if not status.trading_allowed:
            checks["ok"] = False
            checks["problems"].append(status.blocked_reason)

        checks["telegram"] = self.notifier.enabled
        if not self.notifier.enabled:
            checks["problems"].append(
                "تيليجرام غير مفعّل - الاقتراحات ستُسجَّل محلياً فقط.")

        checks["session_open"] = session_is_open()
        return checks

    # ------------------------------------------------------------------
    def tradable_symbols(self) -> List[str]:
        """الأسهم المسموح تداولها بعد الفلترة الشرعية."""
        if self.shariah_allow:
            return un.filter_by_shariah(
                self.conn, self.shariah_allow, self.shariah_source)
        return un.all_symbols(self.conn)

    def setup_weights(self, refresh: bool = False) -> Dict[tuple, float]:
        """أوزان الأنماط المقيسة من الاختبار التاريخي."""
        if self._weights is None or refresh:
            rows = self.conn.execute(
                "SELECT setup, regime, weight FROM setup_performance").fetchall()
            self._weights = {(r["setup"], r["regime"]): float(r["weight"] or 0)
                             for r in rows}
        return self._weights

    def weight_for(self, setup: str, regime: Optional[str]) -> float:
        weights = self.setup_weights()
        if not weights:
            # لا قياس بعد: صفر إلا إذا سُمح بالتجربة صراحة
            return 1.0 if self.allow_uncalibrated else 0.0
        return weights.get((setup, regime or "UNKNOWN"), 0.0)

    def is_calibrated(self, setup: str, regime: Optional[str]) -> bool:
        """هل لهذا النمط قياس فعلي في هذه الحالة؟

        العلم يتبع وجود صف قياس في setup_performance، لا حقل النمط نفسه.
        بدون ذلك يُوسم اقتراح مبني على قياس حقيقي بأنه غير معاير، وهو
        تضليل في الاتجاه المعاكس.
        """
        return (setup, regime or "UNKNOWN") in self.setup_weights()

    # ------------------------------------------------------------------
    def _history(self, symbol: str, limit: int = 300) -> Optional[Dict[str, List]]:
        rows = self.conn.execute(
            "SELECT ts, open, high, low, close, volume FROM bars"
            " WHERE symbol = ? AND interval = '1day' ORDER BY ts DESC LIMIT ?",
            (symbol, limit)).fetchall()
        if len(rows) < MIN_HISTORY_BARS:
            return None
        rows = list(reversed(rows))
        return {
            "ts": [r["ts"] for r in rows],
            "open": [float(r["open"] or r["close"]) for r in rows],
            "high": [float(r["high"] or r["close"]) for r in rows],
            "low": [float(r["low"] or r["close"]) for r in rows],
            "close": [float(r["close"]) for r in rows],
            "volume": [float(r["volume"] or 0) for r in rows],
        }

    def current_regime(self) -> Optional[str]:
        """صنّف حالة السوق من تاريخ المؤشر المخزّن."""
        bars = self._history(self.benchmark)
        if not bars:
            log.warning("لا يوجد تاريخ كافٍ للمؤشر %s - حالة السوق غير معروفة",
                        self.benchmark)
            return None
        breadth = rg.compute_breadth(self.conn, bars["ts"][-1])
        snap = rg.classify(bars["close"], breadth=breadth, as_of=bars["ts"][-1])
        rg.save_regime(self.conn, snap)
        return snap.regime

    def _append_live(self, bars: Dict[str, List], quote: Quote) -> Dict[str, List]:
        """ألحق السعر اللحظي كشمعة اليوم بدل استبدال التاريخ.

        إن كان آخر طابع زمني هو اليوم نفسه، يُحدَّث بدل أن يُضاف صف مكرر
        يفسد حساب المؤشرات.
        """
        today = quote.ts.strftime("%Y-%m-%d") if quote.ts else \
            now_riyadh().strftime("%Y-%m-%d")
        out = {k: list(v) for k, v in bars.items()}

        if out["ts"] and str(out["ts"][-1])[:10] == today:
            idx = -1
        else:
            for key in ("ts", "open", "high", "low", "close", "volume"):
                out[key].append(None)
            idx = -1
            out["ts"][idx] = today

        out["close"][idx] = quote.price
        out["open"][idx] = quote.open if quote.open is not None else quote.price
        out["high"][idx] = max(quote.high or quote.price, quote.price)
        out["low"][idx] = min(quote.low or quote.price, quote.price)
        out["volume"][idx] = quote.volume or 0.0
        return out

    # ------------------------------------------------------------------
    def run_cycle(self, symbols: Optional[Sequence[str]] = None,
                  dry_run: bool = True) -> CycleResult:
        """دورة واحدة كاملة: جلب، تحليل، قرار، تنبيه."""
        result = CycleResult(ts=now_riyadh().isoformat(timespec="seconds"))

        status = self.risk.status()
        if not status.trading_allowed:
            result.errors.append(status.blocked_reason)
            self.notifier.notify_risk_block(status.blocked_reason)
            return result

        universe = list(symbols) if symbols else self.tradable_symbols()
        result.scanned = len(universe)
        if not universe:
            result.errors.append("لا توجد أسهم مسموح تداولها.")
            return result

        result.regime = self.current_regime()

        try:
            quotes = self.provider.get_quotes(universe)
        except ProviderError as exc:
            result.errors.append(f"فشل جلب الأسعار: {exc}")
            self.notifier.notify_error(f"فشل جلب الأسعار: {exc}")
            return result
        result.quotes_received = len(quotes)

        bench_bars = self._history(self.benchmark)
        bench_closes = bench_bars["close"] if bench_bars else None

        for symbol in universe:
            quote = quotes.get(symbol)
            if quote is None:
                result.rejected[symbol] = "لم يصل سعر"
                continue

            history = self._history(symbol)
            if history is None:
                result.rejected[symbol] = "تاريخ غير كافٍ"
                continue

            report = ql.check_bars(symbol, history)
            if not report.ok:
                result.skipped_quality.append(symbol)
                result.rejected[symbol] = f"فشل فحص الجودة: {report.errors[0]}"
                continue

            bars = self._append_live(history, quote)
            bench = (bench_closes[-len(bars["close"]):]
                     if bench_closes and len(bench_closes) >= len(bars["close"])
                     else None)

            snap = F.build(symbol, bars["open"], bars["high"], bars["low"],
                           bars["close"], bars["volume"], bars["ts"],
                           benchmark_closes=bench, regime=result.regime)
            if not snap.is_complete:
                result.rejected[symbol] = "خصائص ناقصة"
                continue

            proposal = self._decide(snap, quote)
            if isinstance(proposal, Proposal):
                result.proposals.append(proposal)
                if not dry_run:
                    self._emit(proposal)
            else:
                result.rejected[symbol] = proposal

        result.proposals.sort(key=lambda p: p.score * max(p.weight, 0.01),
                              reverse=True)
        return result

    def _decide(self, snap: F.FeatureSnapshot, quote: Quote):
        """طبّق الأنماط والأوزان والمخاطر. يُرجع Proposal أو سبب رفض نصياً."""
        matches = S.evaluate_all(snap, self.registry, respect_regime=True)
        if not matches:
            return "لا نمط مطابق"

        for match in matches:
            if match.score < self.min_score:
                continue

            weight = self.weight_for(match.setup, snap.regime)
            if weight <= 0:
                continue

            company = un.get_company(self.conn, snap.symbol)
            sizing: PositionSize = self.risk.approve_entry(
                snap.symbol, quote.price, snap.atr or 0.0,
                target_price=match.target_hint, stop_price=match.stop_hint)
            if not sizing.approved:
                return f"{match.setup}: {sizing.reason}"

            return Proposal(
                symbol=snap.symbol,
                name_ar=company.name_ar if company else "",
                action="BUY", setup=match.setup, price=quote.price,
                shares=sizing.shares, stop_price=sizing.stop_price,
                target_price=match.target_hint, score=match.score,
                weight=weight, regime=snap.regime, reason_ar=match.reason_ar,
                risk_amount=sizing.risk_amount,
                position_value=sizing.position_value,
                calibrated=self.is_calibrated(match.setup, snap.regime),
            )

        top = matches[0]
        weight = self.weight_for(top.setup, snap.regime)
        if weight <= 0:
            return (f"{top.setup}: وزن النمط صفر في حالة "
                    f"{rg.REGIME_AR.get(snap.regime or '', snap.regime)}")
        return f"{top.setup}: الدرجة {top.score:.0f} دون الحد {self.min_score:.0f}"

    def _emit(self, proposal: Proposal) -> None:
        """سجّل الإشارة والأمر المقترح وأرسل التنبيه."""
        cur = self.conn.execute(
            """
            INSERT INTO signals
                (symbol, setup, action, price, score, confidence, regime,
                 features, reason_ar, created_at, delivered)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (proposal.symbol, proposal.setup, proposal.action, proposal.price,
             proposal.score, min(1.0, proposal.weight / 2.0), proposal.regime,
             json.dumps(proposal.to_dict(), ensure_ascii=False),
             proposal.reason_ar, datetime.now().isoformat(timespec="seconds")),
        )
        signal_id = int(cur.lastrowid)
        proposal.signal_id = signal_id

        self.conn.execute(
            """
            INSERT INTO orders
                (signal_id, symbol, side, order_type, quantity, limit_price,
                 status, created_at, notes)
            VALUES (?, ?, ?, 'LIMIT', ?, ?, 'PROPOSED', ?, ?)
            """,
            (signal_id, proposal.symbol, proposal.action, proposal.shares,
             proposal.price, datetime.now().isoformat(timespec="seconds"),
             proposal.reason_ar),
        )
        self.conn.commit()

        warning = "" if proposal.calibrated else "\n⚠️ نمط غير معاير - للتجربة فقط"
        delivered = self.notifier.notify_buy(
            symbol=f"{proposal.symbol} {proposal.name_ar}".strip(),
            price=proposal.price, shares=proposal.shares,
            reason=proposal.reason_ar + warning,
            target_1=proposal.target_price, stop_loss=proposal.stop_price,
            position_value=proposal.position_value,
            risk_amount=proposal.risk_amount,
        )
        if delivered:
            self.conn.execute("UPDATE signals SET delivered = 1 WHERE id = ?",
                              (signal_id,))
            self.conn.commit()

    # ------------------------------------------------------------------
    def render_cycle_ar(self, result: CycleResult) -> str:
        """اعرض حصيلة الدورة نصاً."""
        lines = [
            "=" * 62,
            f"دورة {result.ts}",
            f"  {result.summary_ar()}",
            "=" * 62,
        ]
        if result.errors:
            lines.append("أخطاء:")
            lines += [f"  - {e}" for e in result.errors]

        if result.proposals:
            lines.append("")
            lines.append("الاقتراحات (الأعلى أولاً):")
            for p in result.proposals:
                flag = "" if p.calibrated else "  [غير معاير]"
                lines.append(
                    f"  {p.symbol} {p.name_ar} | {p.action} {p.shares} سهم"
                    f" @ {p.price:.2f} | وقف {p.stop_price:.2f}"
                    f" | درجة {p.score:.0f} وزن {p.weight:.2f}{flag}")
                lines.append(f"      {p.reason_ar}")
        else:
            lines.append("")
            lines.append("لا اقتراحات في هذه الدورة.")

        if result.rejected:
            lines.append("")
            lines.append(f"أسباب عدم الاقتراح ({len(result.rejected)} سهماً):")
            for symbol, reason in list(result.rejected.items())[:12]:
                lines.append(f"  {symbol}: {reason}")
        return "\n".join(lines)
