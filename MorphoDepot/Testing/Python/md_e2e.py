"""End-to-end fixtures + flows: drive REAL create / publish / release against live GitHub + the
App (test-mode). Uses the global `H` (Harness). These are heavier and stateful, kept separate from
the fast workflow net. A repo named with the mdtest- prefix routes through the App test-mode (no
reviewer email; approve-id fetchable via /repos/_test/pending)."""
import slicer
import numpy as np

E2E_VOL = "mdteste2evol"
E2E_COLOR = "mdteste2ecolors"


def _rm(name):
    for _ in range(100):          # bounded so a stuck node can't spin forever
        n = slicer.mrmlScene.GetFirstNodeByName(name)
        if not n:
            break
        slicer.mrmlScene.RemoveNode(n)


def setupCreateFixtures():
    """A synthetic source volume + a User terminology color table, selected in the Create tab."""
    _rm(E2E_VOL)
    _rm(E2E_COLOR)
    vol = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", E2E_VOL)
    arr = np.zeros((20, 20, 20), "int16")
    arr[5:15, 5:15, 5:15] = 200
    slicer.util.updateVolumeFromArray(vol, arr)

    ct = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLColorTableNode", E2E_COLOR)
    ct.SetTypeToUser()
    ct.SetNumberOfColors(3)
    ct.SetColor(0, "Background", 0.0, 0.0, 0.0, 0.0)
    ct.SetColor(1, "StructureA", 1.0, 0.0, 0.0, 1.0)
    ct.SetTerminology(1, "SCT", "85756007", "Tissue", "SCT", "85756007", "Tissue")
    ct.SetColor(2, "StructureB", 0.0, 1.0, 0.0, 1.0)
    ct.SetTerminology(2, "SCT", "85756007", "Tissue", "SCT", "85756007", "Tissue")

    H.goTab("Create")
    H.w.createUI.inputSelector.setCurrentNode(vol)
    H.w.createUI.colorSelector.setCurrentNode(ct)
    H.w.createUI.segmentationSelector.setCurrentNode(None)
    slicer.app.processEvents()
    return vol, ct


def e2eCreate(name, archival):
    """Drive Create to STAGE a real repo (short-term=personal, archival=org). Auto-accepts the
    custom confirmation modal and disables auto-assign (skips the workflow push). Returns the
    staged nameWithOwner (or an error)."""
    vol, ct = setupCreateFixtures()
    H.fillValidForm(name=name, shortTerm=not archival)
    H.w.createUI.inputSelector.setCurrentNode(vol)
    H.w.createUI.colorSelector.setCurrentNode(ct)
    H.w.createUI.autoAssignCheckBox.checked = False
    slicer.app.processEvents()
    orig = H.w.showConfirmationDialog
    H.w.showConfirmationDialog = lambda *a, **k: True   # auto-accept (it's a custom QDialog)
    H.dialogLog = []
    err = None
    try:
        H.w.onCreateRepository()
    except Exception as e:
        import traceback
        err = {"err": str(e), "tb": traceback.format_exc().splitlines()[-5:]}
    finally:
        H.w.showConfirmationDialog = orig
    H.pump(200)
    return {"staged": getattr(H.w, "_stagedNameWithOwner", None), "dialogs": H.shown(), "error": err}


def e2eDiscard():
    """Drive Discard to delete the currently-staged repo (teardown). Confirm dialog auto-answered."""
    H.dialogLog = []
    err = None
    try:
        H.w.onDiscard()
    except Exception as e:
        err = str(e)
    H.pump(200)
    return {"staged": getattr(H.w, "_stagedNameWithOwner", None), "dialogs": H.shown(), "error": err}


def e2ePublish():
    """Drive Make Public (onPublish). For an archival repo this requests review (the gate); the
    repo stays staged + pending. Returns staged name + dialog texts."""
    H.dialogLog = []
    err = None
    try:
        H.w.onPublish()
    except Exception as e:
        import traceback
        err = {"err": str(e), "tb": traceback.format_exc().splitlines()[-5:]}
    H.pump(200)
    return {"staged": getattr(H.w, "_stagedNameWithOwner", None), "dialogs": H.shown(),
            "texts": [d["text"][:200] for d in H.dialogLog], "error": err}


def probeCreateValidation(archival=True, name="mdtest-e2e-probe"):
    """Set up fixtures + fill the form, then run _collectAccessionInputs WITHOUT creating a repo.
    Returns whether validation passed + what (if any) dialogs it raised + a peek at the color check."""
    vol, ct = setupCreateFixtures()
    H.fillValidForm(name=name, shortTerm=not archival)
    # fillValidForm doesn't touch the node selectors, re-assert them just in case
    H.w.createUI.inputSelector.setCurrentNode(vol)
    H.w.createUI.colorSelector.setCurrentNode(ct)
    slicer.app.processEvents()
    inp = H.w._collectAccessionInputs()
    return {
        "collectOK": inp is not None,
        "dialogs": H.shown(),
        "colorType": ct.GetTypeAsString(),
        "term1": (ct.GetTerminologyAsString(1) or "")[:40],
        "notTerminology": H.w._colorTableNotTerminology(ct),
        "volDims": str(vol.GetImageData().GetDimensions()) if vol.GetImageData() else None,
    }
