"""Base interface for payment providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class PaymentResult:
    payment_id: str
    pay_url: str
    raw: dict


class PaymentProvider(Protocol):
    name: str

    @property
    def enabled(self) -> bool:  # pragma: no cover - protocol
        ...

    async def create(
        self,
        *,
        amount_minor: int,
        currency: str,
        order_id: int,
        description: str,
    ) -> PaymentResult:
        ...

    async def is_paid(self, payment_id: str) -> bool:
        ...
