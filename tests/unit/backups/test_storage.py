"""Tests fuer den Backup-Storage-Layer.

Decken ab:

* Schreibe + Lese -> Round-Trip (Inhalt unveraendert, SHA passt)
* Index-Update + Mehrfach-Append
* Sortierung bei gleichem Zeitstempel (idempotent, ueber Schreibvorgaenge
  hinweg stabil, abwaertskompatibel zu Indizes ohne ``seq``)
* Retention: Pre-Apply-Pool, Scheduled-Pool, Manual + Pre-Apply geteilt
* Orphan-Cleanup beim Prune
* delete_all_for_device
* Defensive: kaputter Index, fehlende Geraete-IDs, leerer Content
"""

from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path

import pytest

from opn_cockpit.backups import (
    BackupNotFoundError,
    BackupRecord,
    BackupStoreError,
    append_backup,
    delete_all_for_device,
    list_backups,
    prune_backups,
    read_backup_content,
)
from opn_cockpit.backups import storage as storage_mod
from opn_cockpit.backups.storage import (
    BACKUP_FILE_SUFFIX,
    INDEX_FILENAME,
    _sort_records_desc,
)

XML_SAMPLE = (
    b'<?xml version="1.0"?><opnsense><system><hostname>opn-1</hostname></system></opnsense>'
)
XML_SAMPLE_2 = (
    b'<?xml version="1.0"?><opnsense><system><hostname>opn-2</hostname></system></opnsense>'
)


class TestAppendAndRead:
    def test_round_trip(self, tmp_path: Path) -> None:
        record = append_backup(
            "dev-1",
            XML_SAMPLE,
            trigger="manual",
            device_name_at_creation="HQ Berlin",
            storage_root=tmp_path,
        )
        assert record.device_id == "dev-1"
        assert record.size_bytes == len(XML_SAMPLE)
        assert record.size_compressed > 0
        assert record.size_compressed < len(XML_SAMPLE) * 2  # gzip nicht aufblähend
        assert record.sha256
        assert record.trigger == "manual"
        assert record.device_name_at_creation == "HQ Berlin"

        content = read_backup_content("dev-1", record.id, storage_root=tmp_path)
        assert content == XML_SAMPLE

    def test_multiple_appends_keep_order(self, tmp_path: Path) -> None:
        r1 = append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
        r2 = append_backup("d", XML_SAMPLE_2, trigger="pre-apply", storage_root=tmp_path)
        listed = list_backups("d", storage_root=tmp_path)
        assert len(listed) == 2
        # neueste zuerst
        assert listed[0].id == r2.id
        assert listed[1].id == r1.id

    def test_unknown_trigger_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BackupStoreError, match="Trigger"):
            append_backup(
                "d",
                XML_SAMPLE,
                trigger="bogus",
                storage_root=tmp_path,
            )

    def test_empty_content_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BackupStoreError, match="Leerer"):
            append_backup(
                "d",
                b"",
                trigger="manual",
                storage_root=tmp_path,
            )

    def test_empty_device_id_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BackupStoreError, match="device_id"):
            append_backup(
                "",
                XML_SAMPLE,
                trigger="manual",
                storage_root=tmp_path,
            )


class TestList:
    def test_no_backups_returns_empty(self, tmp_path: Path) -> None:
        assert list_backups("unknown", storage_root=tmp_path) == []

    def test_corrupt_index_raises(self, tmp_path: Path) -> None:
        device_dir = tmp_path / "dev-1"
        device_dir.mkdir()
        (device_dir / INDEX_FILENAME).write_text("NOT JSON", encoding="utf-8")
        with pytest.raises(BackupStoreError, match="Index"):
            list_backups("dev-1", storage_root=tmp_path)


