cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(MorphoDepot)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://www.slicer.org/wiki/Documentation/Nightly/Extensions/MorphoDepot")
set(EXTENSION_CATEGORY "SlicerMorph")
set(EXTENSION_CONTRIBUTORS "Steve Pieper (Isomics, Inc.)")
set(EXTENSION_DESCRIPTION "Code to support collaborative segmentation projects using github.")
set(EXTENSION_ICONURL "https://github.com/SlicerMorph/SlicerMorphoDepot/blob/main/MorphoDepot.png?raw=true")
set(EXTENSION_SCREENSHOTURLS "https://github.com/user-attachments/assets/2d81e4f3-8d8b-49e4-97f4-f906053d375f https://github.com/user-attachments/assets/9481ce0f-dc37-4900-9cdc-14bb0922df59")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(MorphoDepot)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
