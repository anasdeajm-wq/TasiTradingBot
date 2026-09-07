"""
tasi/risk.py
============
إدارة المخاطر - position sizing and the limits that stop trading.

الفرق عن النسخة الأولى: الحجم يُحسب من ATR لا من نسبة ثابتة. وقف الخسارة
الثابت بنسبة 2% يكون ضيقاً جداً على سهم يتحرك 4% يومياً وواسعاً جداً على
سهم يتحرك 0.8%. الحساب من ATR يجعل المخاطرة الفعلية متساوية بين أسهم
مختلفة التقلب، وهو الشرط ليكون قياس R في الاختبار التاريخي ذا معنى.

كل النسب هنا **معاملات قابلة للضبط**، وقيمها الحالية ابتدائية. الأرقام
النهائية تأتي من نتائج backtest.py بعد تشغيله على بيانات حقيقية.

إضافة مهمة: الصفقة تُرفض إذا كانت تكاليف الدخول والخروج تلتهم نسبة كبيرة
من الربح المستهدف. مع رأس مال صغير هذه ليست حالة نادرة بل هي القاعدة،
ومن الأمانة أن يقولها النظام صراحة بدل أن يقترح صفقة خاسرة حسابياً.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# المعاملات الابتدائية - starting parameters, to be calibrated from measurement
# ---------------------------------------------------------------------------
DEFAULT_CAPITAL = 100_000.0
RISK_PER_TRADE = 0.01           # 1% من رأس المال لكل صفقة
DAILY_LOSS_LIMIT = 0.03         # 3%
WEEKLY_LOSS_LIMIT = 0.06        # 6%
MAX_POSITION_PCT = 0.20         # أقصى قيمة لصفقة واحدة
MAX_OPEN_POSITIONS = 5
ATR_STOP_MULT = 1.5             # وقف الخسارة = ATR × هذا المعامل

# تكاليف التداول. عدّلها على عمولة وسيطك الفعلية.
COMMISSION_PCT = 0.155 / 100    # لكل جهة
MIN_COMMISSION = 0.0            # حد أدنى للعمولة إن وُجد

# الصفقة مرفوضة إذا التهمت التكاليف أكثر من هذه النسبة من الربح المستهدف.
MAX_COST_RATIO = 0.25


@dataclass
class PositionSize:
    """نتيجة حساب حجم الصفقة."""

    symbol: str
    approved: bool
    shares: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: Optional[float] = None
    risk_amount: float = 0.0        # المبلغ المعرّض للخطر
    position_value: float = 0.0
    round_trip_cost: float = 0.0    # تكلفة الدخول والخروج
    expected_profit: float = 0.0    # عند بلوغ الهدف، بعد التكاليف
    cost_ratio: Optional[float] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol, "approved": self.approved,
            "shares": self.shares,
            "entry_price": round(self.entry_price, 4),
            "stop_price": round(self.stop_price, 4),
            "target_price": round(self.target_price, 4) if self.target_price else None,
            "risk_amount": round(self.risk_amount, 2),
            "position_value": round(self.position_value, 2),
            "round_trip_cost": round(self.round_trip_cost, 2),
            "expected_profit": round(self.expected_profit, 2),
            "cost_ratio": round(self.cost_ratio, 3) if self.cost_ratio else None,
            "reason": self.reason,
        }


@dataclass
class RiskStatus:
    capital: float
    daily_pnl: float
    weekly_pnl: float
    daily_limit_amount: float
    weekly_limit_amount: float
    daily_limit_hit: bool
    weekly_limit_hit: bool
    open_positions: int
    trading_allowed: bool
    blocked_reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "capital": round(self.capital, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "weekly_pnl": round(self.weekly_pnl, 2),
            "daily_limit": round(self.daily_limit_amount, 2),
            "weekly_limit": round(self.weekly_limit_amount, 2),
            "open_positions": self.open_positions,
            "trading_allowed": self.trading_allowed,
            "blocked_reason": self.blocked_reason,
        }


class RiskManager:
    """بوابة المخاطر. كل دخول يمر من هنا."""

    def __init__(
        self,
        capital: float = DEFAULT_CAPITAL,
        risk_per_trade: float = RISK_PER_TRADE,
        daily_loss_limit: float = DAILY_LOSS_LIMIT,
        weekly_loss_limit: float = WEEKLY_LOSS_LIMIT,
        max_position_pct: float = MAX_POSITION_PCT,
        max_open_positions: int = MAX_OPEN_POSITIONS,
        atr_stop_mult: float = ATR_STOP_MULT,
        commission_pct: float = COMMISSION_PCT,
        min_commission: float = MIN_COMMISSION,
        max_cost_ratio: float = MAX_COST_RATIO,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        if capital <= 0:
            raise ValueError("رأس المال يجب أن يكون موجباً")
        self.capital = float(capital)
        self.risk_per_trade = float(risk_per_trade)
        self.daily_loss_limit = float(daily_loss_limit)
        self.weekly_loss_limit = float(weekly_loss_limit)
        self.max_position_pct = float(max_position_pct)
        self.max_open_positions = int(max_open_positions)
        self.atr_stop_mult = float(atr_stop_mult)
        self.commission_pct = float(commission_pct)
        self.min_commission = float(min_commission)
        self.max_cost_ratio = float(max_cost_ratio)
        self.conn = conn

    # ------------------------------------------------------------------
    @property
    def risk_amount(self) -> float:
        return self.capital * self.risk_per_trade

    @property
    def daily_limit_amount(self) -> float:
        return self.capital * self.daily_loss_limit

    @property
    def weekly_limit_amount(self) -> float:
        return self.capital * self.weekly_loss_limit

    @staticmethod
    def week_start(day: Optional[date] = None) -> date:
        """أسبوع تداول يبدأ الأحد."""
        day = day or date.today()
        return day - timedelta(days=(day.weekday() + 1) % 7)

    def commission(self, value: float) -> float:
        return max(value * self.commission_pct, self.min_commission)

    # ------------------------------------------------------------------
    def realized_pnl(self, since: date, until: Optional[date] = None) -> float:
        if self.conn is None:
            return 0.0
        until = until or date.today()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0.0) FROM positions"
            " WHERE status = 'CLOSED' AND DATE(closed_at) BETWEEN ? AND ?",
            (since.isoformat(), until.isoformat()),
        ).fetchone()
        return float(row[0] or 0.0)

    def open_position_count(self) -> int:
        if self.conn is None:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'OPEN'").fetchone()
        return int(row[0] or 0)

    def status(self, day: Optional[date] = None) -> RiskStatus:
        day = day or date.today()
        daily = self.realized_pnl(day, day)
        weekly = self.realized_pnl(self.week_start(day), day)
        daily_hit = daily <= -self.daily_limit_amount
        weekly_hit = weekly <= -self.weekly_limit_amount
        open_count = self.open_position_count()

        blocked = ""
        if weekly_hit:
            blocked = (f"بلغ حد الخسارة الأسبوعي ({self.weekly_loss_limit:.0%} = "
                       f"{self.weekly_limit_amount:,.0f} ريال). إيقاف حتى الأسبوع القادم.")
        elif daily_hit:
            blocked = (f"بلغ حد الخسارة اليومي ({self.daily_loss_limit:.0%} = "
                       f"{self.daily_limit_amount:,.0f} ريال). إيقاف حتى الجلسة القادمة.")
        elif open_count >= self.max_open_positions:
            blocked = f"الصفقات المفتوحة بلغت الحد الأقصى ({self.max_open_positions})."

        return RiskStatus(
            capital=self.capital, daily_pnl=daily, weekly_pnl=weekly,
            daily_limit_amount=self.daily_limit_amount,
            weekly_limit_amount=self.weekly_limit_amount,
            daily_limit_hit=daily_hit, weekly_limit_hit=weekly_hit,
            open_positions=open_count, trading_allowed=not blocked,
            blocked_reason=blocked,
        )

    # ------------------------------------------------------------------
    def size_from_atr(
        self,
        symbol: str,
        entry_price: float,
        atr: float,
        target_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> PositionSize:
        """احسب الحجم بحيث تساوي الخسارة عند الوقف نسبة المخاطرة المحددة.

            وقف الخسارة = سعر الدخول - (ATR × المعامل)
            عدد الأسهم  = (رأس المال × نسبة المخاطرة) / (الدخول - الوقف)
        """
        result = PositionSize(symbol=symbol, approved=False,
                              entry_price=entry_price, target_price=target_price)

        if not entry_price or entry_price <= 0:
            result.reason = "سعر الدخول غير صالح."
            return result
        if stop_price is None:
            if not atr or atr <= 0:
                result.reason = "ATR غير متوفر، ولا يمكن تحديد وقف الخسارة."
                return result
            stop_price = entry_price - atr * self.atr_stop_mult

        result.stop_price = stop_price
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            result.reason = "وقف الخسارة يجب أن يكون أقل من سعر الدخول."
            return result

        shares = math.floor(self.risk_amount / risk_per_share)

        max_value = self.capital * self.max_position_pct
        if shares * entry_price > max_value:
            shares = math.floor(max_value / entry_price)

        if shares < 1:
            result.reason = (
                f"رأس المال لا يكفي لسهم واحد ضمن حدود المخاطرة. "
                f"سعر السهم {entry_price:.2f} ريال، والمخاطرة المسموحة "
                f"{self.risk_amount:.2f} ريال."
            )
            return result

        position_value = shares * entry_price
        entry_cost = self.commission(position_value)
        exit_cost = self.commission(position_value)
        result.round_trip_cost = entry_cost + exit_cost
        result.shares = int(shares)
        result.position_value = position_value
        result.risk_amount = shares * risk_per_share + result.round_trip_cost

        # هل تلتهم التكاليف الربح المستهدف؟
        if target_price:
            gross = (target_price - entry_price) * shares
            result.expected_profit = gross - result.round_trip_cost
            if gross > 0:
                result.cost_ratio = result.round_trip_cost / gross
                if result.cost_ratio > self.max_cost_ratio:
                    result.reason = (
                        f"التكاليف تلتهم {result.cost_ratio:.0%} من الربح "
                        f"المستهدف ({gross:.2f} ريال مقابل تكلفة "
                        f"{result.round_trip_cost:.2f} ريال). الصفقة غير مجدية."
                    )
                    return result
            if result.expected_profit <= 0:
                result.reason = (
                    f"الربح المتوقع بعد التكاليف سالب "
                    f"({result.expected_profit:.2f} ريال)."
                )
                return result

        result.approved = True
        return result

    def approve_entry(
        self,
        symbol: str,
        entry_price: float,
        atr: float,
        target_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        day: Optional[date] = None,
    ) -> PositionSize:
        """البوابة النهائية: الحدود أولاً، ثم الحجم، ثم جدوى التكلفة."""
        status = self.status(day)
        if not status.trading_allowed:
            return PositionSize(symbol=symbol, approved=False,
                                entry_price=entry_price,
                                reason=status.blocked_reason)

        if self.conn is not None:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM positions WHERE symbol = ? AND status = 'OPEN'",
                (symbol,)).fetchone()
            if int(row[0] or 0):
                return PositionSize(symbol=symbol, approved=False,
                                    entry_price=entry_price,
                                    reason="توجد صفقة مفتوحة على هذا السهم.")

        return self.size_from_atr(symbol, entry_price, atr, target_price, stop_price)

    # ------------------------------------------------------------------
    def viability_report(self, sample_price: float = 30.0,
                         sample_atr: float = 0.6) -> Dict[str, object]:
        """هل رأس المال الحالي كافٍ للتداول أصلاً؟

        يجيب برقم لا برأي: أقل رأس مال تصبح عنده الصفقة النموذجية مجدية
        بعد خصم العمولة ذهاباً وإياباً.
        """
        stop_distance = sample_atr * self.atr_stop_mult
        target_distance = sample_atr * 3.0

        sizing = self.size_from_atr(
            "SAMPLE", sample_price, sample_atr,
            target_price=sample_price + target_distance)

        # أقل رأس مال يجعل نسبة التكلفة مقبولة عند هذه المعاملات
        # التكلفة ≈ 2 × عمولة × قيمة الصفقة، والربح = عدد الأسهم × مسافة الهدف
        # النسبة = 2 × عمولة × سعر / مسافة الهدف
        cost_ratio_per_share = (2 * self.commission_pct * sample_price) / target_distance
        needs_larger_target = cost_ratio_per_share > self.max_cost_ratio

        min_shares_needed = 1
        min_capital = (min_shares_needed * stop_distance) / self.risk_per_trade

        return {
            "capital": self.capital,
            "risk_per_trade": self.risk_amount,
            "sample": {"price": sample_price, "atr": sample_atr,
                       "stop_distance": round(stop_distance, 3),
                       "target_distance": round(target_distance, 3)},
            "cost_ratio_at_target": round(cost_ratio_per_share, 3),
            "cost_ratio_limit": self.max_cost_ratio,
            "structurally_unviable": needs_larger_target,
            "min_capital_for_one_share": round(min_capital, 2),
            "sample_result": sizing.to_dict(),
        }
