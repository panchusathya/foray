// The shared pieces. Everything here is presentational — no fetching, no
// routing decisions — so a page is a data shape plus a layout.

import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { initials, isLive, statusLabel, statusTone } from '../lib/applications.js';
import { IconAlert, IconArrow, IconBack, IconCheck, IconCopy } from './icons.jsx';

export function Screen({ children, wide = false }) {
  return <main className={wide ? 'screen wide' : 'screen'}>{children}</main>;
}

export function Head({ title, sub, action }) {
  return (
    <header className="screen-head">
      <div className="grow">
        <h1>{title}</h1>
        {sub ? <div className="sub">{sub}</div> : null}
      </div>
      {action}
    </header>
  );
}

export function BackLink({ to, children }) {
  return (
    <Link to={to} className="back">
      <IconBack />
      {children}
    </Link>
  );
}

export function SectionHead({ label, to, linkLabel = 'See all' }) {
  return (
    <div className="spread">
      <span className="section-label">{label}</span>
      {to ? (
        <Link to={to} className="section-link">
          {linkLabel}
          <IconArrow />
        </Link>
      ) : null}
    </div>
  );
}

export function Mono({ name, size = '', tone = '' }) {
  return <span className={['mono', size, tone].filter(Boolean).join(' ')}>{initials(name)}</span>;
}

export function Empty({ title, children, action, glyph }) {
  return (
    <div className="empty">
      {glyph ? <span className="mono gold">{glyph}</span> : null}
      <h2>{title}</h2>
      {children ? <p>{children}</p> : null}
      {action}
    </div>
  );
}

export function ErrorNote({ error }) {
  if (!error) return null;
  return (
    <div className="banner error" role="alert">
      <IconAlert />
      <span>{error.message || String(error)}</span>
    </div>
  );
}

export function Note({ children, tone = 'info' }) {
  if (!children) return null;
  return (
    <div className={`banner ${tone}`}>
      {tone === 'good' ? <IconCheck /> : null}
      <span>{children}</span>
    </div>
  );
}

export function Skeleton({ height = 74, count = 3 }) {
  return (
    <div className="stack" aria-hidden="true">
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="skel" style={{ height }} />
      ))}
    </div>
  );
}

export function StatusChip({ status }) {
  const classes = ['status', statusTone(status), isLive(status) ? 'live' : '']
    .filter(Boolean)
    .join(' ');
  return <span className={classes}>{statusLabel(status)}</span>;
}

export function Stat({ n, k, tone = '', to }) {
  const body = (
    <>
      <div className="n">{n}</div>
      <div className="k">{k}</div>
    </>
  );
  const className = ['stat', tone].filter(Boolean).join(' ');
  return to ? (
    <Link to={to} className={className}>
      {body}
    </Link>
  ) : (
    <div className={className}>{body}</div>
  );
}

/**
 * Copy to clipboard, with the confirmation on the button itself.
 *
 * The clipboard is permission-gated and simply absent in some in-app
 * browsers, so a failure says nothing and calls `onFallback` — the text is on
 * screen either way, and an error somebody cannot act on is worse than none.
 */
export function CopyButton({ text, label = 'Copy', className = 'btn small', onFallback }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef(null);

  useEffect(() => () => clearTimeout(timer.current), []);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1800);
    } catch {
      if (onFallback) onFallback();
    }
  }, [text, onFallback]);

  return (
    <button type="button" className={className} onClick={copy} disabled={!text}>
      {copied ? <IconCheck /> : <IconCopy />}
      {copied ? 'Copied' : label}
    </button>
  );
}

/**
 * A bottom sheet on a phone, a wide card on a desktop (the CSS handles the
 * difference). Escape closes it and focus moves into it on open, because this
 * is where a drafted message gets edited and it must not be a keyboard trap.
 */
export function Sheet({ title, sub, onClose, children }) {
  const panel = useRef(null);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    panel.current?.focus();
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = previous;
    };
  }, [onClose]);

  return (
    <div
      className="scrim"
      onPointerDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        className="sheet"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panel}
      >
        <div className="grabber" />
        <h2>{title}</h2>
        {sub ? (
          <p className="tiny" style={{ marginBottom: 'var(--s-3)' }}>
            {sub}
          </p>
        ) : null}
        {children}
      </div>
    </div>
  );
}
