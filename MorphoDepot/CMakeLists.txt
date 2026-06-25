#-----------------------------------------------------------------------------
set(MODULE_NAME MorphoDepot)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  # MorphoDepotLib sub-package: MorphoDepot.py imports every one of these at load time, so they
  # must be packaged/installed alongside the main script or the module fails to import.  Helper
  # modules belong HERE (in the sub-package), never at the top level next to MorphoDepot.py —
  # Slicer's factory scans top-level scripts and would register a bare helper as a broken module.
  ${MODULE_NAME}Lib/__init__.py
  ${MODULE_NAME}Lib/accession_form.py
  ${MODULE_NAME}Lib/contributors.py
  ${MODULE_NAME}Lib/forms.py
  ${MODULE_NAME}Lib/logic_accession.py
  ${MODULE_NAME}Lib/logic_contribute.py
  ${MODULE_NAME}Lib/logic_controlplane.py
  ${MODULE_NAME}Lib/logic_deps.py
  ${MODULE_NAME}Lib/logic_github.py
  ${MODULE_NAME}Lib/logic_objectstore.py
  ${MODULE_NAME}Lib/logic_release.py
  ${MODULE_NAME}Lib/logic_repo.py
  ${MODULE_NAME}Lib/logic_repoclerk.py
  ${MODULE_NAME}Lib/logic_search.py
  ${MODULE_NAME}Lib/screenshot_dialog.py
  ${MODULE_NAME}Lib/search_form.py
  ${MODULE_NAME}Lib/widget_annotate.py
  ${MODULE_NAME}Lib/widget_configure.py
  ${MODULE_NAME}Lib/widget_create.py
  ${MODULE_NAME}Lib/widget_release.py
  ${MODULE_NAME}Lib/widget_review.py
  ${MODULE_NAME}Lib/widget_search.py
  ${MODULE_NAME}Lib/widget_validation.py
  )

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/${MODULE_NAME}.png
  Resources/UI/${MODULE_NAME}Annotate.ui
  Resources/UI/${MODULE_NAME}Create.ui
  Resources/UI/${MODULE_NAME}Configure.ui
  Resources/UI/${MODULE_NAME}Release.ui
  Resources/UI/${MODULE_NAME}Review.ui
  Resources/UI/${MODULE_NAME}Search.ui
  )

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )

#-----------------------------------------------------------------------------
if(BUILD_TESTING)

  # Register the unittest subclass in the main script as a ctest.
  # Note that the test will also be available at runtime.
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)

  # Additional build-time testing
  add_subdirectory(Testing)
endif()
