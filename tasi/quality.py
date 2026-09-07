"""
tasi/quality.py
===============
فحص جودة البيانات - catches corrupt series before they become decisions.

السبب في وجود هذا الملف: أي خلل في ترتيب الشموع أو في تعديل التجزئة
يُنتج حركات سعرية وهمية ضخمة. المؤشرات تُحسب عليها بلا اعتراض، والأنماط
تتطابق، والاختبار التاريخي يخرج بأرقام تبدو سليمة وهي بلا معنى.

الفحوص هنا رخيصة وتُشغَّل قبل أي قياس:
    - ترتيب الطوابع الزمنية وتكرارها
    - قفزات سعرية تتجاوز حدود السوق المعقولة
    - أسعار غير منطقية (أعلى أقل من أدنى، أسعار صفرية أو سالبة)
    - فجوات وأحجام صفرية

الحد اليومي في تداول ±10% للسوق الرئيسية، فأي حركة تتجاوز ذلك بوضوح
إما تجزئة غير معدّلة أو بيانات فاسدة، لا حركة حقيقية.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

# حد التذبذب اليومي في السوق الرئيسية ±10%. نترك هامشاً للسوق الموازية
# (نمو) التي حدها ±30%، ولحالات الإدراج الجديد.
MAIN_MARKET_LIMIT_PCT = 10.0
SUSPICIOUS_MOVE_PCT = 32.0      # فوق هذا: تجزئة أو بيانات فاسدة شبه مؤكدة


@dataclass
class QualityReport:
    symbol: str
    bars: int = 0
    ok: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    max_move_pct: Optional[float] = None
    suspicious_dates: List[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.ok = False

    def summary_ar(self) -> str:
        if self.ok and not self.warnings:
            return f"{self.symbol}: سليم ({self.bars} شمعة)"
        parts = [f"{self.symbol}: {self.bars} شمعة"]
        if self.errors:
            parts.append(f"أخطاء {len(self.errors)}: " + " | ".join(self.errors[:3]))
        if self.warnings:
            parts.append(f"تنبيهات {len(self.warnings)}: " + " | ".join(self.warnings[:3]))
        return " || ".join(parts)


def check_bars(symbol: str, bars: Dict[str, Sequence],
               limit_pct: float = SUSPICIOUS_MOVE_PCT) -> QualityReport:
    """افحص سلسلة شموع. الأخطاء تمنع القياس، والتنبيهات تُسجَّل فقط."""
    ts = list(bars.get("ts", []))
    o = list(bars.get("open", []))
    h = list(bars.get("high", []))
    l = list(bars.get("low", []))
    c = list(bars.get("close", []))
    v = list(bars.get("volume", []))

    report = QualityReport(symbol=symbol, bars=len(c))
    if not c:
        report.add_error("لا توجد شموع")
        return report

    lengths = {len(x) for x in (ts, o, h, l, c) if x}
    if len(lengths) > 1:
        report.add_error(f"أطوال السلاسل غير متطابقة: {sorted(lengths)}")
        return report

    # 1) ترتيب الطوابع الزمنية - الخطأ الأخطر لأنه صامت تماماً
    unsorted_at = [i for i in range(1, len(ts)) if ts[i] <= ts[i - 1]]
    if unsorted_at:
        report.add_error(
            f"الطوابع الزمنية غير مرتّبة تصاعدياً عند {len(unsorted_at)} موضع "
            f"(أولها {ts[unsorted_at[0]]}). الترتيب الخاطئ يُنتج حركات وهمية.")

    duplicates = len(ts) - len(set(ts))
    if duplicates:
        report.add_error(f"{duplicates} طابع زمني مكرر")

    # 2) أسعار غير منطقية
    for i in range(len(c)):
        if c[i] is None or c[i] <= 0:
            report.add_error(f"سعر إغلاق غير صالح عند {ts[i] if i < len(ts) else i}")
            break
        if h and l and h[i] < l[i]:
            report.add_error(f"أعلى أقل من أدنى عند {ts[i] if i < len(ts) else i}")
            break
        if h and l and not (l[i] <= c[i] <= h[i]):
            report.warnings.append(
                f"الإغلاق خارج نطاق الشمعة عند {ts[i] if i < len(ts) else i}")
            break

    # 3) قفزات سعرية مستحيلة
    moves = []
    for i in range(1, len(c)):
        if c[i - 1]:
            move = (c[i] - c[i - 1]) / c[i - 1] * 100.0
            moves.append(move)
            if abs(move) >= limit_pct:
                report.suspicious_dates.append(
                    f"{ts[i] if i < len(ts) else i} ({move:+.1f}%)")
    if moves:
        report.max_move_pct = max(moves, key=abs)

    if report.suspicious_dates:
        report.add_error(
            f"{len(report.suspicious_dates)} حركة تتجاوز {limit_pct:.0f}%: "
            f"{', '.join(report.suspicious_dates[:3])}"
            f"{'...' if len(report.suspicious_dates) > 3 else ''}. "
            "غالباً تجزئة غير معدّلة أو بيانات فاسدة.")
    elif report.max_move_pct and abs(report.max_move_pct) > MAIN_MARKET_LIMIT_PCT:
        report.warnings.append(
            f"أكبر حركة {report.max_move_pct:+.1f}% تتجاوز حد السوق الرئيسية "
            f"±{MAIN_MARKET_LIMIT_PCT:.0f}%")

    # 4) أحجام صفرية
    if v:
        zero_volume = sum(1 for x in v if not x)
        if zero_volume > len(v) * 0.2:
            report.warnings.append(
                f"{zero_volume} شمعة بحجم صفري ({zero_volume / len(v):.0%}) - "
                "سيولة ضعيفة أو بيانات ناقصة")

    return report


def check_all(series: Dict[str, Dict[str, Sequence]],
              limit_pct: float = SUSPICIOUS_MOVE_PCT
              ) -> Dict[str, QualityReport]:
    return {sym: check_bars(sym, bars, limit_pct) for sym, bars in series.items()}
