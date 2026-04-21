# localpost.consumers

> **Status:** experimental — API surface is still being shaped; expect breaking changes before `1.0`.

Consumer services for message sources. A consumer is a small `@hosting.service`
wrapper that reads from a source (in-memory channel, AnyIO stream, `queue.Queue`,
Pub/Sub) and dispatches each item to a handler — sync or async — with a
concurrency cap and graceful-shutdown semantics.

## Install

Most consumers need no extras. For the managed brokers:

```bash
pip install localpost[pubsub]           # Google Cloud Pub/Sub
pip install localpost[sqs]              # AWS SQS (adapter WIP; see Status below)
pip install localpost[kafka]            # confluent-kafka (adapter WIP)
pip install localpost[nats]             # nats-py (adapter WIP)
pip install localpost[azure-queue]      # Azure Storage Queue (adapter WIP)
pip install localpost[azure-servicebus] # Azure Service Bus (adapter WIP)
```

## Status

Shipped adapters:

- `channel.py` — in-memory `threadtools.Channel`
- `stream.py` — AnyIO `ObjectReceiveStream`
- `stdlib_queue.py` — stdlib `queue.Queue` / `SimpleQueue`
- `pubsub.py` — Google Cloud Pub/Sub (module imports some internals that are
  being reworked — treat as in-progress)

Adapters for SQS, Kafka, NATS and Azure exist as extras in `pyproject.toml` but
the integrations currently live in [`examples/consumers/`](../../examples/consumers/).
They will move into `localpost.consumers` as they stabilise.

## Quick start

```python
import anyio
from localpost.consumers.stream import stream_consumer
from localpost.hosting import run_app


async def handle(message: str):
    print(f"got: {message}")
    await anyio.sleep(0.5)


async def main():
    send, recv = anyio.create_memory_object_stream[str]()

    async def produce():
        for i in range(20):
            await send.send(f"msg-{i}")
            await anyio.sleep(0.1)
        await send.aclose()

    consumer = stream_consumer(recv, handle, max_concurrency=5)
    # `consumer` is a `@hosting.service`; pass it to run_app or await it directly
    async with consumer:
        await produce()


if __name__ == "__main__":
    anyio.run(main)
```

For a full consumer-as-service example, see
[`examples/host/channel.py`](../../examples/host/channel.py).

## Key concepts

- **Handler duality** — every consumer accepts both `SyncHandler[T]` and
  `AsyncHandler[T]` (types from `_utils.py`). Sync handlers are offloaded to
  a thread pool (sized by `max_concurrency`). Async handlers are scheduled in
  the consumer's task group.
- **`max_concurrency`** — a semaphore around in-flight items. The puller blocks
  once the cap is reached, so back-pressure flows to the source.
- **`process_leftovers`** — on shutdown, finish items that were already pulled
  from the source (`True`, default) or drop them (`False`).
- **Async-context-manager lifecycle** — every consumer is an async CM. Entering
  it starts the puller; leaving it drains (or cancels) remaining work.
- **Thread-bridge** — `stdlib_queue` and `channel` run the pull loop in a
  worker thread (AnyIO `to_thread.run_sync`) and dispatch into the async task
  group via `from_thread.run_sync`.

## Public API

| Symbol                                                | Source               |
| ----------------------------------------------------- | -------------------- |
| `channel_consumer(stream, handler, *, max_concurrency, process_leftovers=True)` | `channel.py` |
| `stream_consumer(stream, handler, *, max_concurrency=inf, process_leftovers=True)` | `stream.py` |
| `queue_consumer(queue, handler, *, max_concurrency, process_leftovers=True, shutdown_timeout=5.0)` | `stdlib_queue.py` |
| `pubsub_consumer(subscription_path, *, num_consumers=1)` | `pubsub.py`       |
| `SyncHandler[T]`, `AsyncHandler[T]`, `AnyHandler[T]`  | `_utils.py`          |

## Writing a new consumer

A consumer is a function decorated with `@hosting.service` that yields once it
has started. Pattern from `stream.py`:

```python
from anyio import Semaphore, create_task_group
from localpost import hosting
from localpost.consumers._utils import AnyHandler, ensure_async_handler


@hosting.service
async def my_consumer(source, h: AnyHandler, /, *, max_concurrency: int):
    sem = Semaphore(max_concurrency)
    handler = ensure_async_handler(h)

    async def handle(item):
        try:
            await handler(item)
        finally:
            sem.release()

    async def pull():
        await sem.acquire()
        async for item in source:
            tg.start_soon(handle, item)
            await sem.acquire()

    async with create_task_group() as tg:
        tg.start_soon(pull)
        yield                     # service is ready; wait for shutdown here
```

Accept a source, a handler, and at least `max_concurrency`. Honour
`process_leftovers` if your source can be closed mid-flight.

## See also

- Examples: [`examples/consumers/`](../../examples/consumers/) (SQS, Kafka stubs)
- Channel + host: [`examples/host/channel.py`](../../examples/host/channel.py)
- Sync primitives: [`../threadtools.py`](../threadtools.py)
