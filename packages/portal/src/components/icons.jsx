/**
 * Inline stroke icons, drawn on a 24-grid at 1.7 weight.
 *
 * Inline and hand-drawn rather than an icon package: this origin's CSP is
 * `default-src 'self'` with no CDN allowance, an icon font would need a
 * `font-src` widening for eight glyphs, and a package would ship a few hundred
 * paths to use nine. `currentColor` everywhere so a tab, a button and a chip
 * all tint their icon from the text colour they already set.
 */

function Icon({ children, ...rest }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const IconHome = (props) => (
  <Icon {...props}>
    <path d="M3.6 10.2 12 3.8l8.4 6.4V19a1.4 1.4 0 0 1-1.4 1.4h-3.2v-6h-7.6v6H5a1.4 1.4 0 0 1-1.4-1.4Z" />
  </Icon>
);

export const IconSpark = (props) => (
  <Icon {...props}>
    <path d="M12 3.2 13.9 9l5.9 1.9-5.9 1.9-1.9 5.9-1.9-5.9L4.2 10.9 10.1 9Z" />
    <path d="M18.6 3.4v3.2M20.2 5h-3.2" />
  </Icon>
);

export const IconTracker = (props) => (
  <Icon {...props}>
    <rect x="3.4" y="4.4" width="5.4" height="15.2" rx="1.5" />
    <rect x="10.9" y="4.4" width="5.4" height="9.4" rx="1.5" />
    <path d="M18.4 4.4h2.2v15.2h-2.2z" opacity=".45" />
  </Icon>
);

export const IconPeople = (props) => (
  <Icon {...props}>
    <circle cx="9.4" cy="8.4" r="3.2" />
    <path d="M3.6 19.4c0-3 2.6-5.2 5.8-5.2s5.8 2.2 5.8 5.2" />
    <path d="M16.4 5.6a3 3 0 0 1 0 5.7M17.6 14.6c1.9.6 3.2 2.2 3.2 4.3" />
  </Icon>
);

export const IconDoc = (props) => (
  <Icon {...props}>
    <path d="M13.4 3.6H7.2a1.6 1.6 0 0 0-1.6 1.6v13.6a1.6 1.6 0 0 0 1.6 1.6h9.6a1.6 1.6 0 0 0 1.6-1.6V8.6Z" />
    <path d="M13.4 3.6v5h5" />
    <path d="M9 13.4h6M9 16.6h4" />
  </Icon>
);

export const IconUser = (props) => (
  <Icon {...props}>
    <circle cx="12" cy="8.2" r="3.6" />
    <path d="M4.8 20c0-3.6 3.2-6.2 7.2-6.2s7.2 2.6 7.2 6.2" />
  </Icon>
);

export const IconCheck = (props) => (
  <Icon {...props}>
    <path d="m5 12.6 4.4 4.4L19 7.4" />
  </Icon>
);

export const IconX = (props) => (
  <Icon {...props}>
    <path d="M6.4 6.4 17.6 17.6M17.6 6.4 6.4 17.6" />
  </Icon>
);

export const IconArrow = (props) => (
  <Icon {...props}>
    <path d="M5 12h13M12.4 6l6 6-6 6" />
  </Icon>
);

export const IconBack = (props) => (
  <Icon {...props}>
    <path d="M19 12H6M11.6 18 5.6 12l6-6" />
  </Icon>
);

export const IconSearch = (props) => (
  <Icon {...props}>
    <circle cx="10.8" cy="10.8" r="6.2" />
    <path d="m15.4 15.4 4 4" />
  </Icon>
);

export const IconCopy = (props) => (
  <Icon {...props}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M15.4 5.6A1.6 1.6 0 0 0 13.8 4H6a2 2 0 0 0-2 2v7.8a1.6 1.6 0 0 0 1.6 1.6" />
  </Icon>
);

export const IconExternal = (props) => (
  <Icon {...props}>
    <path d="M13.4 4.6H19.4v6" />
    <path d="m19.4 4.6-8 8" />
    <path d="M18 14.4v4a1.6 1.6 0 0 1-1.6 1.6H5.6A1.6 1.6 0 0 1 4 18.4V7.6A1.6 1.6 0 0 1 5.6 6h4" />
  </Icon>
);

export const IconBuilding = (props) => (
  <Icon {...props}>
    <path d="M4.6 20.4V5.6a1.4 1.4 0 0 1 1.4-1.4h7a1.4 1.4 0 0 1 1.4 1.4v14.8" />
    <path d="M14.4 9.6h3.6a1.4 1.4 0 0 1 1.4 1.4v9.4" />
    <path d="M7.8 8h3M7.8 11.6h3M7.8 15.2h3M3 20.4h18" />
  </Icon>
);

export const IconWrite = (props) => (
  <Icon {...props}>
    <path d="M16.2 4.4a2 2 0 0 1 2.8 2.8L8.6 17.6l-3.8 1 1-3.8Z" />
    <path d="m14.4 6.2 3.4 3.4" />
  </Icon>
);

export const IconList = (props) => (
  <Icon {...props}>
    <path d="M8.4 6.6h11M8.4 12h11M8.4 17.4h11" />
    <path d="M4.6 6.6h.01M4.6 12h.01M4.6 17.4h.01" />
  </Icon>
);

export const IconBoard = (props) => (
  <Icon {...props}>
    <rect x="3.6" y="4.6" width="5.4" height="14.8" rx="1.4" />
    <rect x="11" y="4.6" width="5.4" height="9.4" rx="1.4" />
    <rect x="18.4" y="4.6" width="2" height="12" rx="1" />
  </Icon>
);

export const IconAlert = (props) => (
  <Icon {...props}>
    <circle cx="12" cy="12" r="8.4" />
    <path d="M12 7.8v5M12 16.1h.01" />
  </Icon>
);

export const IconClock = (props) => (
  <Icon {...props}>
    <circle cx="12" cy="12" r="8.4" />
    <path d="M12 7.4V12l3 1.8" />
  </Icon>
);

export const IconSignOut = (props) => (
  <Icon {...props}>
    <path d="M9.4 20H6a1.6 1.6 0 0 1-1.6-1.6V5.6A1.6 1.6 0 0 1 6 4h3.4" />
    <path d="M15.4 15.6 19 12l-3.6-3.6M19 12H9.2" />
  </Icon>
);
