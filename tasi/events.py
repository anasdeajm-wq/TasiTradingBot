"""
tasi/events.py
==============
دراسة الأحداث - measuring whether a corporate event moves price abnormally.

هذا منهج مختلف جوهرياً عن الأنماط الفنية. بدل السؤال «هل يتكرر شكل في
الرسم؟» نسأل «هل يتحرك السهم حركة غير عادية حول حدث معلوم التاريخ؟».
السؤال الثاني أقل ازدحاماً لأن تنفيذه يحتاج بيانات أحداث لا رسوماً.

المنهج المطبَّق هو دراسة الأحداث المعيارية:

    العائد غير العادي = عائد السهم − عائد المؤشر

طرح المؤشر ضروري: سهم صعد ٢٪ في يوم صعد فيه السوق ٢٪ لم يفعل شيئاً
يستحق الرصد. بدون هذا الطرح تُنسب حركة السوق كلها إلى الحدث.

ملاحظة مهمة عن التوزيعات: الأسعار المخزّنة معدّلة بعامل adjclose، أي أن
الهبوط الميكانيكي يوم الاستحقاق مطروح أصلاً. ما يقيسه هذا الملف هو ما
يتبقى بعد ذلك الهبوط، وهو وحده ما يمكن أن يُسمّى أفضلية.

الدلالة الإحصائية تُقاس بإحصاءة t تقريبية. مع عيّنات صغيرة يكون متوسط
موجب بلا دلالة مجرد ضجيج، وعرضه كاكتشاف خداع.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DIVIDEND = "DIVIDEND"
SPLIT = "SPLIT"

# نافذة الدراسة حول الحدث: أيام قبل وبعد
DEFAULT_BEFORE = 10
DEFAULT_AFTER = 10

# الحد الأدنى للأحداث قبل السماح بأي استنتاج
MIN_EVENTS = 30


@dataclass
class Event:
    symbol: str
    kind: str
    ex_date: str
    amount: Optional[float] = None
    details: str = ""


@dataclass
class WindowStats:
    """إحصاءات يوم واحد داخل نافذة الحدث."""

    offset: int                       # -10 .. +10 نسبة إلى يوم الحدث
    count: int
    mean_abnormal: float              # متوسط العائد غير العادي بالنسبة المئوية
    std: float
    t_stat: float
    positive_share: float             # نسبة الأحداث الموجبة

    @property
    def significant(self) -> bool:
        """دلالة عند مستوى ٥٪ تقريباً."""
        return abs(self.t_stat) >= 1.96 and self.count >= MIN_EVENTS


@dataclass
class EventStudy:
    kind: str
    events_studied: int = 0
    symbols: int = 0
    windows: List[WindowStats] = field(default_factory=list)
    cumulative: List[Tuple[int, float]] = field(default_factory=list)

    def significant_days(self) -> List[WindowStats]:
        return [w for w in self.windows if w.significant]

    def verdict_ar(self) -> str:
        if self.events_studied < MIN_EVENTS:
            return (f"عدد الأحداث {self.events_studied} أقل من {MIN_EVENTS}، "
                    "ولا يكفي لأي استنتاج.")
        found = self.significant_days()
        if not found:
            return (f"لا يوم ذو دلالة إحصائية في نافذة الحدث "
                    f"({self.events_studied} حدثاً). لا أفضلية هنا.")
        best = max(found, key=lambda w: abs(w.t_stat))
        return (f"{len(found)} يوماً ذا دلالة من {len(self.windows)}. "
                f"الأقوى عند اليوم {best.offset:+d}: "
                f"{best.mean_abnormal:+.3f}% وسطياً، t={best.t_stat:.2f}، "
                f"على {best.count} حدثاً.")

    def render_ar(self) -> str:
        lines = [
            "═" * 70,
            f"دراسة أحداث: {self.kind}",
            f"  {self.events_studied} حدثاً على {self.symbols} سهماً",
            "═" * 70,
            "",
            f"{'اليوم':>6}{'عدد':>7}{'عائد غير عادي':>16}{'t':>9}"
            f"{'موجب':>8}  دلالة",
            "─" * 70,
        ]
        for window in self.windows:
            mark = "★" if window.significant else ""
            lines.append(
                f"{window.offset:>+6d}{window.count:>7}"
                f"{window.mean_abnormal:>+15.3f}%{window.t_stat:>9.2f}"
                f"{window.positive_share:>7.0%}  {mark}")
        lines += ["", "الحكم", "─" * 70, f"  {self.verdict_ar()}", "═" * 70]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
def store_events(conn: sqlite3.Connection, events: Iterable[Event]) -> int:
    rows = [(e.symbol, e.kind, e.ex_date, e.amount, "", e.details)
            for e in events]
    conn.executemany(
        "INSERT OR IGNORE INTO corporate_actions"
        " (symbol, action, ex_date, amount, ratio, details)"
        " VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def load_events(conn: sqlite3.Connection, kind: str = DIVIDEND,
                symbols: Optional[Sequence[str]] = None) -> List[Event]:
    sql = "SELECT symbol, action, ex_date, amount FROM corporate_actions WHERE action = ?"
    params: List[object] = [kind]
    if symbols:
        placeholders = ",".join("?" * len(symbols))
        sql += f" AND symbol IN ({placeholders})"
        params += list(symbols)
    sql += " ORDER BY ex_date"
    return [Event(symbol=r["symbol"], kind=r["action"], ex_date=r["ex_date"],
                  amount=r["amount"]) for r in conn.execute(sql, params)]


def fetch_events_from_yahoo(symbols: Sequence[str],
                            provider=None) -> List[Event]:
    """اجلب التوزيعات والتجزئات من ياهو."""
    import requests
    from .providers.yahoo import HEADERS, to_yahoo, from_yahoo

    session = requests.Session()
    session.headers.update(HEADERS)
    out: List[Event] = []

    for symbol in symbols:
        try:
            response = session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{to_yahoo(symbol)}",
                params={"range": "10y", "interval": "1d", "events": "div,split"},
                timeout=30)
            if response.status_code != 200:
                continue
            result = (response.json().get("chart") or {}).get("result")
            if not result:
                continue
            events = result[0].get("events", {}) or {}
        except Exception:                                  # noqa: BLE001
            continue

        for stamp, payload in (events.get("dividends") or {}).items():
            out.append(Event(
                symbol=from_yahoo(symbol), kind=DIVIDEND,
                ex_date=datetime.fromtimestamp(
                    int(stamp), tz=timezone.utc).date().isoformat(),
                amount=payload.get("amount")))
        for stamp, payload in (events.get("splits") or {}).items():
            out.append(Event(
                symbol=from_yahoo(symbol), kind=SPLIT,
                ex_date=datetime.fromtimestamp(
                    int(stamp), tz=timezone.utc).date().isoformat(),
                details=payload.get("splitRatio", "")))
    return out


# ---------------------------------------------------------------------------
def _returns(closes: Sequence[float]) -> List[Optional[float]]:
    out: List[Optional[float]] = [None]
    for i in range(1, len(closes)):
        previous = closes[i - 1]
        out.append((closes[i] - previous) / previous * 100 if previous else None)
    return out


def study(
    conn: sqlite3.Connection,
    events: Sequence[Event],
    benchmark: str = "TASI",
    before: int = DEFAULT_BEFORE,
    after: int = DEFAULT_AFTER,
    min_events: int = MIN_EVENTS,
) -> EventStudy:
    """قِس العائد غير العادي حول الأحداث.

    ``before`` و ``after`` بعدد جلسات التداول لا الأيام التقويمية، فتُتجنّب
    العطل الرسمية تلقائياً.
    """
    from .quality import check_bars
    from .sweep import load_series

    bench = load_series(conn, benchmark)
    if not bench:
        raise ValueError(f"لا توجد بيانات للمؤشر {benchmark}")
    bench_index = {ts: i for i, ts in enumerate(bench["ts"])}
    bench_returns = _returns(bench["close"])

    cache: Dict[str, Optional[dict]] = {}

    def series_for(symbol: str) -> Optional[dict]:
        if symbol not in cache:
            bars = load_series(conn, symbol)
            if bars and check_bars(symbol, bars).ok:
                cache[symbol] = {
                    "index": {ts: i for i, ts in enumerate(bars["ts"])},
                    "returns": _returns(bars["close"]),
                    "n": len(bars["close"]),
                }
            else:
                cache[symbol] = None
        return cache[symbol]

    # abnormal[offset] = قائمة العوائد غير العادية عند ذلك الإزاحة
    abnormal: Dict[int, List[float]] = {o: [] for o in range(-before, after + 1)}
    used_events = 0
    used_symbols = set()

    for event in events:
        blob = series_for(event.symbol)
        if not blob:
            continue
        anchor = blob["index"].get(event.ex_date)
        bench_anchor = bench_index.get(event.ex_date)
        if anchor is None or bench_anchor is None:
            continue
        if anchor - before < 1 or anchor + after >= blob["n"]:
            continue
        if bench_anchor - before < 1 or bench_anchor + after >= len(bench["close"]):
            continue

        contributed = False
        for offset in range(-before, after + 1):
            stock = blob["returns"][anchor + offset]
            index = bench_returns[bench_anchor + offset]
            if stock is None or index is None:
                continue
            abnormal[offset].append(stock - index)
            contributed = True
        if contributed:
            used_events += 1
            used_symbols.add(event.symbol)

    result = EventStudy(kind=events[0].kind if events else "",
                        events_studied=used_events,
                        symbols=len(used_symbols))

    running = 0.0
    for offset in range(-before, after + 1):
        values = abnormal[offset]
        if not values:
            continue
        n = len(values)
        mean = sum(values) / n
        variance = (sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
        std = math.sqrt(variance)
        t_stat = (mean / (std / math.sqrt(n))) if std and n > 1 else 0.0
        result.windows.append(WindowStats(
            offset=offset, count=n, mean_abnormal=mean, std=std,
            t_stat=t_stat,
            positive_share=sum(1 for v in values if v > 0) / n))
        running += mean
        result.cumulative.append((offset, running))

    return result
