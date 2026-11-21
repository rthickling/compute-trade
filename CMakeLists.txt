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
option(COMPUTE_TRADE_ACCEPTANCE_LIVE "Register live exchange acceptance tests" OFF)
option(COMPUTE_TRADE_SANITIZERS "Enable AddressSanitizer and UndefinedBehaviorSanitizer" OFF)
if(COMPUTE_TRADE_SANITIZERS)
    add_compile_options(-fsanitize=address,undefined -fno-sanitize-recover=all
        -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address,undefined -fno-sanitize-recover=all)
endif()
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

find_package(PkgConfig REQUIRED)
pkg_check_modules(JSON_C REQUIRED IMPORTED_TARGET json-c)

add_library(compute_trade_exchange STATIC src/exchange.cpp src/app.cpp src/workers.cpp)
target_link_libraries(compute_trade_exchange PUBLIC compute_trade_headers PRIVATE engine PkgConfig::JSON_C)

add_executable(ct_exchange src/main.cpp)
target_link_libraries(ct_exchange PRIVATE compute_trade_exchange)

add_executable(ct_event_page_probe tools/event_page_positive_control.cpp)
target_link_libraries(ct_event_page_probe PRIVATE compute_trade_headers engine)

option(COMPUTE_TRADE_EVENT_PAGE_PROBE "Capture Simplx Event backing allocations" OFF)
if(COMPUTE_TRADE_EVENT_PAGE_PROBE)
    if(NOT CMAKE_CXX_COMPILER_ID STREQUAL "GNU" OR NOT CMAKE_BUILD_TYPE STREQUAL "RelWithDebInfo"
        OR COMPUTE_TRADE_SANITIZERS)
        message(FATAL_ERROR "The Event page probe requires GCC RelWithDebInfo without sanitizers")
    endif()
    foreach(target IN ITEMS ct_exchange ct_event_page_probe)
        target_sources(${target} PRIVATE tools/event_page_wrap.cpp)
        target_link_options(${target} PRIVATE -no-pie -Wl,--wrap=malloc)
    endforeach()
endif()

add_executable(ct_injector src/injector.cpp)
target_link_libraries(ct_injector PRIVATE compute_trade_headers PkgConfig::JSON_C)

add_executable(ct_matcher_bench src/matcher_bench.cpp)
target_link_libraries(ct_matcher_bench PRIVATE compute_trade_headers engine)

if(BUILD_TESTING)
    foreach(test IN ITEMS sbe order_book matcher)
        add_executable(${test}_test tests/${test}_test.cpp)
        target_link_libraries(${test}_test PRIVATE compute_trade_headers)
        add_test(NAME ${test} COMMAND ${test}_test)
    endforeach()
    add_executable(simplx_alignment_test tests/simplx_alignment_test.cpp)
    target_link_libraries(simplx_alignment_test PRIVATE compute_trade_headers engine)
    add_test(NAME simplx_alignment COMMAND simplx_alignment_test)
    set_tests_properties(simplx_alignment PROPERTIES TIMEOUT 15)
    find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
    add_test(NAME exchange_integration COMMAND "${Python3_EXECUTABLE}"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/integration_test.py"
        "$<TARGET_FILE:ct_exchange>" "$<TARGET_FILE:ct_injector>")
    set_tests_properties(exchange_integration PROPERTIES TIMEOUT 60)
    add_test(NAME acceptance_framework COMMAND "${Python3_EXECUTABLE}"
        -m unittest discover -s acceptance -t . -v)
    set_tests_properties(acceptance_framework PROPERTIES
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/tests"
        LABELS framework TIMEOUT 30)

    if(COMPUTE_TRADE_ACCEPTANCE_LIVE)
        foreach(group IN ITEMS book flow transport)
            add_test(NAME acceptance_${group} COMMAND "${Python3_EXECUTABLE}"
                -m acceptance.runner --group "${group}" --exchange "$<TARGET_FILE:ct_exchange>"
                --injector "$<TARGET_FILE:ct_injector>")
            set_tests_properties(acceptance_${group} PROPERTIES
                WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/tests"
                LABELS acceptance RUN_SERIAL TRUE TIMEOUT 120)
        endforeach()
    endif()
endif()
