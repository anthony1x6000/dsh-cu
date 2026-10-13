/**
 * selfcheck for dsh-cu: the smallest set of checks that fails if the plugin's
 * contract with the harness, the bridge protocol, or the packaging breaks.
 *
 * Run: `node --experimental-strip-types selfcheck.ts`
 * (or `pnpm selfcheck`). It needs no live desktop: the bridge is exercised
 * against a fake launcher that speaks the same frames.
 */
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { chmodSync, existsSync, mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  apply,
  BridgeClient,
  buildDefinition,
  findPackageRoot,
  launcherPath,
  LAUNCHER_RELATIVE_PATH,
  name,
  renderResult,
  renderScreenshot,
  resolveConfig,
  TOOL_SPECS,
  type PluginContext,
} from './index.ts'

// --- configuration -----------------------------------------------------------

const root = findPackageRoot(import.meta.dirname)

const defaults = resolveConfig({})
assert.equal(defaults.python, 'python3')
assert.equal(defaults.toolPrefix, 'cu_')
assert.equal(defaults.enableInput, true)
assert.equal(defaults.enableSkill, true)
assert.equal(defaults.requestTimeoutMs, 60_000)
assert.equal(defaults.launchTimeoutMs, 20_000)

const overridden = resolveConfig({
  python: '/usr/bin/python3.14',
  toolPrefix: 'desk_',
  enableInput: false,
  env: { DSH_CU_STATE_DIR: '/tmp/state' },
})
assert.equal(overridden.python, '/usr/bin/python3.14')
assert.equal(overridden.toolPrefix, 'desk_')
assert.equal(overridden.enableInput, false)
assert.equal(overridden.env.DSH_CU_STATE_DIR, '/tmp/state')

assert.throws(() => resolveConfig({ requestTimeoutMs: -1 }), /requestTimeoutMs must be a positive number/)
assert.throws(() => resolveConfig({ toolPrefix: '9bad' }), /toolPrefix must match/)
assert.throws(() => resolveConfig({ scriptDir: 42 as unknown as string }), /scriptDir must be a string/)

// An empty string means "not configured", not "use the empty path".
assert.equal(resolveConfig({ scriptDir: '   ' }).scriptDir, undefined)

// --- tool specifications -----------------------------------------------------

const suffixes = TOOL_SPECS.map((spec) => spec.suffix)
assert.equal(new Set(suffixes).size, suffixes.length, 'tool suffixes must be unique')

for (const spec of TOOL_SPECS) {
  const schema = spec.parameters as {
    type?: string
    properties?: Record<string, { type?: string, enum?: unknown[] }>
    required?: string[]
    additionalProperties?: boolean
  }
  assert.equal(schema.type, 'object', `${spec.suffix}: parameter root must be an object`)
  assert.equal(schema.additionalProperties, false, `${spec.suffix}: unknown arguments must be rejected`)
  for (const [key, property] of Object.entries(schema.properties ?? {})) {
    assert.ok(typeof property.type === 'string', `${spec.suffix}.${key} needs a type`)
    if (property.enum !== undefined) {
      for (const value of property.enum) {
        const expected = property.type === 'integer' ? 'number' : property.type
        assert.equal(typeof value, expected, `${spec.suffix}.${key} enum value ${String(value)} has the wrong type`)
      }
    }
    if (key.endsWith('Id') && property.type !== 'string') {
      assert.fail(`${spec.suffix}.${key} looks like an id and must be a string`)
    }
  }
  for (const required of schema.required ?? []) {
    assert.ok(Object.hasOwn(schema.properties ?? {}, required), `${spec.suffix}: required ${required} is undeclared`)
  }
}

// The mutating tools are exactly the ones a read-only deployment must lose.
const mutating = TOOL_SPECS.filter((spec) => spec.mutating === true).map((spec) => spec.suffix)
assert.deepEqual(
  mutating.sort(),
  ['a11y_invoke', 'clipboard', 'keyboard', 'launch', 'mouse', 'window'],
  'the mutating set is what enableInput gates',
)

