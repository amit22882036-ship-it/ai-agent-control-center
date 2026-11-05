import Icon from '../Icon'
import { AUTO_PROVIDERS } from '../../../allocationPresentation.mjs'
import { providerLabel } from '../../../providerPresentation.mjs'
import { toolState } from '../../model/newTask.mjs'
import { TOOLS } from '../../vocabulary.mjs'

const TOOL_COLOR = { claude: 'blue', codex: 'green', gemini: 'yellow', mock: 'neutral' }

// Helpers: optional, only for a Claude Code or Codex lead, and only the two real policies.
export function HelpersSection({ draft, view, update, policy, statuses, disabled }) {
  const lead = providerLabel(draft.provider)
  const model = view.config.model || 'its provider default'
  return <section className="a-nt-sec" aria-labelledby="nt-helpers">
    <div className="a-nt-sh"><h2 id="nt-helpers">Helpers</h2><span>Agents the lead brings in for parts of the work</span></div>
    <div className="a-swrow">
      <div><b id="nt-deleg">Let the lead bring in helpers</b>
        <small>{!view.canLead ? (draft.provider === 'mock' ? 'Mock simulates a single agent.' : 'Only Claude Code and Codex can lead helpers.')
          : view.helpers ? 'It decides whether helpers are worth it. It may also do everything itself.'
            : 'The lead does all the work itself. No helper creation is authorized.'}</small></div>
      <button type="button" role="switch" className="a-switch" aria-checked={view.helpers} aria-labelledby="nt-deleg"
        disabled={disabled || !view.canLead} onClick={() => update({ autonomous: !draft.autonomous })} />
    </div>
    {view.helpers && <div className="a-hcfg">
      <span className="a-lc-l" id="nt-alloc">Choosing each helper’s tool</span>
      <div className="a-acards" role="radiogroup" aria-labelledby="nt-alloc">
        {[['explicit', 'Follow the lead’s choices', 'The lead picks each helper’s tool and model.'],
          ['auto', 'Also allow automatic choice', 'When the lead leaves it open, the Control Center picks.']].map(([value, title, text]) =>
          <label key={value} className={`a-acard${draft.allocation === value ? ' is-on' : ''}`}>
            <input type="radio" name="nt-alloc" value={value} checked={draft.allocation === value} disabled={disabled}
              onChange={() => update({ allocation: value, ...(value === 'auto' && !draft.autoProviders.length ? { autoProviders: [draft.provider] } : {}) })} />
            <span><b>{title}</b><small>{text}</small></span></label>)}
      </div>
      {draft.allocation === 'explicit'
        ? <p className="a-expl">A helper with no tool named uses the provider the plan requests, or {lead}. If a named tool isn’t available, that helper waits;
          it’s never swapped for another tool.</p>
        : <>
          <p className="a-expl">The lead’s own choices still win. For a helper it leaves open, the Control Center picks one of the tools checked here that is allowed,
            ready and suited to that work. If none fits, the helper waits with an explanation. Running agents are never switched.</p>
          <div className="a-pchips" role="group" aria-label="Tools that may be picked automatically">
            {AUTO_PROVIDERS.map((provider) => {
              const state = toolState(provider, policy, statuses)
              const on = draft.autoProviders.includes(provider)
              return <label key={provider} className={`a-pchip${on ? ' is-on' : ''}${state.allowed ? '' : ' is-off'}`}>
                {/* A tool that lost its permission can still be cleared, never newly chosen. */}
                <input type="checkbox" name="helper-tools" value={provider} checked={on} disabled={disabled || (!state.allowed && !on)}
                  onChange={(event) => update({ autoProviders: event.target.checked ? [...draft.autoProviders, provider] : draft.autoProviders.filter((item) => item !== provider) })} />
                <span className="a-tool-mark a-tool-mark--sm" data-color={TOOL_COLOR[provider]} aria-hidden="true">{TOOLS[provider].mark}</span>
                {providerLabel(provider)}{state.allowed ? (state.usable ? '' : ' (not ready)') : ' (not allowed)'}
              </label>
            })}
          </div>
        </>}
      <p className="a-note-i"><Icon name="info" size={14} /><span>Helpers don’t inherit {model}{view.config.effort ? ` · ${view.config.effort}` : ''}: each helper’s
        model and effort come from the lead’s choice{draft.allocation === 'auto' ? ' or the automatic pick' : ''}. Limits and preferences for automatic choice can be
        set on the task after it starts.</span></p>
    </div>}
  </section>
}

