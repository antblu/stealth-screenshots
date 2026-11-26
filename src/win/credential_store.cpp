#include "win/credential_store.hpp"

#include <windows.h>
#include <wincred.h>

#include <cstring>

namespace stealth::win {

bool CredentialStore::save_credential(std::string_view api_credential,
                                      std::wstring_view target_name) {
    std::wstring target(target_name);
    if (api_credential.empty()) {
        return delete_credential(target_name);
    }

    CREDENTIALW cred{};
    cred.Type = CRED_TYPE_GENERIC;
    cred.TargetName = target.data();
    cred.CredentialBlobSize = static_cast<DWORD>(api_credential.size());
    cred.CredentialBlob = reinterpret_cast<LPBYTE>(const_cast<char*>(api_credential.data()));
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE;
    cred.UserName = const_cast<LPWSTR>(L"api_key");

    return CredWriteW(&cred, 0) == TRUE;
}

std::optional<std::string> CredentialStore::load_credential(std::wstring_view target_name) {
    std::wstring target(target_name);
    PCREDENTIALW raw_cred = nullptr;
    if (!CredReadW(target.c_str(), CRED_TYPE_GENERIC, 0, &raw_cred) || !raw_cred) {
        return std::nullopt;
    }

    std::string result;
    if (raw_cred->CredentialBlob && raw_cred->CredentialBlobSize > 0) {
        result.assign(reinterpret_cast<const char*>(raw_cred->CredentialBlob),
                      raw_cred->CredentialBlobSize);
    }
    CredFree(raw_cred);
    return result;
}

bool CredentialStore::delete_credential(std::wstring_view target_name) {
    std::wstring target(target_name);
    if (CredDeleteW(target.c_str(), CRED_TYPE_GENERIC, 0) == TRUE) {
        return true;
    }
    return GetLastError() == ERROR_NOT_FOUND;
}

}  // namespace stealth::win
