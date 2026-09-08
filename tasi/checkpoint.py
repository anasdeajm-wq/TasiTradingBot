"""
tasi/checkpoint.py
===================
تثبيت الحالة الحرجة - snapshotting what cannot be regenerated.

المشكلة التي يحلّها هذا الملف: قواعد البيانات (data/*.db) مستبعدة من git
عمداً لأنها كبيرة (مئات الميغابايت من الشموع القابلة لإعادة التحميل مجاناً
في أي وقت). لكن بيئة التشغيل حاوية مؤقتة تُستعاد بعد فترة خمول، وما لم
يُرفع إلى git يُفقد نهائياً عند ذلك.

بعض ما في القاعدة **لا يمكن إعادة توليده أبداً**:
    - سجل التشغيل الورقي (paper_decisions/paper_outcomes): قرارات اتُّخذت
      بأسعار لحظة معينة. لو ضاعت، ضاع الدليل الوحيد على الفجوة بين الورق
      والسوق الحقيقي - وهو كل الغرض من التشغيل الورقي.
    - التوقعات ونتائجها (predictions/prediction_results): نفس المنطق.
    - التصنيف الشرعي المستورد (shariah_status): قائمة مصدر خارجي في لحظة
      زمنية معينة. لو ضاعت نعيد الاستيراد، لكن نفقد التأريخ الدقيق لما
      رآه النظام حين اتُّخذ كل قرار.
    - أوزان الأنماط المقيسة (setup_performance): نتيجة اختبار تاريخي طويل.

هذا الملف يُصدِّر هذه الجداول فقط - لا الشموع ولا القوائم المالية - إلى
JSON يُحفَظ في `snapshots/` ويُرفع إلى git. حجمها كيلوبايتات لا ميغابايتات.

الاستعادة تُعيد البيانات إلى قاعدة فارغة أو موجودة. لا تُستبدل الشموع أو
القوائم المالية لأنها ليست جزءاً من هذا التثبيت - تُعاد بأمر `fetch` العادي.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List

# الجداول الحرجة فقط - سجل حي لا يُعاد توليده، لا بيانات سوق قابلة لإعادة الجلب
CRITICAL_TABLES = [
    "companies",
    "shariah_status",
    "setup_performance",
    "market_regimes",
    "predictions",
    "prediction_results",
    "paper_runs",
    "paper_decisions",
    "paper_outcomes",
    "paper_equity",
]


def export_all(conn: sqlite3.Connection, path: str) -> Dict[str, int]:
    """صدّر الجداول الحرجة إلى ملف JSON واحد. يُرجع عدد الصفوف لكل جدول."""
    snapshot: Dict[str, List[dict]] = {}
    counts: Dict[str, int] = {}

    for table in CRITICAL_TABLES:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if not exists:
            continue
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        snapshot[table] = [dict(r) for r in rows]
        counts[table] = len(rows)

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1,
                              sort_keys=True), encoding="utf-8")
    return counts


def import_all(conn: sqlite3.Connection, path: str,
               mode: str = "ignore") -> Dict[str, int]:
    """استعد الجداول الحرجة من ملف JSON.

    ``mode``: "ignore" (احتفظ بالموجود عند التعارض، الافتراضي والأسلم
    بعد استعادة تلقائية) أو "replace" (الملف المستورد يفوز - يُستخدم عند
    استعادة صريحة إلى قاعدة معروف أنها أُعيد بناؤها من الصفر).
    """
    from . import paper as paper_module
    paper_module.ensure_schema(conn)   # paper_* جداول تُنشأ عند الطلب لا مع db.connect

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    counts: Dict[str, int] = {}

    verb = "OR IGNORE" if mode == "ignore" else "OR REPLACE"
    # الترتيب في CRITICAL_TABLES يحترم تبعية المفاتيح الأجنبية (مثلاً
    # paper_decisions يشير إلى paper_runs). ترتيب JSON نفسه أبجدي
    # (sort_keys=True عند التصدير) فلا يصلح للاستيراد كما هو.
    for table in CRITICAL_TABLES:
        rows = data.get(table, [])
        if not rows:
            counts[table] = 0
            continue
        columns = list(rows[0].keys())
        placeholders = ", ".join("?" * len(columns))
        column_list = ", ".join(columns)
        conn.executemany(
            f"INSERT {verb} INTO {table} ({column_list}) VALUES ({placeholders})",
            [tuple(row[c] for c in columns) for row in rows])
        counts[table] = len(rows)

    conn.commit()
    return counts
