#!/usr/bin/env python

from localpost.experimental.consumers.sqs import lambda_handler, sqs_queue_consumer


@sqs_queue_consumer("weather-forecasts")
@lambda_handler
async def handle_message(event, context):
    import random

    import anyio

    messages = event["Records"]
    print(f"Messages received: {messages}")
    await anyio.sleep(random.uniform(0.01, 1.5))  # Simulate some work


if __name__ == "__main__":
    import logging

    import localpost

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(handle_message))
