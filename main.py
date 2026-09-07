"""
main.py
=======
نظام تداول آلي للسوق السعودي (تداول) - Tadawul automated trading bot.

الوظائف:
    - جلب الأسعار من Twelve Data كل دقيقة
    - حفظ البيانات في قاعدة بيانات SQLite
    - حساب المؤشرات (indicators.py)
    - تطبيق الاستراتيجية (strategy.py)
    - إدارة المخاطر (risk_manager.py)
    - إرسال التنبيهات عبر تيليجرام (telegram_notifier.py)

الأسهم المتابَعة:
    2222.SR أرامكو السعودية
    1120.SR مصرف الراجحي
    2010.SR سابك
    4001.SR أسواق عبدالله العثيم
    4180.SR فتيحي القابضة

التشغيل - usage:
    python main.py --once        جولة واحدة: اجلب الأسعار واعرض النتائج
    python main.py --loop        التشغيل المستمر كل دقيقة
    python main.py --backfill    تحميل التاريخ اليومي (مطلوب لـ MA200)
    python main.py --status      اعرض حالة المخاطر والصفقات المفتوحة
    python main.py --self-test   اختبار المنظومة ببيانات تجريبية (بدون شبكة)

متغيرات البيئة - environment variables:
    TWELVE_DATA_API_KEY   مفتاح Twelve Data (مطلوب للأسعار الحقيقية)
    TELEGRAM_BOT_TOKEN    توكن بوت تيليجرام
    TELEGRAM_CHAT_ID      معرّف المحادثة
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

import indicators
import strategy
from risk_manager import RiskManager
from strategy import Position, Signal
from telegram_notifier import TelegramNotifier

# ---------------------------------------------------------------------------
# الإعدادات - configuration
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("TASI_DB_PATH", "tasi_data.db")
API_BASE = "https://api.twelvedata.com"
API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")

POLL_SECONDS = 60                 # جلب الأسعار كل دقيقة
HISTORY_BARS = 250                # عدد الشموع اليومية (يكفي لـ MA200)
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
RATE_LIMIT_PER_MIN = 8            # حد الباقة المجانية في Twelve Data

# توقيت السوق السعودي - Tadawul session (Sunday..Thursday, 10:00-15:00 AST)
RIYADH_TZ = timezone(timedelta(hours=3))
MARKET_OPEN = dtime(10, 0)
MARKET_CLOSE = dtime(15, 0)
TRADING_WEEKDAYS = {6, 0, 1, 2, 3}   # Python weekday(): Sun=6, Mon=0 .. Thu=3

# الأسهم الخمسة. المفتاح هو الرمز كما طلبه المستخدم، والقيمة اسم الشركة.
# ملاحظة: Twelve Data يستخدم الرمز الرقمي المجرد مع mic_code=XSAU
# (أي "2222" وليس "2222.SR")، ويتم التحويل في to_api_symbol().
SYMBOLS: Dict[str, str] = {
    "2222.SR": "أرامكو السعودية",
    "1120.SR": "مصرف الراجحي",
    "2010.SR": "سابك",
    "4001.SR": "أسواق عبدالله العثيم",
    "4180.SR": "فتيحي القابضة",
}
MIC_CODE = "XSAU"                 # رمز سوق تداول في معيار ISO 10383

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tasi")


def to_api_symbol(symbol: str) -> str:
    """حوّل 2222.SR إلى الرمز الذي يفهمه Twelve Data (2222)."""
    return symbol.split(".")[0]


def now_riyadh() -> datetime:
    return datetime.now(RIYADH_TZ)


def market_is_open(moment: Optional[datetime] = None) -> bool:
    """هل السوق السعودي مفتوح الآن؟ (الأحد-الخميس، 10:00-15:00 بتوقيت الرياض)"""
    moment = moment or now_riyadh()
    if moment.weekday() not in TRADING_WEEKDAYS:
        return False
    return MARKET_OPEN <= moment.time() <= MARKET_CLOSE


# ---------------------------------------------------------------------------
# قاعدة البيانات - SQLite storage
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT    NOT NULL,
    interval  TEXT    NOT NULL DEFAULT '1day',
    ts        TEXT    NOT NULL,          -- وقت الشمعة (نص ISO)
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL    NOT NULL,
    volume    REAL    DEFAULT 0,
    fetched_at TEXT   NOT NULL,
    UNIQUE(symbol, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_prices_symbol_ts ON prices(symbol, interval, ts);

CREATE TABLE IF NOT EXISTS quotes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    price     REAL NOT NULL,
    volume    REAL,
    change_pct REAL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_symbol ON quotes(symbol, fetched_at);

CREATE TABLE IF NOT EXISTS indicators (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    ts        TEXT NOT NULL,
    close     REAL,
    ma20      REAL,
    ma50      REAL,
    ma200     REAL,
    rsi       REAL,
    macd      REAL,
    macd_signal REAL,
    macd_hist REAL,
    volume_ratio REAL,
    UNIQUE(symbol, ts)
);

CREATE TABLE IF NOT EXISTS signals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    action    TEXT NOT NULL,
    price     REAL NOT NULL,
    reason    TEXT,
    tag       TEXT,
    target_1  REAL,
    target_2  REAL,
    stop_loss REAL,
    created_at TEXT NOT NULL,
    notified  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    shares      INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    stop_price  REAL,
    target_1    REAL,
    target_2    REAL,
    target_1_hit INTEGER DEFAULT 0,
    exit_price  REAL,
    pnl         REAL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN / CLOSED
    opened_at   TEXT NOT NULL,
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status, symbol);
"""


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    """أنشئ قاعدة البيانات والجداول إن لم تكن موجودة."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    log.info("قاعدة البيانات جاهزة: %s", os.path.abspath(path))
    return conn


def save_bars(conn: sqlite3.Connection, symbol: str, bars: Sequence[dict],
              interval: str = "1day") -> int:
    """احفظ الشموع في جدول prices (تجاهل المكرر)."""
    fetched = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            symbol, interval, bar["ts"], bar.get("open"), bar.get("high"),
            bar.get("low"), bar["close"], bar.get("volume", 0), fetched,
        )
        for bar in bars
    ]
    cur = conn.executemany(
        """
        INSERT OR REPLACE INTO prices
            (symbol, interval, ts, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return cur.rowcount


def save_quote(conn: sqlite3.Connection, symbol: str, price: float,
               volume: Optional[float], change_pct: Optional[float]) -> None:
    conn.execute(
        "INSERT INTO quotes (symbol, price, volume, change_pct, fetched_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (symbol, price, volume, change_pct,
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def load_history(conn: sqlite3.Connection, symbol: str, interval: str = "1day",
                 limit: int = HISTORY_BARS) -> Tuple[List[float], List[float]]:
    """اقرأ الإغلاقات والأحجام من الأقدم إلى الأحدث."""
    rows = conn.execute(
        """
        SELECT close, volume FROM prices
         WHERE symbol = ? AND interval = ?
         ORDER BY ts DESC LIMIT ?
        """,
        (symbol, interval, limit),
    ).fetchall()
    rows = list(reversed(rows))
    return [float(r["close"]) for r in rows], [float(r["volume"] or 0) for r in rows]


def save_indicators(conn: sqlite3.Connection, snap: indicators.IndicatorSnapshot,
                    ts: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO indicators
            (symbol, ts, close, ma20, ma50, ma200, rsi, macd, macd_signal,
             macd_hist, volume_ratio)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (snap.symbol, ts, snap.close, snap.ma20, snap.ma50, snap.ma200,
         snap.rsi, snap.macd, snap.macd_signal, snap.macd_hist,
         snap.volume_ratio),
    )
    conn.commit()


def save_signal(conn: sqlite3.Connection, signal: Signal, notified: bool) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals
            (symbol, action, price, reason, tag, target_1, target_2, stop_loss,
             created_at, notified)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (signal.symbol, signal.action, signal.price, signal.reason, signal.tag,
         signal.target_1, signal.target_2, signal.stop_loss,
         signal.timestamp, int(notified)),
    )
    conn.commit()
    return int(cur.lastrowid)


def open_positions(conn: sqlite3.Connection) -> Dict[str, Position]:
    """الصفقات المفتوحة من قاعدة البيانات."""
    rows = conn.execute("SELECT * FROM trades WHERE status = 'OPEN'").fetchall()
    return {
        r["symbol"]: Position(
            symbol=r["symbol"],
            shares=int(r["shares"]),
            entry_price=float(r["entry_price"]),
            stop_price=float(r["stop_price"]) if r["stop_price"] is not None else None,
            target_1_hit=bool(r["target_1_hit"]),
            opened_at=r["opened_at"],
        )
        for r in rows
    }


# ---------------------------------------------------------------------------
# عميل Twelve Data - API client
# ---------------------------------------------------------------------------
class TwelveDataError(RuntimeError):
    """خطأ من واجهة Twelve Data."""


class TwelveDataClient:
    """عميل Twelve Data مع إعادة المحاولة واحترام حد الطلبات."""

    def __init__(self, api_key: str = API_KEY, mic_code: str = MIC_CODE,
                 rate_limit: int = RATE_LIMIT_PER_MIN) -> None:
        self.api_key = api_key
        self.mic_code = mic_code
        self.rate_limit = rate_limit
        self.session = requests.Session()
        self._calls: List[float] = []

    # -- rate limiting -------------------------------------------------
    def _throttle(self) -> None:
        """احترم حد الطلبات في الدقيقة (الباقة المجانية = 8 طلبات)."""
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < 60]
        if len(self._calls) >= self.rate_limit:
            wait = 60 - (now - self._calls[0]) + 0.5
            if wait > 0:
                log.info("انتظار %.1f ثانية لتجاوز حد الطلبات", wait)
                time.sleep(wait)
            self._calls = [t for t in self._calls if time.monotonic() - t < 60]
        self._calls.append(time.monotonic())

    def _get(self, endpoint: str, params: dict) -> dict:
        """طلب مع إعادة المحاولة (backoff أسّي) عند أخطاء الشبكة أو 429."""
        if not self.api_key:
            raise TwelveDataError(
                "مفتاح Twelve Data غير موجود. عيّن المتغير TWELVE_DATA_API_KEY."
            )

        params = dict(params, apikey=self.api_key)
        delay = 2.0
        last_error = ""

        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            try:
                response = self.session.get(
                    f"{API_BASE}/{endpoint}", params=params, timeout=REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                last_error = f"خطأ شبكة: {exc}"
                log.warning("محاولة %d/%d فشلت: %s", attempt, MAX_RETRIES, last_error)
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 429:
                last_error = "تجاوز حد الطلبات (429)"
                log.warning("%s - انتظار %.0f ثانية", last_error, delay)
                time.sleep(delay)
                delay *= 2
                continue

            try:
                payload = response.json()
            except ValueError:
                raise TwelveDataError(
                    f"رد غير صالح من الخادم ({response.status_code}): "
                    f"{response.text[:200]}"
                )

            # Twelve Data returns errors as a JSON body with status="error",
            # sometimes alongside HTTP 200.
            if isinstance(payload, dict) and payload.get("status") == "error":
                message = payload.get("message", "خطأ غير معروف")
                code = payload.get("code")
                if code == 429:
                    time.sleep(delay)
                    delay *= 2
                    last_error = message
                    continue
                raise TwelveDataError(f"[{code}] {message}")

            return payload

        raise TwelveDataError(f"فشل الطلب بعد {MAX_RETRIES} محاولات: {last_error}")

    # -- endpoints -----------------------------------------------------
    def quotes(self, symbols: Sequence[str]) -> Dict[str, dict]:
        """اجلب أسعار عدة أسهم في طلب واحد.

        Twelve Data accepts a comma-separated symbol list and returns either a
        single object (one symbol) or a dict keyed by symbol (many).
        """
        api_symbols = [to_api_symbol(s) for s in symbols]
        payload = self._get(
            "quote",
            {"symbol": ",".join(api_symbols), "mic_code": self.mic_code},
        )

        result: Dict[str, dict] = {}
        if "symbol" in payload:                      # سهم واحد
            result[symbols[0]] = payload
            return result

        reverse = {to_api_symbol(s): s for s in symbols}
        for api_symbol, data in payload.items():
            display = reverse.get(api_symbol, api_symbol)
            if isinstance(data, dict) and data.get("status") == "error":
                log.error("%s: %s", display, data.get("message"))
                continue
            result[display] = data
        return result

    def time_series(self, symbol: str, interval: str = "1day",
                    outputsize: int = HISTORY_BARS) -> List[dict]:
        """اجلب الشموع التاريخية (الأقدم أولاً)."""
        payload = self._get(
            "time_series",
            {
                "symbol": to_api_symbol(symbol),
                "mic_code": self.mic_code,
                "interval": interval,
                "outputsize": outputsize,
                "order": "ASC",
            },
        )
        values = payload.get("values") or []
        return [
            {
                "ts": v["datetime"],
                "open": _as_float(v.get("open")),
                "high": _as_float(v.get("high")),
                "low": _as_float(v.get("low")),
                "close": _as_float(v.get("close")),
                "volume": _as_float(v.get("volume")) or 0.0,
            }
            for v in values
            if _as_float(v.get("close")) is not None
        ]


def _as_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# البوت - orchestration
# ---------------------------------------------------------------------------
class TradingBot:
    """يربط الجلب والتخزين والمؤشرات والاستراتيجية والمخاطر والتنبيهات."""

    def __init__(self, conn: sqlite3.Connection,
                 client: Optional[TwelveDataClient] = None,
                 notifier: Optional[TelegramNotifier] = None,
                 risk: Optional[RiskManager] = None,
                 dry_run: bool = True) -> None:
        self.conn = conn
        self.client = client or TwelveDataClient()
        self.notifier = notifier or TelegramNotifier()
        self.risk = risk or RiskManager(conn=conn)
        self.dry_run = dry_run   # لا تُسجَّل صفقات فعلية في وضع المحاكاة

    # ------------------------------------------------------------------
    def backfill(self, symbols: Iterable[str] = tuple(SYMBOLS)) -> Dict[str, int]:
        """حمّل التاريخ اليومي لكل سهم (مطلوب لحساب MA200)."""
        saved: Dict[str, int] = {}
        for symbol in symbols:
            try:
                bars = self.client.time_series(symbol, "1day", HISTORY_BARS)
                save_bars(self.conn, symbol, bars, "1day")
                saved[symbol] = len(bars)
                log.info("%s: تم حفظ %d شمعة يومية", symbol, len(bars))
            except TwelveDataError as exc:
                saved[symbol] = 0
                log.error("%s: فشل تحميل التاريخ - %s", symbol, exc)
        return saved

    # ------------------------------------------------------------------
    def fetch_prices(self, symbols: Sequence[str] = tuple(SYMBOLS)) -> Dict[str, dict]:
        """اجلب الأسعار الحالية واحفظها في قاعدة البيانات."""
        quotes = self.client.quotes(symbols)
        for symbol, data in quotes.items():
            price = _as_float(data.get("close") or data.get("price"))
            if price is None:
                continue
            volume = _as_float(data.get("volume")) or 0.0
            change_pct = _as_float(data.get("percent_change"))
            save_quote(self.conn, symbol, price, volume, change_pct)

            # حدّث شمعة اليوم في جدول الأسعار
            ts = data.get("datetime") or now_riyadh().strftime("%Y-%m-%d")
            save_bars(
                self.conn, symbol,
                [{
                    "ts": ts,
                    "open": _as_float(data.get("open")),
                    "high": _as_float(data.get("high")),
                    "low": _as_float(data.get("low")),
                    "close": price,
                    "volume": volume,
                }],
                "1day",
            )
        return quotes

    # ------------------------------------------------------------------
    def analyse(self, symbol: str) -> indicators.IndicatorSnapshot:
        """احسب المؤشرات من التاريخ المخزَّن."""
        closes, volumes = load_history(self.conn, symbol)
        snap = indicators.compute_all(symbol, closes, volumes)
        if snap.close is not None:
            save_indicators(self.conn, snap, now_riyadh().isoformat(timespec="seconds"))
        return snap

    # ------------------------------------------------------------------
    def act(self, snap: indicators.IndicatorSnapshot,
            positions: Dict[str, Position]) -> Signal:
        """طبّق الاستراتيجية ثم المخاطر ثم أرسل التنبيه."""
        position = positions.get(snap.symbol)
        signal = strategy.evaluate(snap, position)

        if not signal.is_actionable:
            return signal

        if signal.action == strategy.BUY:
            sizing = self.risk.approve_entry(snap.symbol, signal.price)
            if not sizing.approved:
                log.info("%s: إشارة شراء مرفوضة - %s", snap.symbol, sizing.reason)
                signal.action = strategy.HOLD
                signal.reason = f"{signal.reason} | مرفوضة: {sizing.reason}"
                return signal

            signal.stop_loss = sizing.stop_price
            notified = self.notifier.notify_signal(
                signal,
                {
                    "shares": sizing.shares,
                    "stop_loss": sizing.stop_price,
                    "position_value": sizing.position_value,
                    "risk_amount": sizing.risk_amount,
                },
            )
            save_signal(self.conn, signal, notified)
            if not self.dry_run:
                self._open_trade(signal, sizing)
            return signal

        # SELL
        extra = {}
        if position:
            shares = position.shares
            if signal.exit_fraction and signal.exit_fraction < 1.0:
                shares = max(1, int(position.shares * signal.exit_fraction))
            extra = {
                "shares": shares,
                "entry_price": position.entry_price,
                "pnl": (signal.price - position.entry_price) * shares,
            }
        notified = self.notifier.notify_signal(signal, extra)
        save_signal(self.conn, signal, notified)
        if not self.dry_run and position:
            self._close_trade(signal, position, int(extra.get("shares", 0)))
        return signal

    def _open_trade(self, signal: Signal, sizing) -> None:
        self.conn.execute(
            """
            INSERT INTO trades
                (symbol, shares, entry_price, stop_price, target_1, target_2,
                 status, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (signal.symbol, sizing.shares, signal.price, sizing.stop_price,
             signal.target_1, signal.target_2,
             datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def _close_trade(self, signal: Signal, position: Position, shares: int) -> None:
        pnl = (signal.price - position.entry_price) * shares
        if signal.exit_fraction and signal.exit_fraction < 1.0:
            self.conn.execute(
                "UPDATE trades SET shares = shares - ?, target_1_hit = 1,"
                " pnl = COALESCE(pnl, 0) + ?"
                " WHERE symbol = ? AND status = 'OPEN'",
                (shares, pnl, signal.symbol),
            )
        else:
            self.conn.execute(
                "UPDATE trades SET status = 'CLOSED', exit_price = ?,"
                " pnl = COALESCE(pnl, 0) + ?, closed_at = ?"
                " WHERE symbol = ? AND status = 'OPEN'",
                (signal.price, pnl,
                 datetime.now().isoformat(timespec="seconds"), signal.symbol),
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    def run_once(self, symbols: Sequence[str] = tuple(SYMBOLS)) -> List[dict]:
        """جولة واحدة: جلب -> حفظ -> مؤشرات -> استراتيجية -> مخاطر -> تنبيه."""
        status = self.risk.status()
        if not status.trading_allowed:
            log.warning("التداول متوقف: %s", status.blocked_reason)
            self.notifier.notify_risk_block(status.blocked_reason)

        quotes = self.fetch_prices(symbols)
        positions = open_positions(self.conn)

        results: List[dict] = []
        for symbol in symbols:
            quote = quotes.get(symbol)
            if not quote:
                log.error("%s: لم يتم استلام سعر", symbol)
                results.append({"symbol": symbol, "error": "لا توجد بيانات"})
                continue

            snap = self.analyse(symbol)
            signal = self.act(snap, positions)
            results.append({
                "symbol": symbol,
                "name": SYMBOLS.get(symbol, ""),
                "quote": quote,
                "snapshot": snap,
                "signal": signal,
            })
        return results

    # ------------------------------------------------------------------
    def run_loop(self, interval: int = POLL_SECONDS,
                 respect_market_hours: bool = True) -> None:
        """التشغيل المستمر: جولة كل دقيقة."""
        log.info("بدء التشغيل المستمر (كل %d ثانية). اضغط Ctrl+C للإيقاف.", interval)
        while True:
            started = time.monotonic()
            try:
                if respect_market_hours and not market_is_open():
                    log.info(
                        "السوق مغلق (%s بتوقيت الرياض) - في انتظار الجلسة",
                        now_riyadh().strftime("%A %H:%M"),
                    )
                else:
                    results = self.run_once()
                    print_results(results)
            except TwelveDataError as exc:
                log.error("خطأ في جلب البيانات: %s", exc)
                self.notifier.notify_error(str(exc))
            except KeyboardInterrupt:
                log.info("تم الإيقاف بواسطة المستخدم")
                return
            except Exception as exc:                      # noqa: BLE001
                log.exception("خطأ غير متوقع: %s", exc)
                self.notifier.notify_error(f"خطأ غير متوقع: {exc}")

            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))


# ---------------------------------------------------------------------------
# العرض - console output
# ---------------------------------------------------------------------------
def _fmt(value: Optional[float], width: int = 8, digits: int = 2) -> str:
    return "-".rjust(width) if value is None else f"{value:>{width}.{digits}f}"


def print_results(results: Sequence[dict]) -> None:
    """اعرض النتائج في جدول."""
    print()
    print("=" * 104)
    print(f"نتائج الجولة - {now_riyadh():%Y-%m-%d %H:%M:%S} (توقيت الرياض)"
          f" | السوق {'مفتوح' if market_is_open() else 'مغلق'}")
    print("=" * 104)
    header = (
        f"{'الرمز':<10}{'الشركة':<22}{'السعر':>9}{'MA20':>9}{'MA50':>9}"
        f"{'MA200':>9}{'RSI':>7}{'MACD':>9}{'Vol×':>7}  {'الإشارة':<8}"
    )
    print(header)
    print("-" * 104)

    for row in results:
        symbol = row["symbol"]
        if row.get("error"):
            print(f"{symbol:<10}{row.get('name', ''):<22}  ⚠️  {row['error']}")
            continue
        snap = row["snapshot"]
        signal = row["signal"]
        icon = {"BUY": "🟢 شراء", "SELL": "🔴 بيع", "HOLD": "⚪ انتظار"}.get(
            signal.action, signal.action
        )
        print(
            f"{symbol:<10}{row.get('name', ''):<22}"
            f"{_fmt(snap.close, 9)}{_fmt(snap.ma20, 9)}{_fmt(snap.ma50, 9)}"
            f"{_fmt(snap.ma200, 9)}{_fmt(snap.rsi, 7, 1)}{_fmt(snap.macd, 9, 3)}"
            f"{_fmt(snap.volume_ratio, 7, 2)}  {icon:<8}"
        )
        print(f"{'':<10}└─ {signal.reason}")
    print("=" * 104)


def print_status(conn: sqlite3.Connection, risk: RiskManager) -> None:
    """اعرض حالة المخاطر والصفقات المفتوحة."""
    status = risk.status()
    print()
    print("=" * 70)
    print("حالة إدارة المخاطر")
    print("=" * 70)
    print(f"رأس المال              : {status.capital:>12,.2f} ريال")
    print(f"المخاطرة لكل صفقة (1%) : {risk.risk_amount:>12,.2f} ريال")
    print(f"حد الخسارة اليومي (3%) : {status.daily_limit_amount:>12,.2f} ريال")
    print(f"حد الخسارة الأسبوعي(6%): {status.weekly_limit_amount:>12,.2f} ريال")
    print("-" * 70)
    print(f"ربح/خسارة اليوم        : {status.daily_pnl:>12,.2f} ريال")
    print(f"ربح/خسارة الأسبوع      : {status.weekly_pnl:>12,.2f} ريال")
    print(f"الصفقات المفتوحة       : {status.open_positions:>12}")
    print(f"التداول مسموح          : {'نعم ✅' if status.trading_allowed else 'لا ⛔'}")
    if status.blocked_reason:
        print(f"السبب                  : {status.blocked_reason}")

    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY opened_at"
    ).fetchall()
    if rows:
        print("-" * 70)
        print("الصفقات المفتوحة:")
        for r in rows:
            print(f"  {r['symbol']:<10} {r['shares']:>6} سهم @ "
                  f"{r['entry_price']:.2f} | وقف {r['stop_price']:.2f}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# اختبار ذاتي ببيانات تجريبية - offline self-test
# ---------------------------------------------------------------------------
def self_test(conn: sqlite3.Connection) -> List[dict]:
    """شغّل المنظومة كاملة ببيانات تجريبية مولَّدة محلياً (بدون شبكة).

    ⚠️ البيانات هنا ليست أسعاراً حقيقية - الغرض إثبات أن سلسلة العمل
    (المؤشرات -> الاستراتيجية -> المخاطر -> التنبيه) تعمل بلا أخطاء.
    """
    log.warning("اختبار ذاتي: البيانات مولَّدة محلياً وليست أسعاراً حقيقية")
    rng = random.Random(20260907)
    bot = TradingBot(conn, notifier=TelegramNotifier(enabled=False), dry_run=True)

    base_prices = {
        "2222.SR": 27.0, "1120.SR": 92.0, "2010.SR": 62.0,
        "4001.SR": 13.5, "4180.SR": 38.0,
    }
    start = datetime.now() - timedelta(days=260)

    for symbol, base in base_prices.items():
        price = base
        bars = []
        for day in range(240):
            drift = 0.0006 if symbol in ("2222.SR", "1120.SR") else -0.0002
            price *= 1 + drift + rng.gauss(0, 0.011)
            volume = rng.uniform(1.0e6, 3.0e6)
            if day == 239:                      # شمعة أخيرة بحجم مرتفع
                volume *= 2.4
                price *= 1.015
            bars.append({
                "ts": (start + timedelta(days=day)).strftime("%Y-%m-%d"),
                "open": price * 0.998, "high": price * 1.006,
                "low": price * 0.994, "close": price, "volume": volume,
            })
        save_bars(conn, symbol, bars, "1day")

    positions = open_positions(conn)
    results = []
    for symbol in base_prices:
        snap = bot.analyse(symbol)
        signal = bot.act(snap, positions)
        results.append({
            "symbol": symbol, "name": SYMBOLS.get(symbol, ""),
            "quote": {}, "snapshot": snap, "signal": signal,
        })
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="نظام تداول آلي للسوق السعودي - Tadawul trading bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true",
                       help="جولة واحدة: اجلب الأسعار واعرض النتائج")
    group.add_argument("--loop", action="store_true",
                       help="تشغيل مستمر كل دقيقة")
    group.add_argument("--backfill", action="store_true",
                       help="تحميل التاريخ اليومي (مطلوب لـ MA200)")
    group.add_argument("--status", action="store_true",
                       help="اعرض حالة المخاطر والصفقات")
    group.add_argument("--self-test", action="store_true",
                       help="اختبار المنظومة ببيانات تجريبية بدون شبكة")
    parser.add_argument("--db", default=DB_PATH, help=f"مسار قاعدة البيانات (افتراضي {DB_PATH})")
    parser.add_argument("--interval", type=int, default=POLL_SECONDS,
                        help="الفاصل الزمني بالثواني (افتراضي 60)")
    parser.add_argument("--capital", type=float, default=100_000.0,
                        help="رأس المال بالريال (افتراضي 100000)")
    parser.add_argument("--ignore-market-hours", action="store_true",
                        help="تجاهل أوقات جلسة تداول في وضع --loop")
    parser.add_argument("--live", action="store_true",
                        help="سجّل الصفقات فعلياً في قاعدة البيانات (افتراضياً محاكاة)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    conn = init_db(args.db)
    risk = RiskManager(capital=args.capital, conn=conn)

    if args.status:
        print_status(conn, risk)
        return 0

    if args.self_test:
        print_results(self_test(conn))
        print_status(conn, risk)
        return 0

    notifier = TelegramNotifier()
    bot = TradingBot(conn, notifier=notifier, risk=risk, dry_run=not args.live)

    if args.backfill:
        saved = bot.backfill()
        total = sum(saved.values())
        log.info("تم حفظ %d شمعة إجمالاً", total)
        return 0 if total else 1

    if args.loop:
        bot.run_loop(args.interval, respect_market_hours=not args.ignore_market_hours)
        return 0

    # الافتراضي: جولة واحدة
    try:
        results = bot.run_once()
    except TwelveDataError as exc:
        log.error("فشل جلب الأسعار: %s", exc)
        notifier.notify_error(f"فشل جلب الأسعار: {exc}")
        return 1

    print_results(results)
    print_status(conn, risk)
    return 0


if __name__ == "__main__":
    sys.exit(main())
