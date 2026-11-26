#include "win/app_window.hpp"

#include "win/credential_store.hpp"
#include "win/settings_dialog.hpp"
#include "win/utf8.hpp"
#include "win/winhttp_client.hpp"

#include <wtsapi32.h>

namespace stealth::win {
namespace {

constexpr wchar_t kWindowClassName[] = L"StealthScreenshotsControlWindow";
constexpr UINT WM_APP_STATUS_UPDATE = WM_APP + 1;

constexpr int IDC_BTN_START = 2001;
constexpr int IDC_BTN_PAUSE = 2002;
constexpr int IDC_BTN_STOP = 2003;
constexpr int IDC_BTN_SETTINGS = 2004;

constexpr int HOTKEY_TOGGLE_WINDOW = 1;  // Ctrl+Alt+O
constexpr int HOTKEY_PAUSE_RESUME = 2;   // Ctrl+Alt+P
constexpr int HOTKEY_STOP_SESSION = 3;   // Ctrl+Alt+S

}  // namespace

AppWindow::AppWindow(HINSTANCE instance) : instance_(instance) {
    settings_ = SettingsStore::load();
    api_credential_ = CredentialStore::load_credential().value_or("");

    logger_ = std::make_shared<core::Logger>();
    capture_source_ = std::make_shared<WgcCaptureSource>();
    api_client_ = std::make_shared<WinHttpApiClient>();
    controller_ = std::make_unique<core::SessionController>(
        capture_source_, api_client_, logger_);

    controller_->set_status_callback([this](const core::StatusSnapshot& snap) {
        {
            std::lock_guard lock(status_mutex_);
            latest_snapshot_ = snap;
        }
        if (hwnd_) {
            PostMessageW(hwnd_, WM_APP_STATUS_UPDATE, 0, 0);
        }
    });
}

AppWindow::~AppWindow() {
    if (controller_) {
        controller_->stop();
    }
    if (capture_source_) {
        capture_source_->stop_capture();
    }
    if (ui_font_) {
        DeleteObject(ui_font_);
    }
}

bool AppWindow::create() {
    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = AppWindow::wnd_proc;
    wc.hInstance = instance_;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
    wc.lpszClassName = kWindowClassName;

    if (!RegisterClassExW(&wc)) {
        return false;
    }

    hwnd_ = CreateWindowExW(
        0,
        kWindowClassName,
        L"Stealth Screenshots",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        CW_USEDEFAULT,
        CW_USEDEFAULT,
        420,
        210,
        nullptr,
        nullptr,
        instance_,
        this);

    if (!hwnd_) {
        return false;
    }

    ShowWindow(hwnd_, SW_SHOW);
    UpdateWindow(hwnd_);
    return true;
}

int AppWindow::run_message_loop() {
    MSG msg{};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    return static_cast<int>(msg.wParam);
}

LRESULT CALLBACK AppWindow::wnd_proc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
    AppWindow* self = nullptr;
    if (msg == WM_NCCREATE) {
        auto* cs = reinterpret_cast<CREATESTRUCTW*>(lparam);
        self = reinterpret_cast<AppWindow*>(cs->lpCreateParams);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
        self->hwnd_ = hwnd;
    } else {
        self = reinterpret_cast<AppWindow*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    }

    if (self) {
        return self->handle_message(msg, wparam, lparam);
    }
    return DefWindowProcW(hwnd, msg, wparam, lparam);
}

