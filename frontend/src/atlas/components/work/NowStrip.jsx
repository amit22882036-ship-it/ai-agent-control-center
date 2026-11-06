import { StatusBead } from '../identity'
import Icon from '../Icon'

const READOUTS = [
  { scope: 'needs', key: 'needs', bead: 'ask', label: 'Need you', one: 'Needs you' },
  { scope: 'working', key: 'working', bead: 'work', label: 'Working', one: 'Working' },
  { scope: 'waiting', key: 'waiting', bead: 'helpers', label: 'Waiting', one: 'Waiting' },
  { scope: 'held', key: 'held', bead: 'blocked', label: 'On hold', one: 'On hold' },
  { scope: 'pending', key: 'pending', bead: 'pending', label: 'Not started', one: 'Not started' },
]

// What is happening, in counts of loaded Tasks, and the way to scope Work to any of it. Each
// readout is a toggle: pressing the one that is on returns to all work. Counts are of the Tasks
// loaded so far and say so when older pages remain.
export default function NowStrip({ counts, families, filter, onScope, partial, changedCount = 0, onChanged, onClearChanged, onFirstNeed, stepping, describedBy }) {
  const scoped = filter !== 'all'
  const open = READOUTS.filter((item) => counts[item.key] > 0)
  return <div className="a-now-wrap"><div className="a-now" role="group" aria-label="What is happening" aria-describedby={describedBy}>
    <button type="button" className="a-now-item a-now-item--all" aria-pressed={!scoped} onClick={() => onScope('all')}
      title={partial ? 'Every loaded task family. Older pages are not counted.' : 'Every task family'}>
      <span className="a-now-n a-num">{families.all}</span>{' '}
      <span className="a-now-l">{families.all === 1 ? 'Task family' : 'Task families'}</span>
    </button>
    <span className="a-now-sep" aria-hidden="true" />
    {READOUTS.map((item) => {
      const n = counts[item.key]
      const on = filter === item.scope
      return <button key={item.scope} type="button" className={`a-now-item a-now-item--${item.key}${n ? '' : ' is-zero'}`} aria-pressed={on}
        onClick={() => onScope(on ? 'all' : item.scope)} title={`${n} ${n === 1 ? 'task' : 'tasks'}${partial ? ' among the loaded tasks' : ''}`}>
        <span className="a-now-n a-num">
          <StatusBead state={item.bead} size={13} decorative />{n}
        </span>{' '}
        <span className="a-now-l">{n === 1 ? item.one : item.label}</span>
      </button>
    }).flatMap((node, index) => (index === 0 && onFirstNeed && !stepping ? [node, <button key="go" type="button" className="a-now-go"
      onClick={onFirstNeed} aria-label="Next task that needs you" title="Next task that needs you · A" aria-keyshortcuts="A">
      Next<Icon name="right" size={13} /></button>] : [node]))}
    <span className="a-sp" />
    {changedCount > 0 && <span className="a-now-changed">
      <button type="button" className="a-now-item a-now-item--changed" aria-pressed={filter === 'changed'} onClick={() => onChanged(filter === 'changed' ? 'all' : 'changed')}
        title="Tasks whose record changed since you last opened them or cleared these marks">
        <span className="a-now-n a-num"><span className="a-changed" aria-hidden="true" />{changedCount}</span>{' '}
        <span className="a-now-l">Changed</span>
      </button>
      <button type="button" className="a-link a-now-clear" onClick={onClearChanged}>Mark all seen</button>
    </span>}
    <button type="button" className="a-now-item a-now-item--done" aria-pressed={filter === 'done'}
      onClick={() => onScope(filter === 'done' ? 'all' : 'done')} title={`${families.done} finished task ${families.done === 1 ? 'family' : 'families'}`}>
      <span className="a-now-n a-num"><StatusBead state="done" size={13} decorative />{families.done}</span>{' '}
      <span className="a-now-l">Done</span>
    </button>
    {(families.canceled > 0 || filter === 'canceled') && <button type="button" className="a-now-item a-now-item--canceled" aria-pressed={filter === 'canceled'}
      onClick={() => onScope(filter === 'canceled' ? 'all' : 'canceled')} title={`${families.canceled} canceled task ${families.canceled === 1 ? 'family' : 'families'}`}>
      <span className="a-now-n a-num"><StatusBead state="stopped" size={13} decorative />{families.canceled}</span>{' '}
      <span className="a-now-l">Canceled</span>
    </button>}
  </div>
  {/* Open work in proportion, in the same colours as the counts above it. */}
  {open.length > 0 && <div className="a-nowbar" aria-hidden="true">
    {open.map((item) => <i key={item.key} className={`is-${item.key}`} style={{ flexGrow: counts[item.key] }} />)}
  </div>}
  </div>
}
