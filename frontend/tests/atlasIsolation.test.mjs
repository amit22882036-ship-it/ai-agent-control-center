import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const atlasDir = fileURLToPath(new URL('../src/atlas/', import.meta.url))
const stylesDir = join(atlasDir, 'styles')
const fontsDir = join(atlasDir, 'fonts')

function files(dir, pattern) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => entry.isDirectory()
    ? files(join(dir, entry.name), pattern) : pattern.test(entry.name) ? [join(dir, entry.name)] : [])
}

// A small CSS reader: yields each style rule's selector list, each @font-face body and each
// @keyframes name, descending into @media and @supports blocks.
function cssRules(text) {
  const source = text.replace(/\/\*[\s\S]*?\*\//g, '')
  const result = []
  function walk(start, end) {
    let i = start
    while (i < end) {
      const open = source.indexOf('{', i)
      if (open === -1 || open >= end) return
      const prelude = source.slice(i, open).trim()
      let depth = 1
      let j = open + 1
      while (depth && j < end) { if (source[j] === '{') depth++; else if (source[j] === '}') depth--; j++ }
      const body = source.slice(open + 1, j - 1)
      // Conditional groups (media, support and container queries) hold ordinary rules: check those.
      if (/^@(media|supports|container)\b/.test(prelude)) walk(open + 1, j - 1)
      else if (prelude.startsWith('@font-face')) result.push({ kind: 'font-face', body })
      else if (prelude.startsWith('@keyframes')) result.push({ kind: 'keyframes', name: prelude.split(/\s+/)[1] })
      else result.push({ kind: 'rule', selectors: prelude.split(',').map((selector) => selector.trim()) })
      i = j
    }
  }
  walk(0, source.length)
  return result
}

test('every Atlas style rule is scoped to the .atlas root, so the classic interface is unaffected', () => {
  const sheets = files(stylesDir, /\.css$/)
  assert.ok(sheets.length >= 5)
  let checked = 0
  for (const sheet of sheets) {
    for (const rule of cssRules(readFileSync(sheet, 'utf8'))) {
      if (rule.kind === 'rule') {
        for (const selector of rule.selectors) {
          assert.match(selector, /\.atlas\b/, `${sheet}: "${selector}" is not scoped to .atlas`)
          checked++
        }
      } else if (rule.kind === 'keyframes') {
        assert.match(rule.name, /^atlas-/, `${sheet}: keyframes "${rule.name}" needs the atlas- prefix`)
      } else {
        assert.match(rule.body, /font-family:\s*'Atlas /, `${sheet}: font families must use an Atlas-only name`)
        assert.match(rule.body, /src:\s*url\('\.\.\/fonts\/[a-z-]+\.woff2'\)/, `${sheet}: fonts must be local files`)
      }
    }
  }
  assert.ok(checked > 200, `only ${checked} selectors were checked`)
})

test('Atlas styles and code make no request to a remote host', () => {
  for (const file of [...files(stylesDir, /\.css$/), ...files(atlasDir, /\.(jsx?|mjs)$/)]) {
    const text = readFileSync(file, 'utf8')
    assert.doesNotMatch(text, /https?:\/\/(?!127\.0\.0\.1)/, `${file} references a remote URL`)
  }
})

test('every local font has its license and every referenced font file exists', () => {
  const css = readFileSync(join(stylesDir, 'tokens.css'), 'utf8')
  const referenced = [...css.matchAll(/url\('\.\.\/fonts\/([a-z-]+\.woff2)'\)/g)].map((match) => match[1])
  assert.ok(referenced.length >= 5)
  for (const font of referenced) {
    const bytes = readFileSync(join(fontsDir, font))
    assert.equal(bytes.subarray(0, 4).toString('latin1'), 'wOF2', `${font} is not a WOFF2 file`)
    const family = font.replace(/-(latin|latin-ext|hebrew)\.woff2$/, '')
    const license = readFileSync(join(fontsDir, `${family}-OFL.txt`), 'utf8')
    assert.match(license, /SIL OPEN FONT LICENSE Version 1\.1/)
  }
  assert.ok(existsSync(join(fontsDir, 'README.md')))
})

test('Atlas uses the shared event connection once and changes state only through its action client', () => {
  const code = files(atlasDir, /\.(jsx?|mjs)$/).map((file) => [file, readFileSync(file, 'utf8')])
  const connections = code.filter(([, text]) => text.includes('connectAgentEvents('))
  assert.equal(connections.length, 1, 'exactly one module opens the event connection')
  assert.equal((connections[0][1].match(/connectAgentEvents\(/g) || []).length, 1)
  for (const [file, text] of code) {
    assert.doesNotMatch(text, /new EventSource\(/, `${file} must reuse connectAgentEvents`)
    // Since M4, agent actions exist; every one goes through api/agentClient.mjs and apiFetch (CSRF).
    if (!file.endsWith(join('api', 'agentClient.mjs'))) {
      assert.doesNotMatch(text, /(POST|PUT|PATCH|DELETE)['"`]/, `${file} sends a state-changing request outside the action client`)
    }
    // A plain fragment link ("#section") would leave Atlas; routes are built with formatHash().
    assert.doesNotMatch(text, /href="#/, `${file} uses a fragment link`)
  }
})

test('the action client is the only state-changing module and always uses the CSRF-aware apiFetch', () => {
  const client = readFileSync(join(atlasDir, 'api', 'agentClient.mjs'), 'utf8')
  assert.match(client, /import \{ API_BASE, apiFetch \} from '\.\.\/\.\.\/apiClient\.mjs'/)
  assert.doesNotMatch(client, /fetch\(/, 'no bare fetch in the action client')
  assert.match(client, /fetcher = apiFetch/)
})
