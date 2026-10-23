cmake_minimum_required(VERSION 3.25)

foreach(required_variable IN ITEMS
        FOMOD_LOGGER_SOURCE_DIR
        FOMOD_LOGGER_EXPECTED_COMMIT)
  if(NOT DEFINED ${required_variable} OR "${${required_variable}}" STREQUAL "")
    message(FATAL_ERROR "release source check is missing ${required_variable}")
  endif()
endforeach()

set(FOMOD_LOGGER_CLEAN_GIT_ENV
  "${CMAKE_COMMAND}" -E env
  "--unset=GIT_DIR"
  "--unset=GIT_WORK_TREE"
  "--unset=GIT_COMMON_DIR"
  "--unset=GIT_INDEX_FILE"
  "--unset=GIT_OBJECT_DIRECTORY"
  "--unset=GIT_ALTERNATE_OBJECT_DIRECTORIES"
  "--unset=GIT_QUARANTINE_PATH"
  "--unset=GIT_CONFIG_COUNT"
  "--unset=GIT_CONFIG_PARAMETERS"
  "--unset=GIT_CONFIG_SYSTEM"
  "--unset=GIT_CONFIG_GLOBAL"
  "--unset=GIT_EXEC_PATH"
  "GIT_OPTIONAL_LOCKS=0"
  "GIT_NO_REPLACE_OBJECTS=1"
)

function(fomod_logger_verify_git_bytes source_dir expected_commit label)
  foreach(git_required IN ITEMS
          FOMOD_LOGGER_GIT_EXECUTABLE
          FOMOD_LOGGER_PYTHON_EXECUTABLE
          FOMOD_LOGGER_GIT_VERIFY_SCRIPT
          FOMOD_LOGGER_VERIFY_TEMP_DIR)
    if(NOT DEFINED ${git_required} OR
       "${${git_required}}" STREQUAL "")
      message(FATAL_ERROR "${label} Git mode is missing ${git_required}")
    endif()
  endforeach()
  if(NOT EXISTS "${FOMOD_LOGGER_GIT_EXECUTABLE}" OR
     NOT EXISTS "${FOMOD_LOGGER_PYTHON_EXECUTABLE}" OR
     NOT EXISTS "${FOMOD_LOGGER_GIT_VERIFY_SCRIPT}")
    message(FATAL_ERROR "${label} Git verification tool is unavailable")
  endif()

  file(REAL_PATH "${FOMOD_LOGGER_SOURCE_DIR}" logger_source_real)
  file(REAL_PATH
       "${FOMOD_LOGGER_SOURCE_DIR}/packaging/verify-git-source.py"
       expected_driver_real)
  file(REAL_PATH "${FOMOD_LOGGER_GIT_VERIFY_SCRIPT}" supplied_driver_real)
  if(NOT supplied_driver_real STREQUAL expected_driver_real)
    message(FATAL_ERROR
      "Git verifier must be the source-bound packaging/verify-git-source.py")
  endif()

  execute_process(
    COMMAND ${FOMOD_LOGGER_CLEAN_GIT_ENV}
            "${FOMOD_LOGGER_GIT_EXECUTABLE}"
            -C "${FOMOD_LOGGER_SOURCE_DIR}"
            rev-parse --show-toplevel
    RESULT_VARIABLE driver_root_result
    OUTPUT_VARIABLE driver_git_root
    ERROR_VARIABLE driver_root_error
    OUTPUT_STRIP_TRAILING_WHITESPACE
  )
  if(NOT driver_root_result EQUAL 0)
    message(FATAL_ERROR
      "unable to resolve verifier source root: ${driver_root_error}")
  endif()
  file(REAL_PATH "${driver_git_root}" driver_git_root_real)
  if(NOT driver_git_root_real STREQUAL logger_source_real)
    message(FATAL_ERROR
      "verifier Git top-level ${driver_git_root_real} does not match "
      "Logger source root ${logger_source_real}")
  endif()

  execute_process(
    COMMAND ${FOMOD_LOGGER_CLEAN_GIT_ENV}
            "${FOMOD_LOGGER_GIT_EXECUTABLE}"
            -C "${FOMOD_LOGGER_SOURCE_DIR}"
            rev-parse --verify HEAD
    RESULT_VARIABLE driver_head_result
    OUTPUT_VARIABLE driver_head
    ERROR_VARIABLE driver_head_error
    OUTPUT_STRIP_TRAILING_WHITESPACE
  )
  if(NOT driver_head_result EQUAL 0 OR
     NOT driver_head STREQUAL FOMOD_LOGGER_EXPECTED_COMMIT)
    message(FATAL_ERROR
      "verifier source HEAD does not match expected commit "
      "${FOMOD_LOGGER_EXPECTED_COMMIT}: ${driver_head_error}")
  endif()

  file(MAKE_DIRECTORY "${FOMOD_LOGGER_VERIFY_TEMP_DIR}")
  string(RANDOM LENGTH 16 ALPHABET 0123456789abcdef verifier_nonce)
  set(materialized_driver
      "${FOMOD_LOGGER_VERIFY_TEMP_DIR}/verify-git-source-${verifier_nonce}.py")
  execute_process(
    COMMAND ${FOMOD_LOGGER_CLEAN_GIT_ENV}
            "${FOMOD_LOGGER_GIT_EXECUTABLE}"
            -C "${FOMOD_LOGGER_SOURCE_DIR}"
            show
            "${FOMOD_LOGGER_EXPECTED_COMMIT}:packaging/verify-git-source.py"
    RESULT_VARIABLE materialize_result
    ERROR_VARIABLE materialize_error
    OUTPUT_FILE "${materialized_driver}"
  )
  if(NOT materialize_result EQUAL 0)
    file(REMOVE "${materialized_driver}")
    message(FATAL_ERROR
      "unable to materialize commit-bound Git verifier: ${materialize_error}")
  endif()

  execute_process(
    COMMAND "${FOMOD_LOGGER_PYTHON_EXECUTABLE}" -I -S -B
            "${materialized_driver}"
            --root "${source_dir}"
            --expected-commit "${expected_commit}"
            --git-executable "${FOMOD_LOGGER_GIT_EXECUTABLE}"
    RESULT_VARIABLE byte_result
    OUTPUT_VARIABLE byte_output
    ERROR_VARIABLE byte_error
  )
  file(REMOVE "${materialized_driver}")
  if(NOT byte_result EQUAL 0)
    message(FATAL_ERROR
      "${label} tracked-byte verification failed:\n"
      "${byte_output}${byte_error}")
  endif()
  string(JSON verified_commit ERROR_VARIABLE verified_commit_error
         GET "${byte_output}" commit)
  string(JSON verified_root ERROR_VARIABLE verified_root_error
         GET "${byte_output}" root)
  string(JSON verified_count_type ERROR_VARIABLE verified_count_error
         TYPE "${byte_output}" tracked_file_count)
  file(REAL_PATH "${source_dir}" expected_verified_root)
  file(TO_CMAKE_PATH "${verified_root}" normalized_verified_root)
  file(TO_CMAKE_PATH "${expected_verified_root}" normalized_expected_root)
  if(NOT verified_commit_error STREQUAL "NOTFOUND" OR
     NOT verified_root_error STREQUAL "NOTFOUND" OR
     NOT verified_count_error STREQUAL "NOTFOUND" OR
     NOT verified_commit STREQUAL expected_commit OR
     NOT normalized_verified_root STREQUAL normalized_expected_root OR
     NOT verified_count_type STREQUAL "NUMBER")
    message(FATAL_ERROR
      "${label} verifier returned an invalid or mismatched identity record")
  endif()
