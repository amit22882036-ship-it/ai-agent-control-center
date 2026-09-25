import Icon from './Icon'
import Popover from './Popover'
import ThemeControl from './ThemeControl'
import { workspaceSummary } from './agentPresentation.mjs'
export default function WorkspaceBar({ agents, search, onSearch, notifications, showStart, onStart, newAgentButtonRef, startingAgent }) {
  const notificationsBlocked = notifications.permission === 'denied'
  return <header className="workspace-bar">
    <a className="product-brand" href="#workspace"><span className="product-mark"><Icon name="workspace" size={20} /></span><span>Control Center<small>{workspaceSummary(agents)}</small></span></a>
    <label className="workspace-search"><Icon name="search" /><span className="sr-only">Search agents</span>
      <input type="search" placeholder="Search agents" value={search} onChange={(event) => onSearch(event.target.value)} />
    </label>
    <div className="bar-actions">
      <ThemeControl />
      <Popover label="Notifications" className="notification-popover" trigger={<Icon name="bell" />}>
        <h3>Notifications</h3><p className="notification-status" role="status">{notifications.status}</p>
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
