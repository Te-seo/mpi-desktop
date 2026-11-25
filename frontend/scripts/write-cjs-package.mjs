// After tsc compiles dist-electron/{main,preload}.js as CommonJS, the parent
// package.json sets "type": "module" so Node treats those .js files as ESM and
// crashes with "exports is not defined in ES module scope". The canonical
// Node.js fix is to drop a tiny package.json next to the CJS output that says
// the opposite.
//
// Run from the frontend/ directory.
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'

const target = 'dist-electron/package.json'
mkdirSync(dirname(target), { recursive: true })
writeFileSync(target, JSON.stringify({ type: 'commonjs' }) + '\n', 'utf8')
console.log(`wrote ${target}`)
