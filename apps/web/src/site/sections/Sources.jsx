import React from 'react';
import { Reveal } from '../../ui/index.js';
import { SectionLabel, Display } from '../bits.jsx';

const SOURCES = [
  {
    name: 'Public reports',
    examples: 'Operational reports · NGO updates · institutional and news sources',
    use: 'Start or update a case, preserve claims, times, stated positions and reported outcomes.',
    limit: 'A repeated report is not independent corroboration, and a reported coordinate is not automatically a verified position.',
  },
  {
    name: 'AIS',
    examples: 'Vessel identity · position · course · speed · reception context',
    use: 'Reconstruct vessel movement, compare response activity, detect gaps and test spatial relationships.',
    limit: 'Missing AIS positions do not prove that a vessel was absent or deliberately silent. Reception, provider coverage and equipment state can all create gaps.',
  },
  {
    name: 'Maritime radio',
    examples: 'DSC · NAVTEX · receiver observations',
    use: 'Add time-bounded radio evidence and associate it with other maritime observations when identity and timing permit.',
    limit: 'Repeated reception of one transmission does not create several independent sources.',
  },
  {
    name: 'Satellite',
    examples: 'Acquisition records · imagery · footprint and timing metadata',
    use: 'Add an independent view when acquisition time, geometry and resolution are relevant to a specific case.',
    limit: 'A satellite pass can be too early, too late, too coarse or cloud-obscured. Availability alone is not evidence of the event.',
  },
  {
    name: 'Environment',
    examples: 'Wind · waves · currents · weather',
    use: 'Provide context and, when the origin is known well enough, drive a versioned reconstruction or drift model.',
    limit: 'Model output is derived. It must never replace the reported or observed position that the model started from.',
  },
];

export default function Sources() {
  return (
    <section id="sources" className="section sources">
      <SectionLabel index="Sources / 005" title="What enters the system" />
      <div className="sources__head">
        <Display id="sources-title">
          Different sources answer<br />different questions.
        </Display>
        <Reveal delay={120}>
          <p>
            SeaCommons does not turn every input into one generic confidence score. Each source
            keeps its own evidentiary role and its own failure modes. The point is to know what a
            source can support before using it to strengthen a case.
          </p>
        </Reveal>
      </div>

      <Reveal className="sources__grid" stagger={70}>
        {SOURCES.map((source) => (
          <article className="source-card" key={source.name}>
            <header>
              <h3>{source.name}</h3>
              <span>{source.examples}</span>
            </header>
            <div>
              <small>Used for</small>
              <p>{source.use}</p>
            </div>
            <div>
              <small>Does not prove</small>
              <p>{source.limit}</p>
            </div>
          </article>
        ))}
      </Reveal>
    </section>
  );
}
