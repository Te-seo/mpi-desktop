const {execFileSync} = require('node:child_process')
const {resolve} = require('node:path')
module.exports = async ({appOutDir, electronPlatformName}) => {
  if (electronPlatformName !== 'win32') return
  const repo = resolve(__dirname, '../..')
  execFileSync('python', [resolve(repo, 'scripts/inspect_windows_no_apk.py'), '--directory', appOutDir],
    {cwd: repo, stdio: 'inherit'})
}
