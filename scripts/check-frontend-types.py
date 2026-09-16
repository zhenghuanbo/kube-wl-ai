#!/usr/bin/env python3
"""Check the hand-written frontend types against the generated OpenAPI schema.

The TypeScript types in ``frontend/src/types`` are written by hand, because the
generated schema is not turned into types. That leaves one real failure mode:
a field is renamed or dropped on the backend, the frontend keeps reading the
old name, and TypeScript is perfectly happy — the value is simply ``undefined``
at runtime and the page renders a blank where a number should be.

This catches that. It reads every ``export interface`` out of ``src/types`` and
requires each declared field to exist in the matching OpenAPI schema component.

Request payloads are checked too, via ``PAYLOAD_ALIASES``: the frontend names
them ``...Payload`` while the backend names them ``...Create``/``...Update``.
This is the direction that matters most, because a misspelled field in a request
body is the failure that has no visible symptom at all.

Only one direction is enforced. A TS field with no backend counterpart is a
bug; a backend field the frontend has not adopted yet is just unused, so it is
reported as informational rather than failing the run.

Frontend-only interfaces (``Page``, ``ApiMessage``, view-local form types) have
no schema component and are skipped. Run from anywhere:

    python3 scripts/check-frontend-types.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TYPES_DIR = ROOT / "frontend" / "src" / "types"
SCHEMA_PATH = ROOT / "frontend" / "openapi" / "schema.json"

INTERFACE_RE = re.compile(r"^export interface (\w+)([^{]*)\{", re.MULTILINE)
EXTENDS_RE = re.compile(r"extends\s+([\w, ]+)")

# The frontend calls a request body `...Payload`; the backend calls the same
# thing `...Create` / `...Update` / `...Launch`. Without this the request
# direction — the one where a typo is invisible — would go unchecked.
PAYLOAD_ALIASES = {
    "ApplicationCreatePayload": "ApplicationCreate",
    "ApplicationUpdatePayload": "ApplicationUpdate",
    "ApplicationLaunchPayload": "ApplicationLaunch",
    "InstanceCreatePayload": "InstanceCreate",
    "InstanceUpdatePayload": "InstanceUpdate",
    "SshKeyCreatePayload": "SshKeyCreate",
    "TaskCreatePayload": "TaskCreate",
    "TaskUpdatePayload": "TaskUpdate",
    "WorkspaceCreatePayload": "WorkspaceCreate",
    "WorkspaceUpdatePayload": "WorkspaceUpdate",
    "WorkspaceMemberAssignPayload": "WorkspaceMemberAssign",
    "WorkspaceResourceGrantPayload": "WorkspaceResourceGrantCreate",
    # Storage and images name their request models after the *action* rather
    # than the verb: registering an image is not "creating" it — the registry
    # already has it — and resizing is not an update.
    "StorageCreatePayload": "StorageCreate",
    "StorageUpdatePayload": "StorageUpdate",
    "ImageRegisterPayload": "ImageRegister",
    "ImageUpdatePayload": "ImageUpdate",
    # Read models: the backend appends `Read` where the frontend does not.
    "WorkloadEvent": "WorkloadEventRead",
    "SshKey": "SshKeyRead",
    "ResourceSpec": "ResourceSpecRead",
    "RuntimeLimit": "RuntimeLimitRead",
    "GpuDevice": "GpuDeviceRead",
    "ComputeNode": "ComputeNodeRead",
    "RegistryStatus": "RegistryStatusRead",
    "Quota": "QuotaRead",
    "WorkspaceMember": "WorkspaceMemberRead",
    "WorkspaceResourceGrant": "WorkspaceResourceGrantRead",
    "UsageTotals": "UsageTotalsRead",
    "UsageDay": "UsageDayRead",
    "UsageSeries": "UsageSeriesRead",
    "UsageBreakdownRow": "UsageBreakdownRowRead",
    "UsageBreakdown": "UsageBreakdownRead",
    "UsageBreakdownDimension": "UsageBreakdownDimensionRead",
    "UsageSummary": "UsageSummaryRead",
    "UsageRollupResult": "UsageRollupResultRead",
    "UsageMeta": "UsageMetaRead",
    # Monitoring names its read models after what they *are* rather than
    # appending `Read`, so the mapping has to be spelled out.
    "MonitorScope": "MonitorScopeRead",
    "MonitorDashboard": "MonitorDashboardRead",
    "MonitorEmbed": "MonitorEmbedRead",
    "MonitorNode": "MonitorNodeSummary",
    # Phase 8. `ApiToken` is the *listing* read model — the issued one is a
    # separate, deliberate type — and the audit row is named for the table.
    "ApiToken": "ApiTokenRead",
    "ApiTokenCreatePayload": "ApiTokenCreate",
    "AuditEntry": "AuditLogRead",
}
# A field line: `name?: type` or `name: type`. Indented, so object literal types
# nested inside a field never match at the start of their own line.
FIELD_RE = re.compile(r"^\s{2,}(\w+)\??\s*:", re.MULTILINE)


def load_schema_properties() -> dict[str, set[str]]:
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    return {
        name: set(component.get("properties", {}))
        for name, component in schema["components"]["schemas"].items()
    }


def parse_interfaces(path: Path) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """Extract declared fields and parent interfaces from one TS file."""
    text = path.read_text(encoding="utf-8")
    found: dict[str, set[str]] = {}
    parents: dict[str, list[str]] = {}

    for match in INTERFACE_RE.finditer(text):
        name = match.group(1)
        extends = EXTENDS_RE.search(match.group(2))
        parents[name] = [base.strip() for base in extends.group(1).split(",")] if extends else []
        # Walk to the matching closing brace so nested braces are handled.
        depth = 1
        index = match.end()
        while index < len(text) and depth > 0:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        body = text[match.end() : index - 1]
        # Strip comments so an example like `// was: fooBar` is not read as a field.
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
        body = re.sub(r"//[^\n]*", "", body)
        found[name] = set(FIELD_RE.findall(body))

    return found, parents


def with_inherited(
    name: str,
    own: dict[str, set[str]],
    parents: dict[str, list[str]],
    seen: frozenset[str] = frozenset(),
) -> set[str]:
    """Union a interface's fields with those it inherits.

    Without this, `interface TaskDetail extends TaskSummary` would look like it
    declares only ten fields, and every inherited field would be misreported as
    one the frontend has not adopted.
    """
    if name in seen:
        return set()
    fields = set(own.get(name, set()))
    for parent in parents.get(name, []):
        fields |= with_inherited(parent, own, parents, seen | {name})
    return fields


def main() -> int:
    if not SCHEMA_PATH.exists():
        print(f"missing {SCHEMA_PATH}; run ./scripts/gen-openapi.sh first", file=sys.stderr)
        return 1

    properties = load_schema_properties()
    checked = 0
    unmapped: list[str] = []
    missing: list[str] = []
    unused: list[str] = []

    for path in sorted(TYPES_DIR.glob("*.ts")):
        own, parents = parse_interfaces(path)
        for interface in sorted(own):
            fields = with_inherited(interface, own, parents)
            if not fields:
                continue
            schema_fields = properties.get(PAYLOAD_ALIASES.get(interface, interface))
            if schema_fields is None:
                unmapped.append(interface)
                continue

            checked += 1
            for field in sorted(fields - schema_fields):
                missing.append(f"{interface}.{field}  ({path.name})")
            for field in sorted(schema_fields - fields):
                unused.append(f"{interface}.{field}")

    print(f"checked {checked} interfaces against {SCHEMA_PATH.name}")

    if unmapped:
        print(f"\n{len(unmapped)} frontend-only interface(s), not checked: {', '.join(unmapped)}")

    if unused:
        print(f"\n{len(unused)} field(s) the frontend does not declare yet (informational):")
        for entry in unused:
            print(f"  {entry}")

    if missing:
        print(f"\n{len(missing)} field(s) declared in TypeScript but absent from the API:")
        for entry in missing:
            print(f"  {entry}")
        print(
            "\nThe backend renamed or removed these. TypeScript cannot see it: the\n"
            "value arrives as undefined and the page silently renders blank.",
            file=sys.stderr,
        )
        return 1

    print("\nOK — every declared field exists in the API schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
