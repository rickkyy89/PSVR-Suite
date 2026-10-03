cmake_minimum_required(VERSION 3.24)
project(psvr_pc VERSION 0.2.0 LANGUAGES C CXX)

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
add_executable(psvr_diag tools/psvr_diag/main.cpp tools/psvr_diag/pnp_windows.cpp tools/psvr_diag/control_windows.cpp)
target_link_libraries(psvr_diag PRIVATE psvr_diag_core hidapi::winapi setupapi cfgmgr32 hid winusb ole32 advapi32)
target_compile_definitions(psvr_diag PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
if(MSVC)
    target_compile_options(psvr_diag PRIVATE /W4 /permissive- /utf-8)
else()
    target_compile_options(psvr_diag PRIVATE -Wall -Wextra -Wpedantic)
    target_link_options(psvr_diag PRIVATE -static -static-libgcc -static-libstdc++)
endif()

include(CTest)
if(BUILD_TESTING)
    add_executable(psvr_diag_tests tests/diag_tests.cpp)
    target_link_libraries(psvr_diag_tests PRIVATE psvr_diag_core)
    if(MINGW)
        target_link_options(psvr_diag_tests PRIVATE -static -static-libgcc -static-libstdc++)
    endif()
    add_test(NAME diag_core COMMAND psvr_diag_tests)
    add_test(NAME diag_help COMMAND psvr_diag --help)
    set_tests_properties(diag_help PROPERTIES PASS_REGULAR_EXPRESSION "Passive PSVR1")
    foreach(case unknown_option missing_config invalid_config conflicting_actions enumerate_action dry_run_without_action)
        add_test(NAME diag_${case} COMMAND ${CMAKE_COMMAND}
            -DDIAG=$<TARGET_FILE:psvr_diag> -DCASE=${case}
            -DFIXTURE=${CMAKE_CURRENT_SOURCE_DIR}/tests/invalid_config.json
            -P ${CMAKE_CURRENT_SOURCE_DIR}/tests/check_exit.cmake)
    endforeach()
    add_test(NAME diag_power_dry_run COMMAND psvr_diag --power-on --dry-run --log-dir ${CMAKE_CURRENT_BINARY_DIR}/test-logs)
    set_tests_properties(diag_power_dry_run PROPERTIES PASS_REGULAR_EXPRESSION "packet=17 00 aa 04 01 00 00 00; hardware not opened")
endif()

install(TARGETS psvr_diag RUNTIME DESTINATION .)
install(FILES config.json DESTINATION .)
install(FILES LICENSE third_party/BSL-1.0.txt third_party/README.md
    DESTINATION licenses)
install(FILES ${nlohmann_json_SOURCE_DIR}/LICENSE.MIT DESTINATION licenses RENAME json-MIT.txt)
install(FILES ${hidapi_SOURCE_DIR}/LICENSE-bsd.txt DESTINATION licenses RENAME hidapi-BSD.txt)
install(FILES ${hidapi_SOURCE_DIR}/LICENSE.txt DESTINATION licenses RENAME hidapi-license-choice.txt)
