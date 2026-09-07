"""
tasi/cli.py
===========
أدوات سطر الأوامر - operational commands for the system.

    python -m tasi.cli db-status
    python -m tasi.cli universe-import companies.csv
    python -m tasi.cli shariah-template --out shariah_template.csv
    python -m tasi.cli shariah-import list.csv --source "مركز المقاصد - العصيمي" --as-of 2026-07-01
    python -m tasi.cli shariah-report
    python -m tasi.cli tradable --allow PURE
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from typing import List, Optional, Sequence

from . import db
from . import universe as u


def _print_table(rows: Sequence[Sequence[object]], headers: Sequence[str]) -> None:
    """اطبع جدولاً بمحاذاة صحيحة مع النص العربي."""
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


# ---------------------------------------------------------------------------
def cmd_db_status(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    counts = db.table_counts(conn)
    print(f"قاعدة البيانات: {args.db}\n")
    _print_table([(k, v) for k, v in counts.items()], ["الجدول", "الصفوف"])
    return 0


def cmd_universe_import(args: argparse.Namespace) -> int:
    """استورد قائمة الشركات من CSV: symbol,name_ar,name_en,sector,market"""
    conn = db.connect(args.db)
    companies: List[u.Company] = []
    with open(args.path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "symbol" not in reader.fieldnames:
            print("خطأ: الملف يجب أن يحتوي عمود symbol", file=sys.stderr)
            return 1
        for row in reader:
            symbol = u.normalize_symbol(row.get("symbol", ""))
            if not symbol:
                continue
            companies.append(u.Company(
                symbol=symbol,
                name_ar=(row.get("name_ar") or "").strip(),
                name_en=(row.get("name_en") or "").strip(),
                sector=(row.get("sector") or "").strip(),
                industry=(row.get("industry") or "").strip(),
                market=(row.get("market") or "MAIN").strip().upper(),
            ))
    count = u.upsert_companies(conn, companies)
    print(f"تم استيراد {count} شركة.")
    return 0


def cmd_shariah_template(args: argparse.Namespace) -> int:
    """أنشئ ملف CSV فارغاً بأرقام الشركات المعروفة ليُملأ يدوياً."""
    conn = db.connect(args.db)
    symbols = u.all_symbols(conn)
    if not symbols:
        print(
            "لا توجد شركات في قاعدة البيانات بعد.\n"
            "استورد قائمة الشركات أولاً (universe-import) أو املأ الملف يدوياً.",
            file=sys.stderr,
        )
    with open(args.out, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "name_ar", "status", "purification"])
        for symbol in symbols:
            company = u.get_company(conn, symbol)
            writer.writerow([symbol, company.name_ar if company else "", "", ""])
    print(f"تم إنشاء القالب: {args.out} ({len(symbols)} صف)")
    print("املأ عمود status بإحدى القيم: نقي / مختلط / محرم")
    return 0


def cmd_shariah_import(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        counts = u.import_shariah_csv(
            conn, args.path, source=args.source, as_of=args.as_of,
            symbol_col=args.symbol_col, status_col=args.status_col,
            purification_col=args.purification_col,
        )
    except (OSError, ValueError) as exc:
        print(f"فشل الاستيراد: {exc}", file=sys.stderr)
        return 1

    print(f"المصدر: {args.source} | تاريخ السريان: {args.as_of}\n")
    rows = [(u.STATUS_AR.get(k, k), v) for k, v in counts.items()
            if k != "skipped" and v]
    _print_table(rows, ["التصنيف", "العدد"])
    if counts.get("skipped"):
        print(f"\nتم تخطي {counts['skipped']} صفاً بلا رمز.")
    return 0


def cmd_shariah_report(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    report = u.coverage_report(conn, args.source)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["unverified"]:
        print(
            f"\nتنبيه: {report['unverified']} سهماً بلا تصنيف شرعي محقَّق. "
            "هذه الأسهم مستبعدة من التداول حتى تُصنَّف."
        )
    return 0


def cmd_tradable(args: argparse.Namespace) -> int:
    """اعرض الأسهم المسموح تداولها حسب التصنيف المختار."""
    conn = db.connect(args.db)
    symbols = u.filter_by_shariah(conn, args.allow, source=args.source)
    rows = []
    for symbol in symbols:
        company = u.get_company(conn, symbol)
        status = u.get_shariah_status(conn, symbol, args.source)
        rows.append((symbol, company.name_ar if company else "",
                     status["status_ar"], status["as_of"] or ""))
    if not rows:
        print("لا توجد أسهم مطابقة. تحقق من استيراد التصنيف الشرعي.")
        return 1
    _print_table(rows, ["الرمز", "الشركة", "التصنيف", "التاريخ"])
    print(f"\nالإجمالي: {len(rows)} سهم")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tasi", description="أدوات نظام تحليل السوق السعودي")
    parser.add_argument("--db", default=db.DEFAULT_DB, help="مسار قاعدة البيانات")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("db-status", help="حالة الجداول").set_defaults(func=cmd_db_status)

    p = sub.add_parser("universe-import", help="استيراد قائمة الشركات من CSV")
    p.add_argument("path")
    p.set_defaults(func=cmd_universe_import)

    p = sub.add_parser("shariah-template", help="إنشاء قالب CSV للتصنيف الشرعي")
    p.add_argument("--out", default="shariah_template.csv")
    p.set_defaults(func=cmd_shariah_template)

    p = sub.add_parser("shariah-import", help="استيراد التصنيف الشرعي من CSV")
    p.add_argument("path")
    p.add_argument("--source", required=True, help="اسم الجهة المصنِّفة")
    p.add_argument("--as-of", required=True, dest="as_of",
                   help="تاريخ سريان التصنيف YYYY-MM-DD")
    p.add_argument("--symbol-col", default="symbol")
    p.add_argument("--status-col", default="status")
    p.add_argument("--purification-col", default=None)
    p.set_defaults(func=cmd_shariah_import)

    p = sub.add_parser("shariah-report", help="تقرير تغطية التصنيف الشرعي")
    p.add_argument("--source", default=None)
    p.set_defaults(func=cmd_shariah_report)

    p = sub.add_parser("tradable", help="الأسهم المسموح تداولها")
    p.add_argument("--allow", nargs="+", default=["PURE"],
                   help="التصنيفات المسموحة: PURE MIXED")
    p.add_argument("--source", default=None)
    p.set_defaults(func=cmd_tradable)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
