"""
risk_manager.py
===============
إدارة المخاطر - Risk management for the Tadawul trading bot.

القواعد المطلوبة:
    - المخاطرة لكل صفقة = 1% من رأس المال
    - رأس المال = 100,000 ريال سعودي
    - حد الخسارة اليومي = 3%
    - حد الخسارة الأسبوعي = 6%

The manager is the single gate every new position must pass through. It sizes
positions from the risk budget, and it halts new entries once the daily or
weekly drawdown limit is hit.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# الإعدادات الافتراضية - defaults from the spec
# ---------------------------------------------------------------------------
DEFAULT_CAPITAL = 100_000.0        # رأس المال بالريال السعودي
RISK_PER_TRADE = 0.01              # 1% من رأس المال لكل صفقة
DAILY_LOSS_LIMIT = 0.03            # 3% حد الخسارة اليومي
WEEKLY_LOSS_LIMIT = 0.06           # 6% حد الخسارة الأسبوعي

# The spec fixes the risk budget but not the stop distance. A 2% initial stop
# is used so that "risk 1% of capital" resolves to a concrete share count;
# override it per trade via `size_position(stop_pct=...)` or by passing an
# explicit `stop_price`.
DEFAULT_STOP_PCT = 0.02            # وقف الخسارة الافتراضي 2% تحت سعر الدخول
MAX_POSITION_PCT = 0.20            # لا تتجاوز الصفقة الواحدة 20% من رأس المال
MAX_OPEN_POSITIONS = 5             # أقصى عدد صفقات مفتوحة في نفس الوقت

# Tadawul trades in whole shares; there are no fractional lots.
MIN_SHARES = 1


@dataclass
class PositionSize:
    """نتيجة حساب حجم الصفقة."""

    symbol: str
    approved: bool
    shares: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    risk_amount: float = 0.0        # المبلغ المعرّض للخطر بالريال
    position_value: float = 0.0     # قيمة الصفقة بالريال
    reason: str = ""                # سبب الرفض إن وُجد

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "approved": self.approved,
            "shares": self.shares,
            "entry_price": round(self.entry_price, 4),
            "stop_price": round(self.stop_price, 4),
            "risk_amount": round(self.risk_amount, 2),
            "position_value": round(self.position_value, 2),
            "reason": self.reason,
        }


@dataclass
class RiskStatus:
    """حالة المخاطر الحالية - a point-in-time view of the risk budget."""

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
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "capital": round(self.capital, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "weekly_pnl": round(self.weekly_pnl, 2),
            "daily_limit_amount": round(self.daily_limit_amount, 2),
            "weekly_limit_amount": round(self.weekly_limit_amount, 2),
            "daily_limit_hit": self.daily_limit_hit,
            "weekly_limit_hit": self.weekly_limit_hit,
            "open_positions": self.open_positions,
            "trading_allowed": self.trading_allowed,
            "blocked_reason": self.blocked_reason,
        }


class RiskManager:
    """بوابة المخاطر - every entry passes through `approve_entry`."""

    def __init__(
        self,
        capital: float = DEFAULT_CAPITAL,
        risk_per_trade: float = RISK_PER_TRADE,
        daily_loss_limit: float = DAILY_LOSS_LIMIT,
        weekly_loss_limit: float = WEEKLY_LOSS_LIMIT,
        default_stop_pct: float = DEFAULT_STOP_PCT,
        max_position_pct: float = MAX_POSITION_PCT,
        max_open_positions: int = MAX_OPEN_POSITIONS,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        if capital <= 0:
            raise ValueError("capital must be positive")
        self.capital = float(capital)
        self.risk_per_trade = float(risk_per_trade)
        self.daily_loss_limit = float(daily_loss_limit)
        self.weekly_loss_limit = float(weekly_loss_limit)
        self.default_stop_pct = float(default_stop_pct)
        self.max_position_pct = float(max_position_pct)
        self.max_open_positions = int(max_open_positions)
        self.conn = conn

        # In-memory ledger, used when no SQLite connection is supplied.
        self._realized: List[Dict[str, object]] = []
        self._open: Dict[str, Dict[str, object]] = {}

    # ------------------------------------------------------------------
    # حدود الخسارة - loss limits
    # ------------------------------------------------------------------
    @property
    def risk_amount(self) -> float:
        """المبلغ المسموح خسارته في الصفقة الواحدة (1% من رأس المال)."""
        return self.capital * self.risk_per_trade

    @property
    def daily_limit_amount(self) -> float:
        return self.capital * self.daily_loss_limit

    @property
    def weekly_limit_amount(self) -> float:
        return self.capital * self.weekly_loss_limit

    @staticmethod
    def week_start(day: Optional[date] = None) -> date:
        """بداية الأسبوع في السوق السعودي: الأحد.

        Tadawul's trading week runs Sunday -> Thursday, so the weekly window
        is anchored on Sunday rather than Monday.
        """
        day = day or date.today()
        # Python: Monday=0 ... Sunday=6. Days since the most recent Sunday:
        days_since_sunday = (day.weekday() + 1) % 7
        return day - timedelta(days=days_since_sunday)

    def realized_pnl(self, since: date, until: Optional[date] = None) -> float:
        """الأرباح/الخسائر المحققة خلال فترة - realized PnL over a window."""
        until = until or date.today()
        if self.conn is not None:
            row = self.conn.execute(
                """
                SELECT COALESCE(SUM(pnl), 0.0) FROM trades
                 WHERE status = 'CLOSED'
                   AND DATE(closed_at) >= ?
                   AND DATE(closed_at) <= ?
                """,
                (since.isoformat(), until.isoformat()),
            ).fetchone()
            return float(row[0] or 0.0)

        total = 0.0
        for trade in self._realized:
            closed = trade["closed_at"]
            if since <= closed <= until:
                total += float(trade["pnl"])
        return total

    def daily_pnl(self, day: Optional[date] = None) -> float:
        day = day or date.today()
        return self.realized_pnl(day, day)

    def weekly_pnl(self, day: Optional[date] = None) -> float:
        day = day or date.today()
        return self.realized_pnl(self.week_start(day), day)

    def open_position_count(self) -> int:
        if self.conn is not None:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status = 'OPEN'"
            ).fetchone()
            return int(row[0] or 0)
        return len(self._open)

    def status(self, day: Optional[date] = None) -> RiskStatus:
        """حالة المخاطر الحالية."""
        day = day or date.today()
        daily = self.daily_pnl(day)
        weekly = self.weekly_pnl(day)

        daily_hit = daily <= -self.daily_limit_amount
        weekly_hit = weekly <= -self.weekly_limit_amount
        open_count = self.open_position_count()

        blocked = ""
        if weekly_hit:
            blocked = (
                f"تم بلوغ حد الخسارة الأسبوعي "
                f"({self.weekly_loss_limit:.0%} = {self.weekly_limit_amount:,.0f} ريال). "
                f"إيقاف الدخول حتى بداية الأسبوع القادم."
            )
        elif daily_hit:
            blocked = (
                f"تم بلوغ حد الخسارة اليومي "
                f"({self.daily_loss_limit:.0%} = {self.daily_limit_amount:,.0f} ريال). "
                f"إيقاف الدخول حتى الجلسة القادمة."
            )
        elif open_count >= self.max_open_positions:
            blocked = f"عدد الصفقات المفتوحة بلغ الحد الأقصى ({self.max_open_positions})."

        return RiskStatus(
            capital=self.capital,
            daily_pnl=daily,
            weekly_pnl=weekly,
            daily_limit_amount=self.daily_limit_amount,
            weekly_limit_amount=self.weekly_limit_amount,
            daily_limit_hit=daily_hit,
            weekly_limit_hit=weekly_hit,
            open_positions=open_count,
            trading_allowed=not blocked,
            blocked_reason=blocked,
        )

    def can_trade(self, day: Optional[date] = None) -> bool:
        """هل يُسمح بفتح صفقات جديدة الآن؟"""
        return self.status(day).trading_allowed

    # ------------------------------------------------------------------
    # حجم الصفقة - position sizing
    # ------------------------------------------------------------------
    def size_position(
        self,
        symbol: str,
        entry_price: float,
        stop_price: Optional[float] = None,
        stop_pct: Optional[float] = None,
    ) -> PositionSize:
        """احسب عدد الأسهم بحيث لا تتجاوز الخسارة 1% من رأس المال.

            shares = (capital * 1%) / (entry - stop)

        The result is additionally capped by `max_position_pct` of capital.
        """
        result = PositionSize(symbol=symbol, approved=False, entry_price=entry_price)

        if entry_price is None or entry_price <= 0:
            result.reason = "سعر الدخول غير صالح."
            return result

        if stop_price is None:
            pct = self.default_stop_pct if stop_pct is None else stop_pct
            stop_price = entry_price * (1.0 - pct)

        result.stop_price = stop_price
        stop_distance = entry_price - stop_price
        if stop_distance <= 0:
            result.reason = "وقف الخسارة يجب أن يكون أقل من سعر الدخول."
            return result

        budget = self.risk_amount                       # 1% من رأس المال
        shares = math.floor(budget / stop_distance)

        # سقف قيمة الصفقة - cap the notional exposure of a single name.
        max_value = self.capital * self.max_position_pct
        if shares * entry_price > max_value:
            shares = math.floor(max_value / entry_price)

        if shares < MIN_SHARES:
            result.reason = (
                "حجم الصفقة أقل من سهم واحد بعد تطبيق حدود المخاطرة."
            )
            return result

        result.approved = True
        result.shares = int(shares)
        result.risk_amount = shares * stop_distance
        result.position_value = shares * entry_price
        return result

    def approve_entry(
        self,
        symbol: str,
        entry_price: float,
        stop_price: Optional[float] = None,
        stop_pct: Optional[float] = None,
        day: Optional[date] = None,
    ) -> PositionSize:
        """البوابة النهائية: تحقق من الحدود ثم احسب حجم الصفقة."""
        status = self.status(day)
        if not status.trading_allowed:
            return PositionSize(
                symbol=symbol,
                approved=False,
                entry_price=entry_price,
                reason=status.blocked_reason,
            )

        if self.conn is not None:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM trades WHERE symbol = ? AND status = 'OPEN'",
                (symbol,),
            ).fetchone()
            already_open = int(row[0] or 0)
        else:
            already_open = 1 if symbol in self._open else 0

        if already_open:
            return PositionSize(
                symbol=symbol,
                approved=False,
                entry_price=entry_price,
                reason="توجد صفقة مفتوحة على هذا السهم بالفعل.",
            )

        return self.size_position(symbol, entry_price, stop_price, stop_pct)

    # ------------------------------------------------------------------
    # دفتر الصفقات - in-memory ledger (used when there is no DB)
    # ------------------------------------------------------------------
    def register_open(
        self,
        symbol: str,
        shares: int,
        entry_price: float,
        stop_price: float,
        opened_at: Optional[datetime] = None,
    ) -> None:
        self._open[symbol] = {
            "shares": shares,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "opened_at": opened_at or datetime.now(),
        }

    def register_close(
        self,
        symbol: str,
        exit_price: float,
        shares: Optional[int] = None,
        closed_at: Optional[date] = None,
    ) -> float:
        """أغلق صفقة (كلياً أو جزئياً) وسجّل الربح/الخسارة. يُرجع الـ PnL."""
        position = self._open.get(symbol)
        if not position:
            return 0.0

        qty = int(shares or position["shares"])
        qty = min(qty, int(position["shares"]))
        pnl = (exit_price - float(position["entry_price"])) * qty

        self._realized.append(
            {
                "symbol": symbol,
                "pnl": pnl,
                "closed_at": closed_at or date.today(),
            }
        )

        position["shares"] = int(position["shares"]) - qty
        if position["shares"] <= 0:
            self._open.pop(symbol, None)
        return pnl
