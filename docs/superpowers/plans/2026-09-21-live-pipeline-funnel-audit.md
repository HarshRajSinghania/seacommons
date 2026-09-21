# 2026-09-21 — Live pipeline funnel audit: why raw volume doesn't reach Live

Status: **audit complete, no code changed in this pass.** Scope decision
needed before implementation (see §5).

## 0. The question

"Live shows almost nothing while raw ingestion volume is high — where is the
breaking point, and can Live show the engine is working instead of looking
dead?" Traced every stage of `docs/fixes.md`'s own canonical pipeline
(`RAW → NORMALIZED → DERIVED → EPISODE → HYPOTHESIS → REVIEW/PUBLICATION →
LIVE`) against the actual code, end to end, file by file.

## 1. Headline finding

**The Maritime Intelligence funnel has a wired gap, not just strict gates.**
A fully built, unit-tested review/publication mechanism
(`core/review/maritime.py::apply_maritime_review`) that performs exactly the
`review_ready → assessed → published` transition documented in
`docs/fixes.md` §M6 is **never called from any HTTP route**:

```
grep -rn "core.review" apps/api/core/api/routes/*.py   -> zero matches
grep -rln "apply_maritime_review"                       -> only test files
```

Traced the automatic engine that *does* run in production
(`core/intel/hypothesis_engine.py::evaluate_episode`, lines 207-295): it
auto-transitions `candidate → collecting` (line 275) and
`collecting → review_ready` (line 291) — both driven purely by evidence
already present. **Nothing in the automatically-running pipeline ever calls
`transition(hyp, "assessed", ...)` or `transition(hyp, "published", ...)`.**
`core/intel/hypothesis_publication.py::public_hypothesis_collection()` (the
sole function that feeds public Maritime Intelligence Live,
`core/live/feed.py:531-533`) only reads
`list_hypotheses(state="published", ...)`. So:

**Every Maritime Intelligence hypothesis that becomes fully corroborated —
however good the evidence — gets stuck at `review_ready` forever, because
there is no product surface through which a human operator can approve it.**
It is visible read-only in the operator dashboard's "cases" panel
(`operator_ingestion.py` — confirmed a GET-only listing with a `blockers[]`
field, including `NOT_REVIEW_READY`, but no accompanying action endpoint),
and it is durable in Play (`play.py:147` includes `review_ready` in
`_PUBLIC_PLAY_INVESTIGATION_STATES`), but it can never reach public Live.

This traces back to the "Review v0 / publication controls" packet
(`docs/current_work.md`, accepted/closed at `19dbb7d`): its own summary says
*"Humanitarian approvals create audited incident transitions, while Maritime
approvals delegate to the existing hypothesis state machine."* The domain
logic (`ReviewRecord`, `apply_maritime_review`, `apply_humanitarian_review`,
the append-only ledger) is real and tested
(`tests/test_maritime_review_apply.py`, `tests/test_humanitarian_review_apply.py`)
— it was accepted as "development-complete," which was true for the domain
layer, but no route/UI was ever built on top of it. That packet closed one
layer short of the operator actually being able to use it.

**This is, with high confidence, the single biggest reason raw volume
doesn't turn into visible Live cases on the Maritime side.** It is a
five-file wiring gap (one route, one minimal operator UI action, tests),
not a redesign.

## 2. The rest of the funnel, stage by stage (with the gate at each step)

The funnel itself is already instrumented and countable:
`GET /api/v1/operator/ingestion/funnel` (`operator_ingestion.py:626-825`,
operator-auth only) already returns `raw → normalized → derived → episodes
→ hypotheses → corroborated → review_ready → live` stage counts plus
diagnostics (event types, analysis states, episode families, hypothesis
reason/counter-indicator histograms). **This did not need to be built — it
already exists and already answers "where does volume drop."** I could not
run it against real numbers (no database is attached in this checkout), but
its query logic is itself the evidence for the gates below.

