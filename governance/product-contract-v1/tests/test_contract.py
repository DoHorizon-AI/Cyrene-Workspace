"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_contract.py                                                │
│  Module: governance.product_contract_v1.tests.test_contract         │
│  Role: Offline common-profile and registry validation.              │
│                                                                     │
│  模块职责：离线验证公共兼容配置与产品契约注册表。                           │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, validate

CONTRACT_ROOT = Path(__file__).parents[1]


def _json(name: str) -> dict:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def test_common_schemas_are_valid_draft_2020_12() -> None:
    for path in CONTRACT_ROOT.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(schema)


def test_product_event_is_a_resource_notification_not_state_payload() -> None:
    schema = _json("product-event.schema.json")
    event = {
        "specversion": "1.0",
        "id": "event-1",
        "source": "https://catalyst.example/api/v1",
        "type": "dev.cyrene.catalyst.dataset-version.updated.v1",
        "subject": "dataset-versions/11111111-1111-4111-8111-111111111111",
        "time": "2026-08-31T20:00:00Z",
        "datacontenttype": "application/json",
        "dataschema": "https://schemas.cyrene.dev/catalyst/product/v1/dataset-version.schema.json",
        "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
        "data": {
            "resourceUri": "https://catalyst.example/api/v1/dataset-versions/11111111-1111-4111-8111-111111111111",
            "resourceVersion": 2,
            "change": "UPDATED",
        },
    }
    validate(instance=event, schema=schema, format_checker=FormatChecker())
    assert set(event["data"]) == {"resourceUri", "resourceVersion", "change"}


def test_registry_contains_each_product_once() -> None:
    registry_path = CONTRACT_ROOT / "product-contracts-v1.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    validate(
        instance=registry,
        schema=_json("product-contracts-v1.schema.json"),
        format_checker=FormatChecker(),
    )
    ids = [product["id"] for product in registry["products"]]
    assert sorted(ids) == [
        "catalyst",
        "echo",
        "exchange",
        "navigator",
        "reactor",
        "yield",
    ]
