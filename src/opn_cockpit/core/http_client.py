"""HTTPS-Client mit Egress-Allowlist, TLS-pro-Host, Retry und Fehler-Mapping.

Verbindlich (siehe Plan, Schritt 2):

* **Egress-Allowlist (R-SEC-7):** Jeder Request prüft (host, port) gegen die
  bei Konstruktion übergebene Menge konfigurierter Ziele und wirft
  ``EgressDeniedError``, wenn das Ziel nicht inventarisiert ist.
* **TLS-Konfiguration pro Host:** Jedes Ziel hat seinen eigenen
  ``httpx.Client`` mit gerätespezifischer Verify-Einstellung.
* **Retry mit exponentiellem Backoff:** ausschließlich bei Netzwerk- und
  5xx-Fehlern. Niemals bei 4xx (Auth, Validierung) — sofortiger Fail.
* **Fehler-Mapping in Tool-eigene Exceptions:** Aufrufer bekommen
  ``AuthError`` / ``ValidationError`` / ``UnreachableError`` / ``ApiError`` /
  ``EgressDeniedError``, keine httpx-Internals.
* **Mockbarkeit (R-NFR-4):** Optionale ``transport``-Injektion für Tests gegen
  ``httpx.MockTransport``.

Keine GUI-, ``keyring``- oder Logging-Imports.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from opn_cockpit.core.errors import (
    ApiError,
    AuthError,
    EgressDeniedError,
    UnreachableError,
    ValidationError,
    make_context,
)

# HTTP-Status-Code-Schwellen (RFC 7231)
HTTP_OK_MIN = 200
HTTP_OK_MAX_EXCLUSIVE = 300
HTTP_CLIENT_ERROR_MIN = 400
HTTP_SERVER_ERROR_MIN = 500
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpTuning:
    """Konfigurierbare Timing-/Retry-Parameter für alle HTTPS-Calls.

    Defaults gemäß Plan: PAW-naher Lauf mit ~25 Geräten, OPNsense-
    ``reconfigure`` braucht typischerweise 2-10 Sekunden.
    """

    connect_timeout_s: float = 5.0
    read_timeout_s: float = 30.0
    reconfigure_timeout_s: float = 60.0
    retry_count: int = 2
    retry_backoff_base_s: float = 0.5
    retry_backoff_factor: float = 4.0
    retryable_status_codes: frozenset[int] = field(
        default_factory=lambda: frozenset({502, 503, 504})
    )

    def backoff_delay_s(self, attempt: int) -> float:
        """Verzögerung vor dem nächsten Retry (0-indizierter Attempt)."""
        return self.retry_backoff_base_s * (self.retry_backoff_factor**attempt)


# ---------------------------------------------------------------------------
# Target-Beschreibung
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpTarget:
    """Adressiert ein OPNsense-Gerät auf HTTP-Ebene.

    ``verify`` ist entweder ``True`` (System-CA-Bundle), ``False`` (TLS-Prüfung
    deaktiviert — wird in der UI als Risiko markiert, Spec R-SEC-4) oder ein
    Pfad/eine CA-Bundle-Datei.
    """

    host: str
    port: int = 443
    verify: bool | str = True

    @property
    def key(self) -> tuple[str, int]:
        return (self.host.lower(), self.port)

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _basic_auth_header(key: str, secret: str) -> str:
    """Baut den ``Authorization: Basic <…>``-Header.

    Erwartet die rohen Credential-Werte und gibt nur den fertig kodierten
    Header-Wert zurück. Klartext-Secret verlässt diese Funktion nicht.
    """
    token = base64.b64encode(f"{key}:{secret}".encode()).decode("ascii")
    return f"Basic {token}"


class HttpClient:
    """HTTPS-Frontend gegen alle inventarisierten OPNsense-Hosts.

    Erzeugt pro Ziel-Host genau einen ``httpx.Client``, der dessen
    Verify-Einstellung trägt. Beim Konstruktor wird die Egress-Allowlist
    festgenagelt; spätere Aufrufe gegen unbekannte Hosts werden blockiert.
    """

    def __init__(
        self,
        *,
        targets: list[HttpTarget],
        tuning: HttpTuning | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        if not targets:
            # Leeres Inventar ist explizit erlaubt (frischer Setup), aber dann
            # ist auch jeder Request defaultmäßig abgelehnt — siehe `call`.
            self._allowed: dict[tuple[str, int], HttpTarget] = {}
        else:
            self._allowed = {t.key: t for t in targets}
        self._tuning = tuning or HttpTuning()
        self._transport = transport
        self._sleep = sleep
        self._clients: dict[tuple[str, int], httpx.Client] = {}

    # ----- Public API -----

    @property
    def tuning(self) -> HttpTuning:
        return self._tuning

    def call(
        self,
        target: HttpTarget,
        key: str,
        secret: str,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout_override_s: float | None = None,
    ) -> httpx.Response:
        """Führt einen API-Call gegen ``target`` aus.

        ``target`` muss bei Konstruktion in der Allowlist gewesen sein, sonst
        ``EgressDeniedError`` (auch wenn dieselben Host-/Port-Werte
        übereinstimmen — die Object-Identität ist nicht ausreichend, das
        ``(host.lower(), port)``-Paar muss übereinstimmen).

        Bei netzwerk- oder 5xx-Fehlern wird gemäß ``HttpTuning`` mit
        exponentiellem Backoff erneut versucht. 4xx wird sofort als
        ``AuthError`` / ``ValidationError`` weitergeworfen — kein Retry.

        Auth-Header (Basic) wird je Aufruf neu gebaut. Klartext-Secret
        verlässt diese Methode niemals (es landet nur in der base64-
        kodierten Header-Form auf der Leitung).
        """
        if target.key not in self._allowed:
            raise EgressDeniedError(
                f"Egress verweigert: Ziel {target.host}:{target.port} ist nicht im Inventar.",
                context=make_context(
                    host=target.host,
                    port=target.port,
                    method=method,
                    path=path,
                    error_kind="egress_denied",
                ),
            )
        client = self._get_or_create_client(target)
        headers = {
            "Authorization": _basic_auth_header(key, secret),
            "Accept": "application/json",
        }
        request_timeout = self._compose_timeout(timeout_override_s)
        return self._execute_with_retry(
            client=client,
            method=method,
            url=path,
            headers=headers,
            json=json,
            timeout=request_timeout,
            target=target,
        )

    def close(self) -> None:
        """Schließt alle internen httpx-Clients."""
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----- Internals -----

    def _get_or_create_client(self, target: HttpTarget) -> httpx.Client:
        cached = self._clients.get(target.key)
        if cached is not None:
            return cached
        # Für Tests: ein injizierter Transport überschreibt das verify-Setting,
        # weil httpx den Transport bevorzugt nutzt.
        client = httpx.Client(
            base_url=target.base_url,
            verify=target.verify,
            timeout=httpx.Timeout(self._tuning.read_timeout_s),
            transport=self._transport,
            trust_env=False,  # ignoriert HTTP_PROXY etc. auf der PAW
        )
        self._clients[target.key] = client
        return client

    def _compose_timeout(self, override_s: float | None) -> httpx.Timeout:
        connect = self._tuning.connect_timeout_s
        read = override_s if override_s is not None else self._tuning.read_timeout_s
        return httpx.Timeout(connect=connect, read=read, write=read, pool=read)

    def _execute_with_retry(
        self,
        *,
        client: httpx.Client,
        method: str,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any] | None,
        timeout: httpx.Timeout,
        target: HttpTarget,
    ) -> httpx.Response:
        attempts = self._tuning.retry_count + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                response = client.request(
                    method=method,
                    url=url,
                    json=json,
                    headers=headers,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    self._sleep(self._tuning.backoff_delay_s(attempt))
                    continue
                raise UnreachableError(
                    f"Timeout gegen {target.host}:{target.port} ({method} {url}).",
                    context=make_context(
                        host=target.host,
                        port=target.port,
                        method=method,
                        path=url,
                        error_kind="timeout",
                    ),
                ) from exc
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    self._sleep(self._tuning.backoff_delay_s(attempt))
                    continue
                raise UnreachableError(
                    f"Netzwerk-Fehler gegen {target.host}:{target.port}: {exc.__class__.__name__}",
                    context=make_context(
                        host=target.host,
                        port=target.port,
                        method=method,
                        path=url,
                        error_kind="network",
                    ),
                ) from exc
            except httpx.HTTPError as exc:
                # Sonstige httpx-Fehler (z. B. TLS) — als unerreichbar einstufen.
                raise UnreachableError(
                    f"HTTP-Fehler gegen {target.host}:{target.port}: {exc.__class__.__name__}",
                    context=make_context(
                        host=target.host,
                        port=target.port,
                        method=method,
                        path=url,
                        error_kind="http",
                    ),
                ) from exc

            if (
                response.status_code in self._tuning.retryable_status_codes
                and attempt < attempts - 1
            ):
                self._sleep(self._tuning.backoff_delay_s(attempt))
                continue

            self._raise_for_status(response, method=method, url=url, target=target)
            return response

        # Defensive — wir kommen hier nur an, wenn die Schleife ohne Rückgabe
        # endet. last_exc ist dann gesetzt.
        raise UnreachableError(
            f"Aufruf gegen {target.host}:{target.port} hat alle Versuche aufgebraucht.",
            context=make_context(
                host=target.host,
                port=target.port,
                method=method,
                path=url,
                error_kind="exhausted",
            ),
        ) from last_exc

    def _raise_for_status(
        self,
        response: httpx.Response,
        *,
        method: str,
        url: str,
        target: HttpTarget,
    ) -> None:
        code = response.status_code
        if HTTP_OK_MIN <= code < HTTP_OK_MAX_EXCLUSIVE:
            return
        summary = self._short_body_summary(response)
        ctx = make_context(
            host=target.host,
            port=target.port,
            method=method,
            path=url,
            status_code=code,
            summary=summary,
        )
        if code in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
            raise AuthError(
                f"Authentifizierung fehlgeschlagen (HTTP {code}) bei {target.host}.",
                context=ctx,
            )
        if HTTP_CLIENT_ERROR_MIN <= code < HTTP_SERVER_ERROR_MIN:
            raise ValidationError(
                f"API-Validierungsfehler (HTTP {code}) bei {target.host}.",
                context=ctx,
            )
        # 500 + alle nicht-explizit-retrybaren 5xx
        raise ApiError(
            f"Server-Fehler (HTTP {code}) bei {target.host}.",
            context=ctx,
        )

    @staticmethod
    def _short_body_summary(response: httpx.Response, max_len: int = 200) -> str:
        """Liefert eine maximal ``max_len`` Zeichen lange, deliberate Zusammenfassung.

        Versucht zuerst JSON-Inhalte zu parsen und nur die ersten Schlüssel
        zu zeigen — vermeidet, dass komplette Rohdaten in die Exception
        rutschen. Bei nicht-JSON-Antworten wird der Text vorne abgeschnitten.
        """
        try:
            data = response.json()
        except ValueError:
            text = response.text
            return text if len(text) <= max_len else text[: max_len - 1] + "…"
        if isinstance(data, dict):
            keys = ",".join(sorted(data.keys())[:6])
            return f"json-keys={keys}"
        if isinstance(data, list):
            return f"json-list[len={len(data)}]"
        as_str = str(data)
        return as_str if len(as_str) <= max_len else as_str[: max_len - 1] + "…"
