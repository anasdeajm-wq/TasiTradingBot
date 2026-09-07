"""
tasi/providers/replay.py
========================
مزوّد إعادة التشغيل - replays stored bars as if they were live.

الفائدة: يسمح ببناء واختبار كامل منطق القرار والمخاطر والتعلّم دون أي
اشتراك مدفوع ودون انتظار افتتاح السوق. نفس الشيفرة التي تعمل هنا تعمل
لاحقاً على البث اللحظي، لأن كليهما ينفّذ نفس الواجهة.

مهم: البيانات هنا مصدرها قاعدة البيانات المحلية. إن كانت القاعدة فارغة
فالمزوّد يرفع خطأ صريحاً بدل أن يرجع أسعاراً ملفّقة.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence

from .base import (Bar, MarketDataProvider, ProviderCapabilities, ProviderError,
                   Quote)


class ReplayProvider(MarketDataProvider):
    """يعيد تشغيل الشموع المخزّنة خطوة بخطوة."""

    def __init__(self, conn: sqlite3.Connection, interval: str = "1day",
                 speed: float = 0.0) -> None:
        self.conn = conn
        self.interval = interval
        self.speed = speed          # ثوانٍ بين كل خطوة (0 = بأقصى سرعة)
        self._cursor: Dict[str, int] = {}

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="replay",
            realtime=False,
            delay_minutes=0,
            websocket=True,          # يحاكي البث من البيانات المخزّنة
            intraday_history=True,
            news=False,
            fundamentals=False,
            max_symbols_per_call=1000,
            notes="مزوّد محلي لإعادة التشغيل والاختبار - ليس بيانات حية.",
        )

    def list_symbols(self) -> List[Dict[str, str]]:
        rows = self.conn.execute(
            "SELECT symbol, name_ar, name_en, sector FROM companies "
            "WHERE is_active = 1 ORDER BY symbol"
        ).fetchall()
        return [dict(r) for r in rows]

    def _bars(self, symbol: str, limit: int = 100000) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM bars WHERE symbol = ? AND interval = ? "
            "ORDER BY ts ASC LIMIT ?",
            (symbol, self.interval, limit),
        ).fetchall()

    def get_quotes(self, symbols: Sequence[str]) -> Dict[str, Quote]:
        """السعر عند الخطوة الحالية لكل سهم."""
        out: Dict[str, Quote] = {}
        for symbol in symbols:
            bars = self._bars(symbol)
            if not bars:
                continue
            idx = min(self._cursor.get(symbol, len(bars) - 1), len(bars) - 1)
            bar = bars[idx]
            prev = bars[idx - 1]["close"] if idx > 0 else None
            out[symbol] = Quote(
                symbol=symbol,
                price=float(bar["close"]),
                ts=datetime.fromisoformat(bar["ts"]) if "T" in str(bar["ts"])
                else datetime.strptime(str(bar["ts"])[:10], "%Y-%m-%d"),
                open=bar["open"], high=bar["high"], low=bar["low"],
                prev_close=prev, volume=bar["volume"], turnover=bar["turnover"],
            )
        if not out:
            raise ProviderError(
                "لا توجد شموع مخزّنة لإعادة التشغيل. شغّل التحميل التاريخي أولاً."
            )
        return out

    def get_bars(self, symbol: str, interval: str = "1day",
                 limit: int = 250) -> List[Bar]:
        rows = self.conn.execute(
            "SELECT * FROM bars WHERE symbol = ? AND interval = ? "
            "ORDER BY ts DESC LIMIT ?",
            (symbol, interval, limit),
        ).fetchall()
        return [
            Bar(symbol=symbol, ts=datetime.fromisoformat(str(r["ts"]))
                if "T" in str(r["ts"])
                else datetime.strptime(str(r["ts"])[:10], "%Y-%m-%d"),
                open=r["open"], high=r["high"], low=r["low"],
                close=r["close"], volume=r["volume"] or 0.0,
                turnover=r["turnover"], trades=r["trades"])
            for r in reversed(rows)
        ]

    # -- تحكم في إعادة التشغيل ------------------------------------------
    def seek(self, symbol: str, index: int) -> None:
        self._cursor[symbol] = max(0, index)

    def step(self, symbols: Sequence[str]) -> bool:
        """تقدّم خطوة واحدة. يُرجع False عند نهاية البيانات."""
        advanced = False
        for symbol in symbols:
            length = len(self._bars(symbol))
            current = self._cursor.get(symbol, 0)
            if current < length - 1:
                self._cursor[symbol] = current + 1
                advanced = True
        return advanced

    def stream_quotes(self, symbols: Sequence[str],
                      on_quote: Callable[[Quote], None]) -> None:
        """أعد تشغيل التاريخ كأنه بث حي."""
        import time
        for symbol in symbols:
            self._cursor[symbol] = 0
        while True:
            for quote in self.get_quotes(symbols).values():
                on_quote(quote)
            if not self.step(symbols):
                break
            if self.speed:
                time.sleep(self.speed)
