"""JSON-Import für Firewall-Aliasse.

JSON-Format (UTF-8): eine Liste von Objekten.

```json
[
  {"name": "branch_ips", "type": "host", "content": ["1.1.1.1", "2.2.2.2"], "descr": "Lab"},
  {"name": "lab_ports", "type": "port", "content": [22, 80, 443]}
]
```

Per Eintrag:

* ``name`` und ``type`` sind Pflicht.
* ``content`` ist eine Liste von Strings oder Zahlen. Zahlen werden zu
  Strings konvertiert (Ports / IPs in beliebigem Format).
* ``descr`` ist optional.
* ``merge_mode`` ist optional (``"create"`` oder ``"append"``). Default:
  ``"create"``. Kann via CLI-Schalter ``--append`` global überschrieben werden.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opn_cockpit.core.errors import ValidationError, make_context
from opn_cockpit.core.objects.aliases import AliasSpec, MergeMode
from opn_cockpit.core.validation import validate_alias_name, validate_alias_type


@dataclass(slots=True)
class JsonImportResult:
    specs: list[AliasSpec] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


def parse_aliases_json(
    path: Path | str,
    *,
    override_merge_mode: MergeMode | None = None,
) -> JsonImportResult:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return JsonImportResult(errors=[f"Datei nicht lesbar: {p} ({exc})"])
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return JsonImportResult(errors=[f"JSON nicht parsbar: {exc}"])

    result = JsonImportResult()
    if not isinstance(raw, list):
        result.errors.append("JSON-Wurzel muss eine Liste sein.")
        return result

    for idx, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            found = type(entry).__name__
            result.errors.append(f"Eintrag {idx}: Objekt erwartet, gefunden {found}.")
            continue
        try:
            spec = _entry_to_alias(entry, override_merge_mode)
        except ValidationError as exc:
            result.errors.append(f"Eintrag {idx}: {exc}")
            continue
        result.specs.append(spec)
    return result


def _entry_to_alias(
    raw: dict[str, Any],
    override_merge_mode: MergeMode | None,
) -> AliasSpec:
    name = str(raw.get("name", "")).strip()
    type_value = str(raw.get("type", "")).strip()
    validate_alias_name(name)
    validate_alias_type(type_value)

    content_raw = raw.get("content", [])
    if not isinstance(content_raw, list):
        raise ValidationError(
            "'content' muss eine Liste sein.",
            context=make_context(error_kind="alias_content_type"),
        )
    content = tuple(str(item) for item in content_raw if str(item).strip())

    merge_mode_raw = str(raw.get("merge_mode", "create"))
    inferred_mode: MergeMode = "append" if merge_mode_raw == "append" else "create"
    merge_mode: MergeMode = override_merge_mode or inferred_mode

    return AliasSpec(
        name=name,
        type=type_value,
        content=content,
        descr=str(raw.get("descr", "")),
        merge_mode=merge_mode,
    )
