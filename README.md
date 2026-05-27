# FusionBatchInsert

A Fusion 360 add-in that places multiple jointed copies of a component across a hole pattern in a single operation.

Fusion's built-in **Duplicate with Joint** command creates one copy at a time. When you need 50 or 200 copies of a washer, bolt, or any other jointed part across a regular hole pattern, this add-in does it in one click.

---

## Features

- **Select Similar** — auto-detects all holes on the target component whose axis direction matches the reference joint. Yellow rings highlight the found locations before you commit.
- **Manual targeting** — select individual joint origins or circular hole edges when you want fine-grained control.
- **Flip direction** — toggle the joint flip before committing, matching the orientation you need.
- **Optional live preview** — tick "Preview placement" to render all instances in the viewport before clicking OK. Labelled with a warning because it can be slow on assemblies with hundreds of holes.
- **One undo step** — all instances created in one execute call, so a single Ctrl+Z reverses everything.
- **Supports both named Joint Origins and implicit geometry** — works whether your joints were created from named origin markers or directly from faces and circular edges.

---

## Requirements

- Autodesk Fusion 360 (any recent version supporting the Fusion 360 API)
- Windows 10/11 or macOS

---

## Installation

### Windows

1. Download or clone this repository.
2. Create a directory junction from the Fusion 360 add-ins folder to the bundle:

```powershell
cmd /c mklink /J "%APPDATA%\Autodesk\ApplicationPlugins\FusionBatchInsert.bundle" "C:\path\to\FusionBatchInsert.bundle"
```

Replace `C:\path\to\` with the actual path to the cloned repository.

3. Restart Fusion 360. The **Batch Insert** button appears in the **Assemble** drop-down panel in the Design workspace.

### macOS

1. Download or clone this repository.
2. Create a symbolic link:

```bash
ln -s /path/to/FusionBatchInsert.bundle \
  "$HOME/Library/Application Support/Autodesk/ApplicationPlugins/FusionBatchInsert.bundle"
```

3. Restart Fusion 360.

### Verify the installation

Go to **Utilities → Add-Ins**. FusionBatchInsert should appear in the list with version **1.1.0**.

---

## Usage

### Select Similar mode (default)

1. Click **Batch Insert** in the Assemble panel.
2. Pick the component that already has a joint (*Source component*). Yellow rings immediately appear on all matching unoccupied holes.
3. The info box shows how many matches were found (e.g. *"Found 47 matching joint origins on 'Baseplate'"*).
4. Optionally tick **Flip direction** if the orientation is wrong.
5. Optionally tick **Preview placement** (⚠ slow on large assemblies) to see the actual instances before committing.
6. Click **OK**.

### Manual mode

1. Untick **Select similar**.
2. Pick the source component, then pick each target joint origin or circular hole edge individually.
3. Optionally adjust Flip / Preview.
4. Click **OK**.

### Tips

- The source component must already have at least one joint defined. Create a single joint manually first, then use Batch Insert for the rest.
- If the assembly has existing joint compute errors (red/yellow markers in the timeline), fix those before running Batch Insert. Adding a new joint triggers a full recompute and will surface any pre-existing failures.
- For assemblies with many hundreds of holes, skip the Preview checkbox and click OK directly. The yellow rings already show where copies will land.

---

## How it works

When **Select Similar** is active the add-in:

1. Reads the reference joint's axis direction from the source component.
2. Searches the target component for named Joint Origins first (fast path), then falls back to a BRep circular-edge scan (hole pattern detection).
3. Groups edges by their perpendicular-plane centre so that only one edge per physical hole is selected, at the same face depth as the reference joint.
4. Draws non-destructive yellow `CustomGraphics` rings at every found location.
5. On OK, iterates the target list and calls `root.occurrences.addExistingComponent` + `root.joints.add` for each — all within a single command execute, giving one undo step.

---

## Changelog

### 1.1.0
- Improved error messages: Fusion cascade-failure dumps are condensed to one line with a count, and actionable guidance is shown when all copies fail due to assembly compute errors.
- Preview checkbox restored with warning label: *"⚠ may be slow on large assemblies"*.
- `_find_similar_targets()` is now called once per source pick (previously called twice, which caused performance issues on large assemblies).
- Null-guard improvements in the validate handler.

### 1.0.8
- Preview checkbox temporarily removed in favour of yellow-ring-only feedback (reverted in 1.1.0).

### 1.0.7
- Yellow highlight rings (`CustomGraphics`) show matched hole locations in Select Similar mode.
- Validate handler simplified: no longer runs the BRep scan on every validate event (fixes greyed-out OK button on assemblies with many holes).
- Phase-gate Preview checkbox: live preview is opt-in to avoid rebuilding 100+ instances on every input change.

### 1.0.6
- `_find_matching_circular_edges()` fallback: Select Similar now works on plain hole patterns with no named Joint Origins.

### 1.0.5
- Fixed: only the first instance was placed in manual multi-target mode. `JointGeometry` transients become stale after `joints.add()` modifies the model; the fix re-fetches `comp_geo` at the start of each loop iteration.

### 1.0.4
- Fixed: `InternalValidationError: targetObj` in Select Similar — caused by calling `.geometry` on a proxied `JointOrigin`. Fixed by stripping to `nativeObject` first.

### 1.0.3
- `executePreview` + `isValidResult=True` architecture: preview commits on OK without a second execute call. All instance creation unified in `_do_batch`.

### 1.0.2
- Support for implicit joint geometry (faces, circular edges) in addition to named Joint Origins.

### 1.0.1
- Fixed `RootJointOrigins` invalid filter crash on command open.

### 1.0.0
- Initial release: basic batch insert via named Joint Origins, Select Similar checkbox, manual target selection, Flip direction.

---

## License

MIT License — © 2026 Danecca Ltd
