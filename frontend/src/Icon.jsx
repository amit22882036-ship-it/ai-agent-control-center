const paths = {
  workspace: 'M4 4h6v6H4z M14 4h6v6h-6z M4 14h6v6H4z M14 14h6v6h-6z',
  search: 'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 2v2 M12 20v2 M2 12h2 M20 12h2 M5 5l1.5 1.5 M17.5 17.5L19 19 M5 19l1.5-1.5 M17.5 6.5L19 5',
  moon: 'M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11',
  monitor: 'M3 4h18v13H3z M8 21h8 M12 17v4',
  bell: 'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9 M10 21h4',
  info: 'M12 11v6 M12 8h.01 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  plus: 'M12 5v14 M5 12h14', close: 'M6 6l12 12 M6 18L18 6',
  chevron: 'M9 5l7 7-7 7', down: 'M5 9l7 7 7-7',
  arrow: 'M5 12h14 M13 6l6 6-6 6', filter: 'M4 6h16 M7 12h10 M10 18h4',
  history: 'M3 10a9 9 0 1 1 1 7 M3 4v6h6 M12 7v6l4 2',
  agent: 'M8 3h8 M12 3v3 M4 6h16v14H4z M8 11v2 M16 11v2 M9 17h6',
  edit: 'M13.5 6.5l4 4 M4 20l4.5-1 10-10a2.8 2.8 0 0 0-4-4l-10 10z',
  more: 'M5 12h.01 M12 12h.01 M19 12h.01',
}
export default function Icon({ name, size = 18, ...props }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...props}><path d={paths[name] || paths.agent} /></svg>
}
