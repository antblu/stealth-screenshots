#include "win/app_window.hpp"

#include <commctrl.h>
#include <winrt/base.h>

namespace {

class SingleInstanceMutex {
public:
    explicit SingleInstanceMutex(const wchar_t* name)
        : handle_(CreateMutexW(nullptr, FALSE, name)),
          already_exists_(handle_ != nullptr && GetLastError() == ERROR_ALREADY_EXISTS) {}

    ~SingleInstanceMutex() {
        if (handle_) {
            CloseHandle(handle_);
        }
    }

    SingleInstanceMutex(const SingleInstanceMutex&) = delete;
    SingleInstanceMutex& operator=(const SingleInstanceMutex&) = delete;

    bool is_primary() const {
        return handle_ != nullptr && !already_exists_;
    }

    bool already_running() const {
        return already_exists_ || (handle_ == nullptr && GetLastError() == ERROR_ACCESS_DENIED);
    }

private:
    HANDLE handle_ = nullptr;
    bool already_exists_ = false;
};

}  // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int) {
    SingleInstanceMutex instance_guard(L"Local\\StealthScreenshotsSingleInstanceMutex");
    if (!instance_guard.is_primary()) {
        if (instance_guard.already_running()) {
            stealth::win::AppWindow::activate_existing_instance();
        }
        return 0;
    }

    winrt::init_apartment(winrt::apartment_type::single_threaded);

    INITCOMMONCONTROLSEX icc{};
    icc.dwSize = sizeof(icc);
    icc.dwICC = ICC_STANDARD_CLASSES;
    InitCommonControlsEx(&icc);

    stealth::win::AppWindow app(instance);
    if (!app.create()) {
        return 1;
    }

    return app.run_message_loop();
}