```
RAW OBSERVATIONS  (SourceObservationDB)
   │  no gate — every inbound envelope is persisted, lossless
   ▼
NORMALIZED EVENTS  (IntelEventDB)
   │  no additional gate at this step
   ▼
DERIVED CUES  (ais_anomaly / correlated_alert / vessel_identity / dark_candidate,
   │           or analysis_state in {anomaly, evidence_candidate, evidence})
   │  gate: core/mda/watch.py + core/mda/offshore_context.py
   │    - offshore-only (qualify_offshore_anomaly requires context["offshore"])
   │    - anomaly_context_suppression(): low-specificity families (gap,
   │      rendezvous, loiter, infra_proximity, circular_pattern, static
   │      position) get suppressed with a reason code for passenger/HSC
   │      vessel classes running scheduled/baseline-consistent service
   │      (this session's earlier packet, f06bc85) — correct by design,
   │      reduces class-baseline false positives, not a bug
   ▼
EPISODES  (MaritimeEpisodeDB)
   │  gate: hypothesis_engine.py::_should_persist_episode (lines 185-205)
   │    only persists when: safety/port_call family + signal/evidence state,
   │    OR cross_modal_investigation_ready, OR verification==
   │    multi_source_corroborated / independent>=2, OR signal_count>=2 AND
   │    evidence_count>=2. A single raw anomaly never becomes a persisted
   │    episode by itself (correct — docs/fixes.md M5.2)
   ▼
HYPOTHESES  (InvestigationHypothesisDB, state=candidate)
   │  gate: hypothesis_eligibility.py's per-type gates (hypothesis.py
   │    dark_transit_gate / covert_rendezvous_gate / position_spoofing_gate
   │    / etc.) — each independently requires >=2 distinct pieces of
   │    evidence, never a single raw observation (correct — M6 exit gate)
   ▼
COLLECTING  (state=collecting)
   │  automatic (hypothesis_engine.py:272-276)
   ▼
REVIEW_READY  (state=review_ready)
   │  automatic (hypothesis_engine.py:283-292): requires evidence_stage in
   │    {corroborated, assessed, confirmed} AND >=2 distinct evidence_links
   │    AND non-empty reason_codes — correct, matches M6
   ▼
ASSESSED / PUBLISHED  (state=assessed / published)
   │  ███ NO AUTOMATIC OR OPERATOR-ACCESSIBLE PATH EXISTS ███
   │  apply_maritime_review() can do this correctly but is never called
   │  from any route (§1)
   ▼
PUBLIC LIVE (Maritime Intelligence)
```

Separately, **Maritime Safety self-reports** (distress beacon, aground, NUC,
restricted manoeuvrability) bypass the episode/hypothesis path entirely by
design (confirmed and tested in the previous packet this session,
`tests/test_beacon_compartment.py`) — gated instead by
`is_useful_public_case_feature()`'s own freshness/corroboration/port-context
checks. That path is not affected by the finding above; it is a separate,
correctly-functioning, single-source-urgent-signal lane.

## 3. A second, structural contributor (by design, but worth naming)

