// Every Atlas page starts with one of these. Its heading receives focus after a navigation, so
// keyboard and screen reader users land at the start of the new page.
// `mark` is a decorative symbol drawn before the heading (outside it, so the heading is only its words).
export default function PageHeader({ title, subtitle, actions, crumbs, mark }) {
  const heading = <h1 className="a-title" tabIndex={-1} data-page-heading>{title}</h1>
  return <header className="a-page-head">
    <div className="a-page-headmain">
      {crumbs}
      {mark ? <div className="a-page-titlerow">{mark}{heading}</div> : heading}
      {subtitle && <p className="a-subtitle">{subtitle}</p>}
    </div>
    {actions}
  </header>
}
