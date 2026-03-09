from __future__ import annotations

import inspect
from collections.abc import Collection, Iterable
from typing import Any, Protocol

from pydantic import BaseModel, Field, TypeAdapter

import localpost.spec.openapi as openapi_spec
from localpost.http.openapi.app import BadRequest, OpResult
from localpost.http.router import RequestCtx, Response


class PydanticBodyConverter:
    def __init__(self, model: type[BaseModel]):
        self.model = model

    def update_doc(self, op: openapi_spec.Operation, doc: openapi_spec.OpenAPI) -> None:
        pass  # TODO Implement, add schema

    def __call__(self, req: RequestCtx) -> BaseModel | BadRequest[str]:
        return self.model.model_validate_json(req.body())


class PydanticResultConverter:
    def __init__(self, model: type[BaseModel]):
        self.model = model

    def update_doc(self, op: openapi_spec.Operation, doc: openapi_spec.OpenAPI) -> None:
        pass  # TODO Implement, add schema

    def __call__(self, res: OpResult, req: RequestCtx) -> Response:
        body = res.body
        assert isinstance(body, BaseModel), f"Expected {self.model.__name__}, got {type(body).__name__}"
        # TODO Handle PydanticSerializationError
        # TODO Do it via TypeAdapter, to get bytes directly
        # return [res.model_dump_json().encode()]
        return Response(res.status_code, res.headers, [body.model_dump_json().encode()])
