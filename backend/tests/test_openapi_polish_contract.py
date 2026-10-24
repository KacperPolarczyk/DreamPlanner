"""Generated OpenAPI descriptions and HTTP metadata contract tests."""

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from src.main import create_app
from src.openapi_pl import SCHEMA_DESCRIPTIONS, TAG_DESCRIPTIONS

EXPECTED_OPERATIONS_SHA256 = (
    "a46eb4c0f96edb82dce2ed860d4ac923b2284fa2eb15fb9f08ad5bf03a4d247d"
)
EXPECTED_FIELDS_SHA256 = (
    "6c8aef38bc85c33a667f6733e58502dd6aef30bdea544a2d9680a3d1df808713"
)


def _schema() -> dict:
    """Generate the API document through the application's public hook."""
    return create_app().openapi()


def test_served_openapi_json_contains_localized_schema() -> None:
    """Confirm the serialized HTTP document uses the localized hook."""
    with TestClient(create_app()) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["description"].startswith("DreamPlanner: API do")
    assert len(schema["paths"]) == 33
    assert len(schema["components"]["schemas"]) == 38


def test_all_public_openapi_descriptions_are_reviewed_polish_metadata() -> None:
    """Check exact component and group coverage plus every description slot."""
    schema = _schema()
    assert schema["info"]["description"].startswith("DreamPlanner: API do")
    assert {tag["name"]: tag["description"] for tag in schema["tags"]} == (
        TAG_DESCRIPTIONS
    )
    assert schema["components"]["securitySchemes"]["BearerAuth"][
        "description"
    ].startswith("Token dostępu JWT")
    components = schema["components"]["schemas"]
    assert {name: item["description"] for name, item in components.items()} == (
        SCHEMA_DESCRIPTIONS
    )
    for component in components.values():
        for field in component.get("properties", {}).values():
            assert field["description"]
    assert len(schema["paths"]) == 33
    assert sum(map(len, schema["paths"].values())) == 47
    for methods in schema["paths"].values():
        for operation in methods.values():
            assert operation["summary"] and operation["description"]
            for parameter in operation.get("parameters", []):
                assert parameter["description"]
            if "requestBody" in operation:
                assert operation["requestBody"]["description"]
            for response in operation["responses"].values():
                assert response["description"]
                for header in response.get("headers", {}).values():
                    assert header["description"]
    rendered = json.dumps(schema, ensure_ascii=False)
    assert "\u2013" not in rendered
    for untranslated in (
        "Successful Response",
        "Invalid access token",
        "Protected responses are not cacheable",
        "Canonical path of the created resource",
        "Confirm process liveness",
        "Return current database fields",
    ):
        assert untranslated not in rendered


def test_paths_methods_and_json_field_names_are_stable() -> None:
    """Freeze identifiers separately from editable Polish prose."""
    schema = _schema()
    operations = sorted(
        (path, method)
        for path, methods in schema["paths"].items()
        for method in methods
    )
    fields = {
        name: sorted(component.get("properties", {}))
        for name, component in schema["components"]["schemas"].items()
    }
    assert hashlib.sha256(json.dumps(operations).encode()).hexdigest() == (
        EXPECTED_OPERATIONS_SHA256
    )
    assert hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest() == (
        EXPECTED_FIELDS_SHA256
    )


def test_response_headers_and_problem_schemas_match_the_http_contract() -> None:
    """Document only headers emitted by each operation's response path."""
    schema = _schema()
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            protected = bool(operation.get("security"))
            public_auth = path.startswith("/api/v1/auth/")
            responses = operation["responses"]
            assert "500" in responses
            assert ("503" in responses) == (
                protected
                or path
                in {
                    "/api/v1/auth/sessions",
                    "/api/v1/auth/sessions/current/refresh",
                }
            )
            for code, response in responses.items():
                headers = response.get("headers", {})
                if (
                    protected
                    or public_auth
                    or (path == "/api/v1/health" and code == "500")
                ):
                    assert headers["Cache-Control"]["schema"]["const"] == "no-store"
                if code == "401":
                    assert ("WWW-Authenticate" in headers) == protected
                    if protected:
                        assert headers["WWW-Authenticate"]["schema"]["const"] == (
                            "Bearer"
                        )
                if code == "201":
                    assert headers["Location"]["schema"]["type"] == "string"
                if code in {
                    "400",
                    "401",
                    "403",
                    "404",
                    "409",
                    "422",
                    "429",
                    "500",
                    "503",
                }:
                    problem = response["content"]["application/problem+json"]["schema"]
                    if "oneOf" in problem:
                        assert code == "422"
                    else:
                        assert set(problem["required"]) >= {
                            "type",
                            "title",
                            "status",
                            "detail",
                        }
            if (method, path) in {
                ("post", "/api/v1/auth/sessions"),
                ("post", "/api/v1/auth/sessions/current/refresh"),
            }:
                cookie = responses["200"]["headers"]["Set-Cookie"]
                assert cookie["schema"] == {"type": "string"}
                assert "value" not in cookie["schema"]


def test_activation_422_describes_both_actual_problem_variants() -> None:
    """Separate field parsing errors from service password validation."""
    schema = _schema()
    for path in ("/api/v1/auth/activations", "/api/v1/auth/reactivations"):
        variants = schema["paths"][path]["post"]["responses"]["422"]["content"][
            "application/problem+json"
        ]["schema"]["oneOf"]
        field_error, service_error = variants
        assert "errors" in field_error["required"]
        assert field_error["properties"]["type"]["const"] == (
            "urn:dreamplanner:problem:validation-error"
        )
        assert field_error["properties"]["status"]["const"] == 422
        assert "errors" not in service_error["properties"]
        assert service_error["additionalProperties"] is False
        assert service_error["properties"]["type"]["const"] == (
            "urn:dreamplanner:problem:validation-error"
        )
        assert service_error["properties"]["status"]["const"] == 422


def test_bigint_identifiers_remain_json_strings() -> None:
    """Keep large identifiers precise for JavaScript API clients."""
    components = _schema()["components"]["schemas"]
    for name, component in components.items():
        for field_name, field in component.get("properties", {}).items():
            if field_name == "id" or field_name.endswith("_id"):
                if name.endswith(("Response", "Summary", "Item")):
                    assert field["type"] == "string", (name, field_name)


def test_checked_source_and_local_contracts_have_no_en_dash() -> None:
    """Inspect source and ignored local contracts while excluding generated files."""
    root = Path(__file__).resolve().parents[1]
    excluded = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    extensions = {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".txt", ".sh"}
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in extensions:
            continue
        if any(part in excluded for part in path.relative_to(root).parts):
            continue
        if "\u2013" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
