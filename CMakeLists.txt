cmake_minimum_required(VERSION 3.24)
project(psvr_pc VERSION 0.3.0 LANGUAGES C CXX)

if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "PSVR-PC milestone 1 requires Windows x64")
endif()
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

include(FetchContent)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "Static dependencies" FORCE)
set(JSON_BuildTests OFF CACHE BOOL "No dependency tests" FORCE)
set(JSON_Install OFF CACHE BOOL "No dependency install" FORCE)
FetchContent_Declare(hidapi
    GIT_REPOSITORY https://github.com/libusb/hidapi.git
    GIT_TAG d6b2a974608dec3b76fb1e36c189f22b9cf3650c)
FetchContent_Declare(nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG 55f93686c01528224f448c19128836e7df245f72)
FetchContent_MakeAvailable(hidapi nlohmann_json)

add_library(psvr_diag_core INTERFACE)
target_include_directories(psvr_diag_core INTERFACE tools/psvr_diag)
target_link_libraries(psvr_diag_core INTERFACE nlohmann_json::nlohmann_json)
add_executable(psvr_diag tools/psvr_diag/main.cpp tools/psvr_diag/pnp_windows.cpp tools/psvr_diag/control_windows.cpp tools/psvr_diag/imu_capture.cpp)
target_link_libraries(psvr_diag PRIVATE psvr_diag_core hidapi::winapi setupapi cfgmgr32 hid winusb ole32 advapi32)
target_compile_definitions(psvr_diag PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
if(MSVC)
    target_compile_options(psvr_diag PRIVATE /W4 /permissive- /utf-8)
else()
    target_compile_options(psvr_diag PRIVATE -Wall -Wextra -Wpedantic)
    target_link_options(psvr_diag PRIVATE -static -static-libgcc -static-libstdc++)
endif()

add_library(psvr_display_inventory STATIC
    tools/psvr_display/display_inventory.cpp
    tools/psvr_display/display_inventory_windows.cpp)
target_include_directories(psvr_display_inventory PUBLIC tools/psvr_display)
target_link_libraries(psvr_display_inventory PUBLIC nlohmann_json::nlohmann_json user32)
target_compile_definitions(psvr_display_inventory PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
add_executable(psvr_display tools/psvr_display/main.cpp)
target_link_libraries(psvr_display PRIVATE psvr_display_inventory)
if(MSVC)
    target_compile_options(psvr_display_inventory PRIVATE /W4 /permissive- /utf-8)
    target_compile_options(psvr_display PRIVATE /W4 /permissive- /utf-8)
else()
    target_compile_options(psvr_display_inventory PRIVATE -Wall -Wextra -Wpedantic)
    target_compile_options(psvr_display PRIVATE -Wall -Wextra -Wpedantic)
    target_link_options(psvr_display PRIVATE -static -static-libgcc -static-libstdc++)
endif()

include(CTest)
if(BUILD_TESTING)
    add_executable(psvr_diag_tests tests/diag_tests.cpp)
    target_link_libraries(psvr_diag_tests PRIVATE psvr_diag_core)
    if(MINGW)
        target_link_options(psvr_diag_tests PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    add_test(NAME diag_core COMMAND psvr_diag_tests)
    add_executable(psvr_diag_consumer_tests tests/diag_consumer_tests.cpp)
    target_link_libraries(psvr_diag_consumer_tests PRIVATE psvr_diag_core)
    target_include_directories(psvr_diag_consumer_tests PRIVATE "${hidapi_SOURCE_DIR}/hidapi")
    target_compile_definitions(psvr_diag_consumer_tests PRIVATE
        WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE HID_API_NO_EXPORT_DEFINE)
    if(MINGW)
        target_link_options(psvr_diag_consumer_tests PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    add_test(NAME diag_consumer_offline COMMAND psvr_diag_consumer_tests
        "${CMAKE_CURRENT_BINARY_DIR}/test-logs/diag-consumer")
    add_executable(psvr_diag_windows_backend_tests tests/diag_windows_backend_tests.cpp
        tests/pnp_windows_test_backend.cpp tests/control_windows_test_backend.cpp)
    target_link_libraries(psvr_diag_windows_backend_tests PRIVATE psvr_diag_core ole32)
    add_executable(psvr_device_session_tests tests/device_session_tests.cpp)
    target_link_libraries(psvr_device_session_tests PRIVATE psvr_diag_core)
    foreach(target psvr_diag_windows_backend_tests psvr_device_session_tests)
        target_compile_definitions(${target} PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
        if(MINGW)
            target_link_options(${target} PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
    endforeach()
    add_test(NAME diag_windows_backends_offline COMMAND psvr_diag_windows_backend_tests)
    add_test(NAME device_session_offline COMMAND psvr_device_session_tests)
    add_executable(psvr_suite_config_fixture tests/suite_config_fixture.cpp)
    target_link_libraries(psvr_suite_config_fixture PRIVATE psvr_diag_core)
    if(MINGW)
        target_link_options(psvr_suite_config_fixture PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    add_executable(psvr_imu_tests tests/imu_tests.cpp)
    target_link_libraries(psvr_imu_tests PRIVATE psvr_diag_core)
    if(MINGW)
        target_link_options(psvr_imu_tests PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    add_test(NAME imu_math_fusion COMMAND psvr_imu_tests)
    add_executable(psvr_imu_capture_tests tests/imu_capture_tests.cpp tools/psvr_diag/imu_capture.cpp)
    target_link_libraries(psvr_imu_capture_tests PRIVATE psvr_diag_core)
    target_include_directories(psvr_imu_capture_tests PRIVATE "${hidapi_SOURCE_DIR}/hidapi")
    target_compile_definitions(psvr_imu_capture_tests PRIVATE
        WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE HID_API_NO_EXPORT_DEFINE)
    if(MINGW)
        target_link_options(psvr_imu_capture_tests PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    add_test(NAME imu_capture_offline COMMAND psvr_imu_capture_tests
        "${CMAKE_CURRENT_BINARY_DIR}/test-logs/imu-capture")
    add_test(NAME diag_help COMMAND psvr_diag --help)
    set_tests_properties(diag_help PROPERTIES PASS_REGULAR_EXPRESSION "Passive PSVR1")
    foreach(case unknown_option missing_config invalid_config conflicting_actions enumerate_action dry_run_without_action imu_control_conflict imu_short_duration imu_bad_axis imu_empty_axis)
        add_test(NAME diag_${case} COMMAND ${CMAKE_COMMAND}
            -DDIAG=$<TARGET_FILE:psvr_diag> -DCASE=${case}
            -DFIXTURE=${CMAKE_CURRENT_SOURCE_DIR}/tests/invalid_config.json
            -P ${CMAKE_CURRENT_SOURCE_DIR}/tests/check_exit.cmake)
    endforeach()
    add_test(NAME diag_power_dry_run COMMAND psvr_diag --power-on --dry-run --log-dir ${CMAKE_CURRENT_BINARY_DIR}/test-logs)
    set_tests_properties(diag_power_dry_run PROPERTIES PASS_REGULAR_EXPRESSION "packet=17 00 aa 04 01 00 00 00; hardware not opened")
    find_package(Python3 3.11 COMPONENTS Interpreter QUIET)
    if(Python3_FOUND)
        add_test(NAME suite_config_native_interop COMMAND ${Python3_EXECUTABLE} -B
            "${CMAKE_CURRENT_SOURCE_DIR}/tests/check_suite_config.py"
            "$<TARGET_FILE:psvr_suite_config_fixture>")
    endif()
    if(Python3_FOUND AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_monitor.py")
        add_test(NAME monitor_python COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s ${CMAKE_CURRENT_SOURCE_DIR}/tests -p test_monitor.py -v)
        set_tests_properties(monitor_python PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR})
    endif()
    add_executable(psvr_display_inventory_tests tests/display_inventory_tests.cpp)
    target_link_libraries(psvr_display_inventory_tests PRIVATE psvr_display_inventory)
    if(MINGW)
        target_link_options(psvr_display_inventory_tests PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    add_test(NAME display_inventory_synthetic COMMAND psvr_display_inventory_tests)
    add_test(NAME display_cli_no_hardware COMMAND ${CMAKE_COMMAND}
        -DDISPLAY=$<TARGET_FILE:psvr_display> -P ${CMAKE_SOURCE_DIR}/tests/check_display_cli.cmake)
    add_executable(psvr_distortion_tests tests/distortion_tests.cpp)
    target_include_directories(psvr_distortion_tests PRIVATE driver)
    if(MINGW)
        target_link_options(psvr_distortion_tests PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    add_test(NAME distortion_math_synthetic COMMAND psvr_distortion_tests)
    add_executable(psvr_optical_profile_tests tests/optical_profile_tests.cpp)
    target_include_directories(psvr_optical_profile_tests PRIVATE driver)
    target_link_libraries(psvr_optical_profile_tests PRIVATE nlohmann_json::nlohmann_json)
    if(MINGW)
        target_link_options(psvr_optical_profile_tests PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    add_test(NAME optical_profile_loader COMMAND psvr_optical_profile_tests
        "${CMAKE_SOURCE_DIR}/driver/package/resources/profiles/identity_unverified.json")
    if(Python3_FOUND)
        foreach(release_check license_collection release_preparation)
            add_test(NAME ${release_check}_python COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
                -s "${CMAKE_SOURCE_DIR}/tests" -p "test_${release_check}.py" -v)
            set_tests_properties(${release_check}_python PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
        endforeach()
        add_test(NAME presentation_capture_summary COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s "${CMAKE_SOURCE_DIR}/tests" -p test_presentation_summary.py -v)
        add_test(NAME hmd_edid_minimal_patch COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s "${CMAKE_SOURCE_DIR}/tests" -p test_hmd_edid.py -v)
        add_test(NAME native_transition_summary COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s "${CMAKE_SOURCE_DIR}/tests" -p test_native_transition_summary.py -v)
        add_test(NAME hmd_edid_restore_plan COMMAND powershell -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/test_hmd_edid_plan.ps1" -PythonExe "${Python3_EXECUTABLE}")
        add_test(NAME native_transition_log_reader COMMAND powershell -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/test_direct_mode_log_reader.ps1")
        add_test(NAME display_core_readonly_probe COMMAND powershell -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/test_display_core_probe.ps1")
        add_test(NAME direct_readiness_classifier COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s "${CMAKE_SOURCE_DIR}/tests" -p test_direct_readiness.py -v)
        add_test(NAME local_direct_test_procedure COMMAND powershell -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/test_local_direct_test.ps1" -PythonExe "${Python3_EXECUTABLE}")
        add_test(NAME display_core_stale_unknown COMMAND powershell -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/test_display_core_stale_unknown.ps1")
        add_test(NAME native_direct_selective_restore COMMAND powershell -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/test_native_direct_restore.ps1")
        add_test(NAME capture_refresh_baseline_snapshot COMMAND powershell -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/test_capture_refresh_baseline_snapshot.ps1")
        add_test(NAME set_trial_refresh_merge COMMAND powershell -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/test_set_trial_refresh_merge.ps1")
        add_test(NAME edid_verified_120_proposal COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s "${CMAKE_SOURCE_DIR}/tests" -p test_edid_120_proposal.py -v)
        add_test(NAME edid_offline_trial_matrix COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s "${CMAKE_SOURCE_DIR}/tests" -p test_edid_trial_matrix.py -v)
        add_test(NAME refresh90_trial_preparation COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s "${CMAKE_SOURCE_DIR}/tests" -p test_refresh90_trial.py -v)
        add_test(NAME patterns_python COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s "${CMAKE_SOURCE_DIR}/tests" -p test_patterns.py -v)
        set_tests_properties(patterns_python PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
        add_test(NAME reference_edid_python COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s "${CMAKE_SOURCE_DIR}/tests" -p test_reference_edid.py -v)
        set_tests_properties(reference_edid_python PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
        execute_process(COMMAND ${Python3_EXECUTABLE} -c "import numpy"
            RESULT_VARIABLE PSVR_CAMERA_NUMPY_RESULT OUTPUT_QUIET ERROR_QUIET)
        if(PSVR_CAMERA_NUMPY_RESULT EQUAL 0)
            add_test(NAME recording_brightness_detector COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
                -s "${CMAKE_SOURCE_DIR}/tests" -p test_recording_brightness.py -v)
            add_test(NAME camera_python COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
                -s "${CMAKE_SOURCE_DIR}/tests" -p "test_camera*.py" -v)
            set_tests_properties(camera_python PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
            add_test(NAME suite_python COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
                -s "${CMAKE_SOURCE_DIR}/tests" -p "test_suite*.py" -v)
            set_tests_properties(suite_python PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
            execute_process(COMMAND ${Python3_EXECUTABLE} -c "import pytest"
                RESULT_VARIABLE PSVR_CAMERA_PYTEST_RESULT OUTPUT_QUIET ERROR_QUIET)
            if(PSVR_CAMERA_PYTEST_RESULT EQUAL 0)
                add_test(NAME camera_kalman_pytest COMMAND ${Python3_EXECUTABLE} -B -m pytest
                    "${CMAKE_SOURCE_DIR}/tests/test_camera_pose_estimator.py"
                    "${CMAKE_SOURCE_DIR}/tests/test_camera_pose_uncertainty.py"
                    "${CMAKE_SOURCE_DIR}/tests/test_camera_position_pipeline.py"
                    "${CMAKE_SOURCE_DIR}/tests/test_camera_position_replay.py" -q)
                set_property(TEST camera_kalman_pytest APPEND PROPERTY
                    LABELS "kalman;offline")
                add_test(NAME camera_kalman_optimization COMMAND ${Python3_EXECUTABLE} -B -m pytest
                    "${CMAKE_SOURCE_DIR}/tests/test_camera_kalman_adaptive.py"
                    "${CMAKE_SOURCE_DIR}/tests/test_camera_kalman_recovery_fit.py"
                    "${CMAKE_SOURCE_DIR}/tests/test_camera_kalman_onset.py"
                    "${CMAKE_SOURCE_DIR}/tests/test_camera_kalman_benchmark.py"
                    "${CMAKE_SOURCE_DIR}/tests/test_camera_kalman_budget.py" -q)
                set_tests_properties(camera_kalman_optimization PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
                set_tests_properties(camera_kalman_pytest PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
            else()
                message(STATUS "SKIP: camera_kalman_pytest/camera_kalman_optimization not registered (pytest assente; see tools/psvr_camera/requirements-test.txt)")
            endif()
        else()
            message(STATUS "SKIP: recording_brightness/camera_python/suite_python/kalman tests not registered (numpy assente per il Python selezionato; see BUILD.md)")
        endif()
        add_test(NAME app_readiness_python COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
            -s "${CMAKE_SOURCE_DIR}/tests" -p test_app_readiness.py -v)
        set_tests_properties(app_readiness_python PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
    else()
        message(STATUS "SKIP: python release/readiness/pattern tests not registered (Python3 assente; see BUILD.md)")
    endif()
endif()

add_executable(psvr_optics tools/psvr_optics/main.cpp)
target_include_directories(psvr_optics PRIVATE driver)
target_link_libraries(psvr_optics PRIVATE nlohmann_json::nlohmann_json)
if(MSVC)
    target_compile_options(psvr_optics PRIVATE /W4 /permissive- /utf-8)
else()
    target_compile_options(psvr_optics PRIVATE -Wall -Wextra -Wpedantic)
    target_link_options(psvr_optics PRIVATE -municode -static -static-libgcc -static-libstdc++)
endif()
if(BUILD_TESTING AND Python3_FOUND)
    add_test(NAME optics_cli_offline COMMAND ${Python3_EXECUTABLE} -B
        "${CMAKE_SOURCE_DIR}/tests/check_optics_cli.py" "$<TARGET_FILE:psvr_optics>"
        "${CMAKE_SOURCE_DIR}/driver/package/resources/profiles/identity_unverified.json")
endif()
add_custom_target(psvr_optics_package ALL
    COMMAND ${CMAKE_COMMAND} -E make_directory "${CMAKE_SOURCE_DIR}/out/optics/licenses"
    COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:psvr_optics>" "${CMAKE_SOURCE_DIR}/out/optics/psvr_optics.exe"
    COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/tools/psvr_optics/README.md" "${CMAKE_SOURCE_DIR}/out/optics/README.md"
    COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/driver/package/resources/profiles/identity_unverified.json" "${CMAKE_SOURCE_DIR}/out/optics/identity_unverified.json"
    COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/LICENSE" "${CMAKE_SOURCE_DIR}/out/optics/licenses/LICENSE"
    COMMAND ${CMAKE_COMMAND} -E copy "${nlohmann_json_SOURCE_DIR}/LICENSE.MIT" "${CMAKE_SOURCE_DIR}/out/optics/licenses/json-MIT.txt"
    DEPENDS psvr_optics)
add_executable(psvr_position_probe tools/psvr_position_probe/main.cpp)
target_include_directories(psvr_position_probe PRIVATE driver)
target_link_libraries(psvr_position_probe PRIVATE psvr_diag_core ws2_32)
if(MSVC)
    target_compile_options(psvr_position_probe PRIVATE /W4 /permissive- /utf-8)
else()
    target_compile_options(psvr_position_probe PRIVATE -Wall -Wextra -Wpedantic)
    target_link_options(psvr_position_probe PRIVATE -municode -static -static-libgcc -static-libstdc++)
endif()
if(BUILD_TESTING AND Python3_FOUND)
    add_test(NAME position_probe_interop COMMAND ${Python3_EXECUTABLE} -B
        "${CMAKE_SOURCE_DIR}/tests/check_position_probe.py" "$<TARGET_FILE:psvr_position_probe>")
endif()
add_custom_target(psvr_position_probe_package ALL
    COMMAND ${CMAKE_COMMAND} -E make_directory "${CMAKE_SOURCE_DIR}/out/position_probe/licenses"
    COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:psvr_position_probe>" "${CMAKE_SOURCE_DIR}/out/position_probe/psvr_position_probe.exe"
    COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/tools/psvr_position_probe/README.md" "${CMAKE_SOURCE_DIR}/out/position_probe/README.md"
    COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/LICENSE" "${CMAKE_SOURCE_DIR}/out/position_probe/licenses/LICENSE"
    COMMAND ${CMAKE_COMMAND} -E copy "${nlohmann_json_SOURCE_DIR}/LICENSE.MIT" "${CMAKE_SOURCE_DIR}/out/position_probe/licenses/json-MIT.txt"
    DEPENDS psvr_position_probe)
if(NOT Python3_FOUND)
    find_package(Python3 3.11 COMPONENTS Interpreter QUIET)
endif()
if(Python3_FOUND)
    add_custom_target(psvr_patterns_package ALL
        COMMAND ${Python3_EXECUTABLE} -B "${CMAKE_SOURCE_DIR}/tools/psvr_patterns/generate_patterns.py"
            --output-dir "${CMAKE_BINARY_DIR}/diagnostic-patterns" --force
        COMMAND ${CMAKE_COMMAND} -E make_directory "${CMAKE_SOURCE_DIR}/out/patterns"
        COMMAND ${CMAKE_COMMAND} -E copy_directory "${CMAKE_BINARY_DIR}/diagnostic-patterns" "${CMAKE_SOURCE_DIR}/out/patterns"
        COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/tools/psvr_patterns/README.md" "${CMAKE_SOURCE_DIR}/out/patterns/README.md")
endif()

add_custom_target(psvr_display_package ALL
    COMMAND ${CMAKE_COMMAND} -E make_directory "${CMAKE_SOURCE_DIR}/out/display/licenses"
    COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:psvr_display>" "${CMAKE_SOURCE_DIR}/out/display/psvr_display.exe"
    COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/tools/psvr_display/README.md" "${CMAKE_SOURCE_DIR}/out/display/README.md"
    COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/LICENSE" "${CMAKE_SOURCE_DIR}/out/display/licenses/LICENSE"
    COMMAND ${CMAKE_COMMAND} -E copy "${nlohmann_json_SOURCE_DIR}/LICENSE.MIT" "${CMAKE_SOURCE_DIR}/out/display/licenses/json-MIT.txt"
    DEPENDS psvr_display)

install(TARGETS psvr_diag psvr_display psvr_optics RUNTIME DESTINATION .)
install(FILES config.json DESTINATION .)
install(FILES LICENSE third_party/BSL-1.0.txt third_party/README.md
    DESTINATION licenses)
install(FILES ${nlohmann_json_SOURCE_DIR}/LICENSE.MIT DESTINATION licenses RENAME json-MIT.txt)
install(FILES ${hidapi_SOURCE_DIR}/LICENSE-bsd.txt DESTINATION licenses RENAME hidapi-BSD.txt)
install(FILES ${hidapi_SOURCE_DIR}/LICENSE.txt DESTINATION licenses RENAME hidapi-license-choice.txt)

option(PSVR_BUILD_DRIVER "Build the OpenVR HMD driver (display opt-in)" ON)
if(PSVR_BUILD_DRIVER)
    FetchContent_Declare(openvr_sdk
        GIT_REPOSITORY https://github.com/ValveSoftware/openvr.git
        GIT_TAG 0924064316de3effbcd1acf1e309182a2deb1c05
        SOURCE_SUBDIR psvrpc_headers_only)
    # Headers only: no OpenVR client import library or SDK sample executables.
    FetchContent_MakeAvailable(openvr_sdk)
    add_library(psvr_openvr INTERFACE)
    target_include_directories(psvr_openvr SYSTEM INTERFACE "${openvr_sdk_SOURCE_DIR}/headers")
    add_executable(psvr_runtime_probe tools/psvr_runtime_probe/main.cpp)
    if(MSVC)
        add_executable(psvr_runtime_telemetry tools/psvr_runtime_probe/telemetry.cpp)
        target_link_libraries(psvr_runtime_telemetry PRIVATE psvr_openvr nlohmann_json::nlohmann_json kernel32 user32)
        target_compile_options(psvr_runtime_telemetry PRIVATE /W4 /permissive- /utf-8)
    endif()
    target_link_libraries(psvr_runtime_probe PRIVATE psvr_openvr nlohmann_json::nlohmann_json kernel32)
    if(MSVC)
        target_compile_options(psvr_runtime_probe PRIVATE /W4 /permissive- /utf-8)
    else()
        target_compile_options(psvr_runtime_probe PRIVATE -Wall -Wextra -Wpedantic)
        target_link_options(psvr_runtime_probe PRIVATE -municode -static -static-libgcc -static-libstdc++)
    endif()
    add_custom_target(psvr_runtime_probe_package ALL
        COMMAND ${CMAKE_COMMAND} -E make_directory "${CMAKE_SOURCE_DIR}/out/runtime_probe/licenses"
        COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:psvr_runtime_probe>" "${CMAKE_SOURCE_DIR}/out/runtime_probe/psvr_runtime_probe.exe"
        COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/tools/psvr_runtime_probe/README.md" "${CMAKE_SOURCE_DIR}/out/runtime_probe/README.md"
        COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/LICENSE" "${CMAKE_SOURCE_DIR}/out/runtime_probe/licenses/LICENSE"
        COMMAND ${CMAKE_COMMAND} -E copy "${openvr_sdk_SOURCE_DIR}/LICENSE" "${CMAKE_SOURCE_DIR}/out/runtime_probe/licenses/openvr-LICENSE.txt"
        COMMAND ${CMAKE_COMMAND} -E copy "${nlohmann_json_SOURCE_DIR}/LICENSE.MIT" "${CMAKE_SOURCE_DIR}/out/runtime_probe/licenses/json-MIT.txt"
        DEPENDS psvr_runtime_probe)
    if(MSVC)
        add_dependencies(psvr_runtime_probe_package psvr_runtime_telemetry)
        add_custom_command(TARGET psvr_runtime_probe_package POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:psvr_runtime_telemetry>" "${CMAKE_SOURCE_DIR}/out/runtime_probe/psvr_runtime_telemetry.exe")
        install(TARGETS psvr_runtime_telemetry RUNTIME DESTINATION .)
    endif()
    install(TARGETS psvr_runtime_probe RUNTIME DESTINATION .)
    if(MSVC)
        add_library(driver_psvrpc SHARED driver/driver_psvrpc.cpp driver/presenter_d3d11.cpp)
    else()
        add_library(driver_psvrpc SHARED driver/driver_psvrpc.cpp)
    endif()
    target_link_libraries(driver_psvrpc PRIVATE psvr_diag_core psvr_openvr hidapi::winapi psvr_display_inventory ws2_32 ole32 setupapi)
    if(MSVC)
        target_link_libraries(driver_psvrpc PRIVATE d3d11 dxgi)
    endif()
    target_compile_definitions(driver_psvrpc PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    set_target_properties(driver_psvrpc PROPERTIES PREFIX "")
    if(MSVC)
        target_compile_options(driver_psvrpc PRIVATE /W4 /permissive- /utf-8)
    else()
        target_compile_options(driver_psvrpc PRIVATE -Wall -Wextra -Wpedantic)
        target_link_options(driver_psvrpc PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    set(PSVR_DRIVER_PACKAGE "${CMAKE_SOURCE_DIR}/out/driver_psvrpc/psvrpc" CACHE PATH "Driver staging root")
    # Always stage resources, even when DLL did not relink or package was deleted.
    add_custom_target(psvr_driver_package ALL
        COMMAND ${CMAKE_COMMAND} -E make_directory "${PSVR_DRIVER_PACKAGE}/bin/win64" "${PSVR_DRIVER_PACKAGE}/licenses"
        COMMAND ${CMAKE_COMMAND} -E copy_directory "${CMAKE_SOURCE_DIR}/driver/package" "${PSVR_DRIVER_PACKAGE}"
        COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:driver_psvrpc>" "${PSVR_DRIVER_PACKAGE}/bin/win64/driver_psvrpc.dll"
        COMMAND ${CMAKE_COMMAND} -E copy "${openvr_sdk_SOURCE_DIR}/LICENSE" "${PSVR_DRIVER_PACKAGE}/licenses/openvr-LICENSE.txt"
        COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_SOURCE_DIR}/LICENSE" "${CMAKE_SOURCE_DIR}/third_party/BSL-1.0.txt" "${hidapi_SOURCE_DIR}/LICENSE-bsd.txt" "${PSVR_DRIVER_PACKAGE}/licenses"
        COMMAND ${CMAKE_COMMAND} -E copy "${nlohmann_json_SOURCE_DIR}/LICENSE.MIT" "${PSVR_DRIVER_PACKAGE}/licenses/json-MIT.txt"
        DEPENDS driver_psvrpc)
    if(Python3_FOUND)
        add_dependencies(psvr_driver_package psvr_patterns_package)
        add_custom_command(TARGET psvr_driver_package POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E make_directory "${PSVR_DRIVER_PACKAGE}/resources/diagnostics"
            COMMAND ${CMAKE_COMMAND} -E copy_directory "${CMAKE_BINARY_DIR}/diagnostic-patterns" "${PSVR_DRIVER_PACKAGE}/resources/diagnostics")
    endif()
    if(BUILD_TESTING)
        add_executable(psvr_imu_velocity_tests tests/imu_velocity_tests.cpp)
        target_include_directories(psvr_imu_velocity_tests PRIVATE driver)
        target_link_libraries(psvr_imu_velocity_tests PRIVATE psvr_diag_core psvr_openvr)
        if(MINGW)
            target_link_options(psvr_imu_velocity_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME imu_velocity_geometry COMMAND psvr_imu_velocity_tests)
        add_executable(psvr_headset_buttons_tests tests/headset_buttons_tests.cpp)
        target_include_directories(psvr_headset_buttons_tests PRIVATE driver)
        target_compile_definitions(psvr_headset_buttons_tests PRIVATE
            WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
        target_link_libraries(psvr_headset_buttons_tests PRIVATE ole32 setupapi)
        if(MINGW)
            target_link_options(psvr_headset_buttons_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME headset_buttons_offline COMMAND psvr_headset_buttons_tests)
        add_executable(psvr_pose_transition_tests tests/pose_transition_tests.cpp)
        target_include_directories(psvr_pose_transition_tests PRIVATE driver)
        target_link_libraries(psvr_pose_transition_tests PRIVATE psvr_diag_core psvr_openvr)
        if(MINGW)
            target_link_options(psvr_pose_transition_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME pose_validity_transitions COMMAND psvr_pose_transition_tests)
        add_executable(psvr_presenter_backend_contract_tests tests/presenter_backend_contract_tests.cpp)
        target_include_directories(psvr_presenter_backend_contract_tests PRIVATE driver)
        if(MINGW)
            target_link_options(psvr_presenter_backend_contract_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME presenter_backend_contract COMMAND psvr_presenter_backend_contract_tests)
        if(MSVC AND Python3_FOUND)
            add_test(NAME continuous_telemetry_cli_offline COMMAND ${Python3_EXECUTABLE} -B
                "${CMAKE_SOURCE_DIR}/tests/check_telemetry_cli.py" "$<TARGET_FILE:psvr_runtime_telemetry>")
        endif()
        add_executable(psvr_virtual_display_contract_tests tests/virtual_display_contract_tests.cpp)
        target_include_directories(psvr_virtual_display_contract_tests PRIVATE driver)
        target_link_libraries(psvr_virtual_display_contract_tests PRIVATE psvr_openvr)
        if(MINGW)
            target_link_options(psvr_virtual_display_contract_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME virtual_display_002_contract COMMAND psvr_virtual_display_contract_tests)
        add_executable(psvr_virtual_display_lifecycle_tests tests/virtual_display_lifecycle_tests.cpp)
        target_include_directories(psvr_virtual_display_lifecycle_tests PRIVATE driver)
        target_link_libraries(psvr_virtual_display_lifecycle_tests PRIVATE psvr_openvr)
        if(MINGW)
            target_link_options(psvr_virtual_display_lifecycle_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME virtual_display_002_lifecycle COMMAND psvr_virtual_display_lifecycle_tests)
        if(WIN32 AND CMAKE_SIZEOF_VOID_P EQUAL 8)
            # Offline test DLL and independent raw-slot caller, never linked to the driver.
            add_library(psvr_virtual_display_boundary_fixture SHARED tests/virtual_display_boundary_fixture.cpp)
            target_include_directories(psvr_virtual_display_boundary_fixture PRIVATE driver)
            target_link_libraries(psvr_virtual_display_boundary_fixture PRIVATE psvr_openvr)
            add_executable(psvr_virtual_display_boundary_tests tests/virtual_display_boundary_tests.cpp)
            target_link_libraries(psvr_virtual_display_boundary_tests PRIVATE kernel32)
            foreach(boundary_target psvr_virtual_display_boundary_fixture psvr_virtual_display_boundary_tests)
                set_property(TARGET ${boundary_target} PROPERTY INTERPROCEDURAL_OPTIMIZATION FALSE)
                if(MSVC)
                    target_compile_options(${boundary_target} PRIVATE /W4 /permissive- /utf-8)
                elseif(MINGW)
                    target_compile_options(${boundary_target} PRIVATE -Wall -Wextra -Wpedantic)
                    target_link_options(${boundary_target} PRIVATE -static -static-libgcc -static-libstdc++)
                endif()
            endforeach()
            add_dependencies(psvr_virtual_display_boundary_tests psvr_virtual_display_boundary_fixture)
            add_test(NAME virtual_display_002_boundary COMMAND psvr_virtual_display_boundary_tests
                "$<TARGET_FILE:psvr_virtual_display_boundary_fixture>")
        endif()
        add_executable(psvr_driver_tests tests/driver_tests.cpp)
        target_include_directories(psvr_driver_tests PRIVATE driver)
        target_link_libraries(psvr_driver_tests PRIVATE psvr_diag_core psvr_openvr)
        if(MINGW)
            target_link_options(psvr_driver_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME driver_pose_contract COMMAND psvr_driver_tests "$<TARGET_FILE:driver_psvrpc>")
        add_executable(psvr_position_tests tests/position_tests.cpp)
        target_include_directories(psvr_position_tests PRIVATE driver)
        target_link_libraries(psvr_position_tests PRIVATE psvr_diag_core psvr_openvr nlohmann_json::nlohmann_json ws2_32)
        if(MINGW)
            target_link_options(psvr_position_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME position_udp_contract COMMAND psvr_position_tests)
        add_executable(psvr_position_continuity_tests tests/position_continuity_tests.cpp)
        target_include_directories(psvr_position_continuity_tests PRIVATE driver)
        target_link_libraries(psvr_position_continuity_tests PRIVATE psvr_diag_core psvr_openvr)
        if(MINGW)
            target_link_options(psvr_position_continuity_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME position_continuity_contract COMMAND psvr_position_continuity_tests)
        add_executable(psvr_imu_settings_tests tests/imu_settings_tests.cpp)
        target_include_directories(psvr_imu_settings_tests PRIVATE driver)
        target_link_libraries(psvr_imu_settings_tests PRIVATE psvr_diag_core psvr_openvr)
        if(MINGW)
            target_link_options(psvr_imu_settings_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME imu_timing_settings_contract COMMAND psvr_imu_settings_tests)
        add_executable(psvr_driver_imu_export_tests tests/driver_imu_export_tests.cpp)
        target_include_directories(psvr_driver_imu_export_tests PRIVATE driver)
        target_link_libraries(psvr_driver_imu_export_tests PRIVATE
            psvr_diag_core psvr_openvr nlohmann_json::nlohmann_json ws2_32)
        add_executable(psvr_driver_camera_interop_fixture tests/driver_camera_interop_fixture.cpp)
        target_include_directories(psvr_driver_camera_interop_fixture PRIVATE driver)
        target_link_libraries(psvr_driver_camera_interop_fixture PRIVATE
            psvr_diag_core psvr_openvr nlohmann_json::nlohmann_json ws2_32)
        foreach(camera_bridge_target psvr_driver_imu_export_tests psvr_driver_camera_interop_fixture)
            if(MSVC)
                target_compile_options(${camera_bridge_target} PRIVATE /W4 /permissive- /utf-8)
            elseif(MINGW)
                target_compile_options(${camera_bridge_target} PRIVATE -Wall -Wextra -Wpedantic)
                target_link_options(${camera_bridge_target} PRIVATE -static -static-libgcc -static-libstdc++)
            endif()
        endforeach()
        add_test(NAME driver_imu_export_contract COMMAND psvr_driver_imu_export_tests)
        if(Python3_FOUND)
            add_test(NAME driver_camera_interop COMMAND ${Python3_EXECUTABLE} -B
                "${CMAKE_SOURCE_DIR}/tests/check_driver_camera_interop.py"
                "$<TARGET_FILE:psvr_driver_camera_interop_fixture>")
            set_tests_properties(driver_camera_interop PROPERTIES TIMEOUT 20)
        endif()
        if(Python3_FOUND)
            add_test(NAME driver_factory_package COMMAND ${Python3_EXECUTABLE} -B "${CMAKE_SOURCE_DIR}/tests/check_driver_package.py" "${PSVR_DRIVER_PACKAGE}")
        endif()
        add_test(NAME driver_scripts COMMAND powershell -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/test_driver_scripts.ps1")
        add_test(NAME projection_trial_scripts COMMAND powershell -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/test_projection_trial.ps1")
        add_executable(psvr_display_component_tests tests/display_component_tests.cpp)
        target_include_directories(psvr_display_component_tests PRIVATE driver)
        target_link_libraries(psvr_display_component_tests PRIVATE psvr_openvr)
        add_executable(psvr_display_selection_tests tests/display_selection_tests.cpp)
        target_include_directories(psvr_display_selection_tests PRIVATE driver)
        target_link_libraries(psvr_display_selection_tests PRIVATE psvr_openvr psvr_display_inventory)
        if(MINGW)
            target_link_options(psvr_display_component_tests PRIVATE -static -static-libgcc -static-libstdc++)
            target_link_options(psvr_display_selection_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME display_component_contract COMMAND psvr_display_component_tests)
        add_executable(psvr_display_windows_abi_tests tests/display_windows_abi_tests.cpp)
        target_include_directories(psvr_display_windows_abi_tests PRIVATE driver)
        target_link_libraries(psvr_display_windows_abi_tests PRIVATE psvr_openvr)
        if(MINGW)
            target_link_options(psvr_display_windows_abi_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME display_windows_abi_contract COMMAND psvr_display_windows_abi_tests)
        add_test(NAME display_selection_integration COMMAND psvr_display_selection_tests)
        add_executable(psvr_driver_runtime_mock_tests tests/driver_runtime_mock_tests.cpp)
        target_link_libraries(psvr_driver_runtime_mock_tests PRIVATE psvr_openvr kernel32)
        add_dependencies(psvr_driver_runtime_mock_tests driver_psvrpc)
        if(MINGW)
            target_link_options(psvr_driver_runtime_mock_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME driver_runtime_mock COMMAND psvr_driver_runtime_mock_tests "$<TARGET_FILE:driver_psvrpc>")
        add_executable(psvr_optics_component_tests tests/optics_component_tests.cpp)
        target_include_directories(psvr_optics_component_tests PRIVATE driver)
        target_link_libraries(psvr_optics_component_tests PRIVATE psvr_openvr nlohmann_json::nlohmann_json)
        if(MINGW)
            target_link_options(psvr_optics_component_tests PRIVATE -static -static-libgcc -static-libstdc++)
        endif()
        add_test(NAME optics_component_integration COMMAND psvr_optics_component_tests
            "${CMAKE_SOURCE_DIR}/driver/package/resources/profiles/identity_unverified.json")
        if(Python3_FOUND)
            add_test(NAME runtime_probe_cli_offline COMMAND ${Python3_EXECUTABLE} -B
                "${CMAKE_SOURCE_DIR}/tests/check_runtime_probe_cli.py" "$<TARGET_FILE:psvr_runtime_probe>")
        endif()
    endif()
endif()
