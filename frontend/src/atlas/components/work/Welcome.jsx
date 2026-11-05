import { useState } from 'react'
import { Button } from '../primitives'
import Icon from '../Icon'
import { RegisterProjectDialog } from '../create/projects'

// The first thing a new installation shows: three steps, each with its own action, and which of
// them is already done. It replaces the empty Work page until the first task exists.
export default function Welcome({ directory, navigate, onRefreshProjects }) {
  const [adding, setAdding] = useState(false)
  const hasProject = directory.projects.length > 0
  const steps = [
    { key: 'tools', icon: 'plug', title: 'Check your AI tools',
      text: 'Claude Code, Codex and Gemini CLI run on your own account. Connections shows which are ready; the Mock tool needs nothing and uses no model.',
      action: <Button size="sm" icon="plug" onClick={() => navigate('connections')}>Open Connections</Button> },
    { key: 'project', icon: 'folder', title: 'Add a project', done: hasProject,
      text: hasProject ? `${directory.projects.length === 1 ? 'One project is' : `${directory.projects.length} projects are`} ready. Agents work in private copies; your folder is never edited directly.`
        : 'Any Git repository with at least one commit. Agents work in private copies of it, never in your folder.',
      action: <Button size="sm" variant={hasProject ? 'secondary' : 'primary'} icon="plus" onClick={() => setAdding(true)}>{hasProject ? 'Add another' : 'Add a project'}</Button> },
    { key: 'task', icon: 'spark', title: 'Describe the work',
      text: 'Write what you want in plain language, pick a tool and how much it may do. You can watch, answer, redirect or stop it at any time.',
      action: <Button size="sm" variant={hasProject ? 'primary' : 'secondary'} icon="plus" disabled={!hasProject} onClick={() => navigate('new')}>New task</Button> },
  ]
  return <section className="a-welcome" aria-labelledby="welcome-title">
    <div className="a-welcome-head">
      <span className="a-welcome-mark" aria-hidden="true"><Icon name="spark" size={22} /></span>
      <h2 id="welcome-title" className="a-welcome-title">Welcome to the Control Center</h2>
      <p className="a-welcome-lead">One place to start AI coding agents on your projects, see what each one is doing, and step in when they need you.</p>
    </div>
    <ol className="a-welcome-steps">
      {steps.map((step, index) => <li key={step.key} className={`a-welcome-step${step.done ? ' is-done' : ''}`}>
        <span className="a-welcome-num" aria-hidden="true">{step.done ? <Icon name="check" size={15} stroke={2.4} /> : index + 1}</span>
        <div className="a-welcome-body">
          <h3>{step.title}{step.done && <span className="a-sr"> (done)</span>}</h3>
          <p>{step.text}</p>
          <div className="a-welcome-act">{step.action}</div>
        </div>
      </li>)}
    </ol>
    {adding && <RegisterProjectDialog open onClose={() => setAdding(false)} onRefresh={onRefreshProjects}
      onRegistered={() => { setAdding(false); onRefreshProjects?.() }} />}
  </section>
}
