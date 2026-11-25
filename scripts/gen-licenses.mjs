// Generate the OSS license manifest used by desktop release notices.
//   node scripts/gen-licenses.mjs
// Scans three shipped ecosystems — desktop npm (frontend/), Python
// (pip-licenses over the engine env), and the Android companion — tags copyleft
// licenses, and writes licenses/third-party.json as
// { ecosystems: [{ id, label, entries }] }.
//
// Resilient by design: each scan is independent and, if it fails or yields
// nothing (missing tool, no network, node_modules absent in CI), falls back to
// the previously committed entries for that ecosystem so a build never wipes the
// notice. Run automatically before the desktop build via its `prebuild` script.
import { execFileSync, execSync } from 'node:child_process'
import { writeFileSync, mkdirSync, readFileSync, existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// npx resolves to a .cmd shim on Windows, which Node refuses to spawn without a
// shell — so license-checker runs through execSync (shell) with a quoted path.
const NPX = process.platform === 'win32' ? 'npx.cmd' : 'npx'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..')
const frontendRoot = resolve(repoRoot, 'frontend')
const windowsNoApk = process.argv[2] === '--windows-no-apk'
if (windowsNoApk && !process.argv[3]) throw new Error('Windows scan requires an isolated output path')
const rootManifest = resolve(repoRoot, 'licenses/third-party.json')
const outPath = windowsNoApk ? resolve(process.argv[3]) : rootManifest
if (windowsNoApk && outPath === rootManifest) throw new Error('Windows scan cannot replace the root release manifest')

// Our own workspace packages (private, no license field) are reported as UNLICENSED
// by license-checker. They are first-party, not third-party OSS, so they must never
// appear in this notice — drop them by name wherever they show up.
function pkgName(dir) {
  try {
    return JSON.parse(readFileSync(resolve(dir, 'package.json'), 'utf-8')).name || null
  } catch {
    return null
  }
}
const FIRST_PARTY = new Set([pkgName(frontendRoot)].filter(Boolean))

// Recognized permissive, commercially-distributable licenses — SPDX ids plus a few
// free-text names that pip-licenses emits, uppercased. `permissive` is an explicit
// ALLOWLIST: anything we don't recognize falls through to `unknown` so a novel
// source-available / non-commercial license (BUSL, PolyForm, CC-BY-NC, Elastic, …)
// is never silently reported as commercially distributable.
const PERMISSIVE = new Set([
  'MIT', 'MIT-0', 'MIT-CMU', 'X11',
  'ISC',
  '0BSD', 'BSD', 'BSD-2-CLAUSE', 'BSD-3-CLAUSE', 'BSD-3-CLAUSE-CLEAR',
  'APACHE-2.0', 'APACHE-1.1',
  'BLUEOAK-1.0.0',
  'BSL-1.0', // Boost Software License — NOT BUSL/Business Source
  'ZLIB', 'LIBPNG', 'NTP', 'JSON',
  'UNLICENSE', 'WTFPL',
  'CC0-1.0', 'CC-BY-4.0', 'CC-BY-3.0',
  'PYTHON-2.0', 'PSF-2.0', 'PSF',
])

// Free-text license names → a canonical token the allowlist / copyleft tests expect.
const NAME_ALIASES = [
  [/PYTHON SOFTWARE FOUNDATION.*/, 'PSF'],
  [/\bMIT LICENSE\b/, 'MIT'],
  [/\bAPACHE (?:SOFTWARE )?LICENSE\b/, 'APACHE-2.0'],
  [/\bBSD LICENSE\b/, 'BSD'],
  [/GNU AFFERO[^,;]*/, 'AGPL'],
  [/GNU LESSER[^,;]*/, 'LGPL'],
  [/GNU GENERAL PUBLIC[^,;]*/, 'GPL'],
  [/MOZILLA PUBLIC[^,;]*/, 'MPL'],
]

// License -> compliance category, graded by the MOST restrictive term in a
// compound expression ("Apache-2.0 AND LGPL-3.0-or-later" → weak-copyleft):
//   permissive      on the PERMISSIVE allowlist — attribution only.
//   weak-copyleft   LGPL/MPL/EPL/CDDL/MS-RL — fine in a proprietary product as long
//                   as the component stays dynamically linked/replaceable + a source
//                   offer is provided for that component.
//   strong-copyleft GPL/AGPL/SSPL/OSL/EUPL/CeCILL — blocks proprietary distribution.
//   proprietary     UNLICENSED / "SEE LICENSE IN …" third-party packages.
//   unknown         missing/garbled OR unrecognized (not on the allowlist) — manual look.
function classify(license) {
  const s = String(license ?? '').trim()
  if (!s || /^unknown$/i.test(s)) return 'unknown'
  if (/^unlicensed$/i.test(s) || /SEE LICENSE/i.test(s)) return 'proprietary'
  let up = s.toUpperCase()
  for (const [re, canon] of NAME_ALIASES) up = up.replace(re, canon)
  // Copyleft graded over the whole expression. GPL but not LGPL/AGPL via lookbehind.
  if (/\bAGPL|\bSSPL|\bOSL-|\bEUPL|\bCECILL|\bPARITY/.test(up) || /(?<![AL])GPL/.test(up)) return 'strong-copyleft'
  if (/\bLGPL|\bMPL|\bEPL|\bCDDL|\bMS-RL/.test(up)) return 'weak-copyleft'
  // Permissive ONLY if every component of the expression is on the allowlist.
  const tokens = up.split(/\s+(?:AND|OR|WITH)\s+|[()+,/]/).map((t) => t.trim()).filter(Boolean)
  if (tokens.length > 0 && tokens.every((t) => PERMISSIVE.has(t))) return 'permissive'
  return 'unknown'
}

// The project CORE (MPI desktop + Python engine) is distributed under GPL-3.0
// (root LICENSE). In a copyleft work, a copyleft dependency is only a problem if
// it CANNOT be combined into GPL-3.0. This decides whether a strong-copyleft dep
// is compatible (fine — the GPL-3.0 source offer already covers it) or a blocker
// (needs review). GPL-3.0/AGPL-3.0/LGPL-3.0 and GPL-2.0-or-later combine into a
// GPL-3.0 work; GPL-2.0-only, SSPL, OSL, EUPL, CeCILL, Parity do NOT (auto).
function gplCompatibleCopyleft(license) {
  const up = String(license ?? '').toUpperCase()
  if (/\bSSPL|\bOSL-|\bEUPL|\bCECILL|\bPARITY/.test(up)) return false
  if (/GPL-2\.0-ONLY|GPLV2(?!\s*OR)/.test(up)) return false
  if (/AGPL-3|\bGPL-3|LGPL-3|GPL-3\.0/.test(up)) return true
  if (/GPL-2\.0-OR-LATER|GPL-2\.0\+/.test(up)) return true
  // Bare "LGPL"/"MPL"/"GPL-…-or-later" without a hostile marker → assume combinable.
  if (/\bL?GPL\b|GPL-.*OR-LATER/.test(up)) return true
  return false
}

const entry = (name, version, license, repository) => ({
  name,
  version: version ?? '',
  license: license || 'UNKNOWN',
  repository: repository || null,
  category: classify(license),
})

/** Scan an npm project with license-checker; returns [] on any failure. */
function scanNpm(startDir, label) {
  if (!existsSync(resolve(startDir, 'node_modules'))) {
    console.warn(`[licenses] ${label}: node_modules missing at ${startDir}, skipping`)
    return []
  }
  try {
    const raw = execSync(
      `${NPX} --yes license-checker --production --json --start "${startDir}"`,
      { encoding: 'utf-8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] }
    )
    const data = JSON.parse(raw)
    return Object.entries(data)
      .map(([key, v]) => {
        const at = key.lastIndexOf('@')
        return entry(key.slice(0, at), key.slice(at + 1), v.licenses, v.repository)
      })
      .sort((a, b) => a.name.localeCompare(b.name))
  } catch (err) {
    console.warn(`[licenses] ${label}: scan failed (${err.message.split('\n')[0]})`)
    return []
  }
}

// --- Android / Gradle -------------------------------------------------------
// The companion.apk (android-companion/) is bundled inside the desktop installer
// via electron-builder extraResources, so its OSS deps create the same
// attribution obligation as the npm/Python ecosystems. There is no lightweight,
// network-free "license-checker for Gradle", so we resolve licenses from a
// curated map of the coordinate GROUP (every AndroidX/Jetpack/Kotlin artifact is
// Apache-2.0). Anything not in the map resolves to UNKNOWN → classify() flags it,
// so a future non-Apache Gradle dep can never be silently reported as permissive.
const GRADLE_GROUP_LICENSE = [
  [/^androidx\./, { license: 'Apache-2.0', repository: 'https://developer.android.com/jetpack/androidx' }],
  [/^com\.google\.android\.material$/, { license: 'Apache-2.0', repository: 'https://github.com/material-components/material-components-android' }],
  [/^com\.google\.android\./, { license: 'Apache-2.0', repository: 'https://developers.google.com/android' }],
  [/^com\.google\.guava$/, { license: 'Apache-2.0', repository: 'https://github.com/google/guava' }],
  [/^org\.jetbrains\.kotlin/, { license: 'Apache-2.0', repository: 'https://github.com/JetBrains/kotlin' }],
  [/^org\.jetbrains\.kotlinx/, { license: 'Apache-2.0', repository: 'https://github.com/Kotlin/kotlinx.coroutines' }],
  [/^org\.jetbrains$/, { license: 'Apache-2.0', repository: 'https://github.com/JetBrains/java-annotations' }],
]
// Exact reviewed artifact only; do not infer MIT for other Bouncy Castle artifacts or versions.
// POM: https://central.sonatype.com/artifact/org.bouncycastle/bcpkix-jdk15to18/1.84
// License text is preserved in the Android companion packaged notices.
const resolveGradleLicense = (group, artifact, version) => {
  if (group === 'org.bouncycastle' && artifact === 'bcpkix-jdk15to18' && version === '1.84') {
    return { license: 'MIT', repository: 'https://github.com/bcgit/bc-java/tree/r1rv84' }
  }
  for (const [re, meta] of GRADLE_GROUP_LICENSE) if (re.test(group)) return meta
  return { license: 'UNKNOWN', repository: null }
}

/** Scan the android-companion Gradle project. Parses declared coordinates from
 * every *.gradle.kts plus the Kotlin stdlib implied by the kotlin-android plugin,
 * and resolves each to a license via GRADLE_GROUP_LICENSE. Returns [] if the
 * project is absent. Transitive AndroidX/Kotlin deps are all Apache-2.0 and are
 * covered by the same license text in the bundled notice. */
function scanGradle(label) {
  const androidRoot = resolve(repoRoot, 'android-companion')
  if (!existsSync(androidRoot)) {
    console.warn(`[licenses] ${label}: android-companion missing, skipping`)
    return []
  }
  const files = ['build.gradle.kts', 'settings.gradle.kts', 'app/build.gradle.kts']
    .map((f) => resolve(androidRoot, f))
    .filter((p) => existsSync(p))
  const found = new Map() // "group:artifact" -> version
  let kotlinVersion = null
  for (const f of files) {
    const text = readFileSync(f, 'utf-8')
    for (const m of text.matchAll(
      /(?:implementation|api|compileOnly|runtimeOnly|ksp|kapt)\s*\(\s*["']([A-Za-z0-9_.]+):([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)["']/g,
    )) {
      found.set(`${m[1]}:${m[2]}`, m[3])
    }
    const kp = text.match(/org\.jetbrains\.kotlin\.android["']?\s*\)?\s*version\s*["']([A-Za-z0-9_.-]+)["']/)
    if (kp) kotlinVersion = kp[1]
  }
  // The kotlin-android plugin always bundles the matching kotlin-stdlib.
  if (kotlinVersion) found.set('org.jetbrains.kotlin:kotlin-stdlib', kotlinVersion)
  return [...found.entries()]
    .map(([ga, version]) => {
      const [group, artifact] = ga.split(':')
      const { license, repository } = resolveGradleLicense(group, artifact, version)
      return entry(ga, version, license, repository)
    })
    .sort((a, b) => a.name.localeCompare(b.name))
}

// PEP 503 name normalization, so pyproject names match pip-licenses output.
const normalizePy = (n) => String(n).toLowerCase().replace(/[-_.]+/g, '-')

/** Package names from pyproject.toml [project].dependencies ONLY — the engine's
 * shipped runtime deps, which is exactly what data-engine.exe bundles. Optional
 * groups are deliberately EXCLUDED: `dev` (pytest, pip-licenses) is never shipped,
 * and `ios-windows` (pymobiledevice3, GPL-3.0) is an opt-in install the PyInstaller
 * build excludes from the frozen exe — so listing it would (a) misrepresent the
 * distributed artifact and (b) make the manifest non-deterministic across machines
 * (present only where the extra happens to be installed), breaking the CI staleness
 * gate. The optional iOS component is documented in the root README instead. */
function declaredPyDeps() {
  const names = new Set()
  const path = resolve(repoRoot, 'pyproject.toml')
  if (!existsSync(path)) return names
  const toml = readFileSync(path, 'utf-8')
  const addFrom = (block) => {
    for (const m of block.matchAll(/["']\s*([A-Za-z0-9][A-Za-z0-9._-]*)/g)) names.add(normalizePy(m[1]))
  }
  const core = toml.match(/(?:^|\n)dependencies\s*=\s*\[([\s\S]*?)\]/)
  if (core) addFrom(core[1])
  return names
}

/** Scan the Python engine env with pip-licenses, filtered to pyproject deps.
 * Prefers the project venv; returns [] if pip-licenses is unavailable. */
function scanPython(label) {
  const venvPy =
    process.platform === 'win32'
      ? resolve(repoRoot, '.venv/Scripts/python.exe')
      : resolve(repoRoot, '.venv/bin/python')
  const pliArgs = ['--format=json', '--with-urls']
  const attempts = [
    ...(existsSync(venvPy) ? [[venvPy, ['-m', 'piplicenses', ...pliArgs]]] : []),
    // A global pip-licenses executable can inspect a different Python than the
    // one PyInstaller uses. Point it at the project venv explicitly; otherwise
    // a release can publish valid licenses with stale/wrong package versions.
    ...(existsSync(venvPy) ? [['pip-licenses', ['--python', venvPy, ...pliArgs]]] : []),
    ...(!existsSync(venvPy)
      ? [
          ['pip-licenses', pliArgs],
          ['python', ['-m', 'piplicenses', ...pliArgs]],
          ['python3', ['-m', 'piplicenses', ...pliArgs]],
        ]
      : []),
  ]
  const declared = declaredPyDeps()
  for (const [cmd, cmdArgs] of attempts) {
    try {
      const raw = execFileSync(cmd, cmdArgs, {
        encoding: 'utf-8',
        maxBuffer: 64 * 1024 * 1024,
        stdio: ['ignore', 'pipe', 'ignore'],
      })
      const data = JSON.parse(raw)
      return data
        .filter((p) => declared.size === 0 || declared.has(normalizePy(p.Name)))
        .map((p) => entry(p.Name, p.Version, p.License, p.URL && p.URL !== 'UNKNOWN' ? p.URL : null))
        .sort((a, b) => a.name.localeCompare(b.name))
    } catch {
      // try the next invocation
    }
  }
  console.warn(`[licenses] ${label}: pip-licenses unavailable, skipping (install: pip install pip-licenses)`)
  return []
}

// Existing manifest, for per-ecosystem fallback when a fresh scan is empty.
let previous = {}
if (existsSync(outPath)) {
  try {
    const parsed = JSON.parse(readFileSync(outPath, 'utf-8'))
    if (Array.isArray(parsed)) previous = { 'npm-desktop': parsed } // migrate old flat shape
    else for (const e of parsed.ecosystems ?? []) previous[e.id] = e.entries ?? []
  } catch {
    /* ignore a malformed existing file */
  }
}

const specs = [
  { id: 'npm-desktop', label: '데스크톱 (npm)', scan: () => scanNpm(frontendRoot, '데스크톱 (npm)') },
  { id: 'python', label: '엔진 (Python)', scan: () => scanPython('엔진 (Python)') },
  ...(!windowsNoApk && existsSync(resolve(repoRoot, 'android-companion')) ? [{ id: 'gradle-android', label: '동반 앱 (Android/Gradle)', scan: () => scanGradle('동반 앱 (Android/Gradle)') }] : []),
]

const ecosystems = specs.map(({ id, label, scan }) => {
  let entries = scan()
  if (windowsNoApk && !entries.length) throw new Error(`Windows runtime scan empty: ${id}`)
  if (!windowsNoApk && entries.length === 0 && (previous[id]?.length ?? 0) > 0) {
    console.warn(`[licenses] ${label}: using ${previous[id].length} previously committed entries`)
    entries = previous[id]
  }
  // Normalize fresh + fallback alike: drop first-party packages and (re)derive the
  // category, so previously committed entries pick up the current classification.
  entries = entries
    .filter((e) => !FIRST_PARTY.has(e.name))
    .map((e) => entry(e.name, e.version, e.license, e.repository))
    .sort((a, b) => a.name.localeCompare(b.name))
  return { id, label, entries }
})

mkdirSync(dirname(outPath), { recursive: true })
writeFileSync(outPath, JSON.stringify({ ecosystems }, null, 2) + '\n', 'utf-8')

const total = ecosystems.reduce((n, e) => n + e.entries.length, 0)
const flat = ecosystems.flatMap((e) => e.entries)
// Copyleft deps that lawfully combine into the GPL-3.0 core: weak-copyleft
// (LGPL/MPL — dynamically linked/replaceable) plus GPL-compatible strong-copyleft.
// The GPL-3.0 source offer for the whole work already satisfies their source term.
const copyleft = flat.filter(
  (e) => e.category === 'weak-copyleft' || (e.category === 'strong-copyleft' && gplCompatibleCopyleft(e.license))
)
// Blockers under a GPL-3.0 core: proprietary (can't relicense), unknown
// (unclassified — needs a look), and GPL-INCOMPATIBLE copyleft (can't combine).
const blockers = flat.filter(
  (e) =>
    e.category === 'proprietary' ||
    e.category === 'unknown' ||
    (e.category === 'strong-copyleft' && !gplCompatibleCopyleft(e.license))
)
console.log(`wrote ${total} entries (${ecosystems.map((e) => `${e.id}:${e.entries.length}`).join(', ')}) to ${outPath}`)
if (copyleft.length) {
  console.log(
    `[licenses] ${copyleft.length} copyleft dependency(ies) — compatible with the GPL-3.0 core; ` +
      `the project's source offer already covers them:\n` +
      copyleft.map((e) => `  - ${e.name}@${e.version} (${e.license}) [${e.category}]`).join('\n')
  )
}
if (blockers.length) {
  const msg =
    `[licenses] ${blockers.length} third-party dependency(ies) are proprietary / unknown / GPL-incompatible ` +
    `and CANNOT ship in the GPL-3.0 work — review before building:\n` +
    blockers.map((e) => `  - ${e.name}@${e.version} (${e.license}) [${e.category}]`).join('\n')
  // Enforcement gate: a blocker fails the build so a proprietary/unknown/GPL-
  // incompatible dependency can never ship unnoticed. Override for a deliberate,
  // reviewed exception with LICENSES_ALLOW_BLOCKERS=1 (documented in README.md).
  if (!windowsNoApk && process.env.LICENSES_ALLOW_BLOCKERS === '1') {
    console.warn(`${msg}\n[licenses] LICENSES_ALLOW_BLOCKERS=1 explicitly set — continuing despite blockers.`)
  } else {
    console.error(msg)
    console.error(
      '[licenses] Build failed. Remove/replace the dependency, or set LICENSES_ALLOW_BLOCKERS=1 after a documented review.',
    )
    process.exit(1)
  }
} else {
  console.log('[licenses] no proprietary / unknown / GPL-incompatible third-party deps — all deps combine into the GPL-3.0 core.')
}