class TestSortStability:
    """Gleichstand beim ``timestamp_utc`` darf die Reihenfolge nicht drehen.

    Der Zeitstempel im Index hat nur Millisekunden-Aufloesung (auf Windows
    real ~15 ms). Zwei Backups im selben Tick tragen denselben Wert — die
    Sortierung muss den Gleichstand aus dem Record heraus aufloesen und
    nicht aus der Listenposition, sonst dreht jedes Schreiben die
    Reihenfolge erneut um.

    Alle Tests hier frieren die Uhr ein. Sie haengen damit **nicht** daran,
    ob zwei echte Appends zufaellig in dieselbe Millisekunde fallen: der
    Gleichstand ist erzwungen, der Fehler schlaegt in jedem Lauf zu.
    """

    @staticmethod
    def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            storage_mod,
            "_utc_now_iso",
            lambda: "2026-11-13T12:00:00.000Z",
        )

    @staticmethod
    def _record(backup_id: str, seq: int) -> BackupRecord:
        return BackupRecord(
            id=backup_id,
            device_id="d",
            timestamp_utc="2026-11-13T12:00:00.000Z",
            trigger="manual",
            size_bytes=1,
            size_compressed=1,
            sha256="",
            seq=seq,
        )

    def test_sort_is_idempotent(self) -> None:
        # Zweimal sortieren muss dasselbe Ergebnis liefern wie einmal.
        # Genau das war kaputt: der alte Positions-Tiebreaker drehte
        # Gleichstaende bei jedem Durchlauf wieder um.
        records = [self._record("a", 1), self._record("b", 2), self._record("c", 3)]
        once = _sort_records_desc(records)
        twice = _sort_records_desc(once)
        assert [r.id for r in once] == ["c", "b", "a"]
        assert twice == once

    def test_sort_ignores_input_order(self) -> None:
        # Dieselbe Menge in beliebiger Eingabereihenfolge -> dasselbe Ergebnis.
        a, b, c = self._record("a", 1), self._record("b", 2), self._record("c", 3)
        expected = ["c", "b", "a"]
        assert [r.id for r in _sort_records_desc([a, b, c])] == expected
        assert [r.id for r in _sort_records_desc([c, b, a])] == expected
        assert [r.id for r in _sort_records_desc([b, a, c])] == expected

    def test_same_millisecond_appends_list_newest_first(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Drei Appends in derselben "Millisekunde". list_backups liest den
        # Index frisch von Disk und muss das zuletzt angelegte zuerst zeigen.
        self._freeze_clock(monkeypatch)
        r1 = append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
        r2 = append_backup("d", XML_SAMPLE, trigger="pre-apply", storage_root=tmp_path)
        r3 = append_backup("d", XML_SAMPLE_2, trigger="scheduled", storage_root=tmp_path)

        assert {r.timestamp_utc for r in (r1, r2, r3)} == {"2026-11-13T12:00:00.000Z"}
        listed = list_backups("d", storage_root=tmp_path)
        assert [r.id for r in listed] == [r3.id, r2.id, r1.id]

    def test_order_survives_repeated_rewrites(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Jeder Append schreibt den gesamten Index neu. Nach beliebig vielen
        # Schreibvorgaengen muss die Reihenfolge immer noch stimmen.
        self._freeze_clock(monkeypatch)
        created = [
            append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
            for _ in range(6)
        ]
        expected = [r.id for r in reversed(created)]
        assert [r.id for r in list_backups("d", storage_root=tmp_path)] == expected
        # Auch ein Prune schreibt den Index neu - Reihenfolge bleibt.
        prune_backups(
            "d",
            retention_pre_apply=4,
            retention_scheduled=99,
            storage_root=tmp_path,
        )
        assert [r.id for r in list_backups("d", storage_root=tmp_path)] == expected[:4]

    def test_prune_keeps_the_newest_of_a_tie(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Folgefehler derselben Ursache: _keep_newest sortiert mit derselben
        # Funktion. Bei Gleichstand haette sonst das aelteste Backup
        # ueberlebt und das juengste waere geloescht worden.
        self._freeze_clock(monkeypatch)
        created = [
            append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
            for _ in range(3)
        ]
        deleted = prune_backups(
            "d",
            retention_pre_apply=1,
            retention_scheduled=99,
            storage_root=tmp_path,
        )
        assert {r.id for r in deleted} == {created[0].id, created[1].id}
        remaining = list_backups("d", storage_root=tmp_path)
        assert [r.id for r in remaining] == [created[2].id]

    def test_legacy_index_without_seq_sorts_below_new_records(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Bestandsdaten auf echten Geraeten haben kein seq-Feld. Sie muessen
        # lesbar bleiben und duerfen nicht vor ein neu angelegtes Backup
        # mit demselben Zeitstempel rutschen.
        device_dir = tmp_path / "d"
        device_dir.mkdir(parents=True)
        legacy = {
            "id": "legacy-1",
            "device_id": "d",
            "timestamp_utc": "2026-11-13T12:00:00.000Z",
            "trigger": "scheduled",
            "size_bytes": 10,
            "size_compressed": 5,
            "sha256": "cafe",
        }
        (device_dir / INDEX_FILENAME).write_text(
            json.dumps([legacy]),
            encoding="utf-8",
        )
        self._freeze_clock(monkeypatch)
        fresh = append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)

        listed = list_backups("d", storage_root=tmp_path)
        assert [r.id for r in listed] == [fresh.id, "legacy-1"]
        assert listed[1].seq == 0
        assert listed[0].seq == 1


class TestReadContent:
    def test_missing_id_raises_not_found(self, tmp_path: Path) -> None:
        # Geraet hat Backup, aber andere ID
        rec = append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
        with pytest.raises(BackupNotFoundError):
            read_backup_content("d", "no-such-id", storage_root=tmp_path)
        # echtes Lesen geht weiterhin
        assert read_backup_content("d", rec.id, storage_root=tmp_path) == XML_SAMPLE


class TestPruneRetention:
    def test_pre_apply_pool_keeps_newest(self, tmp_path: Path) -> None:
        # 5 pre-apply Backups, Limit 3 -> 2 weg
        for _ in range(5):
            append_backup("d", XML_SAMPLE, trigger="pre-apply", storage_root=tmp_path)
        deleted = prune_backups(
            "d",
            retention_pre_apply=3,
            retention_scheduled=99,
            storage_root=tmp_path,
        )
        assert len(deleted) == 2
        remaining = list_backups("d", storage_root=tmp_path)
        assert len(remaining) == 3
        # Geloeschte Files sind weg
        for r in deleted:
            f = tmp_path / "d" / f"{r.id}{BACKUP_FILE_SUFFIX}"
            assert not f.exists()

    def test_manual_shares_pool_with_pre_apply(self, tmp_path: Path) -> None:
        # 2 manual + 2 pre-apply, Limit 3 -> 1 weg (egal welcher Trigger,
        # die aelteste aus dem geteilten Pool fliegt)
        append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
        append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
        append_backup("d", XML_SAMPLE, trigger="pre-apply", storage_root=tmp_path)
        append_backup("d", XML_SAMPLE, trigger="pre-apply", storage_root=tmp_path)
        deleted = prune_backups(
            "d",
            retention_pre_apply=3,
            retention_scheduled=99,
            storage_root=tmp_path,
        )
        assert len(deleted) == 1
        remaining = list_backups("d", storage_root=tmp_path)
        assert len(remaining) == 3

    def test_post_apply_shares_pool_with_pre_apply(self, tmp_path: Path) -> None:
        # Post-Apply zaehlt zum gleichen Pool wie pre-apply/manual, damit
        # Pre-/Post-Apply-Paare gemeinsam verfallen.
        append_backup("d", XML_SAMPLE, trigger="pre-apply", storage_root=tmp_path)
        append_backup("d", XML_SAMPLE, trigger="post-apply", storage_root=tmp_path)
        append_backup("d", XML_SAMPLE, trigger="post-apply", storage_root=tmp_path)
        append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
        deleted = prune_backups(
            "d",
            retention_pre_apply=2,
            retention_scheduled=99,
            storage_root=tmp_path,
        )
        assert len(deleted) == 2
        remaining = list_backups("d", storage_root=tmp_path)
        assert len(remaining) == 2

    def test_scheduled_pool_is_separate(self, tmp_path: Path) -> None:
        # 5 scheduled + 5 pre-apply. pre-apply-Limit 3, scheduled-Limit 3.
        # -> 2 + 2 = 4 weg
        for _ in range(5):
            append_backup("d", XML_SAMPLE, trigger="scheduled", storage_root=tmp_path)
        for _ in range(5):
            append_backup("d", XML_SAMPLE, trigger="pre-apply", storage_root=tmp_path)
        deleted = prune_backups(
            "d",
            retention_pre_apply=3,
            retention_scheduled=3,
            storage_root=tmp_path,
        )
        assert len(deleted) == 4
        remaining = list_backups("d", storage_root=tmp_path)
        assert len(remaining) == 6
        # Verteilung pruefen
        counts = Counter(r.trigger for r in remaining)
        assert counts["pre-apply"] == 3
        assert counts["scheduled"] == 3

    def test_orphan_files_get_pruned(self, tmp_path: Path) -> None:
        # Manuell eine orphan-Datei reinlegen
        device_dir = tmp_path / "d"
        device_dir.mkdir(parents=True)
        orphan = device_dir / f"orphan-id{BACKUP_FILE_SUFFIX}"
        with gzip.open(orphan, "wb") as gz:
            gz.write(b"ich gehoere zu keinem Index-Eintrag")
        # Ein echter Eintrag
        append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
        assert orphan.exists()
        prune_backups("d", storage_root=tmp_path)
        # Orphan ist nach Prune weg
        assert not orphan.exists()


class TestDeleteAllForDevice:
    def test_removes_directory(self, tmp_path: Path) -> None:
        for _ in range(3):
            append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
        count = delete_all_for_device("d", storage_root=tmp_path)
        assert count == 3
        assert not (tmp_path / "d").exists()
        assert list_backups("d", storage_root=tmp_path) == []

    def test_idempotent_when_nothing_to_delete(self, tmp_path: Path) -> None:
        assert delete_all_for_device("never-existed", storage_root=tmp_path) == 0


class TestRecordSerialization:
    def test_round_trip_dict(self) -> None:
        rec = BackupRecord(
            id="abc",
            device_id="dev",
            timestamp_utc="2026-06-02T10:00:00.000Z",
            trigger="pre-apply",
            size_bytes=1234,
            size_compressed=678,
            sha256="deadbeef",
            related_plan_id="plan-123",
            device_name_at_creation="HQ Berlin",
        )
        roundtrip = BackupRecord.from_dict(rec.to_dict())
        assert roundtrip == rec

    def test_unknown_trigger_falls_back_to_manual(self) -> None:
        rec = BackupRecord.from_dict(
            {
                "id": "x",
                "device_id": "d",
                "timestamp_utc": "...",
                "trigger": "totally-bogus",
                "size_bytes": 0,
                "size_compressed": 0,
                "sha256": "",
            }
        )
        assert rec.trigger == "manual"

    def test_index_json_on_disk_is_valid(self, tmp_path: Path) -> None:
        append_backup("d", XML_SAMPLE, trigger="manual", storage_root=tmp_path)
        idx_path = tmp_path / "d" / INDEX_FILENAME
        assert idx_path.exists()
        data = json.loads(idx_path.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["trigger"] == "manual"
