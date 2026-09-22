# SeaCommons documentation

SeaCommons is an open maritime research infrastructure for turning fragmented observations into traceable, contestable and uncertainty-aware evidence. It is designed for situations in which signals arrive from different systems, at different times, with different levels of precision and authority. The system does not treat volume as certainty. It preserves where a claim came from, how it was transformed, what is inferred and what remains unknown.

The public documentation is organised around one principle: a source record, an analytical cue, an investigation and a public case are different objects. SeaCommons keeps those differences visible rather than collapsing them into one map marker.

> SeaCommons is research infrastructure. It is not an emergency dispatch service and is not a replacement for maritime rescue coordination authorities.

## 01. Scope and system model

SeaCommons combines public reporting, vessel information, maritime radio evidence, satellite observations and environmental context inside one evidence model. It supports two public incident families, **Humanitarian** and **Maritime**, while keeping the source lineage and evidentiary role of every observation explicit.

The platform is a modular monorepo, not a collection of independent microservices. Its operational backend is one FastAPI application with optional workers sharing the same domain code and database. The public website and documentation, the operational console, the API and workers, and the Public Live edge are separate deployable surfaces because they have different trust and availability requirements.

The authenticated operational plane can contain richer records, internal review state and private material. Public Live is a separate reduced-trust product. It receives only a validated public projection. The public website can read aggregate status and privacy-filtered Live contracts, but it does not implement its own analytics logic and it never reads operator-only data.

This separation is central to the architecture. PostgreSQL is the production system of record. Object storage owns attachments and larger artefacts. In-memory stores, browser snapshots and edge snapshots are caches or projections. They improve responsiveness and resilience, but none of them becomes a second source of truth.

### Architectural invariants

- User-originated signals are private by default.
- Only the canonical public projection can cross into Public Live.
- Missing coordinates remain missing.
- Approximate or area-level geometry keeps explicit precision metadata.
- Duplicate realtime delivery is idempotent.
- A stale event version cannot resurrect a newer removed or resolved state.
- Source heartbeat freshness and event recency are different concepts.
- Public pages reuse canonical API contracts instead of maintaining parallel analytics.

## 02. From observation to investigation

SeaCommons processes evidence in explicit stages. Each stage answers a different question, and the counts shown on the homepage should therefore never be read as equivalent incident totals.

### Observation

An observation is an immutable source envelope received from a provider, partner, operator or sensor path. It records provenance before interpretation. Receiving an observation does not mean that SeaCommons considers the event verified.

### Normalized event

Normalization translates provider-specific fields into a shared vocabulary. It makes different sources comparable without pretending that they have equal authority or accuracy. Source identity, transport, timestamps, coordinate precision and verification role remain attached.

### Derived cue

A derived cue is an analytical output produced by a rule or model. Examples include AIS integrity concerns, dark-gap context, rendezvous context, proximity to infrastructure or a bounded radio interpretation. A cue can make an event worth investigating, but it is not a factual finding by itself.

### Episode

An episode groups related observations around one maritime situation while preserving the evidence that contributed to it. Grouping is based on explicit identity, temporal, spatial and source-lineage constraints rather than simple proximity alone.

### Investigation hypothesis

A hypothesis is a contestable interpretation of an episode. It can accumulate supporting evidence, conflicting evidence or context, and it can expire when the evidence no longer supports the association. SeaCommons treats hypotheses as analytical objects rather than conclusions.

### Public case

Public Live contains only the subset that satisfies the public projection and publication rules. Play preserves public case records and timelines so additional evidence can enrich a case without rewriting what was known earlier.

The pipeline is intentionally asymmetric. Many observations can produce one episode. Several cues can describe the same underlying source lineage. One case can persist while new observations arrive. This is why an observation count, an episode count and an archive count describe different things.

## 03. Provenance, lineage and source independence

Provenance is not metadata added after analysis. It is part of the evidence model.

SeaCommons distinguishes the organisation or sensor that originated a claim from the transport used to obtain it. A statement published by the same organisation through X, RSS and email remains one source lineage. Likewise, one AIS broadcast received through two providers does not become two independent observations simply because it travelled through two technical routes.

This rule matters most in corroboration. Reprocessing the same underlying material through several detectors may produce several indicators, but it does not manufacture source independence.

