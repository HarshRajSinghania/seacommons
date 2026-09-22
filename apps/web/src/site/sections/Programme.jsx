import React from 'react';
import { Reveal, TiltCard } from '../../ui/index.js';
import { SectionLabel, Display } from '../bits.jsx';

const WP = [
  {
    n: '01',
    tag: 'Keep',
    title: 'Store what arrived',
    body: 'The first record keeps the source, transport, timestamps, payload reference and location precision before interpretation begins.',
    points: ['Original source', 'Received time', 'Precision and privacy'],
    code: 'SOURCE → OBS',
    tone: 'blue',
  },
  {
    n: '02',
    tag: 'Translate',
    title: 'Put different sources in one vocabulary',
    body: 'Provider-specific fields are normalised so they can be compared. The normalisation does not erase who said what or how precise the original information was.',
    points: ['Canonical event types', 'Source lineage kept', 'Unknown stays unknown'],
    code: 'OBS → EVENT',
    tone: 'lime',
  },
  {
    n: '03',
    tag: 'Test',
    title: 'Derive cues without promoting them to facts',
    body: 'Rules and models can flag a gap, a rendezvous, an identity problem or response context. These outputs remain derived cues until other evidence supports a stronger claim.',
    points: ['Rules and models', 'Counter-indicators', 'Expiry when support disappears'],
    code: 'EVENT → CUE',
    tone: 'paper',
  },
  {
    n: '04',
    tag: 'Publish',
    title: 'Show only what survives the public rules',
    body: 'Cases cross into Live only after privacy, lifecycle, source policy and location precision are checked. Play keeps the public history after the immediate Live window.',
    points: ['Fail closed', 'Versioned lifecycle', 'Persistent archive'],
    code: 'CASE → PUBLIC',
    tone: 'amber',
  },
];

export default function Programme() {
  return (
    <section id="research" className="section programme">
      <SectionLabel index="Pipeline / 006" title="What happens after a source arrives" tone="light" />
      <div className="programme__head">
        <Display id="research-title">
          The system keeps the steps separate<br />because they mean different things.
        </Display>
        <Reveal delay={120}>
          <p>
            A source observation, a normalised event, a derived cue and a public case are not four
            names for the same object. SeaCommons keeps the boundaries between them so later readers
            can see what was received, what software added and what was finally published.
          </p>
        </Reveal>
      </div>

      <Reveal className="wp-grid" stagger={90}>
        {WP.map((w) => (
          <TiltCard className={`wp-card wp-card--${w.tone}`} key={w.n}>
            <div className="wp-card__top">
              <span>{w.n}</span>
              <span>{w.tag}</span>
            </div>
            <h3>{w.title}</h3>
            <p>{w.body}</p>
            <ul>{w.points.map((p) => <li key={p}>{p}</li>)}</ul>
            <span className="wp-card__code" aria-hidden="true">{w.code}</span>
          </TiltCard>
        ))}
      </Reveal>
    </section>
  );
}
