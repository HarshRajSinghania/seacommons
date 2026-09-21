# Current work — Evidence Fusion Development Loop

> **2026-09-21 current OSINT/Live handoff:** see `docs/OSINT_LIVE_HANDOFF_2026-09-21.md`. It supersedes the older release/baseline notes below for the active Live/Play/AIS-coverage/satellite work. Current review branch is `feat/ais-coverage-witness`, PR #206, based on `origin/main` `e75ce40`.

> **Canonical loop:** `docs/superpowers/plans/2026-09-06-evidence-fusion-development-loop.md`
> **Current packet:** none — Packet J accepted/closed
> **Last closed packet plan:** `docs/superpowers/plans/2026-09-09-outbound-http-ssrf-hardening-v1.md`
> **Release-qualified main:** `6202aa2`
> **Production behavior baseline:** `6202aa2`
> **Production schema:** `0026_radio_ais_associations`

## Production baseline

Packets H, I, and J are accepted and production-verified. Release-qualified `main` and the current production behavior baseline are `6202aa2`. Public Live uses the canonical Humanitarian/Maritime split and `/api/v1/live/pipeline` reports the bounded acquisition families `ais`, `first_party`, `partner`, `public_feed`, and `radio`. The AIS runtime remains in the authorized legacy mode; raw AIS context layers are default-off on public Live and remain user-toggleable.

The Mediterranean radio mesh is live with ephemeral Listen Live audio and managed receiver coverage/failover. The production policy is reconnect-before-failover for transient receiver drops, three desired independent active lineages for the monitored channel when capacity exists, and ranked standby promotion only when reconnect/staleness rules require it. Audio Evidence persistence remains disabled (`AUDIO_EVIDENCE_ENABLED=false`); Listen Live is ephemeral PCM with `no-store` and no persistent audio body.

Final Packet H evidence on the current production line: full backend `1632 passed, 2 skipped`; focused H backend `110 passed`; Live web `65 passed`; Full CI and CodeQL green on `b358dec`; Vercel production READY; Alembic `0026_radio_ais_associations (head)`. Publication/privacy gates remain fail-closed and physical receiver lineage remains the radio independence boundary.

## Completed development packets

### Free/Open AIS Fusion v1

Development-complete and shadow-ready. It adds the compatibility-preserving AIS event bus, normalized provider/health contract, AISStream + Open Waters/aiscast adapters, conservative reconciliation, coverage-aware gap reasoning, reconciled SAR context, bounded observability, and `legacy | shadow | fused` runtime modes. Runtime remains `legacy` unless an operator explicitly authorizes a cutover.

### Humanitarian Verification v1

Development-complete and review-ready through `961c436`. Alarm Phone aliases resolve to one `operational_origin` identity; operational SAR NGOs resolve to `verification`; IOM Missing Migrants remains `archive_reference`. Real monitored NGO X handles normalize to their canonical source identities before transport reasoning.

Verification sources extract deterministic claims, associate only on strong multi-feature matching, and update replayable `sar_mission` / `resolution` assessments without creating another Humanitarian incident or mutating canonical lifecycle. AIS alone cannot confirm rescue. Verification events with no deterministic Humanitarian claim do not create correlation noise. Operator summaries and metrics preserve the Humanitarian privacy boundary.

Release evidence: focused privacy/lineage `141 passed`; full backend `1350 passed, 2 skipped`; Ruff/mypy/migrations/Python dependency audit green; web test/lint/typecheck/build/audit green; edge `12 passed`, Wrangler dry-run and audit green.

## Completed packet — Remote Maritime Radio v1

Development-complete / review-ready through `6a4f885`. Provider-neutral contracts, explicit bounded receiver registry, KiwiSDR/OpenWebRX adapters, canonical SourceObservation persistence, disabled-by-default runtime, bounded health/metrics, and operator-safe status are implemented. Physical receiver lineage is authoritative for evidence source identity; frontend/provider multiplicity cannot inflate independence. No continuous audio/IQ is persisted.

Release evidence: focused radio/privacy/lineage `82 passed`; full backend `1385 passed, 2 skipped`; Ruff/mypy/migrations/canonical project dependency audit green; web/edge gates green. Production receiver activation remains separate and unauthorized.

## Completed packet — DSC + NAVTEX Structured Evidence v1

