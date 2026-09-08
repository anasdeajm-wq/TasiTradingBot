"""
tasi/paper.py
=============
التشغيل الورقي - a live, honest record of what the system said and what happened.

الغرض من هذا الملف أن يقيس الفجوة الوحيدة التي لا يقدر أي اختبار تاريخي
على قياسها: الفرق بين الورق والسوق الفعلي. الاختبار التاريخي يفترض تنفيذاً
بسعر معيّن وانزلاقاً مقدَّراً وبيانات نظيفة. التشغيل الورقي يواجه السوق
الحقيقي بأسعاره وفجواته وأخطاء بياناته.

القاعدة الحاكمة هنا واحدة: **القرار يُسجَّل قبل معرفة النتيجة، ولا
يُعدَّل بعدها أبداً.** سجل يُنقَّح بعد ظهور النتيجة عديم القيمة، لأن كل
قرار فيه سيبدو صائباً.

لذلك:
    - كل قرار يُخزَّن بختم زمني وسعر لحظة اتخاذه
    - لا توجد دالة لتعديل قرار مسجَّل، فقط لإغلاقه بنتيجته
    - المقارنة بين المتوقع والمحقق تُحسب آلياً ولا تُدخَل يدوياً
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    strategy    TEXT NOT NULL,
    params      TEXT,                   -- JSON
    capital     REAL NOT NULL,
    started_at  TEXT NOT NULL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS paper_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES paper_runs(id),
    decided_at  TEXT NOT NULL,          -- وقت اتخاذ القرار
    as_of_date  TEXT NOT NULL,          -- تاريخ البيانات المستخدمة
    symbol      TEXT NOT NULL,
    action      TEXT NOT NULL,          -- BUY / HOLD / SELL
    rank        INTEGER,
    score       REAL,
    price_at_decision REAL NOT NULL,    -- السعر لحظة القرار
    weight      REAL,
    rationale_ar TEXT,
    features    TEXT,                   -- JSON: ما كان النظام يراه
    UNIQUE(run_id, as_of_date, symbol)
);

CREATE TABLE IF NOT EXISTS paper_outcomes (
    decision_id INTEGER PRIMARY KEY REFERENCES paper_decisions(id),
    resolved_at TEXT NOT NULL,
    exit_price  REAL NOT NULL,
    return_pct  REAL NOT NULL,
    benchmark_return_pct REAL,
    excess_pct  REAL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS paper_equity (
    run_id      INTEGER NOT NULL REFERENCES paper_runs(id),
    ts          TEXT NOT NULL,
    equity      REAL NOT NULL,
    benchmark   REAL,
    PRIMARY KEY (run_id, ts)
) WITHOUT ROWID;
"""


@dataclass
class Decision:
    symbol: str
    action: str
    price_at_decision: float
    rank: Optional[int] = None
    score: Optional[float] = None
    weight: Optional[float] = None
    rationale_ar: str = ""
    features: Dict[str, object] = field(default_factory=dict)


@dataclass
class RunSummary:
    name: str
    started_at: str
    capital: float
    decisions: int = 0
    resolved: int = 0
    open_decisions: int = 0
    mean_return: Optional[float] = None
    mean_excess: Optional[float] = None
    win_rate: Optional[float] = None
    equity: Optional[float] = None
    benchmark_equity: Optional[float] = None

    def render_ar(self) -> str:
        lines = [
            "═" * 62,
            f"التشغيل الورقي: {self.name}",
            f"بدأ في {self.started_at} برأس مال {self.capital:,.0f} ريال",
            "═" * 62,
            f"  قرارات مسجّلة   : {self.decisions}",
            f"  مُقيَّمة        : {self.resolved}",
            f"  قائمة          : {self.open_decisions}",
        ]
        if self.resolved:
            lines += [
                f"  متوسط العائد   : {self.mean_return:+.2f}%",
                f"  مقابل المؤشر   : {self.mean_excess:+.2f}%",
                f"  نسبة الربح     : {self.win_rate:.0%}",
            ]
        else:
            lines.append("  لم تُقيَّم أي نتيجة بعد.")
        if self.equity is not None:
            lines.append(f"  رأس المال      : {self.equity:,.2f} ريال")
        if self.benchmark_equity is not None:
            lines.append(f"  المؤشر لو جلست : {self.benchmark_equity:,.2f} ريال")
        lines.append("═" * 62)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def create_run(conn: sqlite3.Connection, name: str, strategy: str,
               capital: float, params: Optional[Dict] = None,
               notes: str = "") -> int:
    """ابدأ تشغيلاً ورقياً جديداً. الاسم فريد فلا يُخلط تشغيلان."""
    ensure_schema(conn)
    cursor = conn.execute(
        "INSERT INTO paper_runs (name, strategy, params, capital, started_at,"
        " notes) VALUES (?, ?, ?, ?, ?, ?)",
        (name, strategy, json.dumps(params or {}, ensure_ascii=False), capital,
         datetime.now().isoformat(timespec="seconds"), notes))
    conn.commit()
    return int(cursor.lastrowid)


def get_run(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM paper_runs WHERE name = ?",
                        (name,)).fetchone()


