// Every Atlas page starts with one of these. Its heading receives focus after a navigation, so
// keyboard and screen reader users land at the start of the new page.
export default function PageHeader({ title, subtitle, actions, crumbs }) {
  return <header className="a-page-head">
    <div className="a-page-headmain">
      {crumbs}
      <h1 className="a-title" tabIndex={-1} data-page-heading>{title}</h1>
      {subtitle && <p className="a-subtitle">{subtitle}</p>}
    </div>
    {actions}
  </header>
}
