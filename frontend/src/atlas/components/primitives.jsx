import Icon from './Icon'

const cx = (...names) => names.filter(Boolean).join(' ')

export function Button({ variant = 'secondary', size = 'md', icon, kbd, children, className, type = 'button', ...props }) {
  return <button type={type} className={cx('a-btn', `a-btn--${variant}`, size === 'sm' && 'a-btn--sm', className)} {...props}>
    {icon && <Icon name={icon} size={size === 'sm' ? 14 : 15} />}
    {children}
    {kbd && <kbd className="a-kbd" aria-hidden="true">{kbd}</kbd>}
  </button>
}

// An icon-only control always carries an accessible name, also shown as a tooltip.
export function IconButton({ icon, label, size = 'md', className, ...props }) {
  return <button type="button" className={cx('a-iconbtn', size === 'sm' && 'a-iconbtn--sm', className)} aria-label={label} title={label} {...props}>
    <Icon name={icon} size={size === 'sm' ? 15 : 17} />
  </button>
}

export function Chip({ tone = 'neutral', children }) {
  return <span className={cx('a-chip', tone !== 'neutral' && `a-chip--${tone}`)}>{children}</span>
}

export function Kbd({ children }) {
  return <kbd className="a-kbd">{children}</kbd>
}

// A section card. With `titleId` its heading names the section and can receive focus (tabIndex -1)
// when an in-page index jumps to it.
export function Card({ title, titleId, aside, children, className, as: Tag = 'section', ...props }) {
  return <Tag className={cx('a-card', className)} aria-labelledby={titleId} {...props}>
    {(title || aside) && <div className="a-card-head">
      {title && <h2 className="a-card-title" id={titleId} tabIndex={titleId ? -1 : undefined}>{title}</h2>}{aside}</div>}
    <div className="a-card-body">{children}</div>
  </Tag>
}

const NOTICE_ICONS = { info: 'info', ok: 'check', warn: 'clock', error: 'warn' }
export function Notice({ tone = 'info', title, children, actions, role }) {
  return <div className={cx('a-notice', `a-notice--${tone}`)} role={role}>
    <span className="a-notice-icon"><Icon name={NOTICE_ICONS[tone]} size={17} /></span>
    <div className="a-notice-text">{title && <strong>{title}</strong>}{children && <span>{children}</span>}</div>
    {actions && <div className="a-notice-actions">{actions}</div>}
  </div>
}

export function EmptyState({ icon = 'layers', title, children, actions }) {
  return <div className="a-empty">
    <span className="a-empty-icon"><Icon name={icon} size={22} /></span>
    <h3 className="a-empty-title">{title}</h3>
    {children && <p className="a-empty-text">{children}</p>}
    {actions && <div className="a-empty-actions">{actions}</div>}
  </div>
}

// A loading placeholder in the final layout's shape; screen readers hear the label once.
export function Skeleton({ lines = 3, label = 'Loading' }) {
  return <div className="a-skeleton" role="status" aria-label={label}>
    {Array.from({ length: lines }, (_, index) => <span key={index} className="a-skeleton-line" style={{ width: `${92 - index * 17}%` }} />)}
  </div>
}
