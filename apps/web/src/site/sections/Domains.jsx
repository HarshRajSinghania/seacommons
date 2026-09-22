import React from 'react';
import { Reveal, SpotlightCard } from '../../ui/index.js';
import { SectionLabel, Display } from '../bits.jsx';

const HUMANITARIAN = [
  'Distress and rescue reports',
  'Missing persons and shipwrecks',
  'Pushbacks and migration incidents',
  'SAR activity and rescue outcomes',
  'Civil SAR and NGO verification',
];

const MARITIME = [
  'AIS gaps and dark-activity candidates',
  'Position and identity integrity',
  'Transfers, rendezvous and loitering',
  'Navigation safety and port-call context',
  'Infrastructure, sanctions and security context',
];

const SURFACES = [
  {
    label: 'Live',
    href: 'https://live.seacommons.org',
    meta: 'What is public now',
    body: 'Cases that currently pass the public publication rules. Live is case-first: it is not a raw AIS map and it does not expose every observation in the backend.',
  },
  {
    label: 'Play',
    href: 'https://play.seacommons.org',
    meta: 'What remains on record',
    body: 'The public archive. A case keeps its timeline, evidence and later updates instead of disappearing when the immediate Live window ends.',
  },
  {
    label: 'Docs',
    href: '/docs',
    meta: 'How the system works',
    body: 'The technical reference for source lineage, correlation, public/private boundaries, API contracts, modelling, tests and known limitations.',
  },
];

export default function Domains() {
  return (
    <section className="section domains" aria-labelledby="domains-title">
      <SectionLabel index="Scope / 001" title="What SeaCommons follows" />
      <div className="domains__head">
        <Display id="domains-title">
          Two kinds of cases.<br />Different risks, different rules.
        </Display>
        <Reveal delay={120}>
          <p>
            Humanitarian cases concern people in distress, rescue activity and what happened next.
            Maritime cases concern vessel behaviour, identity, navigation and other activity worth
            examining. They share infrastructure, but they do not share the same publication rules.
          </p>
        </Reveal>
      </div>

      <Reveal className="domains__grid" stagger={100}>
        <article id="humanitarian" className="domain-card domain-card--humanitarian">
          <div className="domain-card__top">
            <span>Humanitarian</span>
            <small>People · distress · rescue</small>
          </div>
          <h3>Where are people reported to be at risk, and what can we establish about the response?</h3>
          <p>
            A humanitarian case can begin with a call, a public report or a rescue organisation's
            update. Coordinates may be exact, approximate, area-level or absent. SeaCommons keeps
            that precision visible and restricts sensitive material before anything becomes public.
          </p>
          <ul>
            {HUMANITARIAN.map((item) => <li key={item}>{item}</li>)}
          </ul>
          <a href="#humanitarian-detail">Humanitarian method ↓</a>
        </article>

        <article id="maritime" className="domain-card domain-card--maritime">
          <div className="domain-card__top">
            <span>Maritime</span>
            <small>Vessels · behaviour · context</small>
          </div>
          <h3>What is unusual in a vessel's track, identity or operating context, and what else explains it?</h3>
          <p>
            A gap in AIS, a close approach between vessels or a sanctions match is not a conclusion.
            SeaCommons treats these as reasons to look closer, checks coverage and source lineage,
            and keeps ordinary explanations visible alongside the suspicious ones.
          </p>
          <ul>
            {MARITIME.map((item) => <li key={item}>{item}</li>)}
          </ul>
          <a href="#maritime-detail">Maritime method ↓</a>
        </article>
      </Reveal>

      <div className="domains__surfaces">
        <SectionLabel index="Access / 002" title="Live, archive and technical reference" />
        <Reveal className="domains__surface-grid" stagger={90}>
          {SURFACES.map((surface) => (
            <SpotlightCard as="a" className="surface-card" href={surface.href} key={surface.label}>
              <span>{surface.meta}</span>
              <strong>{surface.label}</strong>
              <p>{surface.body}</p>
              <i aria-hidden="true">↗</i>
            </SpotlightCard>
          ))}
        </Reveal>
      </div>
    </section>
  );
}
