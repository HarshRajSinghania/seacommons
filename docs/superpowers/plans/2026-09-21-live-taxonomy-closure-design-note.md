# 2026-09-21 — Public Live/Play taxonomy closure: design note

Status: **research + scoping complete, implementation not yet started.**
Scope decision needed from the product owner before code changes (see §7).

## 0. What triggered this

Reported symptom: production Live reports `2` items (two AIS-MOB/distress-beacon
observations near Mallorca), both with valid coordinates and valid public
properties, but **no markers render** and the incident-type dropdown looks
like it "consists mostly of `distress_beacon`" today.

## 1. Root cause (traced, not guessed)

This is a **regression I introduced in this same session**, in commit
`e3144ea` (`fix(intel): stop upgrading raw observations into hypothesis
semantics`), landed a few turns before this task started. Saying so plainly
because the user's brief explicitly asked me to check for concurrent
test-only commits before touching overlapping code — this one is mine, not a
concurrent session's, and it is the direct cause of the reported bug.

`apps/api/core/domain/incident_taxonomy.py::incident_type()` used to map
every maritime observation token (regex over `anomaly_type` /
`ais_nav_status_kind` / etc.) straight into one of a **closed, stable set**
of category names: `dark_activity`, `spoofing`, `transfer`,
`infrastructure_proximity`, `loitering`, `navigation_safety`,
`identity_integrity`, `piracy_security`, `environmental_hazard`,
`context_report`, `public_observation`, `maritime_context`. That closed set
is mirrored verbatim in the frontend's stable selector taxonomy
(`apps/web/src/main.jsx:143-179`, `SIGNALS_MACRO_GROUPS`) — the checkbox list
that drives `activeSignalCategories` (`main.jsx:2597-2622`) and gates whether
a feature with a given `incident_type` renders at all
(`activeSignalCategories.has(signalCategoryOf(props))`,
`main.jsx:2615`).

Commit `e3144ea` added a new `observation_type()` helper (a genuinely good
idea — see §3) and changed `incident_type()`'s maritime branch to return that
raw, **open-ended**, detector-specific label directly whenever no explicit
`InvestigationHypothesis` exists:

```python
observed = observation_type(...)
if observed != "maritime_context":
    return observed          # <- now leaks raw labels: distress_beacon,
                              #    ais_gap, position_anomaly, ...
```

`observation_type()` returns `distress_beacon` for
`ais_nav_status_kind == "distress_beacon"` (`incident_taxonomy.py:127-128`).
That value is **not** a member of `SIGNALS_MACRO_GROUPS`, and
`signalCategoryOf()`'s alias table (`apps/web/src/features/intel/categories.js:172-186`)
has no entry for it either — so `signalCategoryOf()` returns the raw string
`"distress_beacon"` verbatim, `activeSignalCategories.has(...)` is `false`,
and the feature is silently dropped from the map while still being counted
server-side (the count comes from an independent, unfiltered code path).

The same commit created two more instances of the identical defect for the
exact same reason: `ais_gap` (previously mapped to stable `dark_activity`)
and `position_anomaly` (previously mapped to stable `spoofing`) also now leak
raw and are **not** covered by the frontend's alias table. `rendezvous`
survives by luck — `categories.js` happens to alias it to `transfer` already.
`infrastructure_proximity`, `loitering`, `port_call` survive because their
observation-level label already equals the stable bucket name.

