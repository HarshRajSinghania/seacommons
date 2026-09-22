import React, { useEffect, useRef } from 'react';
import { SplitText, Magnetic, ShinyText, Reveal } from '../../ui/index.js';
import { useReducedMotion } from '../../ui/motion.js';

export default function Hero() {
  const reducedMotion = useReducedMotion();
  const videoRef = useRef(null);

  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    if (reducedMotion) el.pause();
    else el.play().catch(() => {});
  }, [reducedMotion]);

  return (
    <section id="top" className="hero" aria-labelledby="hero-title">
      <div className="hero__bg" aria-hidden="true">
        <video
          ref={videoRef}
          className="hero__video"
          src="/media/seacommons-hero-loop.mp4"
          autoPlay={!reducedMotion}
          muted
          loop
          playsInline
          preload="auto"
        />
        <div className="hero__grid" />
        <div className="hero__grain" />
        <div className="hero__vignette" />
      </div>

      <div className="hero__copy">
        <p className="kicker"><ShinyText static>Observe · normalize · correlate · preserve</ShinyText></p>
        <h1 id="hero-title" className="hero__title">
          <SplitText text="From fragmented maritime signals to" as="span" />
          <br />
          <em><SplitText text="traceable public cases." as="span" delay={520} /></em>
        </h1>
        <Reveal className="hero__intro" delay={200}>
          <p>
            SeaCommons receives heterogeneous observations, preserves provenance, derives bounded
            analytical cues and groups related evidence without converting uncertainty into certainty.
          </p>
          <div className="hero__actions">
            <Magnetic>
              <a className="btn btn--primary" href="https://live.seacommons.org">
                Open Live <span aria-hidden="true">↗</span>
              </a>
            </Magnetic>
            <a className="btn btn--ghost" href="/docs">
              Read the docs <span aria-hidden="true">→</span>
            </a>
          </div>
        </Reveal>
      </div>

      <a className="hero__scroll" href="#environments" aria-label="Scroll to environments">
        <span />
      </a>
    </section>
  );
}
