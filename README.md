# pyhazus

A standalone, cross-platform Python implementation of the FEMA Hazus loss estimation methodology,
reconstructed from the Hazus 7.0 / 7.2 installers and the FEMA technical manuals. It has no ArcGIS Pro,
SQL Server or Windows COM dependency at runtime.

Status: early development. Flood is the first hazard being ported.

- `tools/` — one-time tooling to unpack the FEMA installers and export their databases
- `src/hazus/` — the library (`import hazus`)
- `docs/` — reconstructed methodology notes (e.g. `docs/flood-dataflow.md`)

The FEMA installers, extracted payloads and decompiled sources are not part of this repository
(see `.gitignore`).
