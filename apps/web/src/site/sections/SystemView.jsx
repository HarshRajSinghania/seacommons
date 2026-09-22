import React from 'react';
import { Reveal } from '../../ui/index.js';
import { SectionLabel, Display } from '../bits.jsx';

const STEPS = [
  { n: '01', tag: 'OBS', title: 'Observation', body: 'An immutable source envelope: who or what supplied it, when SeaCommons received it, and what the source actually contained.', foot: 'No verification implied' },
  { n: '02', tag: 'EVENT', title: 'Normalised event', body: 'The source is translated into a shared vocabulary while its original authority, precision and lineage stay attached.', foot: 'Comparable, not equal' },
  { n: '03', tag: 'CUE', title: 'Derived cue', body: 'A rule or model identifies something worth checking: a gap, a rendezvous, a position problem, a radio association or another bounded signal.', foot: 'Still not a finding' },
  { n: '04', tag: 'EP', title: 'Episode or hypothesis', body: 'Related material is grouped only when identity, time, place and source lineage support the association. Weak associations can expire.', foot: 'Contestable analysis' },
  { n: '05', tag: 'LIVE', title: 'Public case', body: 'A reduced version crosses the public boundary only after privacy, publication policy, lifecycle and geometry precision checks.', foot: 'Public projection' },
  { n: '06', tag: 'PLAY', title: 'Archive record', body: 'The case remains in Play with its timeline, so later evidence can add to the record without pretending it was known earlier.', foot: 'History is preserved' },
];

const METHOD = [
  { n: '01', title: 'Provenance', body: 'The record says where a claim came from and which later outputs depend on it.' },
  { n: '02', title: 'Independence', body: 'Copies, transports and reprocessing do not manufacture independent corroboration.' },
  { n: '03', title: 'Precision', body: 'An exact point, an approximate point, an area and an unknown location remain different states.' },
  { n: '04', title: 'Contradiction', body: 'Later evidence can support, weaken or contradict a case without deleting the earlier state.' },
];

export default function SystemView() {
  return (
    <section id="system" className="section systemview">
      <SectionLabel index="Architecture / 007" title="The evidence model" tone="dark" />
      <div className="systemview__head">
        <Display id="system-title">
          Six stages.<br />Each answers a different question.
        </Display>
        <Reveal delay={120}>
          <p>
            PostgreSQL is the durable system of record. Live and Play read public projections of
            that record; they do not run separate investigative logic. Edge and browser snapshots
            can keep the interface available, but they are caches, not a second truth.
          </p>
        </Reveal>
      </div>

      <Reveal as="ol" className="pipeline pipeline--six" stagger={70}>
        {STEPS.map((s) => (
          <li key={s.n}>
            <div><span>{s.n}</span><i>{s.tag}</i></div>
            <h3>{s.title}</h3>
            <p>{s.body}</p>
            <small>{s.foot}</small>
          </li>
        ))}
      </Reveal>

      <div id="method" className="method">
        <SectionLabel index="Method / 007A" title="Four distinctions the interface should never hide" />
        <div className="method__head">
          <Display id="method-title">
            The useful part is not the marker.<br />It is the chain behind it.
          </Display>
          <Reveal delay={120}>
            <p>
              SeaCommons is designed so a reader can move backwards from a public case to the
              observations, transformations and uncertainties that produced it. If that chain is
              missing, the result should not be presented as well established.
            </p>
          </Reveal>
        </div>
        <Reveal className="method__grid" stagger={90}>
          {METHOD.map((m) => (
            <article key={m.n}>
              <span>{m.n}</span>
              <h3>{m.title}</h3>
              <p>{m.body}</p>
            </article>
          ))}
        </Reveal>
      </div>
    </section>
  );
}
