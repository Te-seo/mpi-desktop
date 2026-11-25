// Generate THIRD-PARTY-NOTICES.txt for the desktop distribution.
//   node scripts/gen-notices.mjs
//
// The MPI desktop app is distributed under GPL-3.0 (root LICENSE). GPL §4-6 and
// the permissive licenses of the bundled dependencies require that the license
// text + copyright notices, and an offer of the corresponding source, TRAVEL WITH
// the installed binary — a web page alone does not discharge that for a downloaded
// app. This script builds a single notices document covering everything the
// installer ships (the Electron desktop = npm-desktop, the frozen Python engine =
// python, and the bundled companion.apk = gradle-android) and is run automatically
// before every desktop build via the `prebuild` npm hook, then bundled by
// electron-builder (extraResources) into the install directory.
//
// It reads the package LIST from the committed manifest
// (licenses/third-party.json, the single source of truth kept fresh by
// gen-licenses.mjs) and the full license TEXT from each package on disk
// (node_modules for npm, pip-licenses for Python). Missing text degrades to a
// reference line; the script never throws so a build is never blocked by it.
import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '..')
const repoRoot = resolve(frontendRoot, '..')
const windowsNoApk = process.argv[2] === '--windows-no-apk'
if (windowsNoApk && process.argv.length !== 5) throw new Error('Windows notices require exact inventory and output paths')
const manifestPath = windowsNoApk ? resolve(process.argv[3]) : resolve(repoRoot, 'licenses/third-party.json')
const licensePath = resolve(repoRoot, 'LICENSE')
const outPath = windowsNoApk ? resolve(process.argv[4]) : process.env.MPI_NOTICES_OUT
  ? resolve(process.env.MPI_NOTICES_OUT)
  : resolve(frontendRoot, 'THIRD-PARTY-NOTICES.txt')
const nodeModules = resolve(frontendRoot, 'node_modules')

const REPO_URL = 'https://github.com/Te-seo/mpi-desktop'
const CORRESPONDING_RELEASE_URL = 'https://dl.mpinspect.com/mpi-source-2026.11.24-1-core-1e3f384204efb519fac800d1629f27efdf07c33af357098b69c3b731484d4de4.zip'
const RULE = '='.repeat(78)
const SUBRULE = '-'.repeat(78)

const normalizePy = (n) => String(n).toLowerCase().replace(/[-_.]+/g, '-')

// ---------------------------------------------------------------------------
// License text sources
// ---------------------------------------------------------------------------
const NPM_LICENSE_FILES = [
  'LICENSE', 'LICENSE.md', 'LICENSE.txt', 'LICENCE', 'LICENCE.md', 'LICENCE.txt',
  'LICENSE-MIT', 'LICENSE-MIT.txt', 'LICENSE.BSD', 'COPYING', 'COPYING.md', 'license', 'license.md',
]

/** Full license text of an npm package from node_modules, or null. */
function npmLicenseText(name) {
  const dir = resolve(nodeModules, ...name.split('/'))
  if (!existsSync(dir)) return null
  for (const f of NPM_LICENSE_FILES) {
    const p = resolve(dir, f)
    if (existsSync(p)) {
      try {
        const t = readFileSync(p, 'utf-8').trim()
        if (t) return t
      } catch {
        /* unreadable — try the next candidate */
      }
    }
  }
  return null
}

