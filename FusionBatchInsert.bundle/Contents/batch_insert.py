"""Batch Insert command.

Workflow:
  1. User picks a component that already has a joint ("Source component").
  2a. "Select similar" OFF (default): user manually picks joint origins / circular
      hole edges / sketch points/circles into the "Target locations" list.
  2b. "Select similar" ON: yellow rings highlight all matching holes and the same
      "Target locations" list is auto-populated with every match found. The user
      can remove entries they don't want (ctrl-click in the viewport, or the
      list's own remove control) before clicking OK — whatever remains in the
      list is exactly what gets placed.
  3. Optionally tick "Flip direction" before clicking OK.
  4. Click OK — one new occurrence per remaining target is created and jointed
     in one undo step.
"""

from __future__ import annotations

import json
import math
import os
import traceback
from typing import List, Optional, Tuple

import adsk.core
import adsk.fusion

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMMAND_ID      = "FusionBatchInsert_Command"
COMMAND_NAME    = "Batch Insert"
COMMAND_TOOLTIP = (
    "Select a jointed component, then place copies at matching joint origins. "
    "Use 'Select similar' to auto-detect all matching holes, or pick targets manually."
)

INPUT_SOURCE   = "bi_source"
INPUT_SIMILAR  = "bi_similar"
INPUT_INFO     = "bi_info"
INPUT_TARGETS  = "bi_targets"
INPUT_FLIP     = "bi_flip"
INPUT_PREVIEW  = "bi_preview"

_SIMILAR_DIR_TOL = 0.9998  # cos(~1.3°) — axis-direction match tolerance

_retained: List[adsk.core.EventHandler] = []
_cg_group: Optional[adsk.fusion.CustomGraphicsGroup] = None  # live target highlights

# Guards the bulk clearSelection()/addSelection() loop in _refresh_similar:
# Fusion can fire inputChanged for programmatic selection edits too, and
# without this a large match list would trigger one nested redraw per
# addSelection() call instead of the single one at the end of the loop.
_populating_targets = False


def _res_folder() -> str:
    return os.path.join(os.path.dirname(__file__), "resources", "BatchInsert")


def _read_version() -> str:
    try:
        manifest = os.path.join(os.path.dirname(__file__), "FusionBatchInsert.manifest")
        with open(manifest, "r") as f:
            return json.load(f).get("version", "?")
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(
    ui: adsk.core.UserInterface,
    panel: adsk.core.ToolbarPanel,
    handlers: list,
) -> None:
    cmd_def = ui.commandDefinitions.itemById(COMMAND_ID)
    if cmd_def is None:
        cmd_def = ui.commandDefinitions.addButtonDefinition(
            COMMAND_ID, COMMAND_NAME,
            f"v{_read_version()} — {COMMAND_TOOLTIP}",
            _res_folder(),
        )

    on_created = _CreatedHandler()
    cmd_def.commandCreated.add(on_created)
    handlers.append(on_created)
    _retained.append(on_created)

    ctrl = panel.controls.itemById(COMMAND_ID)
    if ctrl is None:
        ctrl = panel.controls.addCommand(cmd_def)
    ctrl.isVisible = True
    # Don't auto-promote to the toolbar top level — that's a user choice
    # (Marketplace guideline: "Users can promote frequently-used commands
    # to the top level of the toolbar themselves").


def unregister(
    ui: adsk.core.UserInterface,
    panel: adsk.core.ToolbarPanel,
) -> None:
    ctrl = panel.controls.itemById(COMMAND_ID)
    if ctrl:
        ctrl.deleteMe()
    cmd_def = ui.commandDefinitions.itemById(COMMAND_ID)
    if cmd_def:
        cmd_def.deleteMe()
    _retained.clear()


# ---------------------------------------------------------------------------
# Target highlight graphics (yellow rings shown in Select-similar mode)
# ---------------------------------------------------------------------------

_HIGHLIGHT_YELLOW = (255, 204, 0, 255)  # RGBA
_RING_SEGS = 24                          # polygon approximation for each ring


def _clear_target_highlights() -> None:
    global _cg_group
    try:
        if _cg_group is not None and _cg_group.isValid:
            _cg_group.deleteMe()
            adsk.core.Application.get().activeViewport.refresh()
    except Exception:
        pass
    _cg_group = None


