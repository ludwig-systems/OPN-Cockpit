"""Storage-Foundation fuer SQLite-Backends (v3.1).

Heute genutzt von SqliteAuditBackend, SqlitePlanStoreBackend,
SqliteProfileStoreBackend. Alle drei teilen sich eine Datei
``$OPNCOCKPIT_DATA_DIR/opn-cockpit.db`` und damit eine einzelne
Connection — das vermeidet einen "wer faengt mit der DB an"-Race.
"""
