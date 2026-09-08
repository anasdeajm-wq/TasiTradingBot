"""
tasi/fundamentals.py
====================
البيانات المالية ومرشّح الجودة - separating a rising price from a sound company.

السبب المباشر لوجود هذا الملف: أعلى سهم في ترتيب الزخم عندنا كان شركة
تخسر مليارات وربحها الإجمالي سالب وديونها ضعف حقوق ملكيتها. السعر يصعد
والشركة تنزف. الزخم وحده لا يرى الفرق، والقوائم المالية تراه.

المقاييس المحسوبة، وكلها نِسب لا أرقام مطلقة حتى تكون قابلة للمقارنة
بين شركة رأسمالها مليار وأخرى رأسمالها تريليون:

    الربحية   : هامش صافي، هامش تشغيلي، العائد على حقوق الملكية
    المتانة   : الدين إلى حقوق الملكية، الدين إلى الأصول، تغطية الفوائد
    السيولة   : النسبة الجارية
    النقد     : التدفق النقدي الحر إلى الإيرادات
    النمو     : نمو الإيرادات وصافي الدخل

**تنبيه شرعي مهم:** بعض هذه النسب (الدين إلى الأصول مثلاً) تُستخدم في
معايير الفرز الشرعي، لكن حسابها هنا **ليس حكماً شرعياً**. المعايير
تختلف في المقام المستخدم (أصول أم قيمة سوقية) وفي العتبات وفي معالجة
الاستثناءات. النِّسب معروضة لتقارنها بمصدرك أنت، لا لتحلّ محله.
"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional, Sequence

import requests

BASE = "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
}

FIELDS = [
    "annualTotalRevenue", "annualNetIncome", "annualOperatingIncome",
    "annualGrossProfit", "annualTotalDebt", "annualStockholdersEquity",
    "annualTotalAssets", "annualCashAndCashEquivalents", "annualFreeCashFlow",
    "annualOperatingCashFlow", "annualEBITDA", "annualBasicEPS",
    "annualCurrentAssets", "annualCurrentLiabilities", "annualInterestExpense",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol      TEXT NOT NULL,
    as_of       TEXT NOT NULL,          -- تاريخ نهاية السنة المالية
    field       TEXT NOT NULL,
    value       REAL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of, field)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_fund_symbol ON fundamentals(symbol, as_of DESC);
"""


@dataclass
class QualityScore:
    """مقاييس جودة الشركة، كلها نِسب."""

    symbol: str
    as_of: str = ""
    net_margin: Optional[float] = None          # صافي الدخل / الإيرادات
    operating_margin: Optional[float] = None
    gross_margin: Optional[float] = None
    roe: Optional[float] = None                 # العائد على حقوق الملكية
    debt_to_equity: Optional[float] = None
    debt_to_assets: Optional[float] = None
    interest_coverage: Optional[float] = None   # التشغيلي / الفوائد
    current_ratio: Optional[float] = None
    fcf_margin: Optional[float] = None
    revenue_growth: Optional[float] = None
    income_growth: Optional[float] = None
    profitable: Optional[bool] = None
    years_of_data: int = 0

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    # ------------------------------------------------------------------
    def failures(self,
                 max_debt_to_equity: float = 2.0,
                 min_current_ratio: float = 0.8,
                 min_interest_coverage: float = 1.5) -> List[str]:
        """أسباب رفض الشركة، كل سبب برقمه. القائمة الفارغة تعني اجتيازاً."""
        reasons: List[str] = []
        if self.profitable is False:
            reasons.append(f"خسارة صافية (هامش {self.net_margin:.1%})"
                           if self.net_margin is not None else "خسارة صافية")
        if self.gross_margin is not None and self.gross_margin < 0:
            reasons.append(f"ربح إجمالي سالب ({self.gross_margin:.1%})")
        if (self.debt_to_equity is not None
                and self.debt_to_equity > max_debt_to_equity):
            reasons.append(f"دين/حقوق ملكية {self.debt_to_equity:.2f}"
                           f" فوق {max_debt_to_equity}")
        if (self.current_ratio is not None
                and self.current_ratio < min_current_ratio):
            reasons.append(f"نسبة جارية {self.current_ratio:.2f}"
                           f" دون {min_current_ratio}")
        if (self.interest_coverage is not None
                and self.interest_coverage < min_interest_coverage):
            reasons.append(f"تغطية فوائد {self.interest_coverage:.2f}"
                           f" دون {min_interest_coverage}")
        return reasons

    def passes(self, **thresholds: float) -> bool:
        return not self.failures(**thresholds)

    @property
    def has_enough_data(self) -> bool:
        """هل تكفي البيانات للحكم؟ الغياب ليس اجتيازاً."""
        return self.years_of_data >= 2 and self.net_margin is not None


# ---------------------------------------------------------------------------
def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def fetch_symbol(symbol: str, session: Optional[requests.Session] = None
                 ) -> Dict[str, Dict[str, float]]:
    """اجلب القوائم المالية السنوية. يُرجع {تاريخ: {حقل: قيمة}}."""
    from .providers.yahoo import to_yahoo

    session = session or requests.Session()
    session.headers.update(HEADERS)
    try:
        response = session.get(
            f"{BASE}/{to_yahoo(symbol)}",
            params={"symbol": to_yahoo(symbol), "type": ",".join(FIELDS),
                    "period1": "1420070400", "period2": "1790000000"},
            timeout=30)
        if response.status_code != 200:
            return {}
        blocks = (response.json().get("timeseries") or {}).get("result") or []
    except Exception:                                      # noqa: BLE001
        return {}

    out: Dict[str, Dict[str, float]] = {}
    for block in blocks:
        meta = block.get("meta", {})
        field = (meta.get("type") or [None])[0]
        if not field:
            continue
        for entry in block.get(field) or []:
            if not entry:
                continue
            as_of = entry.get("asOfDate")
            raw = (entry.get("reportedValue") or {}).get("raw")
            if as_of is None or raw is None:
                continue
            out.setdefault(as_of, {})[field.replace("annual", "")] = float(raw)
    return out


