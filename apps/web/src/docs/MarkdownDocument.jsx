import React from 'react';

function slugify(value) {
  return value.toLowerCase().replace(/[^a-z0-9\s-]/g, '').trim().replace(/\s+/g, '-');
}

function inline(text) {
  const tokens = [];
  const re = /(\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
  let cursor = 0;
  let match;
  while ((match = re.exec(text)) !== null) {
    if (match.index > cursor) tokens.push(text.slice(cursor, match.index));
    const value = match[0];
    if (value.startsWith('**')) {
      tokens.push(<strong key={tokens.length}>{value.slice(2, -2)}</strong>);
    } else {
      const parts = value.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      tokens.push(<a key={tokens.length} href={parts[2]}>{parts[1]}</a>);
    }
    cursor = match.index + value.length;
  }
  if (cursor < text.length) tokens.push(text.slice(cursor));
  return tokens;
}

export function extractHeadings(markdown) {
  return markdown.split('\n').filter((line) => /^##\s+/.test(line)).map((line) => {
    const label = line.replace(/^##\s+/, '').trim();
    return { label, id: slugify(label) };
  });
}

export default function MarkdownDocument({ markdown }) {
  const lines = markdown.replace(/\r/g, '').split('\n');
  const blocks = [];
  let paragraph = [];
  let list = null;
  let quote = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(<p key={blocks.length}>{inline(paragraph.join(' '))}</p>);
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    const Tag = list.ordered ? 'ol' : 'ul';
    blocks.push(<Tag key={blocks.length} className="md-list">{list.items.map((item, index) => <li key={index}>{inline(item)}</li>)}</Tag>);
    list = null;
  };
  const flushQuote = () => {
    if (!quote.length) return;
    blocks.push(<blockquote key={blocks.length}>{inline(quote.join(' '))}</blockquote>);
    quote = [];
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line.trim()) {
      flushParagraph(); flushList(); flushQuote();
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      flushParagraph(); flushList(); flushQuote();
      const level = heading[1].length;
      const label = heading[2].trim();
      if (level === 1) blocks.push(<h1 key={blocks.length}>{inline(label)}</h1>);
      if (level === 2) blocks.push(<h2 id={slugify(label)} key={blocks.length}>{inline(label)}</h2>);
      if (level === 3) blocks.push(<h3 key={blocks.length}>{inline(label)}</h3>);
      continue;
    }

    if (line.startsWith('> ')) {
      flushParagraph(); flushList();
      quote.push(line.slice(2).trim());
      continue;
    }

    const unordered = line.match(/^[-*]\s+(.+)$/);
    const ordered = line.match(/^\d+\.\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph(); flushQuote();
      const isOrdered = Boolean(ordered);
      if (list && list.ordered !== isOrdered) flushList();
      if (!list) list = { ordered: isOrdered, items: [] };
      list.items.push((unordered || ordered)[1]);
      continue;
    }

    paragraph.push(line.trim());
  }

  flushParagraph(); flushList(); flushQuote();
  return <article className="markdown-document">{blocks}</article>;
}
