# Development Notes — FusionBatchInsert

## Bundle structure

```
FusionBatchInsert.bundle/
  PackageContents.xml          — version shown in Utilities → Add-Ins; bump AppVersion + ComponentEntry Version
  Contents/
    FusionBatchInsert.manifest — JSON: version, runOnStartup, id
    FusionBatchInsert.py       — entry point: run() / stop()
    batch_insert.py            — all command logic
    resources/BatchInsert/     — 16x16, 32x32, 64x64 toolbar icons
    resources/help/help.html   — in-app help page (Command.helpFile — F1 / Help button)
```

Bump the version (`AppVersion`, `ComponentEntry Version`, and the manifest's
`"version"`) together on every change, and update the `<span class="version">`
and "should appear... with version" line in `resources/help/help.html` to match
— Marketplace requires an updated version number on every submission.

## Deployment (Windows)

Fusion loads add-ins from `%APPDATA%\Autodesk\ApplicationPlugins\`. A directory junction
links that location to this repo so edits take effect after a reload (no copy step):

```powershell
cmd /c mklink /J "%APPDATA%\Autodesk\ApplicationPlugins\FusionBatchInsert.bundle" "C:\Fusion Gadgets\Batch Insert\FusionBatchInsert.bundle"
```

Reload the add-in: Utilities → Add-Ins → select FusionBatchInsert → Stop → Run.

## Handler lifecycle

Fusion's garbage collector drops Python event handlers unless they are retained. Two lists
keep handlers alive for the full session:

- `handlers` (passed in from `FusionBatchInsert.py`) — prevents GC during the session
- `_retained` (module-level list in `batch_insert.py`) — second retention as safety net

Pattern: every handler is appended to both lists after being registered.

`_CreatedHandler` fires once per command invocation and creates the remaining handlers
(`_InputChanged`, `_Validate`, `_Preview`, `_Execute`, `_Destroy`).

## Input IDs

| Constant       | ID            | Type                    | Purpose                            |
|----------------|---------------|-------------------------|------------------------------------|
| `INPUT_SOURCE` | `bi_source`   | SelectionCommandInput   | Source occurrence (has a joint)    |
| `INPUT_SIMILAR`| `bi_similar`  | BoolValueCommandInput   | Select Similar toggle              |
| `INPUT_INFO`   | `bi_info`     | TextBoxCommandInput     | Match count / status (read-only)   |
| `INPUT_TARGETS`| `bi_targets`  | SelectionCommandInput   | Target list — manual picks, or auto-populated matches in Select-similar mode (single source of truth for `_do_batch`) |
| `INPUT_FLIP`   | `bi_flip`     | BoolValueCommandInput   | Flip direction (seeded from joint) |
| `INPUT_PREVIEW`| `bi_preview`  | BoolValueCommandInput   | Gate for live preview              |

## Help file (F1 / Help button)

`Command.helpFile` (set on `args.command` in `_CreatedHandler`, not on the
`CommandDefinition`) must be a **local file path** — Fusion rejects a bare URL.
It shows a Help button in the dialog's lower-left corner and backs the F1
shortcut; Fusion opens it with whatever the OS associates with the file type
(browser for `.html`). Points at `resources/help/help.html`, which is the same
file copied to the root of the App Store submission zip.

## executePreview behaviour (critical)

When `_PreviewHandler` sets `args.isValidResult = True`, Fusion **commits that result on OK
and does NOT fire `execute`**. This means all instance creation must happen inside
`_PreviewHandler` (via `_do_batch(args, silent=True)`), not in `_ExecuteHandler`.

When preview is off (`prev.value = False`), `_PreviewHandler` immediately returns
`isValidResult = False` (no-op). Fusion then calls `_ExecuteHandler` on OK.

Never create instances inside `executePreview` for assemblies with many targets — Fusion
will crash. The Preview checkbox is intentionally opt-in with a warning label.

## JointGeometry vs JointOrigin

`joint.geometryOrOriginOne/Two` returns either type depending on how the joint was created:

- Named Joint Origin → returns `JointOrigin`
- Implicit geometry (face/edge/point) → returns `JointGeometry`

`_joint_sides()` returns the raw object; `_make_geo1()` and `_find_similar_targets()` must
handle both types.

### Proxied vs native objects

Fusion wraps BRep / JO objects in occurrence context when accessed through assemblies.
Calling `.geometry` on a proxied `JointOrigin` throws `InternalValidationError`.
Always use `nativeObject` before accessing geometry properties:

```python
native = getattr(jo, 'nativeObject', None) or jo
axis   = native.geometry.primaryAxisVector   # safe
```

### JointGeometry staleness

`JointGeometry` objects returned by `joint.geometryOrOriginX` are transient references
tied to the current model state. After `root.joints.add()` modifies the model, any cached
`JointGeometry` reference is stale and will produce incorrect results. The fix: re-fetch
`comp_geo` via `_joint_sides()` at the start of every loop iteration.

## Select Similar logic

`_find_similar_targets()`:
1. Extracts `ref_axis` from the reference `JointOrigin` or `JointGeometry`.
2. Searches `target_comp.jointOrigins` for matching axis direction (fast path).
3. If no named origins found, falls back to `_find_matching_circular_edges()`.

### Target list is the single source of truth

`INPUT_TARGETS` (`bi_targets`) is not manual-mode-only: in Select-similar mode it is
auto-populated with every match, and stays editable so unwanted matches can be removed
(via the list's own remove control, or ctrl-click in the viewport). `_do_batch()` always
reads targets from this list — it never re-runs `_find_similar_targets()` at execute
time — so what's highlighted/listed in the dialog is exactly what gets placed, in both
modes. `_ValidateHandler` requires `tgt.selectionCount >= 1` in both modes for the same
reason.

Two distinct refresh paths exist and must not be conflated:
- `_refresh_similar(inputs)` — runs the full match search and *replaces* the list's
  contents. Called on source change or when "Select similar" is first ticked. Never call
  this in response to a `INPUT_TARGETS` change — it would silently undo the user's edit.
- `_sync_targets_display(inputs)` — re-derives the info text and rings from whatever is
  *currently* in the list, without searching. Called after the user edits the list, and
  when re-enabling the dialog after a real-instance preview.

Populating the list programmatically (`tgt_sel.addSelection(...)` in a loop) can itself
trigger nested `inputChanged` events for `INPUT_TARGETS` — Fusion doesn't only fire that
event for user-driven edits. The module-level `_populating_targets` flag guards against
this; without it, a large match list would trigger one nested redraw per entry instead
of the single one at the end of the populate loop.

Named `JointOrigin` matches must be re-contextualized via `createForAssemblyContext`
before `addSelection()` (see `_to_selectable()`) — unlike the ring-drawing path, which
only needs `nativeObject` + a manual transform, a real selection has to be an in-context
proxy or Fusion won't accept it as a picked entity.

**Gotcha:** not every `CommandInput` property is safe to set on every input type — e.g.
renaming a `SelectionCommandInput` via `.name` can throw depending on the Fusion version.
Because `_InputChangedHandler.notify()` wraps everything in a single `except: pass` (to
never crash the live dialog), an uncaught exception from a cosmetic property set can
silently abort the rest of the handler — including the refresh call that was about to
run. Wrap any non-essential property set in its own try/except rather than trusting the
outer catch-all; it hides failures rather than isolating them.

`_find_matching_circular_edges()`:
- Iterates all `BRepEdge`s in all bodies of the target component.
- Filters to circular edges whose normal matches `ref_axis` within `_SIMILAR_DIR_TOL` (cos ~1.3°).
- Groups edges by their **perpendicular-plane centre** (position projected onto the plane
  perpendicular to the axis) — so multiple coaxial edges on the same hole count as one hole.
- Picks one edge per hole: the one whose depth along the axis is closest to `ref_origin`
  (same face level as the reference joint).
- Skips edges whose `nativeObject.entityToken` is already used in an existing joint.

## Yellow highlight rings

`_refresh_similar(inputs)` is the single entry point — calls `_find_similar_targets()` once
and uses the result for both the info text and `_draw_target_highlights()`.
Do not call `_find_similar_targets()` separately for info text; it is an expensive BRep scan.

`_draw_target_highlights(targets, root)` creates a `CustomGraphicsGroup` on `rootComponent`
and draws a 24-segment polygon ring at each target's world-space centre/normal/radius.
The group reference is stored in `_cg_group`; `_clear_target_highlights()` deletes it.
`_DestroyHandler` calls `_clear_target_highlights()` on command close.

## Error handling

Fusion's `joints.add()` exception message includes a cascade of all joints that failed
to recompute (e.g. *"3 : Failed to create component : Rigid 37 / Compute Failed…"*).
These are usually pre-existing broken joints in the assembly, not caused by our code.
`_summarise_error()` trims the message to the primary failure + a count of sub-failures.
`_errors_are_compute_failures()` detects when all errors are assembly health issues and
adds a hint to check the timeline.
