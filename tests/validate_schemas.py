"""Validate every tool schema against the JSON Schema meta-schema.

Tool schemas travel with **every** model request, so one malformed schema does
not degrade a single tool — it makes every turn fail. That is not hypothetical:
`required: true` written inside a property (the `defineTool` authoring DSL's
form) instead of a root `required` array made this plugin's provider answer

    400 invalid_request_error: Error validating JSON Schema:
        <ValidationError: "True is not of type 'array'">

for every request, which is exactly what a meta-schema validator reports.

This script is the gate for that class of bug. It validates the schemas the
plugin actually ships, and can validate a whole recorded request instead:

    python3 tests/validate_schemas.py                     # the plugin's own tools
    python3 tests/validate_schemas.py --session <log.jsonl>   # every tool in a request

Needs the `jsonschema` package (the validator the provider's error came from).
Without it the script says so and exits 0, so it never blocks a plain test run:

    python3 -m venv /tmp/v && /tmp/v/bin/pip install jsonschema
    /tmp/v/bin/python tests/validate_schemas.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def plugin_schemas() -> dict[str, object]:
    """Dump the built plugin's tool schemas through Node."""
    script = (
        "import('./lib/index.js').then(m => {"
        "  const out = {};"
        "  for (const spec of m.TOOL_SPECS) out[spec.suffix] = spec.parameters;"
        "  process.stdout.write(JSON.stringify(out));"
        "})"
    )
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def session_schemas(log: Path) -> dict[str, object]:
    """Every tool schema in the first request header of a recorded session."""
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"request/header"' not in line:
            continue
        header = json.loads(line)["data"]["header"]
        return {tool["name"]: tool["parameters"] for tool in header["tools"]}
    raise SystemExit(f"no request/header event in {log}")


def main() -> int:
    """Validate the chosen schema set and report each failure."""
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:
        print("jsonschema is not installed; skipping meta-schema validation.")
        print("  python3 -m venv /tmp/v && /tmp/v/bin/pip install jsonschema")
        return 0

    if "--session" in sys.argv:
        log = Path(sys.argv[sys.argv.index("--session") + 1])
        schemas = session_schemas(log)
        label = f"{len(schemas)} tools from {log.name}"
    else:
        schemas = {f"cu_{name}": schema for name, schema in plugin_schemas().items()}
        label = f"{len(schemas)} dsh-cu tools"

    failures: list[tuple[str, str]] = []
    for name, schema in schemas.items():
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            failures.append((name, exc.message))

    print(f"meta-schema validation: {label}")
    for name, message in failures:
        print(f"  FAIL {name}: {message}")
    if failures:
        print(f"{len(failures)} of {len(schemas)} schemas are invalid")
        return 1
    print(f"  all {len(schemas)} schemas are valid JSON Schema 2020-12")
    return 0


if __name__ == "__main__":
    sys.exit(main())
