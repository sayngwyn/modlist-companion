cmake_minimum_required(VERSION 3.25)

project(fomod_logger VERSION 1.0.0 LANGUAGES CXX)

find_package(Git QUIET)
find_package(Python3 3.12 QUIET COMPONENTS Interpreter)

file(READ "${CMAKE_CURRENT_LIST_DIR}/release_identity.json"
     FOMOD_LOGGER_RELEASE_IDENTITY_JSON)
string(JSON FOMOD_LOGGER_RELEASE_VERSION GET
       "${FOMOD_LOGGER_RELEASE_IDENTITY_JSON}" public_semver)
string(JSON FOMOD_LOGGER_RELEASE_MO2_VERSION GET
       "${FOMOD_LOGGER_RELEASE_IDENTITY_JSON}" mo2_version)
string(JSON FOMOD_LOGGER_RELEASE_VERSION_TUPLE GET
       "${FOMOD_LOGGER_RELEASE_IDENTITY_JSON}" windows_file_version)

file(READ "${CMAKE_CURRENT_LIST_DIR}/packaging/native-dependencies.json"
     FOMOD_LOGGER_NATIVE_DEPENDENCIES_JSON)
string(JSON FOMOD_LOGGER_MO2_ABI_TARGET GET
       "${FOMOD_LOGGER_NATIVE_DEPENDENCIES_JSON}" mo2_abi_target)
string(JSON FOMOD_LOGGER_UIBASE_COMMIT GET
       "${FOMOD_LOGGER_NATIVE_DEPENDENCIES_JSON}" uibase commit)
string(JSON FOMOD_LOGGER_UIBASE_SOURCE_ARCHIVE_PREFIX GET
       "${FOMOD_LOGGER_NATIVE_DEPENDENCIES_JSON}"
       uibase source_archive_prefix)

set(FOMOD_LOGGER_VERSION "${FOMOD_LOGGER_RELEASE_VERSION}" CACHE STRING
    "Exact public FOMOD Logger SemVer")
set(FOMOD_LOGGER_MO2_VERSION "${FOMOD_LOGGER_RELEASE_MO2_VERSION}" CACHE STRING
    "MO2-safe representation of the public version")
set(FOMOD_LOGGER_VERSION_TUPLE "${FOMOD_LOGGER_RELEASE_VERSION_TUPLE}"
    CACHE STRING "Windows numeric file-version tuple")
if(NOT FOMOD_LOGGER_VERSION STREQUAL FOMOD_LOGGER_RELEASE_VERSION OR
   NOT FOMOD_LOGGER_MO2_VERSION STREQUAL FOMOD_LOGGER_RELEASE_MO2_VERSION OR
   NOT FOMOD_LOGGER_VERSION_TUPLE STREQUAL FOMOD_LOGGER_RELEASE_VERSION_TUPLE)
  message(FATAL_ERROR
    "release identity overrides do not match release_identity.json")
endif()

set(FOMOD_LOGGER_GIT_COMMIT "" CACHE STRING
    "Exact 40-hex source commit embedded in the native binary")
option(FOMOD_LOGGER_RELEASE_BUILD
       "Require exact clean source before and after the native build" OFF)
if(FOMOD_LOGGER_RELEASE_BUILD AND FOMOD_LOGGER_GIT_COMMIT STREQUAL "")
  message(FATAL_ERROR
    "Release builds require an explicit FOMOD_LOGGER_GIT_COMMIT; "
    "the implicit HEAD fallback is for developer builds only.")