Even once §1 is fixed, corroboration itself will stay hard to reach for
purely-AIS-derived anomalies, because SeaCommons correctly enforces that
**multiple AIS feeds are not independent evidence** (this session's earlier
finding, `docs/DATA_FLOW.md`: "Provider multiplicity... never turns one AIS
broadcast into multiple independent intelligence sources"). Independent
corroboration for a Maritime Intelligence hypothesis realistically needs a
second *modality*: radio, satellite, a human/NGO/official report, or a
genuinely independent detection source. Checked the state of each in this
production line:

- **Radio** (`core/radio/*`): disabled by default in production
  (`docs/current_work.md`: physical-receiver-gated, off unless an operator
  explicitly authorizes a cutover).
- **Satellite** (`core/intel/satellite_observation.py` /
  `darkship_cue.py`): a bounded 30-minute job checks at most six recent
  incident-level events per run (`docs/updates.md` §22) — sparse by
  construction, not a systematic second sensor on every anomaly.
- **Global Fishing Watch** (`core/intel/gfw_monitor.py`): fully implemented,
  explicitly built *"as corroboration for our own AIS analysis"* (its own
  docstring), covers the whole Med + Black Sea for free, non-commercially —
  and is a **complete no-op in this environment** because
  `config.GFW_API_TOKEN` is unset (`gfw_monitor.py:108-110`,
  `config.py:208` defaults to `""`). This is not a missing capability, it is
  an unset credential.

So even after wiring the review action, the Maritime funnel will stay thin
until at least one genuinely independent modality is actually flowing.
Turning on GFW is the lowest-risk, zero-new-code lever available (§5).

One open item flagged, not confirmed: `hypothesis_engine.py::
_attach_cross_modal_evidence` (lines 41-118) hardcodes
`source_lineage="ais_sensor_lineage"` for **every** event of type
`ais_anomaly`/`ais_rendezvous`/`ais_spike` (lines 61-67), regardless of
`event.source`. If a GFW-sourced event is normalized into one of those
`event.type` values, this specific cross-modal packet builder could tag it
into the *same* lineage bucket as SeaCommons' own AIS pipeline, which would
silently defeat GFW's purpose as independent corroboration in this one code
path (the separate `contributing_independence_groups` path used by
`is_independently_corroborated()` was not fully traced against GFW-sourced
metadata this session — I'm flagging this as **needs verification with real
GFW data before relying on it**, not asserting it as a confirmed bug).

## 4. Play

Play's design is already exactly what was described: `_PUBLIC_PLAY_INVESTIGATION_STATES`
(`play.py:147`) admits `review_ready | assessed | published | rejected` —
i.e. Play already shows an investigation once it has cleared the evidence
gates, without waiting for the (currently unreachable) `published`
publication step, and separately gives resolved/terminal Humanitarian
incidents and completed satellite/Sentinel association updates their own
continuing lifecycle (`docs/updates.md` §22's `current_drift_id`/incident
timeline model, satellite `reverse`/`nearest`/`forward` observations).
**Play does not need a change for the "only final, checked data, with an
update lifecycle" requirement — it already does this.** The reason Play
today shows more investigation content than Live is not a Play defect: it
is the direct downstream consequence of §1 (Live requires `published`, Play
accepts `review_ready` and above), which is exactly correct product design,
not a bug on the Play side.

## 5. What's missing — external research

Searched recent (2026) open-source projects for two distinct needs: (a) a
review/triage surface, (b) additional independent evidence sources.

**(a) Review/triage tooling.** Recent open-source human-in-the-loop review
tools found (Latitude, Confident AI, Braintrust — all MIT/self-hostable,
2026) are built for LLM/agent-evaluation annotation queues, not
domain-specific investigative review with typed fields like
`reason_codes`/`counter_indicators`/`evidence_links`/`blockers`. Adopting
one would mean re-modelling `ReviewRecord` into a generic annotation schema,
running a second service, and mapping its output back into
`apply_maritime_review()` — strictly more moving parts and more
destabilization risk than the actual fix, which is: wire the **already
correct, already tested** `apply_maritime_review()` to one new route plus a
minimal action in the existing operator dashboard cases panel
(`operator_dashboard.py` already renders `blockers[]`/`possible_meaning`/
`illegal_activity_status` per case — an "Approve" button posting a
`ReviewRecord` is additive to that same view, not a new surface).
**Recommendation: do not add an external tool for this. Wire the internal
one.**

**(b) Independent evidence sources**, already-integrated or newly found:

- **Global Fishing Watch** — already integrated, dormant, zero-cost, zero
  new code (§3). Register a free `GFW_API_TOKEN`.
  [gfw-api-python-client](https://github.com/GlobalFishingWatch/gfw-api-python-client),
  [GFW APIs](https://globalfishingwatch.org/our-apis/) (encounters,
  loitering, AIS-disabling gaps, Sentinel-1/2 SAR-matched detections and
  fixed-infrastructure detections — all free, non-commercial).
- **AllenAI `vessel-detection-sentinels` / `sar_vessel_detect`**
  ([GitHub](https://github.com/allenai/vessel-detection-sentinels)) —
  production-grade, open-weights Sentinel-1 vessel detection (the same
  model family running in Skylight). Confirmed compatible with the existing
  data model without a shape change (previous packet this session, §3 of
  that design note) — but this is a genuinely new capability (inference
  worker), correctly out of scope for "without destabilizing, no
  regressions." Named here as a candidate for a dedicated future packet
  (`docs/fixes.md` §M7.3 already anticipates exactly this: "any CFAR/ML SAR
  processing must run outside the API process, bounded queue, timeouts").
- Nothing else found this pass materially changes the picture — the
  constraint isn't a missing detector, it's that the two detectors/sources
  already built (GFW) or already scoped (Sentinel-1 ML) either aren't
  turned on or aren't started, and the review step that would let corroborated
  evidence reach Live doesn't exist yet.

## 6. Recommended packet (not started — needs your scope confirmation)

1. **Wire `apply_maritime_review` to a route.** One new endpoint (e.g.
   `POST /api/v1/operator/review/maritime/{hypothesis_id}`) that builds a
   `ReviewRecord` from an authenticated operator action and calls the
   existing `apply_maritime_review()` — no change to the domain logic
   itself, it is already correct and tested. Add the equivalent for
   `apply_humanitarian_review` if it has the same gap (not yet checked this
   session — first thing to verify before starting).
2. **Minimal operator UI**: one "Approve" / "Reject" / "Needs more evidence"
   action in the existing cases panel (`operator_dashboard.py`), calling #1.
3. **Turn on GFW**: register `GFW_API_TOKEN`, verify in staging that
   GFW-sourced events actually register as an independent lineage group
   (resolves the §3 open item empirically) before relying on it for
   corroboration.
4. **A public, count-only funnel** on Live/the public status endpoint —
   reusing the *shape* of the existing operator funnel
   (`raw → normalized → derived → episodes → hypotheses → review_ready →
   published`) but exposing counts only, no candidate content, no per-item
   detail — this is what would make Live "show the engine is working" even
   on a quiet day, without violating "raw candidate noise must not be
   exposed publicly" (this session's earlier invariant #10). `/api/v1/live/
   pipeline` already exists but only reports source connectivity, not
   volume — this would be a second, small, additive endpoint or a field
   added to it.
5. Full regression suite + focused hypothesis/review/live tests + a replay
   fixture proving a hypothesis that reaches `review_ready` still cannot
   reach Live without an explicit `ReviewRecord`, and does reach Live once
   one is applied.

Not proposing to touch any evidence gate, corroboration rule, or episode
threshold — all confirmed correct by design this session. The fix is
wiring, not loosening standards.