Development-complete / review-ready through `f09afcb`. Already-decoded DSC/NAVTEX inputs normalize into immutable structured observations keyed to physical receiver lineage. DSC distress may project only a Maritime Safety candidate; NAVTEX remains context-only. No waveform/audio/IQ is persisted, and no signal type creates or resolves Humanitarian state.

Release evidence: focused `116 passed`; full backend `1424 passed, 2 skipped`; Ruff/mypy/migrations/canonical dependency audit green. Structured runtime remains disabled by default and production activation is separate.

## Completed packet — Audio Evidence v1

Development-complete / review-ready through `90d08e4`. Immutable bounded audio artifact metadata, migration `0022_audio_artifacts`, disabled-by-default acquisition policy, and derived-only transcript contracts are implemented. Production capture remains unauthorized.

Release evidence: focused audio/privacy/provenance `80 passed`; full backend `1458 passed, 2 skipped`; Ruff/mypy/migrations/canonical dependency audit green.

## Completed packet — Cross-modal Evidence Fusion v1

Development-complete / review-ready through `52efdd1`. Evidence packets preserve source lineage and modality; AIS provider multiplicity collapses to one AIS modality; derived AIS/audio evidence never adds independence. Contradictions remain explicit records rather than confidence averages. Humanitarian ResolutionAssessment and MaritimeEpisode receive bounded context only, with no lifecycle/publication/hypothesis-state mutation and no receiver/MMSI leakage.

Release evidence: focused privacy/lineage `108 passed`; full backend `1483 passed, 2 skipped`; Ruff/mypy/migrations/dependency audit green; web/edge gates green.

## Completed packet — Review v0 / publication controls

Development-complete / review-ready through code HEAD `19dbb7d`. ReviewRecord is immutable and replay-deterministic; migration `0023_review_records` adds the append-only ledger; Humanitarian approvals create audited incident transitions, while Maritime approvals delegate to the existing hypothesis state machine. `published` is not a ReviewRecord transition and all publication remains behind existing domain-specific gates.

Release evidence: focused review/privacy/publication `181 passed`; full backend `1519 passed, 2 skipped`; Ruff/mypy/migrations through `0023`/dependency audit green; web and edge gates green. Exact-diff review fixes are included.

## Current execution packet

No packet is currently active. Packet J — **Outbound HTTP / SSRF Hardening v1** — is accepted/closed. PR #164 established the canonical outbound trust boundary and PR #165 added the semantics-preserving Play catalog prefilter required by production qualification. Release-qualified `main` is `6202aa2`; schema remains `0026_radio_ais_associations`.

Packet J production evidence: exact-head Full CI run `34755443674` completed SUCCESS with `repository`, `api`, `web`, `edge`, `browser-e2e`, and real-host `browser-production-smoke` all green. The API job reports `1715 passed, 2 skipped`; the Humanitarian contract gate reports `58 passed`. Web production dependencies audit at zero known vulnerabilities after the MapLibre 6.5.0 security update. The outbound smoke accepted the public HTTPS release asset and blocked a loopback HTTPS target with `blocked_target`.

The v1 trust boundary uses `PUBLIC_UNTRUSTED`, `PUBLIC_FIXED`, and `OPERATOR_INTERNAL`, DNS/IP pinning, manual redirect validation, bounded response contracts, redacted failures, and bounded metrics. The checked-in legacy inventory contains 29 exact path/callee entries representing 44 direct HTTP calls; CI rejects any increase. Fixed collector batches and WebSocket URL governance remain explicit follow-up work rather than hidden exceptions.

Production qualification also closed the Live/Play defects exposed during this packet: Alarm Phone reply monitoring now re-checks durable Humanitarian records rather than the volatile 600-event deque; resolved incidents leave operational Live but remain in Play; Play is a complete public catalog rather than a 24-hour/positioned-only subset; pagination has no fixed 100-page ceiling; and map-pin geolocation has a north-up fallback that propagates large uncertainty instead of false precision. The Pozzallo case `2c39cf93` is resolved and retained in Play with its OCR coordinate; the Crete case `7232dafc` is resolved and retained in Play with `media_pin_landmark` position `34.10776, 25.10054`, approximately 70.5 km uncertainty, and `machine_ocr_unverified`. Both are absent from operational Humanitarian Live.

Current Play production verification after PR #165: counts and first page agree at `226` public records (`54` Humanitarian, `172` Maritime), the first page returns all 226 unique records with `next_offset=null`, and both verified historical cases remain present. First-page latency on the Oracle API fell from about 20.5 s before the prefilter to about 4.65 s while preserving the same canonical `public_intel_feature()` publication/privacy gate.

