#pragma once

#include <optional>
#include <string>
#include <string_view>

namespace stealth::win {

class CredentialStore {
public:
    static constexpr wchar_t kDefaultTargetName[] = L"StealthScreenshots/ApiCredential";

    static bool save_credential(std::string_view api_credential,
                                std::wstring_view target_name = kDefaultTargetName);
    static std::optional<std::string> load_credential(
        std::wstring_view target_name = kDefaultTargetName);
    static bool delete_credential(std::wstring_view target_name = kDefaultTargetName);
};

}  // namespace stealth::win