/** name(normalized) -> { text } from pip-licenses --with-license-file. */
function pythonLicenseTexts() {
  const venvPy =
    process.platform === 'win32'
      ? resolve(repoRoot, '.venv/Scripts/python.exe')
      : resolve(repoRoot, '.venv/bin/python')
  const args = ['--format=json', '--with-license-file', '--with-urls']
  const attempts = [
    ...(existsSync(venvPy) ? [[venvPy, ['-m', 'piplicenses', ...args]]] : []),
    ...(existsSync(venvPy) ? [['pip-licenses', ['--python', venvPy, ...args]]] : []),
    ['pip-licenses', args],
    ['python', ['-m', 'piplicenses', ...args]],
    ['python3', ['-m', 'piplicenses', ...args]],
  ]
  for (const [cmd, cmdArgs] of attempts) {
    try {
      const raw = execFileSync(cmd, cmdArgs, {
        encoding: 'utf-8',
        maxBuffer: 64 * 1024 * 1024,
        stdio: ['ignore', 'pipe', 'ignore'],
      })
      const map = new Map()
      for (const p of JSON.parse(raw)) {
        const text = p.LicenseText && p.LicenseText !== 'UNKNOWN' ? String(p.LicenseText).trim() : null
        map.set(normalizePy(p.Name), text)
      }
      return map
    } catch {
      /* try the next invocation */
    }
  }
  return new Map()
}

// ---------------------------------------------------------------------------
// Document assembly
// ---------------------------------------------------------------------------
function loadManifest() {
  try {
    return JSON.parse(readFileSync(manifestPath, 'utf-8')).ecosystems ?? []
  } catch {
    return []
  }
}
const ecosystemById = (ecos, id) => ecos.find((e) => e.id === id)?.entries ?? []

function section(title) {
  return `\n${RULE}\n${title}\n${RULE}\n`
}

function npmBlock(entries) {
  const parts = []
  let missing = 0
  for (const e of entries) {
    const head = `${e.name}@${e.version}  —  ${e.license}${e.repository ? `\n${e.repository}` : ''}`
    const text = npmLicenseText(e.name)
    if (text) {
      parts.push(`${SUBRULE}\n${head}\n${SUBRULE}\n\n${text}\n`)
    } else {
      missing++
      parts.push(`${SUBRULE}\n${head}\n${SUBRULE}\n\n(License text not bundled in the package; see the repository above or npmjs.com/package/${e.name}.)\n`)
    }
  }
  if (missing) console.warn(`[notices] npm: ${missing}/${entries.length} package(s) shipped no license file inline`)
  return parts.join('\n')
}

function pythonBlock(entries, texts) {
  const parts = []
  let missing = 0
  for (const e of entries) {
    const head = `${e.name}@${e.version}  —  ${e.license}${e.repository ? `\n${e.repository}` : ''}`
    const text = texts.get(normalizePy(e.name))
    if (text) {
      parts.push(`${SUBRULE}\n${head}\n${SUBRULE}\n\n${text}\n`)
    } else {
      missing++
      parts.push(`${SUBRULE}\n${head}\n${SUBRULE}\n\n(License text unavailable from the environment; see ${e.repository || 'the project homepage'}.)\n`)
    }
  }
  if (missing) console.warn(`[notices] python: ${missing}/${entries.length} package(s) had no license text`)
  return parts.join('\n')
}

// Canonical Apache License 2.0 text (apache.org/licenses/LICENSE-2.0.txt), embedded
// so Android components identified as Apache-2.0 carry a copy of the License, as
// Apache-2.0 §4 requires — independent of what any dependency ships inline.
const APACHE_2_0 = `                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS`

function androidBlock(entries) {
  if (!entries.length) return ''
  const list = entries.map((e) => `  - ${e.name}@${e.version}  —  ${e.license}${e.repository ? `  (${e.repository})` : ''}`).join('\n')
  const apache = entries.filter((e) => e.license === 'Apache-2.0')
  let text = `The bundled Android companion (companion.apk) includes the following components,\nwith each component's license identified individually:\n\n${list}\n\n`
  if (apache.length) {
    const holders = []
    if (apache.some((e) => e.name.startsWith('androidx.'))) {
      holders.push('  Copyright (c) The Android Open Source Project (AndroidX / Jetpack)')
    }
    if (apache.some((e) => e.name.startsWith('org.jetbrains.kotlin:'))) {
      holders.push('  Copyright (c) JetBrains s.r.o. and Kotlin Programming Language contributors (Kotlin standard library)')
    }
    if (holders.length) text += `${holders.join('\n')}\n\n`
    text += `Apache License 2.0 (applies only to components explicitly marked Apache-2.0 above):\n\n${APACHE_2_0}\n`
  }
  if (apache.length !== entries.length) {
    text += '\nOther components remain subject to their own identified licenses and source links above; the Apache License text does not replace those terms.\n'
  }
  return text
}

