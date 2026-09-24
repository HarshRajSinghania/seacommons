# Public glossary

Status: public-facing terminology for Live labels. Last reviewed: 2026-09-24.

This glossary explains labels that appear on [Public Live](https://live.seacommons.org) and in the public documentation. It is not an incident catalogue and does not describe operational locations, personal data, or real-time distress details.

A **label is not a confirmed event**. Public Live projects observations, derived cues and modelled context. Readers should treat each item as evidence with a stated role, not as a completed investigation.

For the full evidence model see the public technical reference (`apps/web/src/docs/content/SEACOMMONS.md`) and [PUBLIC_DOCUMENTATION.md](PUBLIC_DOCUMENTATION.md).

## Incident families

### Humanitarian

The public family for people-centred search-and-rescue and related reporting: distress calls, missing-person or vessel reports, and updates about people at sea. Humanitarian items are still observations or assessments. They are not a dispatch ticket and do not replace maritime rescue coordination authorities.

A Humanitarian label does **not** establish that a rescue is underway, that a count of people is exact, or that an incident has been independently confirmed.

### Maritime

The public family for vessel, radio and domain-awareness context: AIS tracks and gaps, radio (DSC / NAVTEX) interpretations, infrastructure proximity, and other maritime-safety cues that are not themselves a people-centred distress case.

A Maritime label does **not** establish illegality, intent, or that a vessel is in distress. It is context for reading the map, not an accusation.

## Terms used on Live and in the docs

### Observation

A recorded claim that something was seen, received or published: a radio message, an AIS position report, a human report, or a satellite acquisition. An observation has source lineage. It is not automatically an incident.

Does **not** establish: that the claim is complete, accurate, or independently confirmed.

### Derived cue

An analytical output from a rule or model applied to observations. Examples in the public docs include AIS integrity concerns, dark-gap context, rendezvous context, infrastructure proximity, or a bounded radio interpretation.

Does **not** establish: a factual finding, motive, or that an incident occurred. A cue can make a situation worth investigating; it is not the investigation.

### AIS (Automatic Identification System)

A shipborne broadcast of identity and position used for tracks, coverage reasoning and bounded anomaly detectors. The same broadcast received through more than one provider remains one observation lineage.

Does **not** establish: that a missing position is deliberate, that a navigation status is independently verified, or that a vessel is in distress. AIS navigation status is a transponder report; operational cause is not independently confirmed from AIS alone.

### DSC (Digital Selective Calling)

A structured maritime radio protocol. In SeaCommons, configured receivers can contribute DSC (and NAVTEX) evidence with explicit physical-receiver lineage.

Does **not** establish: that a decoded message is a confirmed distress incident, that the transmitter location is exact, or that every reception implies an emergency. Coverage depends on explicitly configured receivers.

### Dark activity / AIS gap

Public language for a period without expected AIS positions, sometimes shown with neighbouring traffic and reception context. The architecture treats gaps as ambiguous until coverage and provider health are considered.

An AIS gap is an **observation of missing reports**, sometimes wrapped in a **derived cue**. It does **not** establish intentional silence, "going dark", smuggling, or a distress event. Reception outages and upstream degradation must not be described as intentional dark activity.

### Corroboration

Independent evidence that supports a defined proposition. Independence follows source lineage, not the number of detectors or transport routes. Reprocessing the same material through several rules does not manufacture corroboration.

Does **not** establish: certainty, illegality, intent, or responsibility. Corroboration means independent support for a stated claim, proportionate to what the evidence can show.

### Public Live projection

The reduced, privacy-filtered map at `live.seacommons.org`. Raw reports, contact identifiers, attachments, precise sensitive locations and internal review state stay on the authenticated plane.

Does **not** establish: that Public Live is an exhaustive picture of activity at sea. Coverage varies by provider, geography, licensing, latency and sensors.

## How to read a label

1. Note the family (Humanitarian or Maritime).
2. Separate what was **observed** from what was **derived** or **modelled**.
3. Check whether two indicators share a source lineage before treating them as corroboration.
4. Treat AIS gaps, sanctions context and identity anomalies as investigation context, not accusations.
