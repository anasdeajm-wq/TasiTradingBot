"""
tasi/journal.py
===============
ملخص ما قبل الافتتاح - the pre-open briefing.

ما يجيب عنه هذا الملف، وهو بالضبط ما طُلب:
    - ماذا فعل النظام أمس، حتى آخر ثانية سُجِّلت
    - كم ربحنا أو خسرنا، محققاً وغير محقق
    - أين وصلنا منذ البداية
    - ما توقعات اليوم بالحرف والرقم
    - أي توقعات سابقة أخطأت، ولماذا

قاعدة صارمة هنا: التقرير لا يخترع رقماً. أي بند بلا بيانات يُكتب صراحة
أنه غير متوفر. تقرير جميل بأرقام مؤلّفة أسوأ من تقرير ناقص صادق، لأن
الأول يُبنى عليه قرار.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import predictions as P
from . import regime as rg
from . import setups as S


def _fmt_sar(value: Optional[float]) -> str:
    if value is None:
        return "غير متوفر"
    return f"{value:,.2f} ريال"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "غير متوفر"
    return f"{value:+.2f}%"


# ---------------------------------------------------------------------------
def session_activity(conn: sqlite3.Connection, day: str) -> Dict[str, object]:
    """ماذا فعل النظام في جلسة معيّنة."""
    signals = conn.execute(
        "SELECT setup, action, symbol, price, score, reason_ar, created_at"
        " FROM signals WHERE DATE(created_at) = ? ORDER BY created_at",
        (day,)).fetchall()

    orders = conn.execute(
        "SELECT symbol, side, quantity, limit_price, status, created_at"
        " FROM orders WHERE DATE(created_at) = ? ORDER BY created_at",
        (day,)).fetchall()

    closed = conn.execute(
        "SELECT symbol, quantity, avg_entry, realized_pnl, opened_at, closed_at"
        " FROM positions WHERE status = 'CLOSED' AND DATE(closed_at) = ?",
        (day,)).fetchall()

    realized = sum(float(r["realized_pnl"] or 0) for r in closed)

    return {
        "date": day,
        "signals": [dict(r) for r in signals],
        "orders": [dict(r) for r in orders],
        "closed_positions": [dict(r) for r in closed],
        "realized_pnl": realized,
        "signals_count": len(signals),
        "orders_count": len(orders),
    }


def open_positions_view(conn: sqlite3.Connection,
                        last_prices: Optional[Dict[str, float]] = None
                        ) -> List[Dict[str, object]]:
    """المراكز المفتوحة مع الربح غير المحقق إن توفر السعر."""
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at"
    ).fetchall()

    out = []
    for r in rows:
        price = (last_prices or {}).get(r["symbol"])
        unrealized = None
        change_pct = None
        if price is not None:
            unrealized = (price - float(r["avg_entry"])) * int(r["quantity"])
            change_pct = (price - float(r["avg_entry"])) / float(r["avg_entry"]) * 100
        out.append({
            "symbol": r["symbol"], "quantity": r["quantity"],
            "avg_entry": r["avg_entry"], "stop_price": r["stop_price"],
            "last_price": price, "unrealized_pnl": unrealized,
            "change_pct": change_pct, "opened_at": r["opened_at"],
        })
    return out


def equity_summary(conn: sqlite3.Connection) -> Dict[str, object]:
    """أين وصلنا منذ البداية."""
    rows = conn.execute(
        "SELECT ts, total_equity FROM equity_curve ORDER BY ts").fetchall()
    if not rows:
        return {"available": False,
                "note": "منحنى رأس المال فارغ - لم تُسجَّل جلسات بعد."}

    first = float(rows[0]["total_equity"])
    last = float(rows[-1]["total_equity"])
    peak = max(float(r["total_equity"]) for r in rows)
    drawdown = (last - peak) / peak * 100 if peak else 0.0

    return {
        "available": True,
        "start_equity": first, "current_equity": last, "peak_equity": peak,
        "total_return_pct": ((last - first) / first * 100) if first else None,
        "drawdown_from_peak_pct": drawdown,
        "sessions": len(rows),
    }


def resolve_due_predictions(
    conn: sqlite3.Connection,
    actuals: Dict[int, float],
    regimes: Optional[Dict[int, str]] = None,
) -> List[Dict[str, object]]:
    """قيّم التوقعات التي حان أجلها بالنتائج الفعلية الممرَّرة.

    ``actuals`` يربط رقم التوقع بالتغير الفعلي. التوقع الذي لا نتيجة له
    يبقى بلا تقييم بدل أن يُقيَّم بصفر مفترض.
    """
    results = []
    for row in P.due_predictions(conn):
        pid = int(row["id"])
        if pid not in actuals:
            continue
        results.append(P.resolve_prediction(
            conn, pid, actuals[pid],
            regime_at_resolution=(regimes or {}).get(pid)))
    return results


# ---------------------------------------------------------------------------
def build_briefing(
    conn: sqlite3.Connection,
    for_date: Optional[str] = None,
    last_prices: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    """ابنِ تقرير ما قبل الافتتاح كاملاً."""
    today = for_date or date.today().isoformat()
    previous = (date.fromisoformat(today) - timedelta(days=1)).isoformat()

    activity = session_activity(conn, previous)
    positions = open_positions_view(conn, last_prices)
    equity = equity_summary(conn)
    accuracy = P.accuracy_report(conn)
    learned = P.lessons(conn)

    regime_row = conn.execute(
        "SELECT * FROM market_regimes ORDER BY date DESC LIMIT 1").fetchone()
    current_regime = dict(regime_row) if regime_row else None

    upcoming = conn.execute(
        """
        SELECT p.* FROM predictions p
        LEFT JOIN prediction_results r ON r.prediction_id = p.id
        WHERE r.prediction_id IS NULL
        ORDER BY p.made_at DESC LIMIT 10
        """).fetchall()

    unrealized = sum(p["unrealized_pnl"] or 0 for p in positions
                     if p["unrealized_pnl"] is not None)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "for_date": today,
        "previous_session": activity,
        "open_positions": positions,
        "unrealized_pnl": unrealized if positions else None,
        "equity": equity,
        "market_regime": current_regime,
        "prediction_accuracy": accuracy,
        "lessons": learned,
        "open_predictions": [dict(r) for r in upcoming],
    }


def render_briefing_ar(briefing: Dict[str, object]) -> str:
    """اعرض التقرير نصاً عربياً جاهزاً للإرسال."""
    lines: List[str] = []
    add = lines.append

    add("=" * 62)
    add(f"ملخص ما قبل الافتتاح - {briefing['for_date']}")
    add(f"أُنشئ في: {briefing['generated_at']}")
    add("=" * 62)

    # حالة السوق
    regime = briefing.get("market_regime")
    add("")
    add("حالة السوق")
    add("-" * 62)
    if regime:
        add(f"  التصنيف : {rg.REGIME_AR.get(regime['regime'], regime['regime'])}")
        add(f"  التاريخ : {regime['date']}")
        if regime.get("tasi_close") is not None:
            add(f"  إغلاق المؤشر : {regime['tasi_close']:,.2f}"
                f" ({_fmt_pct(regime.get('tasi_change'))})")
        if regime.get("breadth") is not None:
            add(f"  اتساع السوق : {regime['breadth']:.0%}")
        if regime.get("notes"):
            add(f"  السبب : {regime['notes']}")
    else:
        add("  غير متوفر - لم تُصنَّف أي جلسة بعد.")

    # الجلسة السابقة
    activity = briefing["previous_session"]
    add("")
    add(f"ماذا فعلنا أمس ({activity['date']})")
    add("-" * 62)
    add(f"  إشارات : {activity['signals_count']}")
    add(f"  أوامر  : {activity['orders_count']}")
    add(f"  الربح المحقق : {_fmt_sar(activity['realized_pnl'])}")
    if activity["signals"]:
        add("  تفاصيل الإشارات:")
        for s in activity["signals"][:10]:
            setup_ar = S.SETUP_NAMES_AR.get(s["setup"], s["setup"])
            add(f"    {s['created_at'][11:19]} | {s['symbol']} | {s['action']}"
                f" | {setup_ar} | {s['price']:.2f}")
    if activity["closed_positions"]:
        add("  الصفقات المغلقة:")
        for c in activity["closed_positions"]:
            add(f"    {c['symbol']} | {c['quantity']} سهم"
                f" | ربح/خسارة {_fmt_sar(c['realized_pnl'])}")
    if not activity["signals"] and not activity["orders"]:
        add("  لا نشاط مسجّل في هذه الجلسة.")

    # المراكز المفتوحة
    add("")
    add("المراكز المفتوحة")
    add("-" * 62)
    if briefing["open_positions"]:
        for p in briefing["open_positions"]:
            line = (f"  {p['symbol']} | {p['quantity']} سهم"
                    f" | دخول {p['avg_entry']:.2f}")
            if p["last_price"] is not None:
                line += (f" | آخر {p['last_price']:.2f}"
                         f" ({_fmt_pct(p['change_pct'])})"
                         f" | غير محقق {_fmt_sar(p['unrealized_pnl'])}")
            else:
                line += " | السعر الحالي غير متوفر"
            add(line)
        add(f"  إجمالي غير المحقق : {_fmt_sar(briefing['unrealized_pnl'])}")
    else:
        add("  لا توجد مراكز مفتوحة.")

    # رأس المال
    equity = briefing["equity"]
    add("")
    add("أين وصلنا")
    add("-" * 62)
    if equity.get("available"):
        add(f"  البداية : {_fmt_sar(equity['start_equity'])}")
        add(f"  الحالي  : {_fmt_sar(equity['current_equity'])}")
        add(f"  القمة   : {_fmt_sar(equity['peak_equity'])}")
        add(f"  العائد الكلي : {_fmt_pct(equity['total_return_pct'])}")
        add(f"  التراجع عن القمة : {_fmt_pct(equity['drawdown_from_peak_pct'])}")
        add(f"  عدد الجلسات : {equity['sessions']}")
    else:
        add(f"  {equity.get('note')}")

    # دقة التوقعات
    accuracy = briefing["prediction_accuracy"]
    add("")
    add("دقة التوقعات السابقة")
    add("-" * 62)
    if accuracy.get("resolved"):
        add(f"  توقعات مقيَّمة : {accuracy['resolved']}")
        add(f"  نسبة الإصابة  : {accuracy['hit_rate']:.0%}")
        add(f"  معايرة الثقة (Brier) : {accuracy['brier_score']}")
        if accuracy.get("failure_causes"):
            add("  أسباب الخطأ:")
            for cause, count in accuracy["failure_causes"].items():
                add(f"    {cause}: {count}")
    else:
        add(f"  {accuracy.get('note', 'لا توجد توقعات مقيَّمة بعد.')}")

    # الدروس
    add("")
    add("ما تعلمناه")
    add("-" * 62)
    for lesson in briefing["lessons"]:
        add(f"  - {lesson}")

    # التوقعات المفتوحة
    add("")
    add("توقعات قائمة لم تُقيَّم بعد")
    add("-" * 62)
    if briefing["open_predictions"]:
        for p in briefing["open_predictions"]:
            add(f"  {p['target']} | {p['horizon']} | {p['direction']}"
                f" | {_fmt_pct(p['predicted_pct'])}"
                f" | ثقة {p['confidence']:.0%} | يُقيَّم في {p['resolve_at']}")
    else:
        add("  لا توجد توقعات قائمة.")

    add("")
    add("=" * 62)
    return "\n".join(lines)


def save_journal(conn: sqlite3.Connection, day: str, briefing: Dict[str, object],
                 summary_text: str) -> None:
    activity = briefing["previous_session"]
    equity = briefing["equity"]
    conn.execute(
        """
        INSERT INTO session_journal
            (date, signals_count, orders_count, realized_pnl, unrealized_pnl,
             equity_close, summary_ar, lessons_ar)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            signals_count = excluded.signals_count,
            orders_count = excluded.orders_count,
            realized_pnl = excluded.realized_pnl,
            unrealized_pnl = excluded.unrealized_pnl,
            equity_close = excluded.equity_close,
            summary_ar = excluded.summary_ar,
            lessons_ar = excluded.lessons_ar
        """,
        (day, activity["signals_count"], activity["orders_count"],
         activity["realized_pnl"], briefing.get("unrealized_pnl"),
         equity.get("current_equity"), summary_text,
         "\n".join(briefing["lessons"])),
    )
    conn.commit()
