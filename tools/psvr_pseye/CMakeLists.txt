# SPDX-License-Identifier: MIT
#
# psvr_pseye - native PS3 Eye capture module.
#
# Two artifacts:
#   psvr_pseye.dll  MIT shim, no link-time third-party dependency (always built)
#   ps3eye.dll      upstream PS3EYEDriver (MIT + GPLv2), built from a pinned
#                   commit plus the in-repo bounded-grab patch, links libusb-1.0
#                   (LGPL-2.1) at run time
#
# The shim loads ps3eye.dll with LoadLibraryEx. That keeps the shim source MIT,
# but a distribution containing both DLLs is a combined distribution that must
# satisfy the GPLv2 for ps3eye.dll; the stage ships the full licence texts and
# NOTICE.txt. See README.md.
#
# The upstream source is never edited in place: it is copied into the build
# tree and patches/0001-bounded-grab-and-actual-fps.patch is applied there, so
# the build is reproducible from the pin plus the tracked patch.
#
# Intended to be added with add_subdirectory(tools/psvr_pseye) by the parent,
# but also builds standalone.

cmake_minimum_required(VERSION 3.24)
project(psvr_pseye LANGUAGES C CXX)

if(NOT WIN32)
    message(FATAL_ERROR "psvr_pseye requires Windows (libusb-win32 binding)")
endif()

