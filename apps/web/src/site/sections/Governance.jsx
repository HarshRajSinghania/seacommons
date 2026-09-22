import React from 'react';
import { Reveal } from '../../ui/index.js';
import { SectionLabel, Display } from '../bits.jsx';

const TIERS = [
  { code: 'PUBLIC / P0', title: 'Public methods and case records', body: 'Documentation, public case projections, aggregated system status and material that can be released without exposing private operational data.', tone: 'lime' },
  { code: 'RESEARCH / R1', title: 'Controlled analytical material', body: 'Purpose-bound datasets and working material with documented access, minimisation, retention and review conditions.', tone: 'sea' },
  { code: 'OPERATIONAL / O2', title: 'Sensitive operational material', body: 'Identifying reports, precise sensitive positions, contact details, private attachments and analyst state. This does not belong in the public projection.', tone: 'amber' },
];

const PRINCIPLES = ['Minimise what is stored', 'Keep purpose explicit', 'Review sensitive publication', 'Allow correction', 'Assess dual-use risk', 'Delete when retention ends'];

export default function Governance() {
  return (
    <section id="governance" className="section governance">
      <SectionLabel index="Governance / 008" title="Public does not mean everything" />
      <div className="governance__grid">
        <Display id="governance-title">
          SeaCommons publishes a reduced record.<br />The sensitive material stays behind it.
        </Display>
        <Reveal className="tiers" stagger={90}>
          {TIERS.map((t) => (
            <article className={`tier tier--${t.tone}`} key={t.code}>
              <span>{t.code}</span>
              <h3>{t.title}</h3>
              <p>{t.body}</p>
            </article>
          ))}
        </Reveal>
      </div>
      <Reveal className="principles" stagger={60}>
        {PRINCIPLES.map((p) => <p key={p}>{p}</p>)}
      </Reveal>

      <div className="limits">
        <Reveal className="limits__heading" y={12}>
          <span>Operational limit</span>
          <h2>SeaCommons can be incomplete, delayed or wrong.</h2>
        </Reveal>
        <Reveal className="limits__copy" delay={120}>
          <p>
            Coverage changes by provider, geography, licensing and sensor availability. AIS gaps
            are ambiguous without reception context. Satellite acquisitions can miss the relevant
            time. Human reports can be approximate or late. A modelled position remains modelled.
          </p>
          <p>
            SeaCommons is research infrastructure, not an emergency dispatch service. It must not
            replace rescue coordination, emergency communications or qualified operational judgment.
          </p>
        </Reveal>
      </div>
    </section>
  );
}
