#pragma once

#include "win/settings_store.hpp"

#include <windows.h>

#include <string>

namespace stealth::win {

bool show_settings_dialog(HWND parent,
                          HINSTANCE instance,
                          AppSettings& settings,
                          std::string& api_credential);

}  // namespace stealth::win