def _draw_target_highlights(
    targets: List[Tuple],
    root: adsk.fusion.Component,
) -> None:
    """Draw yellow rings for a pre-computed targets list. Caller clears first."""
    global _cg_group
    try:
        app    = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return

        cg_group = root.customGraphicsGroups.add()
        _cg_group = cg_group

        yellow = adsk.fusion.CustomGraphicsSolidColorEffect.create(
            adsk.core.Color.create(*_HIGHLIGHT_YELLOW)
        )

        for target_entity, t_occ in targets:
            try:
                center, normal, radius = _entity_world_cnr(target_entity, t_occ)
                if center is None:
                    continue
                _add_ring(cg_group, center, normal, radius, yellow)
            except Exception:
                continue

        app.activeViewport.refresh()
    except Exception:
        pass


def _to_selectable(entity, target_occ: Optional[adsk.fusion.Occurrence]):
    """Return an in-context proxy suitable for SelectionCommandInput.addSelection."""
    if target_occ is None:
        return entity
    try:
        return entity.createForAssemblyContext(target_occ)
    except Exception:
        return entity


def _refresh_similar(inputs: adsk.core.CommandInputs) -> None:
    """
    Runs the full Select-similar match search and (re)populates the "Target
    locations" selection input with every match found, replacing whatever was
    there before. Called when the source changes or "Select similar" is first
    turned on — NOT on every target-list edit (see _sync_targets_display),
    otherwise the user's manual deselections would be wiped out.
    """
    _clear_target_highlights()
    try:
        src_sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(INPUT_SOURCE))
        tgt_sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(INPUT_TARGETS))
        info    = adsk.core.TextBoxCommandInput.cast(inputs.itemById(INPUT_INFO))

        if src_sel is None or src_sel.selectionCount != 1:
            if tgt_sel:
                tgt_sel.clearSelection()
            if info:
                info.formattedText = (
                    "Pick a source component to detect matching joint origins."
                )
            return

        app    = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return

        occ   = adsk.fusion.Occurrence.cast(src_sel.selection(0).entity)
        root  = design.rootComponent
        joint = _find_joint(root, occ)

        if joint is None:
            if tgt_sel:
                tgt_sel.clearSelection()
            if info:
                info.formattedText = (
                    "No joint found on this component — create a joint first."
                )
            return

        _, asm_geo, target_occ = _joint_sides(joint, occ)
        targets = _find_similar_targets(asm_geo, target_occ, root)
        n = len(targets)

        global _populating_targets
        if tgt_sel:
            _populating_targets = True
            try:
                tgt_sel.clearSelection()
                for entity, t_occ in targets:
                    try:
                        tgt_sel.addSelection(_to_selectable(entity, t_occ))
                    except Exception:
                        pass
            finally:
                _populating_targets = False

        if info:
            if n == 0:
                info.formattedText = (
                    "No unoccupied matching joint origins found on the target component."
                )
            else:
                comp_name = (
                    target_occ.component.name if target_occ else "root component"
                )
                info.formattedText = (
                    f"Found {n} matching joint origin(s) on '{comp_name}'. "
                    "Remove any you don't need from the list below, then click OK."
                )

        if targets:
            _draw_target_highlights(targets, root)

    except Exception:
        pass


def _sync_targets_display(inputs: adsk.core.CommandInputs) -> None:
    """
    Re-derive the info text and yellow highlight rings from whatever is
    CURRENTLY in the "Target locations" list, without re-running the match
    search. Used after the user edits the auto-populated list (add/remove a
    target) in Select-similar mode, and when re-enabling the dialog after a
    real-instance preview — either way the existing selections must be
    preserved, not overwritten by a fresh _find_similar_targets() scan.
    """
    try:
        src_sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(INPUT_SOURCE))
        tgt_sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(INPUT_TARGETS))
        info    = adsk.core.TextBoxCommandInput.cast(inputs.itemById(INPUT_INFO))

        _clear_target_highlights()
        if src_sel is None or src_sel.selectionCount != 1 or tgt_sel is None:
            return

        app    = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return

        occ   = adsk.fusion.Occurrence.cast(src_sel.selection(0).entity)
        root  = design.rootComponent
        joint = _find_joint(root, occ)
        if joint is None:
            return

        _, _, target_occ = _joint_sides(joint, occ)
        targets = [
            (tgt_sel.selection(i).entity, target_occ)
            for i in range(tgt_sel.selectionCount)
        ]
        n = len(targets)

        if info:
            if n == 0:
                info.formattedText = (
                    "No targets selected — every match was removed from the list. "
                    "Ctrl-click matches in the viewport to add some back."
                )
            else:
                comp_name = (
                    target_occ.component.name if target_occ else "root component"
                )
                info.formattedText = (
                    f"{n} target(s) selected on '{comp_name}'. Click OK to place all copies."
                )

        if targets:
            _draw_target_highlights(targets, root)

    except Exception:
        pass


