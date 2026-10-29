import PageHeader from '../shell/PageHeader'
import { Button, EmptyState } from '../components/primitives'

export default function NotFoundPage({ requested, navigate }) {
  return <div className="a-page">
    <PageHeader title="Page not found" />
    <EmptyState icon="info" title={`There is no Atlas page called “${requested}”`}
      actions={<Button variant="primary" onClick={() => navigate('work')}>Go to Work</Button>}>
      The address may be from a later version, or it was typed by hand.
    </EmptyState>
  </div>
}
