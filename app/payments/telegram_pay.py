"""Telegram Payments provider.

This wrapper does NOT generate a pay URL — Telegram Payments are delivered as
native invoices via `bot.send_invoice`. We track the payment via aiogram's
`pre_checkout_query` and `successful_payment` updates and mark the order paid
when the matching `successful_payment` arrives (matched by invoice_payload).
"""

from __future__ import annotations

from app.payments.base import PaymentResult


class TelegramPaymentsProvider:
    name = "telegram"

    def __init__(self, provider_token: str) -> None:
        self.provider_token = provider_token

    @property
    def enabled(self) -> bool:
        return bool(self.provider_token)

    async def create(
        self,
        *,
        amount_minor: int,
        currency: str,
        order_id: int,
        description: str,
    ) -> PaymentResult:
        # Telegram Payments are sent via send_invoice — we just return a
        # synthetic identifier so the caller can route accordingly.
        return PaymentResult(
            payment_id=f"tg:{order_id}",
            pay_url="",
            raw={"order_id": order_id},
        )

    async def is_paid(self, payment_id: str) -> bool:
        # Status is updated by successful_payment update handler directly on
        # the order row; this method is unused for Telegram Payments.
        return False