def _entity_world_cnr(target_entity, target_occ):
    """Return (Point3D center, Vector3D normal, float radius) in world space."""
    tf = target_occ.transform if target_occ is not None else None

    jo = adsk.fusion.JointOrigin.cast(target_entity)
    if jo is not None:
        try:
            native = getattr(jo, 'nativeObject', None) or jo
            geom   = native.geometry
            center = geom.origin.copy()
            normal = geom.primaryAxisVector.copy()
            radius = 0.3  # 3 mm default ring for joint origins
            if tf is not None:
                center.transformBy(tf)
                normal.transformBy(tf)
                normal.normalize()
            return center, normal, radius
        except Exception:
            return None, None, None

    edge = adsk.fusion.BRepEdge.cast(target_entity)
    if edge is not None:
        try:
            native_e = getattr(edge, 'nativeObject', None) or edge
            circle   = adsk.core.Circle3D.cast(native_e.geometry)
            if circle is None:
                return None, None, None
            center = circle.center.copy()
            normal = circle.normal.copy()
            radius = circle.radius
            if tf is not None:
                center.transformBy(tf)
                normal.transformBy(tf)
                normal.normalize()
            return center, normal, radius
        except Exception:
            return None, None, None

    return None, None, None


def _add_ring(cg_group, center, normal, radius, color_effect) -> None:
    """Draw a closed polygon ring at the given center / normal direction."""
    ref = adsk.core.Vector3D.create(0.0, 0.0, 1.0)
    if abs(normal.dotProduct(ref)) > 0.999:
        ref = adsk.core.Vector3D.create(1.0, 0.0, 0.0)
    u = normal.crossProduct(ref)
    u.normalize()
    v = normal.crossProduct(u)
    v.normalize()

    coords: List[float] = []
    for i in range(_RING_SEGS):
        a = 2.0 * math.pi * i / _RING_SEGS
        ca, sa = math.cos(a), math.sin(a)
        coords += [
            center.x + radius * (ca * u.x + sa * v.x),
            center.y + radius * (ca * u.y + sa * v.y),
            center.z + radius * (ca * u.z + sa * v.z),
        ]

    cg_coords = adsk.fusion.CustomGraphicsCoordinates.create(coords)
    line = cg_group.addLines(cg_coords, list(range(_RING_SEGS)), [True])
    line.color = color_effect
    line.weight = 2.0


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

class _CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args: adsk.core.CommandCreatedEventArgs) -> None:
        try:
            cmd    = args.command
            inputs = cmd.commandInputs

            try:
                cmd.setDialogMinimumSize(420, 50)
            except Exception:
                pass

            # 1. Source component
            src = inputs.addSelectionInput(
                INPUT_SOURCE,
                "Source component",
                "Pick the component that already has a joint defined",
            )
            src.setSelectionLimits(1, 1)
            src.addSelectionFilter("Occurrences")

            # 2. Select-similar checkbox (off by default; manual selection is expected)
            inputs.addBoolValueInput(INPUT_SIMILAR, "Select similar", True, "", False)

            # 3. Info text (shown in Select-similar mode)
            info = inputs.addTextBoxCommandInput(
                INPUT_INFO, "",
                "Pick a source component to detect matching joint origins.",
                2, True,
            )
            info.isVisible = False

            # 4. Target selection. Always visible: in manual mode the user
            #    builds this list by hand; in Select-similar mode it is
            #    auto-populated with every match (see _refresh_similar) and
            #    stays editable so unwanted matches can be removed.
            tgt = inputs.addSelectionInput(
                INPUT_TARGETS,
                "Target locations",
                "Pick joint origins, circular hole edges, or sketch points/circles where copies will be placed",
            )
            tgt.setSelectionLimits(0, 0)  # min=0: Fusion enforces it otherwise even when disabled
            for _f in ("JointOrigins", "CircularEdges",
                       "SketchPoints", "SketchCurves", "SketchCircles", "SketchArcs"):
                try:
                    tgt.addSelectionFilter(_f)
                except Exception:
                    pass
            tgt.isEnabled = False
            tgt.isVisible = True

            # 5. Flip direction
            flip = inputs.addBoolValueInput(INPUT_FLIP, "Flip direction", True, "", False)
            flip.isEnabled = False

            # 6. Preview — off by default; disabled until selection is ready.
            #    Warning is intentional: creating 100+ instances in preview is slow.
            prev = inputs.addBoolValueInput(
                INPUT_PREVIEW,
                "Preview placement  ⚠ may be slow on large assemblies",
                True, "", False,
            )
            prev.isEnabled = False

            on_changed = _InputChangedHandler()
            cmd.inputChanged.add(on_changed)
            _retained.append(on_changed)

            on_validate = _ValidateHandler()
            cmd.validateInputs.add(on_validate)
            _retained.append(on_validate)

            on_preview = _PreviewHandler()
            cmd.executePreview.add(on_preview)
            _retained.append(on_preview)

            on_execute = _ExecuteHandler()
            cmd.execute.add(on_execute)
            _retained.append(on_execute)

            on_destroy = _DestroyHandler()
            cmd.destroy.add(on_destroy)
            _retained.append(on_destroy)

        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                traceback.format_exc(), COMMAND_NAME
            )


