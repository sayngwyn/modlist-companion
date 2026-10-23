cmake_minimum_required(VERSION 3.25)

foreach(required IN ITEMS FOMOD_LOGGER_RCC FOMOD_LOGGER_RESOURCE_SOURCE_DIR
                          FOMOD_LOGGER_RESOURCE_OUTPUT)
  if(NOT DEFINED ${required} OR "${${required}}" STREQUAL "")
    message(FATAL_ERROR "${required} is required")
  endif()
endforeach()

# Qt 6.7.1 otherwise embeds checkout/extraction mtimes in the resource table.
# Use the same fixed epoch as ZIP metadata, independently of the caller's env.
set(temporary_output "${FOMOD_LOGGER_RESOURCE_OUTPUT}.tmp")
execute_process(
  COMMAND "${CMAKE_COMMAND}" -E env
    "SOURCE_DATE_EPOCH=315532800" "QT_RCC_SOURCE_DATE_OVERRIDE=315532800"
    "${FOMOD_LOGGER_RCC}" --name resources
    --output "${temporary_output}" resources.qrc
  WORKING_DIRECTORY "${FOMOD_LOGGER_RESOURCE_SOURCE_DIR}"
  RESULT_VARIABLE result
  OUTPUT_VARIABLE output
  ERROR_VARIABLE error
)
if(NOT result EQUAL 0)
  message(FATAL_ERROR "Qt resource generation failed: ${output}${error}")
endif()

# rcc also writes absolute filenames in comments. Their bytes become MSVC
# debug-source checksums even though comments do not affect machine code.
# Change only the exact comment prefix; leave resource data and names intact.
file(READ "${temporary_output}" generated)
file(TO_CMAKE_PATH "${FOMOD_LOGGER_RESOURCE_SOURCE_DIR}" source_directory)
string(REPLACE "// ${source_directory}/" "// " generated "${generated}")
file(WRITE "${temporary_output}" "${generated}")
execute_process(
  COMMAND "${CMAKE_COMMAND}" -E copy_if_different
          "${temporary_output}" "${FOMOD_LOGGER_RESOURCE_OUTPUT}"
  RESULT_VARIABLE copy_result
)
if(NOT copy_result EQUAL 0)
  message(FATAL_ERROR "Unable to publish generated resource source")
endif()
file(REMOVE "${temporary_output}")
