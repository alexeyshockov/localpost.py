# HTTP server backends: h11 and httptools

`localpost.http` ships two parser implementations that live
side-by-side. They share the listening socket, selector loop, op
queue, stale-conn sweep, and shutdown coordination (everything in
`_base.py`). They differ only in how they drive the parser.

| `ServerConfig.backend` | Parser    | Extra           | Notes                                 |
| ---------------------- | --------- | --------------- | ------------------------------------- |
| `"h11"` *(default)*    | h11       | `[http-server]` | pure Python, readable                 |
| `"httptools"`          | httptools | `[http-fast]`   | C-based llhttp; faster header parsing |

There is one entry point — `start_http_server(config, handler)` —
and one hosted-service wrapper — `http_server(config, handler)`. The
parser is selected via `ServerConfig.backend`:

```python
from localpost.http import ServerConfig, start_http_server

with start_http_server(
    ServerConfig(backend="httptools"), my_handler
) as server:
    while True:
        server.run()
```

Pick whichever fits — handler code is identical. Both populate the
same neutral `Request` / `NativeResponse` types from
`localpost.http`.

## Why two implementations, not one Protocol

The two backends are intentionally **not** unified behind a parser
Protocol. h11 is pull-events + parse/serialize; httptools is
push-callbacks + parse-only. Forcing one shape over both restricts
the faster backend without buying anything portable, and the share
that *could* be hoisted (`_base.py`) already is.

The full rationale, including alternatives we rejected, is in
[ADR-0002](../adr/0002-h11-httptools-coexist.md).

## httptools backend caveats (initial scope)

- `Content-Length` response bodies only — chunked transfer-encoding
  is a follow-up. `Router` and `wrap_wsgi` already set
  `Content-Length`, so this matches today's behaviour.
- HTTP Upgrade negotiation surfaces as 400 Bad Request — revisit
  if/when WebSockets come back on the roadmap.

For perf context, see
[`benchmarks/http/PERF_FINDINGS.md`](../../benchmarks/http/PERF_FINDINGS.md).