class _InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args: adsk.core.InputChangedEventArgs) -> None:
        try:
            inputs  = args.inputs
            cid     = args.input.id

            src     = adsk.core.SelectionCommandInput.cast(inputs.itemById(INPUT_SOURCE))
            similar = adsk.core.BoolValueCommandInput.cast(inputs.itemById(INPUT_SIMILAR))
            tgt     = adsk.core.SelectionCommandInput.cast(inputs.itemById(INPUT_TARGETS))
            flip    = adsk.core.BoolValueCommandInput.cast(inputs.itemById(INPUT_FLIP))
            prev    = adsk.core.BoolValueCommandInput.cast(inputs.itemById(INPUT_PREVIEW))

            use_similar = (similar is not None and similar.value)
            in_preview  = (prev is not None and prev.value)
            has_source  = (src is not None and src.selectionCount == 1)

            # --- Select-similar / manual toggle ---
            if cid == INPUT_SIMILAR:
                if inputs.itemById(INPUT_INFO):
                    inputs.itemById(INPUT_INFO).isVisible = use_similar
                if tgt:
                    try:
                        tgt.name = (
                            "Matched targets (remove any you don't need)"
                            if use_similar else "Target locations"
                        )
                    except Exception:
                        pass
                    if not use_similar:
                        tgt.clearSelection()
                if use_similar and has_source and not in_preview:
                    _refresh_similar(inputs)
                else:
                    _clear_target_highlights()

            # --- Source changed ---
            if cid == INPUT_SOURCE:
                # Seed flip from the existing joint
                if has_source and flip:
                    try:
                        app    = adsk.core.Application.get()
                        design = adsk.fusion.Design.cast(app.activeProduct)
                        occ    = adsk.fusion.Occurrence.cast(src.selection(0).entity)
                        joint  = _find_joint(design.rootComponent, occ)
                        if joint:
                            flip.value = joint.isFlipped
                    except Exception:
                        pass
                if flip:
                    flip.isEnabled = has_source

                if not has_source:
                    if tgt:
                        tgt.clearSelection()
                    _clear_target_highlights()
                elif use_similar and not in_preview:
                    _refresh_similar(inputs)
                elif not use_similar and tgt and not in_preview:
                    tgt.hasFocus = True

            # --- Target list edited (either mode): redraw rings / info from
            #     whatever is now selected, without re-running the match search ---
            if cid == INPUT_TARGETS and use_similar and not in_preview and not _populating_targets:
                _sync_targets_display(inputs)

            # --- Preview checkbox toggled ---
            if cid == INPUT_PREVIEW:
                if in_preview:
                    # Lock selection; real instances will replace the rings
                    if src:     src.isEnabled     = False
                    if similar: similar.isEnabled  = False
                    if tgt:     tgt.isEnabled      = False
                    if flip:    flip.isEnabled      = True
                    _clear_target_highlights()
                else:
                    # Unlock selection. Redraw from the EXISTING target list
                    # rather than re-running Select-similar, which would wipe
                    # out any entries the user removed before previewing.
                    if src:     src.isEnabled     = True
                    if similar: similar.isEnabled  = True
                    if use_similar and has_source:
                        _sync_targets_display(inputs)

            # --- Uniform enable/disable sync, run after any of the above ---
            if tgt:
                tgt.isEnabled = has_source and not in_preview
            if prev:
                prev.isEnabled = (
                    has_source and not in_preview
                    and tgt is not None and tgt.selectionCount >= 1
                )

        except Exception:
            pass  # never crash the live dialog