// The "Before you start" card: a small picture, the plain summary, the usage note and Start.
export function StartCard({ draft, view, starting, onStart, onCancel, outcome }) {
  const lead = draft.provider === 'mock' ? 'Practice' : providerLabel(draft.provider)
  return <aside className="a-nt-side" aria-label="Before you start"><div className="a-card a-nt-card">
    <div className="a-card-head"><h2 className="a-card-title">Before you start</h2></div>
    <div className="a-card-body">
      <div className="a-ntpic" aria-hidden="true"><svg viewBox={`0 0 320 ${view.helpers ? 168 : 96}`} width="320" height={view.helpers ? 168 : 96}
        className="a-ntpic-svg" preserveAspectRatio="xMidYMid meet">
        <circle cx="160" cy="30" r="21" className={`a-pv-lead a-pv-${TOOL_COLOR[draft.provider]}`} />
        <text x="160" y="35" textAnchor="middle" className="a-pv-mark">{TOOLS[draft.provider].mark}</text>
        <text x="160" y="68" textAnchor="middle" className="a-pv-title">Lead · {lead}</text>
        <text x="160" y="84" textAnchor="middle" className="a-pv-sub">{draft.provider === 'mock' ? 'Simulated' : `${view.config.model || 'Default model'}${view.config.effort ? ` · ${view.config.effort}` : ''}`}</text>
        {view.helpers && <><path d="M160 92V100M100 100H220M100 100V115M160 100V115M220 100V115" className="a-pv-thread" />
          {[100, 160, 220].map((x) => <g key={x}><circle cx={x} cy="128" r="13" className="a-pv-slot" />
            <text x={x} y="132" textAnchor="middle" className="a-pv-plus">+</text>
            <text x={x} y="158" textAnchor="middle" className="a-pv-sub">{view.auto ? (draft.autoProviders.map((p) => TOOLS[p].mark).join(' / ') || '?') : 'lead picks'}</text></g>)}</>}
      </svg><p className="a-pv-cap">{view.helpers ? 'Helpers appear only if the lead brings them in.' : 'One agent, working alone.'}</p></div>
      <div className="a-summary" aria-live="polite">{view.summary.map((line) => <p key={line} className="a-bidi" dir="auto">{line}</p>)}</div>
      {view.helpers && <p className="a-note-i"><Icon name="bolt" size={14} /><span>Helpers and follow-up rounds use more of your plans, possibly on other
        provider accounts. Provider limits and billing still apply; paid usage settings live in each provider account and are not visible here.</span></p>}
      <div className="a-nt-acts">
        <button type="button" className="a-btn a-btn--ghost" onClick={onCancel} disabled={starting}>Cancel</button>
        <span className="a-sp" />
        <button type="button" className="a-btn a-btn--primary" onClick={onStart} disabled={starting || Boolean(view.problem)}
          aria-describedby="nt-why" aria-busy={starting || undefined}>{starting ? 'Starting…' : 'Start task'}<kbd className="a-kbd" aria-hidden="true">Ctrl ⏎</kbd></button>
      </div>
      <p className="a-pv-cap" id="nt-why" role="status">{starting ? 'Checking permissions and starting…' : view.problem || 'Ready to start.'}</p>
      {outcome}
    </div>
  </div></aside>
}
