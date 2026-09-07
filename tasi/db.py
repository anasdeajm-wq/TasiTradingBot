"""
tasi/db.py
==========
قاعدة البيانات المركزية - the single SQLite store for the whole system.

التصميم مبني على فصل أربع طبقات:
    1. الكون  (universe)      : الشركات، القطاعات، التصنيف الشرعي
    2. السوق  (market)        : الشموع، التيكات، دفتر الأوامر، الأخبار
    3. القرار (decisions)     : الإشارات، الأوامر، الصفقات، المراكز
    4. التعلّم (learning)     : التوقعات، نتائجها، أداء كل نمط

الطبقة الرابعة هي التي تجعل النظام يتحسّن: كل توقع يُسجَّل مع كامل
المتغيرات التي بُني عليها، ثم يُقيَّم لاحقاً بالنتيجة الفعلية. من هذا
السجل تُحسب نسبة الإصابة لكل نمط في كل حالة سوق.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

DEFAULT_DB = os.getenv("TASI_DB", "data/tasi.db")

SCHEMA = """
PRAGMA journal_mode = WAL;         -- قراءة متزامنة أثناء الكتابة
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- ===================================================================
-- 1. الكون - universe
-- ===================================================================
CREATE TABLE IF NOT EXISTS companies (
    symbol          TEXT PRIMARY KEY,      -- 2222
    name_ar         TEXT,
    name_en         TEXT,
    sector          TEXT,
    industry        TEXT,
    market          TEXT DEFAULT 'MAIN',   -- MAIN (الرئيسية) / NOMU (نمو)
    isin            TEXT,
    listed_shares   REAL,
    free_float      REAL,
    is_active       INTEGER DEFAULT 1,
    updated_at      TEXT
);

-- التصنيف الشرعي يُخزَّن مؤرّخاً لأنه يتغيّر كل ربع سنة.
-- الحالة الافتراضية UNVERIFIED: النظام لا يخمّن الحكم الشرعي أبداً.
CREATE TABLE IF NOT EXISTS shariah_status (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL REFERENCES companies(symbol),
    status      TEXT NOT NULL,             -- PURE (نقي) / MIXED (مختلط)
                                           -- / NON_COMPLIANT (محرّم) / UNVERIFIED
    source      TEXT NOT NULL,             -- الجهة المصنِّفة
    as_of       TEXT NOT NULL,             -- تاريخ سريان التصنيف
    purification_rate REAL,                -- نسبة التطهير إن وُجدت
    notes       TEXT,
    imported_at TEXT NOT NULL,
    UNIQUE(symbol, source, as_of)
);
CREATE INDEX IF NOT EXISTS idx_shariah_symbol ON shariah_status(symbol, as_of DESC);

-- ===================================================================
-- 2. السوق - market data
-- ===================================================================
CREATE TABLE IF NOT EXISTS bars (
    symbol   TEXT NOT NULL,
    interval TEXT NOT NULL,                -- 1min / 5min / 60min / 1day
    ts       TEXT NOT NULL,                -- ISO, توقيت الرياض
    open     REAL, high REAL, low REAL, close REAL NOT NULL,
    volume   REAL DEFAULT 0,
    turnover REAL,                         -- قيمة التداول بالريال
    trades   INTEGER,                      -- عدد الصفقات
    PRIMARY KEY (symbol, interval, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_bars_lookup ON bars(symbol, interval, ts DESC);

-- التيكات اللحظية أثناء الجلسة. تُلخَّص إلى شموع وتُقلَّم دورياً.
CREATE TABLE IF NOT EXISTS ticks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    ts        TEXT NOT NULL,
    price     REAL NOT NULL,
    volume    REAL,
    bid       REAL, ask REAL,
    bid_size  REAL, ask_size REAL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS index_bars (
    index_code TEXT NOT NULL,              -- TASI / NOMU / قطاعي
    interval   TEXT NOT NULL,
    ts         TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL NOT NULL,
    volume REAL, turnover REAL,
    PRIMARY KEY (index_code, interval, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS news (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT,                      -- NULL = خبر عام على السوق
    headline    TEXT NOT NULL,
    body        TEXT,
    source      TEXT,
    url         TEXT,
    published_at TEXT NOT NULL,
    sentiment   REAL,                      -- -1 سلبي .. +1 إيجابي
    category    TEXT,                      -- ARNINGS / DIVIDEND / MERGER ...
    ingested_at TEXT NOT NULL,
    UNIQUE(url, headline)
);
CREATE INDEX IF NOT EXISTS idx_news_symbol ON news(symbol, published_at DESC);

CREATE TABLE IF NOT EXISTS corporate_actions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT NOT NULL,
    action   TEXT NOT NULL,                -- DIVIDEND / SPLIT / RIGHTS / CAPITAL
    ex_date  TEXT,
    amount   REAL,
    ratio    TEXT,
    details  TEXT,
    UNIQUE(symbol, action, ex_date)
);

-- ===================================================================
-- 3. القرار - signals, orders, positions
-- ===================================================================
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    setup       TEXT NOT NULL,             -- اسم النمط الذي أطلق الإشارة
    action      TEXT NOT NULL,             -- BUY / SELL / SCALE_IN / EXIT
    price       REAL NOT NULL,
    score       REAL,                      -- درجة قوة الإشارة 0..100
    confidence  REAL,                      -- الثقة المقدّرة 0..1
    regime      TEXT,                      -- حالة السوق وقت الإشارة
    features    TEXT,                      -- JSON: كل المتغيرات المستخدمة
    reason_ar   TEXT,
    created_at  TEXT NOT NULL,
    delivered   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, created_at DESC);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   INTEGER REFERENCES signals(id),
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,             -- BUY / SELL
    order_type  TEXT DEFAULT 'LIMIT',
    quantity    INTEGER NOT NULL,
    limit_price REAL,
    status      TEXT DEFAULT 'PROPOSED',   -- PROPOSED / SENT / FILLED
                                           -- / PARTIAL / CANCELLED / REJECTED
    filled_qty  INTEGER DEFAULT 0,
    avg_fill    REAL,
    created_at  TEXT NOT NULL,
    ack_at      TEXT,                      -- وقت تأكيد المستخدم للتنفيذ
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    avg_entry    REAL NOT NULL,
    stop_price   REAL,
    targets      TEXT,                     -- JSON: قائمة الأهداف ونسب الخروج
    realized_pnl REAL DEFAULT 0,
    status       TEXT DEFAULT 'OPEN',
    opened_at    TEXT NOT NULL,
    closed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status, symbol);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts           TEXT PRIMARY KEY,
    cash         REAL NOT NULL,
    market_value REAL NOT NULL,
    total_equity REAL NOT NULL,
    daily_pnl    REAL,
    drawdown     REAL
);

