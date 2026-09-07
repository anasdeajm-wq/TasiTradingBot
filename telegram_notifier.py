"""
telegram_notifier.py
====================
إرسال التنبيهات عبر تيليجرام - Telegram alerts for every BUY / SELL signal.

الإعداد - configuration via environment variables:
    TELEGRAM_BOT_TOKEN   توكن البوت من BotFather
    TELEGRAM_CHAT_ID     معرّف المحادثة أو القناة

If either variable is missing the notifier stays disabled and logs the message
locally instead of raising, so the trading loop never dies because of a
messaging outage.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT = 15
MAX_MESSAGE_LEN = 4096  # حد تيليجرام لطول الرسالة


class TelegramNotifier:
    """مرسل التنبيهات - a thin, fail-safe wrapper over the Telegram Bot API."""

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.enabled = bool(enabled and self.token and self.chat_id)
        self.sent: List[str] = []      # سجل محلي للرسائل - useful in tests

        if not self.enabled:
            log.warning(
                "تيليجرام غير مفعّل: تأكد من TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID"
            )

    # ------------------------------------------------------------------
    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """أرسل رسالة نصية. يُرجع True عند النجاح."""
        self.sent.append(text)

        if not self.enabled:
            log.info("[تيليجرام معطّل] %s", text.replace("\n", " | "))
            return False

        if len(text) > MAX_MESSAGE_LEN:
            text = text[: MAX_MESSAGE_LEN - 20] + "\n... (تم الاختصار)"

        url = f"{API_BASE}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            response = self.session.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            log.error("فشل إرسال رسالة تيليجرام: %s", exc)
            return False

        if response.status_code != 200:
            log.error(
                "تيليجرام رفض الرسالة (%s): %s",
                response.status_code,
                response.text[:200],
            )
            return False

        return True

    # ------------------------------------------------------------------
    # قوالب الرسائل - message templates
    # ------------------------------------------------------------------
    def notify_buy(
        self,
        symbol: str,
        price: float,
        shares: int = 0,
        reason: str = "",
        target_1: Optional[float] = None,
        target_2: Optional[float] = None,
        stop_loss: Optional[float] = None,
        position_value: float = 0.0,
        risk_amount: float = 0.0,
    ) -> bool:
        """تنبيه إشارة شراء."""
        lines = [
            "🟢 <b>إشارة شراء</b>",
            f"📊 السهم: <b>{symbol}</b>",
            f"💰 السعر: <b>{price:.2f}</b> ريال",
        ]
        if shares:
            lines.append(f"📦 الكمية: <b>{shares:,}</b> سهم")
        if position_value:
            lines.append(f"💵 قيمة الصفقة: {position_value:,.2f} ريال")
        if risk_amount:
            lines.append(f"⚠️ المخاطرة: {risk_amount:,.2f} ريال")
        if stop_loss:
            lines.append(f"🛑 وقف الخسارة: <b>{stop_loss:.2f}</b>")
        if target_1:
            lines.append(f"🎯 الهدف الأول (+2%): <b>{target_1:.2f}</b>")
        if target_2:
            lines.append(f"🎯 الهدف الثاني (+4%): <b>{target_2:.2f}</b>")
        if reason:
            lines.append(f"📌 السبب: {reason}")
        lines.append(f"🕐 {datetime.now():%Y-%m-%d %H:%M:%S}")
        return self.send("\n".join(lines))

    def notify_sell(
        self,
        symbol: str,
        price: float,
        shares: int = 0,
        reason: str = "",
        tag: str = "",
        entry_price: Optional[float] = None,
        pnl: Optional[float] = None,
    ) -> bool:
        """تنبيه إشارة بيع (هدف أول / هدف ثانٍ / وقف خسارة)."""
        icon = "🛑" if tag == "STOP_LOSS" else "🔴"
        title = {
            "TARGET_1": "بيع - الهدف الأول (+2%)",
            "TARGET_2": "بيع - الهدف الثاني (+4%)",
            "STOP_LOSS": "بيع - وقف الخسارة",
        }.get(tag, "إشارة بيع")

        lines = [
            f"{icon} <b>{title}</b>",
            f"📊 السهم: <b>{symbol}</b>",
            f"💰 سعر البيع: <b>{price:.2f}</b> ريال",
        ]
        if entry_price:
            change = (price - entry_price) / entry_price
            lines.append(f"📥 سعر الدخول: {entry_price:.2f} ({change:+.2%})")
        if shares:
            lines.append(f"📦 الكمية: <b>{shares:,}</b> سهم")
        if pnl is not None:
            sign = "✅ ربح" if pnl >= 0 else "❌ خسارة"
            lines.append(f"{sign}: <b>{pnl:,.2f}</b> ريال")
        if reason:
            lines.append(f"📌 السبب: {reason}")
        lines.append(f"🕐 {datetime.now():%Y-%m-%d %H:%M:%S}")
        return self.send("\n".join(lines))

    def notify_signal(self, signal, extra: Optional[Dict[str, object]] = None) -> bool:
        """أرسل تنبيهاً انطلاقاً من كائن Signal."""
        extra = extra or {}
        if signal.action == "BUY":
            return self.notify_buy(
                symbol=signal.symbol,
                price=signal.price,
                shares=int(extra.get("shares", 0) or 0),
                reason=signal.reason,
                target_1=signal.target_1,
                target_2=signal.target_2,
                stop_loss=extra.get("stop_loss") or signal.stop_loss,
                position_value=float(extra.get("position_value", 0) or 0),
                risk_amount=float(extra.get("risk_amount", 0) or 0),
            )
        if signal.action == "SELL":
            return self.notify_sell(
                symbol=signal.symbol,
                price=signal.price,
                shares=int(extra.get("shares", 0) or 0),
                reason=signal.reason,
                tag=signal.tag,
                entry_price=extra.get("entry_price"),
                pnl=extra.get("pnl"),
            )
        return False

    # ------------------------------------------------------------------
    def notify_risk_block(self, reason: str) -> bool:
        """تنبيه عند إيقاف التداول بسبب حدود الخسارة."""
        return self.send(
            "⛔ <b>إيقاف التداول</b>\n"
            f"📌 {reason}\n"
            f"🕐 {datetime.now():%Y-%m-%d %H:%M:%S}"
        )

    def notify_error(self, message: str) -> bool:
        """تنبيه بخطأ تشغيلي (فشل جلب الأسعار مثلاً)."""
        return self.send(
            "⚠️ <b>خطأ في النظام</b>\n"
            f"{message}\n"
            f"🕐 {datetime.now():%Y-%m-%d %H:%M:%S}"
        )

    def test_connection(self) -> bool:
        """تحقق من صحة التوكن عبر getMe."""
        if not self.enabled:
            return False
        try:
            response = self.session.get(
                f"{API_BASE}/bot{self.token}/getMe", timeout=self.timeout
            )
            return response.status_code == 200 and response.json().get("ok", False)
        except (requests.RequestException, ValueError) as exc:
            log.error("فشل الاتصال بتيليجرام: %s", exc)
            return False
