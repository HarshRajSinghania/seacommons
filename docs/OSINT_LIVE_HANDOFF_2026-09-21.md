# SeaCommons OSINT Live Handoff — 2026-09-21

This document is the canonical handoff for continuing the current SeaCommons OSINT/Live work from another agent or engineering session.

## Git state

- Repository: `suezcanalxyz/seacommons`
- Base branch: `main`
- Current remote main: `e75ce400dd0cfce03b63bc99eb6b8bf40f37bde1` (`e75ce40`)
- Working branch: `feat/ais-coverage-witness`
- PR: #206, `fix: tighten OSINT live evidence and add AIS coverage witnesses`
- Branch commits above main:
  - `33e0567 fix(osint): tighten live evidence and add AIS coverage witnesses`
  - `04f9a35 feat(osint): use community coverage and track satellite case history`
- Divergence at handoff: 0 behind / 2 ahead of `origin/main`
- PR checks at handoff: repository, API, web, edge, browser-e2e, CodeQL, Vercel all green; production smoke is conditionally skipped by workflow.
- Production has NOT been updated to PR #206 yet. Production main is still `e75ce40`.

Do not pop historical production stashes or destructively reset the production checkout. Existing operational/tmp files on the VM are intentional.

## Product contract

SeaCommons must distinguish:

1. Raw observation: durable sensor/report input.
2. Candidate/investigation: interesting but not yet suitable for a public claim.
3. Qualified public Live evidence/case: specific enough to surface publicly with explicit verification state.
4. Investigation hypothesis: stronger interpretation that remains separate from raw evidence.
5. Play: durable evidence-backed archive/dossier.

Public top-level categories remain exactly:

- Humanitarian
- Maritime

Satellite, sanctions, corroborated, SAR fleet and similar concepts are facets/evidence layers, not main categories.

Live does not require corroboration for every item. Live requires specificity. Strong claims require corroboration.

## Corroboration invariant

`corroborated` means at least two genuinely independent evidence lineages supporting the same claim.

Examples of independent lineages:

- Alarm Phone operational report
- AIS sensor lineage
- DSC/radio decoded distress
- Sentinel-1/optical/VIIRS vessel detection
- official authority/port/SAR notice
- independently geolocated visual evidence

Not independent:

- multiple AIS aggregators receiving the same AIS transmission
- multiple SDR receivers hearing the same DSC message
- multiple news articles copying one authority statement
- multiple detectors derived from the same AIS stream

Community AIS stations are therefore coverage witnesses, never a second corroborating lineage.

## What PR #206 fixes

### 1. 24h funnel semantics

The old dashboard counted hypothesis housekeeping updates as recent investigative activity.

Production replay before the fix showed roughly 3,508 expired/rejected hypothesis updates in the 24h window even though there were no active hypotheses.

The 24h funnel now excludes expired/rejected housekeeping transitions and labels the stage `Active hypotheses`.

Read-only production replay after the fix:

- active hypotheses in 24h: 0
- expired/rejected housekeeping excluded: 3,508
- real recent episodes: 3

Do not revert this to a raw `updated_at` count.

### 2. Sanctions identity hardening

A sanctions name match must not override a conflicting vessel identity.

Real replay case:

- current AIS name: TITAN
- current AIS IMO: 9126998
- sanctions record with same name included IMO 9293741

Correct result:

- `sanctions_name_only_match`
- `identity_conflict`
- NOT `sanctions_hit`

### 3. AIS safety beacon semantics

AIS-SART / AIS-MOB / AIS-equipped EPIRB must not become a public distress conclusion from one isolated ping.

Current public rule:

- single fresh ping: internal/single observation only
- repeat-confirmed active transmission: eligible as a public safety-beacon observation
- explicit test/stale/port-cluster context: suppress from public Live
- independent Alarm Phone/DSC/official confirmation may strengthen the claim

