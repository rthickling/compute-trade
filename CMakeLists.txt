cmake_minimum_required(VERSION 3.28)
project(ComputeTrade VERSION 0.1.0 LANGUAGES CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "This initial Simplx/TCP implementation targets Linux")
endif()

# Match the Simplx fork's C++26 baseline, including CMake 3.28's missing flag mappings.
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_CXX_SCAN_FOR_MODULES OFF)
include(CheckCXXCompilerFlag)
check_cxx_compiler_flag("-std=c++26" COMPUTE_TRADE_HAS_CXX26)
if(COMPUTE_TRADE_HAS_CXX26)
    set(CMAKE_CXX26_STANDARD_COMPILE_OPTION "-std=c++26")
    set(CMAKE_CXX26_EXTENSION_COMPILE_OPTION "-std=gnu++26")
else()
    check_cxx_compiler_flag("-std=c++2c" COMPUTE_TRADE_HAS_CXX2C)
    if(NOT COMPUTE_TRADE_HAS_CXX2C)
        message(FATAL_ERROR "Select a C++26-capable compiler, e.g. CXX=g++-16")
    endif()
    set(CMAKE_CXX26_STANDARD_COMPILE_OPTION "-std=c++2c")
    set(CMAKE_CXX26_EXTENSION_COMPILE_OPTION "-std=gnu++2c")
endif()
set(CMAKE_CXX_STANDARD 26)
list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES
    CMAKE_CXX26_STANDARD_COMPILE_OPTION CMAKE_CXX26_EXTENSION_COMPILE_OPTION)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

include(CTest)
set(SIMPLX_SOURCE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../simplx" CACHE PATH
    "Path to the rthickling/simplx checkout (tested at c0b2ca712f1c187b6c120d5bcaf441bdff1600ae)")
if(NOT EXISTS "${SIMPLX_SOURCE_DIR}/include/simplx.h" OR NOT EXISTS "${SIMPLX_SOURCE_DIR}/CMakeLists.txt")
    message(FATAL_ERROR "Simplx checkout missing. Clone github.com/rthickling/simplx beside this project, or set SIMPLX_SOURCE_DIR.")
endif()
# Build the dependency's engine without its tutorials, tests, or Google Test submodule.
set(compute_trade_testing ${BUILD_TESTING})
set(BUILD_TESTING OFF)
set(SIMPLX_BUILD_TUTORIALS OFF CACHE BOOL "Build Simplx tutorials" FORCE)
add_subdirectory("${SIMPLX_SOURCE_DIR}" "${CMAKE_BINARY_DIR}/simplx" EXCLUDE_FROM_ALL)
set(BUILD_TESTING ${compute_trade_testing})

add_library(compute_trade_headers INTERFACE)
target_include_directories(compute_trade_headers INTERFACE "${CMAKE_CURRENT_SOURCE_DIR}/include")
target_compile_options(compute_trade_headers INTERFACE -Wall -Wextra -Wpedantic -Werror)

add_library(compute_trade_exchange STATIC src/exchange.cpp)
target_link_libraries(compute_trade_exchange PUBLIC compute_trade_headers PRIVATE engine)

add_executable(ct_exchange src/main.cpp)
target_link_libraries(ct_exchange PRIVATE compute_trade_exchange)

add_executable(ct_injector src/injector.cpp)
target_link_libraries(ct_injector PRIVATE compute_trade_headers)

if(BUILD_TESTING)
    foreach(test IN ITEMS sbe order_book)
        add_executable(${test}_test tests/${test}_test.cpp)
        target_link_libraries(${test}_test PRIVATE compute_trade_headers)
        add_test(NAME ${test} COMMAND ${test}_test)
    endforeach()
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    add_test(NAME exchange_integration COMMAND "${Python3_EXECUTABLE}"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/integration_test.py"
        "$<TARGET_FILE:ct_exchange>" "$<TARGET_FILE:ct_injector>")
    set_tests_properties(exchange_integration PROPERTIES TIMEOUT 60)
endif()
