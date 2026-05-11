# HTTP connection model

How `localpost.http` moves a connection from accept through dispatch to
release, and how the sync and async surfaces stay aligned.

## Dispatch chain

The internals are a three-link, loose-coupled chain:

```
Selector  ── owns fd→SelectorCallback map; nothing HTTP-specific
   │
   ▼
ConnHandler  ── after-accept policy; owns RequestHandler + ConnFactory;
   │              decides which Selector tracks the new conn
   ▼
RequestHandler  ── single-shot dispatch on headers-complete; handlers
                   that need the body call ``ctx.receive(size)`` /
                   ``localpost.http.read_body(ctx)`` themselves
```

Each link knows only the next. `Selector` doesn't carry a
`RequestHandler` — the handler is owned by the `ConnHandler` and
threaded into the conn at construction. This factoring is what makes
the acceptor topology (1 acceptor thread + N worker selectors) drop in
without touching the request hot path.

- **`Selector`** — dumb fd→callback dispatcher. Built-in callbacks:
  `_DrainWakeup` (wakeup pipe), `_AcceptListener` (listen socket), and
  `BaseHTTPConn` itself (a conn *is* its own per-fd callback).
- **`ConnHandler`** — `Callable[[Selector, socket, addr], None]`,
  after-accept policy. Two built-ins: `TrackHere` (default — track on
  the selector that accepted) and `RoundRobinAcceptor` (acceptor
  topology — spread conns across worker selectors via
  `Selector.post_track`).
- All callbacks are spelled as **callable dataclasses** (not
  closures), so their state is `repr`-able for debugging.

## Two-state connection model

A connection is either **TRACKED** (registered in the selector — the
selector owns the fd, parser, and I/O) or **BORROWED** (a worker
thread holds the parser + socket; fd is unregistered). The dispatcher
unregisters before handing off to a worker (`stop_tracking`);
`finish_response` re-registers via `_maybe_give_back`. Two states, no
third "watchdog" mode, no shared mode field for threads to race on.

```
            accept()
                │
                ▼
       ConnHandler builds conn,
       routes to a selector via
         track() or post_track()
                │
                ▼
       ┌──────────────────────┐         ┌──────────┐
       │      TRACKED         │  close  │          │
       │  fd registered       ├────────▶│  CLOSED  │
       │  selector owns I/O   │         │          │
       └──┬───────────────────┘         └──────────┘
          │  ▲                                ▲
   borrow │  │ _maybe_give_back               │ close
          ▼  │                                │
       ┌──────────────────────┐               │
       │      BORROWED        │               │
       │  fd unregistered     ├───────────────┘
       │  worker owns I/O     │
       └──────────────────────┘
```

Both topologies (single selector and acceptor + N worker selectors)
funnel through the same diagram; only the entry edge differs:

| From → to                | Method                 | Caller thread  | Mechanism                                        |
| ------------------------ | ---------------------- | -------------- | ------------------------------------------------ |
| accept → TRACKED         | `Selector.track`       | selector       | inline `selectors.register` (TrackHere topology) |
| accept → TRACKED         | `Selector.post_track`  | acceptor       | op queue + wakeup pipe (RoundRobinAcceptor)      |
| TRACKED → BORROWED       | `stop_tracking`        | selector       | inline `selectors.unregister`                    |
| BORROWED → TRACKED       | `Selector.track`       | worker         | op queue + wakeup pipe                           |
| TRACKED → CLOSED         | `BaseHTTPConn.close`   | selector       | inline (idle / keep-alive / error)               |
| BORROWED → CLOSED        | `close` + `post_close` | worker         | op queue + wakeup pipe (`_fd_to_key` cleanup)    |

The op queue + wakeup pipe is the only cross-thread synchronisation
edge (the `os.write` to the wakeup pipe is a full memory barrier).
After `track()` returns, anything the worker did to the conn's parser
(`h11.Connection.send` / `httptools.HttpRequestParser.feed_data`) is
visible to the selector. After `stop_tracking()` returns, anything
the selector did is visible to the worker. The parser is never
touched concurrently from two threads.

## Pull-based client-disconnect detection

While the worker holds a borrowed connection, client disconnects are
detected on demand: `check_cancelled()` does a non-blocking
`recv(1, MSG_PEEK | MSG_DONTWAIT)` on the request socket. `b""` means
peer FIN — `RequestCancelled` is raised. Handlers that do regular I/O
surface disconnects via `EPIPE` / `ECONNRESET` naturally; handlers
that compute without I/O should call `check_cancelled()` periodically
(same contract as service-shutdown cancellation).

This replaces an earlier push-based design where the selector kept
the socket registered in a third "watchdog" mode and fired EOF
events. That worked but introduced a 3-way state machine with
cross-thread races. The pull-based variant collapses to two states
and one syscall per `check_cancelled()` call. Full rationale in
[ADR-0004](../adr/0004-pull-based-disconnect-detection.md).

## Sync vs. async request-context surface

`HTTPReqCtx` (sync) and `AsyncHTTPReqCtx` (async) share the data side
(`request`, `body`, `attrs`, `response_status`, `remote_addr` /
`local_addr` / `scheme`, `disconnected`) and the terminal write
methods (`complete`, `stream`, `sendfile`, plus `receive` for body
reads). A handler that only touches that core surface is portable
between transports.

A few members are deliberately asymmetric:

| Member                       | Sync | Async | Why |
| ---------------------------- | ---- | ----- | --- |
| `borrow()` / `borrowed`      | yes  | —     | Only the sync native server hands a connection between selector and worker threads. Async transports own the conn for the coroutine's lifetime; there's nothing to borrow. The WSGI bridge satisfies the sync member trivially (always borrowed, no-op CM) so handler code stays portable. |
| `disconnected` poll          | yes  | yes   | Native sync does a non-blocking `MSG_PEEK` per access (sticky once True). WSGI bridge is always False — surface disconnects via `BrokenPipeError` from the host's per-chunk write instead. Async transports flip the flag on `http.disconnect`. |
| `check_cancelled()` (raises) | yes  | —     | Sync handlers without `ctx` in scope can call it from anywhere on the request thread; async code already has `ctx` and uses `ctx.disconnected`. |

The wire-driver trio (`start_response` / `send` / `finish_response`)
is **not** part of the public Protocol on either side. Sync native
backends still use it internally to drive h11 / httptools, and 1xx
informational responses (100 Continue / 102 Processing) are emitted
by the server through that internal path. Handlers express responses
declaratively via `complete()` (one-shot) or `stream(response,
chunks)` (chunk iterator) — both portable across sync and async.

Why the native server doesn't grow an async path of its own (and
why async traffic goes through the ASGI bridge instead) is covered
in [ADR-0003](../adr/0003-sync-native-http-server.md).
