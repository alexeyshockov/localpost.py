# Static file serving

## Context

The HTTP server (`localpost/http/`) is tuned for the JSON-API common case
— small bodies, one-chunk responses, headers + first body chunk coalesced
into a single `sendall` (`server_h11.py:420`, `server_httptools.py:639`).
Serving static files through this path means reading every byte into
Python and shipping it through `send` — no zero-copy, no Range, no
conditionals, no cache-control story.

The user's deployment is **CDN-fronted** (CloudFront / Cloud CDN /
Cloudflare). With `Cache-Control: public, max-age=…, immutable` on
fingerprinted assets, origin is hit roughly once per file per edge per
forever. That shrinks the optimization target:

- **In scope (v1):** zero-copy body via `socket.sendfile()`, conditional
  GET (`ETag` / `If-None-Match`, `Last-Modified` / `If-Modified-Since`),
  single-range support, `Cache-Control` passthrough.
- **Out of scope (CDN handles them, or rare):** on-the-fly compression,
  precompressed sidecars (`foo.css.br`, `foo.css.zst`), multipart byte
  ranges, symlink-escape hardening beyond `Path.resolve()`.

## Design

### `static_handler` — public entry

New module: `localpost/http/static.py`. Exports a single function:

```python
def static_handler(
    root: str | os.PathLike[str],
    *,
    prefix: bytes = b"/",
    cache_control: str | None = None,
    index: str | None = "index.html",
) -> RequestHandler: ...
```

`prefix` is stripped off `ctx.request.path` before resolution — lets the
caller mount the handler under any URL prefix without a router. `root` is
resolved once at construction (`Path(root).resolve(strict=True)`), and
every request path resolves under it via `is_relative_to`.

Returns a closure that satisfies `RequestHandler` —
`Callable[[HTTPReqCtx], BodyHandler | None]`.

### Pre-body / body split (mirrors `Router`)

Stat happens once in pre-body. Decisions that don't need the body run
inline on the selector via `ctx.complete()`; the actual sendfile runs in
a returned `BodyHandler` (which `thread_pool_handler` will dispatch to a
worker). Open happens in the body handler — no syscall wasted on a 304.

| Outcome | Phase | Action |
|---------|-------|--------|
| 405 wrong method | pre-body, selector | `complete()` with `Allow: GET, HEAD` |
| 404 not found / traversal / not file | pre-body, selector | `complete()` |
| 304 If-None-Match / If-Modified-Since | pre-body, selector | `complete()` |
| 416 unsatisfiable Range | pre-body, selector | `complete()` with `Content-Range: bytes */size` |
| 200 / 206 HEAD | pre-body, selector | `complete()` (no body) |
| 200 / 206 GET | returns `BodyHandler` | sendfile on worker |

### Sendfile path

`socket.sendfile()` requires a blocking socket — non-blocking is
documented as unsupported. The existing `_send_all` blocking-with-timeout
pattern (`_base.py:275`) and `_maybe_give_back` non-blocking reset
(`server_h11.py:340`) already cover the lifecycle:

```python
sock = ctx.conn.sock
sock.settimeout(ctx.selector.config.rw_timeout)
ctx.start_response(Response(status, headers))   # buffers header bytes
ctx.send(b"")                                    # flushes header bytes
with open(path, "rb") as f:
    sock.sendfile(f, offset=range_start, count=range_len)
ctx.finish_response()                            # empty EOM under fixed CL
# give-back path resets timeout to non-blocking
```

`send(b"")` flushes the buffered headers on both backends — verified:
- h11: `parser.send(h11.Data(b""))` returns `b""`, the
  `_pending_header_bytes is not None` branch combines and sends
  (`server_h11.py:425`).
- httptools: `if not chunk and self._pending_header_bytes is None: return`
  is bypassed when headers are pending, falls through to the
  `_pending_header_bytes is not None` send (`server_httptools.py:639`).

No new method on `HTTPReqCtx` Protocol.

### Helpers in `static.py`

- `_resolve(root: Path, url_path: bytes) -> Path | None` — percent-decode,
  reject `..` segments defensively, `Path.resolve()`, then
  `is_relative_to(root_resolved)`. Returns `None` on traversal / missing.
