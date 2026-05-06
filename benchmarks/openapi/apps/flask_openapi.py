"""flask-openapi (v5) served by Gunicorn (1 worker, gthread, 32 threads).

Idiomatic flask-openapi v5: declare per-source Pydantic models for path,
query, body — handler signatures take them as named parameters
(``path``, ``query``, ``body``). Validation failures return 422 by
default (configured via ``validation_error_status``).
"""

from __future__ import annotations

import sys
from typing import Any

from flask import Response, jsonify
from flask_openapi import OpenAPI
from gunicorn.app.base import BaseApplication
from pydantic import BaseModel

from benchmarks.openapi.apps._cli import parse_port


class ItemPath(BaseModel):
    item_id: int


class ProfilePath(BaseModel):
    user_id: str


class SearchQuery(BaseModel):
    q: str
    limit: int = 20
    offset: int = 0


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


def build_app() -> OpenAPI:
    app = OpenAPI(__name__)

    @app.get("/ping")
    def ping() -> Response:
        return Response(b"pong", mimetype="text/plain")

    @app.get("/items/<int:item_id>")
    def get_item(path: ItemPath) -> Response:
        return jsonify(Item(id=path.item_id).model_dump())

    @app.get("/search")
    def search(query: SearchQuery) -> Response:
        return jsonify(SearchResult(q=query.q, limit=query.limit, offset=query.offset).model_dump())

    @app.post("/users/<string:user_id>/profile")
    def update_profile(path: ProfilePath, body: ProfileUpdate) -> Response:
        tags = sorted({t.strip().lower() for t in body.tags if t.strip()})
        result = Profile(
            user_id=path.user_id,
            display_name=body.display_name.strip(),
            email=body.email.strip().lower(),
            version=body.version + 1,
            tags=tags,
            settings=body.settings,
        )
        return jsonify(result.model_dump())

    _ = (ping, get_item, search, update_profile)
    return app


class _GunicornApp(BaseApplication):
    def __init__(self, app, options: dict):
        self._app = app
        self._options = options
        super().__init__()

    def load_config(self) -> None:
        for k, v in self._options.items():
            self.cfg.set(k, v)

    def load(self):
        return self._app


def main() -> int:
    port = parse_port()
    options = {
        "bind": f"127.0.0.1:{port}",
        "workers": 1,
        "threads": 32,
        "worker_class": "gthread",
        "accesslog": None,
        "errorlog": "-",
        "loglevel": "warning",
    }
    _GunicornApp(build_app(), options).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