A beacon is the transmitter/device, not automatically the parent vessel or proof of casualty outcome.

### 4. Community AIS coverage witnesses

Migration: `0029_ais_coverage_snapshots`

Current Central Mediterranean public station set:

- AIS-catcher Gozo, station 3372
- NGN AIS Malta, station 1553
- AIS Strait of Messina, station 2146
- Golfo di Milazzo, station 2931
- Riace AIS Station, station 3697

For each station SeaCommons stores time-bounded snapshots of:

- station status
- last received packet
- 24h online rate
- observed maximum reception distance
- messages/hour
- vessels/hour
- station coordinates
- observation timestamp

Security invariant: public station profiles are fetched through `FixedOriginClient`; do not add direct `urlopen`/raw HTTP bypasses.

A snapshot is accepted only when fresh/healthy. Coverage is evaluated against the observed range at that time, not a made-up static radius.

For AIS gaps, healthy local coverage can now come from either:

- >=5 neighboring vessels reporting before AND >=5 after with sufficient local coverage ratio
- OR a valid community AIS coverage witness at the gap location/time

The witness also participates in dark-gap hypothesis eligibility.

It does NOT create an independent evidence lineage.

### 5. Satellite case history

Satellite enrichment no longer searches only the reported event point.

For a Humanitarian case it now builds a bounded temporal path:

- reported origin: reverse / nearest / forward scene discovery
- up to three sampled points along the current drift trajectory: nearest scene discovery

Every satellite observation records the case target that motivated the lookup:

- target role
- target lat/lon
- target time
- drift_id when applicable
- trajectory index
- search direction

If one Copernicus scene is relevant to multiple case targets, it is stored once and the `case_targets` provenance is merged.

Play exposes:

- `association_status`
- `case_targets`

Important: a satellite scene over the AOI remains `contextual`. Merely having a Sentinel scene does not corroborate a vessel claim.

## Historical replay findings

### Humanitarian

Current production DB audit:

- 67 Humanitarian incidents
- only 1 structured Claim row
- 7 SAR mission assessments
- 0 `SAME_INCIDENT` correlation decisions

There were 207 decisions marked as source-independent, but these deduplicate to only 13 observation/incident pairs. Manual replay shows the apparent independence is not enough.

Example false association:

- Alarm Phone case in/near Algeria
- SOS Méditerranée Ocean Viking rescue in Libyan SRR
- temporal difference ~2 hours
- spatial separation ~1,430 km

This must remain `UNCERTAIN`.

Conclusion: no historical Humanitarian case should currently be promoted automatically to `corroborated`. The missing work is structured historical extraction + case association, not relabelling.

### Maritime

The only historical multi-lineage material already promoted to corroborated episodes is mainly sanctions + AIS port-call evidence. Do not manufacture more corroborated cases by counting repeated AIS-derived signals.

## Current critical bottlenecks

### A. Satellite vessel association

This is the highest-value next OSINT packet.

Current state:

- Copernicus scene discovery works
- temporal case/drift path now works
- GFW unmatched SAR detections can be materialized as satellite observations
- historical satellite DB observations are mostly contextual
- there is not yet a robust generic Sentinel vessel-to-case association layer

Required next stage:

1. For each relevant acquisition time, propagate the case target to that timestamp:
   - Maritime gap: predicted vessel position/uncertainty from last AIS fix, SOG/COG and behavior model
   - Humanitarian: reported point or drift position/uncertainty at acquisition time
2. Obtain vessel detections from the scene, not merely scene metadata.
3. For every detection compute:
   - distance to propagated target
   - temporal delta
   - target uncertainty radius/ellipse
   - detection uncertainty
   - other known AIS vessels in/near the candidate area
   - sensor/resolution constraints
4. Persist a conservative association status:
   - `matched`
   - `plausible_match`
   - `unmatched_candidate`
   - `excluded`
   - `insufficient_quality`