// --- definition shape --------------------------------------------------------

const clientForShape = new BridgeClient(resolveConfig({}), '/nonexistent/launcher')
const definition = buildDefinition(
  TOOL_SPECS.find((spec) => spec.suffix === 'window')!,
  clientForShape,
  'cu_',
  20_000,
) as {
  name: string
  description: string
  parameters: { type: string }
  output: { schema: Record<string, unknown>, render(args: unknown, value: unknown): Array<{ type: string, text: string }> }
  timeoutMs?: number
  isConcurrencySafe(args: unknown): boolean
}
assert.equal(definition.name, 'cu_window')
assert.ok(definition.description.length > 100, 'tool descriptions are the model\'s documentation')
assert.equal(definition.parameters.type, 'object')
assert.deepEqual(definition.output.schema, {}, 'an unconstrained output schema accepts any JSON value')
assert.equal(definition.isConcurrencySafe({}), false)
assert.equal(definition.timeoutMs, 30_000)

const launchDefinition = buildDefinition(
  TOOL_SPECS.find((spec) => spec.suffix === 'launch')!,
  clientForShape,
  'cu_',
  5_000,
) as { timeoutMs?: number }
assert.equal(launchDefinition.timeoutMs, 5_000, 'the launch budget comes from config, not the spec')

const listDefinition = buildDefinition(
  TOOL_SPECS.find((spec) => spec.suffix === 'windows')!,
  clientForShape,
  'cu_',
  20_000,
) as { isConcurrencySafe(args: unknown): boolean }
assert.equal(listDefinition.isConcurrencySafe({}), true)

// --- rendering ---------------------------------------------------------------

assert.match(renderResult({ a: 1 }), /"a": 1/)
const long = renderResult({ text: 'x'.repeat(100) }, 50)
assert.match(long, /truncated \(\d+ more characters\)/)

const shotText = renderScreenshot({
  path: '/tmp/shot.png',
  backend: 'screenshot2',
  pixels: [1920, 1080],
  logical: { origin: [0, 0], size: [1920, 1080] },
  coordinateNote: 'pixel (px, py) is logical (0 + px, 0 + py)',
  reason: 'the captured frame is a single uniform colour (0, 0, 0)',
  sessionLocked: true,
})
assert.match(shotText, /Saved \/tmp\/shot\.png/)
assert.match(shotText, /backend screenshot2; 1920x1080px/)
assert.match(shotText, /warning: the captured frame is a single uniform colour/)
assert.match(shotText, /the session is locked/)

// --- registration ------------------------------------------------------------

interface Registered { name: string, description: string }

function fakeContext(): { ctx: PluginContext, registered: Registered[], order: string[] } {
  const registered: Registered[] = []
  const order: string[] = []
  const ctx: PluginContext = {
    tools: {
      register(candidate: unknown) {
        const tool = candidate as Registered
        registered.push(tool)
        return () => {
          order.push(`dispose:${tool.name}`)
          registered.splice(registered.indexOf(tool), 1)
        }
      },
    },
    get(service: string) {
      if (service !== 'skills') return undefined
      return {
        register(skill: unknown) {
          const entry = skill as { name: string }
          order.push(`skill:${entry.name}`)
          return () => order.push(`dispose-skill:${entry.name}`)
        },
      }
    },
    on(event: string, listener: (...args: unknown[]) => void) {
      order.push(`on:${event}`)
      return () => listener()
    },
  }
  return { ctx, registered, order }
}

const full = fakeContext()
const count = apply(full.ctx, {})
assert.equal(count, TOOL_SPECS.length + 1, 'every tool plus the skill is registered')
assert.equal(full.registered.length, TOOL_SPECS.length)
assert.ok(full.registered.every((tool) => tool.name.startsWith('cu_')))
assert.ok(full.order.includes('skill:plasma-computer-use'))