endif()
if(FOMOD_LOGGER_GIT_COMMIT STREQUAL "")
  if(EXISTS
      "${CMAKE_CURRENT_LIST_DIR}/fomod_logger-source-manifest.json")
    file(READ
      "${CMAKE_CURRENT_LIST_DIR}/fomod_logger-source-manifest.json"
      FOMOD_LOGGER_EXTRACTED_SOURCE_MANIFEST)
    string(JSON FOMOD_LOGGER_GIT_COMMIT_DISCOVERED GET
      "${FOMOD_LOGGER_EXTRACTED_SOURCE_MANIFEST}" repository_commit)
  elseif(EXISTS "${CMAKE_CURRENT_LIST_DIR}/.git" AND Git_FOUND)
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" -C "${CMAKE_CURRENT_LIST_DIR}"
              rev-parse --verify HEAD
      OUTPUT_VARIABLE FOMOD_LOGGER_GIT_COMMIT_DISCOVERED
      OUTPUT_STRIP_TRAILING_WHITESPACE
      ERROR_QUIET
    )
  endif()
  set(FOMOD_LOGGER_GIT_COMMIT "${FOMOD_LOGGER_GIT_COMMIT_DISCOVERED}"
      CACHE STRING "Exact 40-hex source commit embedded in the native binary" FORCE)
endif()
string(LENGTH "${FOMOD_LOGGER_GIT_COMMIT}" FOMOD_LOGGER_GIT_COMMIT_LENGTH)
if(NOT FOMOD_LOGGER_GIT_COMMIT_LENGTH EQUAL 40 OR
   NOT FOMOD_LOGGER_GIT_COMMIT MATCHES "^[0-9a-f]+$")
  message(FATAL_ERROR
    "FOMOD_LOGGER_GIT_COMMIT must be a lowercase 40-hex commit. "
    "Source-archive builds must pass the repository_commit from the source manifest.")
endif()

set(FOMOD_LOGGER_UIBASE_SOURCE_DIR "" CACHE PATH
    "Clean pinned uibase source tree used only for headers")
set(FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY "" CACHE FILEPATH
    "Exact x64 RelWithDebInfo uibase import library linked by Logger")
set(FOMOD_LOGGER_UIBASE_COMPANION_DLL "" CACHE FILEPATH
    "Companion x64 RelWithDebInfo uibase.dll from the same build prefix")

foreach(required_input IN ITEMS
    FOMOD_LOGGER_UIBASE_SOURCE_DIR
    FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY
    FOMOD_LOGGER_UIBASE_COMPANION_DLL)
  if("${${required_input}}" STREQUAL "")
    message(FATAL_ERROR "${required_input} is required")
  endif()
endforeach()
if(NOT EXISTS "${FOMOD_LOGGER_UIBASE_SOURCE_DIR}/include/uibase/iplugin.h")
  message(FATAL_ERROR
    "FOMOD_LOGGER_UIBASE_SOURCE_DIR does not contain the pinned uibase include/uibase tree")
endif()
if(NOT EXISTS "${FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY}" OR
   IS_DIRECTORY "${FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY}")
  message(FATAL_ERROR "FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY is not a file")
endif()
if(NOT EXISTS "${FOMOD_LOGGER_UIBASE_COMPANION_DLL}" OR
   IS_DIRECTORY "${FOMOD_LOGGER_UIBASE_COMPANION_DLL}")
  message(FATAL_ERROR "FOMOD_LOGGER_UIBASE_COMPANION_DLL is not a file")
endif()

