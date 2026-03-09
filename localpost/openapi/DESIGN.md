# Idea

localpost.http.openapi is slim Python web framework built around OpenAPI spec. The main idea is to use Python type 
system to define HTTP endpoints and infer as much OpenAPI spec from it as possible.

## Router

This is a slim foundation piece, like Werkzeug or Starlette, to communicate with the underlying HTTP server (WSGI, ASGI or our own HTTP server implementation).

Here we define a RequestHandler, a basic interface for HTTP handlers (Request in, Response out).

## Operations

An operation is a Python function, that maps to an HTTP endpoint in OpenAPI terms.

Operation configuration is taken from the function's signature:
- parameters type and annotations
- return type and annotations

An operation in an app is directly converted to a request handler for the Router.

## Arg resolvers

Each function parameter should have a corresponding resolver, that will resolve the value from the request.

Built-in resolver factories:
- FromPath
- FromQuery
- FromHeader
- FromBody

Each arg resolver can end a operation by returning a instance of `OpResult`. Otherwise, the resolver should return a resolved value which will be used as an argument in the function call, for the corresponding parameter.

## Filters

A filter is kinda like a middleware, but limited to the first part of the request handling pipeline.

Each filter can end a operation by returning a instance of `OpResult`. Otherwise, the filter should return `None` to continue the request handling.

## Result converters

A result converter is a function that converts the return value of an operation's function call into a Response instance.

## OpenAPI integration via the type system

Ideally, in most of the applications, the OpenAPI spec should be inferred from the type annotations of the operation functions.

Internally, the mechanism works like this:
- when HttpApp instance is created, an empty OpenAPI doc is created
- when an operation is added to the app, we do two things:
  - wrap the operation's function to create a request handler, for the Router (with all the filters, arg resolvers and result converters inside)
  - modify passed OpenAPI doc to include the operation's details, component schemas, and security schemes

HttpApp router & openapi_doc are both immutable

## OpenAPI spec

OpenAPI spec is a set of Python dataclasses, which represent the OpenAPI specification.

These dataclasses are designed to create an immutable structure.

Usual way to with it:
- create an empty OpenAPI doc instance
- add operations to it (each change creates a new doc instance)
- add security schemes to it (each change creates a new doc instance)
- add components (JSON schemas) to it (each change creates a new doc instance)
