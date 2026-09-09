# Changelog

## 1.2.0 — 2026-09-09
- "Select similar" now defaults to OFF — manual target selection is the default workflow.
- Select Similar's matches are now loaded straight into the "Target locations" selection
  list instead of only being drawn as rings. Each match becomes a removable entry (via
  the list's own remove control, or ctrl-click in the viewport), so over-eager matches
  can be trimmed before committing. `_refresh_similar()` populates the list on source
  change / toggle-on; `_sync_targets_display()` redraws rings/info from the list's
  current contents after an edit, without re-running the match search (which would
  otherwise wipe out the user's deselections).
- `_do_batch()` now always reads targets from the "Target locations" list (both modes),
  instead of Select Similar re-running `_find_similar_targets()` at execute time. What
  you see highlighted/listed is exactly what gets placed.
- `_ValidateHandler` now requires at least one target in both modes — previously
  Select Similar mode was always "valid" even with zero matches, only failing at
  execute time with a message box.
- Added a re-entrancy guard (`_populating_targets`) around the bulk list-population
  loop: Fusion can fire `inputChanged` for programmatic selection edits too, and
  without the guard a large match list triggered one nested redraw per entry.
- Fixed: toggling "Select similar" on could silently fail to refresh the match count/
  list when the source was already selected. Root cause: a cosmetic `tgt.name = ...`
  rename, run before the refresh call, could throw on this input type; the outer
  catch-all handler swallowed the exception and aborted before reaching the refresh.
  Fixed by wrapping the rename in its own try/except.

## 1.1.1 — 2026-05-27
- Fixed: Select Similar OK/Preview buttons remain greyed out even when matches are found.
  Root cause: `INPUT_TARGETS.setSelectionLimits(1, 0)` caused Fusion to enforce a 1-selection
  minimum even on the hidden/disabled targets input. Changed to `setSelectionLimits(0, 0)`;
  the validate handler already enforces the manual-mode requirement explicitly.
- Manual mode now accepts sketch geometry: sketch points (`SketchPoint`) and sketch circles/arcs
  (`SketchCurve`). `_target_to_geo2` dispatches to `createByPoint` / `createByCurve` accordingly.
  Selection filters updated to include SketchPoints, SketchCurves, SketchCircles, SketchArcs.

## 1.1.0 — 2026-05-27
- Improved error messages: Fusion cascade-failure dumps condensed to one line + count.
  Actionable guidance shown when all copies fail due to assembly compute errors.
- Preview checkbox restored with warning label "⚠ may be slow on large assemblies".
- `_find_similar_targets()` called once per source pick (was twice — caused slowdowns
  on large assemblies and could leave the OK button greyed out).
- Null guards added in `_ValidateHandler` for all three input casts.

## 1.0.9 *(skipped — merged into 1.1.0)*

## 1.0.8 — 2026-05-27
- Preview checkbox temporarily removed; `_PreviewHandler` made a no-op.
- `_refresh_similar()` helper introduced to merge duplicate `_find_similar_targets()` calls.
- Simplified `_InputChangedHandler` (no phase-gate logic).

## 1.0.7 — 2026-05-27
- Yellow highlight rings via `CustomGraphics` show matched holes in Select Similar mode.
- `_ValidateHandler` simplified: no longer calls `_count_similar()` on every validate event.
  Fixes greyed-out OK button when assembly has many (100+) BRep edges.
- Two-phase dialog: "Preview placement" checkbox gates live preview; unchecked = fast path.
- `_DestroyHandler` cleans up `CustomGraphicsGroup` on command close.

## 1.0.6 — 2026-05-27
- `_find_matching_circular_edges()` fallback: Select Similar now works on plain hole
  patterns with no named Joint Origins. Edges grouped by perpendicular-plane centre;
  one edge per hole chosen at the same face depth as the reference joint.

## 1.0.5 — 2026-05-27
- Fixed: only first instance placed in manual multi-target mode.
  Root cause: `JointGeometry` transients become stale after `joints.add()` modifies
  the model. Fix: re-fetch `comp_geo_i` via `_joint_sides()` at top of each loop iteration.

## 1.0.4 — 2026-05-27
- Fixed: `InternalValidationError: targetObj` in Select Similar.
  Root cause: calling `.geometry` on a proxied `JointOrigin` (from `joint.geometryOrOriginTwo`).
  Fix: strip to `nativeObject` first — `native = getattr(ref_jo, 'nativeObject', None) or ref_jo`.

## 1.0.3 — 2026-05-27
- `_do_batch()` shared by `_PreviewHandler` (silent) and `_ExecuteHandler`.
  `executePreview` + `isValidResult=True` commits preview on OK; execute never fires.
  All instance creation unified in one function to avoid the "only 1 committed" bug.

## 1.0.2 — 2026-05-27
- `_make_geo1()` added to support implicit `JointGeometry` (faces, circular edges)
  in addition to named `JointOrigin`. Dispatches to `createByCurve` / `createByNonPlanarFace`
  / `createByPlanarFace` / `createByPoint` based on entity type.

## 1.0.1 — 2026-05-27
- Fixed: `RootJointOrigins` is not a valid selection filter — crashed `_CreatedHandler`
  on command open. Removed invalid filter; remaining filters wrapped in try/except.

## 1.0.0 — 2026-05-27
- Initial release.
- Button in Assemble panel (Design workspace).
- Select Similar checkbox: auto-detects matching joint origins by axis direction.
- Manual target selection: joint origins or circular edges.
- Flip direction checkbox, seeded from source joint.
- Single undo step for all created instances.