class _ValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args: adsk.core.ValidateInputsEventArgs) -> None:
        try:
            inputs = args.inputs
            src    = adsk.core.SelectionCommandInput.cast(inputs.itemById(INPUT_SOURCE))
            tgt    = adsk.core.SelectionCommandInput.cast(inputs.itemById(INPUT_TARGETS))

            if src is None or src.selectionCount != 1:
                args.areInputsValid = False
                return

            # Whether populated by Select-similar or picked manually, the
            # target list is the single source of truth for what gets placed.
            args.areInputsValid = (tgt is not None and tgt.selectionCount >= 1)

        except Exception:
            args.areInputsValid = False


class _PreviewHandler(adsk.core.CommandEventHandler):
    """
    Runs the full batch as a live preview ONLY when the user has checked
    'Preview placement'. When unchecked this is a no-op so that Fusion never
    builds instances on routine input changes (avoids the performance hit).
    When isValidResult=True Fusion commits this result on OK without calling execute.
    """
    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        try:
            prev = adsk.core.BoolValueCommandInput.cast(
                args.command.commandInputs.itemById(INPUT_PREVIEW)
            )
            if not (prev and prev.value):
                args.isValidResult = False
                return
        except Exception:
            args.isValidResult = False
            return
        _do_batch(args, silent=True)


class _ExecuteHandler(adsk.core.CommandEventHandler):
    """Fires on OK when preview was not active (or as fallback)."""
    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        _do_batch(args, silent=False)


class _DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        _clear_target_highlights()


def _summarise_error(copy_num: int, exc: Exception) -> str:
    """
    Condense Fusion's cascade-failure dumps into one readable line.
    Fusion's joint-add errors look like:
      "3 : Failed to create component : Rigid 37 / Compute Failed // INVALID_OPERANTS…"
    We show the first meaningful segment and hide the per-joint noise.
    """
    raw = str(exc)
    # Take everything before the first cascade entry ("Rigid N /", "Sketch /", etc.)
    import re
    trimmed = re.split(r'\s*[A-Za-z]+\d+\s*/\s*', raw, maxsplit=1)[0].strip(' :')
    if not trimmed:
        trimmed = raw.splitlines()[0]
    if len(trimmed) > 180:
        trimmed = trimmed[:177] + "…"
    # Count how many sub-failures were folded in
    n_sub = len(re.findall(r'Compute Failed', raw))
    suffix = f"  ({n_sub} joint(s) failed to recompute)" if n_sub else ""
    return f"Copy {copy_num}: {trimmed}{suffix}"


def _errors_are_compute_failures(errors: List[str]) -> bool:
    """True when every error looks like a Fusion assembly recompute failure."""
    keywords = ("compute failed", "invalid_operants", "invalid operands",
                "failed to create component", "project_source_lost")
    return all(
        any(k in e.lower() for k in keywords)
        for e in errors
    )