5. Only a real physically independent associated detection may contribute a satellite lineage to corroboration.
6. Never interpret absence of a detection as proof of vessel absence unless scene quality/coverage supports that inference.

Desired OSINT result:

`AIS gap + healthy AIS coverage + independent SAR detection consistent with propagated position`

can become a serious dark-transit investigation.

### B. Radio / DSC semantic decoding

Last audited runtime showed RF transport alive but essentially no useful decoded DSC associations.

Required:

- diagnose decoder input/framing, not just receiver connectivity
- distinguish RF burst reception from a valid decoded DSC message
- persist decoded semantic fields and physical_lineage
- correlate DSC/MMSI/position/time with AIS and Humanitarian cases
- multiple receivers of one transmission increase reception confidence but remain one radio lineage
- create `RadioAISAssociation` only when identity/time/position tests pass

Desired result:

`Alarm Phone + independent DSC distress`

or

`AIS anomaly + independent DSC safety/distress message`

may support real corroboration.

### C. Historical Humanitarian backfill

The historical store is under-structured.

Backfill should run existing/new extraction over historical operational and verification posts and persist:

- people aboard/rescued/dead/missing ranges
- coordinates/place/route
- vessel descriptions
- NGO/SAR asset references
- authority references
- source-specific case/thread identifiers
- outcome/disembarkation/rescue claims

Then rerun correlation conservatively with:

- exact source/thread ID where available
- temporal bounds
- spatial compatibility
- people-count compatibility
- asset/vessel compatibility
- source independence
- contradictions

Do not use temporal proximity alone as a same-incident criterion.

The 207 historical independent decisions should also be deduplicated/compacted operationally. There are only ~13 unique pairs in the audited set.

### D. Candidate/shadow queue

Public Live must stay selective. Do not solve recall by lowering public thresholds.

Maintain an operator-only candidate layer with explicit:

- `why_not_live`
- missing evidence
- coverage status
- relevant baseline/context
- next evidence needed
- last evaluated time
- promotion reason when it becomes Live

This is the correct way to retain investigative recall while keeping public precision high.

## Live resilience and UX

A zero-case Live must look intentionally quiet, not broken.

Recommended public Live state when there are no qualified cases:

- clear text: `No qualified public cases in the current 24h window`
- last successful ingestion timestamp
- high-level source/coverage health, without exposing private/raw observations
- map remains rendered
- optional AIS receiver coverage footprint layer
- optional Civil SAR Fleet layer as an asset layer, never as cases
- UTC/local time
- data freshness indicator
- explicit distinction between:
  - no qualified public case
  - source degraded
  - source disconnected
  - API unavailable

Do NOT fill the public map with low-confidence candidates to make it visually busy.

A useful public 24h summary can show aggregate activity without exposing rejected raw events, for example:

- observations ingested
- candidates evaluated
- qualified Live cases
- corroborated claims
- source modalities currently healthy

Operator-only Live should additionally expose the shadow candidate queue and rejection reasons.

This is the best way to make Live feel complete/interactive while preserving OSINT integrity.

## Live resilience engineering checklist

Before calling Live production-resilient, verify:

### Data continuity

- API restart does not erase qualified 24h public cases.
- worker restart does not duplicate episodes/hypotheses.
- source outage is distinguishable from zero qualified cases.
- public retention uses authoritative `live_expires_at`, not browser-local timestamp heuristics.
- all source freshness clocks are UTC and explicit.
- delayed sources cannot retroactively create a misleading current Live case.

### Failure isolation

- AISstream outage does not break Humanitarian ingestion.
- Copernicus outage does not break AIS/Live.
- AIS-catcher outage only removes its coverage witness.
- radio decoder failure does not block API startup.
- one malformed external payload cannot poison the ingestion worker.
- all external HTTP uses the outbound security layer.

### Semantics