function build() {
  const ecos = loadManifest()
  const npmDesktop = ecosystemById(ecos, 'npm-desktop')
  const python = ecosystemById(ecos, 'python')
  const android = ecosystemById(ecos, 'gradle-android')
  const isLegacyCompanionInventory = ecos.some(e => e.id === 'gradle-android')
  if (windowsNoApk && (!npmDesktop.length || !python.length || android.length)) throw new Error('Incorrect no-APK Windows notice scope')

  let gplText = ''
  try {
    gplText = readFileSync(licensePath, 'utf-8').trim()
  } catch {
    gplText = '(GPL-3.0 license text unavailable; see https://www.gnu.org/licenses/gpl-3.0.txt)'
  }

  const header =
    `MPI (MPI) — THIRD-PARTY NOTICES\n${RULE}\n\n` +
    `This product is the "MPI" desktop application (MPI core), which is FREE\n` +
    `SOFTWARE distributed under the GNU General Public License, version 3 (GPL-3.0).\n` +
    `The full GPL-3.0 text is reproduced at the end of this file and in the LICENSE\n` +
    `file installed alongside this application.\n\n` +
    `WRITTEN OFFER / CORRESPONDING SOURCE (GPL-3.0 §3, §6):\n` +
    `  The complete corresponding source code for this application is publicly\n` +
    `  available at:\n\n      ${REPO_URL}\n\n` +
    `  Exact corresponding source for the previous Windows release 2026.11.24-1:\n\n      ${CORRESPONDING_RELEASE_URL}\n\n` +
    `  Use the source link for your exact installer version. If you\n` +
    `  cannot obtain the source there, the copyright holder will provide it on\n` +
    `  request via the project repository's issue tracker.\n\n` +
    `This application bundles the third-party software listed below\n` +
    (windowsNoApk || !isLegacyCompanionInventory ? `(the Electron desktop UI and the Python measurement engine). Each component\n` :
      `(the Electron desktop UI, the Python measurement engine, and the Android\ncompanion app). Each component\n`) +
    `is subject to its own identified license. License\n` +
    `text is included where available; other entries refer to upstream sources.\n` +
    `This notice does not grant distribution rights or establish GPL compatibility;\n` +
    `the separate release license-compliance checks must also pass.\n`

  const doc =
    header +
    section(`1. DESKTOP APPLICATION (npm) — ${npmDesktop.length} packages`) +
    npmBlock(npmDesktop) +
    (windowsNoApk
      ? section(`2. MEASUREMENT ENGINE (Python)`) +
        'See the bundled ENGINE-RUNTIME-NOTICES.txt for the audited physical engine payload.\n\n'
      : section(`2. MEASUREMENT ENGINE (Python) — ${python.length} packages`) +
        pythonBlock(python, python.length ? pythonLicenseTexts() : new Map())) +
    (windowsNoApk || !isLegacyCompanionInventory ? '' : section(`3. ANDROID COMPANION (Gradle) — ${android.length} components`) + androidBlock(android)) +
    section('4. THIS APPLICATION — GNU GENERAL PUBLIC LICENSE v3.0') +
    `\n${gplText}\n`

  writeFileSync(outPath, doc.replace(/\r\n/g, '\n'), 'utf-8')
  const kb = Math.round(Buffer.byteLength(doc, 'utf-8') / 1024)
  console.log(
    `[notices] wrote ${outPath} (${kb} KB): npm-desktop ${npmDesktop.length}, python ${python.length}, android ${android.length}`,
  )
  if (!npmDesktop.length) console.warn('[notices] WARNING: npm-desktop is empty — is the manifest stale / node_modules missing?')
}

build()
