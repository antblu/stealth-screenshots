cmake_minimum_required(VERSION 3.22)
project(stealth_screenshots VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(stealth_core STATIC
    src/core/api_contract.cpp
    src/core/base64.cpp
    src/core/logger.cpp
    src/core/session_controller.cpp
    src/core/url_utils.cpp
)
target_include_directories(stealth_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)

if(MSVC)
    target_compile_options(stealth_core PRIVATE /W4 /WX /permissive- /utf-8 /EHsc)
else()
    target_compile_options(stealth_core PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

if(WIN32)
    add_executable(stealth_screenshots WIN32
        src/win/app_window.cpp
        src/win/credential_store.cpp
        src/win/main.cpp
        src/win/settings_dialog.cpp
        src/win/settings_store.cpp
        src/win/wgc_capture.cpp
        src/win/winhttp_client.cpp
        resources/app.rc
    )
    target_include_directories(stealth_screenshots PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/src
        ${CMAKE_CURRENT_SOURCE_DIR}/resources
    )
    target_compile_definitions(stealth_screenshots PRIVATE
        WIN32_LEAN_AND_MEAN
        NOMINMAX
        UNICODE
        _UNICODE
        _WIN32_WINNT=0x0A00
    )
    if(MSVC)
        target_compile_options(stealth_screenshots PRIVATE /W4 /WX /permissive- /utf-8 /EHsc /await:strict)
        target_link_options(stealth_screenshots PRIVATE /MANIFEST:NO)
    endif()
    target_link_libraries(stealth_screenshots PRIVATE
        stealth_core
        windowsapp
        winhttp
        advapi32
        wtsapi32
        d3d11
        dxgi
        windowscodecs
        user32
        shell32
        ole32
        comctl32
    )

    install(TARGETS stealth_screenshots RUNTIME DESTINATION .)
    install(FILES README.md LICENSE docs/API_CONTRACT.md docs/SETUP_AND_VERIFICATION.md DESTINATION .)
    set(CPACK_GENERATOR "ZIP")
    set(CPACK_PACKAGE_NAME "stealth-screenshots-win64")
    include(CPack)
endif()

option(BUILD_TESTING "Build unit tests" ON)
if(BUILD_TESTING)
    enable_testing()
    add_executable(stealth_core_tests
        tests/test_main.cpp
        tests/test_core.cpp
    )
    target_link_libraries(stealth_core_tests PRIVATE stealth_core)
    if(MSVC)
        target_compile_options(stealth_core_tests PRIVATE /W4 /WX /permissive- /utf-8 /EHsc)
    else()
        target_compile_options(stealth_core_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
    endif()
    add_test(NAME stealth_core_tests COMMAND stealth_core_tests)
endif()
