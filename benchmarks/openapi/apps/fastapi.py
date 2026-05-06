"""FastAPI served by Uvicorn (1 worker).

Idiomatic FastAPI: Pydantic v2 models + ``response_model=...`` so the
typed response goes through Pydantic serialization. Validation failures
return 422 by default — what the bench's ``validation_failure``
scenario expects.
"""

from __future__ import annotations

import sys
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from benchmarks.openapi.apps._cli import parse_port


class Item(BaseModel):
    id: int


class SearchResult(BaseModel):
    q: str
    limit: int
    offset: int


class ProfileUpdate(BaseModel):
    display_name: str
    email: str
    version: int
    tags: list[str]
    settings: dict[str, Any]


class Profile(BaseModel):
    user_id: str
    display_name: str
    email: str
    version: int
    tags: list[str]
    settings: dict[str, Any]


def build_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/ping", response_class=PlainTextResponse)
    def ping() -> str:
        return "pong"

    @app.get("/items/{item_id}", response_model=Item)
    def get_item(item_id: int) -> Item:
        return Item(id=item_id)

    @app.get("/search", response_model=SearchResult)
    def search(q: str, limit: int = 20, offset: int = 0) -> SearchResult:
        return SearchResult(q=q, limit=limit, offset=offset)

    @app.post("/users/{user_id}/profile", response_model=Profile)
    def update_profile(user_id: str, body: ProfileUpdate) -> Profile:
        tags = sorted({t.strip().lower() for t in body.tags if t.strip()})
        return Profile(
            user_id=user_id,
            display_name=body.display_name.strip(),
            email=body.email.strip().lower(),
            version=body.version + 1,
            tags=tags,
            settings=body.settings,
        )

    _ = (ping, get_item, search, update_profile)
    return app


def main() -> int:
    port = parse_port()
    uvicorn.run(
        build_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