def store(conn: sqlite3.Connection, symbol: str,
          data: Dict[str, Dict[str, float]]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(symbol, as_of, field, value, now)
            for as_of, fields in data.items()
            for field, value in fields.items()]
    conn.executemany(
        "INSERT OR REPLACE INTO fundamentals (symbol, as_of, field, value,"
        " fetched_at) VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def fetch_many(conn: sqlite3.Connection, symbols: Sequence[str],
               workers: int = 6, on_progress=None) -> Dict[str, int]:
    """اجلب واحفظ لعدة رموز بالتوازي."""
    ensure_schema(conn)
    session = requests.Session()
    session.headers.update(HEADERS)
    results: Dict[str, int] = {}

    def grab(symbol: str):
        return symbol, fetch_symbol(symbol, session)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (symbol, data) in enumerate(pool.map(grab, symbols), 1):
            results[symbol] = store(conn, symbol, data) if data else 0
            if on_progress and i % 25 == 0:
                on_progress(i, len(symbols), sum(1 for v in results.values() if v))
    return results


def load(conn: sqlite3.Connection, symbol: str) -> Dict[str, Dict[str, float]]:
    rows = conn.execute(
        "SELECT as_of, field, value FROM fundamentals WHERE symbol = ?"
        " ORDER BY as_of", (symbol,)).fetchall()
    out: Dict[str, Dict[str, float]] = {}
    for row in rows:
        out.setdefault(row["as_of"], {})[row["field"]] = row["value"]
    return out


# ---------------------------------------------------------------------------
def _safe_ratio(numerator: Optional[float],
                denominator: Optional[float]) -> Optional[float]:
    """قسمة تُرجع None بدل الانفجار أو رقم بلا معنى عند مقام صفري أو سالب."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def score(conn: sqlite3.Connection, symbol: str,
          as_of: Optional[str] = None) -> QualityScore:
    """احسب مقاييس الجودة من أحدث سنة مالية متاحة.

    ``as_of`` يقيّد الحساب بالسنوات المنتهية عنده أو قبله، وهو ضروري
    للاختبار التاريخي حتى لا يُستخدم تقرير لم يكن قد صدر بعد.
    """
    data = load(conn, symbol)
    if as_of:
        data = {k: v for k, v in data.items() if k <= as_of}
    result = QualityScore(symbol=symbol, years_of_data=len(data))
    if not data:
        return result

    dates = sorted(data)
    latest = dates[-1]
    current = data[latest]
    result.as_of = latest

    revenue = current.get("TotalRevenue")
    net = current.get("NetIncome")
    equity = current.get("StockholdersEquity")

    result.net_margin = _safe_ratio(net, revenue)
    result.operating_margin = _safe_ratio(current.get("OperatingIncome"), revenue)
    result.gross_margin = _safe_ratio(current.get("GrossProfit"), revenue)
    result.roe = _safe_ratio(net, equity if (equity or 0) > 0 else None)
    result.debt_to_equity = _safe_ratio(
        current.get("TotalDebt"), equity if (equity or 0) > 0 else None)
    result.debt_to_assets = _safe_ratio(current.get("TotalDebt"),
                                        current.get("TotalAssets"))
    result.interest_coverage = _safe_ratio(
        current.get("OperatingIncome"),
        current.get("InterestExpense") if (current.get("InterestExpense") or 0) > 0
        else None)
    result.current_ratio = _safe_ratio(current.get("CurrentAssets"),
                                       current.get("CurrentLiabilities"))
    result.fcf_margin = _safe_ratio(current.get("FreeCashFlow"), revenue)
    if net is not None:
        result.profitable = net > 0

    if len(dates) >= 2:
        previous = data[dates[-2]]
        prior_revenue = previous.get("TotalRevenue")
        if prior_revenue and prior_revenue > 0 and revenue is not None:
            result.revenue_growth = (revenue - prior_revenue) / prior_revenue
        prior_net = previous.get("NetIncome")
        if prior_net and prior_net > 0 and net is not None:
            result.income_growth = (net - prior_net) / prior_net

    return result


def screen(conn: sqlite3.Connection, symbols: Sequence[str],
           require_data: bool = True, **thresholds: float
           ) -> Dict[str, List[str]]:
    """افحص مجموعة رموز. يُرجع {رمز: أسباب الرفض}؛ القائمة الفارغة اجتياز.

    ``require_data`` يرفض الشركة التي لا تكفي بياناتها. الغياب ليس
    اجتيازاً: شركة بلا قوائم منشورة ليست شركة سليمة بالضرورة.
    """
    out: Dict[str, List[str]] = {}
    for symbol in symbols:
        quality = score(conn, symbol)
        if not quality.has_enough_data:
            out[symbol] = (["بيانات مالية غير كافية"] if require_data else [])
            continue
        out[symbol] = quality.failures(**thresholds)
    return out
