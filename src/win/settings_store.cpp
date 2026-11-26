#include "win/settings_store.hpp"

#include "win/utf8.hpp"

#include <windows.h>
#include <shlobj.h>

#include <filesystem>
#include <fstream>
#include <sstream>

namespace stealth::win {
namespace {

std::filesystem::path get_settings_path() {
    PWSTR app_data = nullptr;
    std::filesystem::path base;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_LocalAppData, 0, nullptr, &app_data)) && app_data) {
        base = app_data;
        CoTaskMemFree(app_data);
    } else {
        base = std::filesystem::current_path();
    }
    return base / "StealthScreenshots" / "settings.cfg";
}

std::string escape_newlines(std::string_view s) {
    std::string out;
    out.reserve(s.size());
    for (char c : s) {
        if (c == '\\') {
            out += "\\\\";
        } else if (c == '\n') {
            out += "\\n";
        } else if (c == '\r') {
            out += "\\r";
        } else {
            out.push_back(c);
        }
    }
    return out;
}

std::string unescape_newlines(std::string_view s) {
    std::string out;
    out.reserve(s.size());
    for (std::size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '\\' && i + 1 < s.size()) {
            const char next = s[i + 1];
            if (next == 'n') {
                out.push_back('\n');
                ++i;
                continue;
            }
            if (next == 'r') {
                out.push_back('\r');
                ++i;
                continue;
            }
            if (next == '\\') {
                out.push_back('\\');
                ++i;
                continue;
            }
        }
        out.push_back(s[i]);
    }
    return out;
}

}  // namespace

AppSettings SettingsStore::load() {
    AppSettings settings;
    const auto path = get_settings_path();
    std::ifstream in(path, std::ios::binary);
    if (!in.is_open()) {
        return settings;
    }

    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        const auto eq = line.find('=');
        if (eq == std::string::npos) {
            continue;
        }
        const std::string key = line.substr(0, eq);
        const std::string val = unescape_newlines(line.substr(eq + 1));
        if (key == "endpoint_url") {
            settings.endpoint_url = val;
        } else if (key == "model") {
            if (!val.empty()) {
                settings.model = val;
            }
        } else if (key == "system_prompt") {
            settings.system_prompt = val;
        } else if (key == "allow_http_localhost") {
            settings.allow_http_localhost = (val == "1" || val == "true");
        }
    }
    return settings;
}

bool SettingsStore::save(const AppSettings& settings) {
    const auto path = get_settings_path();
    std::error_code ec;
    std::filesystem::create_directories(path.parent_path(), ec);

    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (!out.is_open()) {
        return false;
    }

    out << "endpoint_url=" << escape_newlines(settings.endpoint_url) << "\n";
    out << "model=" << escape_newlines(settings.model) << "\n";
    out << "system_prompt=" << escape_newlines(settings.system_prompt) << "\n";
    out << "allow_http_localhost=" << (settings.allow_http_localhost ? "1" : "0") << "\n";
    return out.good();
}

}  // namespace stealth::win
