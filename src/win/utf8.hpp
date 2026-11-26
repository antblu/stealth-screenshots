#pragma once

#include <string>
#include <string_view>

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace stealth::win {

inline std::wstring utf8_to_wide(std::string_view input) {
    if (input.empty()) {
        return {};
    }
    const int needed = MultiByteToWideChar(
        CP_UTF8, 0, input.data(), static_cast<int>(input.size()), nullptr, 0);
    if (needed <= 0) {
        return {};
    }
    std::wstring out(static_cast<std::size_t>(needed), L'\0');
    MultiByteToWideChar(
        CP_UTF8, 0, input.data(), static_cast<int>(input.size()), out.data(), needed);
    return out;
}

inline std::string wide_to_utf8(std::wstring_view input) {
    if (input.empty()) {
        return {};
    }
    const int needed = WideCharToMultiByte(
        CP_UTF8, 0, input.data(), static_cast<int>(input.size()), nullptr, 0, nullptr, nullptr);
    if (needed <= 0) {
        return {};
    }
    std::string out(static_cast<std::size_t>(needed), '\0');
    WideCharToMultiByte(
        CP_UTF8, 0, input.data(), static_cast<int>(input.size()), out.data(), needed, nullptr, nullptr);
    return out;
}

}  // namespace stealth::win