- fishing/passenger/HSC ordinary gaps remain suppressed unless stronger context exists.
- a coverage witness may strengthen a gap but never create corroboration.
- repeat AIS beacon transmission is still one AIS lineage.
- SAR responder AIS enriches the Humanitarian case and never becomes a separate distress case.
- drift is model evidence/context, not an independent incident-reporting lineage.
- sanctions name-only identity conflicts remain non-hits.
- `corroborated` always traces to >=2 independent groups supporting the same claim.

### UI

- map works on desktop/mobile at 0 cases.
- map works on desktop/mobile with Humanitarian + Maritime cases.
- Civil SAR Fleet toggle is independent from case layer.
- source degradation is visible but not alarming/sensational.
- selected Humanitarian case shows responder evidence and drift relation.
- Play shows satellite chronology and evidence state.
- stale cases disappear from Live but remain correctly archived in Play where policy allows.

## Required tests / acceptance gates

Run before merge/deploy of future OSINT packets:

1. `git diff --check`
2. outbound HTTP bypass inventory
3. Alembic has exactly one head
4. migration from current production head succeeds
5. focused unit tests for changed modules
6. full backend test suite
7. frontend tests
8. frontend typecheck/build
9. browser E2E
10. production-like replay against real DB in read-only mode where possible

Current PR #206 local gate at this handoff:

- full backend: 1967 passed, 2 skipped, 0 failed
- targeted coverage/satellite tests: green
- Ruff changed files: green
- outbound HTTP bypass inventory: green
- Alembic head: `0029_ais_coverage_snapshots`
- GitHub PR checks: green

## Production smoke after PR #206 is merged

After merging, on the Oracle VM:

1. save/inspect any dirty tracked production state before updating
2. fast-forward `main`
3. install requirements only if changed
4. run configuration validation with production env
5. run Alembic upgrade to `0029_ais_coverage_snapshots`
6. restart API, worker, live edge publisher
7. confirm:
   - `/health`
   - `/ready`
   - Live endpoint
   - operator funnel
   - receiver snapshots are being persisted
   - scheduler coverage refresh runs
   - satellite scheduler runs
8. production replay:
   - 24h active hypothesis count is not inflated by expiry housekeeping
   - a weak passenger/fishing gap stays out of Live
   - a gap inside a fresh community coverage footprint may qualify when all other criteria pass
   - coverage witness does not add a second independence group
   - single safety-beacon ping stays out
   - repeated beacon transmission behaves according to the public contract
9. confirm Live zero-case state renders normally.
10. inspect journals for new HTTP/security, DB, satellite or scheduler errors.

## Current priority order for the next agent

P0. Merge/deploy PR #206 only after current CI remains green and production safety checks are satisfied.

P1. Production-smoke the community AIS snapshots and confirm they affect real future gap qualification as designed.

P1. Build satellite vessel detection/association at acquisition time. This is the highest-value path toward genuine new corroborated Maritime cases.

P1. Fix DSC semantic decoding and create real radio-to-case/AIS associations. This is the highest-value path toward genuine new corroborated Humanitarian cases.

P1. Backfill structured Humanitarian claims and rerun conservative historical correlation.

P1. Add/finish operator shadow candidate queue with explicit rejection reasons.

P2. Improve zero-case Live UX with source health, freshness and optional coverage layers rather than low-quality public pins.

P2. Reduce Play cold-catalog latency without changing public eligibility semantics. Previous shortcut that skipped public geometry changed catalog parity and must NOT be reused.

## Non-goals

Do not:

- create a new parallel pipeline for satellite/radio
- make `corroborated` a generic confidence score
- promote an event because two detectors use the same AIS data
- promote historical correlations based only on time proximity
- treat satellite scene availability as vessel evidence
- treat drift output as an independent source
- expose raw candidate noise in public Live
- turn SAR responder behavior into a separate Humanitarian distress case
- use a sanctions name match when IMO/MMSI identity conflicts
- use direct outbound HTTP bypasses for new integrations
