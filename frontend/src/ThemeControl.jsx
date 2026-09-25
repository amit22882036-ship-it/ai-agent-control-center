import { useEffect, useState } from 'react'
import { readTheme, connectTheme, themeModes } from './theme.mjs'
import Icon from './Icon'
import Popover from './Popover'
export default function ThemeControl() {
  const [mode, setMode] = useState(() => readTheme())
  useEffect(() => connectTheme(mode, document.documentElement, window.matchMedia('(prefers-color-scheme: dark)')), [mode])
  return <Popover label="Appearance" trigger={<><Icon name={{ automatic: 'monitor', light: 'sun', dark: 'moon' }[mode]} /><span className="appearance-label">Appearance</span></>}>
    <fieldset className="choice-group"><legend>Appearance</legend>
      {themeModes.map((value) => <label className="choice" key={value}>
        <Icon name={{ automatic: 'monitor', light: 'sun', dark: 'moon' }[value]} />
        <span>{value[0].toUpperCase() + value.slice(1)}</span>
        <input type="radio" name="theme" value={value} checked={mode === value} onChange={() => {
          setMode(value)
          try { localStorage.setItem('control-center.theme', value) } catch { /* Session preference still works. */ }
        }} />
      </label>)}
    </fieldset>
  </Popover>
}
