"""``AliasAdapter`` und ``AliasesController`` für Firewall-Aliasse.

Zweite konkrete Implementation des in :mod:`opn_cockpit.core.objects.base`
definierten Protokolls. Bietet zwei Modi:

* **create** — neuen Alias anlegen. Wenn ein Alias gleichen Namens schon
  existiert, ist das ein Konflikt (R-PRE-3 zeigt das in der Vorschau,
  R-RUN-5 markiert Idempotenz wenn Inhalt identisch ist).

* **append** — Inhalt zu einem bestehenden Alias hinzufügen (Merge, R-ACT-2).
  Schlägt fehl, wenn der Alias nicht existiert.

API-Konventionen (mit Schritt 0 / API-Spike final zu verifizieren):

* Suchen:       ``POST /api/firewall/alias/searchItem``
* Anlegen:      ``POST /api/firewall/alias/addItem`` (wirft Fehler bei Namens-Kollision)
* Holen:        ``POST /api/firewall/alias/getItem/{uuid}``
* Aktualisieren: ``POST /api/firewall/alias/setItem/{uuid}``
* Aktivieren:   ``POST /api/firewall/alias/reconfigure``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from opn_cockpit.core.errors import (
    ApiError,
    AuthError,
    EgressDeniedError,
    ReconfigureError,
    UnreachableError,
    ValidationError,
    make_context,
)
from opn_cockpit.core.objects._endpoints import (
    ALIAS_ADD,
    ALIAS_GET,
    ALIAS_RECONFIGURE,
    ALIAS_SEARCH,
    ALIAS_SET,
)
from opn_cockpit.core.objects.base import (
    AddOutcome,
    Diff,
    DiffKind,
    RequestContext,
    VerifyOutcome,
)
from opn_cockpit.core.validation import validate_alias_name, validate_alias_type

if TYPE_CHECKING:
    from opn_cockpit.core.http_client import HttpClient


# ---------------------------------------------------------------------------
# Datentypen
# ---------------------------------------------------------------------------


MergeMode = Literal["create", "append"]


@dataclass(frozen=True, slots=True)
class AliasIdentity:
    name: str


@dataclass(frozen=True, slots=True)
class AliasSpec:
    """Soll-Beschreibung eines Aliases.

    Wenn ``merge_mode == "append"`` wird der ``content`` zu einem bestehenden
    Alias hinzugefügt (Mengen-Union). Wenn ``merge_mode == "create"`` (Default),
    legt der Adapter einen neuen Alias an und scheitert bei Namenskollision.
    """

    name: str
    type: str
    content: tuple[str, ...]
    descr: str = ""
    merge_mode: MergeMode = "create"

    def to_identity(self) -> AliasIdentity:
        return AliasIdentity(name=self.name)


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _content_to_str(content: tuple[str, ...] | list[str]) -> str:
    """OPNsense erwartet den Alias-Inhalt als Newline-separierten String."""
    return "\n".join(str(item) for item in content)


def _raise_if_not_saved(body: Any, path: str, ctx: RequestContext) -> None:
    """OPNsense ``addItem``/``setItem`` antworten oft mit ``200 OK`` plus
    Body ``{"result":"failed","validations":{...}}``. Das ist ein stilles
    No-Op — frueher haben wir das als Erfolg gemeldet, der Eintrag fehlte
    aber spaeter. Hier wird der Body geprueft und im Fehlerfall ein
    sprechender ``ApiError`` geworfen.

    Akzeptiert wird alles, was nicht explizit ein ``failed`` enthaelt — manche
    Endpunkte liefern ``saved``, andere bloss ``uuid``-Felder oder leere Bodies.
    """
    if not isinstance(body, dict):
        return
    result = body.get("result")
    if isinstance(result, str) and result.lower() in {"failed", "error"}:
        validations = body.get("validations")
        detail = ""
        if isinstance(validations, dict) and validations:
            detail = "; ".join(
                f"{k}: {v}" for k, v in validations.items() if v
            )
        raise ApiError(
            (
                f"OPNsense lehnte den Schreibvorgang ab "
                f"(result='{result}'{(': ' + detail) if detail else ''})."
            ),
            context=make_context(
                host=ctx.target.host,
                port=ctx.target.port,
                method="POST",
                path=path,
                error_kind="opnsense_save_failed",
            ),
        )


def _content_from_api(value: Any) -> tuple[str, ...]:
    """Normalisiert das Content-Feld der OPNsense-API.

    Die API liefert je nach Endpoint einen String (Newline-separated) oder
    ein Dict (für Multi-Wert-Felder mit Beschriftungen). Wir bauen daraus
    immer ein Tuple aus eindeutigen Strings.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(line.strip() for line in value.splitlines() if line.strip())
    if isinstance(value, dict):
        # OPNsense kann eine Map zurückgeben: { "value1": {...}, "value2": {...} }
        return tuple(str(k) for k in value)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def _row_to_spec(row: dict[str, Any]) -> AliasSpec:
    return AliasSpec(
        name=str(row.get("name", "")),
        type=str(row.get("type", "host")),
        content=_content_from_api(row.get("content")),
        descr=str(row.get("description", row.get("descr", ""))),
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AliasAdapter:
    """Adapter für Firewall-Aliasse."""

    subsystem: ClassVar[str] = "firewall_alias"

    def identity(self, spec: AliasSpec) -> AliasIdentity:
        return spec.to_identity()

    def to_payload(self, spec: AliasSpec) -> dict[str, Any]:
        return {
            "name": spec.name,
            "type": spec.type,
            "content": _content_to_str(spec.content),
            "description": spec.descr,
        }

    def spec_to_dict(self, spec: AliasSpec) -> dict[str, Any]:
        return {
            "name": spec.name,
            "type": spec.type,
            "content": list(spec.content),
            "descr": spec.descr,
            "merge_mode": spec.merge_mode,
        }

    def spec_from_dict(self, raw: dict[str, Any]) -> AliasSpec:
        content_raw = raw.get("content", [])
        if isinstance(content_raw, str):
            content_raw = [c.strip() for c in content_raw.split(",") if c.strip()]
        if not isinstance(content_raw, list):
            content_raw = []
        merge_mode_raw = str(raw.get("merge_mode", "create"))
        merge_mode: MergeMode = (
            "append" if merge_mode_raw == "append" else "create"
        )
        return AliasSpec(
            name=str(raw.get("name", "")),
            type=str(raw.get("type", "host")),
            content=tuple(str(item) for item in content_raw),
            descr=str(raw.get("descr", "")),
            merge_mode=merge_mode,
        )

    # ----- exists / verify -----

    def exists(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: AliasIdentity,
    ) -> AliasSpec | None:
        """Sucht einen Alias gleichen Namens und liefert seinen Soll-Stand zurück.

        Holt nach erfolgreicher Suche den vollen Eintrag per ``getItem``, weil
        die Suche typischerweise nur eine Zusammenfassung liefert.
        """
        uuid = self._search_uuid(client, ctx, ident.name)
        if uuid is None:
            return None
        full = self._get_by_uuid(client, ctx, uuid)
        return full

    def verify(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: AliasIdentity,
    ) -> VerifyOutcome:
        current = self.exists(client, ctx, ident)
        if current is None:
            return VerifyOutcome(found=False)
        return VerifyOutcome(found=True, detail=f"name={current.name}")

    # ----- add (create + append) -----

    def add(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: AliasSpec,
    ) -> AddOutcome:
        validate_alias_name(spec.name)
        validate_alias_type(spec.type)
        if spec.merge_mode == "append":
            return self._append(client, ctx, spec)
        return self._create(client, ctx, spec)

    def _create(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: AliasSpec,
    ) -> AddOutcome:
        payload = {"alias": self.to_payload(spec)}
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", ALIAS_ADD,
            json=payload,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        _raise_if_not_saved(body, ALIAS_ADD, ctx)
        uuid: str | None = None
        if isinstance(body, dict):
            candidate = body.get("uuid")
            if isinstance(candidate, str) and candidate:
                uuid = candidate
        return AddOutcome(uuid=uuid, raw_status=response.status_code)

    def _append(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: AliasSpec,
    ) -> AddOutcome:
        existing_uuid = self._search_uuid(client, ctx, spec.name)
        if existing_uuid is None:
            raise ValidationError(
                f"Alias '{spec.name}' existiert nicht — append nicht möglich.",
                context=make_context(
                    host=ctx.target.host,
                    port=ctx.target.port,
                    method="POST",
                    path=ALIAS_SEARCH,
                    error_kind="alias_not_found",
                ),
            )
        current = self._get_by_uuid(client, ctx, existing_uuid)
        merged = _merge_content(current.content, spec.content)
        merged_spec = AliasSpec(
            name=spec.name,
            type=current.type,  # Typ bleibt unverändert
            content=merged,
            descr=current.descr or spec.descr,
            merge_mode="append",
        )
        payload = {"alias": self.to_payload(merged_spec)}
        set_path = ALIAS_SET.format(uuid=existing_uuid)
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", set_path,
            json=payload,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        _raise_if_not_saved(body, set_path, ctx)
        return AddOutcome(uuid=existing_uuid, raw_status=response.status_code)

    # ----- diff -----

    def diff(self, current: AliasSpec | None, target_spec: AliasSpec) -> Diff:
        if current is None:
            if target_spec.merge_mode == "append":
                return Diff(
                    kind=DiffKind.NEW,
                    summary=(
                        f"Alias '{target_spec.name}' existiert noch nicht — "
                        "Append wird beim Apply fehlschlagen. Lege ihn zuerst "
                        "per add-alias an."
                    ),
                )
            return Diff(
                kind=DiffKind.NEW,
                summary=(
                    f"Neuer Alias '{target_spec.name}' "
                    f"({target_spec.type}, {len(target_spec.content)} Eintrag/Einträge)"
                ),
            )
        # current existiert
        current_set = set(current.content)
        target_set = set(target_spec.content)
        if target_spec.merge_mode == "create":
            if current_set == target_set:
                return Diff(
                    kind=DiffKind.SKIP,
                    summary=(
                        f"Alias '{target_spec.name}' bereits identisch — "
                        "wird übersprungen."
                    ),
                )
            return Diff(
                kind=DiffKind.UPDATE,
                summary=(
                    f"Konflikt: Alias '{target_spec.name}' existiert mit anderem "
                    "Inhalt. v1 unterstützt kein In-Place-Update bei 'create' — "
                    "Apply wird hier fehlschlagen. Nutze append-alias zum Mergen."
                ),
            )
        # append mode + exists
        missing = [c for c in target_spec.content if c not in current_set]
        if not missing:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Alle Einträge bereits in '{target_spec.name}' "
                    "vorhanden — übersprungen."
                ),
            )
        preview_n = 3
        sample = ", ".join(missing[:preview_n])
        ellipsis = "…" if len(missing) > preview_n else ""
        return Diff(
            kind=DiffKind.UPDATE,
            summary=(
                f"+{len(missing)} Eintrag/Einträge an '{target_spec.name}' "
                f"anhängen ({sample}{ellipsis})"
            ),
        )

    # ----- API-Helfer -----

    def _search_uuid(
        self,
        client: HttpClient,
        ctx: RequestContext,
        name: str,
    ) -> str | None:
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", ALIAS_SEARCH,
            json={"current": 1, "rowCount": -1, "searchPhrase": name},
        )
        try:
            data: Any = response.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        rows = data.get("rows")
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("name", "")) == name:
                uuid = row.get("uuid")
                if isinstance(uuid, str) and uuid:
                    return uuid
        return None

    def _get_by_uuid(
        self,
        client: HttpClient,
        ctx: RequestContext,
        uuid: str,
    ) -> AliasSpec:
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "GET", ALIAS_GET.format(uuid=uuid),
        )
        try:
            data: Any = response.json()
        except ValueError:
            return AliasSpec(name="", type="host", content=())
        if not isinstance(data, dict):
            return AliasSpec(name="", type="host", content=())
        # OPNsense wickelt das Item üblicherweise in {"alias": {...}}
        inner = data.get("alias")
        row = inner if isinstance(inner, dict) else data
        return _row_to_spec(row)


