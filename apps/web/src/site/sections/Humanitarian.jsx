import React from 'react';
import { Reveal } from '../../ui/index.js';
import { SectionLabel, Display } from '../bits.jsx';

const FLOW = [
  ['01', 'Report', 'Keep the original source, publication time, wording and any stated position. If the report only identifies an area, SeaCommons keeps an area; it does not invent a point.'],
  ['02', 'Position', 'Record whether a location was reported directly, extracted from text or media, estimated, or still unknown. An extracted coordinate is not automatically a verified coordinate.'],
  ['03', 'Cross-check', 'Compare later reports, civil SAR updates, vessel activity and other public evidence. Repeated copies of the same original report remain one source lineage.'],
  ['04', 'Outcome', 'Add rescue, disembarkation, fatality or other outcome claims when a source actually supports them. AIS activity by itself cannot prove that a rescue happened.'],
];

const SOURCES = [
  'Alarm Phone and other operational-origin public reporting',
  'Civil SAR organisations and verification groups',
  'AIS tracks for vessel and response context',
  'Public news, RSS and institutional reporting',
  'Weather and ocean data when needed for reconstruction',
];

export default function Humanitarian() {
  return (
    <section id="humanitarian-detail" className="section humanitarian">
      <SectionLabel index="Humanitarian / 003" title="How a humanitarian case is built" tone="light" />
      <div className="humanitarian__head">
        <Display id="humanitarian-title">
          A distress report can be urgent<br />and still be incomplete.
        </Display>
        <Reveal delay={120}>
          <p>
            Reports can arrive late, use approximate positions, repeat earlier claims or disagree
            about the outcome. SeaCommons keeps those differences in the record instead of resolving
            them with a single confidence score.
          </p>
        </Reveal>
      </div>

      <Reveal as="ol" className="humanitarian__flow" stagger={80}>
        {FLOW.map(([n, title, body]) => (
          <li key={n}>
            <span>{n}</span>
            <h3>{title}</h3>
            <p>{body}</p>
          </li>
        ))}
      </Reveal>

      <div className="humanitarian__sources">
        <Reveal>
          <div>
            <span>Evidence used</span>
            <h3>The source matters as much as the claim.</h3>
          </div>
        </Reveal>
        <Reveal as="ul" stagger={70}>
          {SOURCES.map((source) => <li key={source}>{source}</li>)}
        </Reveal>
      </div>

      <Reveal className="humanitarian__rule">
        <strong>Publication rule</strong>
        <p>
          Humanitarian privacy comes before map precision. A private or sensitive position does not
          become public because a model can estimate it. When the evidence supports only a region,
          the public record should show a region or no geometry at all.
        </p>
      </Reveal>
    </section>
  );
}