AIS provider multiplicity is therefore used to improve availability, reconcile fixes and reason about coverage. It does not increase the evidentiary weight of a single broadcast. Provider health and station context can help distinguish likely vessel silence from reception or upstream degradation, while the underlying AIS lineage remains explicit.

Humanitarian evidence uses the same principle with organisation-level identities. An operational origin, a verification organisation and an archive reference have different roles. Transport does not change those roles. Verification sources can add deterministic claims to an existing incident when association is strong enough, but they do not automatically create or resolve a Humanitarian incident.

Radio evidence follows physical receiver lineage. Multiple frontends exposing the same physical receiver remain one evidence source. SeaCommons stores bounded structured signal metadata and provenance rather than treating repeated reception of one transmission as independent corroboration.

## 04. Humanitarian and Maritime taxonomy

The public taxonomy begins with two macro categories.

### Humanitarian

Humanitarian cases cover distress, rescue, missing persons, shipwreck, pushback, migration incidents, SAR activity, resolution and related humanitarian context. These records require conservative source handling because location, identity and lifecycle can have direct safety implications.

A report may be operationally important even when its coordinates are approximate or absent. SeaCommons therefore stores precision explicitly. A reported point, an estimated point, an area and an unknown location are not interchangeable.

Verification evidence can support claims such as people rescued, rescue completed, disembarkation reported, fatalities reported, an asset on scene or a contradictory update. These claims remain separate from the canonical lifecycle until the relevant domain policy says otherwise. AIS behaviour may support a response assessment, but AIS alone cannot establish that a rescue was confirmed.

### Maritime

Maritime investigations cover dark activity, position integrity, transfers or rendezvous, loitering, infrastructure proximity, navigation safety, identity integrity, port calls, piracy or security, environmental hazards, context reports and other public observations.

These labels describe the type of investigation, not an accusation. A dark gap can result from coverage, reception, equipment state or deliberate behaviour. A sanctions match is context, not a finding of wrongdoing. A rendezvous can be operationally normal. SeaCommons keeps the evidentiary basis visible so a reader can distinguish an indicator from a conclusion.

Category and lifecycle are independent dimensions. Category determines what kind of situation is being described. Lifecycle describes whether it is active, needs review, resolved or archived. A lifecycle change should not silently change the category of the incident.

## 05. Verification and corroboration

SeaCommons distinguishes verification of an evidence item from corroboration of a wider claim.

Verification asks whether an observation has been authenticated, parsed correctly and associated with the correct source and event. Corroboration asks whether independent evidence lineages support the same episode or claim.

A case can therefore be multi-indicator without being independently corroborated. Several AIS-derived detectors can agree while still depending on one AIS lineage. Several posts can repeat the same original report. Several receivers can observe the same transmission. SeaCommons records those relationships instead of counting them as independent confirmation.

Strong association requires compatible evidence across more than one dimension. Time alone is not enough. Spatial proximity alone is not enough. Identity alone may be enough for some bounded relationships but not for others. The exact rule depends on the domain and is kept explicit in the analytical contract.

Corroboration also does not imply illegality, intent or responsibility. It means that independent evidence supports a defined proposition. The proposition itself must remain clearly stated and proportionate to what the evidence can establish.

## 06. Live, Play and realtime delivery

**Live** is a case-first public operational view. It is not a raw vessel tracker and not a dump of everything the backend has received. Public Live exposes only privacy-filtered cases and signals that satisfy the current public projection.

The public realtime path is separate from the authenticated operational plane. Canonical records are projected into a reduced public event, validated, placed in a durable outbox and delivered to an edge room. The edge validates the vocabulary again, deduplicates versions and maintains the current public snapshot.

On connection, a browser receives a complete snapshot before incremental updates. If streaming is interrupted, the client can recover from the edge snapshot, then from the public API, then from its last valid local snapshot. Heartbeat freshness is tracked separately from event recency so a quiet but healthy source is not confused with an offline source.

Material changes produce new incident versions. A stale version cannot replace a newer state. Duplicate delivery cannot create a second marker. Removal and expiry events clear cases that no longer qualify. These rules make the public stream resilient to retries and reconnects without sacrificing lifecycle integrity.

**Play** is the persistent public archive and reconstruction surface. A case keeps its timeline and can accumulate additional public evidence over time. Play is designed for inspection after the immediate Live moment, including reported evidence, modelled trajectories and the history of how the public record changed.

