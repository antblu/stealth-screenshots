#include "win/app_window.hpp"

#include <commctrl.h>
#include <winrt/base.h>

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int) {
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