endfunction()

set(source_manifest
    "${FOMOD_LOGGER_SOURCE_DIR}/fomod_logger-source-manifest.json")

# Manifest mode is authoritative whenever a source manifest exists.
if(EXISTS "${source_manifest}")
  if(IS_SYMLINK "${source_manifest}")
    message(FATAL_ERROR "source manifest must not be a symbolic link")
  endif()
  if(EXISTS "${FOMOD_LOGGER_SOURCE_DIR}/.git")
    message(FATAL_ERROR "manifest-bound source must not contain a .git marker")
  endif()
  file(READ "${source_manifest}" manifest_json)
  string(JSON manifest_commit ERROR_VARIABLE manifest_commit_error
         GET "${manifest_json}" repository_commit)
  if(NOT manifest_commit_error STREQUAL "NOTFOUND")
    message(FATAL_ERROR
      "unable to read repository_commit from source manifest: "
      "${manifest_commit_error}")
  endif()
  if(NOT manifest_commit STREQUAL FOMOD_LOGGER_EXPECTED_COMMIT)
    message(FATAL_ERROR
      "source manifest commit ${manifest_commit} does not match expected "
      "${FOMOD_LOGGER_EXPECTED_COMMIT}")
  endif()

  string(JSON manifest_file_count LENGTH "${manifest_json}" files)
  set(expected_files)
  set(expected_directories)
  if(manifest_file_count GREATER 0)
    math(EXPR manifest_last_index "${manifest_file_count} - 1")
    foreach(file_index RANGE 0 ${manifest_last_index})
      string(JSON file_name MEMBER "${manifest_json}" files ${file_index})
      if(IS_ABSOLUTE "${file_name}" OR
         file_name MATCHES "\\\\" OR
         file_name MATCHES ":" OR
         file_name MATCHES "(^|/)(\\.|\\.\\.)($|/)")
        message(FATAL_ERROR "unsafe path in source manifest: ${file_name}")
      endif()
      set(file_path "${FOMOD_LOGGER_SOURCE_DIR}/${file_name}")
      if(NOT EXISTS "${file_path}" OR IS_DIRECTORY "${file_path}" OR
         IS_SYMLINK "${file_path}")
        message(FATAL_ERROR "source manifest file is unavailable: ${file_name}")
      endif()
      string(JSON expected_sha GET "${manifest_json}" files "${file_name}")
      file(SHA256 "${file_path}" actual_sha)
      if(NOT actual_sha STREQUAL expected_sha)
        message(FATAL_ERROR "source manifest hash mismatch: ${file_name}")
      endif()
      list(APPEND expected_files "${file_name}")

      get_filename_component(parent_dir "${file_name}" DIRECTORY)
      while(NOT parent_dir STREQUAL "")
        list(APPEND expected_directories "${parent_dir}")
        get_filename_component(parent_dir "${parent_dir}" DIRECTORY)
      endwhile()
    endforeach()
  endif()

  file(GLOB_RECURSE actual_entries LIST_DIRECTORIES true
       RELATIVE "${FOMOD_LOGGER_SOURCE_DIR}"
       "${FOMOD_LOGGER_SOURCE_DIR}/*")
  set(actual_files)
  set(actual_directories)
  foreach(actual_entry IN LISTS actual_entries)
    set(actual_path "${FOMOD_LOGGER_SOURCE_DIR}/${actual_entry}")
    if(IS_SYMLINK "${actual_path}")
      message(FATAL_ERROR
        "extracted source contains a symbolic link: ${actual_entry}")
    elseif(IS_DIRECTORY "${actual_path}")
      list(APPEND actual_directories "${actual_entry}")
    else()
      list(APPEND actual_files "${actual_entry}")
    endif()
  endforeach()
  list(REMOVE_ITEM actual_files "fomod_logger-source-manifest.json")
  list(REMOVE_DUPLICATES expected_directories)
  list(SORT actual_files)
  list(SORT expected_files)
  list(SORT actual_directories)
  list(SORT expected_directories)
  if(NOT actual_files STREQUAL expected_files OR
     NOT actual_directories STREQUAL expected_directories)
    message(FATAL_ERROR
      "extracted source tree does not exactly match the source manifest")
  endif()

  foreach(manifest_required IN ITEMS
          FOMOD_LOGGER_PYTHON_EXECUTABLE
          FOMOD_LOGGER_GIT_VERIFY_SCRIPT)
    if(NOT DEFINED ${manifest_required} OR
       "${${manifest_required}}" STREQUAL "" OR
       NOT EXISTS "${${manifest_required}}")
      message(FATAL_ERROR
        "manifest source verification is missing ${manifest_required}")
    endif()
  endforeach()
  file(REAL_PATH
       "${FOMOD_LOGGER_SOURCE_DIR}/packaging/verify-git-source.py"
       manifest_expected_driver)
  file(REAL_PATH "${FOMOD_LOGGER_GIT_VERIFY_SCRIPT}" manifest_supplied_driver)
  if(NOT manifest_supplied_driver STREQUAL manifest_expected_driver)
    message(FATAL_ERROR
      "manifest verifier must be the manifest-bound packaging/verify-git-source.py")
  endif()
  execute_process(
    COMMAND "${FOMOD_LOGGER_PYTHON_EXECUTABLE}" -I -S -B
            "${FOMOD_LOGGER_GIT_VERIFY_SCRIPT}"
            --root "${FOMOD_LOGGER_SOURCE_DIR}"
            --expected-commit "${FOMOD_LOGGER_EXPECTED_COMMIT}"
            --manifest
    RESULT_VARIABLE manifest_tree_result
    OUTPUT_VARIABLE manifest_tree_output
    ERROR_VARIABLE manifest_tree_error
  )
  if(NOT manifest_tree_result EQUAL 0)
    message(FATAL_ERROR
      "manifest source filesystem verification failed:\n"
      "${manifest_tree_output}${manifest_tree_error}")
  endif()
  string(JSON manifest_verified_commit ERROR_VARIABLE manifest_commit_result_error
         GET "${manifest_tree_output}" commit)
  string(JSON manifest_verified_root ERROR_VARIABLE manifest_root_result_error
         GET "${manifest_tree_output}" root)
  string(JSON manifest_verified_count_type ERROR_VARIABLE manifest_count_result_error
         TYPE "${manifest_tree_output}" tracked_file_count)
  file(REAL_PATH "${FOMOD_LOGGER_SOURCE_DIR}" manifest_expected_root)
  file(TO_CMAKE_PATH "${manifest_verified_root}" manifest_result_root)
  file(TO_CMAKE_PATH "${manifest_expected_root}" manifest_expected_root_normalized)
  if(NOT manifest_commit_result_error STREQUAL "NOTFOUND" OR
     NOT manifest_root_result_error STREQUAL "NOTFOUND" OR
     NOT manifest_count_result_error STREQUAL "NOTFOUND" OR
     NOT manifest_verified_commit STREQUAL FOMOD_LOGGER_EXPECTED_COMMIT OR
     NOT manifest_result_root STREQUAL manifest_expected_root_normalized OR
     NOT manifest_verified_count_type STREQUAL "NUMBER")
    message(FATAL_ERROR
      "manifest source verifier returned an invalid or mismatched identity record")
  endif()
