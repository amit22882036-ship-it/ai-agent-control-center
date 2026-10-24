import Icon from './Icon'
import Popover from './Popover'
import ThemeControl from './ThemeControl'
import { workspaceSummary } from './agentPresentation.mjs'
import { noticeText } from './onboardingPresentation.mjs'
export default function WorkspaceBar({ agents, search, onSearch, notifications, showStart, onStart, newAgentButtonRef, startingAgent,
  view = 'workspace', onView, providerNotices = { notices: [] } }) {
  const notificationsBlocked = notifications.permission === 'denied'
  const notices = providerNotices.notices || []
  return <header className="workspace-bar">
    <a className="product-brand" href="#workspace"><span className="product-mark"><Icon name="workspace" size={20} /></span><span>Control Center<small>{workspaceSummary(agents)}</small></span></a>
    <label className="workspace-search"><Icon name="search" /><span className="sr-only">Search agents</span>
      <input type="search" placeholder="Search agents" value={search} onChange={(event) => onSearch(event.target.value)} />
    </label>
    <nav className="bar-nav" aria-label="Views">
      <button type="button" className={`close-button${view === 'workspace' ? ' active' : ''}`} aria-current={view === 'workspace' ? 'page' : undefined}
        onClick={() => onView?.('workspace')}>Agents</button>
      <button type="button" className={`close-button${view === 'providers' ? ' active' : ''}`} aria-current={view === 'providers' ? 'page' : undefined}
        onClick={() => onView?.('providers')}>AI Providers</button>
    </nav>
    <div className="bar-actions">
      <ThemeControl />
      <Popover label={notices.length ? `Notifications, ${notices.length} provider update${notices.length > 1 ? 's' : ''}` : 'Notifications'}
        className="notification-popover" trigger={<span className="bell-trigger"><Icon name="bell" />{notices.length > 0 && <span className="bell-badge" aria-hidden="true">{notices.length}</span>}</span>}>
        <h3>Notifications</h3>
        {notices.length > 0 && <ul className="provider-notices" aria-label="Provider updates">{notices.map((notice) => <li key={notice.id}>
          <p>{noticeText(notice)}</p>
          <div className="provider-actions">
            <button type="button" className="close-button" onClick={() => onView?.('providers')}>Review update</button>
            <button type="button" className="close-button" onClick={() => providerNotices.dismiss(notice, 'remind_later')}>Remind me later</button>
            <button type="button" className="close-button" onClick={() => providerNotices.dismiss(notice, 'dismiss')}>Dismiss</button>
          </div></li>)}</ul>}
        {providerNotices.error && <p className="agent-type-note" role="alert">{providerNotices.error}</p>}
        <p className="notification-status" role="status">{notifications.status}</p>
        <div className="notification-prerequisite"><Icon name="info" size={15} /><p>{notifications.prerequisite}</p></div>
        <button type="button" className="start-button" onClick={notifications.toggleNotifications} disabled={!notifications.supported || notifications.requesting || notificationsBlocked}>
          {notifications.requesting ? 'Requesting permission...'
            : notificationsBlocked ? 'Blocked in browser'
              : notifications.enabled ? 'Disable notifications' : 'Enable notifications'}
        </button>
        {notifications.message && <p className="agent-type-note" role="status">{notifications.message}</p>}
      </Popover>
      <button ref={newAgentButtonRef} className="start-button new-agent-button" aria-controls="new-agent-composer" aria-expanded={showStart} onClick={onStart} disabled={startingAgent}><Icon name="plus" />New agent</button>
    </div>
  </header>
}