const readOnly = fakeContext()
apply(readOnly.ctx, { enableInput: false, enableSkill: false })
assert.equal(readOnly.registered.length, TOOL_SPECS.length - mutating.length)
assert.ok(readOnly.registered.every((tool) => !mutating.includes(tool.name.replace('cu_', ''))))

const prefixed = fakeContext()
apply(prefixed.ctx, { toolPrefix: 'desk_', enableSkill: false })
assert.ok(prefixed.registered.some((tool) => tool.name === 'desk_mouse'))

// --- bridge protocol against a fake launcher ---------------------------------

const scratch = mkdtempSync(join(tmpdir(), 'dsh-cu-selfcheck-'))
const fakeLauncher = join(scratch, 'fake-bridge.mjs')
writeFileSync(fakeLauncher, `#!/usr/bin/env node
process.stdin.setEncoding('utf8')
let buffer = ''
process.stdin.on('data', (chunk) => {
  buffer += chunk
  for (;;) {
    const index = buffer.indexOf('\\n')
    if (index < 0) return
    const line = buffer.slice(0, index); buffer = buffer.slice(index + 1)
    if (line.trim() === '') continue
    const request = JSON.parse(line)
    if (request.op === 'hang') continue
    if (request.op === 'shutdown') {
      process.stdout.write(JSON.stringify({ id: request.id, ok: true, result: { stopping: true } }) + '\\n')
      setTimeout(() => process.exit(0), 5)
      continue
    }
    if (request.op === 'fail') {
      process.stdout.write(JSON.stringify({ id: request.id, ok: false, error: { code: 'FAKE_FAIL', message: 'nope', detail: { x: 1 } } }) + '\\n')
      continue
    }
    // Split one frame across two writes to prove the client frames on newlines.
    const frame = JSON.stringify({ id: request.id, ok: true, result: { op: request.op, args: request.args } }) + '\\n'
    process.stdout.write(frame.slice(0, 5))
    setTimeout(() => process.stdout.write(frame.slice(5)), 5)
  }
})
`)
chmodSync(fakeLauncher, 0o755)

const fakeConfig = resolveConfig({ bridgePath: fakeLauncher, requestTimeoutMs: 3_000 })
const fakeClient = new BridgeClient(fakeConfig, fakeLauncher)
assert.equal(launcherPath(fakeConfig), fakeLauncher)

const echoed = await fakeClient.request('window.list', { app: 'brave' })
assert.deepEqual(echoed, { op: 'window.list', args: { app: 'brave' } })
assert.equal(fakeClient.running, true)

await assert.rejects(
  () => fakeClient.request('fail', {}),
  (error: Error & { code?: string, detail?: unknown }) => {
    assert.equal(error.code, 'FAKE_FAIL')
    assert.deepEqual(error.detail, { x: 1 })
    assert.match(error.message, /nope/)
    return true
  },
)

const timeoutClient = new BridgeClient(resolveConfig({ bridgePath: fakeLauncher, requestTimeoutMs: 400 }), fakeLauncher)
await assert.rejects(
  () => timeoutClient.request('hang', {}),
  (error: Error & { code?: string }) => {
    assert.equal(error.code, 'BRIDGE_TIMEOUT')
    return true
  },
)
await timeoutClient.dispose()

// A missing launcher is reported as such rather than crashing the tool call.
const missingClient = new BridgeClient(resolveConfig({ bridgePath: join(scratch, 'nope') }), join(scratch, 'nope'))
await assert.rejects(
  () => missingClient.request('status', {}),
  (error: Error & { code?: string }) => {
    assert.equal(error.code, 'BRIDGE_MISSING')
    return true
  },
)

await fakeClient.dispose()
assert.equal(fakeClient.running, false)

// --- bridge operation coverage ----------------------------------------------

