import Popover from './Popover'
import { displayColors } from './agentIdentity.mjs'

const colorName = (value) => value[0].toUpperCase() + value.slice(1)

export default function AgentColorFilter({ selectedColors, onChange }) {
  const selectedNames = displayColors.filter((color) => selectedColors.has(color)).map(colorName)
  const triggerLabel = selectedNames.length === 0
    ? 'Color · Any'
    : selectedNames.length === 1 ? `Color · ${selectedNames[0]}` : `Colors · ${selectedNames.length}`
  const title = selectedNames.length ? `Agent colors: ${selectedNames.join(', ')}` : 'Agent colors: Any color'

  function toggle(color) {
    const next = new Set(selectedColors)
    if (next.has(color)) next.delete(color)
    else next.add(color)
    onChange(next)
  }

  return <Popover className="agent-color-filter" label={title} trigger={<span title={title}>{triggerLabel}</span>}>
    <fieldset className="choice-group"><legend>Filter by color</legend>
      <button type="button" className={`choice color-filter-any${selectedColors.size === 0 ? ' selected' : ''}`}
        aria-pressed={selectedColors.size === 0} onClick={() => onChange(new Set())}>
        <span>Any color</span>
        <span className="choice-count">Clear selections</span>
      </button>
      {displayColors.map((color) => <label className="choice color-filter-choice" key={color}>
        <input type="checkbox" checked={selectedColors.has(color)} onChange={() => toggle(color)} />
        <span className="filter-check" aria-hidden="true" />
        <span className="color-swatch" data-display-color={color} />
        <span className="color-filter-label">{colorName(color)}</span>
      </label>)}
    </fieldset>
  </Popover>
}
