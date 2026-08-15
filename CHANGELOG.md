# Changelog

## 0.5.0 — 2026-08-15

- Added direct ZIP import for Global Mapper raster tile exports with the
  trusted `Z<zoom>/<y>/<x>.<ext>` layout.
- Added strict ZIP and XML safety limits, coordinate and duplicate validation,
  actual bounds/center calculation, and `global-mapper-z-y-x` source metadata.
- Added Russian and English UI guidance and clear unsupported-layout errors.
- Preserved PMTiles CLI verification for every generated map.
