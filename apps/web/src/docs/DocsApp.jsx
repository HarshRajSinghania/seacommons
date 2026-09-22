import React from 'react';

const GITHUB = 'https://github.com/suezcanalxyz/seacommons';
const API_DOCS = 'https://api.seacommons.org/docs';

const NAV = [
  ['Overview', '#overview'],
  ['Pipeline', '#pipeline'],
  ['Live', '#live'],
  ['Play', '#play'],
  ['Sources', '#sources'],
  ['Verification', '#verification'],
  ['Drift', '#drift'],
  ['Privacy', '#privacy'],
  ['API', '#api'],
  ['Limits', '#limits'],
  ['Developer docs', '#developer'],
];

const PIPELINE = [
  {
    stage: 'Observation',
    field: 'raw_observations',
    meaning: 'An immutable source envelope received by SeaCommons.',
    caution: 'A received observation is not a verified incident.',
  },
  {
    stage: 'Normalized event',
    field: 'parsed_events',
    meaning: 'A source record translated into the shared event vocabulary.',
    caution: 'Normalization standardizes structure, not truth.',
  },
  {
    stage: 'Derived cue',
    field: 'derived_cues',
    meaning: 'A bounded rule or model output such as AIS integrity, rendezvous or dark-gap context.',
    caution: 'A cue is evidence for investigation, not a factual finding.',
  },
  {
    stage: 'Episode',
    field: 'maritime_episodes',
    meaning: 'Related observations and features grouped around one maritime episode.',
    caution: 'Episodes preserve contributing evidence and lineage.',
  },
  {
    stage: 'Investigation hypothesis',
    field: 'investigation_hypotheses',
    meaning: 'An explicit interpretation that can gather supporting or conflicting evidence.',
    caution: 'Hypotheses remain contestable and can expire or be rejected.',
  },
  {
    stage: 'Public Live',
    field: 'live.total',
    meaning: 'The subset currently satisfying the public publication gate.',
    caution: 'Public Live is intentionally narrower than the underlying evidence store.',
  },
];

const SOURCE_GROUPS = [
  ['AIS', 'Position, static and navigation broadcasts, including coverage-aware gap and identity-integrity reasoning. Multiple AIS-derived detectors do not become independent sources simply because they use different algorithms.'],
  ['Human reports', 'Public humanitarian reports and partner or manually entered signals. Geometry can be exact, approximate or area-level and keeps its precision metadata.'],
  ['Radio', 'Structured DSC/NAVTEX and bounded radio monitoring. Receiver lineage is preserved so one transmission observed by multiple receivers is not automatically counted as multiple independent claims.'],
  ['Satellite', 'Satellite observations and acquisitions can contribute evidence or corroboration when their sensor lineage and resolution support the association.'],
  ['Environment', 'Wind, wave and ocean-current data support context and drift modelling. Environmental forcing is not itself evidence that an incident occurred.'],
];

const DEV_DOCS = [
  ['Architecture', 'docs/ARCHITECTURE.md'],
  ['Data flow', 'docs/DATA_FLOW.md'],
  ['Security model', 'docs/SECURITY_MODEL.md'],
  ['OSINT fusion', 'docs/OSINT_FUSION.md'],
  ['Configuration', 'docs/CONFIGURATION.md'],
  ['Testing strategy', 'docs/TESTING.md'],
  ['Operations overview', 'docs/OPERATIONS_OVERVIEW.md'],
  ['Documentation index', 'docs/README.md'],
];

function Brand() {
  return (
    <a className="docs-brand" href="/" aria-label="SeaCommons home">
      <span className="docs-brand__mark" aria-hidden="true"><i /><i /></span>
      <span>SEA<br />COMMONS</span>
    </a>
  );
}

function Section({ id, eyebrow, title, children }) {
  return (
    <section className="docs-section" id={id}>
      <p className="docs-eyebrow">{eyebrow}</p>
      <h2>{title}</h2>
      {children}
    </section>
  );
}

