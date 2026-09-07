"""Validate local Product OpenAPI and schema documents. | 离线校验产品接口与结构契约。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args()
    documents = 0
    schemas = 0
    for root in args.roots:
        contract = root / "contracts/product/v1"
        for path in sorted(contract.glob("*openapi.yaml")):
            document, base = read_from_filename(str(path.resolve()))
            validate(document, base_uri=base)
            documents += 1
        for path in sorted(contract.rglob("*.schema.json")):
            Draft202012Validator.check_schema(json.loads(path.read_text()))
            schemas += 1
    print(f"Validated {documents} OpenAPI documents and {schemas} JSON schemas")


if __name__ == "__main__":
    main()
