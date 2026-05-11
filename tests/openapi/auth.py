"""Tests for ``localpost.openapi.auth`` (HttpBearerAuth, HttpBasicAuth)."""

from __future__ import annotations

import base64

from localpost.openapi import HttpApp, HttpBasicAuth, HttpBearerAuth
from tests.openapi.app import make_ctx, run_op


def _basic_header(user: str, password: str) -> bytes:
    return b"Basic " + base64.b64encode(f"{user}:{password}".encode())


# --- HttpBearerAuth ------------------------------------------------------


class TestHttpBearerAuthRuntime:
    def test_valid_token_passes(self):
        bearer = HttpBearerAuth(validator=lambda t: {"sub": t} if t == "good" else None)
        app = HttpApp(middlewares=[bearer])

        @app.get("/me")
        def me() -> str:
            return "hello"

        op = app.operations[0]
        ctx = make_ctx(path="/me", headers=[(b"authorization", b"Bearer good")])
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert body == b"hello"
        assert ctx.attrs[bearer] == {"sub": "good"}

    def test_invalid_token_returns_401(self):
        bearer = HttpBearerAuth(validator=lambda _t: None)
        app = HttpApp(middlewares=[bearer])

        @app.get("/me")
        def me() -> str:
            return "hello"

        op = app.operations[0]
        ctx = make_ctx(path="/me", headers=[(b"authorization", b"Bearer anything")])
        status, body, _ = run_op(op, ctx)
        assert status == 401
        assert body == b"Invalid token"

    def test_missing_header_returns_401(self):
        bearer = HttpBearerAuth(validator=lambda _t: True)
        app = HttpApp(middlewares=[bearer])

        @app.get("/me")
        def me() -> str:
            return "hello"

        op = app.operations[0]
        status, body, _ = run_op(op, make_ctx(path="/me"))
        assert status == 401
        assert b"Missing or malformed" in body

    def test_non_bearer_scheme_returns_401(self):
        bearer = HttpBearerAuth(validator=lambda _t: True)
        app = HttpApp(middlewares=[bearer])

        @app.get("/me")
        def me() -> str:
            return "hello"

        op = app.operations[0]
        ctx = make_ctx(path="/me", headers=[(b"authorization", _basic_header("u", "p"))])
        status, _, _ = run_op(op, ctx)
        assert status == 401


class TestHttpBearerAuthSpec:
    def test_security_scheme_in_components(self):
        bearer = HttpBearerAuth(validator=lambda _t: True, bearer_format="opaque")
        app = HttpApp(middlewares=[bearer])

        @app.get("/me")
        def me() -> str:
            return "hello"

        doc = app.openapi_doc.to_dict()
        assert doc["components"]["securitySchemes"]["bearerAuth"] == {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque",
        }

    def test_operation_gets_security_and_401(self):
        bearer = HttpBearerAuth(validator=lambda _t: True)
        app = HttpApp(middlewares=[bearer])

        @app.get("/me")
        def me() -> str:
            return "hello"

        op = app.openapi_doc.to_dict()["paths"]["/me"]["get"]
        assert op["security"] == [{"bearerAuth": []}]
        # 401 response is contributed by the @op_middleware wrapper from
        # the middleware's `Unauthorized[str]` return annotation — body
        # schema included.
        assert op["responses"]["401"]["description"] == "Unauthorized"
        assert op["responses"]["401"]["content"]["application/json"]["schema"] == {"type": "string"}

    def test_per_op_does_not_protect_other_ops(self):
        bearer = HttpBearerAuth(validator=lambda _t: True)
        app = HttpApp()

        @app.get("/protected", middlewares=[bearer])
        def protected() -> str:
            return "x"

        @app.get("/public")
        def public() -> str:
            return "y"

        doc = app.openapi_doc.to_dict()
        assert "security" in doc["paths"]["/protected"]["get"]
        assert "security" not in doc["paths"]["/public"]["get"]
        # Scheme is registered once at root regardless of attachment scope.
        assert "bearerAuth" in doc["components"]["securitySchemes"]

    def test_custom_scheme_name(self):
        bearer = HttpBearerAuth(validator=lambda _t: True, scheme_name="apiToken")
        app = HttpApp(middlewares=[bearer])

        @app.get("/me")
        def me() -> str:
            return "hello"

        doc = app.openapi_doc.to_dict()
        assert "apiToken" in doc["components"]["securitySchemes"]
        assert doc["paths"]["/me"]["get"]["security"] == [{"apiToken": []}]