- `_etag(st) -> bytes` — `f'"{st.st_size:x}-{st.st_mtime_ns:x}"'`.
- `_if_none_match(header: bytes, etag: bytes) -> bool` — comma-split,
  trim whitespace, weak-prefix-aware compare.
- `_parse_range(header: bytes, size: int) -> tuple[int, int] | None | "unsatisfiable"`
  — `(start, length)`, `None` for absent / multi-range / unparseable
  (200 fallback per RFC 7233 §3.1), unsatisfiable sentinel for 416.
- `_content_type(path: Path) -> bytes` — `mimetypes.guess_type`,
  `application/octet-stream` fallback.
- `_serve_file(ctx, path, headers, offset, length)` — the sendfile body
  handler.

### Composition (user-visible)

Documented in the README, not built-in:

```python
api    = thread_pool_handler(routes.build().as_handler(), max_concurrency=8)
static = thread_pool_handler(
    static_handler("/var/www", prefix=b"/static/",
                   cache_control="public, max-age=31536000, immutable"),
    max_concurrency=128, backlog=64,
)

def root(ctx):
    return (static if ctx.request.path.startswith(b"/static/") else api)(ctx)

async with http_server(config, root):
    ...
```

Two pools, sized for their workloads — slow-client downloads can't pin
API workers.

## Plan

### Step 1 — `static_handler` skeleton + selector-side outcomes

1. Create `localpost/http/static.py` with `_resolve`, `_etag`,
   `_if_none_match`, `_parse_range`, `_content_type`, and the public
   `static_handler` returning a `RequestHandler` closure.
2. Implement pre-body branches: 405, 404, 304, 416, HEAD-200/206. All via
   `ctx.complete()` inline. Return `None` from those paths.
3. Implement the GET 200 / 206 branch: stat + headers built pre-body,
   `BodyHandler` returned that opens, sets timeout, sendfile, finish.
4. Re-export `static_handler` from `localpost/http/__init__.py`.
5. Run `just check localpost/http/static.py`.

### Step 2 — Tests

`tests/http/test_static.py`:
- Path traversal (`/static/../etc/passwd` → 404).
- Range: full, `0-99`, `100-199`, suffix `-50`, beyond-EOF (416),
  unparseable (200 fallback), multi-range (200 fallback).
- Conditionals: `If-None-Match` hit/miss; `If-Modified-Since` before /
  equal / after mtime.
- HEAD parity: same headers, no body.
- Content-Type: `.html`, `.css`, `.png`, unknown extension.
- Index: directory → `index.html`; missing index → 404.
- Method: `PUT` → 405 + `Allow: GET, HEAD`.
- `Cache-Control` passthrough verbatim.
- Large file (>1 MB) round-trip — exercises sendfile multi-iteration.

Run via `just unit-tests`.

### Step 3 — Docs + example

1. New section in `localpost/http/README.md` covering the handler, the
   sendfile / cache-control story, and the two-pool composition pattern.
2. New `examples/http/static_files.py` — minimal working server.

## Followups (separate PRs / not now)

- **Precompressed sidecars** — `foo.css.br`, `foo.css.zst`. Useful when
  the CDN doesn't negotiate the encoding the client wants (zstd today).
  Pick `path + ".br"` / `".zst"` / `".gz"` if it exists and the client's
  `Accept-Encoding` includes it. Adds a stat-per-encoding to the
  pre-body cost; keep behind an opt-in arg.
- **`compress_handler`** — on-the-fly gzip/br/zstd middleware for the
  *dynamic* path (JSON API responses), for users not behind a CDN.
  Wraps an inner `RequestHandler`, intercepts `complete(...)`,
  compresses, adjusts `Content-Length` + `Content-Encoding`. SSE /
  chunked streaming compression is a further follow-up.
- **Multipart byte ranges** — multi-range `Range:` requests serialised
  as `multipart/byteranges`. Niche; current behaviour is to fall back
  to a 200 full-body response, which is RFC-compliant.
- **Per-route pool API** — currently composition is the user's problem;
  a `Routes` builder that takes a per-route pool argument would let the
  static / API split happen behind one router instead of a hand-rolled
  dispatcher.
- **Symlink-escape hardening** — current `Path.resolve()` follows
  symlinks before the `is_relative_to` check, which is correct, but
  some deployments want symlinks rejected outright. Opt-in flag.
