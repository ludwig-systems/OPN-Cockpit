"""Datenmodell des Backup-Indexes.

``BackupRecord`` ist die kanonische Beschreibung eines persistierten
Backups. Wird sowohl in ``index.json`` serialisiert als auch ueber die
API ans Frontend gereicht (per Pydantic-Schema, siehe ``web/api/schemas.py``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# Erlaubte Trigger-Werte. ``manual`` = User-Klick auf "Backup herunterladen"
# oder "Backup erzeugen", ``pre-apply`` = automatisch vor einem Plan-Apply,
# ``post-apply`` = automatisch nach erfolgreichem Plan-Apply (neue
# Drift-Baseline), ``scheduled`` = aus dem Hintergrund-Scheduler.
BACKUP_TRIGGERS: frozenset[str] = frozenset(
    {"manual", "pre-apply", "post-apply", "scheduled"},
)


@dataclass(frozen=True, slots=True)
class BackupRecord:
    """Metadaten eines persistierten Backups.

    Felder:

    * ``id`` — UUID4, gleichzeitig Dateiname (``<id>.xml.gz``).
    * ``device_id`` — Tresor-Geraete-UUID.
    * ``timestamp_utc`` — ISO-8601 UTC, Zeitpunkt der Erzeugung.
    * ``trigger`` — eines aus ``BACKUP_TRIGGERS``.
    * ``size_bytes`` — Groesse des UNkomprimierten XML.
    * ``size_compressed`` — Groesse der ``.xml.gz``-Datei auf Disk.
    * ``sha256`` — Hex-Digest des unkomprimierten XML. Fuer Drift-Vergleich
      spaeter (#5) als billiger ``has-it-changed?``-Check nutzbar.
    * ``related_plan_id`` — Plan, der dieses Backup ausgeloest hat (nur bei
      ``trigger="pre-apply"``). Sonst leer.
    * ``device_name_at_creation`` — Geraete-Name zum Zeitpunkt des Backups,
      damit Listen-Anzeige funktioniert auch wenn das Geraet umbenannt wurde.
    * ``seq`` — pro Geraet monoton steigende Anlage-Nummer, vergeben von
      ``append_backup``. Einziger Zweck: ein stabiler Tiebreaker fuer die
      Sortierung, wenn zwei Backups denselben ``timestamp_utc`` tragen —
      der hat nur Millisekunden-Aufloesung, auf Windows sogar nur ~15 ms.
      Anders als die Position in der Index-Liste ueberlebt ``seq`` das
      Schreiben und Wiedereinlesen von ``index.json``, die Sortierung
      bleibt dadurch idempotent. Altbestand ohne Feld bekommt ``0``.
    """

    id: str
    device_id: str
    timestamp_utc: str
    trigger: str
    size_bytes: int
    size_compressed: int
    sha256: str
    related_plan_id: str = ""
    device_name_at_creation: str = ""
    seq: int = 0

    # ----- Serialisierung -----

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BackupRecord:
        """Defensiv: jedes Feld in den erwarteten Typ konvertieren.

        Unbekannte Felder werden ignoriert, fehlende defaults. Wenn ein
        ``trigger`` ausserhalb der Whitelist auftaucht (alter Index, neuer
        Trigger), wird er auf ``"manual"`` zurueckgemappt — kein Crash.

        Abwaertskompatibel: ein ``index.json`` aus einer Version vor dem
        ``seq``-Feld hat das Feld nicht. Solche Records bekommen ``seq=0``
        und sortieren damit unter jedem spaeter angelegten Backup — was
        genau der Realitaet entspricht, denn ``append_backup`` vergibt ab
        jetzt ``seq >= 1``.
        """
        trigger_raw = str(raw.get("trigger", "manual"))
        trigger = trigger_raw if trigger_raw in BACKUP_TRIGGERS else "manual"
        return cls(
            id=str(raw.get("id", "")),
            device_id=str(raw.get("device_id", "")),
            timestamp_utc=str(raw.get("timestamp_utc", "")),
            trigger=trigger,
            size_bytes=int(raw.get("size_bytes", 0)),
            size_compressed=int(raw.get("size_compressed", 0)),
            sha256=str(raw.get("sha256", "")),
            related_plan_id=str(raw.get("related_plan_id", "")),
            device_name_at_creation=str(raw.get("device_name_at_creation", "")),
            seq=int(raw.get("seq", 0) or 0),
        )


__all__ = ["BACKUP_TRIGGERS", "BackupRecord"]