# --- HttpBasicAuth -------------------------------------------------------


class TestHttpBasicAuthRuntime:
    def test_valid_credentials_pass(self):
        basic = HttpBasicAuth(validator=lambda u, p: u if (u, p) == ("alice", "wonder") else None)
        app = HttpApp(middlewares=[basic])

        @app.get("/me")
        def me() -> str:
            return "ok"

        op = app.operations[0]
        ctx = make_ctx(
            path="/me",
            headers=[(b"authorization", _basic_header("alice", "wonder"))],
        )
        status, body, _ = run_op(op, ctx)
        assert status == 200
        assert body == b"ok"
        assert ctx.attrs[basic] == "alice"

    def test_invalid_credentials_returns_401_with_challenge(self):
        basic = HttpBasicAuth(validator=lambda _u, _p: None)
        app = HttpApp(middlewares=[basic])

        @app.get("/me")
        def me() -> str:
            return "ok"

        op = app.operations[0]
        ctx = make_ctx(
            path="/me",
            headers=[(b"authorization", _basic_header("u", "p"))],
        )
        status, _, headers = run_op(op, ctx)
        assert status == 401
        assert headers["WWW-Authenticate"].startswith("Basic realm=")

    def test_missing_header_returns_401_with_challenge(self):
        basic = HttpBasicAuth(validator=lambda _u, _p: True)
        app = HttpApp(middlewares=[basic])

        @app.get("/me")
        def me() -> str:
            return "ok"

        op = app.operations[0]
        status, _, headers = run_op(op, make_ctx(path="/me"))
        assert status == 401
        assert "WWW-Authenticate" in headers

    def test_malformed_base64_returns_401(self):
        basic = HttpBasicAuth(validator=lambda _u, _p: True)
        app = HttpApp(middlewares=[basic])

        @app.get("/me")
        def me() -> str:
            return "ok"

        op = app.operations[0]
        ctx = make_ctx(path="/me", headers=[(b"authorization", b"Basic !!!not-b64!!!")])
        status, body, _ = run_op(op, ctx)
        assert status == 401
        assert b"Malformed" in body

    def test_credentials_without_colon_returns_401(self):
        basic = HttpBasicAuth(validator=lambda _u, _p: True)
        app = HttpApp(middlewares=[basic])

        @app.get("/me")
        def me() -> str:
            return "ok"

        op = app.operations[0]
        creds = base64.b64encode(b"no-colon-here").decode()
        ctx = make_ctx(
            path="/me",
            headers=[(b"authorization", f"Basic {creds}".encode())],
        )
        status, body, _ = run_op(op, ctx)
        assert status == 401
        assert b"Malformed" in body


class TestHttpBasicAuthSpec:
    def test_security_scheme_in_components(self):
        basic = HttpBasicAuth(validator=lambda _u, _p: True)
        app = HttpApp(middlewares=[basic])

        @app.get("/me")
        def me() -> str:
            return "ok"

        doc = app.openapi_doc.to_dict()
        assert doc["components"]["securitySchemes"]["basicAuth"] == {
            "type": "http",
            "scheme": "basic",
        }

    def test_operation_gets_security_and_401(self):
        basic = HttpBasicAuth(validator=lambda _u, _p: True)
        app = HttpApp(middlewares=[basic])

        @app.get("/me")
        def me() -> str:
            return "ok"

        op = app.openapi_doc.to_dict()["paths"]["/me"]["get"]
        assert op["security"] == [{"basicAuth": []}]
        assert op["responses"]["401"]["description"] == "Unauthorized"
        assert op["responses"]["401"]["content"]["application/json"]["schema"] == {"type": "string"}


# --- Integration: mixing two middlewares in one app ---------------------


class TestMixedAuth:
    def test_two_middlewares_register_both_schemes(self):
        bearer = HttpBearerAuth(validator=lambda _t: True)
        basic = HttpBasicAuth(validator=lambda _u, _p: True)
        app = HttpApp()

        @app.get("/jwt", middlewares=[bearer])
        def jwt() -> str:
            return "j"

        @app.get("/basic", middlewares=[basic])
        def b() -> str:
            return "b"

        doc = app.openapi_doc.to_dict()
        schemes = doc["components"]["securitySchemes"]
        assert {"bearerAuth", "basicAuth"} <= set(schemes)
        assert doc["paths"]["/jwt"]["get"]["security"] == [{"bearerAuth": []}]
        assert doc["paths"]["/basic"]["get"]["security"] == [{"basicAuth": []}]
