from collections.abc import Callable, Iterator
from dataclasses import dataclass

import msgspec

import localpost.spec.openapi as openapi_spec
from localpost.http.openapi.app import OpResult
from localpost.http.router import RequestCtx, Response


class Event[T](msgspec.Struct, eq=False, omit_defaults=True):
    data: T
    type: str | None = None
    id: str | None = None
    retry: int | None = None


@dataclass(frozen=True, eq=False, slots=True)
class EventStream[T]:
    source: Iterator[T] | Iterator[Event[T]]
    event_data_converter: Callable[[T], str]


class EventStreamResultConverter:
    def update_doc(self, op: openapi_spec.Operation, doc: openapi_spec.OpenAPI) -> None:
        pass  # TODO Implement, add response content-type, etc.

    def __call__(self, res: OpResult, req: RequestCtx) -> Response:
        pass  # TODO Implement (set SSE headers, run the iterator, etc.)
