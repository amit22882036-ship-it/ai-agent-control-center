import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import UiRoot from './atlas/UiRoot.jsx'

// One interface: Lumen (its code is still named Atlas).
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <UiRoot />
  </StrictMode>,
)
