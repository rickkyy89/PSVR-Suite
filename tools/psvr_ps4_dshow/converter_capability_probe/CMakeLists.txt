cmake_minimum_required(VERSION 3.20)
project(psvr_dshow_converter_capability_probe LANGUAGES CXX)
if(NOT WIN32 OR NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "This isolated probe requires existing Windows SDK/MSVC x64; installs nothing")
endif()
add_executable(converter_capability_probe main.cpp)
target_compile_features(converter_capability_probe PRIVATE cxx_std_17)
target_compile_definitions(converter_capability_probe PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
target_compile_options(converter_capability_probe PRIVATE /W4 /EHsc /permissive-)
target_link_libraries(converter_capability_probe PRIVATE ole32 strmiids)
set_property(TARGET converter_capability_probe PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
# Intentionally NOT add_test: successful discovery is not sample conversion,
# detector comparison, negotiated media types, or historical-path evidence.
