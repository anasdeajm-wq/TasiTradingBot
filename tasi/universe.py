"""
tasi/universe.py
================
كون الأسهم - the tradable universe: companies, sectors, Shariah status.

مبدأ أساسي في هذا الملف: **النظام لا يصدر حكماً شرعياً أبداً.**

التصنيف الشرعي (نقي / مختلط / محرّم) يتغيّر كل ربع سنة، ويصدر عن جهات
مختلفة قد تختلف فيما بينها على نفس السهم. لذلك:

    - الحالة الافتراضية لأي سهم هي UNVERIFIED
    - التصنيف يُستورد من مصدر يختاره المستخدم ويثق به
    - كل تصنيف يُخزَّن مع اسم مصدره وتاريخ سريانه
    - الفلترة تعتمد المصدر الذي يحدده المستخدم، لا اجتهاد النظام

هذا ليس تحفّظاً تقنياً: تخمين حكم شرعي خاطئ يعني أن المستخدم قد يشتري
سهماً يعتبره محرّماً، وهذا ضرر لا يصلحه التراجع لاحقاً.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence

# حالات التصنيف الشرعي
PURE = "PURE"                    # نقي
MIXED = "MIXED"                  # مختلط
NON_COMPLIANT = "NON_COMPLIANT"  # محرّم
UNVERIFIED = "UNVERIFIED"        # غير محقَّق - الحالة الافتراضية

VALID_STATUSES = {PURE, MIXED, NON_COMPLIANT, UNVERIFIED}

STATUS_AR = {
    PURE: "نقي",
    MIXED: "مختلط",
    NON_COMPLIANT: "محرّم",
    UNVERIFIED: "غير محقَّق",
}

# مرادفات عربية وإنجليزية تُقبل عند الاستيراد من ملف CSV
_STATUS_ALIASES = {
    "نقي": PURE, "نقية": PURE, "pure": PURE, "compliant": PURE, "halal": PURE,
    "مختلط": MIXED, "مختلطة": MIXED, "mixed": MIXED,
    "محرم": NON_COMPLIANT, "محرّم": NON_COMPLIANT, "غير متوافق": NON_COMPLIANT,
    "non_compliant": NON_COMPLIANT, "haram": NON_COMPLIANT,
}


@dataclass
class Company:
    symbol: str
    name_ar: str = ""
    name_en: str = ""
    sector: str = ""
    industry: str = ""
    market: str = "MAIN"
    isin: str = ""
    is_active: bool = True

    @property
    def display(self) -> str:
        return f"{self.symbol} {self.name_ar or self.name_en}".strip()


def normalize_symbol(symbol: str) -> str:
    """وحّد صيغة الرمز: 2222.SR أو 2222:XSAU أو 2222 -> 2222."""
    s = str(symbol).strip().upper()
    for sep in (".", ":"):
        if sep in s:
            s = s.split(sep)[0]
    return s.strip()


def normalize_status(value: str) -> str:
    """حوّل نص التصنيف إلى إحدى الحالات المعتمدة."""
    key = str(value).strip().lower()
    if key in _STATUS_ALIASES:
        return _STATUS_ALIASES[key]
    upper = str(value).strip().upper()
    return upper if upper in VALID_STATUSES else UNVERIFIED


# ---------------------------------------------------------------------------
# الشركات - companies
# ---------------------------------------------------------------------------
def upsert_companies(conn: sqlite3.Connection, companies: Iterable[Company]) -> int:
    """احفظ أو حدّث بيانات الشركات. يُرجع العدد المتأثر."""
    now = datetime.now().isoformat(timespec="seconds")
    rows = [
        (c.symbol, c.name_ar, c.name_en, c.sector, c.industry, c.market,
         c.isin, int(c.is_active), now)
        for c in companies
    ]
    conn.executemany(
        """
        INSERT INTO companies
            (symbol, name_ar, name_en, sector, industry, market, isin,
             is_active, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            name_ar   = excluded.name_ar,
            name_en   = excluded.name_en,
            sector    = excluded.sector,
            industry  = excluded.industry,
            market    = excluded.market,
            isin      = excluded.isin,
            is_active = excluded.is_active,
            updated_at= excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def get_company(conn: sqlite3.Connection, symbol: str) -> Optional[Company]:
    row = conn.execute(
        "SELECT * FROM companies WHERE symbol = ?", (normalize_symbol(symbol),)
    ).fetchone()
    if not row:
        return None
    return Company(
        symbol=row["symbol"], name_ar=row["name_ar"] or "",
        name_en=row["name_en"] or "", sector=row["sector"] or "",
        industry=row["industry"] or "", market=row["market"] or "MAIN",
        isin=row["isin"] or "", is_active=bool(row["is_active"]),
    )


def all_symbols(conn: sqlite3.Connection, market: Optional[str] = None,
                active_only: bool = True) -> List[str]:
    sql = "SELECT symbol FROM companies WHERE 1=1"
    params: List[object] = []
    if active_only:
        sql += " AND is_active = 1"
    if market:
        sql += " AND market = ?"
        params.append(market)
    sql += " ORDER BY symbol"
    return [r["symbol"] for r in conn.execute(sql, params)]


# ---------------------------------------------------------------------------
# التصنيف الشرعي - Shariah status
# ---------------------------------------------------------------------------
def set_shariah_status(
    conn: sqlite3.Connection,
    symbol: str,
    status: str,
    source: str,
    as_of: str,
    purification_rate: Optional[float] = None,
    notes: str = "",
) -> None:
    """سجّل تصنيفاً شرعياً لسهم من مصدر محدد بتاريخ محدد."""
    status = normalize_status(status)
    if status not in VALID_STATUSES:
        raise ValueError(f"تصنيف غير معروف: {status}")
    if not source:
        raise ValueError("المصدر مطلوب: لا يُقبل تصنيف بلا جهة مصنِّفة")

    conn.execute(
        """
        INSERT INTO shariah_status
            (symbol, status, source, as_of, purification_rate, notes, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, source, as_of) DO UPDATE SET
            status = excluded.status,
            purification_rate = excluded.purification_rate,
            notes = excluded.notes,
            imported_at = excluded.imported_at
        """,
        (normalize_symbol(symbol), status, source, as_of, purification_rate,
         notes, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def get_shariah_status(
    conn: sqlite3.Connection,
    symbol: str,
    source: Optional[str] = None,
    as_of: Optional[str] = None,
) -> Dict[str, object]:
    """أحدث تصنيف شرعي للسهم من مصدر معيّن (أو أي مصدر).

    يُرجع دائماً قاموساً؛ الحالة UNVERIFIED إن لم يوجد تصنيف مستورَد.
    """
    symbol = normalize_symbol(symbol)
    sql = "SELECT * FROM shariah_status WHERE symbol = ?"
    params: List[object] = [symbol]
    if source:
        sql += " AND source = ?"
        params.append(source)
    if as_of:
        sql += " AND as_of <= ?"
        params.append(as_of)
    sql += " ORDER BY as_of DESC, imported_at DESC LIMIT 1"

    row = conn.execute(sql, params).fetchone()
    if not row:
        return {
            "symbol": symbol,
            "status": UNVERIFIED,
            "status_ar": STATUS_AR[UNVERIFIED],
            "source": None,
            "as_of": None,
            "purification_rate": None,
        }
    return {
        "symbol": symbol,
        "status": row["status"],
        "status_ar": STATUS_AR.get(row["status"], row["status"]),
        "source": row["source"],
        "as_of": row["as_of"],
        "purification_rate": row["purification_rate"],
    }


def filter_by_shariah(
    conn: sqlite3.Connection,
    allowed: Sequence[str],
    source: Optional[str] = None,
    symbols: Optional[Sequence[str]] = None,
) -> List[str]:
    """أرجع الأسهم المطابقة للتصنيفات المسموحة فقط.

    الأسهم غير المحقَّقة تُستبعد ما لم يُطلب UNVERIFIED صراحة، حتى لا
    يتسرّب سهم غير مصنَّف إلى قائمة التداول بالخطأ.
    """
    allowed_set = {normalize_status(a) for a in allowed}
    candidates = list(symbols) if symbols else all_symbols(conn)
    result = []
    for symbol in candidates:
        status = get_shariah_status(conn, symbol, source)["status"]
        if status in allowed_set:
            result.append(symbol)
    return result


def import_shariah_csv(
    conn: sqlite3.Connection,
    path: str,
    source: str,
    as_of: str,
    symbol_col: str = "symbol",
    status_col: str = "status",
    purification_col: Optional[str] = None,
) -> Dict[str, int]:
    """استورد قائمة التصنيف الشرعي من ملف CSV يوفّره المستخدم.

    الملف المتوقع (الأعمدة قابلة للتسمية):

        symbol,status
        2222,نقي
        1120,مختلط
        1010,محرم

    يُرجع إحصاء بعدد كل تصنيف، ويرفع الخطأ إذا كان الملف فارغاً أو
    ينقصه عمود مطلوب - أفضل من استيراد صامت ناقص.
    """
    counts: Dict[str, int] = {s: 0 for s in VALID_STATUSES}
    counts["skipped"] = 0

    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"الملف فارغ أو بلا ترويسة: {path}")
        missing = {symbol_col, status_col} - set(reader.fieldnames)
        if missing:
            raise ValueError(
                f"أعمدة مفقودة في {path}: {', '.join(sorted(missing))} "
                f"(الأعمدة الموجودة: {', '.join(reader.fieldnames)})"
            )

        for row in reader:
            raw_symbol = (row.get(symbol_col) or "").strip()
            if not raw_symbol:
                counts["skipped"] += 1
                continue
            symbol = normalize_symbol(raw_symbol)
            status = normalize_status(row.get(status_col, ""))

            purification = None
            if purification_col and row.get(purification_col):
                try:
                    purification = float(str(row[purification_col]).replace("%", ""))
                except ValueError:
                    purification = None

            # أنشئ الشركة إن لم تكن موجودة، حتى لا يفشل قيد المفتاح الأجنبي
            conn.execute(
                "INSERT OR IGNORE INTO companies (symbol, updated_at) VALUES (?, ?)",
                (symbol, datetime.now().isoformat(timespec="seconds")),
            )
            set_shariah_status(conn, symbol, status, source, as_of, purification)
            counts[status] = counts.get(status, 0) + 1

    conn.commit()
    return counts


def coverage_report(conn: sqlite3.Connection,
                    source: Optional[str] = None) -> Dict[str, object]:
    """تقرير التغطية: كم سهماً مصنَّف وكم بقي غير محقَّق."""
    symbols = all_symbols(conn)
    tally: Dict[str, int] = {s: 0 for s in VALID_STATUSES}
    for symbol in symbols:
        tally[get_shariah_status(conn, symbol, source)["status"]] += 1

    total = len(symbols)
    verified = total - tally[UNVERIFIED]
    return {
        "total_companies": total,
        "verified": verified,
        "unverified": tally[UNVERIFIED],
        "coverage_pct": round(100.0 * verified / total, 1) if total else 0.0,
        "by_status": {STATUS_AR[k]: v for k, v in tally.items()},
        "source": source or "أي مصدر",
    }
