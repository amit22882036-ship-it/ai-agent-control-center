import PageHeader from '../shell/PageHeader'
import { Button, EmptyState, Notice, Skeleton } from '../components/primitives'
import { connectionView, overviewView } from '../status.mjs'
import { exitAtlas } from '../uiMode.mjs'

const time = (date) => date?.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

// M1: the Work page frame with the backend's own workspace totals. The task list, the map and
// agent controls are later milestones, so this page says so instead of imitating them.
export default function WorkPage({ live }) {
  const tiles = overviewView(live.overview)
  const connection = connectionView(live)
  return <div className="a-page">
    <PageHeader title="Work" subtitle="Your tasks and the agents working on them." />
    <div className="a-stack">
      <Notice tone="info" title="You are using the Atlas preview"
        actions={<Button variant="primary" icon="swap" onClick={() => exitAtlas()}>Open Classic view</Button>}>
        The new design, navigation and appearance are in place. The task list, the map, task pages and agent controls
        arrive in the next milestones. Until then, follow and manage your work in Classic view. Nothing on this page
        starts, stops or changes an agent.
      </Notice>
      <section aria-labelledby="atlas-work-summary">
        <div className="a-summary-head">
          <h2 id="atlas-work-summary">Right now</h2>
          <span className="a-fine">{connection.tone === 'live' ? 'Updated as changes happen' : connection.detail}</span>
        </div>
        {tiles ? <>
          {live.reachable === false && <Notice tone="warn" role="status" title="Showing the last values received">
            {`Received at ${time(live.receivedAt)}. The backend is not answering right now.`}</Notice>}
          <div className="a-tiles">{tiles.map((tile) => <div key={tile.key}
            className={`a-tile${tile.key === 'needs_input' && tile.value > 0 ? ' a-tile--ask' : ''}`}>
            {tile.value === null
              ? <span className="a-tile-value a-tile-value--na">Not available</span>
              : <span className="a-tile-value a-num">{tile.value}</span>}
            <span className="a-tile-label">{tile.label}</span>
          </div>)}</div>
          <p className="a-fine a-tiles-note">Totals cover all recorded work, not only what a page shows. A completed task is not
            the same as a result applied to your project.</p>
        </> : live.reachable === false
          ? <EmptyState icon="plug" title="The Control Center backend isn’t answering">
            Start the backend; this page updates by itself. It keeps retrying automatically.</EmptyState>
          : <Skeleton lines={3} label="Loading the workspace summary" />}
      </section>
    </div>
  </div>
}