def _do_batch(args: adsk.core.CommandEventArgs, *, silent: bool = False) -> None:
    """
    Core batch-insert logic shared by preview and execute.
    silent=True  → no message boxes (live-preview path).
    silent=False → show summary / errors after placement.
    Sets args.isValidResult=True when at least one instance is placed.
    """
    try:
        app    = adsk.core.Application.get()
        ui     = app.userInterface
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return

        root   = design.rootComponent
        inputs = args.command.commandInputs

        src_sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(INPUT_SOURCE))
        tgt_sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(INPUT_TARGETS))
        flip    = adsk.core.BoolValueCommandInput.cast(inputs.itemById(INPUT_FLIP))

        if src_sel is None or src_sel.selectionCount < 1:
            return

        source_occ = adsk.fusion.Occurrence.cast(src_sel.selection(0).entity)
        joint      = _find_joint(root, source_occ)
        if joint is None:
            ui.messageBox(
                "No joint found on the selected component.", COMMAND_NAME
            )
            return

        _, _, target_occ = _joint_sides(joint, source_occ)
        joint_type = joint.jointMotion.jointType
        is_flipped = (
            flip.value if (flip is not None and flip.isEnabled) else joint.isFlipped
        )

        # The target list is the single source of truth for what gets placed,
        # whether it was auto-populated by Select-similar (and possibly
        # trimmed by the user) or built manually.
        targets = [
            (tgt_sel.selection(i).entity, target_occ)
            for i in range(tgt_sel.selectionCount)
        ] if tgt_sel is not None else []

        if not targets:
            args.isValidResult = False
            if not silent:
                ui.messageBox(
                    "No matching joint origins found. Nothing was placed.", COMMAND_NAME
                )
            return

        created = 0
        errors: List[str] = []

        for i, (target_entity, t_occ) in enumerate(targets):
            try:
                # Re-fetch comp_geo each iteration: JointGeometry transients
                # become stale once root.joints.add() modifies the model.
                comp_geo_i, _, _ = _joint_sides(joint, source_occ)
                _create_instance(
                    root, source_occ, comp_geo_i,
                    is_flipped, joint_type,
                    target_entity, t_occ,
                )
                created += 1
            except Exception as exc:
                errors.append(_summarise_error(i + 1, exc))

        args.isValidResult = (created > 0)

        if not silent:
            msg = f"Batch Insert complete: {created} of {len(targets)} instance(s) placed."
            if errors:
                msg += "\n\nErrors:\n" + "\n".join(errors[:10])
                if len(errors) > 10:
                    msg += f"\n… and {len(errors) - 10} more."
                if created == 0 and _errors_are_compute_failures(errors):
                    msg += (
                        "\n\nThe assembly has joint compute errors (shown above). "
                        "Open the timeline, fix any red/yellow markers, then retry."
                    )
            ui.messageBox(msg, COMMAND_NAME)

    except Exception:
        args.isValidResult = False
        if not silent:
            adsk.core.Application.get().userInterface.messageBox(
                traceback.format_exc(), COMMAND_NAME
            )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _find_joint(
    root: adsk.fusion.Component,
    occ: adsk.fusion.Occurrence,
) -> Optional[adsk.fusion.Joint]:
    """Return the first joint that involves occ, or None."""
    token = occ.entityToken
    for j in root.joints:
        occ1 = j.occurrenceOne
        occ2 = j.occurrenceTwo
        if (occ1 and occ1.entityToken == token) or \
           (occ2 and occ2.entityToken == token):
            return j
    return None


def _joint_sides(
    joint: adsk.fusion.Joint,
    source_occ: adsk.fusion.Occurrence,
) -> Tuple:
    """
    Return (comp_geo_or_origin, asm_geo_or_origin, target_occurrence_or_None).
    comp side = the part being inserted; asm side = what it was jointed to.
    """
    occ1 = joint.occurrenceOne
    if occ1 and occ1.entityToken == source_occ.entityToken:
        return (
            joint.geometryOrOriginOne,
            joint.geometryOrOriginTwo,
            joint.occurrenceTwo,
        )
    return (
        joint.geometryOrOriginTwo,
        joint.geometryOrOriginOne,
        joint.occurrenceOne,
    )


def _find_similar_targets(
    asm_geo,
    target_occ: Optional[adsk.fusion.Occurrence],
    root: adsk.fusion.Component,
) -> List[Tuple]:
    """
    Find all unoccupied holes on the target component that share the same axis
    direction as asm_geo.  Searches named JointOrigins first; falls back to
    BRep circular edges when the target has no named origins (plain hole pattern).
    """
    ref_axis:   Optional[adsk.core.Vector3D] = None
    ref_origin: Optional[adsk.core.Point3D]  = None
    used_jo = _used_joint_origin_tokens(root)

    ref_jo = adsk.fusion.JointOrigin.cast(asm_geo)
    if ref_jo is not None:
        try:
            native = getattr(ref_jo, 'nativeObject', None) or ref_jo
            ref_axis = native.geometry.primaryAxisVector
        except Exception:
            pass
        used_jo.add(ref_jo.entityToken)
    else:
        jg = adsk.fusion.JointGeometry.cast(asm_geo)
        if jg is not None:
            try:
                ref_axis = jg.primaryAxisVector
            except Exception:
                pass
            try:
                ref_origin = jg.origin
            except Exception:
                pass

    if ref_axis is None:
        return []

    target_comp = target_occ.component if target_occ else root
    results: List[Tuple] = []

    # Named joint origins (fast path)
    for jo in target_comp.jointOrigins:
        if jo.entityToken in used_jo:
            continue
        try:
            if _same_direction(ref_axis, jo.geometry.primaryAxisVector):
                results.append((jo, target_occ))
        except Exception:
            continue

    # BRep circular edges (fallback when no named origins exist)
    if not results:
        used_edges = _used_joint_edge_tokens(root)
        results = _find_matching_circular_edges(
            ref_axis, ref_origin, target_comp, target_occ, used_edges
        )

    return results


