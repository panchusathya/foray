// A dropdown of checkboxes: pick any number of options, or none for "all".
//
// The button names what is chosen ("All sequences", one name, or "3
// sequences"), so the filter in force is readable without opening it. The
// panel closes on Escape or a click outside, and filters as you type once the
// list is long enough to need it.
import { useEffect, useRef, useState } from 'react';

export function MultiSelect({
  options,
  value,
  onChange,
  allLabel = 'All',
  noun = 'items',
  testId,
  searchFrom = 8,
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const root = useRef(null);
  const chosen = new Set(value);

  useEffect(() => {
    if (!open) return undefined;
    function onDown(event) {
      if (root.current && !root.current.contains(event.target)) setOpen(false);
    }
    function onKey(event) {
      if (event.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const label = !value.length
    ? allLabel
    : value.length === 1
      ? (options.find((option) => option.value === value[0])?.label ?? `1 ${noun}`)
      : `${value.length} ${noun}`;

  const needle = query.trim().toLowerCase();
  const shown = needle
    ? options.filter((option) => option.label.toLowerCase().includes(needle))
    : options;

  function toggle(optionValue) {
    const next = new Set(chosen);
    if (next.has(optionValue)) next.delete(optionValue);
    else next.add(optionValue);
    // Keep the options' own order so the button text is stable.
    onChange(options.map((option) => option.value).filter((v) => next.has(v)));
  }

  return (
    <div className="multiselect" ref={root} data-testid={testId}>
      <button
        type="button"
        className="multiselect-button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="multiselect-label">{label}</span>
        <span aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div className="multiselect-panel card" role="listbox" aria-multiselectable="true">
          {options.length >= searchFrom ? (
            <input
              type="search"
              placeholder="Filter…"
              value={query}
              autoFocus
              onChange={(event) => setQuery(event.target.value)}
            />
          ) : null}
          <div className="row spread small" style={{ padding: '4px 2px' }}>
            <button
              type="button"
              className="tiny ghost"
              onClick={() => onChange(options.map((option) => option.value))}
            >
              Select all
            </button>
            <button type="button" className="tiny ghost" disabled={!value.length} onClick={() => onChange([])}>
              Clear ({allLabel.toLowerCase()})
            </button>
          </div>
          <div className="multiselect-options">
            {shown.map((option) => (
              <label key={option.value} className="multiselect-option small">
                <input
                  type="checkbox"
                  checked={chosen.has(option.value)}
                  onChange={() => toggle(option.value)}
                />
                <span>{option.label}</span>
              </label>
            ))}
            {!shown.length ? <div className="muted small">No match.</div> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
