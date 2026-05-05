"""Operation results.

Functions registered with :class:`localpost.openapi.HttpApp` return a
plain value (treated as ``200 OK``) or one of the :class:`OpResult`
subclasses below. The class chosen carries both the HTTP status code at
runtime *and* the type-level information the OpenAPI doc builder reads
from the function's return annotation.

Use ``BadRequest[T]``, ``NotFound[T]`` etc. in the return annotation so
the schema generator knows the body type per status code::

    @app.get("/books/{book_id}")
    def get_book(book_id: str) -> Book | NotFound[str]: ...
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from localpost.openapi.sse import Event, EventStream

__all__ = [
    "OpResult",
    "Ok",
    "Created",
    "Accepted",
    "NoContent",
    "EventStreamResult",
    "BadRequest",
    "Unauthorized",
    "Forbidden",
    "NotFound",
    "Conflict",
    "UnprocessableEntity",
    "TooManyRequests",
    "InternalServerError",
]


_EMPTY_HEADERS: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, eq=False, slots=True)
class OpResult:
    """Concrete operation outcome — body + status + headers.

    Subclasses (:class:`Ok`, :class:`BadRequest`, …) override
    :attr:`_status_code` and :attr:`_description`. The class itself is the
    type-level marker the OpenAPI doc reads — keep return annotations
    using the *class*, not the instance::

        def f() -> Book | NotFound[str]:        # ✅ correct
        def f() -> Book | NotFound("nope"):     # ❌ instance, won't type-check
    """

    body: object
    headers: Mapping[str, str] = field(default=_EMPTY_HEADERS)

    _status_code: ClassVar[int] = 200
    _description: ClassVar[str] = "Successful response"

    @property
    def status_code(self) -> int:
        return self._status_code

    @property
    def description(self) -> str:
        return self._description


def _normalize_headers(headers: Mapping[str, str] | None) -> Mapping[str, str]:
    return MappingProxyType(dict(headers)) if headers else _EMPTY_HEADERS


# --- 2xx -----------------------------------------------------------------


@dataclass(frozen=True, eq=False, slots=True, init=False)
class Ok[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 200
    _description: ClassVar[str] = "Successful response"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


@dataclass(frozen=True, eq=False, slots=True, init=False)
class Created[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 201
    _description: ClassVar[str] = "Created"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


@dataclass(frozen=True, eq=False, slots=True, init=False)
class Accepted[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 202
    _description: ClassVar[str] = "Accepted"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


@dataclass(frozen=True, eq=False, slots=True, init=False)
class NoContent(OpResult):
    """No body. Ideal for ``DELETE`` / ``PUT`` returns."""

    _status_code: ClassVar[int] = 204
    _description: ClassVar[str] = "No Content"

    def __init__(self, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", None)
        object.__setattr__(self, "headers", _normalize_headers(headers))


@dataclass(frozen=True, eq=False, slots=True, init=False)
class EventStreamResult[T](OpResult):
    """Server-Sent Events response.

    Body is the stream source — an :class:`~localpost.openapi.sse.EventStream`,
    a generator/iterator of payloads, or an iterable of
    :class:`~localpost.openapi.sse.Event` instances. The response is sent
    with ``Content-Type: text/event-stream`` and chunked transfer encoding;
    one wire event per yielded item.

    Returning a generator/iterator/``EventStream`` directly from an operation
    is auto-promoted to ``EventStreamResult`` — you only construct one
    explicitly to attach extra response headers.
    """

    body: EventStream[T] | Iterator[T] | Iterator[Event[T]] | Iterable[T] | Iterable[Event[T]]

    _status_code: ClassVar[int] = 200
    _description: ClassVar[str] = "Successful response"

    def __init__(
        self,
        body: Any,
        /,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


# --- 4xx -----------------------------------------------------------------


@dataclass(frozen=True, eq=False, slots=True, init=False)
class BadRequest[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 400
    _description: ClassVar[str] = "Bad Request"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


@dataclass(frozen=True, eq=False, slots=True, init=False)
class Unauthorized[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 401
    _description: ClassVar[str] = "Unauthorized"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


@dataclass(frozen=True, eq=False, slots=True, init=False)
class Forbidden[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 403
    _description: ClassVar[str] = "Forbidden"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


@dataclass(frozen=True, eq=False, slots=True, init=False)
class NotFound[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 404
    _description: ClassVar[str] = "Not Found"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


@dataclass(frozen=True, eq=False, slots=True, init=False)
class Conflict[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 409
    _description: ClassVar[str] = "Conflict"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


@dataclass(frozen=True, eq=False, slots=True, init=False)
class UnprocessableEntity[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 422
    _description: ClassVar[str] = "Unprocessable Entity"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


@dataclass(frozen=True, eq=False, slots=True, init=False)
class TooManyRequests[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 429
    _description: ClassVar[str] = "Too Many Requests"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))


# --- 5xx -----------------------------------------------------------------


@dataclass(frozen=True, eq=False, slots=True, init=False)
class InternalServerError[T](OpResult):
    body: T

    _status_code: ClassVar[int] = 500
    _description: ClassVar[str] = "Internal Server Error"

    def __init__(self, body: T, /, *, headers: Mapping[str, str] | None = None) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", _normalize_headers(headers))