def _used_joint_origin_tokens(root: adsk.fusion.Component) -> set:
    used = set()
    for j in root.joints:
        for geo in (j.geometryOrOriginOne, j.geometryOrOriginTwo):
            jo = adsk.fusion.JointOrigin.cast(geo)
            if jo:
                used.add(jo.entityToken)
    return used


def _used_joint_edge_tokens(root: adsk.fusion.Component) -> set:
    used = set()
    for j in root.joints:
        for geo in (j.geometryOrOriginOne, j.geometryOrOriginTwo):
            jg = adsk.fusion.JointGeometry.cast(geo)
            if jg is not None:
                try:
                    e = jg.entityOne
                    if e is not None:
                        native_e = getattr(e, 'nativeObject', None) or e
                        used.add(native_e.entityToken)
                except Exception:
                    pass
    return used


def _find_matching_circular_edges(
    ref_axis:        adsk.core.Vector3D,
    ref_origin:      Optional[adsk.core.Point3D],
    target_comp:     adsk.fusion.Component,
    target_occ:      Optional[adsk.fusion.Occurrence],
    used_edge_tokens: set,
) -> List[Tuple]:
    """
    Return one BRep circular edge per hole on target_comp whose axis matches
    ref_axis.  Edges are grouped by perpendicular-plane centre; the one whose
    depth (along the axis) is closest to ref_origin is chosen per group.
    """
    groups: dict = {}  # (px, py, pz) -> [(depth, edge)]

    for body in target_comp.bRepBodies:
        for edge in body.edges:
            try:
                circle = adsk.core.Circle3D.cast(edge.geometry)
                if circle is None:
                    continue
                if not _same_direction(ref_axis, circle.normal):
                    continue
                native_edge = getattr(edge, 'nativeObject', None) or edge
                if native_edge.entityToken in used_edge_tokens:
                    continue
                c     = circle.center
                depth = ref_axis.x * c.x + ref_axis.y * c.y + ref_axis.z * c.z
                key   = (round(c.x - depth * ref_axis.x, 2),
                         round(c.y - depth * ref_axis.y, 2),
                         round(c.z - depth * ref_axis.z, 2))
                groups.setdefault(key, []).append((depth, edge))
            except Exception:
                continue

    if not groups:
        return []

    ref_depth = None
    if ref_origin is not None:
        ref_depth = (ref_axis.x * ref_origin.x +
                     ref_axis.y * ref_origin.y +
                     ref_axis.z * ref_origin.z)

    results: List[Tuple] = []
    for candidates in groups.values():
        if ref_depth is not None:
            candidates.sort(key=lambda x: abs(x[0] - ref_depth))
        _, best_edge = candidates[0]
        if target_occ is not None:
            try:
                best_edge = best_edge.createForAssemblyContext(target_occ)
            except Exception:
                pass
        results.append((best_edge, target_occ))

    return results


def _same_direction(
    v1: adsk.core.Vector3D,
    v2: adsk.core.Vector3D,
    tol: float = _SIMILAR_DIR_TOL,
) -> bool:
    try:
        u1, u2 = v1.copy(), v2.copy()
        u1.normalize()
        u2.normalize()
        return u1.dotProduct(u2) > tol
    except Exception:
        return False


def _apply_joint_motion(
    ji: adsk.fusion.JointInput,
    joint_type: int,
) -> None:
    JT = adsk.fusion.JointTypes
    JD = adsk.fusion.JointDirections
    try:
        if joint_type == JT.RigidJointType:
            ji.setAsRigidJointMotion()
        elif joint_type == JT.RevoluteJointType:
            ji.setAsRevoluteJointMotion(JD.ZAxisJointDirection)
        elif joint_type == JT.SliderJointType:
            ji.setAsSliderJointMotion(JD.ZAxisJointDirection)
        elif joint_type == JT.CylindricalJointType:
            ji.setAsCylindricalJointMotion(JD.ZAxisJointDirection)
        elif joint_type == JT.PinSlotJointType:
            ji.setAsPinSlotJointMotion(JD.ZAxisJointDirection, JD.XAxisJointDirection)
        elif joint_type == JT.PlanarJointType:
            ji.setAsPlanarJointMotion(JD.ZAxisJointDirection)
        else:
            ji.setAsRigidJointMotion()
    except Exception:
        ji.setAsRigidJointMotion()


