"""CryptoBot (Crypto Pay API) provider.

We bill in user's currency (e.g. RUB) but the actual invoice is in `asset`
(USDT/TON/...) — Crypto Pay supports `fiat` + `accepted_assets` so we can ask
the user to pay any of the accepted cryptos in the right amount.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.payments.base import PaymentResult
from app.utils import to_major_units

log = logging.getLogger(__name__)


class CryptoBotProvider:
    name = "cryptobot"

    def __init__(self, token: str, base_url: str, asset: str = "USDT") -> None:
        self.token = token
        self.base_url = base_url
        self.asset = asset

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Crypto-Pay-API-Token": self.token}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                f"{self.base_url}/{method}", json=payload, timeout=20
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"CryptoPay {method}: {data}")
                return data["result"]

    async def create(
        self,
        *,
        amount_minor: int,
        currency: str,
        order_id: int,
        description: str,
    ) -> PaymentResult:
        amount_major = to_major_units(amount_minor, currency)
        # Use fiat amount with accepted_assets — Crypto Pay computes crypto value.
        payload = {
            "currency_type": "fiat",
            "fiat": currency.upper(),
            "amount": str(round(amount_major, 2)),
            "accepted_assets": "USDT,TON,BTC,ETH,LTC,BNB,TRX",
            "description": description[:1024],
            "payload": f"order:{order_id}",
            "expires_in": 3600,
        }
        try:
            data = await self._call("createInvoice", payload)
        except Exception as e:
            log.warning("createInvoice fiat failed: %s; falling back to asset", e)
            # Fallback: charge fixed asset amount equal to amount_major (assume USDT≈RUB? no).
            # Better: use exchangeRates first.
            rate = await self._fetch_rate(self.asset, currency.upper())
            crypto_amount = amount_major / rate if rate else amount_major
            data = await self._call(
                "createInvoice",
                {
                    "asset": self.asset,
                    "amount": f"{crypto_amount:.6f}",
                    "description": description[:1024],
                    "payload": f"order:{order_id}",
                    "expires_in": 3600,
                },
            )
        pay_url = data.get("bot_invoice_url") or data.get("pay_url") or data.get("mini_app_invoice_url")
        return PaymentResult(payment_id=str(data["invoice_id"]), pay_url=pay_url, raw=data)

    async def _fetch_rate(self, asset: str, fiat: str) -> float | None:
        try:
            rates = await self._call("getExchangeRates", {})
        except Exception:
            return None
        for r in rates:
            if r.get("source") == asset and r.get("target") == fiat:
                try:
                    return float(r["rate"])
                except (KeyError, ValueError):
                    return None
        return None

    async def is_paid(self, payment_id: str) -> bool:
        try:
            data = await self._call("getInvoices", {"invoice_ids": payment_id})
        except Exception as e:
            log.warning("getInvoices failed: %s", e)
            return False
        items = data.get("items") or []
        for inv in items:
            if str(inv.get("invoice_id")) == str(payment_id):
                return inv.get("status") == "paid"
        return False
