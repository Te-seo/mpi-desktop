import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function fixture(t, script) {
  const root = mkdtempSync(join(tmpdir(), 'mpi-license-safety-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const target = join(root, script)
  mkdirSync(dirname(target), { recursive: true })
  copyFileSync(join(repoRoot, script), target)
  return { root, target }
}

function isolatedEnv(value) {
  const env = { ...process.env }
  // No ambient override or system package scanner may affect these synthetic
  // fixtures. Node is invoked by its absolute path; no packages are installed.
  for (const key of Object.keys(env)) {
    if (['PATH', 'LICENSES_ALLOW_BLOCKERS', 'MPI_NOTICES_OUT'].includes(key.toUpperCase())) delete env[key]
  }
  env.PATH = ''
  if (value !== undefined) env.LICENSES_ALLOW_BLOCKERS = value
  return env
}

for (const script of ['scripts/gen-licenses.mjs']) {
  for (const value of [undefined, '', '0', 'false', 'true', 'yes', '01', ' 1', '1 ']) {
    test(`${script} rejects an unknown dependency with override ${JSON.stringify(value)}`, (t) => {
      const { root, target } = fixture(t, script)
      const gradle = join(root, 'android-companion', 'app', 'build.gradle.kts')
      mkdirSync(dirname(gradle), { recursive: true })
      writeFileSync(gradle, 'dependencies { implementation("example.unapproved:sample-sdk:1.0.0") }\n')
      const result = spawnSync(process.execPath, [target], { cwd: root, env: isolatedEnv(value), encoding: 'utf-8' })
      assert.equal(result.error, undefined)
      assert.equal(result.status, 1, result.stdout + result.stderr)
      assert.match(result.stderr, /example\.unapproved:sample-sdk@1\.0\.0.*\[unknown\]/)
      assert.doesNotMatch(result.stdout + result.stderr, /continuing despite blockers/)
    })
  }
}

for (const script of ['scripts/gen-licenses.mjs']) {
  test(`${script} keeps unapproved Play Billing 9.1.0 blocked`, (t) => {
    const { root, target } = fixture(t, script)
    const gradle = join(root, 'android-companion', 'app', 'build.gradle.kts')
    mkdirSync(dirname(gradle), { recursive: true })
    writeFileSync(gradle, 'dependencies { implementation("com.android.billingclient:billing:9.1.0") }\n')
    const result = spawnSync(process.execPath, [target], { cwd: root, env: isolatedEnv(), encoding: 'utf-8' })
    assert.equal(result.error, undefined)
    assert.equal(result.status, 1, result.stdout + result.stderr)
    assert.match(result.stderr, /com\.android\.billingclient:billing@9\.1\.0.*\[unknown\]/)
    assert.doesNotMatch(result.stdout + result.stderr, /continuing despite blockers/)
  })
}

for (const script of ['scripts/gen-licenses.mjs']) {
  test(`${script} recognizes only the reviewed Bouncy Castle coordinate and version`, (t) => {
    const { root, target } = fixture(t, script)
    const gradle = join(root, 'android-companion', 'app', 'build.gradle.kts')
    mkdirSync(dirname(gradle), { recursive: true })
    for (const [coordinate, expected] of [
      ['org.bouncycastle:bcpkix-jdk15to18:1.84', 0],
      ['org.bouncycastle:bcpkix-jdk15to18:1.85', 1],
      ['org.bouncycastle:unreviewed-artifact:1.84', 1],
    ]) {
      writeFileSync(gradle, `dependencies { implementation("${coordinate}") }\n`)
      const result = spawnSync(process.execPath, [target], { cwd: root, env: isolatedEnv(), encoding: 'utf-8' })
      assert.equal(result.error, undefined)
      assert.equal(result.status, expected, result.stdout + result.stderr)
      if (expected === 1) assert.match(result.stderr, /\[unknown\]/)
    }
  })
}

function generateAndroidNotice(t, entries) {
  const { root, target } = fixture(t, 'frontend/scripts/gen-notices.mjs')
  const manifest = join(root, 'licenses', 'third-party.json')
  mkdirSync(dirname(manifest), { recursive: true })
  writeFileSync(manifest, JSON.stringify({ ecosystems: [{ id: 'gradle-android', entries }] }))
  writeFileSync(join(root, 'LICENSE'), 'Synthetic first-party license fixture')
  const result = spawnSync(process.execPath, [target], { cwd: root, env: isolatedEnv(), encoding: 'utf-8' })
  assert.equal(result.error, undefined)
  assert.equal(result.status, 0, result.stdout + result.stderr)
  return readFileSync(join(root, 'frontend', 'THIRD-PARTY-NOTICES.txt'), 'utf-8')
}

const apache = { name: 'androidx.core:core-ktx', version: '1.13.1', license: 'Apache-2.0', repository: 'https://developer.android.com/jetpack/androidx' }
const mit = { name: 'example:mit-library', version: '1.0.0', license: 'MIT', repository: 'https://example.invalid/mit-library' }
const sdk = { name: 'com.android.billingclient:billing', version: '9.1.0', license: 'Android Software Development Kit License Agreement', repository: 'https://developer.android.com/studio/terms' }

test('mixed Android licenses preserve each attribution without blanket Apache or GPL approval', (t) => {
  const text = generateAndroidNotice(t, [apache, mit, sdk])
  for (const entry of [apache, mit, sdk]) {
    assert.ok(text.includes(`${entry.name}@${entry.version}  —  ${entry.license}`))
    assert.ok(text.includes(entry.repository))
  }
  assert.match(text, /applies only to components explicitly marked Apache-2\.0/)
  assert.match(text, /does not grant distribution rights or establish GPL compatibility/)
  assert.doesNotMatch(text, /all licensed under|applies to all Android components|All bundled components are license-compatible/)
})

test('non-Apache-only Android notices do not attach Apache terms or unrelated holders', (t) => {
  const text = generateAndroidNotice(t, [mit, sdk])
  assert.doesNotMatch(text, /Apache License 2\.0 \(applies|Version 2\.0, January 2004|Copyright \(c\) The Android Open Source Project|Copyright \(c\) JetBrains/)
  assert.match(text, /Other components remain subject to their own identified licenses/)
})

test('Apache-only Android notices retain the full Apache text and relevant copyright', (t) => {
  const text = generateAndroidNotice(t, [apache])
  assert.match(text, /Version 2\.0, January 2004/)
  assert.match(text, /Copyright \(c\) The Android Open Source Project/)
  assert.doesNotMatch(text, /Copyright \(c\) JetBrains/)
})

test('empty Android manifest does not invent component licensing', (t) => {
  const text = generateAndroidNotice(t, [])
  assert.match(text, /ANDROID COMPANION \(Gradle\) — 0 components/)
  assert.doesNotMatch(text, /Version 2\.0, January 2004|all licensed under/)
})