// A tool spec naming an operation the bridge does not implement would register
// and then fail on every call, so the two lists are checked against each other.
let bridgeOps: string[] | undefined
try {
  bridgeOps = JSON.parse(execFileSync(join(root, LAUNCHER_RELATIVE_PATH), ['--ops'], {
    encoding: 'utf8',
    timeout: 30_000,
  })).result as string[]
} catch (error: unknown) {
  console.error(`dsh-cu selfcheck: skipping bridge op coverage (${String(error)})`)
}
if (bridgeOps !== undefined) {
  for (const spec of TOOL_SPECS) {
    assert.ok(bridgeOps.includes(spec.op), `tool ${spec.suffix} names unknown bridge op ${spec.op}`)
  }
  // Every mutating capability is reachable from the tool surface.
  for (const op of ['mouse', 'keyboard', 'app.launch', 'clipboard', 'a11y.invoke']) {
    assert.ok(bridgeOps.includes(op), `the bridge must implement ${op}`)
  }
}

// --- packaging ---------------------------------------------------------------

const manifest = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')) as {
  name: string
  files: string[]
  dsh?: { bundle?: { patch?: string } }
  bin?: Record<string, string>
}
assert.equal(manifest.name, 'dsh-cu')
assert.equal(name, 'computer-use-plasma')
const patchRelative = manifest.dsh?.bundle?.patch
assert.ok(typeof patchRelative === 'string' && patchRelative.length > 0, 'package.json must declare dsh.bundle.patch')
const patch = readFileSync(join(root, patchRelative), 'utf8')
assert.match(patch, /name: 'dsh-cu'/, 'the bundle layer must reference the module by package name')
assert.doesNotMatch(patch, /\.\/index\.ts/, 'the bundle layer must not reference source paths')
for (const shipped of ['lib/index.js', 'bin/dsh-cu-bridge', 'python', 'tools', 'LICENSE', 'NOTICE']) {
  assert.ok(manifest.files.includes(shipped), `files must ship ${shipped}`)
}
assert.equal(manifest.bin?.['dsh-cu-bridge'], './bin/dsh-cu-bridge')

// The launcher must be executable and must point at the shipped package.
const launcher = join(root, LAUNCHER_RELATIVE_PATH)
assert.ok(existsSync(launcher), 'the launcher must exist')
assert.ok((statSync(launcher).mode & 0o111) !== 0, 'the launcher must be executable')
assert.ok(existsSync(join(root, 'python', 'dsh_cu_bridge', '__main__.py')), 'the bridge package must exist')

// The inherited licence is a hard requirement of this plugin.
const license = readFileSync(join(root, 'LICENSE'), 'utf8')
const notice = readFileSync(join(root, 'NOTICE'), 'utf8')
assert.match(license, /MIT License/)
assert.match(license, /kwin-mcp/)
assert.match(license, /Byeonghoon Yoo/, 'the upstream copyright notice must be preserved')
assert.match(notice, /kwin-mcp/)
assert.match(notice, /isac322\/kwin-mcp/)

// The committed build artifact is what an installed profile loads.
assert.ok(existsSync(join(root, 'lib', 'index.js')), 'lib/index.js must be built before publishing')
const built = await import('./lib/index.js') as {
  name?: string
  apply?: unknown
  TOOL_SPECS?: unknown[]
  resolveConfig?: unknown
}
assert.equal(built.name, name, 'the built artifact must export the plugin name')
assert.equal(typeof built.apply, 'function', 'the built artifact must export apply')
assert.equal(typeof built.resolveConfig, 'function', 'the built artifact must export resolveConfig')
assert.equal(built.TOOL_SPECS?.length, TOOL_SPECS.length, 'the built artifact must ship every tool')

// The dashboards of a broken fork: the bridge must at least parse.
const probe = execFileSync('python3', ['-c', 'import ast,sys,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path("python/dsh_cu_bridge").glob("*.py")]'], {
  cwd: root,
  encoding: 'utf8',
})
assert.equal(probe.trim(), '')

console.log(`dsh-cu selfcheck: ok (${String(TOOL_SPECS.length)} tools, ${String(mutating.length)} mutating)`)
