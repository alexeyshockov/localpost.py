#!/usr/bin/env python

from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI  # noqa

from localpost.scheduler import Scheduler, delay, every

scheduler = Scheduler()


@scheduler.task(every(timedelta(seconds=3)) // delay((1, 5)))
async def heavy_background_task():
    print("Some work here")


@asynccontextmanager
async def lifespan(_: FastAPI):
    from localpost.scheduler import aserve

    # Scheduler in the same thread, same event loop
    async with aserve(scheduler):
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/predict")
async def predict():
    return {"result": "some prediction"}


if __name__ == "__main__":
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    log_config = LOGGING_CONFIG
    log_config["loggers"]["localpost"] = {"level": "DEBUG", "handlers": ["default"]}

    uvicorn.run(app, log_config=log_config)
