add_library(installer_fomod SHARED)

set_target_properties(installer_fomod PROPERTIES
  AUTOMOC ON
  AUTOMOC_PATH_PREFIX ON
  AUTOUIC ON
  CXX_STANDARD 20
  CXX_STANDARD_REQUIRED YES
  CXX_EXTENSIONS NO
  # Embed compiler debug information in each object. This avoids MSVC's
  # shared compile-PDB writer (and its parallel-build contention) while the
  # linker still emits the final deterministic DLL PDB.
  MSVC_DEBUG_INFORMATION_FORMAT "$<$<CONFIG:Debug,RelWithDebInfo>:Embedded>"
  MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL"
  OUTPUT_NAME fomod_logger
)

configure_file(
  "${CMAKE_CURRENT_SOURCE_DIR}/fomod_logger_version.rc.in"
  "${CMAKE_CURRENT_BINARY_DIR}/fomod_logger_version.rc"
  @ONLY
)

# Relative moc includes and deterministic rcc output keep generated source
# checksums stable when rebuilding from the corresponding-source archive.
target_include_directories(installer_fomod PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
set(fomod_logger_resource_cpp "${CMAKE_CURRENT_BINARY_DIR}/qrc_resources.cpp")
add_custom_command(OUTPUT "${fomod_logger_resource_cpp}"
  COMMAND "${CMAKE_COMMAND}"
    "-DFOMOD_LOGGER_RCC=$<TARGET_FILE:Qt6::rcc>"
    "-DFOMOD_LOGGER_RESOURCE_SOURCE_DIR=${CMAKE_CURRENT_SOURCE_DIR}"
    "-DFOMOD_LOGGER_RESOURCE_OUTPUT=${fomod_logger_resource_cpp}"
    -P "${CMAKE_SOURCE_DIR}/packaging/write-resources.cmake"
  DEPENDS
    resources.qrc
    resources/CloseButtonIcon.png
    resources/LeftButtonIcon.png
    resources/RightButtonIcon.png
    "${CMAKE_SOURCE_DIR}/packaging/write-resources.cmake"
  COMMENT "Generating resources with stable paths and timestamps"
  VERBATIM
)

target_sources(installer_fomod PRIVATE
  installerfomod.cpp
  installerfomod.h
  fomodloggereventwriter.cpp
  fomodloggereventwriter.h
  fomodpatchevidence.cpp
  fomodpatchevidence.h
  fomodpreviouschoices.cpp
  fomodpreviouschoices.h
  fomodinstallerdialog.cpp
  fomodinstallerdialog.h
  fomodinstallerdialog.ui
  fomodscreenshotdialog.cpp
  fomodscreenshotdialog.h
  fomodscreenshotdialog.ui
  scalelabel.cpp
  scalelabel.h
  xmlreader.cpp
  xmlreader.h
  resources.qrc
  "${fomod_logger_resource_cpp}"
  installerfomod.json
  fomodloggerversion.h
  "${CMAKE_CURRENT_BINARY_DIR}/fomod_logger_version.rc"
)

target_compile_definitions(installer_fomod PRIVATE
  FOMOD_LOGGER_VERSION="${FOMOD_LOGGER_VERSION}"
  FOMOD_LOGGER_MO2_VERSION="${FOMOD_LOGGER_MO2_VERSION}"
  NOMINMAX
  _CRT_SECURE_NO_WARNINGS
  QT_MESSAGELOGCONTEXT
)

target_link_libraries(installer_fomod PRIVATE
  FomodLogger::uibase
  Qt6::Core
  Qt6::Gui
  Qt6::Widgets
  shell32
)

if(MSVC)
  target_compile_options(installer_fomod PRIVATE
    /W3
    /permissive-
    /Zc:__cplusplus
    /Zf
    /Brepro
    /experimental:deterministic
    # uibase headers live outside Git source during normal builds and inside
    # the corresponding-source archive after extraction. Give both layouts
    # the same compiler/debug path identity. MSVC applies the first matching
    # mapping, so the dependency must precede its possible parent source root.
    "/pathmap:${FOMOD_LOGGER_UIBASE_SOURCE_DIR}=external/uibase"
    "/pathmap:${CMAKE_SOURCE_DIR}=."
    "/pathmap:${CMAKE_BINARY_DIR}=."
  )
  target_link_options(installer_fomod PRIVATE
    /Brepro
    /INCREMENTAL:NO
    "/PDBALTPATH:$<TARGET_PDB_FILE_NAME:installer_fomod>"
  )
endif()

if(FOMOD_LOGGER_RELEASE_BUILD)
  add_custom_command(TARGET installer_fomod PRE_BUILD
    COMMAND ${FOMOD_LOGGER_RELEASE_VERIFY_COMMAND}
    COMMENT "Verifying clean exact source before FOMOD Logger build"
    VERBATIM
  )
  # Run the post-link source check before publishing the build record. A failed
  # source check therefore cannot leave a record that appears release-eligible.
  add_custom_command(TARGET installer_fomod POST_BUILD
    COMMAND ${FOMOD_LOGGER_RELEASE_VERIFY_COMMAND}
    COMMENT "Verifying clean exact source after FOMOD Logger link"
    VERBATIM
  )
endif()

add_custom_command(TARGET installer_fomod POST_BUILD
  COMMAND "${CMAKE_COMMAND}"
    "-DFOMOD_LOGGER_CONFIGURATION=$<CONFIG>"
    "-DFOMOD_LOGGER_RECORD_OUTPUT=$<TARGET_FILE_DIR:installer_fomod>/fomod_logger-native-build-record.json"
    "-DFOMOD_LOGGER_NATIVE_DLL=$<TARGET_FILE:installer_fomod>"
    "-DFOMOD_LOGGER_UIBASE_IMPORT_LIBRARY=${FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY}"
    "-DFOMOD_LOGGER_UIBASE_COMPANION_DLL=${FOMOD_LOGGER_UIBASE_COMPANION_DLL}"
    "-DFOMOD_LOGGER_PUBLIC_VERSION=${FOMOD_LOGGER_VERSION}"
    "-DFOMOD_LOGGER_MO2_VERSION=${FOMOD_LOGGER_MO2_VERSION}"
    "-DFOMOD_LOGGER_RELEASE_BUILD_JSON=${FOMOD_LOGGER_RELEASE_BUILD_JSON}"
    "-DFOMOD_LOGGER_REPOSITORY_COMMIT=${FOMOD_LOGGER_GIT_COMMIT}"
    "-DFOMOD_LOGGER_MO2_ABI_TARGET=${FOMOD_LOGGER_MO2_ABI_TARGET}"
    "-DFOMOD_LOGGER_GENERATOR=${CMAKE_GENERATOR}"
    "-DFOMOD_LOGGER_GENERATOR_PLATFORM=${CMAKE_GENERATOR_PLATFORM}"
    "-DFOMOD_LOGGER_GENERATOR_TOOLSET=${CMAKE_VS_PLATFORM_TOOLSET}"
    "-DFOMOD_LOGGER_CXX_COMPILER_ID=${CMAKE_CXX_COMPILER_ID}"
    "-DFOMOD_LOGGER_CXX_COMPILER_VERSION=${CMAKE_CXX_COMPILER_VERSION}"
    "-DFOMOD_LOGGER_CMAKE_VERSION=${CMAKE_VERSION}"
    "-DFOMOD_LOGGER_WINDOWS_SDK=${CMAKE_SYSTEM_VERSION}"
    "-DFOMOD_LOGGER_QT_VERSION=${Qt6Core_VERSION}"
    "-DFOMOD_LOGGER_UIBASE_COMMIT=${FOMOD_LOGGER_UIBASE_COMMIT}"
    -P "${CMAKE_SOURCE_DIR}/packaging/write-native-build-record.cmake"
  COMMENT "Writing post-link FOMOD Logger native build record"
  VERBATIM
)

install(TARGETS installer_fomod
  RUNTIME DESTINATION plugins
  LIBRARY DESTINATION plugins
)

# Developer-only tests that need Qt Core but not MO2 (never part of a release).
option(FOMOD_LOGGER_BUILD_NATIVE_TESTS
       "Build the Qt-only native tests next to the plugin" OFF)
if(FOMOD_LOGGER_BUILD_NATIVE_TESTS)
  add_executable(previous_choices_test
    "${PROJECT_SOURCE_DIR}/tests/native/previous_choices_test.cpp"
    fomodpreviouschoices.cpp
    fomodpreviouschoices.h
    fomodpatchevidence.cpp
    fomodpatchevidence.h
    fomodloggereventwriter.cpp
    fomodloggereventwriter.h
  )
  set_target_properties(previous_choices_test PROPERTIES
    CXX_STANDARD 20
    CXX_STANDARD_REQUIRED YES
    CXX_EXTENSIONS NO
    MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL"
  )
  target_include_directories(previous_choices_test PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
  target_compile_definitions(previous_choices_test PRIVATE
    FOMOD_LOGGER_TEST_FIXTURES="${PROJECT_SOURCE_DIR}/tests/native/fixtures"
  )
  target_link_libraries(previous_choices_test PRIVATE Qt6::Core)
endif()
