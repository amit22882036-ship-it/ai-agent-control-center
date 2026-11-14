import { SPLIT_POLICIES } from '../../model/planPhase.mjs'
import Icon from '../Icon'
import { AUTO_PROVIDERS } from '../../../allocationPresentation.mjs'
import { providerLabel } from '../../../providerPresentation.mjs'
import { HELPER_EFFORTS, toolState } from '../../model/newTask.mjs'
import { PlainSelect } from '../agent/fields'
import { TOOLS } from '../../vocabulary.mjs'

const TOOL_COLOR = { claude: 'blue', codex: 'green', gemini: 'yellow', mock: 'neutral' }

// Helpers: optional, only for a Claude Code or Codex lead, and only the two real policies.
export function HelpersSection({ draft, view, update, policy, statuses, disabled, catalog = null }) {
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
      <span className="a-lc-l" id="nt-split">How the plan becomes work</span>
      <div className="a-acards" role="radiogroup" aria-labelledby="nt-split">
        {SPLIT_POLICIES.map((item) => <label key={item.value} className={`a-acard${(draft.splitPolicy || 'lead_decides') === item.value ? ' is-on' : ''}`}>
          <input type="radio" name="nt-split" value={item.value} checked={(draft.splitPolicy || 'lead_decides') === item.value} disabled={disabled}
            onChange={() => update({ splitPolicy: item.value })} />
          <span><b>{item.title}</b><small>{item.text}</small></span></label>)}
      </div>
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
      <span className="a-lc-l" id="nt-hlimit">Helpers’ model and effort</span>
      <p className="a-expl">Each helper uses {model}{view.config.effort ? ` · ${view.config.effort}` : ''} unless the lead picks another model or effort for
        that part (lighter for simple work, stronger for hard parts) and says why. Your limit always applies.</p>
      <div className="a-hlimit" role="group" aria-labelledby="nt-hlimit">
        <PlainSelect label="Most effort a helper may use" value={draft.helperMaxEffort || 'lead'} disabled={disabled}
          onChange={(helperMaxEffort) => update({ helperMaxEffort })}
          options={[{ value: 'lead', label: `Same as the lead${view.config.effort ? ` (${view.config.effort})` : ''}` },
            ...HELPER_EFFORTS.map((level) => ({ value: level, label: level[0].toUpperCase() + level.slice(1) })),
            { value: 'none', label: 'No limit' }]} />
        {draft.provider !== 'gemini' && (catalog?.models || []).some((item) => item.selectable) && <div className="a-pchips" role="group" aria-label="Models helpers may use (none checked: any listed model)">
          {(catalog?.models || []).filter((item) => item.selectable).map((item) => {
            const on = (draft.helperModels || []).includes(item.model_id)
            return <label key={item.model_id} className={`a-pchip${on ? ' is-on' : ''}`}>
              <input type="checkbox" name="helper-models" value={item.model_id} checked={on} disabled={disabled}
                onChange={(event) => update({ helperModels: event.target.checked ? [...(draft.helperModels || []), item.model_id] : (draft.helperModels || []).filter((id) => id !== item.model_id) })} />
              {item.display_name || item.model_id}</label>
          })}
        </div>}
        <small className="a-fine">Models checked here are the only ones helpers may use; none checked allows any listed model. A higher effort is lowered to your limit, and a model outside it falls back to the lead’s.</small>
      </div>
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
