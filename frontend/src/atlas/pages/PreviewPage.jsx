import PageHeader from '../shell/PageHeader'
import { Button, Card, Chip } from '../components/primitives'
import { exitAtlas } from '../uiMode.mjs'

// Screens that later milestones build. Each says what it will hold and where the same work is
// done today, instead of showing controls that do not work yet.
const PREVIEWS = {
  projects: {
    title: 'Projects', subtitle: 'Repositories your agents work in.', milestone: 'Arrives with New Task (M5)',
    plan: [
      ['Your registered projects', 'Each project with its active tasks and the agents working in it.'],
      ['Add a project folder', 'Register a Git repository by its path, with the same checks the Control Center runs today.'],
    ],
    classic: 'In Classic view, choose New agent, then Add existing project. Registered projects appear in the Project list there.',
  },
  connections: {
    title: 'Connections', subtitle: 'The AI tools on this computer that your agents use, with your own sign-in.', milestone: 'Arrives in M8',
    plan: [
      ['Ready for tasks', 'Installation, sign-in, permission and billing route for each tool.'],
      ['Usage limits', 'Only what each tool reports; nothing is estimated.'],
      ['Paid usage beyond your plan', 'Where to check extra usage, credits or top-ups in your provider account.'],
    ],
    classic: 'In Classic view, open AI Providers to install, sign in, allow, update or check a tool and to see its reported usage.',
  },
  new: {
    title: 'New task', subtitle: 'Describe what you want done.', milestone: 'Arrives in M5',
    plan: [
      ['The lead agent', 'Its tool, model, reasoning effort and file access, always visible.'],
      ['Helpers', 'Whether the lead may bring in helpers, and how each helper’s tool is chosen.'],
      ['Before you start', 'A plain summary of what will happen, next to the Start button.'],
    ],
    classic: 'In Classic view, choose New agent. Everything you can start there today keeps working.',
  },
}

export default function PreviewPage({ kind }) {
  const page = PREVIEWS[kind]
  return <div className="a-page">
    <PageHeader title={page.title} subtitle={page.subtitle} />
    <div className="a-preview">
      <Card title="What this page will hold" aside={<Chip tone="accent">{page.milestone}</Chip>}>
        <ol className="a-preview-list">{page.plan.map(([title, text], index) => <li key={title} className="a-preview-item">
          <span className="a-preview-num" aria-hidden="true">{index + 1}</span><span><b>{title}</b>{text}</span></li>)}</ol>
      </Card>
      <Card title="Until then">
        <p className="a-preview-classic">{page.classic}</p>
        <Button variant="primary" icon="swap" onClick={() => exitAtlas()}>Open Classic view</Button>
      </Card>
    </div>
  </div>
}