LRESULT AppWindow::handle_message(UINT msg, WPARAM wparam, LPARAM lparam) {
    switch (msg) {
        case WM_CREATE:
            create_controls();
            WTSRegisterSessionNotification(hwnd_, NOTIFY_FOR_THIS_SESSION);
            RegisterHotKey(hwnd_, HOTKEY_TOGGLE_WINDOW, MOD_CONTROL | MOD_ALT, 'O');
            RegisterHotKey(hwnd_, HOTKEY_PAUSE_RESUME, MOD_CONTROL | MOD_ALT, 'P');
            RegisterHotKey(hwnd_, HOTKEY_STOP_SESSION, MOD_CONTROL | MOD_ALT, 'S');
            update_ui_from_snapshot(controller_->snapshot());
            return 0;

        case WM_COMMAND:
            switch (LOWORD(wparam)) {
                case IDC_BTN_START:
                    on_start();
                    return 0;
                case IDC_BTN_PAUSE:
                    on_pause_resume();
                    return 0;
                case IDC_BTN_STOP:
                    on_stop("Stopped by user");
                    return 0;
                case IDC_BTN_SETTINGS:
                    on_settings();
                    return 0;
            }
            break;

        case WM_HOTKEY:
            if (wparam == HOTKEY_TOGGLE_WINDOW) {
                if (IsWindowVisible(hwnd_)) {
                    ShowWindow(hwnd_, SW_HIDE);
                } else {
                    ShowWindow(hwnd_, SW_SHOW);
                    SetForegroundWindow(hwnd_);
                }
                return 0;
            }
            if (wparam == HOTKEY_PAUSE_RESUME) {
                on_pause_resume();
                return 0;
            }
            if (wparam == HOTKEY_STOP_SESSION) {
                on_stop("Stopped via hotkey");
                ShowWindow(hwnd_, SW_SHOW);
                SetForegroundWindow(hwnd_);
                return 0;
            }
            break;

        case WM_WTSSESSION_CHANGE:
            if (wparam == WTS_SESSION_LOCK) {
                on_stop("Stopped: Windows session locked");
                ShowWindow(hwnd_, SW_SHOW);
            }
            return 0;

        case WM_APP_STATUS_UPDATE: {
            core::StatusSnapshot snap;
            {
                std::lock_guard lock(status_mutex_);
                snap = latest_snapshot_;
            }
            if (snap.state == core::SessionState::Stopped) {
                capture_source_->stop_capture();
                if (!IsWindowVisible(hwnd_)) {
                    ShowWindow(hwnd_, SW_SHOW);
                }
            }
            update_ui_from_snapshot(snap);
            return 0;
        }

        case WM_CLOSE: {
            const auto snap = controller_->snapshot();
            if (snap.state == core::SessionState::Capturing ||
                snap.state == core::SessionState::Uploading ||
                snap.state == core::SessionState::BackingOff ||
                snap.state == core::SessionState::Paused) {
                // Keep running in background without a foreground window.
                ShowWindow(hwnd_, SW_HIDE);
                return 0;
            }
            DestroyWindow(hwnd_);
            return 0;
        }

        case WM_DESTROY:
            UnregisterHotKey(hwnd_, HOTKEY_TOGGLE_WINDOW);
            UnregisterHotKey(hwnd_, HOTKEY_PAUSE_RESUME);
            UnregisterHotKey(hwnd_, HOTKEY_STOP_SESSION);
            WTSUnRegisterSessionNotification(hwnd_);
            controller_->stop();
            capture_source_->stop_capture();
            PostQuitMessage(0);
            return 0;
    }

    return DefWindowProcW(hwnd_, msg, wparam, lparam);
}

