"""Tests for ``localpost.openapi.sse`` — encoder + Operation wiring +
end-to-end streaming."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from dataclasses import dataclass

import httpx

from localpost.openapi import Event, EventStream, HttpApp
from localpost.openapi.sse import encode_event, format_data_field, iter_events


@dataclass
class Update:
    msg: str


# --- Encoder unit tests --------------------------------------------------


class TestEncoder:
    def test_string_payload_passes_through(self):
        out = encode_event("hi")
        assert out == b"data: hi\n\n"

    def test_dict_payload_json_encoded(self):
        out = encode_event({"k": "v"})
        assert out == b'data: {"k":"v"}\n\n'

    def test_event_with_id_and_event_field(self):
        out = encode_event(Event(data="payload", event="update", id="42"))
        # Order: comment, event, id, retry, data
        assert out == b"event: update\nid: 42\ndata: payload\n\n"

    def test_event_with_retry(self):
        out = encode_event(Event(data="x", retry=5000))
        assert out == b"retry: 5000\ndata: x\n\n"

    def test_event_with_comment(self):
        out = encode_event(Event(data="ping", comment="keep-alive"))
        assert out == b": keep-alive\ndata: ping\n\n"

    def test_multi_line_payload_emits_multiple_data_lines(self):
        out = encode_event("line1\nline2\nline3")
        assert out == b"data: line1\ndata: line2\ndata: line3\n\n"

    def test_format_data_field_handles_bytes(self):
        assert format_data_field(b"hi") == "data: hi"

    def test_dataclass_payload_json_encoded(self):
        out = encode_event(Update(msg="hello"))
        assert out == b'data: {"msg":"hello"}\n\n'


class TestIterEvents:
    def test_iterates_events_from_generator(self):
        def gen():
            yield "a"
            yield "b"

        chunks = list(iter_events(gen()))
        assert chunks == [b"data: a\n\n", b"data: b\n\n"]

    def test_iterates_events_from_event_stream(self):
        def gen():
            yield Event(data="x", id="1")
            yield Event(data="y", id="2")

        chunks = list(iter_events(EventStream(gen())))
        assert chunks == [b"id: 1\ndata: x\n\n", b"id: 2\ndata: y\n\n"]


# --- OpenAPI doc tests ---------------------------------------------------


class TestSseSpec:
    def test_generator_return_emits_text_event_stream(self):
        app = HttpApp()

        @app.get("/feed")
        def feed() -> Generator[Update]:
            yield Update(msg="x")

        doc = app.openapi_doc.to_dict()
        responses = doc["paths"]["/feed"]["get"]["responses"]
        assert "text/event-stream" in responses["200"]["content"]
        assert "application/json" not in responses["200"]["content"]

    def test_event_typed_generator_uses_event_schema(self):
        app = HttpApp()

        @app.get("/feed")
        def feed() -> Generator[Event[Update]]:
            yield Event(data=Update(msg="x"))

        doc = app.openapi_doc.to_dict()
        schema = doc["paths"]["/feed"]["get"]["responses"]["200"]["content"]["text/event-stream"]["schema"]
        assert schema["$ref"].startswith("#/components/schemas/Event")

    def test_iterator_return_also_works(self):
        app = HttpApp()

        @app.get("/feed")
        def feed() -> Iterator[Update]:
            return iter([Update(msg="x")])

        doc = app.openapi_doc.to_dict()
        assert "text/event-stream" in (doc["paths"]["/feed"]["get"]["responses"]["200"]["content"])

    def test_event_stream_return_also_works(self):
        app = HttpApp()

        @app.get("/feed")
        def feed() -> EventStream[Update]:
            return EventStream(iter([Update(msg="x")]))

        doc = app.openapi_doc.to_dict()
        assert "text/event-stream" in (doc["paths"]["/feed"]["get"]["responses"]["200"]["content"])


# --- End-to-end integration ----------------------------------------------


class TestSseIntegration:
    def test_streams_events_over_http(self, serve_app):
        app = HttpApp()

        @app.get("/feed")
        def feed() -> Generator[Event[Update]]:
            for i in range(3):
                yield Event(data=Update(msg=f"tick {i}"), id=str(i))

        with serve_app(app) as port:
            with httpx.stream("GET", f"http://127.0.0.1:{port}/feed", timeout=5) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                body = b"".join(resp.iter_bytes())

        # Three events, separated by blank lines, in order.
        events = body.split(b"\n\n")
        non_empty = [e for e in events if e]
        assert len(non_empty) == 3
        assert b"tick 0" in non_empty[0]
        assert b"id: 0" in non_empty[0]
        assert b"tick 2" in non_empty[2]

    def test_finite_generator_terminates_response(self, serve_app):
        app = HttpApp()

        @app.get("/done")
        def done() -> Generator[str]:
            yield "first"
            yield "last"

        with serve_app(app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/done", timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"data: first\n\ndata: last\n\n"
