import React, { useEffect, useState } from 'react';
import { Header, Footer } from './chrome.jsx';
import Hero from './sections/Hero.jsx';
import OverallCounter from './sections/OverallCounter.jsx';
import Environments from './sections/Environments.jsx';
import Programme from './sections/Programme.jsx';
import SystemView from './sections/SystemView.jsx';
import Engine from './sections/Engine.jsx';
import MDA from './sections/MDA.jsx';
import Governance from './sections/Governance.jsx';
import Closing from './sections/Closing.jsx';

/** Progress bar bound to scroll — a small premium cue, transform-only. */
function ScrollProgress() {
  const [p, setP] = useState(0);
  useEffect(() => {
    const onScroll = () => {
      const h = document.documentElement.scrollHeight - window.innerHeight;
      setP(h > 0 ? window.scrollY / h : 0);
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    return () => {
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
    };
  }, []);
  return (
    <div className="scroll-progress" aria-hidden="true">
      <i style={{ transform: `scaleX(${p})` }} />
    </div>
  );
}

export default function SiteApp() {
  return (
    <>
      <ScrollProgress />
      <Header />
      <main id="main">
        <Hero />
        <OverallCounter />
        <Environments />
        <Programme />
        <SystemView />
        <Engine />
        <MDA />
        <Governance />
        <Closing />
      </main>
      <Footer />
    </>
  );
}
