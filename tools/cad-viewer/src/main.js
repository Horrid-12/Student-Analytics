/**
 * Read-only DWG viewer entry point.
 *
 * Bundled by build.mjs into static/cad-viewer/cad-viewer.js and loaded
 * lazily by app/templates/pages/autocad.html the first time someone clicks
 * View. Everything the viewer needs at runtime (JS bundle, workers, wasm,
 * font catalog) is served from /static/cad-viewer/, so the page never talks
 * to a CDN or any other external host.
 *
 * No UI plugins are registered on purpose: the host page owns the modal,
 * the loading/error states, and the only action we expose is Fit.
 */
import {
  AcApDocManager,
  AcApSettingManager,
  AcEdOpenMode,
  acedApplyUiTheme,
} from '@mlightcad/cad-simple-viewer'
import { AcDbDatabaseConverterManager, AcDbFileType } from '@mlightcad/data-model'
import { AcDbLibreDwgConverter } from '@mlightcad/libredwg-converter'

/**
 * Resolve a file next to this bundle. Kept behind a function so the bundler
 * treats the paths as runtime values instead of build-time asset imports.
 * @param {string} relativePath
 * @returns {string}
 */
function assetUrl(relativePath) {
  return new URL(relativePath, import.meta.url).href
}

function workerUrls() {
  return {
    dwgParser: assetUrl('workers/libredwg-parser-worker.js'),
    mtextRender: assetUrl('workers/mtext-renderer-worker.js'),
  }
}

function currentTheme() {
  const theme = document.documentElement.dataset.theme
  return theme === 'dark' ? 'dark' : 'light'
}

/** @type {Promise<import('@mlightcad/cad-simple-viewer').AcApDocManager> | null} */
let managerPromise = null
let drawingQueue = Promise.resolve()
let hasOpenDrawing = false

/**
 * Create the singleton document manager once. Worker URLs are probed before
 * the manager is created so a missing asset fails with a clear error instead
 * of a broken canvas later on.
 * @param {HTMLElement} container canvas parent (must be visible and sized)
 * @param {HTMLElement} host element that hosts the busy overlay
 */
function ensureManager(container, host) {
  if (!managerPromise) {
    managerPromise = (async () => {
      // View-only shell: no command line, no floating undo/redo toolbar, no
      // ribbon/toolbar. Session-only overrides so nothing is written to
      // localStorage.
      AcApSettingManager.instance.apply(
        {
          isShowCommandLine: false,
          isShowShortCutToolbar: false,
          isShowRibbon: false,
          isShowToolbar: false,
          isShowCoordinate: false,
          isShowEntityInfo: false,
          isShowStats: false,
        },
        { persist: false },
      )
      const urls = workerUrls()
      // DWG parsing is opt-in in cad-simple-viewer; register LibreDWG here.
      AcDbDatabaseConverterManager.instance.register(
        AcDbFileType.DWG,
        new AcDbLibreDwgConverter({
          convertByEntityType: false,
          useWorker: true,
          parserWorkerUrl: urls.dwgParser,
        }),
      )
      const ready = await AcApDocManager.checkWebworkerReadiness(urls)
      if (!ready) {
        throw new Error('viewer-assets-missing')
      }
      acedApplyUiTheme(currentTheme(), host)
      const created = AcApDocManager.createInstance({
        container,
        busyIndicatorHost: host,
        autoResize: true,
        // Fonts and drawing templates load from our own static folder.
        baseUrl: assetUrl('assets/'),
        webworkerFileUrls: urls,
        // View-only: no export commands, no OPEN file dialog, no built-in
        // notification bell (host page owns all chrome).
        disableExport: true,
        builtinOpenFileDialog: false,
        notificationCenter: false,
      })
      return created || AcApDocManager.instance
    })()
    // Allow a retry after a failed first attempt (offline start, blocked
    // workers, ...) instead of caching the rejection forever.
    managerPromise.catch(() => {
      managerPromise = null
    })
  }
  return managerPromise
}

/** Initialize the local viewer engine while the selected DWG is downloading. */
export async function prepareViewer({ container, host }) {
  if (!container) {
    throw new Error('viewer-open-failed')
  }
  if (typeof WebGLRenderingContext === 'undefined') {
    throw new Error('viewer-webgl-missing')
  }
  await ensureManager(container, host || container)
  return true
}

/**
 * Open a DWG drawing inside the given container.
 *
 * @param {{container: HTMLElement, host?: HTMLElement, fileName: string, buffer: ArrayBuffer}} options
 * @returns {Promise<true>}
 * @throws {Error} with a stable `code` the host maps to a friendly message:
 *   viewer-assets-missing, viewer-webgl-missing, viewer-parse-failed,
 *   viewer-open-failed.
 */
async function openDrawingNow({ container, host, fileName, buffer }) {
  if (!container || !buffer) {
    throw new Error('viewer-open-failed')
  }
  if (typeof WebGLRenderingContext === 'undefined') {
    throw new Error('viewer-webgl-missing')
  }
  const manager = await ensureManager(container, host || container)
  if (hasOpenDrawing) {
    await manager.closeDocument()
    hasOpenDrawing = false
  }
  let opened = false
  try {
    opened = await manager.openDocument(fileName, buffer, {
      mode: AcEdOpenMode.Read,
      minimumChunkSize: 1000,
      // Keep the host loading overlay up until the full scene is ready.
      progressiveRendering: false,
    })
  } catch (error) {
    console.error('DWG open failed:', error)
    throw new Error('viewer-parse-failed')
  }
  if (!opened) {
    throw new Error('viewer-parse-failed')
  }
  hasOpenDrawing = true

  // openDocument() confirms the file parsed, but CAD entity conversion and
  // deferred text/INSERT geometry can still be running. Do not dismiss the
  // host's loading overlay until both queues settle; otherwise a first open
  // can expose a partial frame. Then do an awaited final fit against the full
  // scene extents instead of relying on the open-time fire-and-forget fit.
  const view = manager.curView
  const idle = await view.waitUntilIdle(90_000)
  if (!idle) {
    throw new Error('viewer-render-timeout')
  }
  try {
    await view.zoomToSmartExtents(30_000)
  } catch (error) {
    console.warn('Smart extents failed; using drawing extents:', error)
    view.zoomToFitDrawing(30_000)
  }
  view.isDirty = true
  return true
}

/** Serialize opens so a quick second click cannot race document creation. */
export function openDrawing(options) {
  const operation = drawingQueue.catch(() => {}).then(() => openDrawingNow(options))
  drawingQueue = operation
  return operation
}

/** Frame the whole drawing (ZOOM Extents). Safe to call any time. */
export function fitDrawing() {
  const manager = AcApDocManager.instance
  if (!manager) return
  try {
    manager.sendStringToExecute('zoom extents')
  } catch (error) {
    console.warn('Fit view failed:', error)
  }
}

export { AcApDocManager }
