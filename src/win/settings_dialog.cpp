#include "win/settings_dialog.hpp"

#include "core/url_utils.hpp"
#include "resource.h"
#include "win/utf8.hpp"

namespace stealth::win {
namespace {

struct DialogContext {
    AppSettings* settings = nullptr;
    std::string* credential = nullptr;
};

std::string get_dlg_item_utf8(HWND dlg, int id) {
    HWND ctrl = GetDlgItem(dlg, id);
    const int len = GetWindowTextLengthW(ctrl);
    if (len <= 0) {
        return {};
    }
    std::wstring buf(static_cast<std::size_t>(len) + 1, L'\0');
    const int copied = GetWindowTextW(ctrl, buf.data(), static_cast<int>(buf.size()));
    buf.resize(copied > 0 ? static_cast<std::size_t>(copied) : 0);
    return wide_to_utf8(buf);
}

INT_PTR CALLBACK settings_dlg_proc(HWND dlg, UINT msg, WPARAM wparam, LPARAM lparam) {
    auto* ctx = reinterpret_cast<DialogContext*>(GetWindowLongPtrW(dlg, GWLP_USERDATA));

    switch (msg) {
        case WM_INITDIALOG: {
            ctx = reinterpret_cast<DialogContext*>(lparam);
            SetWindowLongPtrW(dlg, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(ctx));

            SetDlgItemTextW(dlg, IDC_EDIT_ENDPOINT,
                            utf8_to_wide(ctx->settings->endpoint_url).c_str());
            SetDlgItemTextW(dlg, IDC_EDIT_MODEL,
                            utf8_to_wide(ctx->settings->model).c_str());
            SetDlgItemTextW(dlg, IDC_EDIT_CREDENTIAL,
                            utf8_to_wide(*ctx->credential).c_str());
            SetDlgItemTextW(dlg, IDC_EDIT_PROMPT,
                            utf8_to_wide(ctx->settings->system_prompt).c_str());
            CheckDlgButton(dlg, IDC_CHECK_ALLOW_LOCALHOST,
                           ctx->settings->allow_http_localhost ? BST_CHECKED : BST_UNCHECKED);
            return TRUE;
        }
        case WM_COMMAND:
            if (LOWORD(wparam) == IDOK && ctx) {
                const std::string endpoint = get_dlg_item_utf8(dlg, IDC_EDIT_ENDPOINT);
                const bool allow_localhost =
                    IsDlgButtonChecked(dlg, IDC_CHECK_ALLOW_LOCALHOST) == BST_CHECKED;
                if (!core::parse_endpoint_url(endpoint, allow_localhost)) {
                    MessageBoxW(
                        dlg,
                        L"Please enter a valid HTTPS endpoint URL (or http://localhost when enabled).",
                        L"Invalid Endpoint",
                        MB_OK | MB_ICONWARNING);
                    return TRUE;
                }

                ctx->settings->endpoint_url = endpoint;
                const std::string model = get_dlg_item_utf8(dlg, IDC_EDIT_MODEL);
                ctx->settings->model = model.empty() ? "default" : model;
                ctx->settings->system_prompt = get_dlg_item_utf8(dlg, IDC_EDIT_PROMPT);
                ctx->settings->allow_http_localhost = allow_localhost;
                *ctx->credential = get_dlg_item_utf8(dlg, IDC_EDIT_CREDENTIAL);

                EndDialog(dlg, IDOK);
                return TRUE;
            }
            if (LOWORD(wparam) == IDCANCEL) {
                EndDialog(dlg, IDCANCEL);
                return TRUE;
            }
            break;
    }
    return FALSE;
}

}  // namespace

bool show_settings_dialog(HWND parent,
                          HINSTANCE instance,
                          AppSettings& settings,
                          std::string& api_credential) {
    DialogContext ctx{&settings, &api_credential};
    const INT_PTR res = DialogBoxParamW(
        instance,
        MAKEINTRESOURCEW(IDD_SETTINGS),
        parent,
        settings_dlg_proc,
        reinterpret_cast<LPARAM>(&ctx));
    return res == IDOK;
}

}  // namespace stealth::win
