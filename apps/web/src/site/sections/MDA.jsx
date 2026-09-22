import React from 'react';
import { Reveal, TiltCard } from '../../ui/index.js';
import { SectionLabel, Display } from '../bits.jsx';

const CAPABILITIES = [
  {
    n: 'MAR / 01',
    tag: 'AIS',
    title: 'Gaps in transmission',
    body: 'A long AIS gap may be interesting, but it can also come from weak reception, provider loss or equipment state. SeaCommons checks the surrounding coverage before treating the gap as a case.',
    points: ['Track before and after', 'Nearby reception context', 'Provider health'],
    code: 'GAP ≠ INTENT',
    tone: 'blue',
  },
  {
    n: 'MAR / 02',
    tag: 'Identity',
    title: 'Position and identity problems',
    body: 'Frozen positions, impossible jumps and inconsistent identifiers are recorded as integrity problems. They are questions about the track, not automatic claims about the vessel.',
    points: ['Track consistency', 'Identifier history', 'Alternative explanations'],
    code: 'TRACK ?',
    tone: 'lime',
  },
  {
    n: 'MAR / 03',
    tag: 'Behaviour',
    title: 'Rendezvous and loitering',
    body: 'Close approaches, prolonged co-location and unusual stationary behaviour are compared in time and space. A transfer is only described as such when the available evidence supports that wording.',
    points: ['Time overlap', 'Distance and speed', 'Independent evidence'],
    code: 'VESSEL × VESSEL',
    tone: 'paper',
  },
  {
    n: 'MAR / 04',
    tag: 'Context',
    title: 'What surrounds the event',
    body: 'Ports, offshore infrastructure, sanctions references, navigation status, radio and satellite observations can change how a track should be read. Context is stored as context, not as guilt by association.',
    points: ['Ports and infrastructure', 'Sanctions references', 'Radio and satellite'],
    code: 'CASE + CONTEXT',
    tone: 'amber',
  },
];

export default function MDA() {
  return (
    <section id="maritime-detail" className="section programme maritime-detail">
      <SectionLabel index="Maritime / 004" title="How a maritime investigation starts" tone="light" />
      <div className="maritime-detail__head">
        <Display id="maritime-title">
          An anomaly is a reason to look.<br />It is not the answer.
        </Display>
        <Reveal delay={120}>
          <p>
            SeaCommons looks for patterns in vessel movement and identity, then asks whether the
            pattern survives basic checks: was there coverage, is the identity stable, is the timing
            compatible, and is there any independent evidence? Cases that fail those checks should
            stay weak, expire or never be published.
          </p>
        </Reveal>
      </div>

      <Reveal className="wp-grid" stagger={90}>
        {CAPABILITIES.map((c) => (
          <TiltCard className={`wp-card wp-card--${c.tone}`} key={c.n}>
            <div className="wp-card__top">
              <span>{c.n}</span>
              <span>{c.tag}</span>
            </div>
            <h3>{c.title}</h3>
            <p>{c.body}</p>
            <ul>
              {c.points.map((p) => <li key={p}>{p}</li>)}
            </ul>
            <span className="wp-card__code" aria-hidden="true">{c.code}</span>
          </TiltCard>
        ))}
      </Reveal>

      <Reveal className="maritime-detail__rule">
        <strong>Corroboration rule</strong>
        <p>
          Two providers do not necessarily mean two sources. If both are carrying the same AIS
          broadcast, SeaCommons still has one AIS lineage. Independent corroboration has to come
          from evidence that is independent of that broadcast.
        </p>
      </Reveal>
    </section>
  );
}
