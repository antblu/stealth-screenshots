#pragma once

#include "core/session_controller.hpp"

#include <windows.h>

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>

namespace stealth::win {

class WgcCaptureSource final : public core::ICaptureSource {
public:
    WgcCaptureSource();
    ~WgcCaptureSource() override;

    WgcCaptureSource(const WgcCaptureSource&) = delete;
    WgcCaptureSource& operator=(const WgcCaptureSource&) = delete;

    // Starts capturing whichever top-level window currently has focus.
    bool start_focused_capture(HWND owner_hwnd);
    void stop_capture();

    core::CapturedFrame capture_frame(core::ImageFormat format,
                                      std::uint32_t max_dimension,
                                      std::size_t max_bytes) override;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace stealth::win
