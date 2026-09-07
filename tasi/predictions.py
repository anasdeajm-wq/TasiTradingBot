"""
tasi/predictions.py
===================
التوقعات والتعلّم من الخطأ - prediction logging and self-evaluation.

هذا الملف هو ما يجعل عبارة "تتعلم من نفسها" قابلة للقياس بدل أن تكون
تجميلاً. الفكرة بسيطة وصارمة:

    1. كل توقع يُسجَّل **قبل** أن تُعرف النتيجة، ومعه كامل الخصائص
       وحالة السوق التي بُني عليها. توقع يُكتب بعد الحدث لا قيمة له.
    2. عند حلول أجله يُقيَّم آلياً بالسعر الفعلي.
    3. عند الخطأ يُصنَّف السبب، لأن "أخطأنا" وحدها لا تُصلح شيئاً.
    4. تُقاس **المعايرة** لا نسبة الإصابة فقط: حين يقول النظام ثقة 70%
       هل يصيب فعلاً في 70% من الحالات؟ نظام واثق أكثر مما يستحق أخطر
       من نظام قليل الإصابة يعرف قدره.

مقياس Brier هو المستخدم للمعايرة: كلما اقترب من الصفر كانت الثقة أصدق.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

UP = "UP"
DOWN = "DOWN"
FLAT = "FLAT"

# آفاق التوقع المدعومة وعدد جلسات كل أفق
HORIZONS = {"NEXT_SESSION": 1, "1W": 5, "1M": 22}

# تصنيفات سبب الخطأ
NEWS_SHOCK = "NEWS_SHOCK"           # خبر أو إفصاح لم يكن في الحسبان
REGIME_SHIFT = "REGIME_SHIFT"       # تغيّرت حالة السوق بعد التوقع
LOW_LIQUIDITY = "LOW_LIQUIDITY"     # سيولة ضعيفة جعلت الحركة عشوائية
MODEL_BIAS = "MODEL_BIAS"           # انحياز متكرر في اتجاه واحد
OVERCONFIDENCE = "OVERCONFIDENCE"   # ثقة عالية مع خطأ كبير
NORMAL_VARIANCE = "NORMAL_VARIANCE" # خطأ ضمن التذبذب الطبيعي

FAILURE_AR = {
    NEWS_SHOCK: "صدمة خبرية",
    REGIME_SHIFT: "تغيّر حالة السوق",
    LOW_LIQUIDITY: "سيولة ضعيفة",
    MODEL_BIAS: "انحياز في النموذج",
    OVERCONFIDENCE: "ثقة زائدة",
    NORMAL_VARIANCE: "تذبذب طبيعي",
}

# عتبة اعتبار الحركة "ثابتة" - أقل من هذا لا يُعد صعوداً ولا هبوطاً
FLAT_THRESHOLD_PCT = 0.3


@dataclass
class Prediction:
    scope: str                      # MARKET / SECTOR / SYMBOL
    target: str                     # TASI أو رمز السهم
    horizon: str
    direction: str
    predicted_pct: Optional[float] = None
    low_band: Optional[float] = None
    high_band: Optional[float] = None
    confidence: float = 0.5
    regime: Optional[str] = None
    features: Dict[str, object] = field(default_factory=dict)
    rationale_ar: str = ""
    made_at: str = ""
    resolve_at: str = ""
    id: Optional[int] = None


def direction_of(change_pct: float,
                 threshold: float = FLAT_THRESHOLD_PCT) -> str:
    if change_pct > threshold:
        return UP
    if change_pct < -threshold:
        return DOWN
    return FLAT


# ---------------------------------------------------------------------------
def log_prediction(conn: sqlite3.Connection, pred: Prediction) -> int:
    """سجّل توقعاً. يجب أن يُستدعى قبل معرفة النتيجة."""
    if pred.horizon not in HORIZONS:
        raise ValueError(f"أفق غير معروف: {pred.horizon}")
    if not 0.0 <= pred.confidence <= 1.0:
        raise ValueError("الثقة يجب أن تكون بين 0 و 1")

    made_at = pred.made_at or datetime.now().isoformat(timespec="seconds")
    resolve_at = pred.resolve_at or (
        date.fromisoformat(made_at[:10]) + timedelta(days=HORIZONS[pred.horizon] * 2)
    ).isoformat()

    cur = conn.execute(
        """
        INSERT INTO predictions
            (scope, target, horizon, direction, predicted_pct, low_band,
             high_band, confidence, regime, features, rationale_ar,
             made_at, resolve_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pred.scope, pred.target, pred.horizon, pred.direction,
         pred.predicted_pct, pred.low_band, pred.high_band, pred.confidence,
         pred.regime, json.dumps(pred.features, ensure_ascii=False),
         pred.rationale_ar, made_at, resolve_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def classify_failure(
    predicted_pct: Optional[float],
    actual_pct: float,
    confidence: float,
    regime_at_prediction: Optional[str],
    regime_at_resolution: Optional[str],
    volume_ratio: Optional[float] = None,
    had_news: bool = False,
) -> Tuple[str, str]:
    """صنّف سبب فشل التوقع. يُرجع (التصنيف، شرح عربي).

    الترتيب مقصود: الأسباب الخارجية تُفحص قبل إلقاء اللوم على النموذج،
    والعكس يجعل كل خطأ يبدو خطأ نموذج فلا يتحسن شيء.
    """
    error = abs((predicted_pct or 0.0) - actual_pct)

    if had_news:
        return NEWS_SHOCK, f"خبر أو إفصاح غيّر المسار (خطأ {error:.2f} نقطة)"

    if (regime_at_prediction and regime_at_resolution
            and regime_at_prediction != regime_at_resolution):
        return REGIME_SHIFT, (
            f"تغيّرت حالة السوق من {regime_at_prediction} إلى "
            f"{regime_at_resolution} بعد التوقع")

    if volume_ratio is not None and volume_ratio < 0.5:
        return LOW_LIQUIDITY, (
            f"سيولة ضعيفة ({volume_ratio:.2f}x المتوسط) جعلت الحركة غير موثوقة")

    if confidence >= 0.7 and error > 2.0:
        return OVERCONFIDENCE, (
            f"ثقة {confidence:.0%} مع خطأ {error:.2f} نقطة - الثقة كانت أعلى مما تستحق")

    if error <= 1.0:
        return NORMAL_VARIANCE, f"خطأ صغير ({error:.2f} نقطة) ضمن التذبذب الطبيعي"

    return MODEL_BIAS, f"خطأ {error:.2f} نقطة بلا سبب خارجي واضح"


def resolve_prediction(
    conn: sqlite3.Connection,
    prediction_id: int,
    actual_pct: float,
    regime_at_resolution: Optional[str] = None,
    volume_ratio: Optional[float] = None,
    had_news: bool = False,
) -> Dict[str, object]:
    """قيّم توقعاً بالنتيجة الفعلية، وصنّف سبب الخطأ إن أخطأ."""
    row = conn.execute(
        "SELECT * FROM predictions WHERE id = ?", (prediction_id,)).fetchone()
    if not row:
        raise ValueError(f"لا يوجد توقع بالرقم {prediction_id}")

    actual_direction = direction_of(actual_pct)
    correct = int(actual_direction == row["direction"])
    error_abs = abs((row["predicted_pct"] or 0.0) - actual_pct)

    within_band = None
    if row["low_band"] is not None and row["high_band"] is not None:
        within_band = int(row["low_band"] <= actual_pct <= row["high_band"])

    cause = None
    notes = None
    if not correct:
        cause, notes = classify_failure(
            row["predicted_pct"], actual_pct, row["confidence"],
            row["regime"], regime_at_resolution, volume_ratio, had_news)

    conn.execute(
        """
        INSERT INTO prediction_results
            (prediction_id, actual_pct, actual_direction, correct, error_abs,
             within_band, resolved_at, failure_cause, failure_notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(prediction_id) DO UPDATE SET
            actual_pct = excluded.actual_pct,
            actual_direction = excluded.actual_direction,
            correct = excluded.correct, error_abs = excluded.error_abs,
            within_band = excluded.within_band,
            resolved_at = excluded.resolved_at,
            failure_cause = excluded.failure_cause,
            failure_notes = excluded.failure_notes
        """,
        (prediction_id, actual_pct, actual_direction, correct, error_abs,
         within_band, datetime.now().isoformat(timespec="seconds"), cause, notes),
    )
    conn.commit()

    return {
        "prediction_id": prediction_id,
        "predicted": row["direction"],
        "actual": actual_direction,
        "correct": bool(correct),
        "error_abs": round(error_abs, 3),
        "within_band": within_band,
        "failure_cause": cause,
        "failure_cause_ar": FAILURE_AR.get(cause) if cause else None,
        "notes": notes,
    }


def due_predictions(conn: sqlite3.Connection,
                    as_of: Optional[str] = None) -> List[sqlite3.Row]:
    """التوقعات التي حان أجلها ولم تُقيَّم بعد."""
    as_of = as_of or date.today().isoformat()
    return conn.execute(
        """
        SELECT p.* FROM predictions p
        LEFT JOIN prediction_results r ON r.prediction_id = p.id
        WHERE r.prediction_id IS NULL AND p.resolve_at <= ?
        ORDER BY p.resolve_at
        """,
        (as_of,),
    ).fetchall()


# ---------------------------------------------------------------------------
def accuracy_report(conn: sqlite3.Connection, scope: Optional[str] = None,
                    horizon: Optional[str] = None) -> Dict[str, object]:
    """تقرير الدقة والمعايرة على التوقعات المقيَّمة."""
    sql = """
        SELECT p.direction, p.confidence, p.regime, p.horizon, p.scope,
               r.correct, r.error_abs, r.failure_cause
          FROM predictions p JOIN prediction_results r ON r.prediction_id = p.id
         WHERE 1=1
    """
    params: List[object] = []
    if scope:
        sql += " AND p.scope = ?"
        params.append(scope)
    if horizon:
        sql += " AND p.horizon = ?"
        params.append(horizon)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return {"resolved": 0, "note": "لا توجد توقعات مقيَّمة بعد."}

    total = len(rows)
    correct = sum(r["correct"] for r in rows)

    # Brier score: متوسط مربع الفرق بين الثقة والنتيجة الفعلية (0 أو 1).
    brier = sum((r["confidence"] - r["correct"]) ** 2 for r in rows) / total

    # جدول المعايرة: هل الثقة المعلنة تطابق الإصابة الفعلية؟
    buckets: Dict[str, List[sqlite3.Row]] = {}
    for r in rows:
        key = f"{int(r['confidence'] * 10) * 10}-{int(r['confidence'] * 10) * 10 + 9}%"
        buckets.setdefault(key, []).append(r)

    calibration = []
    for key in sorted(buckets):
        group = buckets[key]
        calibration.append({
            "نطاق الثقة": key,
            "عدد": len(group),
            "الإصابة الفعلية": f"{sum(g['correct'] for g in group) / len(group):.0%}",
            "متوسط الثقة المعلنة": f"{sum(g['confidence'] for g in group) / len(group):.0%}",
        })

    causes: Dict[str, int] = {}
    for r in rows:
        if r["failure_cause"]:
            causes[r["failure_cause"]] = causes.get(r["failure_cause"], 0) + 1

    by_regime: Dict[str, Dict[str, object]] = {}
    for r in rows:
        key = r["regime"] or "UNKNOWN"
        entry = by_regime.setdefault(key, {"عدد": 0, "صحيح": 0})
        entry["عدد"] += 1
        entry["صحيح"] += r["correct"]
    for key, entry in by_regime.items():
        entry["الإصابة"] = f"{entry['صحيح'] / entry['عدد']:.0%}"

    return {
        "resolved": total,
        "hit_rate": round(correct / total, 3),
        "brier_score": round(brier, 4),
        "brier_note": ("أقل ما يمكن 0 وأسوأ 1. أعلى من 0.25 يعني الثقة "
                       "المعلنة أسوأ من التخمين العشوائي."),
        "mean_abs_error": round(sum(r["error_abs"] or 0 for r in rows) / total, 3),
        "calibration": calibration,
        "failure_causes": {FAILURE_AR.get(k, k): v for k, v in causes.items()},
        "by_regime": by_regime,
    }


def lessons(conn: sqlite3.Connection, limit: int = 5) -> List[str]:
    """استخرج دروساً عملية من أنماط الفشل المتكررة.

    ليست نصائح عامة: كل درس مبني على عدّ فعلي في قاعدة البيانات.
    """
    report = accuracy_report(conn)
    if report.get("resolved", 0) < 10:
        return ["عدد التوقعات المقيَّمة قليل جداً لاستخراج دروس موثوقة."]

    out: List[str] = []

    if report["brier_score"] > 0.25:
        out.append(
            f"الثقة المعلنة غير معايرة (Brier {report['brier_score']:.3f}). "
            "يجب خفض الثقة المعلنة حتى تطابق الإصابة الفعلية.")

    for row in report["calibration"]:
        declared = int(row["متوسط الثقة المعلنة"].rstrip("%"))
        actual = int(row["الإصابة الفعلية"].rstrip("%"))
        if row["عدد"] >= 10 and declared - actual >= 15:
            out.append(
                f"في نطاق الثقة {row['نطاق الثقة']}: الثقة المعلنة {declared}% "
                f"والإصابة الفعلية {actual}% على {row['عدد']} توقعاً. ثقة زائدة.")

    causes = report["failure_causes"]
    if causes:
        top_cause, count = max(causes.items(), key=lambda kv: kv[1])
        share = count / report["resolved"]
        if share >= 0.3:
            out.append(
                f"السبب الغالب للخطأ هو {top_cause} ({count} مرة، "
                f"{share:.0%} من التوقعات المقيَّمة).")

    for regime, entry in report["by_regime"].items():
        if entry["عدد"] >= 10:
            rate = entry["صحيح"] / entry["عدد"]
            if rate < 0.4:
                out.append(
                    f"الإصابة في حالة {regime} منخفضة ({rate:.0%} على "
                    f"{entry['عدد']} توقعاً). يُفضّل تقليل التوقعات في هذه الحالة.")

    return out[:limit] or ["لا توجد أنماط فشل بارزة في البيانات الحالية."]
