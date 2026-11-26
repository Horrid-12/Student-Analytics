# Read-only DWG viewer assets

This build-only project bundles the read-only MLightCAD viewer into `static/cad-viewer/`.
The deployed page loads the bundle and its workers locally; it does not fetch JavaScript,
WebAssembly, fonts, or drawing templates from a CDN.

## Rebuild

```powershell
npm ci
npm run build
```

Run these commands from `tools/cad-viewer/`. Commit the generated runtime files under
`static/cad-viewer/`, but never commit `node_modules/`.

The current dependency versions are pinned in `package.json` and `package-lock.json`.
`@mlightcad/libredwg-converter` (and its LibreDWG WebAssembly parser) is GPL-3.0; the build
copies its license to `static/cad-viewer/LICENSE-LIBREDWG.txt`. Review license and source-
offer obligations before distributing or deploying the bundle.

The font catalog, selected fonts, and `acadiso.dxf` template under
`static/cad-viewer/assets/` were vendored from `mlightcad/cad-data`. That repository's
README warns that font licensing may require purchase. Verify redistribution rights for
the selected font assets before public deployment. The runtime catalog is intentionally
trimmed to fonts actually included in the local folder, so missing files never trigger
external requests.
