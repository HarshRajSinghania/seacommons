import React from 'react';
import { Reveal, SpotlightCard } from '../../ui/index.js';

const CARDS = [
  {
    cls: 'closing-card--live',
    href: 'https://live.seacommons.org',
    meta: 'Current public cases',
    title: 'Live',
    body: 'Open the current Humanitarian and Maritime case view.',
  },
  {
    cls: 'closing-card--play',
    href: 'https://play.seacommons.org',
    meta: 'Public archive',
    title: 'Play',
    body: 'Browse earlier cases, their timelines and the evidence that became public later.',
  },
  {
    cls: 'closing-card--docs',
    href: '/docs',
    meta: 'Technical reference',
    title: 'Docs',
    body: 'Read the architecture, source rules, API contracts, modelling methods, tests and known limits.',
  },
];

export default function Closing() {
  return (
    <section className="section closing">
      <Reveal className="closing__intro" y={14}>
        <span>Continue</span>
        <h2>Use the level of detail you need.</h2>
      </Reveal>
      <Reveal className="closing__cards" stagger={110}>
        {CARDS.map((card) => (
          <SpotlightCard as="a" className={`closing-card ${card.cls}`} href={card.href} key={card.title}>
            <span>{card.meta}</span>
            <strong>{card.title}</strong>
            <p>{card.body}</p>
            <i aria-hidden="true">↗</i>
          </SpotlightCard>
        ))}
      </Reveal>
    </section>
  );
}
