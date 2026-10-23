cmake_minimum_required(VERSION 3.22)

set(required_values
  FOMOD_LOGGER_CONFIGURATION
  FOMOD_LOGGER_RECORD_OUTPUT
  FOMOD_LOGGER_NATIVE_DLL
  FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY
  FOMOD_LOGGER_UIBASE_COMPANION_DLL
  FOMOD_LOGGER_PUBLIC_VERSION
  FOMOD_LOGGER_MO2_VERSION
  FOMOD_LOGGER_RELEASE_BUILD_JSON
  FOMOD_LOGGER_REPOSITORY_COMMIT
  FOMOD_LOGGER_MO2_ABI_TARGET
  FOMOD_LOGGER_GENERATOR
  FOMOD_LOGGER_GENERATOR_PLATFORM
  FOMOD_LOGGER_GENERATOR_TOOLSET
  FOMOD_LOGGER_CXX_COMPILER_ID
  FOMOD_LOGGER_CXX_COMPILER_VERSION
  FOMOD_LOGGER_CMAKE_VERSION
  FOMOD_LOGGER_WINDOWS_SDK
  FOMOD_LOGGER_QT_VERSION
  FOMOD_LOGGER_UIBASE_COMMIT
)
foreach(required_value IN LISTS required_values)
  if(NOT DEFINED ${required_value} OR "${${required_value}}" STREQUAL "")
    message(FATAL_ERROR "${required_value} is required")
  endif()
endforeach()

if(NOT FOMOD_LOGGER_CONFIGURATION STREQUAL "RelWithDebInfo")
  if(FOMOD_LOGGER_RELEASE_BUILD_JSON)
    message(FATAL_ERROR
      "A release build record may only be generated after a RelWithDebInfo link")
  endif()
  message(STATUS
    "Skipping native build record for ${FOMOD_LOGGER_CONFIGURATION}; "
    "RelWithDebInfo is required")
  return()
endif()

foreach(required_file IN ITEMS
    FOMOD_LOGGER_NATIVE_DLL
    FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY
    FOMOD_LOGGER_UIBASE_COMPANION_DLL)
  if(NOT EXISTS "${${required_file}}" OR IS_DIRECTORY "${${required_file}}")
    message(FATAL_ERROR "${required_file} is not an available file")
  endif()
endforeach()

file(SHA256 "${FOMOD_LOGGER_NATIVE_DLL}" native_dll_sha256)
file(SHA256 "${FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY}"
     uibase_import_library_sha256)
file(SHA256 "${FOMOD_LOGGER_UIBASE_COMPANION_DLL}"
     uibase_companion_dll_sha256)
get_filename_component(native_dll_name "${FOMOD_LOGGER_NATIVE_DLL}" NAME)
get_filename_component(uibase_import_library_name
  "${FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY}" NAME)
get_filename_component(uibase_companion_dll_name
  "${FOMOD_LOGGER_UIBASE_COMPANION_DLL}" NAME)

set(record "{\n")
string(APPEND record "  \"format\": 2,\n")
string(APPEND record "  \"product\": \"Modlist Companion\",\n")
string(APPEND record "  \"public_version\": \"${FOMOD_LOGGER_PUBLIC_VERSION}\",\n")
string(APPEND record "  \"mo2_version\": \"${FOMOD_LOGGER_MO2_VERSION}\",\n")
string(APPEND record "  \"release_build\": ${FOMOD_LOGGER_RELEASE_BUILD_JSON},\n")
string(APPEND record "  \"configuration\": \"${FOMOD_LOGGER_CONFIGURATION}\",\n")
string(APPEND record "  \"architecture\": \"x86-64\",\n")
string(APPEND record "  \"repository_commit\": \"${FOMOD_LOGGER_REPOSITORY_COMMIT}\",\n")
string(APPEND record "  \"mo2_abi_target\": \"${FOMOD_LOGGER_MO2_ABI_TARGET}\",\n")
string(APPEND record "  \"generator\": \"${FOMOD_LOGGER_GENERATOR}\",\n")
string(APPEND record "  \"generator_platform\": \"${FOMOD_LOGGER_GENERATOR_PLATFORM}\",\n")
string(APPEND record "  \"generator_toolset\": \"${FOMOD_LOGGER_GENERATOR_TOOLSET}\",\n")
string(APPEND record "  \"cxx_compiler_id\": \"${FOMOD_LOGGER_CXX_COMPILER_ID}\",\n")
string(APPEND record "  \"cxx_compiler_version\": \"${FOMOD_LOGGER_CXX_COMPILER_VERSION}\",\n")
string(APPEND record "  \"cmake_version\": \"${FOMOD_LOGGER_CMAKE_VERSION}\",\n")
string(APPEND record "  \"windows_sdk\": \"${FOMOD_LOGGER_WINDOWS_SDK}\",\n")
string(APPEND record "  \"qt_version\": \"${FOMOD_LOGGER_QT_VERSION}\",\n")
string(APPEND record "  \"qt_modules\": [\"Core\", \"Gui\", \"Widgets\"],\n")
string(APPEND record "  \"uibase_commit\": \"${FOMOD_LOGGER_UIBASE_COMMIT}\",\n")
string(APPEND record "  \"native_dll_name\": \"${native_dll_name}\",\n")
string(APPEND record "  \"native_dll_sha256\": \"${native_dll_sha256}\",\n")
string(APPEND record "  \"uibase_import_library_name\": \"${uibase_import_library_name}\",\n")
string(APPEND record "  \"uibase_import_library_sha256\": \"${uibase_import_library_sha256}\",\n")
string(APPEND record "  \"uibase_companion_dll_name\": \"${uibase_companion_dll_name}\",\n")
string(APPEND record "  \"uibase_companion_dll_sha256\": \"${uibase_companion_dll_sha256}\",\n")
string(APPEND record "  \"uibase_companion_role\": \"build-prefix comparison only; not isolated-MO2 host qualification\"\n")
string(APPEND record "}\n")

get_filename_component(record_directory "${FOMOD_LOGGER_RECORD_OUTPUT}" DIRECTORY)
file(MAKE_DIRECTORY "${record_directory}")
string(RANDOM LENGTH 24 ALPHABET 0123456789abcdef temporary_suffix)
set(temporary_record
    "${record_directory}/.${native_dll_name}.${temporary_suffix}.record.tmp")
file(WRITE "${temporary_record}" "${record}")
file(RENAME "${temporary_record}" "${FOMOD_LOGGER_RECORD_OUTPUT}"
     RESULT rename_result)
if(NOT rename_result STREQUAL "0")
  file(REMOVE "${temporary_record}")
  message(FATAL_ERROR
    "Unable to atomically publish native build record: ${rename_result}")
endif()
