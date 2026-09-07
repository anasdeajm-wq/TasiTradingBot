"""
strategy.py
===========
استراتيجية التداول - entry and exit rules for the Tadawul bot.

شروط الشراء (يجب تحققها كلها):
    1. السعر أعلى من MA50
    2. الحجم أكبر من 1.5 ضعف المتوسط
    3. RSI أقل من 70

شروط البيع:
    - الهدف الأول: +2%   (بيع نصف الكمية)
    - الهدف الثاني: +4%  (بيع الكمية المتبقية)
    - وقف الخسارة يأتي من risk_manager (افتراضياً -2%)

The strategy module is deliberately pure: it reads an IndicatorSnapshot and a
position, and returns a Signal. It never touches the network or the database,
which makes every rule directly testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from indicators import IndicatorSnapshot

# ---------------------------------------------------------------------------
# معايير الاستراتيجية - strategy parameters
# ---------------------------------------------------------------------------
MIN_VOLUME_RATIO = 1.5     # الحجم أكبر من 1.5 ضعف المتوسط
MAX_RSI = 70.0             # RSI أقل من 70
TARGET_1_PCT = 0.02        # الهدف الأول +2%
TARGET_2_PCT = 0.04        # الهدف الثاني +4%
TARGET_1_EXIT_FRACTION = 0.5   # بيع 50% عند الهدف الأول

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"


@dataclass
class Signal:
    """إشارة تداول - one decision for one symbol at one point in time."""

    symbol: str
    action: str                       # BUY / SELL / HOLD
    price: float
    reason: str = ""
    conditions: Dict[str, bool] = field(default_factory=dict)
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    stop_loss: Optional[float] = None
    exit_fraction: Optional[float] = None   # للبيع الجزئي
    tag: str = ""                     # TARGET_1 / TARGET_2 / STOP_LOSS
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def is_actionable(self) -> bool:
        return self.action in (BUY, SELL)

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "price": round(self.price, 4) if self.price is not None else None,
            "reason": self.reason,
            "conditions": self.conditions,
            "target_1": round(self.target_1, 4) if self.target_1 else None,
            "target_2": round(self.target_2, 4) if self.target_2 else None,
            "stop_loss": round(self.stop_loss, 4) if self.stop_loss else None,
            "exit_fraction": self.exit_fraction,
            "tag": self.tag,
            "timestamp": self.timestamp,
        }


@dataclass
class Position:
    """صفقة مفتوحة - an open long position."""

    symbol: str
    shares: int
    entry_price: float
    stop_price: Optional[float] = None
    target_1_hit: bool = False
    opened_at: Optional[str] = None

    def unrealized_pct(self, price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (price - self.entry_price) / self.entry_price


# ---------------------------------------------------------------------------
# شروط الشراء - buy rules
# ---------------------------------------------------------------------------
def evaluate_buy(
    snapshot: IndicatorSnapshot,
    min_volume_ratio: float = MIN_VOLUME_RATIO,
    max_rsi: float = MAX_RSI,
) -> Signal:
    """قيّم شروط الشراء الثلاثة على السهم.

    All three conditions must hold. A condition whose indicator is not yet
    available (not enough history) counts as False, so the bot waits rather
    than guessing.
    """
    price = snapshot.close

    above_ma50 = (
        price is not None
        and snapshot.ma50 is not None
        and price > snapshot.ma50
    )
    volume_ok = (
        snapshot.volume_ratio is not None
        and snapshot.volume_ratio > min_volume_ratio
    )
    rsi_ok = snapshot.rsi is not None and snapshot.rsi < max_rsi

    conditions = {
        "price_above_ma50": bool(above_ma50),
        "volume_above_1_5x": bool(volume_ok),
        "rsi_below_70": bool(rsi_ok),
    }

    if all(conditions.values()):
        return Signal(
            symbol=snapshot.symbol,
            action=BUY,
            price=price,
            reason=(
                f"السعر {price:.2f} فوق MA50 ({snapshot.ma50:.2f}) | "
                f"الحجم {snapshot.volume_ratio:.2f}x المتوسط | "
                f"RSI {snapshot.rsi:.1f}"
            ),
            conditions=conditions,
            target_1=price * (1 + TARGET_1_PCT),
            target_2=price * (1 + TARGET_2_PCT),
        )

    return Signal(
        symbol=snapshot.symbol,
        action=HOLD,
        price=price if price is not None else 0.0,
        reason=_explain_failed(conditions, snapshot),
        conditions=conditions,
    )


def _explain_failed(conditions: Dict[str, bool], snapshot: IndicatorSnapshot) -> str:
    """اشرح أي شرط لم يتحقق - human-readable reason the entry was skipped."""
    missing: List[str] = []
    if not conditions["price_above_ma50"]:
        if snapshot.ma50 is None:
            missing.append("MA50 غير متوفر (تاريخ غير كافٍ)")
        else:
            missing.append(f"السعر تحت MA50 ({snapshot.ma50:.2f})")
    if not conditions["volume_above_1_5x"]:
        if snapshot.volume_ratio is None:
            missing.append("نسبة الحجم غير متوفرة")
        else:
            missing.append(f"الحجم {snapshot.volume_ratio:.2f}x < {MIN_VOLUME_RATIO}x")
    if not conditions["rsi_below_70"]:
        if snapshot.rsi is None:
            missing.append("RSI غير متوفر")
        else:
            missing.append(f"RSI {snapshot.rsi:.1f} >= {MAX_RSI:.0f}")
    return " | ".join(missing) if missing else "لا توجد إشارة"


# ---------------------------------------------------------------------------
# شروط البيع - exit rules
# ---------------------------------------------------------------------------
def evaluate_sell(position: Position, price: float) -> Signal:
    """قيّم أهداف البيع ووقف الخسارة على صفقة مفتوحة.

    Priority: stop loss first, then target 2, then target 1. Checking the stop
    before the targets means a bar that gapped through the stop exits rather
    than being treated as a winner.
    """
    entry = position.entry_price
    target_1 = entry * (1 + TARGET_1_PCT)
    target_2 = entry * (1 + TARGET_2_PCT)
    change = position.unrealized_pct(price)

    base = dict(
        symbol=position.symbol,
        price=price,
        target_1=target_1,
        target_2=target_2,
        stop_loss=position.stop_price,
    )

    if position.stop_price is not None and price <= position.stop_price:
        return Signal(
            action=SELL,
            reason=(
                f"وقف الخسارة: السعر {price:.2f} وصل وقف الخسارة "
                f"{position.stop_price:.2f} ({change:+.2%})"
            ),
            exit_fraction=1.0,
            tag="STOP_LOSS",
            **base,
        )

    if price >= target_2:
        return Signal(
            action=SELL,
            reason=(
                f"الهدف الثاني +{TARGET_2_PCT:.0%}: السعر {price:.2f} "
                f"وصل {target_2:.2f} ({change:+.2%}) - بيع الكمية المتبقية"
            ),
            exit_fraction=1.0,
            tag="TARGET_2",
            **base,
        )

    if price >= target_1 and not position.target_1_hit:
        return Signal(
            action=SELL,
            reason=(
                f"الهدف الأول +{TARGET_1_PCT:.0%}: السعر {price:.2f} "
                f"وصل {target_1:.2f} ({change:+.2%}) - بيع "
                f"{TARGET_1_EXIT_FRACTION:.0%} من الكمية"
            ),
            exit_fraction=TARGET_1_EXIT_FRACTION,
            tag="TARGET_1",
            **base,
        )

    return Signal(
        action=HOLD,
        reason=(
            f"الاحتفاظ: {change:+.2%} | الهدف الأول {target_1:.2f} | "
            f"الهدف الثاني {target_2:.2f}"
        ),
        **base,
    )


def evaluate(
    snapshot: IndicatorSnapshot,
    position: Optional[Position] = None,
) -> Signal:
    """نقطة الدخول الموحدة: بيع إن كانت هناك صفقة مفتوحة، وإلا فحص الشراء."""
    if position is not None and position.shares > 0:
        return evaluate_sell(position, snapshot.close)
    return evaluate_buy(snapshot)
