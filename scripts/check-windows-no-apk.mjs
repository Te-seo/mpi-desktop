import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const args = process.argv.slice(2)
if (!((args.length === 1 && ['--notices', '--check'].includes(args[0])) ||
      (args.length === 2 && args[0] === '--installer'))) throw new Error('Invalid no-APK Windows release check')
const env = { ...process.env }
for (const k of Object.keys(env)) {
  if (['LICENSES_ALLOW_BLOCKERS', 'MPI_NOTICES_OUT'].includes(k.toUpperCase())) delete env[k]
}
const scratch = mkdtempSync(join(tmpdir(), 'mpi-windows-no-apk-'))
const run = (program, command) => execFileSync(program, command, { cwd: repo, env, stdio: 'inherit' })
try {
  const manifest = join(scratch, 'inventory.json')
  run(process.execPath, ['scripts/gen-licenses.mjs', '--windows-no-apk', manifest])
  const actual = JSON.parse(readFileSync(manifest, 'utf8')).ecosystems
  const expected = JSON.parse(readFileSync(join(repo, 'licenses', 'third-party.json'), 'utf8')).ecosystems
    .filter(e => ['npm-desktop', 'python'].includes(e.id))
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error('Windows npm/Python license inventory is stale')
  run('python', ['scripts/inspect_windows_no_apk.py', '--directory', join(repo, 'frontend', 'resources')])
  const engine = join(repo, 'dist', 'data-engine.exe')
  const engineNotices = join(repo, 'dist', 'ENGINE-RUNTIME-NOTICES.txt')
  const receipt = join(repo, 'dist', 'engine-runtime-receipt.json')
  const source = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repo, env, encoding: 'utf8' }).trim()
  if (!/^[a-f0-9]{40}$/.test(source)) throw new Error('Cannot resolve exact Windows source commit')
  run('python', ['scripts/build-windows-engine.py', 'verify', '--source-commit', source,
    '--engine', engine, '--receipt', receipt, '--notices', engineNotices])
  if (args[0] === '--check') process.exitCode = 0
  else {
    const notices = args[0] === '--notices' ? join(repo, 'frontend', 'THIRD-PARTY-NOTICES.txt') : join(scratch, 'notices.txt')
    run(process.execPath, ['frontend/scripts/gen-notices.mjs', '--windows-no-apk', manifest, notices])
    if (args[0] === '--installer') {
      run('python', ['scripts/inspect_windows_no_apk.py', '--installer', resolve(args[1]),
        '--notices', notices, '--license', join(repo, 'LICENSE'), '--engine', engine, '--engine-notices', engineNotices])
    }
  }
} finally {
  rmSync(scratch, { recursive: true, force: true })
}
