#!/usr/bin/env python

import sys

import anyio

from localpost.hosting import ServiceLifetime, run_app, service


@service
def channel_example():
    channel_writer, channel_reader = anyio.create_memory_object_stream[str]()

    async def svc(lt: ServiceLifetime):
        async def reader():
            async with channel_reader as ch:
                async for message in ch:
                    print(f"Message received: {message}")

        async def writer():
            for i in range(5):
                await anyio.sleep(1)
                await channel_writer.send(f"hello #{i}")
            await channel_writer.aclose()

        lt.tg.start_soon(reader)
        lt.tg.start_soon(writer)
        lt.set_started()
        await lt.shutting_down.wait()

    return svc


if __name__ == "__main__":
    import logging

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    sys.exit(run_app(channel_example()))
