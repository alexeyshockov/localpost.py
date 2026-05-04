# Compression middleware (dynamic responses)

## Context

We added `static_handler` (zero-copy `socket.sendfile()`) in
`plans/static-files.md`. The follow-up question was whether one
middleware could compress *both* static and dynamic responses.
Answer: not without giving up sendfile — compression and zero-copy
are at odds. Cleanest split:

- **Dynamic path (this plan):** a `compress_handler` middleware that
  intercepts `complete(response, body)`, compresses the body, and
  rewrites headers.
- **Static path (future, not this plan):** opt-in `precompressed=`
  on `static_handler` that picks `foo.css.br` / `foo.css.zst` off
  disk and `sendfile` s it. Build- or deploy-time compression, no
  per-request CPU. Re-uses the same negotiation helper.

## Design

### Public API

```python
def compress_handler(
    inner: RequestHandler,
    *,
    algorithms: Sequence[str] = ("br", "gzip"),   # server preference order
    min_size: int = 1024,
    compressible_types: frozenset[bytes] = DEFAULT_COMPRESSIBLE_TYPES,
) -> RequestHandler: ...
```

Standard `Middleware` shape (`Callable[[RequestHandler], RequestHandler]`).
Defaults:

- `algorithms = ("br", "gzip")` — brotli first (better ratio for text);
  gzip as universal fallback. zstd added later when `compression.zstd`
  (Python 3.14 stdlib) is the floor.
- `min_size = 1024` — below this, framing overhead dominates compression
  savings.
