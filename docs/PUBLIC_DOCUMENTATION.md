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

The public reader experience is bundled directly into the web application from `apps/web/src/docs/content/SEACOMMONS.md`. It synthesizes the canonical architecture, data-flow, security, realtime, OSINT, operations and testing documents into a continuous public technical reference. The public page must not send readers to repository Markdown as a substitute for documentation.

Engineering documentation remains versioned under `docs/`, with `docs/README.md` as its internal canonical index. Historical audits stay available for traceability but are not current product documentation.

## Metrics

The institutional site and public docs must reuse canonical public contracts.
They must not implement a parallel analytics pipeline.

The homepage totals strip reads only:

- `GET /api/v1/play/counts`

Those values are all-time public catalogue counts and must stay visually comparable on one line. Rolling-window pipeline, sensor and freshness metrics belong in technical/status surfaces, not in the homepage totals strip.

The status endpoint remains the canonical contract for raw observations, normalized events, derived cues, episodes, hypotheses, corroboration, public Live, sensor activity and freshness.

## Routing

- `seacommons.org/docs` -> SeaCommons public documentation.
- `api.seacommons.org/docs` -> generated OpenAPI / Swagger documentation.
- `api.seacommons.org/redoc` -> generated ReDoc reference.
- `suezcanal.xyz/docs` -> migration notice linking to `seacommons.org/docs`.

Do not reuse `/docs` on the institutional host for Swagger. The API host keeps
its host-specific `/docs` route before the institutional `/docs` rewrite.

## Copy rules

Public copy is part of the evidence boundary. It should be written in plain
language and should describe what the system actually does before describing
why that work matters.

Editorial rules:

- prefer concrete nouns and verbs over abstract product language;
- explain a mechanism with an example or limit instead of adding an adjective;
- avoid stacked slogans, symmetrical headline pairs and generic phrases such as
  "actionable insights", "trusted intelligence", "uncertainty-aware",
  "accountable evidence" or similar language when the sentence does not explain
  the underlying mechanism;
- do not use three-part rhetorical lists merely for rhythm;
- one sentence should make one main claim when possible;
- name the source, transformation, output or failure mode when it matters;
- say when a capability is partial, experimental, unavailable or planned;
- never describe planned or experimental work as a current production feature;
- distinguish current data coverage from the theoretical capability of the
  architecture;
- state limitations in the same section as the capability they qualify, not in
  a distant disclaimer;
- write for a reader who does not know the internal taxonomy, then provide the
  technical term after the plain-language explanation;
- avoid language that implies certainty, intent, wrongdoing or completeness
  beyond what the evidence supports.

The copy must also preserve SeaCommons domain distinctions:

- a received observation is not a verified incident;
- a derived cue is not a factual finding;
- repeated transformations of one lineage do not create independent corroboration;
- source credibility is not location credibility;
- an extracted coordinate is not automatically a verified coordinate;
- an HTTP or provider failure is not an empty dataset;
- category colour and lifecycle styling are separate;
- model coordinates remain derived candidates rather than reported geometry;
- no automated illegality finding is implied by anomaly labels;
- Humanitarian privacy takes priority over public map precision.

A useful editorial test is simple: if a sentence could be pasted unchanged
onto an unrelated AI or OSINT product, rewrite it until it says something
specific about SeaCommons.


## Simulation and reconstruction presentation

SeaCommons must not use decorative simulation as a substitute for explanation.

Any future public reconstruction or simulation must expose, in the same interface:

- the observed inputs and their provenance;
- the explicit modelled outputs, visually separated from observations;
- the scenario origin and time window;
- forcing inputs and parameter versions;
- assumptions and degraded/fallback inputs;
- a time control that shows how the result evolves;
- uncertainty or ensemble spread, not only a single path;
- a plain-language explanation of what changed and why;
- the analytical question the reconstruction helps answer;
- links back to the underlying case/evidence record.

A simulation that cannot explain its inputs, transformations, uncertainty and purpose should not be published as a SeaCommons product surface.
