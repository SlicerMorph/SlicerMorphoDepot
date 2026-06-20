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


def makeBaseline(name, nsegs, vol=None):
    """A segmentation in the source-volume geometry with `nsegs` disjoint labeled blocks (so it has
    real segments + voxels). Used as the create baseline and, with a different nsegs + the loaded
    repo's volume, as the changed release baseline (M6 keys on segment count)."""
    import vtk
    _rm(name)
    if vol is None:
        vol = slicer.mrmlScene.GetFirstNodeByName(E2E_VOL)
    shape = slicer.util.arrayFromVolume(vol).shape
    lm = np.zeros(shape, "uint8")
    for i in range(nsegs):
        s = 2 + i * 4
        lm[s:s + 3, s:s + 3, s:s + 3] = i + 1
    labelVol = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", name + "-lm")
    labelVol.SetSpacing(vol.GetSpacing())
    labelVol.SetOrigin(vol.GetOrigin())
    m = vtk.vtkMatrix4x4()
    vol.GetIJKToRASDirectionMatrix(m)
    labelVol.SetIJKToRASDirectionMatrix(m)
    slicer.util.updateVolumeFromArray(labelVol, lm)
    seg = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
    seg.SetReferenceImageGeometryParameterFromVolumeNode(vol)
    slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelVol, seg)
    slicer.mrmlScene.RemoveNode(labelVol)
    return seg


def setupCreateFixtures(baselineSegs=0):
    """A synthetic source volume + a User terminology color table (+ optional baseline segmentation
    with `baselineSegs` segments), selected in the Create tab. Returns (vol, color, baseline|None)."""
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

    seg = makeBaseline("mdteste2ebaseline", baselineSegs) if baselineSegs else None
    H.goTab("Create")
    H.w.createUI.inputSelector.setCurrentNode(vol)
    H.w.createUI.colorSelector.setCurrentNode(ct)
    H.w.createUI.segmentationSelector.setCurrentNode(seg)
    slicer.app.processEvents()
    return vol, ct, seg


def e2eCreate(name, archival, baselineSegs=0):
    """Drive Create to STAGE a real repo (short-term=personal, archival=org). Auto-accepts the
    custom confirmation modal and disables auto-assign (skips the workflow push). Returns the
    staged nameWithOwner (or an error)."""
    vol, ct, seg = setupCreateFixtures(baselineSegs)
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
    """Drive Discard to delete the currently-staged repo (teardown). Confirm dialog auto-answered
    by the harness; the member/archival path routes through the App (no UI confirm)."""
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


def e2eLoadForRelease(nwo):
    """Drive the Release tab to load a published repo (refresh -> double-click). Returns whether it
    loaded + the committed baseline's segment count."""
    H.goTab("Release")
    H.w.onRefreshReleaseTab()
    H.pump(500)
    item = None
    for it, rd in (getattr(H.w, "reposByItem", {}) or {}).items():
        if rd.get("nameWithOwner") == nwo:
            item = it
            break
    if item is None:
        return {"loaded": False, "repos": [rd.get("nameWithOwner")
                for rd in (getattr(H.w, "reposByItem", {}) or {}).values()][:15]}
    H.w.onReleaseRepoDoubleClicked(item)
    H.pump(800)   # clone + load
    bl = getattr(H.w.logic, "baselineSegmentationNode", None)
    return {"loaded": H.w.logic.localRepo is not None,
            "currentRepo": H.w.releaseUI.currentRepoLabel.text,
            "committedBaselineSegs": bl.GetSegmentation().GetNumberOfSegments() if bl else None,
            "colorSelected": H.w.releaseUI.newColorSelector.currentNode() is not None}


def e2eChangedBaseline(nsegs):
    """Build a new baseline (nsegs) in the LOADED repo's source-volume geometry + select it as the
    release baseline. Returns whether Make Release is now enabled."""
    vol = slicer.mrmlScene.GetFirstNodeByName(getattr(H.w.logic, "sourceVolumeName", "") or "")
    if vol is None:
        vols = slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
        vol = vols[0] if vols else None
    seg = makeBaseline("mdteste2e-newbaseline", nsegs, vol=vol)
    H.w.releaseUI.newBaselineSelector.setCurrentNode(seg)
    slicer.app.processEvents()
    return {"newBaselineSegs": seg.GetSegmentation().GetNumberOfSegments(),
            "makeEnabled": H.w.releaseUI.makeReleaseButton.enabled}


def e2eMakeRelease():
    """Drive Make Release (onMakeRelease). For an archival repo this pushes a candidate + requests
    review (gate). Returns dialog texts (so callers can see M6/announcement/credit prompts)."""
    H.dialogLog = []
    err = None
    try:
        H.w.onMakeRelease()
    except Exception as e:
        import traceback
        err = {"err": str(e), "tb": traceback.format_exc().splitlines()[-6:]}
    H.pump(400)
    return {"dialogs": H.shown(), "texts": [d["text"][:160] for d in H.dialogLog], "error": err}


def probeCreateValidation(archival=True, name="mdtest-e2e-probe"):
    """Set up fixtures + fill the form, then run _collectAccessionInputs WITHOUT creating a repo.
    Returns whether validation passed + what (if any) dialogs it raised + a peek at the color check."""
    vol, ct, seg = setupCreateFixtures()
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
