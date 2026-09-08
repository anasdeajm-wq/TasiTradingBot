"""
tasi/providers/argaam_shariah.py
==================================
استيراد التصنيف الشرعي السعودي (تاسي + نمو) من أرقام (Argaam) - معيار
د. محمد بن سعود العصيمي، بالإضافة إلى قائمتي الراجحي المالية والبلاد
المالية كتوثيق توافقي.

**لماذا هذا المصدر ولماذا الآن:** طلب المستخدم منذ بداية هذا المشروع تحديداً
معيار العصيمي، وظل هذا مستحيلاً لغياب أي ملف رسمي من مركز المقاصد. أرقام -
منصة بيانات مالية خليجية معروفة، وليست أنا مصدر الحكم - تنشر صفحة مقارنة
حيّة تُحدَّث دورياً تعرض تصنيف ثلاث جهات معاً (الراجحي المالية، د. العصيمي،
البلاد المالية) لكل سهم في تاسي ونمو، مع رابط تفصيلي لكل جهة على حدة.

**لماذا العصيمي تحديداً يُستورد بحالة (نقي/مختلط) وليس الآخرَين:**
عرض أرقام التفصيلي لكل جهة يختلف: صفحة العصيمي (institution=2) تنشر لكل
سهم **مبلغ تطهير فعلي بالريال للسهم الواحد** بالإضافة لتصنيف نصّي ("نقية").
صفحتا الراجحي والبلاد (institution=1 و6) تعرضان قائمة "متوافق" فقط دون أي
تفصيل كمّي - عمود التصنيف فيهما يظهر "-" لكل سهم. لا يمكن اشتقاق نقي/مختلط
من "-"، فلا نحاول: قائمتا الراجحي والبلاد تُستخدمان هنا **توثيقاً توافقياً
فقط** (يُذكر في الملاحظات أن سهماً معيّناً مؤكَّد أيضاً منهما)، لا مصدراً
لحالة شرعية بذاتها.

**نقطة دقيقة في تسمية العصيمي نفسه:** كل الأسهم في قائمته تحمل تصنيف "نقية"
نصّياً حتى لو كان مبلغ التطهير > 0 (بعض النشاط المباح يحمل دخلاً عرضياً
غير شرعي كفوائد ودائع). "نقية" عند العصيمي أوسع من "نقي" في مخطط هذا النظام
(PURE يعني صفر تطهير هنا). لذلك تُحسب الحالة هنا من **الرقم الفعلي** (مبلغ
التطهير) لا من التسمية النصية: صفر = PURE، أكبر من صفر = MIXED. التسمية
الأصلية تُحفَظ حرفياً في `notes` حتى لا يضيع ما قاله المصدر فعلاً.

**ما لا يزال ناقصاً:** لا هذا ولا أي مصدر آلي يُغني عن مراجعة عالم شرعي
مباشرة لكل قرار استثمار حقيقي. هذا تصنيف مصدر خارجي مؤرَّخ، لا فتوى من هذا
النظام - وهذا الفارق ثابت في كل مصدر شرعي مستخدم هنا.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

import requests

from .. import universe as u

BASE = "https://www.argaam.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}

MARKET_IDS = {"MAIN": 3, "NOMU": 14}          # تاسي / نمو - حسب صفحة أرقام
INSTITUTION_IDS = {"osaimi": 2, "alrajhi": 1, "albilad": 6}
INSTITUTION_NAMES_AR = {
    "osaimi": "د. محمد بن سعود العصيمي",
    "alrajhi": "الراجحي المالية",
    "albilad": "البلاد المالية",
}

SOURCE_NAME = "أرقام (Argaam) - معيار د. محمد بن سعود العصيمي"

_ROW_RE = re.compile(
    r'<td class="">(\d+)</td>\s*'
    r'<td class="argaam-font"><a[^>]*>([^<]+)</a></td>\s*'
    r'<td class="center">([^<]*)</td>\s*'
    r'<td class="center">([^<]*)</td>',
    re.S)
_LAST_UPDATED_RE = re.compile(
    r'id="LastUpdated" name="LastUpdated" value="([^"]*)"')


@dataclass
class OsaimiResult:
    symbol: str
    name: str
    market: str                # MAIN / NOMU
    purification_amount: float
    source_label: str          # كما ظهر حرفياً في أرقام، مثلاً "نقية"


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _fetch_institution_page(session: requests.Session, institution: str,
                            market: str) -> str:
    url = f"{BASE}/ar/company/shariahcompaniesbyinstitution/{INSTITUTION_IDS[institution]}"
    response = session.get(url, params={"marketid": MARKET_IDS[market]}, timeout=25)
    response.raise_for_status()
    return response.text


def last_updated(html: str) -> Optional[str]:
    match = _LAST_UPDATED_RE.search(html)
    return match.group(1) if match and match.group(1) else None


def screen_osaimi(markets: Sequence[str] = ("MAIN", "NOMU"),
                  session: Optional[requests.Session] = None
                  ) -> List[OsaimiResult]:
    """اجلب قائمة العصيمي التفصيلية (مبلغ تطهير فعلي لكل سهم) من أرقام."""
    session = session or _session()
    results: List[OsaimiResult] = []
    for market in markets:
        html = _fetch_institution_page(session, "osaimi", market)
        for symbol, name, amount_txt, label in _ROW_RE.findall(html):
            amount_txt = amount_txt.strip()
            try:
                amount = float(amount_txt)
            except ValueError:
                continue  # "-" أو قيمة غير رقمية: بيانات غير كافية، يُستبعد
            results.append(OsaimiResult(
                symbol=symbol.strip(), name=name.strip(), market=market,
                purification_amount=amount, source_label=label.strip()))
    return results


def fetch_compliant_symbols(institution: str,
                            markets: Sequence[str] = ("MAIN", "NOMU"),
                            session: Optional[requests.Session] = None
                            ) -> Set[str]:
    """رموز "متوافقة" حسب مؤسسة أخرى (الراجحي/البلاد) دون تفصيل نقي/مختلط.

    تُستخدم فقط للتوثيق التوافقي في `import_into` - لا تُبنى منها حالة
    شرعية مستقلة لأن المصدر نفسه لا يفصح عن الدرجة (عمود التصنيف "-").
    """
    session = session or _session()
    symbols: Set[str] = set()
    for market in markets:
        html = _fetch_institution_page(session, institution, market)
        for symbol, _name, _amount, _label in _ROW_RE.findall(html):
            symbols.add(symbol.strip())
    return symbols


def import_into(conn: sqlite3.Connection, results: List[OsaimiResult],
                as_of: Optional[str] = None,
                corroboration: Optional[Dict[str, Set[str]]] = None
                ) -> Dict[str, int]:
    """استورد نتائج العصيمي، مع ملاحظة تأكيد الراجحي/البلاد إن توفرت.

    ``corroboration``: مثلاً ``{"alrajhi": {"1120", ...}, "albilad": {...}}``
    - رموز مؤكَّدة "متوافقة" من تلك الجهة، تُضاف كملاحظة نصية لا كحالة.
    """
    as_of = as_of or time.strftime("%Y-%m-%d")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    corroboration = corroboration or {}
    counts = {u.PURE: 0, u.MIXED: 0}

    for r in results:
        status = u.PURE if r.purification_amount == 0 else u.MIXED

        confirmed_by = [INSTITUTION_NAMES_AR[inst] for inst, symbols in
                        corroboration.items() if r.symbol in symbols]
        notes = (f"تصنيف المصدر الحرفي: {r.source_label} - "
                f"مبلغ التطهير للسهم: {r.purification_amount:.4f} ريال")
        if confirmed_by:
            notes += " - مؤكَّد أيضاً (توافق دون تفصيل) من: " + "، ".join(confirmed_by)

        # أنشئ الشركة إن لم تكن موجودة - لا يفشل قيد المفتاح الأجنبي
        conn.execute(
            "INSERT OR IGNORE INTO companies (symbol, name_ar, market, updated_at)"
            " VALUES (?, ?, ?, ?)", (r.symbol, r.name, r.market, now))
        u.set_shariah_status(conn, r.symbol, status, SOURCE_NAME, as_of,
                             purification_rate=r.purification_amount, notes=notes)
        counts[status] += 1

    conn.commit()
    return counts