def record_decisions(conn: sqlite3.Connection, run_id: int, as_of_date: str,
                     decisions: Sequence[Decision]) -> int:
    """سجّل قرارات جولة. يرفض التسجيل المكرر لنفس التاريخ والرمز.

    الرفض مقصود: إعادة التسجيل بعد رؤية النتيجة تُفسد السجل كله.
    """
    now = datetime.now().isoformat(timespec="seconds")
    written = 0
    for decision in decisions:
        try:
            conn.execute(
                """
                INSERT INTO paper_decisions
                    (run_id, decided_at, as_of_date, symbol, action, rank,
                     score, price_at_decision, weight, rationale_ar, features)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, now, as_of_date, decision.symbol, decision.action,
                 decision.rank, decision.score, decision.price_at_decision,
                 decision.weight, decision.rationale_ar,
                 json.dumps(decision.features, ensure_ascii=False)))
            written += 1
        except sqlite3.IntegrityError:
            continue                      # مسجَّل مسبقاً: لا يُكتب فوقه
    conn.commit()
    return written


def open_decisions(conn: sqlite3.Connection, run_id: int) -> List[sqlite3.Row]:
    """القرارات التي لم تُقيَّم بعد."""
    return conn.execute(
        """
        SELECT d.* FROM paper_decisions d
        LEFT JOIN paper_outcomes o ON o.decision_id = d.id
        WHERE d.run_id = ? AND o.decision_id IS NULL AND d.action = 'BUY'
        ORDER BY d.as_of_date, d.rank
        """, (run_id,)).fetchall()


def resolve(conn: sqlite3.Connection, decision_id: int, exit_price: float,
            benchmark_return_pct: Optional[float] = None,
            notes: str = "") -> Dict[str, object]:
    """أغلق قراراً بنتيجته الفعلية. العائد يُحسب ولا يُدخَل."""
    row = conn.execute("SELECT * FROM paper_decisions WHERE id = ?",
                       (decision_id,)).fetchone()
    if not row:
        raise ValueError(f"لا يوجد قرار بالرقم {decision_id}")

    entry = float(row["price_at_decision"])
    if entry <= 0:
        raise ValueError("سعر القرار غير صالح")
    return_pct = (exit_price - entry) / entry * 100
    excess = (return_pct - benchmark_return_pct
              if benchmark_return_pct is not None else None)

    conn.execute(
        """
        INSERT INTO paper_outcomes
            (decision_id, resolved_at, exit_price, return_pct,
             benchmark_return_pct, excess_pct, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(decision_id) DO UPDATE SET
            resolved_at = excluded.resolved_at, exit_price = excluded.exit_price,
            return_pct = excluded.return_pct,
            benchmark_return_pct = excluded.benchmark_return_pct,
            excess_pct = excluded.excess_pct, notes = excluded.notes
        """,
        (decision_id, datetime.now().isoformat(timespec="seconds"), exit_price,
         return_pct, benchmark_return_pct, excess, notes))
    conn.commit()

    return {"decision_id": decision_id, "symbol": row["symbol"],
            "entry": entry, "exit": exit_price,
            "return_pct": round(return_pct, 3),
            "excess_pct": round(excess, 3) if excess is not None else None}


def summarise(conn: sqlite3.Connection, name: str) -> Optional[RunSummary]:
    run = get_run(conn, name)
    if not run:
        return None
    run_id = int(run["id"])

    total = conn.execute(
        "SELECT COUNT(*) c FROM paper_decisions WHERE run_id = ? AND action='BUY'",
        (run_id,)).fetchone()["c"]
    outcomes = conn.execute(
        """
        SELECT o.return_pct, o.excess_pct FROM paper_outcomes o
        JOIN paper_decisions d ON d.id = o.decision_id
        WHERE d.run_id = ?
        """, (run_id,)).fetchall()

    summary = RunSummary(
        name=name, started_at=run["started_at"], capital=float(run["capital"]),
        decisions=total, resolved=len(outcomes),
        open_decisions=total - len(outcomes))

    if outcomes:
        returns = [float(r["return_pct"]) for r in outcomes]
        summary.mean_return = sum(returns) / len(returns)
        summary.win_rate = sum(1 for r in returns if r > 0) / len(returns)
        excesses = [float(r["excess_pct"]) for r in outcomes
                    if r["excess_pct"] is not None]
        if excesses:
            summary.mean_excess = sum(excesses) / len(excesses)

    equity_row = conn.execute(
        "SELECT equity, benchmark FROM paper_equity WHERE run_id = ?"
        " ORDER BY ts DESC LIMIT 1", (run_id,)).fetchone()
    if equity_row:
        summary.equity = float(equity_row["equity"])
        summary.benchmark_equity = (float(equity_row["benchmark"])
                                    if equity_row["benchmark"] is not None else None)
    return summary


def record_equity(conn: sqlite3.Connection, run_id: int, equity: float,
                  benchmark: Optional[float] = None,
                  ts: Optional[str] = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO paper_equity (run_id, ts, equity, benchmark)"
        " VALUES (?, ?, ?, ?)",
        (run_id, ts or date.today().isoformat(), equity, benchmark))
    conn.commit()


def decision_log(conn: sqlite3.Connection, name: str,
                 limit: int = 50) -> List[Dict[str, object]]:
    """سجل القرارات مع نتائجها، الأحدث أولاً."""
    run = get_run(conn, name)
    if not run:
        return []
    rows = conn.execute(
        """
        SELECT d.as_of_date, d.symbol, d.action, d.rank, d.score,
               d.price_at_decision, d.rationale_ar,
               o.exit_price, o.return_pct, o.excess_pct
        FROM paper_decisions d
        LEFT JOIN paper_outcomes o ON o.decision_id = d.id
        WHERE d.run_id = ?
        ORDER BY d.as_of_date DESC, d.rank
        LIMIT ?
        """, (int(run["id"]), limit)).fetchall()
    return [dict(r) for r in rows]
