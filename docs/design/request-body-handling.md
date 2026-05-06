# Request body handling across transports

`localpost.http` exposes two surfaces for accessing the request body:
`ctx.body: bytes` (the buffered body) and `ctx.receive(size) -> bytes`
(read up to `size` bytes). Both are present on the sync
[`HTTPReqCtx`](../../localpost/http/_base.py) and async
[`AsyncHTTPReqCtx`](../../localpost/http/_async_base.py) Protocols.
This note explains what `receive(size)` does in each transport — and
why the same Protocol cleanly covers four very different host servers.

## The contract

`receive(size) -> bytes` means "give me up to `size` bytes of the
request body, or `b""` at end-of-message." Nothing more. **What the
source is** depends on what the transport has done before the handler
runs:

| Transport                        | Body pre-buffered?           | `receive(size)` source                       |
|----------------------------------|------------------------------|----------------------------------------------|
| Native h11 (sync) — JSON path    | Yes, by the selector         | Returns `b""` (parser is at EOM; use `ctx.body`) |
| Native h11 (sync) — streaming    | No (`streaming_pool_handler`)| Reads from the socket, chunk-at-a-time       |
| WSGI bridge (sync)               | Yes, from `wsgi.input`       | Slices the buffered `ctx.body`               |
| ASGI bridge (async)              | Yes, capped by `max_body_size` | Slices the buffered `ctx.body`             |
| RSGI bridge (async, planned)     | No (RSGI streams natively)   | Reads from the protocol via `async for`      |

The handler doesn't care which row applies. It calls `receive(size)`,
processes whatever it gets, and stops at `b""`. That's the entire
contract.

## What pre-buffers what

The pre-buffer happens *outside* the ctx in every case — the ctx
implements one strategy that matches what its host transport gave it.

### Native h11 (sync)

The h11 parser exposes per-chunk events
([`server_h11.py:422`](../../localpost/http/server_h11.py)):
`receive(size)` pumps `parser.next_event()`, calls `conn.receive(size)`
(i.e. `recv` on the socket) when h11 says `NEED_DATA`, and returns the
next `h11.Data` chunk. **It always reads from the wire** — there's no
internal buffer.

`ctx.body` gets populated by the **selector**, not by the ctx itself,
when a [`RequestHandler`](../../localpost/http/_base.py) returns a
[`BodyHandler`](../../localpost/http/_base.py) continuation. The
selector reads the full body into `ctx.body` *before* invoking the
continuation, then runs it. After that, `parser.their_state ==
h11.DONE`, so a follow-up `ctx.receive(...)` correctly returns `b""`.
This is the JSON-API common case: handler decides "I need the body" by
returning a `BodyHandler`, then reads `ctx.body` directly.

The streaming case ([`streaming_pool_handler`](../../localpost/http/_pool.py))
runs the handler on a borrowed conn *without* pre-buffering — the
handler calls `ctx.receive(size)` to pull chunks off the wire as they
arrive. Use it for streaming uploads / large bodies where buffering is
undesirable.

### WSGI bridge (sync)

WSGI ([`wsgi.py`](../../localpost/http/wsgi.py)) doesn't expose
chunked input — the WSGI server has already buffered the body for us
in `wsgi.input`. The bridge reads it once into `ctx.body`;
`ctx.receive(size)` slices that buffer. There's no wire to read from
on this side.

### ASGI bridge (async)

ASGI ([`asgi.py`](../../localpost/http/asgi.py)) hands us body chunks
via `http.request` events on the receive channel. The bridge consumes
them upfront (capped by `max_body_size`) into `ctx.body` before
dispatch; `ctx.receive(size)` slices the buffered body, same shape as
the WSGI path.

A future "streaming mode" could skip the pre-buffer and have
`ctx.receive(size)` pull from the ASGI receive channel directly — same
contract, different impl. Out of scope for v1.

### RSGI bridge (async, planned)

RSGI exposes `async for chunk in proto` natively. A future
`localpost.http.rsgi` adapter will implement
`AsyncHTTPReqCtx.receive(size)` by pulling from the protocol directly,
not via a pre-buffer. This is the first async transport that would
*stream* on `ctx.receive(...)`. The Protocol contract already covers
it — no API change needed.

## Why the Protocol stays clean

There's no `streaming: bool` flag, no separate `read_chunk()` method,
no two competing surfaces. Just `receive(size)` with one consistent
contract. The transport picks the source that matches what its host
server gave it; the handler stays portable.

The `ctx.body: bytes` attribute is a complementary convenience for
the JSON-API common case: when a transport pre-buffers (the default
on WSGI / ASGI, the JSON path on native), `ctx.body` is the
already-buffered bytes. When a transport doesn't pre-buffer, `ctx.body`
is empty and the handler reads via `receive(size)`.

## User-facing knobs

The one thing that *is* user-visible is the *config*:

- **Native sync** — pick `streaming_pool_handler` (no pre-buffer) vs
  `thread_pool_handler` (pre-buffer) when wiring the server.
- **ASGI** — `to_asgi(handler, max_body_size=N)` controls the
  pre-buffer cap. Bodies above the cap raise 413 before the handler
  runs.

In every case, the handler code reads `ctx.body` or `await
ctx.receive(size)` (or sync `ctx.receive(size)`) without knowing which
transport ran it.
