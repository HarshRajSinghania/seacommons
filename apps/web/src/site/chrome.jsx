import React, { useEffect, useRef, useState } from 'react';
import HeaderLive from './HeaderLive.jsx';

function useClocks() {
  const [clock, setClock] = useState({ utc: '--:--:--', local: '--:--:--' });
  useEffect(() => {
    const tick = () => {
      const now = new Date();
      setClock({
        utc: now.toISOString().slice(11, 19),
        local: now.toLocaleTimeString('en-GB', { hour12: false }),
      });
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, []);
  return clock;
}

const NAV = [
  ['Environments', '#environments'],
  ['Research', '#research'],
  ['System', '#system'],
  ['Method', '#method'],
  ['MDA', '#mda'],
  ['Governance', '#governance'],
  ['Docs', '/docs'],
];

export function BrandMark({ small = false }) {
  return (
    <span className={`brandmark ${small ? 'is-small' : ''}`.trim()} aria-hidden="true">
      <i />
      <i />
    </span>
  );
}

export function Header() {
  const [open, setOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const headerRef = useRef(null);
  const { utc, local } = useClocks();

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  useEffect(() => {
    document.body.style.overflow = open ? 'hidden' : '';
    return () => {
      document.body.style.overflow = '';
    };
  }, [open]);

  return (
    <header ref={headerRef} className={`site-header ${scrolled ? 'is-scrolled' : ''}`.trim()}>
      <a className="brand" href="#top" aria-label="SeaCommons — home">
        <BrandMark />
        <span>SEA<br />COMMONS</span>
      </a>
      <span className="site-header__clock mono">UTC {utc} · LOCAL {local}</span>
      <HeaderLive />

      <button
        type="button"
        className="site-header__toggle"
        aria-expanded={open}
        aria-controls="site-nav"
        onClick={() => setOpen((v) => !v)}
      >
        <span>{open ? 'Close' : 'Menu'}</span>
        <i />
      </button>

      <nav id="site-nav" className={`site-nav ${open ? 'is-open' : ''}`.trim()} aria-label="Primary">
        {NAV.map(([label, href]) => (
          <a key={href} href={href} onClick={() => setOpen(false)}>
            {label}
          </a>
        ))}
      </nav>
    </header>
  );
}

export function Footer() {
  return (
    <footer className="site-footer">
      <div className="site-footer__brand">
        <a className="brand" href="#top">
          <BrandMark small />
          <span>SEA<br />COMMONS</span>
        </a>
        <p>Open research infrastructure for accountable maritime evidence.</p>
      </div>
      <div className="site-footer__cols">
        <div>
          <span>Programme</span>
          <a href="#research">Research</a>
          <a href="#method">Methods</a>
          <a href="#governance">Governance</a>
          <a href="/docs">Documentation</a>
        </div>
        <div>
          <span>Surfaces</span>
          <a href="https://play.seacommons.org">Play ↗</a>
          <a href="https://live.seacommons.org">Live ↗</a>
          <a href="https://github.com/suezcanalxyz/seacommons">GitHub ↗</a>
        </div>
        <div>
          <span>Framework</span>
          <a href="/docs">Docs</a>
          <a href="https://api.seacommons.org/docs">API reference ↗</a>
          <a href="https://www.gnu.org/licenses/agpl-3.0.html">AGPL-3.0 ↗</a>
          <a href="/status">System status</a>
          <a href="mailto:research@seacommons.org">Contact</a>
        </div>
      </div>
      <div className="site-footer__base">
        <span>SeaCommons / research prototype</span>
        <a href="https://suezcanal.xyz">Developed by suezcanal.xyz</a>
        <span>© 2026</span>
      </div>
    </footer>
  );
}