-- ===================================================================
-- 4. التعلّم - predictions and self-scoring
-- ===================================================================
-- كل توقع يُسجَّل قبل أن تُعرف النتيجة، مع كامل المتغيرات التي بُني
-- عليها. هذا هو الشرط الوحيد الذي يجعل التقييم اللاحق ذا معنى.
CREATE TABLE IF NOT EXISTS predictions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL,            -- MARKET / SECTOR / SYMBOL
    target       TEXT NOT NULL,            -- TASI أو رمز السهم
    horizon      TEXT NOT NULL,            -- NEXT_SESSION / 1W / 1M
    direction    TEXT NOT NULL,            -- UP / DOWN / FLAT
    predicted_pct REAL,                    -- التغير المتوقع بالنسبة المئوية
    low_band     REAL,                     -- نطاق التوقع
    high_band    REAL,
    confidence   REAL,                     -- 0..1
    regime       TEXT,
    features     TEXT,                     -- JSON
    rationale_ar TEXT,
    made_at      TEXT NOT NULL,
    resolve_at   TEXT NOT NULL             -- متى تُقيَّم
);
CREATE INDEX IF NOT EXISTS idx_pred_resolve ON predictions(resolve_at, id);

CREATE TABLE IF NOT EXISTS prediction_results (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id),
    actual_pct    REAL,
    actual_direction TEXT,
    correct       INTEGER,                 -- 1 صحيح / 0 خاطئ
    error_abs     REAL,                    -- |المتوقع - الفعلي|
    within_band   INTEGER,
    resolved_at   TEXT NOT NULL,
    -- تحليل سبب الخطأ: يُملأ عند الفشل
    failure_cause TEXT,                    -- NEWS_SHOCK / REGIME_SHIFT
                                           -- / LOW_LIQUIDITY / MODEL_BIAS ...
    failure_notes TEXT
);

-- أداء كل نمط في كل حالة سوق. تُحدَّث بعد كل تقييم.
CREATE TABLE IF NOT EXISTS setup_performance (
    setup        TEXT NOT NULL,
    regime       TEXT NOT NULL,
    trades       INTEGER DEFAULT 0,
    wins         INTEGER DEFAULT 0,
    losses       INTEGER DEFAULT 0,
    gross_profit REAL DEFAULT 0,
    gross_loss   REAL DEFAULT 0,
    avg_r        REAL,                     -- متوسط العائد بوحدات المخاطرة
    hit_rate     REAL,
    expectancy   REAL,                     -- التوقع الرياضي لكل صفقة
    weight       REAL DEFAULT 1.0,         -- وزن النمط في القرار
    updated_at   TEXT,
    PRIMARY KEY (setup, regime)
) WITHOUT ROWID;

-- حالة السوق المكتشفة يومياً (اتجاه، تذبذب، اتساع).
CREATE TABLE IF NOT EXISTS market_regimes (
    date        TEXT PRIMARY KEY,
    regime      TEXT NOT NULL,             -- TREND_UP / TREND_DOWN / RANGE
                                           -- / HIGH_VOL / RISK_OFF
    tasi_close  REAL,
    tasi_change REAL,
    breadth     REAL,                      -- نسبة الأسهم الصاعدة
    volatility  REAL,
    turnover    REAL,
    notes       TEXT
);

-- سجل يومي لما فعله النظام، يُستخدم لبناء ملخص ما قبل الافتتاح.
CREATE TABLE IF NOT EXISTS session_journal (
    date        TEXT PRIMARY KEY,
    opened_at   TEXT,
    closed_at   TEXT,
    signals_count INTEGER DEFAULT 0,
    orders_count  INTEGER DEFAULT 0,
    realized_pnl  REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    equity_close  REAL,
    summary_ar    TEXT,
    lessons_ar    TEXT
);

-- سجل تشغيلي: كل خطأ أو انقطاع في البيانات يُسجَّل بدل أن يُبتلع.
CREATE TABLE IF NOT EXISTS system_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    level     TEXT NOT NULL,
    component TEXT NOT NULL,
    message   TEXT NOT NULL,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON system_log(ts DESC);
"""


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    """افتح الاتصال وأنشئ الجداول إن لزم."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def table_counts(conn: sqlite3.Connection) -> dict:
    """عدد الصفوف في كل جدول - نظرة سريعة على حجم البيانات."""
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables
    }
