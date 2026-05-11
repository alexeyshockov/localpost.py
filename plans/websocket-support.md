# WebSocket support — sketch (not yet implemented)

## Context

`HTTPReqCtx` (after the WSGI deployment refactor in
`plans/wsgi-deployment.md`) is shaped for a request/response lifecycle.
WebSocket connections need a different lifecycle — long-lived, bidirectional
message stream after the handshake. They don't fit `HTTPReqCtx` cleanly,
so this is a sibling Protocol.

This plan is a forward-compatibility sketch — we're not implementing
WS now. The point is to make sure the design we do now (Protocol shape,
router shape, transport adapters) leaves the door open.

## Protocol — `WebSocketCtx`

```python
class WebSocketCtx(Protocol):
    request: Request                      # initial handshake request
    remote_addr: str | None
    local_addr: str | None
    scheme: str                           # "ws" | "wss"
    attrs: dict[Any, Any]

    def accept(self, *, subprotocol: str | None = None) -> None: ...
    def reject(self, status: int = 403) -> None: ...
    def receive(self) -> WSMessage: ...   # blocks; returns text / bytes / Close
    def send_bytes(self, data: bytes) -> None: ...
    def send_str(self,  data: str)  -> None: ...
    def close(self, code: int = 1000) -> None: ...

WSHandler = Callable[[WebSocketCtx], None]
```

`WSMessage` is a small ADT — `WSText(str)` / `WSBinary(bytes)` /
`WSClose(code, reason)`.

`request` is the initial handshake request (path, headers, etc.) so
auth / routing decisions reuse existing helpers.

## Router

`Routes.add_ws("/path", handler)` — a separate dispatch table from
the HTTP method table. The connection-upgrade decision (when the native
server sees `Upgrade: websocket`) consults this table; success →
`WebSocketCtx` flow, miss → 404 over HTTP.

`Router._match` gains one more variant in the result type. Per-route
middlewares stay HTTP-only; WS middlewares are a follow-up if needed.

## Transport fit

| Transport | WS support |
| --------- | ---------- |
| native `localpost.http` | needs WS framing in a backend; long-lived borrow already fits the borrow/return state machine. Sync handlers; ~hundreds of concurrent sockets per process. |
| WSGI                    | **not supported.** WSGI has no upgrade primitive. `to_wsgi(handler)` raises if any WS routes are registered, or dispatches HTTP-only. |
| ASGI / RSGI             | Protocol-side fit is clean (both have native WS scopes). Out of scope for this sketch. |

## Concurrency model

Sync `WebSocketCtx`. Each connection holds one worker thread for its
lifetime. Suitable for "simplicity first" — users who need 10k concurrent
sockets deploy under an async transport (ASGI/RSGI adapter, future).

Cancellation: same `check_cancelled()` shape as HTTP, called between
`receive()` returns and during `send_*`. Selector-side disconnect detection
already exists for the borrowed-conn case; reuses without changes.

## OpenAPI integration (future)

OpenAPI 3.x doesn't formally describe WebSocket endpoints. Options:

- AsyncAPI 2/3 spec emitted alongside the OpenAPI doc.
- Keep WS routes opaque in the OpenAPI doc (don't list them).
- Vendor extension (`x-websocket: true`) — non-standard but discoverable.

Decision deferred until WS is actually implemented.

## Pre-work in scope of the WSGI plan

The Protocol additions in `plans/wsgi-deployment.md` (`remote_addr`,
`local_addr`, `scheme`) are the same fields `WebSocketCtx` needs. No
extra design work today — just don't paint ourselves into a corner.

## Open questions to revisit before implementing

1. **Backpressure on send.** Native sync impl: blocking write. Is that
   sufficient, or do we need a queue + `try_send`?
2. **Per-message vs streaming frames.** RSGI exposes per-message; we
   probably do too. Continuation frames are server-internal.
3. **Subprotocol negotiation.** Surface as `accept(subprotocol=...)`
   parameter and a `request.headers` lookup helper.
4. **Sentry integration.** A WS connection is one transaction; spans
   per `receive()` / `send_*()`? Defer until use-case is concrete.
