# Vision

## http server
- HTTP 1.1 only, no other versions (e.g. HTTP/2, HTTP/3)
  - so we expose h11 directly
- no WebSocket support (for now)

## Router

- Simple router, very slim layer like Werkzeug or Starlette

## OpenAPI

- build API from Python types


# TODOs

- Brotli middleware

- Converters (for fluent handler, in OpenAPI)
  - werkzeug.Request
  - Pydantic
  - msgspec
  - dataclass
  - attrs
