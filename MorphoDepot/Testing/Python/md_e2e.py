"""End-to-end fixtures + flows: drive REAL create / publish / release against live GitHub + the
App (test-mode). Uses the global `H` (Harness). These are heavier and stateful, kept separate from
the fast workflow net. A repo named with the mdtest- prefix routes through the App test-mode (no
reviewer email; approve-id fetchable via /repos/_test/pending)."""
import slicer
import numpy as np

E2E_VOL = "mdteste2evol"
E2E_COLOR = "mdteste2ecolors"


def _rm(name):
    n = slicer.mrmlScene.GetFirstNodeByName(name)
    while n:
        slicer.mrmlScene.RemoveNode(n)
        n = slicer.mrmlScene.GetFirstNodeByName(name)


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
        "term1": ct.GetTerminologyAsString(1)[:40],
        "notTerminology": H.w._colorTableNotTerminology(ct),
        "volDims": str(vol.GetImageData().GetDimensions()) if vol.GetImageData() else None,
    }
