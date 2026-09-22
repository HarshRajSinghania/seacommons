# Public documentation contract

Status: canonical public-documentation policy. Last reviewed: 2026-09-22.

SeaCommons owns its public documentation at **https://seacommons.org/docs**.
The documentation that previously lived inside the Suez Canal Republic website
is not a second source of truth. `suezcanal.xyz/docs` should only announce that
SeaCommons documentation moved and direct readers here.

## Public information architecture

The public documentation explains:

1. what SeaCommons is and which surfaces are public;
2. the observation -> normalized event -> derived cue -> episode -> hypothesis -> public Live progression;
3. Humanitarian and Maritime as the two top-level public incident families;
4. the difference between category, lifecycle and evidence stage;
5. Live and Play semantics;
6. source lineage and independence;
7. verification and corroboration;
8. drift/reconstruction semantics;
9. privacy, public projection and location precision;
10. public API surfaces and system limitations.

The public documentation is intentionally curated. It does not expose operator
credentials, private receiver endpoints, deployment secrets or environment-specific configuration.

## Engineering documentation

Engineering documentation remains versioned in this repository under `docs/`.
`docs/README.md` is the canonical index. Architecture, data-flow, security,
configuration, testing and operational documents can be linked from the public
documentation when a reader needs implementation detail.

Historical audits stay available for traceability but are not current product documentation.

## Metrics

The institutional site and public docs must reuse canonical public contracts.
They must not implement a parallel analytics pipeline.

The homepage system view reads:

- `GET /api/v1/status?hours=24`
- `GET /api/v1/play/counts`

The status endpoint already defines the semantics for raw observations,
normalized events, derived cues, episodes, hypotheses, corroboration, public
Live, sensor activity and freshness.

## Routing

- `seacommons.org/docs` -> SeaCommons public documentation.
- `api.seacommons.org/docs` -> generated OpenAPI / Swagger documentation.
- `api.seacommons.org/redoc` -> generated ReDoc reference.
- `suezcanal.xyz/docs` -> migration notice linking to `seacommons.org/docs`.

Do not reuse `/docs` on the institutional host for Swagger. The API host keeps
its host-specific `/docs` route before the institutional `/docs` rewrite.

## Copy rules

Public copy should distinguish observation from interpretation. In particular:

- a received observation is not a verified incident;
- a derived cue is not a factual finding;
- repeated transformations of one lineage do not create independent corroboration;
- category colour and lifecycle styling are separate;
- model coordinates remain derived candidates rather than reported geometry;
- no automated illegality finding is implied by anomaly labels.
