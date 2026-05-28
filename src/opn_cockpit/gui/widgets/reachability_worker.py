"""Hintergrund-Worker für periodische TCP-Reachability-Probes.

Lebt in einem eigenen ``QThread``, damit die UI während des Probings nicht
einfriert. Pro Tick wird die übergebene Geräteliste parallel über einen
``ThreadPoolExecutor`` abgefragt; das Ergebnis (ein Dict ``device_id -> bool``)
wird als Signal in die UI-Thread zurückgegeben.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtCore import QObject, QThread, Signal

from opn_cockpit.core.health import tcp_probe
from opn_cockpit.inventory.model import Device


class _ProbeRunner(QObject):
    """Innerer Worker, der in den QThread verschoben wird."""

    finished = Signal(dict)  # {device_id: bool}

    def __init__(
        self,
        devices: list[Device],
        *,
        max_workers: int = 8,
        timeout_s: float = 3.0,
    ) -> None:
        super().__init__()
        self._devices = devices
        self._max_workers = max(1, min(max_workers, len(devices) or 1))
        self._timeout_s = timeout_s

    def run(self) -> None:
        if not self._devices:
            self.finished.emit({})
            return
        results: dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(
                    tcp_probe, d.host, d.port, timeout_s=self._timeout_s,
                ): d.id
                for d in self._devices
            }
            for future in as_completed(futures):
                device_id = futures[future]
                try:
                    results[device_id] = future.result()
                except Exception:
                    results[device_id] = False
        self.finished.emit(results)


class ReachabilityProbe(QObject):
    """Fasade fuer einmaligen Hintergrund-Probe-Lauf.

    Owner ruft :meth:`start`, kriegt das Ergebnis über das
    :attr:`results_ready` Signal. Eine Instanz pro Lauf — der QThread wird
    nach Abschluss aufgeräumt.
    """

    results_ready = Signal(dict)  # {device_id: bool}

    def __init__(self, devices: list[Device], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._runner = _ProbeRunner(devices)
        self._runner.moveToThread(self._thread)
        self._thread.started.connect(self._runner.run)
        self._runner.finished.connect(self._on_finished)
        self._runner.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._runner.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)

    def start(self) -> None:
        self._thread.start()

    def _on_finished(self, results: dict[str, bool]) -> None:
        self.results_ready.emit(results)
