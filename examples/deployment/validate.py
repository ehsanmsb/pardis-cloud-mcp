"""Offline D0 contract validation. Does not load .env, contact services or execute source."""
import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from pardis_cloud_mcp.deployment import ApplicationBinding, DeploymentPlan, RolePolicy, SourceAssessment


MODELS = {"assessment": SourceAssessment, "application": ApplicationBinding,
          "plan": DeploymentPlan, "policy": RolePolicy}
MAX_BYTES = 128 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=MODELS)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        with args.path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            print("Invalid document: maximum size is 128 KiB.")
            return 1
        document = MODELS[args.kind].model_validate(json.loads(raw))
    except ValidationError as exc:
        # A rejected payload can contain credentials; do not print its values or exception repr.
        print("Invalid document:")
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            path = ".".join(str(part) for part in error["loc"])
            print(f"  {path}: {error['type']}")
        return 1
    except (OSError, UnicodeError, ValueError):
        print("Cannot read a valid UTF-8 JSON document.")
        return 1
    print(document.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
