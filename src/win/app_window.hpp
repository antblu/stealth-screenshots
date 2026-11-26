#pragma once

#include "core/logger.hpp"
#include "core/session_controller.hpp"
#include "win/settings_store.hpp"
#include "win/wgc_capture.hpp"

#include <windows.h>

#include <memory>
#include <mutex>
#include <string>

namespace stealth::win {

class AppWindow {
public:
    explicit AppWindow(HINSTANCE instance);
    ~AppWindow();

    AppWindow(const AppWindow&) = delete;
    AppWindow& operator=(const AppWindow&) = delete;

    bool create();
    int run_message_loop();

private:
    static LRESULT CALLBACK wnd_proc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam);
    LRESULT handle_message(UINT msg, WPARAM wparam, LPARAM lparam);

    void create_controls();
    void on_start();
    void on_pause_resume();
    void on_stop(const std::string& reason = "Stopped");
    void on_settings();
    void update_ui_from_snapshot(const core::StatusSnapshot& snap);
    core::ApiContractConfig build_contract() const;

    HINSTANCE instance_ = nullptr;
    HWND hwnd_ = nullptr;
    HWND status_label_ = nullptr;
    HWND stats_label_ = nullptr;
    HWND hotkey_label_ = nullptr;
    HWND btn_start_ = nullptr;
    HWND btn_pause_ = nullptr;
    HWND btn_stop_ = nullptr;
    HWND btn_settings_ = nullptr;
    HFONT ui_font_ = nullptr;

    AppSettings settings_;
    std::string api_credential_;

    std::shared_ptr<core::Logger> logger_;
    std::shared_ptr<WgcCaptureSource> capture_source_;
    std::shared_ptr<core::IApiClient> api_client_;
    std::unique_ptr<core::SessionController> controller_;

    std::mutex status_mutex_;
    core::StatusSnapshot latest_snapshot_;
};

}  // namespace stealth::win
