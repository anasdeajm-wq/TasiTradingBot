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
    python -m tasi.cli scan --allow-uncalibrated
    python -m tasi.cli preflight
"""

from __future__ import annotations

import argparse
import csv
import os
import json
import sys
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from . import backtest as bt
from . import db
from . import engine as eng
from . import forward as fw
from . import journal as jr
from . import momentum as mo
from . import quality as ql
from . import regime as rg
from . import sweep as sw
from . import setups as S
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

    # التحقق خارج العيّنة: الرقم الرابح داخل العيّنة لا يعني أفضلية.
    verdicts = bt.validate(all_trades, train_fraction=args.train_fraction,
                           min_trades_each=args.min_trades)
    rows = bt.validation_report(verdicts, min_trades=args.min_trades)
    if rows:
        print("\n" + "─" * 70)
        print("التحقق خارج العيّنة")
        print("─" * 70)
        _print_table(
            [[r["النمط"], r["الحالة"], r["صفقات التدريب"],
              f"{r['توقع التدريب']:+.3f}" if r["توقع التدريب"] is not None else "—",
              r["صفقات التحقق"],
              f"{r['توقع التحقق']:+.3f}" if r["توقع التحقق"] is not None else "—",
              r["صمد"]] for r in rows],
            ["النمط", "الحالة", "صفقات تدريب", "توقع تدريب",
             "صفقات تحقق", "توقع تحقق", "صمد"])
        survivors = [r for r in rows if r["صمد"] == "نعم"]
        print(f"\nصمد {len(survivors)} من {len(rows)} تركيبة.")
        if not survivors:
            print("لا يوجد نمط له أفضلية مؤكدة. النظام لن يُنتج إشارات.")

    if args.save:
        bt.save_validated_performance(conn, all_trades,
                                      train_fraction=args.train_fraction,
                                      min_trades_each=args.min_trades)
        print("\nتم الحفظ. الوزن الموجب يُمنح فقط لنمط صمد خارج العيّنة.")
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



def _build_engine(args: argparse.Namespace, conn):
    from .providers.replay import ReplayProvider
    from .notifier import TelegramNotifier

    provider = ReplayProvider(conn)      # المزوّد الوحيد المتاح بلا اشتراك
    risk = RiskManager(capital=args.capital, conn=conn)
    return eng.Engine(
        conn, provider, risk=risk,
        notifier=TelegramNotifier(enabled=not args.no_telegram),
        shariah_allow=args.allow or None, benchmark=args.benchmark,
        allow_uncalibrated=args.allow_uncalibrated, min_score=args.min_score,
    )


def cmd_preflight(args: argparse.Namespace) -> int:
    """افحص جاهزية النظام قبل الجلسة."""
    conn = db.connect(args.db)
    checks = _build_engine(args, conn).preflight(require_realtime=args.realtime)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print()
    if checks["ok"]:
        print("النظام جاهز.")
    else:
        print(f"النظام غير جاهز. {len(checks['problems'])} عائق:")
        for problem in checks["problems"]:
            print(f"  - {problem}")
    return 0 if checks["ok"] else 1


def cmd_scan(args: argparse.Namespace) -> int:
    """شغّل دورة واحدة واعرض الاقتراحات."""
    conn = db.connect(args.db)
    engine = _build_engine(args, conn)

    checks = engine.preflight()
    if not checks["ok"] and not args.force:
        print("النظام غير جاهز:", file=sys.stderr)
        for problem in checks["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        print("\nأضف --force للتشغيل رغم ذلك.", file=sys.stderr)
        return 1

    result = engine.run_cycle(symbols=args.symbols, dry_run=not args.emit)
    print(engine.render_cycle_ar(result))
    if result.proposals and not args.emit:
        print("\n(عرض فقط. أضف --emit لتسجيل الإشارات وإرسال التنبيهات.)")
    return 0



def _yahoo():
    from .providers.yahoo import YahooProvider
    return YahooProvider()


def cmd_discover(args: argparse.Namespace) -> int:
    """اكتشف رموز تداول الموجودة فعلاً واحفظها في قاعدة البيانات."""
    from .providers.yahoo import tadawul_candidates

    conn = db.connect(args.db)
    provider = _yahoo()
    candidates = args.symbols or tadawul_candidates()
    print(f"فحص {len(candidates)} رمزاً مرشحاً عبر ياهو... قد يستغرق دقائق.\n")

    seen = []

    def report(symbol: str, name: str) -> None:
        seen.append(symbol)
        if len(seen) % 25 == 0:
            print(f"  وُجد {len(seen)} حتى الآن...")

    found = provider.discover_symbols(candidates, on_found=report)
    if not found:
        print("لم يُعثر على أي رمز. تحقق من الاتصال.", file=sys.stderr)
        return 1

    companies = [
        u.Company(symbol=e["symbol"], name_en=e["name_en"],
                  market="NOMU" if e["symbol"].startswith("9") else "MAIN")
        for e in found
    ]
    u.upsert_companies(conn, companies)
    print(f"\nتم حفظ {len(companies)} شركة.")
    print(f"  السوق الرئيسية: {sum(1 for c in companies if c.market == 'MAIN')}")
    print(f"  السوق الموازية : {sum(1 for c in companies if c.market == 'NOMU')}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """حمّل الشموع التاريخية من ياهو إلى قاعدة البيانات."""
    conn = db.connect(args.db)
    provider = _yahoo()

    symbols = args.symbols or u.all_symbols(conn)
    if args.benchmark not in symbols:
        symbols = [args.benchmark] + list(symbols)
    if not symbols:
        print("لا توجد رموز. شغّل universe-discover أولاً.", file=sys.stderr)
        return 1

    print(f"جلب {len(symbols)} رمزاً بفاصل {args.interval}...\n")
    saved = failed = flagged = 0
    now = datetime.now().isoformat(timespec="seconds")

    for i, symbol in enumerate(symbols, 1):
        try:
            bars = provider.get_bars(symbol, args.interval, limit=args.limit)
        except Exception as exc:                          # noqa: BLE001
            failed += 1
            if args.verbose:
                print(f"  {symbol}: فشل - {exc}")
            continue

        if not bars:
            failed += 1
            continue

        rows = [(symbol, args.interval, b.ts.date().isoformat(),
                 b.open, b.high, b.low, b.close, b.volume, None, None)
                for b in bars]
        conn.executemany(
            "INSERT OR REPLACE INTO bars"
            " (symbol, interval, ts, open, high, low, close, volume, turnover, trades)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        saved += 1

        series = {"ts": [r[2] for r in rows], "open": [r[3] for r in rows],
                  "high": [r[4] for r in rows], "low": [r[5] for r in rows],
                  "close": [r[6] for r in rows], "volume": [r[7] for r in rows]}
        report = ql.check_bars(symbol, series)
        if not report.ok:
            flagged += 1
            print(f"  ⚠ {report.summary_ar()}")

        if i % 25 == 0:
            print(f"  {i}/{len(symbols)}...")

    print(f"\nتم حفظ {saved} رمزاً | فشل {failed} | فشل فحص الجودة {flagged}")
    total = conn.execute("SELECT COUNT(*) c FROM bars").fetchone()["c"]
    print(f"إجمالي الشموع في القاعدة: {total:,}")
    return 0 if saved else 1



def cmd_sweep(args: argparse.Namespace) -> int:
    """امسح معاملات نمط وقيّم كل تركيبة خارج العيّنة."""
    conn = db.connect(args.db)

    symbols = args.symbols or u.all_symbols(conn)
    if args.market:
        rows = conn.execute("SELECT symbol FROM companies WHERE market = ?",
                            (args.market,)).fetchall()
        allowed = {r["symbol"] for r in rows}
        symbols = [s for s in symbols if s in allowed]
    if args.limit_symbols:
        symbols = symbols[:args.limit_symbols]

    names = args.setups or list(sw.DEFAULT_GRIDS)
    print(f"مسح {len(names)} نمطاً على {len(symbols)} سهماً.\n")

    overall_tested = 0
    overall_survivors = []
    overall_expected = 0.0

    for name in names:
        try:
            cls = sw.setup_class_by_name(name)
        except ValueError as exc:
            print(f"تخطي: {exc}", file=sys.stderr)
            continue
        grid = sw.DEFAULT_GRIDS.get(name, {})
        combos = len(sw.expand_grid(grid))
        print(f"── {S.SETUP_NAMES_AR.get(name, name)} · {combos} تركيبة ──")

        def progress(i, total, survivors):
            if i % 10 == 0 or i == total:
                print(f"   {i}/{total} · ناجون {survivors}")

        report = sw.sweep(conn, cls, grid, symbols, benchmark=args.benchmark,
                          max_bars=args.max_bars,
                          min_trades_each=args.min_trades,
                          train_fraction=args.train_fraction,
                          on_progress=progress)
        overall_tested += report.combinations_tested
        overall_survivors += report.survivors
        overall_expected += report.expected_by_chance

        print(f"   {report.verdict_ar()}")
        top = report.results[:args.top]
        if top:
            _print_table(
                [[r.to_row()["الحالة"], r.to_row()["المعاملات"],
                  r.train_trades,
                  f"{r.train_expectancy:+.3f}" if r.train_expectancy is not None else "—",
                  r.test_trades,
                  f"{r.test_expectancy:+.3f}" if r.test_expectancy is not None else "—",
                  "نعم" if r.survived else "لا"] for r in top],
                ["الحالة", "المعاملات", "تدريب", "توقع تدريب",
                 "تحقق", "توقع تحقق", "صمد"])
        print()

    # الفرضية الصفرية مجموعة من تقديرات كل نمط على حدة، لا رقم مفترض،
    # حتى لا يتناقض الملخص مع أحكام الأنماط أعلاه.
    found = len(overall_survivors)
    print("═" * 66)
    print(f"إجمالي التركيبات المختبَرة : {overall_tested}")
    print(f"الناجون                    : {found}")
    print(f"المتوقع بالصدفة وحدها      : {overall_expected:.1f}")
    print(f"عتبة القبول بعد الهامش     : "
          f"{overall_expected * sw.SweepReport.CHANCE_MARGIN:.1f}")

    if not overall_tested:
        print("\nالحكم: لم تُنتج أي تركيبة صفقات كافية للحكم.")
    elif found == 0:
        print("\nالحكم: لا ناجي. لا أفضلية في فضاء المعاملات المجرَّب.")
    elif found <= overall_expected * sw.SweepReport.CHANCE_MARGIN:
        print(f"\nالحكم: {found} ناجٍ مقابل {overall_expected:.1f} متوقعة بالصدفة. "
              "الفارق ضمن الضجيج، ولا يصلح للتشغيل.")
    else:
        print(f"\nالحكم: {found} ناجٍ مقابل {overall_expected:.1f} متوقعة بالصدفة. "
              "مرشّح يستحق تحققاً متدحرجاً على فترات متعددة قبل أي تشغيل.")
    return 0



def cmd_forward(args: argparse.Namespace) -> int:
    """اختبار تقدّمي بمحفظة واحدة: يقيس نسبة الخطأ والخسارة قبل أي إطلاق."""
    conn = db.connect(args.db)

    symbols = args.symbols or u.all_symbols(conn)
    if args.market:
        rows = conn.execute("SELECT symbol FROM companies WHERE market = ?",
                            (args.market,)).fetchall()
        allowed = {r["symbol"] for r in rows}
        symbols = [s for s in symbols if s in allowed]
    if args.shariah:
        allowed = set(u.filter_by_shariah(conn, args.shariah))
        symbols = [s for s in symbols if s in allowed]
    if args.limit_symbols:
        symbols = symbols[:args.limit_symbols]

    weights = None
    if not args.allow_uncalibrated:
        rows = conn.execute(
            "SELECT setup, regime, weight FROM setup_performance").fetchall()
        weights = {(r["setup"], r["regime"]): float(r["weight"] or 0) for r in rows}
        if not any(weights.values()):
            print("لا يوجد نمط بوزن موجب، فلن تُفتح أي صفقة.", file=sys.stderr)
            print("أضف --allow-uncalibrated لرؤية ما كان سيحدث فعلاً.\n",
                  file=sys.stderr)

    print(f"اختبار تقدّمي على {len(symbols)} سهماً برأس مال "
          f"{args.capital:,.0f} ريال...\n")

    def progress(day, total, equity):
        print(f"  {day}/{total} جلسة · رأس المال {equity:,.0f} ريال")

    try:
        report = fw.run_forward(
            conn, symbols, capital=args.capital, benchmark=args.benchmark,
            weights=weights, max_hold_days=args.max_hold,
            min_score=args.min_score, max_open=args.max_open,
            commission_pct=args.commission / 100,
            risk_per_trade=args.risk / 100,
            allow_uncalibrated=args.allow_uncalibrated,
            on_progress=progress)
    except ValueError as exc:
        print(f"تعذّر التشغيل: {exc}", file=sys.stderr)
        return 1

    print()
    print(report.render_ar())

    if args.by_setup and report.trades:
        by: Dict[str, List] = {}
        for trade in report.trades:
            by.setdefault(trade.setup, []).append(trade)
        print("الأداء حسب النمط")
        print("─" * 64)
        _print_table(
            [[S.SETUP_NAMES_AR.get(k, k), len(v),
              f"{sum(1 for t in v if t.pnl > 0) / len(v):.0%}",
              f"{sum(t.pnl for t in v):+,.0f}"]
             for k, v in sorted(by.items(),
                                key=lambda kv: -sum(t.pnl for t in kv[1]))],
            ["النمط", "صفقات", "نسبة الربح", "صافي الريالات"])
    return 0



def cmd_momentum(args: argparse.Namespace) -> int:
    """الزخم المقطعي: اختبار تاريخي أو الترتيب الحالي."""
    conn = db.connect(args.db)

    if args.picks:
        try:
            picks = mo.current_picks(conn, lookback=args.lookback, hold=args.hold,
                                     min_turnover=args.min_turnover,
                                     market=args.market)
        except ValueError as exc:
            print(f"تعذّر: {exc}", file=sys.stderr)
            return 1
        if not picks:
            print("لا توجد أسهم مطابقة لشروط السيولة.", file=sys.stderr)
            return 1
        print(f"الترتيب الحالي · نظرة {args.lookback} جلسة · "
              f"سيولة ≥ {args.min_turnover / 1e6:.0f}م ريال\n")
        _print_table(
            [[i, h.symbol,
              (u.get_company(conn, h.symbol).name_ar
               if u.get_company(conn, h.symbol) else ""),
              f"{h.score * 100:+.1f}%", f"{h.turnover / 1e6:.1f}م"]
             for i, h in enumerate(picks, 1)],
            ["#", "الرمز", "الشركة", "قوة نسبية", "سيولة يومية"])
        print("\nهذه نتيجة نموذج لم يُشغَّل بمال حقيقي بعد. راجع تنبيه "
              "تحيّز البقاء في tasi/momentum.py قبل أي قرار.")
        return 0

    try:
        result = mo.run(conn, lookback=args.lookback, hold=args.hold,
                        min_turnover=args.min_turnover, slippage=args.slippage,
                        market=args.market)
    except ValueError as exc:
        print(f"تعذّر: {exc}", file=sys.stderr)
        return 1

    if not result.periods:
        print("لم تُنتج أي فترة إعادة توازن.", file=sys.stderr)
        return 1

    print("═" * 60)
    print(f"الزخم المقطعي · نظرة {args.lookback} · {args.hold} أسهم · "
          f"سيولة ≥ {args.min_turnover / 1e6:.0f}م · انزلاق {args.slippage:.1%}")
    print("═" * 60)
    print(f"  عائد المحفظة   : {result.total_return_pct:>+10.1f}%")
    print(f"  عائد المؤشر    : {result.benchmark_return_pct:>+10.1f}%")
    print(f"  الفارق         : {result.excess_pct:>+10.1f}%")
    annual = result.annualised()
    if annual is not None:
        print(f"  عائد سنوي      : {annual:>+10.2f}%")
    print(f"  أقصى تراجع     : {result.max_drawdown_pct():>10.1f}%")
    rate = result.win_rate()
    if rate is not None:
        print(f"  فترات رابحة    : {rate:>10.0%}")
    worst = result.worst_period()
    if worst:
        print(f"  أسوأ فترة      : {worst[1] * 100:>+10.1f}%  ({worst[0][:7]})")
    print(f"  فترات          : {result.periods:>10}")

    if args.by_year:
        print("\nالأداء السنوي")
        print("─" * 60)
        for year, value in sorted(result.by_year().items()):
            print(f"  {year}: {value:>+8.1f}%")

    if args.validate:
        half = len(result.rebalances) // 2
        train = mo.run(conn, lookback=args.lookback, hold=args.hold,
                       min_turnover=args.min_turnover, slippage=args.slippage,
                       market=args.market, end=half)
        test = mo.run(conn, lookback=args.lookback, hold=args.hold,
                      min_turnover=args.min_turnover, slippage=args.slippage,
                      market=args.market, start=half)
        print("\nالتحقق خارج العيّنة")
        print("─" * 60)
        print(f"  تدريب : فارق {train.excess_pct:+.1f}% على {train.periods} فترة")
        print(f"  تحقق  : فارق {test.excess_pct:+.1f}% على {test.periods} فترة")
        survived = train.excess_pct > 0 and test.excess_pct > 0
        print(f"  الحكم : {'صمد' if survived else 'سقط'}")

    print("\n⚠ الكون يضم الشركات المدرجة اليوم فقط. الشركات المشطوبة غائبة،")
    print("  وغيابها يجمّل النتيجة. اخصم ذلك من أي رقم أعلاه.")
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
    p.add_argument("--min-trades", type=int, default=30, dest="min_trades")
    p.add_argument("--train-fraction", type=float, default=0.6,
                   dest="train_fraction",
                   help="نسبة الصفقات المستخدمة للتدريب، والباقي للتحقق")
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

    p = sub.add_parser("universe-discover", help="اكتشف رموز تداول من ياهو")
    p.add_argument("--symbols", nargs="*", default=None)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("fetch", help="حمّل الشموع التاريخية من ياهو")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--interval", default="1day")
    p.add_argument("--limit", type=int, default=2600)
    p.add_argument("--benchmark", default="TASI")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("momentum", help="الزخم المقطعي: اختبار أو ترتيب حالي")
    p.add_argument("--lookback", type=int, default=mo.DEFAULT_LOOKBACK)
    p.add_argument("--hold", type=int, default=mo.DEFAULT_HOLD)
    p.add_argument("--min-turnover", type=float, default=mo.DEFAULT_MIN_TURNOVER,
                   dest="min_turnover")
    p.add_argument("--slippage", type=float, default=mo.DEFAULT_SLIPPAGE)
    p.add_argument("--market", default="MAIN")
    p.add_argument("--picks", action="store_true",
                   help="اعرض الترتيب الحالي بدل الاختبار التاريخي")
    p.add_argument("--by-year", action="store_true", dest="by_year")
    p.add_argument("--validate", action="store_true",
                   help="قسّم زمنياً وتحقق خارج العيّنة")
    p.set_defaults(func=cmd_momentum)

    p = sub.add_parser("forward", help="اختبار تقدّمي بمحفظة: نسبة الخطأ والخسارة")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--market", default="MAIN")
    p.add_argument("--shariah", nargs="*", default=None)
    p.add_argument("--limit-symbols", type=int, default=None, dest="limit_symbols")
    p.add_argument("--capital", type=float, default=100_000.0)
    p.add_argument("--benchmark", default="TASI")
    p.add_argument("--max-hold", type=int, default=15, dest="max_hold")
    p.add_argument("--min-score", type=float, default=50.0, dest="min_score")
    p.add_argument("--max-open", type=int, default=5, dest="max_open")
    p.add_argument("--commission", type=float, default=0.155)
    p.add_argument("--risk", type=float, default=1.0,
                   help="نسبة المخاطرة لكل صفقة بالمئة")
    p.add_argument("--allow-uncalibrated", action="store_true",
                   dest="allow_uncalibrated")
    p.add_argument("--by-setup", action="store_true", dest="by_setup")
    p.set_defaults(func=cmd_forward)

    p = sub.add_parser("sweep", help="مسح معاملات الأنماط مع تحقق خارج العيّنة")
    p.add_argument("--setups", nargs="*", default=None)
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--market", default=None, help="MAIN أو NOMU")
    p.add_argument("--limit-symbols", type=int, default=None, dest="limit_symbols")
    p.add_argument("--benchmark", default="TASI")
    p.add_argument("--max-bars", type=int, default=15, dest="max_bars")
    p.add_argument("--min-trades", type=int, default=30, dest="min_trades")
    p.add_argument("--train-fraction", type=float, default=0.6, dest="train_fraction")
    p.add_argument("--top", type=int, default=8)
    p.set_defaults(func=cmd_sweep)

    for name, helptext, func in (
        ("preflight", "فحص جاهزية النظام قبل الجلسة", cmd_preflight),
        ("scan", "دورة تحليل واحدة مع الاقتراحات", cmd_scan),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--capital", type=float, default=100_000.0)
        p.add_argument("--benchmark", default="TASI")
        p.add_argument("--allow", nargs="*", default=None,
                       help="التصنيفات الشرعية المسموحة، مثل: --allow PURE MIXED")
        p.add_argument("--min-score", type=float, default=50.0, dest="min_score")
        p.add_argument("--allow-uncalibrated", action="store_true",
                       dest="allow_uncalibrated",
                       help="اسمح بأنماط غير معايرة (للتجربة فقط)")
        p.add_argument("--no-telegram", action="store_true", dest="no_telegram")
        if name == "preflight":
            p.add_argument("--realtime", action="store_true",
                           help="اشترط مزوّداً لحظياً")
        else:
            p.add_argument("--symbols", nargs="*", default=None)
            p.add_argument("--emit", action="store_true",
                           help="سجّل الإشارات وأرسل التنبيهات")
            p.add_argument("--force", action="store_true")
        p.set_defaults(func=func)

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
