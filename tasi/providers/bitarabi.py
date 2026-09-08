"""
tasi/providers/bitarabi.py
===========================
استيراد التصنيف الشرعي للأسهم الأمريكية من بيت العربي.

**لماذا هذا المصدر تحديداً:** المصدران اللذان طلبهما المستخدم أولاً - يقين
وسهمك - غير قابلين للوصول الآلي من هذه البيئة: يقين تطبيق صفحة واحدة يحتاج
تنفيذ JavaScript وتعذّر تشغيل متصفح عبر الوكيل الشبكي المفروض على الجلسة،
وسهمك محمي بـ Cloudflare (403). بيت العربي مُقدَّم بديلاً مكافئاً، مع تحقق
فعلي من منهجيته قبل استيراد أي حكم منه - انظر أدناه.

**المنهجية المفصح عنها في الموقع نفسه (صفحة /stock/authority):**
    - معيار الراجحي: دين/قيمة سوقية وأصول ربوية/قيمة سوقية، حدهما 30%
    - "نحدّد الحكم الشرعي آلياً... دون مراجعة بشرية" - نص حرفي من الموقع
    - يُطبَّق فحصان من ثلاثة في معيار AAOIFI فقط. الفحص الثالث (الإيرادات
      المحرَّمة دون 5%) غير مطبَّق لغياب بيانات إيرادات القطاعات الفرعية
    - الحرمة القطاعية (بنوك، تأمين تقليدي...) تُحسم بالنشاط لا بالنِسب

**تناقض اكتُشف ووُضع له حارس:** لبعض الشركات (تويوتا مثالاً) يعرض الموقع
"رأياً نهائياً" في نص عام، بينما يعترف في الصفحة نفسها أنه "لم يُصدر حكماً
شرعياً لهذا السهم بعد" لغياب بيانات موثقة. هذا التناقض غير مقبول، فيُستبعد:
لا يُستورد أي حكم إلا إذا ظهر مدعوماً بأحد أساسين ظاهرين في صفحته نفسها:
    1. جدول نِسب فعلي محسوب (حالة halal و mix عادة)
    2. تعليل نشاطي صريح يذكر القطاع/الصناعة (حالة haram عادة)

**القيد الذي يبقى بعد التحقق:** الفحص آلي بالكامل دون مراجعة عالم شرعي
لكل شركة على حدة، ويغطي فحصين من ثلاثة في معيار AAOIFI فقط. هذا لا يُصلحه
التحقق أعلاه - إنه قيد بنيوي في المصدر نفسه، ويجب أن يبقى مذكوراً مع كل
حكم يُستورد منه. المستخدم حر أن يعتبر هذا كافياً أو أن يطلب مصدراً بمراجعة
بشرية كاملة.
"""

from __future__ import annotations

import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

from .. import universe as u

BASE = "https://bitarabi.com/stock/all"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}

SOURCE_NAME = "بيت العربي - معيار الراجحي (فحص آلي، AAOIFI جزئي)"

STATUS_MAP = {"halal": u.PURE, "mix": u.MIXED, "haram": u.NON_COMPLIANT}

_CARD_RE = re.compile(
    r'<a href="(https://bitarabi\.com/stock/[^"]+)" class="sp-card".*?'
    r'sp-card__badge sp-card__badge--(\w+)"[^>]*>([^<]+)</span>.*?'
    r'<h3 class="sp-card__sym">([^<]+)</h3>\s*<span class="sp-card__name">([^<]*)</span>',
    re.S)
_DEBT_RE = re.compile(
    r"نسبة الديون إلى القيمة السوقية.*?sv2-sr-num\">([\d.]+)%", re.S)
_ACTIVITY_RE = re.compile(r"شركة .{0,40}تنتمي إلى قطاع")


@dataclass
class ScreenResult:
    symbol: str
    name: str
    badge: str                    # halal / mix / haram
    trustworthy: bool
    debt_ratio: Optional[float] = None


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _fetch_page(session: requests.Session, page: int) -> List[Tuple[str, ...]]:
    response = session.get(BASE, params={"page": page}, timeout=25)
    return _CARD_RE.findall(response.text)


def _verify(session: requests.Session,
           card: Tuple[str, str, str, str, str]) -> ScreenResult:
    url, badge, _badge_txt, symbol, name = card
    try:
        html = session.get(url, timeout=25).text
    except requests.RequestException:
        return ScreenResult(symbol, name, badge, False)

    has_ratio_table = "sv2-sharia-ratios" in html and "مقيسة لا مقدَّرة" in html
    has_activity_reason = bool(_ACTIVITY_RE.search(html))
    has_disclaimer = "لم نُصدر حكماً شرعياً" in html
    trustworthy = (has_ratio_table or has_activity_reason) and not has_disclaimer

    debt_ratio = None
    match = _DEBT_RE.search(html)
    if match:
        debt_ratio = float(match.group(1))
    return ScreenResult(symbol, name, badge, trustworthy, debt_ratio)


def screen_all(pages: int = 21, workers: int = 6,
              on_progress=None) -> List[ScreenResult]:
    """اجلب كل الأسهم المصنَّفة وتحقق من كل حكم قبل إرجاعه.

    يُرجع فقط النتائج ``trustworthy`` - الأحكام المدعومة بجدول نِسب فعلي
    أو تعليل نشاطي صريح. الأحكام العامة المتناقضة (كحالة تويوتا) مستبعدة.
    """
    session = _session()

    all_cards: List[Tuple[str, ...]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cards in pool.map(lambda p: _fetch_page(session, p), range(1, pages + 1)):
            all_cards += cards

    definitive = [c for c in all_cards if c[1] != "unknown"]

    results: List[ScreenResult] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, result in enumerate(
                pool.map(lambda c: _verify(session, c), definitive), 1):
            results.append(result)
            if on_progress and i % 100 == 0:
                on_progress(i, len(definitive))

    return [r for r in results if r.trustworthy]


def import_into(conn: sqlite3.Connection, results: List[ScreenResult],
               as_of: Optional[str] = None) -> Dict[str, int]:
    """استورد النتائج الموثّقة إلى قاعدة البيانات."""
    as_of = as_of or time.strftime("%Y-%m-%d")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    counts = {u.PURE: 0, u.MIXED: 0, u.NON_COMPLIANT: 0}

    for result in results:
        status = STATUS_MAP[result.badge]
        # أنشئ الشركة إن لم تكن موجودة - بيت العربي قد يغطي رموزاً خارج
        # قائمة الكون المستوردة مسبقاً، فلا يفشل قيد المفتاح الأجنبي
        conn.execute(
            "INSERT OR IGNORE INTO companies (symbol, name_en, market, updated_at)"
            " VALUES (?, ?, 'US', ?)", (result.symbol, result.name, now))
        notes = (f"دين/قيمة سوقية {result.debt_ratio:.1f}%"
                if result.debt_ratio is not None else "")
        u.set_shariah_status(conn, result.symbol, status, SOURCE_NAME, as_of,
                             purification_rate=None, notes=notes)
        counts[status] += 1

    conn.commit()
    return counts
