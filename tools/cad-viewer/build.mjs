/**
 * Builds the read-only DWG viewer into static/cad-viewer/.
 *
 * Outputs:
 *   static/cad-viewer/cad-viewer.js                 bundled viewer (ESM)
 *   static/cad-viewer/workers/mtext-renderer-worker.js
 *   static/cad-viewer/workers/libredwg-parser-worker.js
 *   static/cad-viewer/workers/libredwg-web.wasm
 *
 * Fonts and the drawing template under static/cad-viewer/assets/ are vendored
 * once from mlightcad/cad-data (see README.md); this script does not download
 * anything.
 *
 * Usage: npm run build   (from tools/cad-viewer/)
 */
import { cp, mkdir, rm } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import esbuild from 'esbuild'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..', '..')
const outDir = path.join(repoRoot, 'static', 'cad-viewer')
const workersOut = path.join(outDir, 'workers')

await mkdir(workersOut, { recursive: true })

// Fresh JS bundle every build; assets/ (fonts, template) are left untouched.
await rm(path.join(outDir, 'cad-viewer.js'), { force: true })

/**
 * The @mlightcad packages import three's example modules without the `.js`
 * suffix (Vite resolves that for them). Do the same here.
 */
const threeExamplesResolver = {
  name: 'three-examples-resolver',
  setup(build) {
    build.onResolve({ filter: /^three\/examples\// }, (args) => {
      const rel = args.path.endsWith('.js') ? args.path : `${args.path}.js`
      return { path: path.join(here, 'node_modules', rel) }
    })
  },
}

const result = await esbuild.build({
  entryPoints: [path.join(here, 'src', 'main.js')],
  outfile: path.join(outDir, 'cad-viewer.js'),
  bundle: true,
  format: 'esm',
  minify: true,
  target: ['es2020'],
  legalComments: 'none',
  define: { 'process.env.NODE_ENV': '"production"' },
  plugins: [threeExamplesResolver],
  logLevel: 'info',
  metafile: true,
})

const copies = [
  [
    'node_modules/@mlightcad/cad-simple-viewer/dist/mtext-renderer-worker.js',
    'mtext-renderer-worker.js',
  ],
  [
    'node_modules/@mlightcad/libredwg-converter/dist/libredwg-parser-worker.js',
    'libredwg-parser-worker.js',
  ],
  // The parser worker loads its wasm relative to import.meta.url, so the wasm
  // must sit next to it.
  ['node_modules/@mlightcad/libredwg-converter/dist/libredwg-web.wasm', 'libredwg-web.wasm'],
]

for (const [from, to] of copies) {
  await cp(path.join(here, from), path.join(workersOut, to))
}
await cp(
  path.join(here, 'node_modules/@mlightcad/libredwg-converter/LICENSE'),
  path.join(outDir, 'LICENSE-LIBREDWG.txt'),
)

const bytes = Object.values(result.metafile.outputs).reduce((sum, out) => sum + out.bytes, 0)
console.log(`built static/cad-viewer/cad-viewer.js (${(bytes / 1024 / 1024).toFixed(2)} MB) + workers`)
