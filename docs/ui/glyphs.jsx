// Harbour crane (fleet), compass (reckon), alembic (imas-ambix),
// flux surfaces (imas-efit), laptop (imas-codex).
// All use currentColor — accent colour is set by the container.

window.GLYPHS = {
  fleet: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 21 V5"/>
      <path d="M3 5 H 20"/>
      <rect x="3" y="4" width="2.5" height="2.2" fill="currentColor" stroke="none" opacity="0.55"/>
      <path d="M16 5 V 11" strokeWidth="1.1"/>
      <rect x="13" y="11" width="6" height="6" rx="0.6" fill="currentColor" opacity="0.18"/>
      <rect x="13" y="11" width="6" height="6" rx="0.6"/>
      <path d="M14.5 13 V15 M16 13 V15 M17.5 13 V15" strokeWidth="0.8" opacity="0.5"/>
      <path d="M3 21 H21" opacity="0.4"/>
    </svg>
  ),
  crew: (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="8" cy="8" r="2.5"/>
      <circle cx="16" cy="8" r="2.5"/>
      <path d="M3.5 19v-2.2c0-2.4 2-4.3 4.5-4.3s4.5 1.9 4.5 4.3V19"/>
      <path d="M11.5 14.2c.8-1.1 2.1-1.7 3.6-1.7 2.5 0 4.5 1.9 4.5 4.3V19" opacity="0.65"/>
    </svg>
  ),
  reckon: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9"/>
      <path d="M12 5 L 14 12 L 12 11 L 10 12 Z" fill="currentColor" stroke="none"/>
      <path d="M12 19 L 14 12 L 12 13 L 10 12 Z" fill="currentColor" stroke="none" opacity="0.35"/>
    </svg>
  ),
  "imas-ambix": (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M8.5 11.5 V8 H15.5 V11.5"/>
      <ellipse cx="12" cy="15" rx="5" ry="4.5"/>
      <path d="M15.5 9.5 H19 V14 H21"/>
      <path d="M8.2 16 a5 4 0 0 0 7.6 0" strokeWidth="1.1" opacity="0.5"/>
      <circle cx="21" cy="15.5" r="0.7" fill="currentColor" stroke="none"/>
    </svg>
  ),
  "imas-efit": (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3 C 17 3 19 7 19 12 C 19 17 16 21 12 21 C 8 21 5 17 5 12 C 5 7 7 3 12 3 Z"/>
      <path d="M12 6 C 15.5 6 17 9 17 12 C 17 15 14.5 18 12 18 C 9.5 18 7 15 7 12 C 7 9 8.5 6 12 6 Z" opacity="0.7"/>
      <path d="M12 9 C 14 9 15 10.5 15 12 C 15 13.5 13.5 15 12 15 C 10.5 15 9 13.5 9 12 C 9 10.5 10 9 12 9 Z" opacity="0.45"/>
      <circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none"/>
    </svg>
  ),
  "imas-codex": (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="4.5" y="4" width="15" height="11" rx="1.3"/>
      <path d="M3 17 H21"/>
      <path d="M10 17 L 9.5 18 H 14.5 L 14 17"/>
      <path d="M7 7 H 12 M7 9.5 H 14" strokeWidth="1" opacity="0.5"/>
    </svg>
  ),
  _default: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
      <rect x="4" y="4" width="16" height="16" rx="3"/>
    </svg>
  ),
};

window.ACCENTS = {
  reckon:       "oklch(0.45 0.16 270)",
  "imas-codex": "oklch(0.5  0.12 145)",
  "imas-ambix": "oklch(0.55 0.13  35)",
  "imas-efit":  "oklch(0.55 0.13 200)",
  _default:     "var(--accent)",
};