**This is not evidence that the previous commit's reasoning was wrong.** The
distinction it drew — a raw, unconfirmed detector observation must not claim
hypothesis-strength language ("spoofing", "dark activity", "transfer") — is
correct and matches `docs/fixes.md` §2.4 and §M0.3 verbatim ("Observation
type describes what was received or measured. It is never the same thing as
an investigative hypothesis."). The implementation mistake was conflating
two things that the frontend's own code comments already say are different:

- `observation_type` — what was actually observed, detector-specific,
  open-ended, evidence-level (`ais_gap`, `position_anomaly`,
  `distress_beacon`, `rendezvous`, ...).
- `incident_type` — the **stable, closed, UI-facing topic bucket** the
  observation is filed under (`dark_activity`, `spoofing`,
  `navigation_safety`, ...). The bucket label is deliberately hedged
  ("Spoofing / position integrity", not "Confirmed spoofing") — it names a
  *topic*, not a *conclusion*. Confidence/certainty is carried separately by
  `evidence_state`, `hypothesis_type`, `verification_status` and `facets`,
  which the same commit correctly added.

Fix: keep `observation_type()` as-is (evidence-level, open), restore
`incident_type()`'s job of mapping every possible observation into the
closed stable set (regardless of whether a hypothesis exists yet), and add a
property test that makes the closed-set membership a release invariant
instead of an implicit convention. Full plan in §7.

## 2. Is this only a naming bug, or a real architecture question?

Both — and they need to be separated rather than solved with one patch.

### 2a. The naming/taxonomy-closure bug (§1) is narrow and is fixed this packet.

### 2b. The deeper question the brief asks — should public Live be case/episode-first rather than event-first — is real, but SeaCommons has *already answered it* in its own canonical docs, and is *partway through building it*. I read `docs/fixes.md` (closed, historical) and `docs/updates.md` (current forward authority, "executable", last touched 2026-09-05) end to end. Relevant facts, not my invention:

- The canonical target pipeline is written down verbatim in `docs/fixes.md`
  §0 and `docs/updates.md` §0:
  `SOURCE INPUT → RAW OBSERVATION → NORMALIZED OBSERVATION → DETERMINISTIC
  FEATURE → CORRELATION → INCIDENT or EPISODE → ASSESSMENT/HYPOTHESIS →
  REVIEW + PUBLICATION → PUBLIC PROJECTION → REPLAY`. This is the same shape
  the brief proposes independently — I did not need to invent it, I needed
  to check whether it was actually built and wired, which is the real
  question.
- **Maritime Intelligence is already case/episode-first for the family it
  covers.** `MaritimeEpisodeDB` (`apps/api/core/db/models.py:111-134`) is a
  real aggregator: `observation_ids[]`, `feature_ids[]`,
  `independence_groups[]`, `alternative_explanations[]`,
  `evidence_fingerprint`, `verification_status`. `InvestigationHypothesisDB`
  (`models.py:578-616`) has a real lifecycle
  (`candidate → collecting → review_ready → assessed → published`),
  `reason_codes`, `counter_indicators`, `evidence_links`, audit history.
  `core/live/feed.py:529-533` (`_published_security_hypothesis_features`)
  confirms raw AIS anomaly/rendezvous/spoofing/infrastructure detector
  output is **evidence only** — it never reaches public Live directly; only
  a hypothesis that has passed the publication gate does. This is the exact
  "raw candidate noise must not be exposed publicly" invariant from the
  brief, and it is already enforced, already tested
  (`tests/test_live_feed.py`, `tests/test_replay_end_to_end.py`).
- **Maritime Safety self-reports (distress beacon, aground, NUC, restricted
  manoeuvrability) deliberately bypass the episode/hypothesis path.** They
  are single-event, single-source, time-critical operational signals, gated
  directly by freshness + corroboration + port/land context
  (`core/live/projection.py::is_useful_public_case_feature`,
  `tests/test_beacon_compartment.py`). This is correct, not a gap: the
  brief's own point 8 ("episode-first risks hiding an urgent single-source
  AIS-SART/MOB/EPIRB signal") is exactly why this family should **not** be
  forced to wait for episode formation. Bellingcat's own methodology
  (identification → collection/preservation → verification → analysis →
  review/confirm → publish — see §3) treats "is this observation itself
  credible/fresh/plausible" as a distinct, earlier gate from "has this
  been corroborated into a bigger case," which is precisely the two-gate
  shape already implemented here (`is_useful_public_case_feature` for
  plausibility/freshness, `is_independently_corroborated` for
  cross-lineage strength).
- **Humanitarian is *not* yet case-first in the sense the brief means.**
  `HumanitarianIncidentDB`'s own docstring says so explicitly
  (`models.py:619-633`): *"v0 scope, honestly bounded: `incident_id` is 1:1
  with the `IntelEvent` id that created it — no cross-source correlation
  exists yet ... this cannot yet merge two independently-reported posts
  about the same real-world case into one incident."* `docs/updates.md`
  already names this gap and has a packet number for it:
  **§7 P2.1 — CorrelationDecision** (`docs/updates.md:718-760`), listed
  under "Correlation, entity resolution and evidence graph," explicitly
  gated to start "only after the first Humanitarian vertical is canonical."
  It has not been built. So today, a `HumanitarianIncident` *is* structurally
  a case object (it owns lifecycle, `source_observation_ids[]`,
  `current_drift_id`), it just doesn't yet fuse multiple independent reports
  of one real event into one incident — it is already 1 incident : 1 (or a
  few, via `IncidentWatch` follow-up) sources, not many-report fusion.

**Conclusion:** the brief's hypothesis ("episode/case-first") is the right
target model, and it is *already SeaCommons' own documented target model*,
already substantially built for Maritime Intelligence, and missing one
specific, already-planned, already-scoped piece for Humanitarian
(cross-source correlation, P2.1). Re-deriving or reimplementing this
architecture from scratch in this packet would either (a) be superficial —
wrapping already-1:1 Humanitarian incidents in a cosmetic "case panel" that
changes nothing structurally — or (b) require building P2.1 correlation from
scratch, which is a multi-week, schema-changing, multi-source-matching
packet of its own and is explicitly out of scope for "smallest coherent
change set" / "no huge pipeline" from the brief itself.

## 3. External research

**Structured analytic techniques / intelligence tradecraft.** Case analysis
correlates information across complex inquiries while incident analysis
supports investigation of one incident/event series — the same
event-vs-case distinction SeaCommons already encodes as `IntelEvent`/
`SourceObservation` vs `HumanitarianIncident`/`MaritimeEpisode`.
[A Guide to Structured Analytic Techniques](https://greydynamics.com/a-guide-to-structured-analytic-techniques-sats-for-intelligence/),
[Cases in Intelligence Analysis](https://us.sagepub.com/en-us/nam/cases-in-intelligence-analysis/book242321).
`InvestigationHypothesisDB.reason_codes` / `counter_indicators` is
effectively a bounded Analysis-of-Competing-Hypotheses record — a
recognized SAT — not a bespoke invention.

**Bellingcat methodology.** The documented pipeline is *identification →
collection & preservation → verification → analysis → review & confirmation
(including multi-source corroboration/cross-referencing) → presentation*
([Bellingcat/GLAN methodology](https://www.bellingcat.com/app/uploads/2022/12/JA-Manual-for-PUBLICATION.pdf),
based on the EDRM). This is materially the same shape as
`docs/fixes.md`'s canonical flow, and it explicitly separates "is this one
piece of evidence verified" from "is the overall claim corroborated by
independent sources" — the same two-tier gate SeaCommons already implements
(`is_useful_public_case_feature` vs `is_independently_corroborated`).

**Sentinel-1 / satellite vessel detection.** AllenAI's
`vessel-detection-sentinels` / `sar_vessel_detect` (used in production in
Skylight and the xView3 challenge) and Bellingcat's own Sentinel-1-based ship
detection and Radar Interference Tracker
([AllenAI](https://github.com/allenai/vessel-detection-sentinels),
[xView3 whitepaper](https://github.com/allenai/sar_vessel_detect/blob/main/whitepaper.pdf),
[Bellingcat RIT](https://www.bellingcat.com/resources/2022/02/11/radar-interference-tracker-a-new-open-source-tool-to-locate-active-military-radar-systems/))
all associate detections to AIS by **acquisition-time propagation with
explicit uncertainty**, never by raw proximity, and treat an unmatched
detection as a bounded "dark candidate," never a confirmation. This matches
`docs/updates.md` §M7.2 / `docs/fixes.md` §12 (M7) verbatim, and matches
`SatelliteObservationDB` (`models.py:537-577`, not read in full this pass but
confirmed to exist with `association_status` fields per the recent
`782cf34` commit in this branch's own history). No code changes are needed
here now — the point of this research was to confirm the *existing*
episode → satellite association model doesn't need to change shape to
accommodate this later, and it doesn't: `MaritimeEpisode.observation_ids[]`
/ `feature_ids[]` can already hold a satellite-derived feature id the same
way it holds an AIS-derived one.

## 4. Alternatives considered

| Option | Verdict |
|---|---|
| **A. Alias `distress_beacon` (and the other two leaks) to their stable bucket only** | Correct as a *symptom* fix, insufficient alone per the brief's explicit instruction — leaves the class of bug (any future detector-specific label can leak the same way) unguarded. |
| **B. Rebuild Live as case/episode-first now, including Humanitarian cross-source correlation (P2.1) and a new case-panel UI** | Right long-term target, but the P2.1 correlation engine does not exist; building it now is a multi-week schema/matching/UI effort, violates "smallest coherent change set," and risks shipping a half-built correlation matcher into a safety-critical distress pipeline without the replay/precision-recall baseline `docs/updates.md` itself requires before that packet starts. |
| **C. Root-cause the taxonomy closure defect this packet (restore closed-set mapping + add a property test that makes leaks impossible to ship silently again), document the case-first architecture finding formally, defer Humanitarian correlation (P2.1) and any case-panel UI to a named follow-up packet** | **Chosen.** Fixes the reported defect and the whole class of bug behind it, is honest about what "case-first" already means for SeaCommons today, and does not touch the safety-critical evidence-gating code (`is_useful_public_case_feature`, `is_independently_corroborated`) that is already correct and already tested. |

## 5. Recommended model (confirmed, not changed, this packet)

```
observation_type   evidence-level, open-ended, detector-specific
                    (ais_gap, distress_beacon, position_anomaly, rendezvous, ...)
        │
        ▼ (closed mapping, always defined, never passthrough)
incident_type       stable, closed, UI-facing topic bucket
                    (navigation_safety, dark_activity, spoofing, transfer,
                     loitering, infrastructure_proximity, identity_integrity,
                     port_call, piracy_security, environmental_hazard,
                     context_report, public_observation, maritime_context
                     | distress, rescue, missing, shipwreck, pushback,
                     land_humanitarian, resolution, migration_incident,
                     sar_activity, humanitarian_context)
        │
        ▼ (orthogonal, never folded into the bucket)
evidence_state / hypothesis_type / verification_status / facets[]
                    how strong is this claim right now
```

`main_category` (`humanitarian` | `maritime`) stays the only top-level
public split — unchanged, already correct, already tested.

## 6. Migration / compatibility implications

- Pure function change in `core/domain/incident_taxonomy.py`; no schema, no
  migration, no new table.
- `taxonomy_fields()` is consumed by `live/projection.py`, `live/feed.py`,
  `live_edge_publisher.py`, `api/routes/play.py` — all four already spread
  its output additively (`**taxonomy`) and none of them hard-code the three
  now-fixed leaked values, so no caller-side changes are required (verified
  by reading each call site, not assumed).
- Historical `IntelEventDB` rows already persisted with the leaked raw
  `incident_type` value are re-derived at read time (`taxonomy_fields()`
  runs on every projection, nothing is written back at rest for this field)
  — so the fix is retroactive for Live and Play without a backfill.
- Frontend `apps/web/src/features/intel/categories.js` needs **no change**:
  its alias table already anticipates and correctly handles every value in
  the restored closed set.

## 7. This packet's scope (needs your go-ahead before I write code)

1. `core/domain/incident_taxonomy.py`: restore closed-bucket mapping in
   `incident_type()` for the maritime branch (distress_beacon →
   `navigation_safety`, ais_gap/long_gap → `dark_activity`, position_jump/
   impossible_speed/teleport/circle_spoof/static_spoof →
   `spoofing`, rendezvous/sts → `transfer`, infra_proximity →
   `infrastructure_proximity`, loiter/abnormal_dwell → `loitering`,
   sanctioned_port_call → `port_call`), driven off `observation_type()`
   rather than duplicating the regex, so there is exactly one place that
   knows what a detector label means. Keep the existing hypothesis-driven
   overrides (`dark_transit`→`dark_activity` etc.) as the same bucket names
   they already are — no behaviour change there, just confirms the mapping
   is consistent whichever path produced it.
2. Add `STABLE_MARITIME_INCIDENT_TYPES` / `STABLE_HUMANITARIAN_INCIDENT_TYPES`
   closed frozensets as the module's public contract, and a property test
   that calls `incident_type()`/`taxonomy_fields()` across every known
   `observation_type`/`anomaly_type`/`ais_nav_status_kind`/`hypothesis_type`
   combination in the module and asserts the result is always a member of
   the closed set — this is what makes "distress_beacon does not disappear
   from the map because it is not a top-level category" and "the stable
   selector taxonomy does not depend on currently returned incident types"
   permanent regression coverage rather than one fixture.
3. Regression fixture reproducing the exact reported production case: two
   AIS-MOB/distress-beacon observations near Mallorca → both project with
   `incident_type == "navigation_safety"`, `observation_type ==
   "distress_beacon"`, both are members of the closed set, and (using the
   existing, correct, untouched `is_useful_public_case_feature` gate) they
   are evaluated as two independent evidence observations, not asserted to
   be one casualty — matching the brief's explicit instruction not to
   assume the two beacons are the same incident.
4. Cross-check `apps/web/src/features/intel/categories.js` and
   `apps/web/src/main.jsx`'s `SIGNALS_MACRO_GROUPS` against the new backend
   closed set with a one-line comment cross-reference in both files (no
   behavioural change expected there — confirmed in §6 — but the comment
   makes the shared contract discoverable instead of implicit, which is what
   let it silently drift this time).
5. Full backend suite, targeted taxonomy/live/play/beacon-compartment
   suites, ruff, mypy on changed files, web lint/typecheck/build/tests.
6. Document the §2b architectural finding (Humanitarian correlation gap =
   already-named P2.1, not a new discovery) in `docs/current_work.md` so the
   next session doesn't re-investigate it from zero.

**Explicitly NOT in this packet** (named as the next one, §8):
- Building `CorrelationDecision` / cross-source Humanitarian incident
  fusion (`docs/updates.md` P2.1).
- Any new "case panel" UI, claim-level corroboration UX, or evidence-layer
  toggle on the map.
- Any change to `is_useful_public_case_feature`,
  `is_independently_corroborated`, `MaritimeEpisode`/`InvestigationHypothesis`
  gating logic — all already correct and already tested.
- Any satellite/Sentinel-1 pipeline work — confirmed in §3 that the existing
  data model already accommodates it later without a shape change.

## 8. Next recommended packet (not started)

**P2.1 — Humanitarian CorrelationDecision**, exactly as scoped in
`docs/updates.md:718-760`: entity/report matching across independent
sources into one `HumanitarianIncident`, with `CorrelationDecision` audit
records, before any Humanitarian "case panel" UI is attempted — building the
UI first would have nothing real to show.