## 07. Maritime sensing and OSINT fusion

SeaCommons receives heterogeneous evidence, but it does not flatten all sources into one confidence score.

AIS observations feed vessel tracks, coverage reasoning and bounded anomaly detectors. Maritime radio can contribute structured DSC and NAVTEX evidence with physical receiver lineage. Satellite observations can provide independent imagery or acquisition context when their timing, resolution and geometry support a specific association. Environmental feeds contribute forcing and situational context rather than incident evidence.

Cross-source fusion is event-driven. New evidence is normalised, evaluated against bounded rules and correlated only when the relevant identity, spatial, temporal and lineage constraints are satisfied. The purpose of fusion is to make relationships inspectable, not to hide them behind a synthetic certainty value.

SeaCommons can create investigation candidates from combinations such as independent distress channels, vessel anomalies, infrastructure proximity or safety events. Every candidate preserves its contributing evidence. If two inputs share one underlying lineage, the system retains that relationship instead of presenting them as two independent confirmations.

## 08. Drift and environmental modelling

Drift is a modelled product, not an observation.

A drift run starts from an explicit origin and time window, then combines environmental forcing such as wind, waves or ocean currents with versioned simulation parameters. The result is a trajectory or uncertainty surface that can support search, reconstruction and interpretation.

SeaCommons keeps modelled geometry visibly distinct from reported geometry. A model output is never promoted to a verified coordinate. If the origin is missing, the system does not invent one merely to produce a trajectory.

Environmental inputs can degrade. When a preferred marine source is unavailable, the system can fall back to another bounded source or to a declared degraded mode. That degradation belongs in the provenance of the result. A useful reconstruction should make it possible to understand the origin, forcing, parameters, time window and output lineage.

The same principle applies to all modelling in SeaCommons: derivation is valuable precisely when it remains identifiable as derivation.

### How a reconstruction should explain itself

SeaCommons does not treat motion graphics or a single animated trajectory as sufficient explanation. A useful reconstruction should let the reader see the observed inputs, the modelled outputs, the origin and time window, environmental forcing, parameter versions, assumptions, degraded inputs and uncertainty spread. Time controls should reveal how the result evolves rather than merely replaying a fixed animation.

The interface should also answer a simple question in plain language: what does this reconstruction help us understand about the case? A model that cannot expose its inputs, transformations, uncertainty and analytical purpose should remain an internal experiment rather than a public SeaCommons surface.

## 09. Privacy, publication and security boundaries

SeaCommons applies privacy before presentation.

Raw humanitarian reports, contact identifiers, attachments, precise sensitive locations, internal review state and analyst notes belong to the authenticated operational plane. Public Live receives a reduced projection, not database access.

Explicit private status always wins. Unknown or blocked source policy fails closed. Raw caller text and private metadata are not copied into the public event. Geometry can be absent, coarse or area-based when the evidence does not justify a precise point.

Production authentication uses OIDC JWT validation and role-based authorisation. Provider webhooks use their own authenticity mechanisms, including signed bodies or configured secret headers. Public edge ingestion is separately authenticated and validates the public contract again.

This defence-in-depth model is deliberate. A public event must be safe at the projection boundary and valid at the edge boundary. The edge is not trusted to sanitise private records that should never have reached it.

Operational security also includes deployment controls outside application code: TLS termination, database isolation, off-host backup, secret rotation, MFA and least-privilege role assignment. Those controls are deployment responsibilities and are not exposed through the public documentation interface.

## 10. Public API and contracts

SeaCommons exposes public read interfaces for status and Public Live, while operational mutation and private material remain behind authentication.

The public status endpoint provides aggregate pipeline counts, Live category totals, sensor activity and freshness. It is intended for transparent system-state reporting and for the institutional site. It does not expose private identifiers or raw operational payloads.

- GET /api/v1/status?hours=24
- GET /api/v1/play/counts
- GET /api/v1/live/signals

The API reference is generated from the deployed OpenAPI contract and is available at **api.seacommons.org/docs** and **api.seacommons.org/redoc**.

Public clients should treat lifecycle, location precision and category vocabulary as contracts rather than display hints. Unknown values should fail closed or degrade visibly instead of being silently coerced.

