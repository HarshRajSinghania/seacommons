import React, { useEffect, useMemo, useState } from 'react';
import publicGuide from './content/SEACOMMONS.md?raw';
import MarkdownDocument, { extractHeadings } from './MarkdownDocument.jsx';

const TOP_LINKS = [
  ['Home', '/'],
  ['Live', 'https://live.seacommons.org'],
  ['Play', 'https://play.seacommons.org'],
  ['Docs', '/docs'],
  ['API', 'https://api.seacommons.org/docs'],
  ['Status', '/status'],
];

function Brand() {
  return (
    <a className="docs-brand" href="/" aria-label="SeaCommons home">
      <span className="docs-brand__mark" aria-hidden="true"><i /><i /></span>
      <span>SEA<br />COMMONS</span>
    </a>
  );
}

function DocsMenu({ open, onToggle, onClose }) {
  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <>
      <button
        type="button"
        className={`docs-menu-toggle ${open ? 'is-open' : ''}`}
        aria-label={open ? 'Close navigation' : 'Open navigation'}
        aria-expanded={open}
        aria-controls="docs-menu"
        onClick={onToggle}
      >
        <i aria-hidden="true" />
      </button>
      <button
        type="button"
        className={`docs-menu-backdrop ${open ? 'is-open' : ''}`}
        aria-label="Close navigation"
        tabIndex={open ? 0 : -1}
        onClick={onClose}
      />
      <nav id="docs-menu" className={`docs-menu ${open ? 'is-open' : ''}`} aria-label="SeaCommons">
        <span className="docs-menu__eyebrow">SeaCommons</span>
        {TOP_LINKS.map(([label, href], index) => (
          <a key={href} href={href} onClick={onClose}>
            <small>{String(index + 1).padStart(2, '0')}</small>
            <span>{label}</span>
            <i aria-hidden="true">↗</i>
          </a>
        ))}
      </nav>
    </>
  );
}

export default function DocsApp() {
  const [menuOpen, setMenuOpen] = useState(false);
  const headings = useMemo(() => extractHeadings(publicGuide), []);

  useEffect(() => {
    document.body.style.overflow = menuOpen ? 'hidden' : '';
    return () => { document.body.style.overflow = ''; };
  }, [menuOpen]);

  return (
    <div className="docs-shell">
      <header className="docs-header">
        <Brand />
        <div className="docs-header__meta">
          <span>Public technical reference</span>
          <span>22 Sep 2026</span>
        </div>
        <DocsMenu
          open={menuOpen}
          onToggle={() => setMenuOpen((value) => !value)}
          onClose={() => setMenuOpen(false)}
        />
      </header>

      <aside className="docs-sidebar" aria-label="Documentation chapters">
        <div className="docs-sidebar__intro">
          <span>Documentation</span>
          <strong>SeaCommons</strong>
          <p>Architecture, evidence model, public interfaces and operational limits.</p>
        </div>
        <nav>
          {headings.map(({ label, id }) => (
            <a key={id} href={`#${id}`}>
              <small>{label.match(/^\d+/)?.[0] || '•'}</small>
              <span>{label.replace(/^\d+\.\s*/, '')}</span>
            </a>
          ))}
        </nav>
      </aside>

      <main className="docs-main" id="docs-main">
        <div className="docs-document-head">
          <p className="docs-kicker">Canonical public documentation</p>
          <div className="docs-document-head__actions">
            <a href="https://live.seacommons.org">Open Live ↗</a>
            <a href="https://play.seacommons.org">Open Play ↗</a>
            <a href="https://api.seacommons.org/docs">API reference ↗</a>
          </div>
        </div>

        <MarkdownDocument markdown={publicGuide} />

        <footer className="docs-footer">
          <Brand />
          <p>
            SeaCommons is open research infrastructure for accountable maritime evidence.
            Public documentation is bundled with the product and released with the same build.
          </p>
          <div>
            <a href="/">Home</a>
            <a href="https://live.seacommons.org">Live</a>
            <a href="https://play.seacommons.org">Play</a>
            <a href="https://api.seacommons.org/docs">API</a>
          </div>
        </footer>
      </main>
    </div>
  );
}
