"""Round-trip tests for the OpenAPI 3.2 dataclasses."""

from __future__ import annotations

import json

from localpost.openapi import spec


class TestOpenAPIVersion:
    def test_default_version_is_3_2(self):
        assert spec.OpenAPI().openapi == "3.2.0"


class TestOperationOnPath:
    def test_add_operation_creates_path_item(self):
        doc = spec.OpenAPI()
        doc = doc.add_operation("/foo", "get", spec.Operation(operation_id="get_foo"))

        assert "/foo" in doc.paths
        assert "get" in doc.paths["/foo"].operations
        assert doc.paths["/foo"].operations["get"].operation_id == "get_foo"

    def test_add_operation_returns_new_instance(self):
        doc = spec.OpenAPI()
        doc2 = doc.add_operation("/foo", "get", spec.Operation())

        assert doc is not doc2
        assert "/foo" not in doc.paths
        assert "/foo" in doc2.paths

    def test_add_operation_lowercases_method(self):
        doc = spec.OpenAPI().add_operation("/foo", "POST", spec.Operation())

        assert "post" in doc.paths["/foo"].operations
        assert "POST" not in doc.paths["/foo"].operations

    def test_add_operation_merges_methods_on_same_path(self):
        doc = spec.OpenAPI()
        doc = doc.add_operation("/foo", "get", spec.Operation(operation_id="get"))
        doc = doc.add_operation("/foo", "post", spec.Operation(operation_id="post"))

        ops = doc.paths["/foo"].operations
        assert set(ops) == {"get", "post"}


class TestSerialization:
    def test_minimal_doc(self):
        d = spec.OpenAPI().to_dict()
        assert d == {"openapi": "3.2.0", "info": {"title": "API", "version": "0.1.0"}}

    def test_to_json_round_trips(self):
        doc = spec.OpenAPI(info=spec.Info(title="X", version="1"))
        loaded = json.loads(doc.to_json())
        assert loaded["openapi"] == "3.2.0"
        assert loaded["info"] == {"title": "X", "version": "1"}

    def test_components_omitted_when_empty(self):
        d = spec.OpenAPI().to_dict()
        assert "components" not in d

    def test_components_emitted_when_populated(self):
        doc = spec.OpenAPI()
        doc = doc.with_components(spec.Components(schemas={"Book": {"type": "object"}}))
        d = doc.to_dict()
        assert d["components"] == {"schemas": {"Book": {"type": "object"}}}

    def test_parameter_emits_in_field(self):
        param = spec.Parameter(name="id", location="path", required=True, schema={"type": "string"})
        assert param.to_dict() == {
            "name": "id",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }


class TestTagGroups:
    """tagGroups is a 3.2 addition."""

    def test_tag_groups_emitted(self):
        doc = spec.OpenAPI(
            tags=(spec.Tag(name="A"), spec.Tag(name="B")),
            tag_groups=(spec.TagGroup(name="Group", tags=("A", "B")),),
        )
        d = doc.to_dict()
        assert d["tagGroups"] == [{"name": "Group", "tags": ["A", "B"]}]


class TestSecurityScheme:
    def test_http_bearer(self):
        scheme = spec.SecurityScheme(type="http", scheme="bearer", bearer_format="JWT")
        assert scheme.to_dict() == {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}

    def test_oauth2_with_device_flow(self):
        scheme = spec.SecurityScheme(
            type="oauth2",
            flows=spec.OAuthFlows(
                device_authorization=spec.OAuthFlow(
                    device_authorization_url="https://auth.example/device",
                    token_url="https://auth.example/token",  # noqa: S106
                    scopes={"read": "Read access"},
                )
            ),
        )
        d = scheme.to_dict()
        assert d["type"] == "oauth2"
        assert d["flows"]["deviceAuthorization"]["deviceAuthorizationUrl"] == "https://auth.example/device"
