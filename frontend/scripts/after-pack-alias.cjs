const fs = require('node:fs')
const path = require('node:path')

/**
 * One-release compatibility alias for external hosts that still launch the old
 * executable name. The version gate makes the alias disappear automatically on
 * the next release while MPI.exe remains the manifest/runtime entrypoint.
 */
module.exports = async function afterSign(context) {
  if (context.packager.appInfo.version !== '2026.7.27-1') return
  const primary = path.join(context.appOutDir, 'MPI.exe')
  const alias = path.join(context.appOutDir, 'PerfInspect.exe')
  if (!fs.existsSync(primary)) {
    throw new Error(`MPI executable is missing: ${primary}`)
  }
  fs.copyFileSync(primary, alias)
}