elseif(EXISTS "${FOMOD_LOGGER_SOURCE_DIR}/.git")
  fomod_logger_verify_git_bytes(
    "${FOMOD_LOGGER_SOURCE_DIR}"
    "${FOMOD_LOGGER_EXPECTED_COMMIT}"
    "release source")
else()
  message(FATAL_ERROR
    "release source must be either an exact-root Git checkout or an extracted "
    "corresponding-source archive with fomod_logger-source-manifest.json")
endif()

set(uibase_identity_variables
  FOMOD_LOGGER_UIBASE_DIR
  FOMOD_LOGGER_UIBASE_EXPECTED_COMMIT
  FOMOD_LOGGER_UIBASE_EXPECTED_PREFIX
)
set(uibase_identity_count 0)
foreach(uibase_identity_variable IN LISTS uibase_identity_variables)
  if(DEFINED ${uibase_identity_variable} AND
     NOT "${${uibase_identity_variable}}" STREQUAL "")
    math(EXPR uibase_identity_count "${uibase_identity_count} + 1")
  endif()
endforeach()

if(NOT uibase_identity_count EQUAL 0)
  if(NOT uibase_identity_count EQUAL 3)
    message(FATAL_ERROR "incomplete uibase release-source identity")
  endif()

  if(EXISTS "${source_manifest}")
    if(IS_ABSOLUTE "${FOMOD_LOGGER_UIBASE_EXPECTED_PREFIX}" OR
       FOMOD_LOGGER_UIBASE_EXPECTED_PREFIX MATCHES "(^|/)\\.\\.(/|$)")
      message(FATAL_ERROR "unsafe embedded uibase prefix")
    endif()
    if(EXISTS "${FOMOD_LOGGER_UIBASE_DIR}/.git")
      message(FATAL_ERROR
        "manifest-bound uibase source must not contain a .git marker")
    endif()
    string(JSON embedded_uibase_commit ERROR_VARIABLE embedded_commit_error
           GET "${manifest_json}" embedded_git_sources uibase commit)
    if(NOT embedded_commit_error STREQUAL "NOTFOUND" OR
       NOT embedded_uibase_commit STREQUAL FOMOD_LOGGER_UIBASE_EXPECTED_COMMIT)
      message(FATAL_ERROR
        "source manifest does not bind the expected uibase commit")
    endif()
    string(JSON embedded_uibase_prefix ERROR_VARIABLE embedded_prefix_error
           GET "${manifest_json}" embedded_git_sources uibase path)
    if(NOT embedded_prefix_error STREQUAL "NOTFOUND" OR
       NOT embedded_uibase_prefix STREQUAL FOMOD_LOGGER_UIBASE_EXPECTED_PREFIX)
      message(FATAL_ERROR
        "source manifest does not bind the expected uibase path")
    endif()
    file(REAL_PATH "${FOMOD_LOGGER_UIBASE_DIR}" uibase_real_path)
    file(REAL_PATH
         "${FOMOD_LOGGER_SOURCE_DIR}/${FOMOD_LOGGER_UIBASE_EXPECTED_PREFIX}"
         embedded_uibase_real_path)
    if(NOT uibase_real_path STREQUAL embedded_uibase_real_path)
      message(FATAL_ERROR
        "uibase source path is not the manifest-bound embedded snapshot")
    endif()
  elseif(EXISTS "${FOMOD_LOGGER_UIBASE_DIR}/.git")
    fomod_logger_verify_git_bytes(
      "${FOMOD_LOGGER_UIBASE_DIR}"
      "${FOMOD_LOGGER_UIBASE_EXPECTED_COMMIT}"
      "uibase source")
  else()
    message(FATAL_ERROR
      "uibase source is neither an exact-root Git checkout nor a "
      "manifest-bound snapshot")
  endif()
endif()
