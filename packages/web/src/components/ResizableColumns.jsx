// Drag-to-resize columns for a `.dgrid` table.
//
// The grid is table-layout: fixed, so a <colgroup> sets every width and the
// table's own width is their sum -- wider than the window means the
// surrounding `.dgrid-wrap` scrolls sideways rather than squeezing columns.
// Widths are remembered per browser (localStorage, best effort: a private
// window or blocked storage just starts from the defaults each time).
// Double-click a handle to put that column back to its default.
import { useCallback, useEffect, useRef, useState } from 'react';

const MIN_WIDTH = 48;

function load(storageKey, defaults) {
  try {
    const saved = JSON.parse(window.localStorage.getItem(storageKey) || '{}');
    const merged = { ...defaults };
    for (const [key, value] of Object.entries(saved)) {
      if (key in defaults && Number.isFinite(value)) merged[key] = Math.max(MIN_WIDTH, value);
    }
    return merged;
  } catch {
    return { ...defaults };
  }
}

export function useColumnWidths(storageKey, defaults) {
  const [widths, setWidths] = useState(() => load(storageKey, defaults));
  const drag = useRef(null);

  useEffect(() => {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(widths));
    } catch {
      // Remembering widths is a convenience; nothing depends on it.
    }
  }, [storageKey, widths]);

  useEffect(() => {
    function onMove(event) {
      const current = drag.current;
      if (!current) return;
      const width = Math.max(MIN_WIDTH, current.start + event.clientX - current.x);
      setWidths((previous) => ({ ...previous, [current.key]: width }));
    }
    function onUp() {
      if (!drag.current) return;
      drag.current = null;
      document.body.classList.remove('col-resizing');
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, []);

  const startResize = useCallback(
    (key, event) => {
      event.preventDefault();
      event.stopPropagation();
      drag.current = { key, x: event.clientX, start: widths[key] };
      document.body.classList.add('col-resizing');
    },
    [widths],
  );

  const reset = useCallback(
    (key) => setWidths((previous) => ({ ...previous, [key]: defaults[key] })),
    [defaults],
  );

  const total = Object.values(widths).reduce((sum, value) => sum + value, 0);
  return { widths, total, startResize, reset };
}

export function ColGroup({ columns, widths }) {
  return (
    <colgroup>
      {columns.map((key) => (
        <col key={key} style={{ width: widths[key] }} />
      ))}
    </colgroup>
  );
}

/** The drag handle on a header cell's right edge. */
export function ResizeHandle({ column, startResize, reset }) {
  return (
    <span
      className="col-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${column} column`}
      title="Drag to resize · double-click to reset"
      onMouseDown={(event) => startResize(column, event)}
      onDoubleClick={() => reset(column)}
      onClick={(event) => event.stopPropagation()}
    />
  );
}
