import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import UiRoot from './atlas/UiRoot.jsx'

// The Lumen interface is the default; the classic interface renders only for addresses marked ui=classic.
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <UiRoot classic={App} />
  </StrictMode>,
)