export default function DocsApp() {
  return (
    <div className="docs-shell">
      <header className="docs-header">
        <Brand />
        <div className="docs-header__links">
          <a href="https://live.seacommons.org">Live ↗</a>
          <a href="https://play.seacommons.org">Play ↗</a>
          <a href={API_DOCS}>API ↗</a>
          <a href={GITHUB}>GitHub ↗</a>
        </div>
      </header>

      <aside className="docs-sidebar" aria-label="Documentation navigation">
        <div>
          <span className="docs-sidebar__label">Documentation</span>
          <strong>Canonical public reference</strong>
          <small>Reviewed 22 September 2026</small>
        </div>
        <nav>
          {NAV.map(([label, href]) => <a key={href} href={href}>{label}</a>)}
        </nav>
      </aside>

      <main className="docs-main" id="docs-main">
        <section className="docs-hero" id="overview">
          <p className="docs-eyebrow">SeaCommons / Documentation</p>
          <h1>Signals are evidence.<br /><em>Evidence is not certainty.</em></h1>
          <p className="docs-lead">
            SeaCommons is open maritime research infrastructure for receiving fragmented signals,
            preserving provenance, deriving bounded analytical cues, grouping related evidence and
            publishing only what passes explicit public-safety and evidentiary gates.
          </p>
          <div className="docs-callouts">
            <div><span>Primary surfaces</span><strong>Live · Play · API</strong></div>
            <div><span>Public categories</span><strong>Humanitarian · Maritime</strong></div>
            <div><span>Repository</span><strong>AGPL-3.0 · open source</strong></div>
          </div>
          <div className="docs-note">
            <strong>Source of truth.</strong>
            <p>This page is the canonical public documentation for SeaCommons. Engineering runbooks,
            historical audits and implementation plans remain versioned in the repository and are
            linked below when they are the appropriate level of detail.</p>
          </div>
        </section>

        <Section id="pipeline" eyebrow="01 / Data model" title="From source records to public cases">
          <p>
            The pipeline deliberately separates what was received from what was inferred. Counts at
            different stages describe different objects and should not be read as equivalent incident totals.
          </p>
          <div className="docs-pipeline">
            {PIPELINE.map((item, index) => (
              <article key={item.stage}>
                <span className="docs-pipeline__index">{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <h3>{item.stage}</h3>
                  <code>{item.field}</code>
                  <p>{item.meaning}</p>
                  <small>{item.caution}</small>
                </div>
              </article>
            ))}
          </div>
          <p className="docs-caption">
            The homepage “Current system view” reads the same public aggregate status contract at
            <code> /api/v1/status?hours=24</code>. It does not maintain a parallel analytics pipeline.
          </p>
        </Section>

        <Section id="live" eyebrow="02 / Public Live" title="A case-first view, not a raw sensor dump">
          <p>
            Live exposes privacy-filtered cases and signals that satisfy the current publication policy.
            Raw AIS and private operational material are not made public simply because they exist in the backend.
          </p>
          <div className="docs-two-col">
            <article>
              <h3>Humanitarian</h3>
              <p>Distress, rescue and safety-context material, including public humanitarian reporting and maritime safety signals.</p>
            </article>
            <article>
              <h3>Maritime</h3>
              <p>Maritime-domain investigations such as dark activity, rendezvous/transfer context, identity integrity, sanctions context and infrastructure proximity where the evidentiary gate is satisfied.</p>
            </article>
          </div>
          <div className="docs-note">
            <strong>Colour and lifecycle are different dimensions.</strong>
            <p>Incident category determines the primary colour. Lifecycle states such as active, needs review,
            resolved or archived use secondary styling and must not silently change what kind of incident is being shown.</p>
          </div>
        </Section>

        <Section id="play" eyebrow="03 / Play" title="Persistent public case records and reconstruction">
          <p>
            Play is the public archive and reconstruction surface. It reads persisted public case records and
            timelines rather than treating every incoming observation as a separate case. A case can accumulate
            additional public evidence over time while preserving the history of what was known at each stage.
          </p>
          <p>
            Drift and reconstruction outputs remain explicitly modelled or derived. They are displayed alongside
            reported evidence, not substituted for it.
          </p>
        </Section>

        <Section id="sources" eyebrow="04 / Sources" title="Different sensors, different clocks, different failure modes">
          <div className="docs-source-grid">
            {SOURCE_GROUPS.map(([title, body]) => (
              <article key={title}><h3>{title}</h3><p>{body}</p></article>
            ))}
          </div>
          <p>
            Source independence is determined by lineage, not by display name. Re-processing one underlying
            publication through several detectors does not manufacture corroboration.
          </p>
        </Section>

        <Section id="verification" eyebrow="05 / Evidence" title="Verification, corroboration and contestability">
          <p>
            SeaCommons distinguishes verification of an individual evidence item from corroboration of a wider
            claim. Multi-source corroboration requires independent evidence lineages. Repeated AIS-derived
            signals, duplicate publications or transformations of the same source do not count as independent support.
          </p>
          <div className="docs-three-col">
            <article><span>Single source</span><h3>Observed</h3><p>One lineage supports the observation or episode.</p></article>
            <article><span>Single source</span><h3>Multi-indicator</h3><p>Several indicators may agree but still share one underlying lineage.</p></article>
            <article><span>Independent sources</span><h3>Corroborated</h3><p>At least two independent evidence lineages support the episode.</p></article>
          </div>
          <div className="docs-note">
            <strong>No automated finding of illegality.</strong>
            <p>Sanctions, spoofing, dark activity or rendezvous context can justify investigation. They do not by
            themselves establish intent, illegality or responsibility.</p>
          </div>
        </Section>

        <Section id="drift" eyebrow="06 / Drift" title="Modelled trajectories remain modelled">
          <p>
            SeaCommons uses environmental forcing and versioned simulation parameters to produce drift trajectories
            and uncertainty surfaces. Model coordinates are derived candidates, not reported geometry.
          </p>
          <p>
            Every useful reconstruction should preserve the origin point, time window, environmental inputs,
            model parameters and output lineage so another analyst can understand what changed and reproduce the run.
          </p>
        </Section>

        <Section id="privacy" eyebrow="07 / Governance" title="Public by policy, not by accident">
          <p>
            User-originated signals are private by default. Only the canonical public projection may cross into
            Public Live. Missing coordinates remain missing, approximate geometry remains approximate and sensitive
            source details are not exposed merely to make a map look more complete.
          </p>
          <p>
            Public status endpoints expose aggregate counts and freshness timestamps, not raw private payloads,
            receiver endpoints, operator information or hidden vessel identities.
          </p>
        </Section>

        <Section id="api" eyebrow="08 / Interfaces" title="Public interfaces">
          <div className="docs-api-list">
            <a href="/api/v1/status?hours=24"><code>GET /api/v1/status</code><span>Aggregate pipeline counts, Live split, sensor activity and freshness.</span></a>
            <a href="/api/v1/live/signals?limit=30&days=2"><code>GET /api/v1/live/signals</code><span>Privacy-filtered public Live signal collection.</span></a>
            <a href={API_DOCS}><code>api.seacommons.org/docs</code><span>OpenAPI / Swagger reference for the current API deployment. ↗</span></a>
            <a href="https://api.seacommons.org/redoc"><code>api.seacommons.org/redoc</code><span>ReDoc API reference. ↗</span></a>
          </div>
        </Section>

        <Section id="limits" eyebrow="09 / Limits" title="What the system does not claim">
          <ul className="docs-limits">
            <li>Public Live is not an exhaustive map of everything occurring at sea.</li>
            <li>Coverage varies by source, geography, provider availability, latency and licensing constraints.</li>
            <li>An AIS gap can be caused by coverage, reception, equipment state or deliberate behaviour; classification requires context.</li>
            <li>Derived cues are investigation aids, not factual conclusions.</li>
            <li>Approximate and area-level geometry should not be read as exact coordinates.</li>
            <li>Historical records can be incomplete when a source was unavailable or had not yet been integrated.</li>
          </ul>
        </Section>

        <Section id="developer" eyebrow="10 / Repository" title="Developer and operational documentation">
          <p>
            The public page stays concise. Canonical engineering documents, runbooks and machine-readable contracts
            remain versioned next to the code so architecture and implementation can change in the same review.
          </p>
          <div className="docs-dev-grid">
            {DEV_DOCS.map(([label, path]) => (
              <a key={path} href={`${GITHUB}/blob/main/${path}`}><strong>{label}</strong><span>{path} ↗</span></a>
            ))}
          </div>
          <div className="docs-note">
            <strong>Historical audits are preserved for traceability.</strong>
            <p>If a dated audit conflicts with a canonical document or executable test, the canonical document and
            current implementation take precedence.</p>
          </div>
        </Section>

        <footer className="docs-footer">
          <Brand />
          <p>Open research infrastructure for accountable maritime evidence.</p>
          <div><a href="/">Home</a><a href="https://live.seacommons.org">Live</a><a href="https://play.seacommons.org">Play</a><a href={GITHUB}>GitHub</a></div>
        </footer>
      </main>
    </div>
  );
}