## 11. Reliability, recovery and observability

SeaCommons is designed around recoverable state rather than assuming continuous connectivity.

The public edge keeps a current snapshot so Live can remain available during an API restart. The publisher uses a durable outbox so temporary delivery failure does not silently discard an event. Reconnect sends a snapshot before deltas, preventing a newly connected browser from having to reconstruct current state from partial messages.

The operational side separates source health, worker health and event freshness. A provider can be connected but quiet. A worker can be alive while an upstream source is stale. An old event can remain relevant even though its transport is currently offline. These states are measured separately.

The current reference deployment is intentionally modest. Compute-heavy drift work, provider availability and source licensing can limit throughput or coverage. SeaCommons exposes degradation rather than presenting a degraded path as equivalent to a preferred source.

## 12. Testing and evidence of correctness

The project uses several test layers because the highest-risk failures cross boundaries.

Backend tests cover ingestion, source policy, lifecycle, correlation, drift, public/private projection and authentication. Edge tests cover deduplication, ordering, restart and reconnect behaviour. Frontend domain tests cover response normalisation, Live semantics, API error handling and simulation logic. Deterministic browser tests exercise the public Live and Play host paths against controlled fixtures.

The most important tests are not visual snapshots. They assert invariants such as: private material cannot enter Public Live, stale events cannot overwrite newer lifecycle state, duplicate delivery remains idempotent, modelled drift cannot masquerade as reported geometry, and unauthorised operational mutation remains blocked.

The production build uses the same unified multi-page packaging path as deployment. This keeps the institutional site, documentation, Live, Play and API host routing inside the same release gate.

## 13. Known limitations

SeaCommons should be read with its limits visible.

Public Live is not an exhaustive representation of everything occurring at sea. Coverage varies by provider, geography, licensing, latency and sensor availability. AIS gaps are ambiguous without coverage context. Satellite observations are constrained by acquisition timing and resolution. Remote radio coverage depends on explicitly configured receivers. Human reports can be incomplete, approximate or delayed.

Automated cues do not establish motive or illegality. Corroboration does not turn an interpretation into certainty. Drift trajectories remain modelled. Approximate geometry should not be read as exact coordinates. Historical archives can have periods in which a source was unavailable or had not yet been integrated.

The system is built to make these limitations inspectable. Uncertainty is part of the record, not a cosmetic confidence label added after the fact.

## 14. Explanatory simulations and reconstructions

SeaCommons does not use simulation as decoration. A modelled view should exist only when it helps a reader understand a real analytical question and when the required inputs are present.

Simulations belong to the dossier of a case, not to the general Live surface. Each reconstruction must separate four evidentiary states: **Observed**, **Derived**, **Modelled** and **Unknown**. The interface should make those states visually distinct and should expose the inputs, assumptions and provenance that produced the modelled output.

A drift reconstruction should show the reported or otherwise explicit origin, the environmental forcing used, the simulation time window, ensemble members, the uncertainty envelope and any later observation that can be compared with the model. A trajectory without those elements is not an adequate explanation.

An AIS-gap reconstruction should show the known track before and after the gap, neighbouring traffic, available reception or provider context and alternative explanations for the missing positions. The purpose is to distinguish a potentially meaningful silence from weak coverage or upstream degradation, not to label the gap as deliberate dark activity.

A temporal case reconstruction should allow a reader to move through the evidence as it became knowable. At each cutoff, the interface should show which observations existed, which derived cues were available, which independent lineages supported the case, what remained uncertain and how the working hypothesis changed.

Model parameters and sensitivity matter. When changing a plausible input materially changes the result, that dependence should be visible. The reader should be able to understand not only the model output but why the output takes that shape and what would make it different.

A modelled result must never replace the underlying observations in the case record. Simulation is an explanatory layer over evidence, not a new source of truth.

## 15. Reading SeaCommons responsibly

SeaCommons is most useful when the reader follows the evidence chain rather than the headline label.

Start with the case category, then inspect its lifecycle. Look at what was directly observed and what was derived. Check source lineage before treating two indicators as independent. Read coordinate precision before interpreting a marker. Treat modelling as modelling. Treat sanctions, dark activity and identity anomalies as context for investigation rather than accusations.

That reading discipline is not separate from the software architecture. It is what the architecture is designed to preserve.