def _target_to_geo2(
    target_entity,
    target_occ: Optional[adsk.fusion.Occurrence],
) -> adsk.fusion.JointGeometry:
    jo = adsk.fusion.JointOrigin.cast(target_entity)
    if jo is not None:
        if target_occ is not None:
            try:
                jo = jo.createForAssemblyContext(target_occ)
            except Exception:
                pass
        return adsk.fusion.JointGeometry.createByJointOrigin(jo)

    edge = adsk.fusion.BRepEdge.cast(target_entity)
    if edge is not None:
        return adsk.fusion.JointGeometry.createByCurve(
            edge, adsk.fusion.JointKeyPointTypes.CenterKeyPoint
        )

    sketch_pt = adsk.fusion.SketchPoint.cast(target_entity)
    if sketch_pt is not None:
        return adsk.fusion.JointGeometry.createByPoint(sketch_pt)

    sketch_curve = adsk.fusion.SketchCurve.cast(target_entity)
    if sketch_curve is not None:
        return adsk.fusion.JointGeometry.createByCurve(
            sketch_curve, adsk.fusion.JointKeyPointTypes.CenterKeyPoint
        )

    raise RuntimeError(
        f"Cannot create joint geometry from entity type '{target_entity.objectType}'. "
        "Select a joint origin, circular edge, sketch point, or sketch circle/arc."
    )


def _make_geo1(
    comp_geo,
    source_occ: adsk.fusion.Occurrence,
    new_occ: adsk.fusion.Occurrence,
) -> adsk.fusion.JointGeometry:
    # Named JointOrigin path (common case)
    jo = adsk.fusion.JointOrigin.cast(comp_geo)
    if jo is None:
        jg = adsk.fusion.JointGeometry.cast(comp_geo)
        if jg is not None:
            e1 = jg.entityOne
            if e1 is not None:
                jo = adsk.fusion.JointOrigin.cast(e1)

    if jo is not None:
        native_jo = getattr(jo, 'nativeObject', None) or jo
        try:
            ctx_jo = native_jo.createForAssemblyContext(new_occ)
        except Exception:
            ctx_jo = native_jo
        return adsk.fusion.JointGeometry.createByJointOrigin(ctx_jo)

    # Implicit JointGeometry path
    jg = adsk.fusion.JointGeometry.cast(comp_geo)
    if jg is None:
        raise RuntimeError(
            f"Cannot build geo1 from '{comp_geo.objectType}' — unsupported type."
        )

    entity = jg.entityOne
    if entity is None:
        raise RuntimeError(
            "JointGeometry.entityOne is None; cannot reconstruct geometry for new occurrence."
        )

    native_entity = getattr(entity, 'nativeObject', None) or entity
    try:
        ctx_entity = native_entity.createForAssemblyContext(new_occ)
    except Exception:
        ctx_entity = native_entity

    key_pt = jg.keyPointType

    edge = adsk.fusion.BRepEdge.cast(ctx_entity)
    if edge is not None:
        return adsk.fusion.JointGeometry.createByCurve(edge, key_pt)

    face = adsk.fusion.BRepFace.cast(ctx_entity)
    if face is not None:
        try:
            return adsk.fusion.JointGeometry.createByNonPlanarFace(face, key_pt)
        except Exception:
            return adsk.fusion.JointGeometry.createByPlanarFace(face, None, key_pt)

    vertex = adsk.fusion.BRepVertex.cast(ctx_entity)
    if vertex is not None:
        return adsk.fusion.JointGeometry.createByPoint(vertex)

    raise RuntimeError(
        f"Unsupported entity type for geo1: '{ctx_entity.objectType}'. "
        "Only BRepEdge, BRepFace, and BRepVertex are handled."
    )


def _create_instance(
    root: adsk.fusion.Component,
    source_occ: adsk.fusion.Occurrence,
    comp_geo,
    is_flipped: bool,
    joint_type: int,
    target_entity,
    target_occ: Optional[adsk.fusion.Occurrence],
) -> None:
    new_occ = root.occurrences.addExistingComponent(
        source_occ.component, adsk.core.Matrix3D.create()
    )
    try:
        geo1 = _make_geo1(comp_geo, source_occ, new_occ)
        geo2 = _target_to_geo2(target_entity, target_occ)

        ji           = root.joints.createInput(geo1, geo2)
        ji.isFlipped = is_flipped
        _apply_joint_motion(ji, joint_type)
        root.joints.add(ji)

    except Exception:
        try:
            new_occ.deleteMe()
        except Exception:
            pass
        raise
