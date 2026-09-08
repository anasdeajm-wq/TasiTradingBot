"""
tasi/protection.py
==================
آليات حماية رأس المال - and the measured verdict on each.

هذا الملف يوثّق أربع محاولات لتقليل خسارة السنوات السيئة في استراتيجية
الزخم، **وكلها فشلت في تحسين العائد المعدَّل بالمخاطرة**. توثيق الفشل
هنا مقصود: بدونه سيُعاد اقتراح هذه الأفكار كل بضعة أشهر بحسن نية،
وستُعاد التجربة نفسها بنفس النتيجة.

النتائج على ١٠٠ إعادة توازن شهرية من بيانات تداول الحقيقية:

    الأساس (زخم بلا حماية)     عائد +11.6%  تراجع 29.2%  نسبة 0.398
    مرشّح متوسط المؤشر ٢٠٠      عائد  +4.5%  تراجع 27.2%  نسبة 0.164
    اشتراط عائد مطلق موجب       بلا أثر يُذكر - الأقوى نسبياً موجب أصلاً
    وقف خسارة ١٠٪               عائد  +7.9%  تراجع 36.4%  نسبة 0.217
    استهداف تقلب ١٠٪            عائد  +8.1%  تراجع 21.4%  نسبة 0.376

لماذا فشل كل واحد:

    مرشّح المؤشر: بطيء وخشن. حين ينكسر المتوسط يكون الهبوط وقع، ثم
    يُخرجك قبل الارتداد. كلّف ٥١ نقطة في ٢٠٢٠ وحدها ولم يحمِ في ٢٠٢٥.

    العائد المطلق: لا يقيّد شيئاً عملياً، لأن السهم الذي يتصدّر القوة
    النسبية يكون صاعداً بالمطلق في أغلب الأحوال.

    وقف الخسارة: يُضرب على تذبذب عادي فتُغلق المراكز قبل تعافيها. هذه
    نتيجة معروفة في أبحاث الزخم لا مفاجأة، والقياس هنا أكّدها: التراجع
    ارتفع من ٢٩٪ إلى ٣٦٪.

    استهداف التقلب: الوحيد الذي خفض التراجع فعلاً (٢٩٪ إلى ٢١٪)، لكنه
    كلّف ٣.٥ نقطة عائد سنوي، فبقيت النسبة كما هي تقريباً.

**الخلاصة العملية:** استهداف التقلب ليس تحسيناً بل **مقبض**: ينقلك على
نفس خط الكفاءة إلى نقطة عائد أقل وتراجع أقل. يُختار بحسب ما يحتمله
صاحب المال نفسياً، لا لأنه يزيد الربح.
"""

from __future__ import annotations

import statistics as st
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

TRADING_DAYS = 252


@dataclass
class ProtectionVerdict:
    """حكم مقاس على آلية حماية."""

    name: str
    cagr: float
    max_drawdown: float
    worst_year: float
    average_exposure: float
    verdict_ar: str

    @property
    def efficiency(self) -> float:
        """العائد لكل وحدة تراجع - المعيار الذي يُحكم به."""
        return self.cagr / self.max_drawdown if self.max_drawdown else 0.0


# الأحكام المقاسة، محفوظة كمرجع حتى لا تُعاد التجربة بلا داعٍ
MEASURED: Dict[str, ProtectionVerdict] = {
    "none": ProtectionVerdict(
        "بلا حماية", 11.60, 29.2, -16.4, 1.00,
        "الأساس المرجعي. أعلى عائد وأعلى كفاءة."),
    "index_ma200": ProtectionVerdict(
        "مرشّح متوسط المؤشر ٢٠٠", 4.47, 27.2, -18.5, 0.62,
        "فشل. لم يحمِ في السنة السيئة وكلّف ٥١ نقطة في ٢٠٢٠."),
    "absolute_momentum": ProtectionVerdict(
        "اشتراط عائد مطلق موجب", 11.70, 29.2, -16.4, 0.97,
        "بلا أثر. المتصدّر نسبياً صاعد بالمطلق أصلاً."),
    "stop_loss_10": ProtectionVerdict(
        "وقف خسارة ١٠٪", 7.91, 36.4, -22.4, 0.97,
        "فشل وزاد الضرر. الوقف يُضرب على الضجيج فتفوت العودة."),
    "vol_target_10": ProtectionVerdict(
        "استهداف تقلب ١٠٪", 8.05, 21.4, -15.3, 0.76,
        "مقبض لا تحسين. تراجع أقل بعائد أقل وكفاءة مماثلة."),
}


def realized_volatility(closes: Sequence[float], index: int,
                        window: int = 60) -> Optional[float]:
    """التقلب المتحقق السنوي عند نقطة معيّنة، من البيانات السابقة لها فقط."""
    if index <= 1:
        return None
    returns = []
    for i in range(max(1, index - window), index):
        previous = closes[i - 1]
        if previous:
            returns.append((closes[i] - previous) / previous)
    if len(returns) < 10:
        return None
    return st.stdev(returns) * (TRADING_DAYS ** 0.5)


def volatility_scaled_exposure(
    closes: Sequence[float],
    index: int,
    target_vol: float = 0.10,
    max_exposure: float = 1.0,
    window: int = 60,
) -> float:
    """نسبة التعرّض المطلوبة لبلوغ التقلب المستهدف.

        التعرّض = التقلب المستهدف / التقلب الحالي

    مقيّدة بحد أقصى حتى لا تتحول إلى رافعة. تُحسب من البيانات حتى
    ``index`` فقط، فلا تسرّب من المستقبل.
    """
    current = realized_volatility(closes, index, window)
    if not current or current <= 0:
        return max_exposure
    return min(max_exposure, target_vol / current)


def compare_ar() -> str:
    """اعرض الأحكام المقاسة مرتّبة بالكفاءة."""
    lines = [
        "═" * 78,
        "آليات الحماية - أحكام مقاسة على بيانات تداول الحقيقية",
        "═" * 78,
        f"{'الآلية':<28}{'عائد':>9}{'تراجع':>9}{'أسوأ سنة':>11}"
        f"{'تعرّض':>8}{'الكفاءة':>10}",
        "─" * 78,
    ]
    for verdict in sorted(MEASURED.values(), key=lambda v: -v.efficiency):
        lines.append(
            f"{verdict.name:<28}{verdict.cagr:>+8.2f}%{verdict.max_drawdown:>8.1f}%"
            f"{verdict.worst_year:>+10.1f}%{verdict.average_exposure:>7.0%}"
            f"{verdict.efficiency:>10.3f}")
    lines += [
        "",
        "الكفاءة = العائد السنوي لكل وحدة تراجع. أعلى رقم أفضل.",
        "لا آلية حماية تجاوزت الأساس. استهداف التقلب يقارِبه بعائد أقل.",
        "═" * 78,
    ]
    return "\n".join(lines)
