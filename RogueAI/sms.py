from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

import httpx


@runtime_checkable
class SMSTransport(Protocol):
    async def send(self, from_number: str, to_number: str, body: str) -> None:
        ...

    async def provision_numbers(self, count: int) -> list[str]:
        """Return `count` phone numbers to assign to AI players."""
        ...


class MockSMSTransport:
    """
    Dev transport — logs to console and optionally fires the mock webhook
    so the game engine can simulate inbound replies.
    """

    def __init__(self, mock_webhook_url: str = "http://localhost:8000/internal/mock-webhook"):
        self._webhook_url = mock_webhook_url
        self._counter = 0

    async def send(self, from_number: str, to_number: str, body: str) -> None:
        print(f"[SMS] {from_number} → {to_number}: {body}")

    async def provision_numbers(self, count: int) -> list[str]:
        return [f"+1555000{i:04d}" for i in range(1, count + 1)]


class SaperlySMSTransport:
    """
    Production transport using Saperly REST API (saperly.com/api/v1).
    Single-line mode: all messages sent from the system line; player identity
    is conveyed via a [Name]: prefix added by the caller.
    """

    def __init__(self, api_key: str, system_number: str):
        self._api_key = api_key
        self._system_number = system_number
        self._line_id: str | None = None
        self._client = httpx.AsyncClient(
            base_url="https://saperly.com",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=15,
        )

    async def _get_line_id(self) -> str:
        if self._line_id:
            return self._line_id
        resp = await self._client.get("/api/v1/lines")
        resp.raise_for_status()
        lines = resp.json().get("lines", [])
        for line in lines:
            if line.get("phone_number") == self._system_number:
                self._line_id = line["id"]
                return self._line_id
        # Fallback: use first active line
        if lines:
            self._line_id = lines[0]["id"]
            return self._line_id
        raise RuntimeError("No Saperly lines found — provision one at saperly.com")

    async def send(self, from_number: str, to_number: str, body: str) -> None:
        line_id = await self._get_line_id()
        resp = await self._client.post(
            "/api/v1/messages",
            json={"line_id": line_id, "to": to_number, "text": body},
        )
        resp.raise_for_status()

    async def provision_numbers(self, count: int) -> list[str]:
        # Single-line mode: return the system number for every slot.
        # Player identity is conveyed via [Name]: message prefix.
        return [self._system_number] * count


class TwilioSMSTransport:
    """
    Fallback transport using Twilio.
    Pre-provision a pool of 8 numbers in the Twilio console; pass them in.
    """

    def __init__(self, account_sid: str, auth_token: str, number_pool: list[str]):
        self._number_pool = number_pool
        try:
            from twilio.rest import Client  # type: ignore
            self._client = Client(account_sid, auth_token)
        except ImportError as exc:
            raise RuntimeError(
                "twilio package not installed. Run: pip install twilio"
            ) from exc

    async def send(self, from_number: str, to_number: str, body: str) -> None:
        await asyncio.to_thread(
            self._client.messages.create,
            from_=from_number,
            to=to_number,
            body=body,
        )

    async def provision_numbers(self, count: int) -> list[str]:
        if len(self._number_pool) < count:
            raise ValueError(
                f"Need {count} numbers but pool has only {len(self._number_pool)}. "
                "Pre-provision numbers in Twilio console."
            )
        return self._number_pool[:count]


def build_transport(transport_type: str, config: dict) -> SMSTransport:
    """Factory — driven by SMS_TRANSPORT env var."""
    if transport_type == "mock":
        return MockSMSTransport(
            mock_webhook_url=config.get("mock_webhook_url", "http://localhost:8000/internal/mock-webhook")
        )
    if transport_type == "saperly":
        return SaperlySMSTransport(
            api_key=config["saperly_api_key"],
            system_number=config["system_phone_number"],
        )
    if transport_type == "twilio":
        return TwilioSMSTransport(
            account_sid=config["twilio_account_sid"],
            auth_token=config["twilio_auth_token"],
            number_pool=config.get("twilio_number_pool", []),
        )
    raise ValueError(f"Unknown SMS_TRANSPORT: {transport_type!r}. Choose mock|saperly|twilio")
