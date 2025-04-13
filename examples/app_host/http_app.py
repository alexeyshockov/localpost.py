#!/usr/bin/env python

from datetime import timedelta

import anyio
from fastapi import FastAPI
from starlette.responses import JSONResponse

from localpost.hosting import AppHost
from localpost.hosting.http import UvicornService
from localpost.scheduler import delay, every, scheduled_task

app = AppHost()

http_api = FastAPI()
app.service(UvicornService.for_app(http_api))


@app.service
async def background_job():
    print("Background job started")
    try:
        while True:
            print("Background job running")
            await anyio.sleep(1)
    finally:
        print("Background job done")


@app.service
@scheduled_task(every(timedelta(seconds=3)) // delay((1, 5)))
async def heavy_periodic_task():
    print("Some periodic work")


@http_api.get("/predict")
async def predict():
    return {"result": "some"}


@http_api.get("/health")
async def health_check() -> JSONResponse:
    return JSONResponse(app.status)


if __name__ == "__main__":
    import logging

    import localpost

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(app))