function(fomod_logger_verify_uibase_source)
  if(EXISTS
      "${CMAKE_CURRENT_LIST_DIR}/fomod_logger-source-manifest.json")
    file(REAL_PATH "${FOMOD_LOGGER_UIBASE_SOURCE_DIR}"
         uibase_source_real_path)
    file(REAL_PATH
         "${CMAKE_CURRENT_LIST_DIR}/${FOMOD_LOGGER_UIBASE_SOURCE_ARCHIVE_PREFIX}"
         embedded_uibase_real_path)
    if(NOT uibase_source_real_path STREQUAL embedded_uibase_real_path)
      message(FATAL_ERROR
        "An extracted source build must consume the manifest-bound embedded "
        "uibase tree at ${embedded_uibase_real_path}")
    endif()
  elseif(EXISTS "${FOMOD_LOGGER_UIBASE_SOURCE_DIR}/.git")
    if(FOMOD_LOGGER_RELEASE_BUILD)
      # The full release verifier runs immediately below and executes only the
      # helper materialized from the expected Logger commit.
      return()
    endif()
    if(NOT Git_FOUND)
      message(FATAL_ERROR "A uibase Git checkout requires Git for verification")
    endif()
    if(NOT Python3_Interpreter_FOUND)
      message(FATAL_ERROR
        "A uibase Git checkout requires Python 3.12 for exact byte verification")
    endif()
    execute_process(
      COMMAND "${Python3_EXECUTABLE}" -I -S -B
              "${CMAKE_CURRENT_LIST_DIR}/packaging/verify-git-source.py"
              --root "${FOMOD_LOGGER_UIBASE_SOURCE_DIR}"
              --expected-commit "${FOMOD_LOGGER_UIBASE_COMMIT}"
              --git-executable "${GIT_EXECUTABLE}"
      RESULT_VARIABLE uibase_byte_result
      OUTPUT_VARIABLE uibase_byte_output
      ERROR_VARIABLE uibase_byte_error
    )
    if(NOT uibase_byte_result EQUAL 0)
      message(FATAL_ERROR
        "uibase tracked-byte verification failed:\n"
        "${uibase_byte_output}${uibase_byte_error}")
    endif()
  else()
    message(FATAL_ERROR
      "uibase source must be either a clean exact Git checkout or the "
      "manifest-bound tree in an extracted corresponding-source archive")
  endif()
endfunction()

function(fomod_logger_verify_release_source)
  if(NOT EXISTS
     "${CMAKE_CURRENT_LIST_DIR}/fomod_logger-source-manifest.json" AND
     EXISTS "${CMAKE_CURRENT_LIST_DIR}/.git" AND NOT Git_FOUND)
    message(FATAL_ERROR "A release Git checkout requires Git for verification")
  endif()
  execute_process(
    COMMAND "${CMAKE_COMMAND}"
      "-DFOMOD_LOGGER_SOURCE_DIR=${CMAKE_CURRENT_LIST_DIR}"
      "-DFOMOD_LOGGER_EXPECTED_COMMIT=${FOMOD_LOGGER_GIT_COMMIT}"
      "-DFOMOD_LOGGER_GIT_EXECUTABLE=${GIT_EXECUTABLE}"
      "-DFOMOD_LOGGER_PYTHON_EXECUTABLE=${Python3_EXECUTABLE}"
      "-DFOMOD_LOGGER_GIT_VERIFY_SCRIPT=${CMAKE_CURRENT_LIST_DIR}/packaging/verify-git-source.py"
      "-DFOMOD_LOGGER_VERIFY_TEMP_DIR=${CMAKE_BINARY_DIR}/source-verifier-tools"
      "-DFOMOD_LOGGER_UIBASE_DIR=${FOMOD_LOGGER_UIBASE_SOURCE_DIR}"
      "-DFOMOD_LOGGER_UIBASE_EXPECTED_COMMIT=${FOMOD_LOGGER_UIBASE_COMMIT}"
      "-DFOMOD_LOGGER_UIBASE_EXPECTED_PREFIX=${FOMOD_LOGGER_UIBASE_SOURCE_ARCHIVE_PREFIX}"
      -P "${CMAKE_CURRENT_LIST_DIR}/packaging/verify-release-source.cmake"
    RESULT_VARIABLE verification_result
    OUTPUT_VARIABLE verification_output
    ERROR_VARIABLE verification_error
  )
  if(NOT verification_result EQUAL 0)
    message(FATAL_ERROR
      "release source verification failed:\n"
      "${verification_output}${verification_error}")
  endif()
endfunction()

fomod_logger_verify_uibase_source()
if(FOMOD_LOGGER_RELEASE_BUILD OR
   EXISTS "${CMAKE_CURRENT_LIST_DIR}/fomod_logger-source-manifest.json")
  fomod_logger_verify_release_source()
endif()

if(NOT WIN32 OR NOT MSVC)
  message(FATAL_ERROR "FOMOD Logger's MO2 2.5.2 native target requires MSVC on Windows")
