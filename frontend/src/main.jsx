import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import UiRoot from './atlas/UiRoot.jsx'

// The classic interface is the default; the Atlas preview renders only for Atlas addresses (#/...).
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <UiRoot classic={App} />
  </StrictMode>,
)