# --------------------------------------------------------------------------- #
# MIT shim: always built, no third-party link dependency.
# --------------------------------------------------------------------------- #
add_library(psvr_pseye SHARED src/psvr_pseye.cpp)
target_include_directories(psvr_pseye PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/include")
set_target_properties(psvr_pseye PROPERTIES PREFIX "" OUTPUT_NAME "psvr_pseye"
    WINDOWS_EXPORT_ALL_SYMBOLS ON)
target_compile_definitions(psvr_pseye PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
if(MSVC)
    target_compile_options(psvr_pseye PRIVATE /W4 /permissive- /utf-8)
else()
    # GetProcAddress -> typed function pointer is the standard dynamic-load
    # idiom; silence the otherwise-noisy cast-function-type warning.
    target_compile_options(psvr_pseye PRIVATE
        -Wall -Wextra -Wpedantic -Wno-cast-function-type)
    target_link_options(psvr_pseye PRIVATE -static-libgcc -static-libstdc++ -static)
endif()

# --------------------------------------------------------------------------- #
# Optional: build the pinned upstream capture library ps3eye.dll.
# --------------------------------------------------------------------------- #
option(PSVR_PSEYE_BUILD_UPSTREAM
    "Build ps3eye.dll from pinned PS3EYEDriver source" ON)
set(PSVR_PSEYE_UPSTREAM_TAG
    "fe03494e648d0bf2aaa77869e5b50965e2e23620"
    CACHE STRING "Pinned inspirit/PS3EYEDriver commit")
set(PSVR_PSEYE_LIBUSB_DLL "" CACHE FILEPATH
    "libusb-1.0.dll to link ps3eye.dll against")
set(PSVR_PSEYE_LIBUSB_INCLUDE_DIR "" CACHE PATH
    "Directory containing libusb.h")
set(PSVR_PSEYE_LIBUSB_LIB "" CACHE FILEPATH
    "libusb-1.0 import library (.lib); required for an MSVC build")
set(PSVR_PSEYE_PATCHED_LIBUSB_SOURCE "" CACHE PATH
    "Patched libusb source/libusb directory, enables dependency body tests")

set(PSVR_PSEYE_PATCH
    "${CMAKE_CURRENT_SOURCE_DIR}/patches/0001-bounded-grab-and-actual-fps.patch")
set(PSVR_PSEYE_PATCHES "${PSVR_PSEYE_PATCH}"
    "${CMAKE_CURRENT_SOURCE_DIR}/patches/0002-transfer-fault-lifetime.patch"
    "${CMAKE_CURRENT_SOURCE_DIR}/patches/0003-refresh-idle-inventory.patch")
get_filename_component(PSVR_PSEYE_REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)

if(PSVR_PSEYE_BUILD_UPSTREAM)
    find_package(Git QUIET)
    if(NOT GIT_EXECUTABLE)
        message(FATAL_ERROR
            "git is required to fetch and patch the pinned ps3eye source "
            "(or configure with -DPSVR_PSEYE_BUILD_UPSTREAM=OFF)")
    endif()

    include(FetchContent)
    if(POLICY CMP0169)
        cmake_policy(SET CMP0169 OLD)
    endif()
    set(FETCHCONTENT_QUIET OFF)
    FetchContent_Declare(ps3eyedriver
        GIT_REPOSITORY https://github.com/inspirit/PS3EYEDriver.git
        GIT_TAG ${PSVR_PSEYE_UPSTREAM_TAG})
    FetchContent_GetProperties(ps3eyedriver)
    if(NOT ps3eyedriver_POPULATED)
        FetchContent_Populate(ps3eyedriver)
    endif()

    # Copy the pinned source into the build tree and apply the tracked patch
    # there. The fetch/checkout directory is never modified, so a clean
    # configure always reproduces the same patched source.
    set(PSVR_PSEYE_UPSTREAM_DIR
        "${CMAKE_CURRENT_BINARY_DIR}/upstream/PS3EYEDriver")
    # Recreate only the known generated inputs, including on patch changes.
    # Never mutate the fetched checkout or rely on reverse-applying old hunks.
    file(MAKE_DIRECTORY "${PSVR_PSEYE_UPSTREAM_DIR}/src")
    foreach(source ps3eye.cpp ps3eye.h ps3eye_capi.cpp ps3eye_capi.h)
        configure_file("${ps3eyedriver_SOURCE_DIR}/src/${source}"
            "${PSVR_PSEYE_UPSTREAM_DIR}/src/${source}" COPYONLY)
    endforeach()
    configure_file("${ps3eyedriver_SOURCE_DIR}/LICENSE"
        "${PSVR_PSEYE_UPSTREAM_DIR}/LICENSE" COPYONLY)
    foreach(patch IN LISTS PSVR_PSEYE_PATCHES)
        set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${patch}")
        # The generated copy lives below the outer repository's ignored build/
        # directory. Stop Git discovery at the PSVR root: otherwise git apply
        # silently skips diff --git hunks as ignored and later patch guards fail.
        execute_process(
            COMMAND "${CMAKE_COMMAND}" -E env
                "GIT_CEILING_DIRECTORIES=${PSVR_PSEYE_REPO_ROOT}"
                "${GIT_EXECUTABLE}" apply --whitespace=nowarn "${patch}"
            WORKING_DIRECTORY "${PSVR_PSEYE_UPSTREAM_DIR}"
            RESULT_VARIABLE PSVR_PSEYE_PATCH_RESULT
            ERROR_VARIABLE PSVR_PSEYE_PATCH_ERROR)
        if(NOT PSVR_PSEYE_PATCH_RESULT EQUAL 0)
            message(FATAL_ERROR
                "failed to apply ${patch}:\n"
                "${PSVR_PSEYE_PATCH_ERROR}")
        endif()
        message(STATUS "ps3eye: applied ${patch}")
    endforeach()

    # find_file()/find_path() skip the search when the cache variable already
    # exists, so the empty CACHE seed above must be removed before searching.
    if(NOT PSVR_PSEYE_LIBUSB_DLL)
        unset(PSVR_PSEYE_LIBUSB_DLL CACHE)
        find_file(PSVR_PSEYE_LIBUSB_DLL NAMES libusb-1.0.dll
            PATHS
                "$ENV{PSVR_PSEYE_LIBUSB_DIR}"
                "C:/Program Files (x86)/Steam"
                "C:/Program Files/FreeCAD 1.0/bin"
            NO_DEFAULT_PATH)
    endif()
    if(NOT PSVR_PSEYE_LIBUSB_INCLUDE_DIR)
        unset(PSVR_PSEYE_LIBUSB_INCLUDE_DIR CACHE)
        find_path(PSVR_PSEYE_LIBUSB_INCLUDE_DIR NAMES libusb.h
            PATHS
                "${ps3eyedriver_SOURCE_DIR}/../libusb/libusb"
                "$ENV{PSVR_PSEYE_LIBUSB_DIR}"
            NO_DEFAULT_PATH)
    endif()

    # MSVC cannot link a .dll directly; it needs the import library. MinGW can.
    set(PSVR_PSEYE_LIBUSB_LINK "${PSVR_PSEYE_LIBUSB_DLL}")
    if(MSVC)
        if(NOT PSVR_PSEYE_LIBUSB_LIB AND PSVR_PSEYE_LIBUSB_DLL)
            get_filename_component(PSVR_PSEYE_LIBUSB_DLL_DIR
                "${PSVR_PSEYE_LIBUSB_DLL}" DIRECTORY)
            # find_library() also skips the search when the cache variable is
            # already defined, so drop the empty seed first.
            unset(PSVR_PSEYE_LIBUSB_LIB CACHE)
            find_library(PSVR_PSEYE_LIBUSB_LIB NAMES libusb-1.0
                PATHS "${PSVR_PSEYE_LIBUSB_DLL_DIR}" NO_DEFAULT_PATH)
        endif()
        set(PSVR_PSEYE_LIBUSB_LINK "${PSVR_PSEYE_LIBUSB_LIB}")
    endif()

    if(NOT PSVR_PSEYE_LIBUSB_DLL OR NOT PSVR_PSEYE_LIBUSB_INCLUDE_DIR OR
       NOT PSVR_PSEYE_LIBUSB_LINK)
        message(WARNING
            "ps3eye.dll not built: set PSVR_PSEYE_LIBUSB_DLL and "
            "PSVR_PSEYE_LIBUSB_INCLUDE_DIR (and, with MSVC, "
            "PSVR_PSEYE_LIBUSB_LIB to libusb-1.0.lib); see "
            "tools/psvr_pseye/README.md. The MIT shim psvr_pseye.dll is still "
            "built.")
    else()
        add_library(ps3eye SHARED
            "${PSVR_PSEYE_UPSTREAM_DIR}/src/ps3eye.cpp"
            "${PSVR_PSEYE_UPSTREAM_DIR}/src/ps3eye_capi.cpp")
        set_target_properties(ps3eye PROPERTIES PREFIX "" OUTPUT_NAME "ps3eye"
            WINDOWS_EXPORT_ALL_SYMBOLS ON)
        target_include_directories(ps3eye PRIVATE
            "${PSVR_PSEYE_LIBUSB_INCLUDE_DIR}"
            "${PSVR_PSEYE_UPSTREAM_DIR}/src")
        target_compile_definitions(ps3eye PRIVATE WIN32)
        target_link_libraries(ps3eye PRIVATE "${PSVR_PSEYE_LIBUSB_LINK}")
        if(MSVC)
            target_compile_options(ps3eye PRIVATE /W3 /utf-8)
        else()
            target_compile_options(ps3eye PRIVATE -w)
            target_link_options(ps3eye PRIVATE -static-libgcc -static-libstdc++ -static)
        endif()
        add_custom_command(TARGET ps3eye POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${PSVR_PSEYE_LIBUSB_DLL}" "$<TARGET_FILE_DIR:ps3eye>"
            COMMENT "Staging libusb-1.0.dll next to ps3eye.dll")
    endif()
endif()

# --------------------------------------------------------------------------- #
# Stage into out/pseye for the Python adapter and the runtime.
# --------------------------------------------------------------------------- #
# <repo>/out/pseye whether built standalone (-S tools/psvr_pseye) or via
# add_subdirectory(tools/psvr_pseye).
include(CTest)
if(BUILD_TESTING AND PSVR_PSEYE_BUILD_UPSTREAM)
    find_package(Python3 3.11 COMPONENTS Interpreter REQUIRED)
    set(fixture_dir "${CMAKE_CURRENT_BINARY_DIR}/offline-fixtures")
    set(fixture_files "${fixture_dir}/transfer_methods.inc" "${fixture_dir}/transfer_callback.inc"
        "${fixture_dir}/inventory_context.inc" "${fixture_dir}/inventory_camera.inc"
        "${fixture_dir}/inventory_calls.inc")
    add_custom_command(OUTPUT ${fixture_files}
        COMMAND "${Python3_EXECUTABLE}" -B "${PSVR_PSEYE_REPO_ROOT}/tests/generate_pseye_fixture.py"
            "${PSVR_PSEYE_UPSTREAM_DIR}" "${fixture_dir}"
        DEPENDS "${PSVR_PSEYE_REPO_ROOT}/tests/generate_pseye_fixture.py"
            "${PSVR_PSEYE_UPSTREAM_DIR}/src/ps3eye.cpp"
            "${PSVR_PSEYE_UPSTREAM_DIR}/src/ps3eye_capi.cpp")
    add_custom_target(psvr_pseye_offline_fixtures DEPENDS ${fixture_files})
    add_executable(psvr_pseye_transfer_tests "${PSVR_PSEYE_REPO_ROOT}/tests/pseye_transfer_tests.cpp"
        "${fixture_dir}/transfer_methods.inc" "${fixture_dir}/transfer_callback.inc")
    target_compile_features(psvr_pseye_transfer_tests PRIVATE cxx_std_17)
    target_include_directories(psvr_pseye_transfer_tests PRIVATE "${fixture_dir}")
    add_dependencies(psvr_pseye_transfer_tests psvr_pseye_offline_fixtures)
    add_test(NAME pseye_transfer_offline COMMAND psvr_pseye_transfer_tests)
    set_tests_properties(pseye_transfer_offline PROPERTIES TIMEOUT 10)
    add_executable(psvr_pseye_inventory_tests "${PSVR_PSEYE_REPO_ROOT}/tests/pseye_inventory_tests.cpp"
        "${fixture_dir}/inventory_context.inc" "${fixture_dir}/inventory_camera.inc"
        "${fixture_dir}/inventory_calls.inc")
    target_compile_features(psvr_pseye_inventory_tests PRIVATE cxx_std_17)
    target_include_directories(psvr_pseye_inventory_tests PRIVATE "${fixture_dir}")
    add_dependencies(psvr_pseye_inventory_tests psvr_pseye_offline_fixtures)
    add_test(NAME pseye_inventory_offline COMMAND psvr_pseye_inventory_tests)
    set_tests_properties(pseye_inventory_offline PROPERTIES TIMEOUT 10)
    add_library(psvr_pseye_legacy_fixture SHARED "${PSVR_PSEYE_REPO_ROOT}/tests/pseye_legacy_fixture.cpp")
    set_target_properties(psvr_pseye_legacy_fixture PROPERTIES PREFIX "" OUTPUT_NAME "ps3eye"
        WINDOWS_EXPORT_ALL_SYMBOLS ON
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/legacy-fixture"
        ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/legacy-fixture"
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/legacy-fixture")
    add_test(NAME pseye_upstream_patch_guard COMMAND "${Python3_EXECUTABLE}" -B
        "${PSVR_PSEYE_REPO_ROOT}/tests/check_pseye_patch_guard.py"
        "$<TARGET_FILE:psvr_pseye>" "$<TARGET_FILE:psvr_pseye_legacy_fixture>")
    if(PSVR_PSEYE_PATCHED_LIBUSB_SOURCE)
        set(libusb_fixture_dir "${fixture_dir}/libusb")
        set(libusb_fixtures "${libusb_fixture_dir}/control_result.inc"
            "${libusb_fixture_dir}/control_copy_guard.inc")
        add_custom_command(OUTPUT ${libusb_fixtures}
            COMMAND "${Python3_EXECUTABLE}" -B
                "${PSVR_PSEYE_REPO_ROOT}/tests/generate_libusb_fixture.py"
                "${PSVR_PSEYE_PATCHED_LIBUSB_SOURCE}" "${libusb_fixture_dir}"
            DEPENDS "${PSVR_PSEYE_REPO_ROOT}/tests/generate_libusb_fixture.py"
                "${PSVR_PSEYE_PATCHED_LIBUSB_SOURCE}/os/windows_winusb.c"
                "${PSVR_PSEYE_PATCHED_LIBUSB_SOURCE}/sync.c")
        add_executable(psvr_libusb_control_tests
            "${PSVR_PSEYE_REPO_ROOT}/tests/libusb_control_length_tests.cpp" ${libusb_fixtures})
        target_compile_features(psvr_libusb_control_tests PRIVATE cxx_std_17)
        target_include_directories(psvr_libusb_control_tests PRIVATE "${libusb_fixture_dir}")
        add_test(NAME pseye_libusb_control_offline COMMAND psvr_libusb_control_tests)
    endif()
endif()
set(PSVR_PSEYE_STAGE "${PSVR_PSEYE_REPO_ROOT}/out/pseye" CACHE PATH
    "Staging directory for the pseye runtime")
add_custom_target(psvr_pseye_package ALL
    COMMAND ${CMAKE_COMMAND} -E make_directory "${PSVR_PSEYE_STAGE}"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "$<TARGET_FILE:psvr_pseye>" "${PSVR_PSEYE_STAGE}/psvr_pseye.dll"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/README.md" "${PSVR_PSEYE_STAGE}/README.md"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/NOTICE.txt" "${PSVR_PSEYE_STAGE}/NOTICE.txt"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/COPYING.GPL-2.0" "${PSVR_PSEYE_STAGE}/COPYING.GPL-2.0"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/COPYING.LGPL-2.1" "${PSVR_PSEYE_STAGE}/COPYING.LGPL-2.1"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${PSVR_PSEYE_PATCH}" "${PSVR_PSEYE_STAGE}/ps3eye-bounded-grab.patch"
    DEPENDS psvr_pseye)
foreach(patch IN LISTS PSVR_PSEYE_PATCHES)
    add_custom_command(TARGET psvr_pseye_package POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different "${patch}" "${PSVR_PSEYE_STAGE}/")
endforeach()
if(PSVR_PSEYE_PATCHED_LIBUSB_SOURCE)
    add_custom_command(TARGET psvr_pseye_package POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${CMAKE_CURRENT_SOURCE_DIR}/patches/libusb-0001-control-overlapped-length.patch"
            "${PSVR_PSEYE_STAGE}/")
endif()
if(TARGET ps3eye)
    add_custom_command(TARGET psvr_pseye_package POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_FILE:ps3eye>" "${PSVR_PSEYE_STAGE}/ps3eye.dll"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${PSVR_PSEYE_LIBUSB_DLL}" "${PSVR_PSEYE_STAGE}/libusb-1.0.dll"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${PSVR_PSEYE_UPSTREAM_DIR}/LICENSE"
            "${PSVR_PSEYE_STAGE}/PS3EYEDriver-LICENSE.txt"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${PSVR_PSEYE_REPO_ROOT}/LICENSE"
            "${PSVR_PSEYE_STAGE}/psvr_pseye-MIT.txt")
    add_dependencies(psvr_pseye_package ps3eye)
endif()