- `compressible_types` — allowlist of exact lowercased main-types
  (text/*, JSON, XML, JS, SVG, YAML, manifest+json…).

`brotli` is optional via the new `[http-compress]` extra. If `"br"` is
in `algorithms` but the dependency is missing, `compress_handler`
raises `ImportError` at construction time (fail fast — don't surprise
the user mid-request).

### Mechanism — wrap the ctx

The middleware can't observe `ctx.complete(...)` from outside the
handler call, so it passes a **proxy ctx** to the inner handler:

```
inner(proxy_ctx)
   ↓
proxy_ctx.complete(response, body)
   ↓
[decision + body compress + header rewrite]
   ↓
real_ctx.complete(rewritten_response, compressed_body)
```

`_CompressedCtx` forwards every attribute / method (`request`, `body`,
`attrs`, `selector`, `conn`, `borrowed`, `borrow`, `receive`,
`response_status`, `start_response`, `send`, `finish_response`,
`sendfile`) verbatim, and intercepts only `complete()`.

`start_response` / `send` / `finish_response` / `sendfile` pass through
**uncompressed** in v1. Streaming compression (chunked-encoded SSE,
generator responses) is a separate piece of work; sendfile compression
is intentionally out of scope (zero-copy rationale).

### Decision logic — per-request, ordered

The middleware skips compression when any of these hold:

| Condition | Why |
|-----------|-----|
| `Accept-Encoding` absent or no `algorithms` entry has q>0 | Client doesn't want it |
| `method == b"HEAD"` | No body to compress; faking one to compute length is silly |
| `body is None` or `len(body) < min_size` | Below threshold; framing dominates |
| Status in `{204, 304}` or `1xx` | No body |
| Status `206` | Range — compressing breaks byte-range semantics |
| Existing `Content-Encoding` (not `identity` or empty) | Don't double-compress |
| `Cache-Control: no-transform` | Honor the directive (RFC 9111 §5.2.1.6) |
| `Content-Type` main-type not in `compressible_types` | Allowlist miss |

When eligible: compress, then **rewrite headers** —

- replace `Content-Length` with the compressed length
- add `Content-Encoding: <enc>`
- merge `Accept-Encoding` into `Vary` (existing `Vary: Cookie` →
  `Vary: Cookie, Accept-Encoding`; existing `Vary: *` left alone)

### Negotiation helper

```python
def _negotiate(accept_encoding: bytes, server_pref: Sequence[str]) -> str | None: ...
```

Parses comma-separated tokens with optional `;q=<float>`, default q=1.0.
`q=0` disables, `*` is a wildcard, `identity` is "no compression".
Returns the highest-server-preference encoding with q>0, or `None`.

This same helper will back precompressed-sidecar negotiation in
`static_handler` later — the negotiation logic is identical, only the
"deliver the bytes" step differs.

### Default compressible types

```python
DEFAULT_COMPRESSIBLE_TYPES = frozenset({
    b"text/html", b"text/plain", b"text/css", b"text/xml", b"text/csv",
    b"text/javascript",
    b"application/json", b"application/javascript", b"application/xml",
    b"application/xhtml+xml", b"application/manifest+json",
    b"application/x-yaml", b"application/rss+xml", b"application/atom+xml",
    b"image/svg+xml",
})
```

Exact main-type match: split `Content-Type` on `;`, lowercase, lookup.
Users override by passing their own `frozenset` to `compress_handler`.

## Plan

### Step 1 — `compress.py`

1. New module `localpost/http/compress.py` with:
   - `compress_handler(inner, *, algorithms, min_size, compressible_types)` — public.
   - `_CompressedCtx` — proxy. Forwards all attrs/methods to
     `_inner`; overrides `complete`.
   - `_negotiate`, `_rewrite_headers`, `_compress(encoding, body)`,
     `_should_compress(method, response, body, min_size,
     compressible_types)`.
   - `DEFAULT_COMPRESSIBLE_TYPES`.
2. Re-export `compress_handler` and `DEFAULT_COMPRESSIBLE_TYPES` from
   `localpost/http/__init__.py`.
3. Add `[http-compress]` extra to `pyproject.toml`:

   ```toml
   http-compress = [
       "brotli ~=1.1",
   ]
   ```

4. Run `just check localpost/http/compress.py`.

### Step 2 — Tests

`tests/http/compress.py`:
- Negotiation: empty / single / multiple / q-values / wildcard /
  identity-only / unsupported.
- Allowlist hit/miss (HTML, JSON, image/png, application/octet-stream,
  user-supplied set).
- Skip conditions: HEAD, 204/304/206, `Cache-Control: no-transform`,
  existing `Content-Encoding`, body < min_size, body is None.
- Header rewrite: `Content-Length` replaced; `Content-Encoding` added;
  `Vary` merged (empty / single / `*` cases).
- Round-trip via httpx (auto-decodes `Content-Encoding`) — confirms
  wire format works for gzip; brotli skipped if dep missing.
- Pass-through: `ctx.sendfile(...)` from a static handler stays
  uncompressed when wrapped.
- Pass-through: streaming handler (`start_response` + `send` +
  `finish_response`) stays uncompressed.

Run via `just unit-tests`.

### Step 3 — Docs + example

- New `localpost.http.compress` section in
  `localpost/http/README.md` — placement after `localpost.http.static`.
  Cover the API, decision matrix, and the `Vary` semantics. Note the
  v1 limitation (only `complete()` intercepted).
- New `examples/http/compressed_api.py` — wraps a small JSON router
  with `compress_handler`.

## Step 4 — Streaming compression (SSE etc.)

`compress_handler` was originally only the `complete()` path. This
extension adds the streaming-response path through the same call site
— no new flag, no behavior change for users who don't stream.

### Decision: enable streaming compression when…

`start_response(response)` is called with a **final** Response (not
1xx) and:

- Method != HEAD
- No `Content-Length` declared (a known-length response is contractually
  fixed; compressing it mid-stream would make the declared length wrong)
- No existing `Content-Encoding`
- No `Cache-Control: no-transform`
- Status not in `{1xx, 204, 304, 206}`
- `Content-Type` main-type is in `compressible_types`

Otherwise: pass `start_response` / `send` / `finish_response` through
verbatim.

`text/event-stream` is added to `DEFAULT_COMPRESSIBLE_TYPES`.

### Header rewrite (streaming)

- Drop `Content-Length` (only present if user set it, in which case we
  already passed through; defensive).
- Ensure `Transfer-Encoding: chunked` (httptools auto-frames; h11
  needs it explicit to switch the writer).
- Add `Content-Encoding: <enc>`.
- Merge `Vary: Accept-Encoding` (same as `_merge_vary`).

### Per-chunk semantics

Each `ctx.send(chunk)` (with non-empty `chunk`) is compressed and
followed by a `Z_SYNC_FLUSH`-equivalent (`brotli.Compressor.flush()`)
so the bytes reach the wire promptly:

```
send(chunk):
    out = encoder.compress(chunk) + encoder.flush()
    if out: inner.send(out)

finish_response():
    tail = encoder.finish()
    if tail: inner.send(tail)
    inner.finish_response()
```

Empty `send(b"")` (sometimes used to flush headers without body bytes)
passes through verbatim — no sync marker injected.

This is the same approach nginx uses (`gzip on; gzip_types text/event-stream`).
All major browsers transparently decompress `Content-Encoding: gzip` /
`br` streams before the EventSource parser sees them.

### Encoder interface

A tiny internal Protocol with two implementations:

```python
class _StreamEncoder(Protocol):
    def compress(self, data: bytes) -> bytes: ...
    def flush(self) -> bytes: ...   # mid-stream, sync flush
    def finish(self) -> bytes: ...  # final flush

class _GzipStreamEncoder:
    # zlib.compressobj(level=6, wbits=31)  → gzip-format output
    # flush(Z_SYNC_FLUSH) for mid-stream, flush(Z_FINISH) for end.

class _BrotliStreamEncoder:
    # brotli.Compressor(quality=4)
    # process / flush / finish.
```

### Tests (additional)

- SSE-shaped round-trip: `Accept-Encoding: gzip` →
  `Content-Encoding: gzip`, body decompresses to all events
  concatenated. Same for brotli (skip if dep missing).
- Per-chunk flushing: read raw socket with timeout between sends;
  verify the first event's compressed bytes arrive before the second
  send.
- Pass-through when Content-Length is set on `start_response`.
- Pass-through when content type is not compressible.
- Pass-through when `Cache-Control: no-transform`.
- Empty `send(b"")` doesn't emit sync-marker bytes.

## Followups (separate PRs)

- **zstd** — add when `compression.zstd` (Python 3.14 stdlib) is the
  supported floor, or as a third-party-backed extra
  (`zstandard`) sooner. Same negotiation logic; one more entry in the
  preference order.
- **Per-algorithm levels** — `levels: Mapping[str, int]` parameter.
  Defaults today are gzip `compresslevel=6`, brotli `quality=4` (favor
  speed). Adding the parameter is small but adds a knob.
- **Precompressed sidecars in `static_handler`** — the natural reuse
  of `_negotiate`. When we ship that, the negotiation helper moves to
  a shared location (`localpost/http/_negotiate.py` or similar).
