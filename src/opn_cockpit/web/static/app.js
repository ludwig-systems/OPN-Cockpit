// OPN-Cockpit Frontend — Iteration 1 (Boot-Smoke)
//
// Hier kommt in Iter 2+ die echte Logik (Login, Inventar, Plan/Apply).
// Aktuell prueft das Skript nur den Server-Health-Endpoint und zeigt das
// Resultat im Boot-Splash. Theme folgt System-Praeferenz.
(function () {
  'use strict';

  // Theme: System-Praeferenz beim Boot
  if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
    document.documentElement.setAttribute('data-theme', 'light');
  }

  const status = document.querySelector('.boot-status');

  fetch('/api/version', { headers: { Accept: 'application/json' } })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((data) => {
      if (status) {
        status.textContent = `Backend bereit · v${data.version}`;
      }
    })
    .catch((err) => {
      if (status) {
        status.textContent = `Backend nicht erreichbar (${err})`;
      }
    });
})();