void AppWindow::create_controls() {
    ui_font_ = CreateFontW(
        -14, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
        CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_SWISS, L"Segoe UI");

    auto make_ctrl = [&](const wchar_t* cls, const wchar_t* text, DWORD style,
                         int x, int y, int w, int h, int id) -> HWND {
        HWND hctrl = CreateWindowExW(
            0, cls, text, WS_CHILD | WS_VISIBLE | style,
            x, y, w, h, hwnd_, reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),
            instance_, nullptr);
        if (hctrl && ui_font_) {
            SendMessageW(hctrl, WM_SETFONT, reinterpret_cast<WPARAM>(ui_font_), TRUE);
        }
        return hctrl;
    };

    status_label_ = make_ctrl(L"STATIC", L"Status: Idle", 0, 16, 16, 370, 22, 0);
    stats_label_ = make_ctrl(L"STATIC", L"Captured: 0  |  Uploaded: 0  |  Skipped: 0",
                             0, 16, 42, 370, 22, 0);

    btn_start_ = make_ctrl(L"BUTTON", L"Start...", BS_DEFPUSHBUTTON, 16, 78, 86, 30, IDC_BTN_START);
    btn_pause_ = make_ctrl(L"BUTTON", L"Pause", 0, 110, 78, 86, 30, IDC_BTN_PAUSE);
    btn_stop_ = make_ctrl(L"BUTTON", L"Stop", 0, 204, 78, 86, 30, IDC_BTN_STOP);
    btn_settings_ = make_ctrl(L"BUTTON", L"Settings...", 0, 298, 78, 88, 30, IDC_BTN_SETTINGS);

    hotkey_label_ = make_ctrl(
        L"STATIC",
        L"Hotkeys: Ctrl+Alt+O (Show/Hide)  |  Ctrl+Alt+P (Pause)  |  Ctrl+Alt+S (Stop)",
        0, 16, 126, 380, 32, 0);
}

core::ApiContractConfig AppWindow::build_contract() const {
    core::ApiContractConfig cfg;
    cfg.endpoint_url = settings_.endpoint_url;
    cfg.model = settings_.model;
    cfg.allow_http_localhost = settings_.allow_http_localhost;
    return cfg;
}

void AppWindow::on_start() {
    if (settings_.endpoint_url.empty()) {
        on_settings();
        if (settings_.endpoint_url.empty()) {
            return;
        }
    }

    if (!capture_source_->pick_and_start(hwnd_)) {
        return;
    }

    controller_->start(build_contract(), api_credential_, settings_.system_prompt);
    update_ui_from_snapshot(controller_->snapshot());

    // Run without a foreground window once the session starts.
    ShowWindow(hwnd_, SW_HIDE);
}

void AppWindow::on_pause_resume() {
    const auto snap = controller_->snapshot();
    if (snap.state == core::SessionState::Paused) {
        controller_->resume();
    } else if (snap.state == core::SessionState::Capturing ||
               snap.state == core::SessionState::Uploading ||
               snap.state == core::SessionState::BackingOff) {
        controller_->pause();
    }
    update_ui_from_snapshot(controller_->snapshot());
}

void AppWindow::on_stop(const std::string& reason) {
    controller_->stop();
    capture_source_->stop_capture();
    auto snap = controller_->snapshot();
    snap.detail = reason;
    update_ui_from_snapshot(snap);
}

void AppWindow::on_settings() {
    if (show_settings_dialog(hwnd_, instance_, settings_, api_credential_)) {
        SettingsStore::save(settings_);
        CredentialStore::save_credential(api_credential_);
        controller_->update_settings(
            build_contract(), api_credential_, settings_.system_prompt);
    }
}

void AppWindow::update_ui_from_snapshot(const core::StatusSnapshot& snap) {
    const std::wstring status_text =
        L"Status: " + utf8_to_wide(core::to_string(snap.state)) +
        L" - " + utf8_to_wide(snap.detail);
    SetWindowTextW(status_label_, status_text.c_str());

    const std::string stats_utf8 =
        "Captured: " + std::to_string(snap.stats.frames_captured) +
        "  |  Uploaded: " + std::to_string(snap.stats.uploads_succeeded) +
        "  |  Skipped: " + std::to_string(snap.stats.ticks_skipped);
    SetWindowTextW(stats_label_, utf8_to_wide(stats_utf8).c_str());

    const bool active =
        snap.state == core::SessionState::Capturing ||
        snap.state == core::SessionState::Uploading ||
        snap.state == core::SessionState::BackingOff ||
        snap.state == core::SessionState::Paused;

    EnableWindow(btn_start_, !active);
    EnableWindow(btn_pause_, active);
    EnableWindow(btn_stop_, active);
    SetWindowTextW(btn_pause_, snap.state == core::SessionState::Paused ? L"Resume" : L"Pause");
}

}  // namespace stealth::win
