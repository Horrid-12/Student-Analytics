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
 * the loading/error states, and the viewer is limited to read-only navigation.
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

let gridOverlay = null

/** Install a lightweight WCS-aligned dot grid over the viewer canvas. */
function ensureGridOverlay(container, view) {
  if (gridOverlay && gridOverlay.container === container) {
    gridOverlay.view = view
    return gridOverlay
  }

  const canvas = document.createElement('canvas')
  canvas.className = 'cad-viewer-grid-overlay'
  canvas.setAttribute('aria-hidden', 'true')
  Object.assign(canvas.style, {
    position: 'absolute',
    inset: '0',
    width: '100%',
    height: '100%',
    zIndex: '2',
    pointerEvents: 'none',
    display: 'none',
  })
  container.appendChild(canvas)
  const context = canvas.getContext('2d')
  let enabled = false
  let frame = 0

  function draw() {
    frame = 0
    if (!enabled || !context) return
    const bounds = container.getBoundingClientRect()
    const width = Math.max(1, bounds.width)
    const height = Math.max(1, bounds.height)
    const ratio = Math.max(1, window.devicePixelRatio || 1)
    const pixelWidth = Math.round(width * ratio)
    const pixelHeight = Math.round(height * ratio)
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth
      canvas.height = pixelHeight
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0)
    context.clearRect(0, 0, width, height)

    try {
      const viewWidth = view.width || width
      const viewHeight = view.height || height
      const corners = [
        view.screenToWorld({ x: 0, y: 0 }),
        view.screenToWorld({ x: viewWidth, y: 0 }),
        view.screenToWorld({ x: viewWidth, y: viewHeight }),
        view.screenToWorld({ x: 0, y: viewHeight }),
      ]
      const toOverlay = (point) => view.canvasToContainer(view.worldToScreen(point))
      const origin = toOverlay({ x: 0, y: 0 })
      const unit = toOverlay({ x: 1, y: 0 })
      const pixelsPerUnit = Math.hypot(unit.x - origin.x, unit.y - origin.y)
      if (!Number.isFinite(pixelsPerUnit) || pixelsPerUnit <= 0) return

      const xs = corners.map((point) => point.x).filter(Number.isFinite)
      const ys = corners.map((point) => point.y).filter(Number.isFinite)
      if (!xs.length || !ys.length) return
      const minX = Math.min(...xs)
      const maxX = Math.max(...xs)
      const minY = Math.min(...ys)
      const maxY = Math.max(...ys)
      const rawStep = 34 / pixelsPerUnit
      const power = 10 ** Math.floor(Math.log10(rawStep))
      const candidates = [1, 2, 5, 10].map((factor) => factor * power)
      const step = candidates.reduce((best, candidate) =>
        Math.abs(Math.log(candidate * pixelsPerUnit / 34)) < Math.abs(Math.log(best * pixelsPerUnit / 34))
          ? candidate
          : best,
      )
      const startX = Math.ceil(minX / step)
      const endX = Math.floor(maxX / step)
      const startY = Math.ceil(minY / step)
      const endY = Math.floor(maxY / step)
      context.fillStyle = 'rgba(190, 205, 222, .28)'
      for (let ix = startX; ix <= endX; ix += 1) {
        const x = ix * step
        for (let iy = startY; iy <= endY; iy += 1) {
          const point = toOverlay({ x, y: iy * step })
          if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) continue
          const major = ix % 5 === 0 && iy % 5 === 0
          context.globalAlpha = major ? 0.85 : 0.55
          context.beginPath()
          context.arc(point.x, point.y, major ? 1.35 : 0.9, 0, Math.PI * 2)
          context.fill()
        }
      }
      context.globalAlpha = 1
    } catch (error) {
      console.warn('Grid refresh failed:', error)
    }
  }

  function refresh() {
    if (enabled && !frame) frame = requestAnimationFrame(draw)
  }

  const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(refresh)
  observer?.observe(container)
  container.addEventListener('wheel', refresh, { passive: true })
  container.addEventListener('pointermove', refresh, { passive: true })
  container.addEventListener('pointerup', refresh, { passive: true })
  window.addEventListener('resize', refresh, { passive: true })

  gridOverlay = {
    container,
    setVisible(value) {
      enabled = Boolean(value)
      canvas.style.display = enabled ? 'block' : 'none'
      if (enabled) refresh()
      else {
        if (frame) cancelAnimationFrame(frame)
        frame = 0
        context?.clearRect(0, 0, canvas.width, canvas.height)
      }
    },
    refresh,
  }
  return gridOverlay
}

/** Initialize the local viewer engine while the selected DWG is downloading. */
export async function prepareViewer({ container, host }) {
  if (!container) {
    throw new Error('viewer-open-failed')
  }
  if (typeof WebGLRenderingContext === 'undefined') {
    throw new Error('viewer-webgl-missing')
  }
  const manager = await ensureManager(container, host || container)
  ensureGridOverlay(container, manager.curView)
  return true
}

/** Show or hide the CAD-style WCS grid on the active drawing. */
export function setGridVisible(visible) {
  gridOverlay?.setVisible(visible)
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
  ensureGridOverlay(container, manager.curView)
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
  gridOverlay?.refresh()
  return true
}

/** Serialize opens so a quick second click cannot race document creation. */
export function openDrawing(options) {
  const operation = drawingQueue.catch(() => {}).then(() => openDrawingNow(options))
  drawingQueue = operation
  return operation
}

export { AcApDocManager }
