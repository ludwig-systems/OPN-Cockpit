"""In-Memory-Session über einem entsperrten Tresor.

Hält den entschlüsselten ``VaultData``, trackt die letzte Benutzer-Aktivität
und sperrt sich nach Ablauf der Inaktivitätszeit selbst — Spec R-SEC-6.

GUI und CLI sind die einzigen Aufrufer:

* Sie rufen :meth:`unlock` nach erfolgreichem ``vault.store.open_vault`` auf.
* Sie rufen :meth:`touch` bei jeder relevanten Benutzer-Interaktion.
* Sie rufen :meth:`check_inactivity` periodisch (z. B. Qt-Timer alle 30 s);
  liefert ``True`` zurück, wenn die Session in diesem Aufruf gesperrt wurde.
* Sie rufen :meth:`lock` bei explizitem Logout.

Die Session wird **stateless** an die Orchestrierung übergeben — sie sieht
nicht die Session, sondern bekommt pro Gerät die Credentials per
``credentials_for(device_id)`` durchgereicht.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opn_cockpit.vault.errors import SessionLockedError, UnknownDeviceError
from opn_cockpit.vault.store import OpenedVault


@dataclass(slots=True)
class Session:
    """Tresor-Sitzung im Speicher.

    Default-Clock ist ``time.monotonic`` — monoton steigend und unbeeinflusst
    von Systemzeit-Sprüngen. Für Tests injizierbar.
    """

    _opened: OpenedVault | None = None
    _vault_path: Path | None = None
    _last_activity_at: float = 0.0
    _clock: Callable[[], float] = field(default=time.monotonic)

    # ----- Statusabfragen -----

    @property
    def is_locked(self) -> bool:
        return self._opened is None

    @property
    def is_unlocked(self) -> bool:
        return self._opened is not None

    @property
    def vault_path(self) -> Path | None:
        return self._vault_path

    @property
    def inactivity_timeout_s(self) -> float:
        """Liefert das im Tresor hinterlegte Inaktivitäts-Timeout in Sekunden.

        ``0.0`` bei gesperrter Session — vermeidet "abgelaufen"-Auswertungen
        auf einem ungeöffneten Tresor.
        """
        if self._opened is None:
            return 0.0
        return max(0.0, float(self._opened.data.settings.inactivity_minutes) * 60.0)

    @property
    def opened(self) -> OpenedVault:
        """Liefert den entsperrten Tresor oder wirft ``SessionLockedError``."""
        if self._opened is None:
            raise SessionLockedError("Session ist gesperrt — bitte Tresor entsperren.")
        return self._opened

    # ----- Lifecycle -----

    def unlock(self, opened: OpenedVault, path: Path) -> None:
        """Aktiviert die Session mit einem frisch geöffneten Tresor."""
        self._opened = opened
        self._vault_path = path
        self._last_activity_at = self._clock()

    def replace_opened(self, opened: OpenedVault) -> None:
        """Tauscht den hinterlegten ``OpenedVault`` aus (z. B. nach ``save_vault``).

        Reset des Inaktivitäts-Timers, weil der User soeben eine bewusste
        Aktion ausgelöst hat.
        """
        if self._opened is None:
            raise SessionLockedError(
                "Session muss entsperrt sein, bevor sie aktualisiert werden kann."
            )
        self._opened = opened
        self._last_activity_at = self._clock()

    def touch(self) -> None:
        """Setzt den Inaktivitäts-Zähler auf jetzt zurück."""
        if self._opened is not None:
            self._last_activity_at = self._clock()

    def lock(self) -> None:
        """Bereinigt die Session.

        Setzt alle Referenzen auf None; der Garbage Collector räumt
        anschließend den ``VaultData``-Block ab. Memory-Wiping in Python ist
        best-effort — wir akzeptieren das, weil ein Angreifer mit
        Memory-Dump-Zugang ohnehin die PAW kompromittiert hat.
        """
        self._opened = None
        self._vault_path = None
        self._last_activity_at = 0.0

    # ----- Inaktivitäts-Verwaltung -----

    def seconds_until_expiry(self) -> float:
        """Verbleibende Sekunden bis zur automatischen Sperre.

        ``0.0`` wenn bereits gesperrt oder abgelaufen.
        """
        if self._opened is None:
            return 0.0
        elapsed = self._clock() - self._last_activity_at
        return max(0.0, self.inactivity_timeout_s - elapsed)

    def check_inactivity(self) -> bool:
        """Sperrt die Session, falls die Inaktivitätszeit abgelaufen ist.

        Returns:
            ``True`` wenn die Session in diesem Aufruf gesperrt wurde,
            sonst ``False``. So kann das GUI/CLI auf das Sperrereignis
            reagieren (Dialog zeigen, Routing umlenken).
        """
        if self._opened is None:
            return False
        if self.seconds_until_expiry() <= 0.0:
            self.lock()
            return True
        return False

    # ----- Credential-Bereitstellung -----

    def credentials_for(self, device_id: str) -> tuple[str, str]:
        """Liefert ``(api_key, api_secret)`` für ein Gerät.

        Schlägt mit ``UnknownDeviceError`` fehl, wenn die ID nicht im Tresor
        steht, und mit ``SessionLockedError``, wenn der Tresor gesperrt ist.
        Der Aufrufer (Executor) hält die Klartext-Strings nur für die
        Dauer eines einzelnen API-Calls und verwirft sie danach im
        ``try/finally``.
        """
        opened = self.opened
        for device in opened.data.devices:
            if device.id == device_id:
                return device.api_key, device.api_secret
        raise UnknownDeviceError(
            f"Gerät mit ID '{device_id}' nicht im Tresor."
        )
