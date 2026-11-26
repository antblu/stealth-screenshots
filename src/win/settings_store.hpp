#pragma once

#include <string>

namespace stealth::win {

struct AppSettings {
    std::string endpoint_url;
    std::string model = "default";
    std::string system_prompt = "Describe the user's current screen concisely.";
    bool allow_http_localhost = false;
};

class SettingsStore {
public:
    static AppSettings load();
    static bool save(const AppSettings& settings);
};

}  // namespace stealth::win
