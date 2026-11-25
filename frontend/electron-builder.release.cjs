const pkg = require('./package.json')

const version = String(pkg.version || '')
if (!/^\d{4}\.\d{1,2}\.\d{1,2}(?:-\d+)?$/.test(version)) {
  throw new Error(`Release version must use YYYY.M.D or YYYY.M.D-revision: ${version}`)
}

// Release builds deliberately consume a clean, versioned renderer staging
// directory instead of frontend/dist. This keeps packaging reproducible even
// when a running preview has the ordinary dist directory locked on Windows.
module.exports = {
  ...pkg.build,
  directories: {
    ...pkg.build.directories,
    output: `build_output_${version}`,
  },
  files: [
    {
      from: `.release-staging/${version}/dist`,
      to: 'dist',
      filter: ['**/*'],
    },
    {
      from: 'dist-electron',
      to: 'dist-electron',
      filter: [
        '**/*',
        '!**/*.map',
        '!**/{test,tests,__tests__}/**/*',
        '!**/*.{spec,test}.{js,cjs,mjs,ts,tsx}',
      ],
    },
    {
      from: '.',
      to: '.',
      filter: ['package.json'],
    },
    // FileSet-only release inputs replace package.json's global build.files
    // matcher. Restore the archive-wide hygiene exclusions so electron-builder's
    // automatically collected production dependencies cannot reintroduce source
    // maps or package-owned tests into app.asar.
    '!**/*.map',
    '!**/{test,tests,__tests__}/**/*',
    '!**/*.{spec,test}.{js,cjs,mjs,ts,tsx}',
  ],
  extraResources: pkg.build.extraResources.map((resource) => {
    if (resource.to === 'data-engine.exe') {
      return {
        ...resource,
        from: `../dist-release-${version}/data-engine.exe`,
      }
    }
    if (resource.to === 'THIRD-PARTY-NOTICES.txt') {
      return {
        ...resource,
        from: `.release-staging/${version}/THIRD-PARTY-NOTICES.txt`,
      }
    }
    return resource
  }),
}
