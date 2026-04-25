"""Shared helpers for the Sentry-handler tests."""

from __future__ import annotations

import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport


class CapturingTransport(Transport):
    """Sentry transport that just records envelopes (no network)."""

    def __init__(self) -> None:
        super().__init__({})
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)

    def flush(self, timeout: float, callback=None) -> None:  # type: ignore[override]
        pass


def transactions(transport: CapturingTransport) -> list[dict]:
    out: list[dict] = []
    for env in transport.envelopes:
        for item in env.items:
            if item.headers.get("type") == "transaction":
                payload = item.payload.json
                if payload is not None:
                    out.append(payload)
    return out


def init_sentry(transport: CapturingTransport) -> None:
    """Boot a Sentry SDK with traces enabled and the in-memory transport."""
    sentry_sdk.init(
        dsn="https://public@example.com/1",
        transport=transport,
        traces_sample_rate=1.0,
        # Disable default integrations that might pollute envelopes.
        default_integrations=False,
        auto_enabling_integrations=False,
    )
