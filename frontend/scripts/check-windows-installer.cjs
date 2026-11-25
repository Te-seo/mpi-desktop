const {execFileSync} = require('node:child_process')
const {resolve} = require('node:path')
module.exports = async (result) => {
  const windows = [...result.platformToTargets.keys()].some(p => p.name === 'windows')
  if (!windows) return []
  const exe = result.artifactPaths.filter(path => path.toLowerCase().endsWith('.exe'))
  if (exe.length !== 1) throw new Error('Expected exactly one inspectable Windows NSIS installer')
  const repo = resolve(__dirname, '../..')
  execFileSync(process.execPath, [resolve(repo, 'scripts/check-windows-no-apk.mjs'), '--installer', exe[0]],
    {cwd: repo, stdio: 'inherit'})
  return []
}