# ---------------------------------------------------------------------------
# Content-Merge
# ---------------------------------------------------------------------------


def _merge_content(
    current: tuple[str, ...],
    additional: tuple[str, ...],
) -> tuple[str, ...]:
    """Vereinigung in deterministischer Reihenfolge: erst bestehende, dann neue.

    Dedupliziert, behält aber die ursprüngliche Sortierung — der Admin
    erkennt im Audit-Log, welche Werte später angekommen sind.
    """
    seen: set[str] = set()
    result: list[str] = []
    for item in current:
        if item not in seen:
            seen.add(item)
            result.append(item)
    for item in additional:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


# ---------------------------------------------------------------------------
# Subsystem-Controller
# ---------------------------------------------------------------------------


class AliasesController:
    """Trägt den ``reconfigure``-Aufruf für das Firewall-Alias-Subsystem."""

    subsystem: ClassVar[str] = "firewall_alias"

    def reconfigure(self, client: HttpClient, ctx: RequestContext) -> None:
        try:
            client.call(
                ctx.target, ctx.key, ctx.secret,
                "POST", ALIAS_RECONFIGURE,
                json={},
                timeout_override_s=client.tuning.reconfigure_timeout_s,
            )
        except (UnreachableError, AuthError, ValidationError, ApiError, EgressDeniedError) as exc:
            raise ReconfigureError(
                "reconfigure des Firewall-Alias-Subsystems fehlgeschlagen.",
                context=make_context(
                    host=ctx.target.host,
                    port=ctx.target.port,
                    method="POST",
                    path=ALIAS_RECONFIGURE,
                    error_kind="reconfigure",
                    summary=exc.context.summary,
                    status_code=exc.context.status_code,
                ),
            ) from exc
