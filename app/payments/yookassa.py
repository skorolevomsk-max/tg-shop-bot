"""YooKassa REST API provider (https://yookassa.ru/developers/api)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import aiohttp

from app.payments.base import PaymentResult
from app.utils import to_major_units

log = logging.getLogger(__name__)

YOOKASSA_BASE = "https://api.yookassa.ru/v3"


class YooKassaProvider:
    name = "yookassa"

    def __init__(self, shop_id: str, secret_key: str, return_url: str) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.return_url = return_url or "https://t.me/"

    @property
    def enabled(self) -> bool:
        return bool(self.shop_id and self.secret_key)

    def _auth(self) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(self.shop_id, self.secret_key)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Idempotence-Key": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession(auth=self._auth(), headers=headers) as s:
            async with s.post(f"{YOOKASSA_BASE}{path}", json=payload, timeout=20) as r:
                data = await r.json()
                if r.status >= 300:
                    raise RuntimeError(f"YooKassa {path} {r.status}: {data}")
                return data

    async def _get(self, path: str) -> dict[str, Any]:
        async with aiohttp.ClientSession(auth=self._auth()) as s:
            async with s.get(f"{YOOKASSA_BASE}{path}", timeout=20) as r:
                data = await r.json()
                if r.status >= 300:
                    raise RuntimeError(f"YooKassa {path} {r.status}: {data}")
                return data

    async def create(
        self,
        *,
        amount_minor: int,
        currency: str,
        order_id: int,
        description: str,
    ) -> PaymentResult:
        amount_major = to_major_units(amount_minor, currency)
        payload = {
            "amount": {
                "value": f"{amount_major:.2f}",
                "currency": currency.upper(),
            },
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": self.return_url,
            },
            "description": description[:128],
            "metadata": {"order_id": str(order_id)},
        }
        data = await self._post("/payments", payload)
        confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")
        return PaymentResult(
            payment_id=data["id"], pay_url=confirmation_url, raw=data
        )

    async def is_paid(self, payment_id: str) -> bool:
        try:
            data = await self._get(f"/payments/{payment_id}")
        except Exception as e:
            log.warning("YooKassa getPayment failed: %s", e)
            return False
        return data.get("status") == "succeeded" and bool(data.get("paid"))