endif()
if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "FOMOD Logger's native target is x86-64 only")
endif()
if(NOT CMAKE_GENERATOR STREQUAL "Visual Studio 17 2022")
  message(FATAL_ERROR "native builds require the Visual Studio 17 2022 generator")
endif()
if(NOT CMAKE_GENERATOR_PLATFORM STREQUAL "x64")
  message(FATAL_ERROR "native builds require the x64 generator platform")
endif()
if(NOT CMAKE_VS_PLATFORM_TOOLSET STREQUAL "v143")
  message(FATAL_ERROR
    "native builds require the v143 platform toolset, found "
    "${CMAKE_VS_PLATFORM_TOOLSET}")
endif()
if(NOT CMAKE_SYSTEM_VERSION STREQUAL "10.0.22621.0")
  message(FATAL_ERROR "native builds require Windows SDK 10.0.22621.0")
endif()

find_package(Qt6 6.7.1 EXACT REQUIRED COMPONENTS Core Gui Widgets)
if(NOT Qt6Core_VERSION STREQUAL "6.7.1" OR
   NOT Qt6Gui_VERSION STREQUAL "6.7.1" OR
   NOT Qt6Widgets_VERSION STREQUAL "6.7.1")
  message(FATAL_ERROR
    "native builds require Qt Core, Gui, and Widgets 6.7.1 exactly")
endif()

# The project consumes only the pinned uibase headers and import library. It
# deliberately does not add_subdirectory() uibase or execute its CMake files.
add_library(FomodLogger_uibase SHARED IMPORTED GLOBAL)
set_target_properties(FomodLogger_uibase PROPERTIES
  IMPORTED_IMPLIB "${FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY}"
  IMPORTED_LOCATION "${FOMOD_LOGGER_UIBASE_COMPANION_DLL}"
  INTERFACE_INCLUDE_DIRECTORIES
    "${FOMOD_LOGGER_UIBASE_SOURCE_DIR}/include/uibase;${FOMOD_LOGGER_UIBASE_SOURCE_DIR}/include/uibase/game_features"
)
add_library(FomodLogger::uibase ALIAS FomodLogger_uibase)

if(FOMOD_LOGGER_RELEASE_BUILD)
  set(FOMOD_LOGGER_RELEASE_BUILD_JSON true)
  set(FOMOD_LOGGER_RELEASE_VERIFY_COMMAND
      "${CMAKE_COMMAND}"
      "-DFOMOD_LOGGER_SOURCE_DIR=${CMAKE_CURRENT_LIST_DIR}"
      "-DFOMOD_LOGGER_EXPECTED_COMMIT=${FOMOD_LOGGER_GIT_COMMIT}"
      "-DFOMOD_LOGGER_GIT_EXECUTABLE=${GIT_EXECUTABLE}"
      "-DFOMOD_LOGGER_PYTHON_EXECUTABLE=${Python3_EXECUTABLE}"
      "-DFOMOD_LOGGER_GIT_VERIFY_SCRIPT=${CMAKE_CURRENT_LIST_DIR}/packaging/verify-git-source.py"
      "-DFOMOD_LOGGER_VERIFY_TEMP_DIR=${CMAKE_BINARY_DIR}/source-verifier-tools"
      "-DFOMOD_LOGGER_UIBASE_DIR=${FOMOD_LOGGER_UIBASE_SOURCE_DIR}"
      "-DFOMOD_LOGGER_UIBASE_EXPECTED_COMMIT=${FOMOD_LOGGER_UIBASE_COMMIT}"
      "-DFOMOD_LOGGER_UIBASE_EXPECTED_PREFIX=${FOMOD_LOGGER_UIBASE_SOURCE_ARCHIVE_PREFIX}"
      -P "${CMAKE_CURRENT_LIST_DIR}/packaging/verify-release-source.cmake")
else()
  set(FOMOD_LOGGER_RELEASE_BUILD_JSON false)
endif()

add_subdirectory(src)
