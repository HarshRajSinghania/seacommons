# SeaCommons — brand strategy

This document is the source of truth for positioning, voice, color semantics and
motion rules across the public site (`apps/web/src/site/`) and the operator
console (`apps/web/src`). It exists so branding decisions don't get re-litigated
implicitly, one component at a time, in future UI work.

## Positioning

SeaCommons is a single master brand with a **modular service portfolio** — it
does not hide its full capability set behind a purely humanitarian front. The
site publicly describes every service family it provides:

- **Fusion** — multi-source normalisation of maritime distress signals, drift
  simulation. The founding, most-developed capability.
- **MDA** (Maritime Domain Awareness) — vessel identity/spoofing detection,
  shadow-fleet correlation, sanctions/grey-zone corroboration, chokepoint and
  infrastructure analytics. Described publicly (`apps/web/src/site/sections/MDA.jsx`)
  at a methodological level; no operational data or live case detail is exposed
  outside the OPERATIONAL/O2 access tier.
- **Engine** — physically calibrated Unreal renderer for the same drift-scene
  record used by Play.
- **Surfaces** — Play (public/synthetic demo), Live (public, privacy-filtered
  feed), Console (operator, authenticated).

There is one wordmark, one palette, one typographic system across all of this.
Modules are distinguished by label/copy, never by a dedicated identity color —
see Color semantics below for why.

## Voice

Formal, epistemic, never promotional. Every public-facing section — including
MDA — talks about *what is detected and under what evidentiary guarantee*
(corroboration required, human review boundary, confidence not verdict), not
about capability in intel-marketing terms. Reuse the vocabulary already
established in Governance/SystemView: uncertainty, provenance, contestability,
correction by design, dual-use assessment.

The console's functional UI copy (labels, badges, tooltips) can stay dense and
technical — it's operational UI, not marketing — but it shares the same color
and typography system as the site.

## Palette — dark navy, azure brand accent

Original identity was a dark green-black surface with a lime-green brand
accent. Both read as "green," which the founder rejected outright — the
surface and the brand accent are now both in the navy/blue family (no lime,
no green, anywhere):

- **Surface** (`--sc-ink-900` … `--sc-ink-700`, `apps/web/src/ui/ui.css`): dark
  navy ramp (`#060a14` → `#111f3b`), replacing the old green-black ramp
  (`#04100d` → `#143029`). Applies to the site, the console (`suez-theme.css`,
  `body` background), and every hardcoded panel/card background that used to
  match the old ramp by literal hex.
- **Brand accent**: the token formerly named `--sc-lime` (`#c8ff3d`) is now
  `--sc-brand` (`#c7dcf5`, cold ice-blue-white — an azure/cyan mid-point was
  tried first and rejected as too "neon tech"). Same role — CTA, wordmark,
  focus ring, primary emphasis — never a status color. Every hardcoded color
  literal derived from it (`ui.css`, `site.css`, `suez-theme.css`, `site.html`,
  `index.html`, the `SystemView` drift-figure SVG gradient) points at this
  value; keep it that way rather than re-hardcoding hex in new components.

Card/tile border tones used for visual rhythm (e.g. `.wp-card--blue/lime/paper/amber`
in Programme and MDA sections, `.env-card--lime/paper/sea` in Environments) keep
their existing slot names (`--lime` etc.) as arbitrary variant identifiers —
renaming those is pure naming hygiene with no visual effect, since the
underlying `var(--sc-brand)` they resolve to already carries the new color.

## Status colors — fixed, brand-wide

Also defined in `apps/web/src/ui/ui.css`, distinct from the brand accent above.
Reused identically on the site (live signal strip, stat bands) and in the
console (MDA/intel severity badges, alerts). A module is never assigned one of
these as its identity color — if e.g. rose were "the MDA color," every MDA
badge would read as critical even when nominal, which breaks the one thing a
severity system has to do reliably.

| Token | Meaning | Never used for |
|---|---|---|
| `--sc-brand` | Brand / call-to-action (azure) | Status or severity |
| `--sc-sea` | Nominal / secondary / live-ok | Alerts |
| `--sc-blue` | Informational | Severity |
| `--sc-amber` | Uncertainty / warning | Brand identity |
| `--sc-rose` | Critical / alert | Decorative use |

Applied in `src/styles.css` to `.mda-sev--*`, `.intel-sev--*`, `.osint-stat--*`
(previously hardcoded hex, now pointing at these tokens).

## Motion budget

Hero motion is calibrated down to match the sober register of the rest of the
site: `ShinyText` no longer loops continuously in the hero kicker (`static`
prop), `Threads` runs at reduced amplitude/count, and `Magnetic` is reserved
for the primary CTA only. Every other section already used `Reveal` /
`SpotlightCard` / `TiltCard` / `AnimatedNumber` sparingly and needs no change.

New motion is justified by content change (e.g. the live signal strip
updating with real data), never added as decoration on its own.

## Live header and current system view

`apps/web/src/site/HeaderLive.jsx` provides the compact public Live ticker in
the institutional header. It reads the privacy-filtered
`GET /api/v1/live/signals` contract and links to `live.seacommons.org`.
Empty and error states remain neutral rather than presenting an application
failure.

`apps/web/src/site/sections/OverallCounter.jsx` sits immediately below the
Hero and reads `GET /api/v1/status?hours=24` plus `GET /api/v1/play/counts`.
It presents canonical pipeline stages (observations, normalized events,
derived cues, episodes, Live cases and archive) separately from sensor
activity. The site must not maintain parallel metric semantics.

## Hero, header and Surfaces — second pass

- **Hero background** is a looping video (`public/media/seacommons-hero-loop.mp4`)
  instead of the WebGL `Threads` canvas. `object-position: center 78%` keeps the
  vessels near the bottom of frame in view after `cover` cropping; the
  `.hero__vignette` gradient darkens from the left (behind the text column) and
  fades out toward the right, rather than a flat bottom band, so the video's
  own content stays visible instead of being crushed under a uniform scrim.
- **Hero HUD card** (`SIM / TRACE 0041`) now carries a small inline SVG —
  triangle vessel markers along a dashed trajectory inside a widening
  uncertainty cone, the same visual grammar as `SystemView`'s `DriftFigure` —
  so the card's text (Origin/Window/Output) has a diagram to match, instead of
  numbers alone.
- **Main CTA** is "Open Live" → `live.seacommons.org` (previously "Enter the
  public demo" → Play). Live is the flagship surface the hero sends people to.
- **Environments cards** ("Surfaces / 001") dropped their long descriptive
  paragraphs for one-line taglines, matching the terser register already used
  by the Closing section's cards. ENGINE is explicitly labelled "Coming soon"
  in both the Environments card and its own section label.
- **Header Live ticker** is part of the translucent header and uses the same
  public Live projection as the Live surface. It replaces the earlier
  standalone signal-strip concept.

## Logo

The current brandmark (`BrandMark` in `chrome.jsx`) is a placeholder pair of
dots. Designing a real mark is out of scope for engineering work — it needs
human visual judgment — but the markup is structured so it can be swapped
without a surrounding refactor.
