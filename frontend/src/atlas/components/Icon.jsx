// Atlas line icons (24px grid, 1.8px stroke). Decorative by default: pair them with visible text
// or an accessible name on the control that contains them.
const PATHS = {
  search: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4',
  plus: 'M12 5v14M5 12h14',
  down: 'M6 9l6 6 6-6',
  right: 'M9 6l6 6-6 6',
  left: 'M15 6l-6 6 6 6',
  list: 'M8 6.5h12M8 12h12M8 17.5h12M4 6.5h.01M4 12h.01M4 17.5h.01',
  map: 'M12 4.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM5.5 15a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM18.5 15a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM12 9.5v2.5M5.5 15v-1a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1',
  sun: 'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4',
  moon: 'M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10z',
  monitor: 'M3.5 5h17v11h-17zM9 20h6M12 16v4',
  close: 'M6 6l12 12M18 6L6 18',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  stop: 'M7.5 7.5h9v9h-9z',
  reply: 'M10 7l-5 5 5 5M5 12h9a5 5 0 0 1 5 5v1',
  redirect: 'M15 5.5l4 4-4 4M19 9.5H10a5 5 0 0 0-5 5v4',
  more: 'M6 12h.01M12 12h.01M18 12h.01',
  focus: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3',
  folder: 'M3.5 7.5a2 2 0 0 1 2-2h3.8l2 2h7.2a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z',
  open: 'M14 4.5h5.5V10M19.5 4.5L11 13M18 14v5.5H4.5V6H10',
  hand: 'M8 13V6.5a1.5 1.5 0 0 1 3 0V12M11 11V4.5a1.5 1.5 0 0 1 3 0V11M14 11V6a1.5 1.5 0 0 1 3 0v7a6 6 0 0 1-6 6h-.5A5.5 5.5 0 0 1 5 15.2L3.8 12a1.5 1.5 0 0 1 2.7-1.3L8 13',
  pause: 'M9 6v12M15 6v12',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7.5V12l3 2',
  bolt: 'M13 3L5 13h6l-1 8 8-10h-6z',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 7.5h.01',
  warn: 'M12 4l9 16H3zM12 10v4M12 17h.01',
  edit: 'M14.5 5.5l4 4M4.5 19.5l4-.9 10.6-10.6-3.1-3.1L5.4 15.5z',
  play: 'M8 5.5l11 6.5-11 6.5z',
  sliders: 'M4 7h9M17 7h3M4 17h3M11 17h9M15 5v4M9 15v4',
  grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  layers: 'M12 3.5l8.5 4.6-8.5 4.6-8.5-4.6zM3.5 13l8.5 4.6 8.5-4.6',
  refresh: 'M19.5 11A7.5 7.5 0 0 0 5.6 7.6M4.5 4.5v3.4h3.4M4.5 13a7.5 7.5 0 0 0 13.9 3.4M19.5 19.5v-3.4h-3.4',
  bell: 'M6 9a6 6 0 1 1 12 0c0 6 2 7.5 2 7.5H4S6 15 6 9M10 20a2 2 0 0 0 4 0',
  plug: 'M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0zM12 16v5',
  swap: 'M7 7h12l-3-3M17 17H5l3 3',
  keyboard: 'M3.5 6.5h17v11h-17zM7 10h.01M10 10h.01M13 10h.01M16 10h.01M8 14h8',
}

export default function Icon({ name, size = 16, stroke = 1.8, ...props }) {
  return <svg className="a-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth={name === 'more' ? 2.8 : stroke} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...props}>
    <path d={PATHS[name] || PATHS.info} /></svg>
}
