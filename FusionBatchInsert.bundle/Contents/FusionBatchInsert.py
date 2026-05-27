"""FusionBatchInsert — place multiple jointed copies of a component in one step.

Adds "Batch Insert" to the Assemble panel in the Design workspace.
"""

import adsk.core
import adsk.fusion
import traceback
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

import batch_insert  # noqa: E402

handlers: list = []

_WORKSPACE_ID = "FusionSolidEnvironment"
_PANEL_IDS = ["SolidAssemblePanel", "AssemblePanel"]


def run(context) -> None:
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        workspace = ui.workspaces.itemById(_WORKSPACE_ID)
        if workspace is None:
            ui.messageBox(
                f"Workspace '{_WORKSPACE_ID}' not found.\n"
                "Open a design and reload the add-in.",
                "Batch Insert",
            )
            return

        panel = _find_panel(workspace, ui)
        if panel is None:
            ui.messageBox(
                "Assemble panel not found in the toolbar.\n"
                "Batch Insert could not be registered.",
                "Batch Insert",
            )
            return

        batch_insert.register(ui, panel, handlers)

    except Exception:
        ui.messageBox(traceback.format_exc(), "Batch Insert — startup error")


def stop(context) -> None:
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        workspace = ui.workspaces.itemById(_WORKSPACE_ID)
        panel = _find_panel(workspace, ui) if workspace else None
        if panel:
            batch_insert.unregister(ui, panel)
    except Exception:
        pass
    finally:
        handlers.clear()


def _find_panel(
    workspace: adsk.core.Workspace,
    ui: adsk.core.UserInterface,
) -> adsk.core.ToolbarPanel:
    for pid in _PANEL_IDS:
        panel = workspace.toolbarPanels.itemById(pid)
        if panel:
            return panel
    for pid in _PANEL_IDS:
        panel = ui.allToolbarPanels.itemById(pid)
        if panel:
            return panel
    return None
