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
    python -m tasi.cli backtest --max-bars 15
    python -m tasi.cli briefing
    python -m tasi.cli viability --capital 300
"""

from __future__ import annotations

import argparse
import csv
import os
import json
import sys
from datetime import date
from typing import List, Optional, Sequence

from . import backtest as bt
from . import db
from . import journal as jr
from . import quality as ql
from . import regime as rg
from . import universe as u
from .risk import RiskManager


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



def _load_bars(conn, symbol: str, interval: str = "1day"):
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM bars"
        " WHERE symbol = ? AND interval = ? ORDER BY ts ASC",
        (symbol, interval)).fetchall()
    if not rows:
        return None
    return {
        "ts": [r["ts"] for r in rows],
        "open": [float(r["open"] or r["close"]) for r in rows],
        "high": [float(r["high"] or r["close"]) for r in rows],
        "low": [float(r["low"] or r["close"]) for r in rows],
        "close": [float(r["close"]) for r in rows],
        "volume": [float(r["volume"] or 0) for r in rows],
    }


def cmd_backtest(args: argparse.Namespace) -> int:
    """قِس أداء كل نمط على البيانات المخزّنة، واحفظ الأوزان."""
    conn = db.connect(args.db)
    symbols = args.symbols or u.all_symbols(conn)
    if not symbols:
        print("لا توجد شركات في قاعدة البيانات.", file=sys.stderr)
        return 1

    benchmark = None
    regimes_by_ts = None
    if args.benchmark:
        bench_bars = _load_bars(conn, args.benchmark)
        if bench_bars:
            benchmark = bench_bars["close"]
            # صنّف حالة السوق لكل يوم من سلسلة المؤشر. بدون هذا يُقاس كل
            # نمط بمتوسط واحد عبر حالات متناقضة، وهو بالضبط ما نتجنبه.
            regimes_by_ts = {}
            closes, stamps = bench_bars["close"], bench_bars["ts"]
            for i in range(60, len(closes)):
                regimes_by_ts[stamps[i]] = rg.classify(
                    closes[:i + 1], as_of=stamps[i]).regime
            from collections import Counter
            spread = Counter(regimes_by_ts.values())
            print("توزيع حالات السوق: "
                  + " | ".join(f"{rg.REGIME_AR.get(k, k)} {v}"
                               for k, v in spread.most_common()) + "\n")
        else:
            print(f"تحذير: لا توجد بيانات للمؤشر {args.benchmark}؛ سيتم "
                  "تجاهل القوة النسبية وتصنيف الحالة.", file=sys.stderr)

    if args.shariah:
        allowed = set(u.filter_by_shariah(conn, args.shariah))
        before = len(symbols)
        symbols = [s for s in symbols if s in allowed]
        print(f"فلترة شرعية: {len(symbols)} من {before} سهماً مطابق "
              f"للتصنيفات {', '.join(args.shariah)}.\n")

    all_trades = []
    skipped = []
    rejected = []
    for symbol in symbols:
        bars = _load_bars(conn, symbol)
        if not bars or len(bars["close"]) < args.start + 20:
            skipped.append(symbol)
            continue

        # افحص الجودة قبل القياس. سلسلة فاسدة تُنتج أرقاماً تبدو سليمة
        # وهي بلا معنى، وهذا أسوأ من عدم وجود أرقام.
        report = ql.check_bars(symbol, bars)
        if not report.ok:
            rejected.append(report)
            continue
        bench = benchmark if (benchmark and len(benchmark) == len(bars["close"])) else None
        all_trades += bt.run_symbol(
            symbol, bars, benchmark_closes=bench, regimes_by_ts=regimes_by_ts,
            max_bars=args.max_bars, start=args.start,
            respect_regime=not args.ignore_regime)

    if skipped:
        print(f"تم تخطي {len(skipped)} سهماً لعدم كفاية التاريخ: "
              f"{', '.join(skipped[:8])}{'...' if len(skipped) > 8 else ''}\n")
    if rejected:
        print(f"تم رفض {len(rejected)} سهماً لفشل فحص الجودة:")
        for report in rejected[:5]:
            print(f"  {report.summary_ar()}")
        print()

    if not all_trades:
        print("لم تُنتج أي إشارة على البيانات المتاحة.", file=sys.stderr)
        return 1

    wins = sum(1 for t in all_trades if t.is_win)
    avg_r = sum(t.r_multiple for t in all_trades) / len(all_trades)
    print(f"الصفقات المحاكاة : {len(all_trades)}")
    print(f"نسبة الإصابة     : {wins / len(all_trades):.1%}")
    print(f"متوسط R          : {avg_r:+.3f}\n")

    stats = bt.aggregate(all_trades)
    rows = bt.summary_report(stats, min_trades=args.min_trades)
    if rows:
        _print_table(
            [[r["النمط"], r["الحالة"], r["صفقات"], r["نسبة الإصابة"],
              f"{r['التوقع']:+.3f}", r["عامل الربح"] or "-", r["صالح"]] for r in rows],
            ["النمط", "الحالة", "صفقات", "إصابة", "التوقع", "عامل الربح", "صالح"])
    else:
        print(f"لا يوجد نمط بلغ {args.min_trades} صفقة على الأقل.")

    if args.save:
        bt.save_performance(conn, stats)
        print("\nتم حفظ الأوزان في setup_performance.")
    else:
        print("\n(لم تُحفظ الأوزان. أضف --save للحفظ.)")
    return 0


def cmd_briefing(args: argparse.Namespace) -> int:
    """اعرض ملخص ما قبل الافتتاح."""
    conn = db.connect(args.db)
    briefing = jr.build_briefing(conn, for_date=args.date)
    text = jr.render_briefing_ar(briefing)
    print(text)
    if args.save:
        jr.save_journal(conn, args.date or date.today().isoformat(), briefing, text)
        print("\nتم حفظ التقرير في session_journal.")
    return 0


def cmd_regime(args: argparse.Namespace) -> int:
    """صنّف حالة السوق من بيانات المؤشر المخزّنة."""
    conn = db.connect(args.db)
    bars = _load_bars(conn, args.benchmark)
    if not bars:
        print(f"لا توجد بيانات للمؤشر {args.benchmark}.", file=sys.stderr)
        return 1
    breadth = rg.compute_breadth(conn, bars["ts"][-1])
    snap = rg.classify(bars["close"], breadth=breadth, as_of=bars["ts"][-1])
    print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))
    if args.save:
        rg.save_regime(conn, snap)
        print("\nتم الحفظ في market_regimes.")
    return 0


def cmd_viability(args: argparse.Namespace) -> int:
    """هل رأس المال كافٍ للتداول بعد التكاليف؟"""
    risk = RiskManager(capital=args.capital, commission_pct=args.commission / 100)
    report = risk.viability_report(sample_price=args.price, sample_atr=args.atr)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sample = report["sample_result"]
    print()
    if sample["approved"]:
        print(f"النتيجة: الصفقة النموذجية ممكنة بـ {sample['shares']} سهم، "
              f"ربح متوقع {sample['expected_profit']:.2f} ريال بعد التكاليف.")
    else:
        print(f"النتيجة: الصفقة النموذجية مرفوضة. {sample['reason']}")
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

    p = sub.add_parser("backtest", help="قياس أداء الأنماط على البيانات المخزّنة")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--benchmark", default="TASI", help="رمز المؤشر للقوة النسبية")
    p.add_argument("--max-bars", type=int, default=15, dest="max_bars")
    p.add_argument("--start", type=int, default=200,
                   help="عدد الشموع المحجوزة لتسخين المؤشرات")
    p.add_argument("--min-trades", type=int, default=5, dest="min_trades")
    p.add_argument("--ignore-regime", action="store_true", dest="ignore_regime")
    p.add_argument("--shariah", nargs="*", default=None,
                   help="اقصر القياس على تصنيفات شرعية، مثل: --shariah PURE MIXED")
    p.add_argument("--save", action="store_true", help="احفظ الأوزان المقيسة")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("briefing", help="ملخص ما قبل الافتتاح")
    p.add_argument("--date", default=None)
    p.add_argument("--save", action="store_true")
    p.set_defaults(func=cmd_briefing)

    p = sub.add_parser("regime", help="تصنيف حالة السوق")
    p.add_argument("--benchmark", default="TASI")
    p.add_argument("--save", action="store_true")
    p.set_defaults(func=cmd_regime)

    p = sub.add_parser("viability", help="هل رأس المال كافٍ بعد التكاليف؟")
    p.add_argument("--capital", type=float, default=300.0)
    p.add_argument("--price", type=float, default=30.0)
    p.add_argument("--atr", type=float, default=0.6)
    p.add_argument("--commission", type=float, default=0.155,
                   help="عمولة الوسيط بالنسبة المئوية لكل جهة")
    p.set_defaults(func=cmd_viability)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        # يحدث عند التمرير إلى head أو less. الخروج بهدوء أنظف من أثر خطأ.
        try:
            sys.stdout.close()
        finally:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        print("\nتم الإيقاف.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
