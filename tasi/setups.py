"""
tasi/setups.py
==============
أنماط الدخول - the candidate entry patterns the system evaluates.

مبدأ التصميم: كل نمط عبارة عن قاعدة **ذات معاملات** لا أرقام مثبّتة في
الشيفرة. المعاملات الحالية قيم ابتدائية معقولة لم تُقس بعد، وُضعت لتشغيل
الاختبار التاريخي فقط. الأرقام النهائية تأتي من القياس في backtest.py،
لا من التخمين.

لذلك كل نمط يحمل حقل ``calibrated`` يبدأ False. النظام يعرف أن النمط غير
معاير، والتقارير تعرضه صراحة بدل أن توهمك بأن الأرقام مدروسة.

الأنماط:
    MomentumBreakout   اختراق بحجم      : كسر قمة N شمعة مع تضخم حجم
    PullbackToMA       ارتداد لمتوسط    : اتجاه صاعد ثم تصحيح لمتوسط
    VolumeSpike        ضخ حجم           : حجم استثنائي مع حركة سعرية
    MeanReversion      ارتداد تشبع بيع  : تشبع بيع في سوق عرضي
    RelativeLeader     قائد نسبي        : يتفوق على المؤشر بوضوح
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .features import FeatureSnapshot
from .regime import HIGH_VOL, RANGE, RISK_OFF, TREND_DOWN, TREND_UP


@dataclass
class SetupMatch:
    """تطابق نمط مع لقطة خصائص."""

    setup: str
    symbol: str
    ts: str
    score: float                      # 0..100 قوة التطابق
    price: float
    reason_ar: str
    stop_hint: Optional[float] = None     # وقف مقترح من بنية النمط
    target_hint: Optional[float] = None
    conditions: Dict[str, bool] = field(default_factory=dict)
    calibrated: bool = False          # هل معاملات النمط مقيسة؟

    def to_dict(self) -> Dict[str, object]:
        return {
            "setup": self.setup, "symbol": self.symbol, "ts": self.ts,
            "score": round(self.score, 1), "price": round(self.price, 4),
            "reason_ar": self.reason_ar,
            "stop_hint": round(self.stop_hint, 4) if self.stop_hint else None,
            "target_hint": round(self.target_hint, 4) if self.target_hint else None,
            "conditions": self.conditions, "calibrated": self.calibrated,
        }


class Setup(ABC):
    """نمط دخول. كل نمط يعلن الحالات التي يعمل فيها ومعاملاته."""

    name = "base"
    name_ar = "نمط"
    # الحالات التي يُسمح فيها بهذا النمط. القائمة الفارغة تعني كل الحالات.
    regimes: List[str] = []

    def __init__(self, **params: float) -> None:
        self.params: Dict[str, float] = dict(self.defaults())
        self.params.update(params)
        self.calibrated = False       # يصبح True بعد المعايرة من القياس

    @staticmethod
    def defaults() -> Dict[str, float]:
        return {}

    def allowed_in(self, regime: Optional[str]) -> bool:
        if not self.regimes or regime is None:
            return True
        return regime in self.regimes

    @abstractmethod
    def evaluate(self, snap: FeatureSnapshot) -> Optional[SetupMatch]:
        """أرجع تطابقاً أو None. لا ترفع استثناءً عند نقص الخصائص."""

    def _match(self, snap: FeatureSnapshot, score: float, reason: str,
               conditions: Dict[str, bool], stop: Optional[float] = None,
               target: Optional[float] = None) -> SetupMatch:
        return SetupMatch(
            setup=self.name, symbol=snap.symbol, ts=snap.ts,
            score=max(0.0, min(100.0, score)), price=snap.close or 0.0,
            reason_ar=reason, conditions=conditions,
            stop_hint=stop, target_hint=target, calibrated=self.calibrated,
        )


# ---------------------------------------------------------------------------
class MomentumBreakout(Setup):
    """اختراق قمة بحجم - كسر أعلى سعر في نافذة مع تضخم الحجم."""

    name = "momentum_breakout"
    name_ar = "اختراق بحجم"
    regimes = [TREND_UP, RANGE]

    @staticmethod
    def defaults() -> Dict[str, float]:
        return {
            "min_volume_ratio": 1.5,   # تضخم الحجم المطلوب
            "max_rsi": 75.0,           # تجنّب الشراء في تشبع شديد
            "breakout_buffer": 0.0,    # نسبة تجاوز القمة المطلوبة
            "atr_stop_mult": 1.5,      # وقف الخسارة = ATR × هذا
            "atr_target_mult": 3.0,
        }

    def evaluate(self, snap: FeatureSnapshot) -> Optional[SetupMatch]:
        if not snap.is_complete or snap.high_20 is None:
            return None

        p = self.params
        threshold = snap.high_20 * (1 + p["breakout_buffer"] / 100.0)
        conditions = {
            "breaks_high20": snap.close >= threshold,
            "volume_surge": (snap.volume_ratio or 0) >= p["min_volume_ratio"],
            "rsi_ok": (snap.rsi or 100) < p["max_rsi"],
            "above_ma50": bool(snap.above_ma50),
        }
        if not all(conditions.values()):
            return None

        # الدرجة: قوة الحجم والزخم فوق الحد الأدنى، لا مجرد تحققه.
        vol_bonus = min(30.0, ((snap.volume_ratio - p["min_volume_ratio"]) * 20.0))
        trend_bonus = 15.0 if snap.trend_up else 0.0
        rsi_room = max(0.0, (p["max_rsi"] - (snap.rsi or 0)) / p["max_rsi"] * 15.0)
        score = 40.0 + vol_bonus + trend_bonus + rsi_room

        stop = snap.close - (snap.atr * p["atr_stop_mult"])
        target = snap.close + (snap.atr * p["atr_target_mult"])
        return self._match(
            snap, score,
            f"اختراق قمة {snap.high_20:.2f} بحجم {snap.volume_ratio:.2f}x "
            f"| RSI {snap.rsi:.0f} | ATR {snap.atr_pct:.1f}%",
            conditions, stop, target,
        )


class PullbackToMA(Setup):
    """ارتداد إلى متوسط - تصحيح داخل اتجاه صاعد قائم."""

    name = "pullback_ma"
    name_ar = "ارتداد لمتوسط"
    regimes = [TREND_UP]

    @staticmethod
    def defaults() -> Dict[str, float]:
        return {
            "ma_distance_pct": 2.0,    # قرب السعر من MA20 بالنسبة المئوية
            "min_rsi": 40.0,           # لا نشتري انهياراً
            "max_rsi": 60.0,           # ولا قمة ممتدة
            "atr_stop_mult": 1.5,
            "atr_target_mult": 2.5,
        }

    def evaluate(self, snap: FeatureSnapshot) -> Optional[SetupMatch]:
        if not snap.is_complete or snap.ma20 is None:
            return None

        p = self.params
        distance = abs(snap.close - snap.ma20) / snap.ma20 * 100.0
        conditions = {
            "uptrend": bool(snap.trend_up) or bool(snap.above_ma50),
            "near_ma20": distance <= p["ma_distance_pct"],
            "rsi_reset": p["min_rsi"] <= (snap.rsi or 0) <= p["max_rsi"],
            "above_ma50": bool(snap.above_ma50),
        }
        if not all(conditions.values()):
            return None

        proximity_bonus = (1 - distance / p["ma_distance_pct"]) * 25.0
        trend_bonus = 20.0 if snap.trend_up else 5.0
        score = 40.0 + proximity_bonus + trend_bonus

        stop = min(snap.close - snap.atr * p["atr_stop_mult"],
                   snap.ma50 if snap.ma50 else snap.close)
        target = snap.close + snap.atr * p["atr_target_mult"]
        return self._match(
            snap, score,
            f"تصحيح إلى MA20 ({snap.ma20:.2f}) بفارق {distance:.1f}% "
            f"| RSI {snap.rsi:.0f} في منطقة إعادة الضبط",
            conditions, stop, target,
        )


class VolumeSpike(Setup):
    """ضخ حجم - حجم استثنائي مصحوب بحركة سعرية موجبة."""

    name = "volume_spike"
    name_ar = "ضخ حجم"
    regimes = [TREND_UP, RANGE, HIGH_VOL]

    @staticmethod
    def defaults() -> Dict[str, float]:
        return {
            "min_volume_ratio": 3.0,   # ضخ حقيقي لا تذبذب حجم عادي
            "min_price_move": 1.5,     # حركة السعر المصاحبة بالنسبة المئوية
            "max_rsi": 80.0,
            "atr_stop_mult": 2.0,
            "atr_target_mult": 3.0,
        }

    def evaluate(self, snap: FeatureSnapshot) -> Optional[SetupMatch]:
        if not snap.is_complete or snap.roc5 is None:
            return None

        p = self.params
        conditions = {
            "huge_volume": (snap.volume_ratio or 0) >= p["min_volume_ratio"],
            "price_moving": (snap.roc5 or 0) >= p["min_price_move"],
            "not_exhausted": (snap.rsi or 100) < p["max_rsi"],
        }
        if not all(conditions.values()):
            return None

        vol_bonus = min(35.0, (snap.volume_ratio - p["min_volume_ratio"]) * 10.0)
        move_bonus = min(20.0, snap.roc5 * 3.0)
        score = 35.0 + vol_bonus + move_bonus

        stop = snap.close - snap.atr * p["atr_stop_mult"]
        target = snap.close + snap.atr * p["atr_target_mult"]
        return self._match(
            snap, score,
            f"ضخ حجم {snap.volume_ratio:.1f}x المتوسط مع حركة "
            f"{snap.roc5:+.1f}% في 5 جلسات",
            conditions, stop, target,
        )


class MeanReversion(Setup):
    """ارتداد من تشبع بيع - يعمل في السوق العرضي فقط."""

    name = "mean_reversion"
    name_ar = "ارتداد تشبع بيع"
    regimes = [RANGE]

    @staticmethod
    def defaults() -> Dict[str, float]:
        return {
            "max_rsi": 32.0,
            "bb_touch": 1.0,           # السعر عند أو تحت النطاق السفلي
            "atr_stop_mult": 1.5,
            "atr_target_mult": 2.0,
        }

    def evaluate(self, snap: FeatureSnapshot) -> Optional[SetupMatch]:
        if not snap.is_complete or snap.bb_lower is None:
            return None

        p = self.params
        conditions = {
            "oversold": (snap.rsi or 100) <= p["max_rsi"],
            "at_lower_band": snap.close <= snap.bb_lower * p["bb_touch"],
            "not_collapsing": (snap.roc10 or 0) > -25.0,
        }
        if not all(conditions.values()):
            return None

        depth_bonus = (p["max_rsi"] - snap.rsi) * 1.5
        score = 45.0 + min(30.0, depth_bonus)

        stop = snap.close - snap.atr * p["atr_stop_mult"]
        target = snap.ma20 if snap.ma20 else snap.close + snap.atr * p["atr_target_mult"]
        return self._match(
            snap, score,
            f"تشبع بيع RSI {snap.rsi:.0f} عند النطاق السفلي "
            f"{snap.bb_lower:.2f} في سوق عرضي",
            conditions, stop, target,
        )


class RelativeLeader(Setup):
    """قائد نسبي - يتفوق على المؤشر بفارق واضح مع حجم داعم."""

    name = "relative_leader"
    name_ar = "قائد نسبي"
    regimes = [TREND_UP, RANGE]

    @staticmethod
    def defaults() -> Dict[str, float]:
        return {
            "min_rel_strength": 5.0,   # تفوق على المؤشر بالنقاط المئوية
            "min_volume_ratio": 1.2,
            "max_rsi": 75.0,
            "atr_stop_mult": 2.0,
            "atr_target_mult": 3.5,
        }

    def evaluate(self, snap: FeatureSnapshot) -> Optional[SetupMatch]:
        if not snap.is_complete or snap.rel_strength is None:
            return None

        p = self.params
        conditions = {
            "outperforming": snap.rel_strength >= p["min_rel_strength"],
            "volume_ok": (snap.volume_ratio or 0) >= p["min_volume_ratio"],
            "rsi_ok": (snap.rsi or 100) < p["max_rsi"],
            "above_ma50": bool(snap.above_ma50),
        }
        if not all(conditions.values()):
            return None

        lead_bonus = min(35.0, (snap.rel_strength - p["min_rel_strength"]) * 3.0)
        score = 40.0 + lead_bonus + (15.0 if snap.trend_up else 0.0)

        stop = snap.close - snap.atr * p["atr_stop_mult"]
        target = snap.close + snap.atr * p["atr_target_mult"]
        return self._match(
            snap, score,
            f"يتفوق على المؤشر بـ {snap.rel_strength:+.1f} نقطة خلال 20 جلسة "
            f"| حجم {snap.volume_ratio:.2f}x",
            conditions, stop, target,
        )


# ---------------------------------------------------------------------------
ALL_SETUPS = [MomentumBreakout, PullbackToMA, VolumeSpike, MeanReversion,
              RelativeLeader]

SETUP_NAMES_AR = {s.name: s.name_ar for s in ALL_SETUPS}


def default_registry() -> List[Setup]:
    """أنشئ نسخة من كل نمط بمعاملاته الابتدائية."""
    return [cls() for cls in ALL_SETUPS]


def evaluate_all(snap: FeatureSnapshot, registry: Optional[List[Setup]] = None,
                 respect_regime: bool = True) -> List[SetupMatch]:
    """قيّم كل الأنماط المسموحة في حالة السوق الحالية، مرتبة بالدرجة."""
    registry = registry if registry is not None else default_registry()
    matches: List[SetupMatch] = []
    for setup in registry:
        if respect_regime and not setup.allowed_in(snap.regime):
            continue
        match = setup.evaluate(snap)
        if match:
            matches.append(match)
    return sorted(matches, key=lambda m: m.score, reverse=True)