Packet I remains accepted/closed with deterministic Chromium as a blocking Full CI gate and a read-only production smoke path. No AIS fusion cutover or audio evidence persistence is authorized; AIS runtime remains legacy and `AUDIO_EVIDENCE_ENABLED=false`.

## 2026-09-21 — Live/Play stable incident taxonomy closure

Plan: `docs/superpowers/plans/2026-09-21-live-taxonomy-closure-design-note.md`.

Production defect (self-introduced same session, commit `e3144ea`): `incident_type()` started returning raw, open-ended `observation_type` labels (`distress_beacon`, `ais_gap`, `position_anomaly`) directly instead of mapping them into the closed, stable topic-bucket vocabulary the public selector taxonomy (`apps/web/src/main.jsx` `SIGNALS_MACRO_GROUPS`) is built from. A raw label with no matching bucket/alias silently drops the feature from the Live map filter while the backend Live count still includes it — reproduced with the reported real case (two AIS-MOB/distress-beacon observations near Mallorca).

Fix: `core/domain/incident_taxonomy.py` now maps every `observation_type` value into `STABLE_MARITIME_INCIDENT_TYPES` / `STABLE_HUMANITARIAN_INCIDENT_TYPES` (closed frozensets, e.g. `distress_beacon → navigation_safety`, `ais_gap → dark_activity`, `position_anomaly → spoofing`) before it can reach a public projection, with a property test (`tests/test_incident_taxonomy.py::test_incident_type_is_always_a_member_of_the_closed_stable_set`) that fails closed if a future detector label has no bucket. Frontend `SIGNALS_MACRO_GROUPS`/`categories.js` needed no behavioural change (both already contain exactly the backend's 13 maritime + 10 humanitarian stable keys) — added cross-reference comments only.

Architectural finding from the same investigation, recorded so it is not re-derived from zero next session: the "should Live be case/episode-first" question was researched (internal docs + external OSINT/Bellingcat/Sentinel-1 methodology) and answered — it already is SeaCommons' own documented target model (`docs/fixes.md` §0, `docs/updates.md` §0), and Maritime Intelligence already implements it (`MaritimeEpisodeDB` + `InvestigationHypothesisDB` gate all raw AIS anomaly/rendezvous/spoofing detector output from public Live; only a published hypothesis reaches it — `core/live/feed.py::_published_security_hypothesis_features`). The real, named, not-yet-built gap is Humanitarian cross-source correlation: `HumanitarianIncidentDB` is explicitly documented 1:1 with its originating event (`apps/api/core/db/models.py:619-633`, "no cross-source correlation exists yet"), and that work is already scoped as **`docs/updates.md` §7 P2.1 — CorrelationDecision**, gated to start only after the Humanitarian vertical is canonical. Building a case-panel UI or a correlation matcher was deliberately deferred out of this packet (design note §4/§7) — recommended as the next packet.

## Loop order

1. Remote Maritime Radio v1 — software-only receiver abstraction, identity/capability/health, KiwiSDR/OpenWebRX adapters, bounded observations.
2. DSC + NAVTEX structured evidence.
3. Audio Evidence v1 — immutable bounded audio artifacts with explicit retention; transcription remains derived evidence.
4. Cross-modal Evidence Fusion v1 — combine Humanitarian, AIS and radio while preserving lineage independence.
5. Review v0 / publication controls on top of real evidence workflows.

## Core domain rules

Alarm Phone is currently the Humanitarian incident-creation authority, not the only Humanitarian source. Source identity is resolved before transport lineage: two distinct first-party organizations can remain independent even when both publish through X, while two transports/frontends for the same organization or physical receiver do not multiply evidence.

NGO AIS behaviour may establish `response_detected` or `rescue_activity_probable`, but AIS alone never confirms rescue. Explicit first-party rescue claims can contribute to `rescue_confirmed` only after strong association with the Alarm Phone incident; ambiguous matches require review.

Radio/audio follows the same model: adapters produce observations/artifacts; source/receiver identity and physical lineage are preserved; derived decodes/transcripts/claims cannot silently mutate canonical lifecycle.

## Execution discipline

TDD RED -> GREEN for every behavior change, one semantic commit per task, focused regression gate before advancing, full release gate at packet completion, and exact diff review before merge readiness. Production deploy/migration/restart remains separately controlled.
