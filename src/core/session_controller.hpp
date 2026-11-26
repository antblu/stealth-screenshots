#pragma once

#include "core/api_contract.hpp"
#include "core/logger.hpp"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <stop_token>
#include <string>
#include <thread>
#include <vector>

namespace stealth::core {

enum class CaptureStatus {
    Ok,
    NoNewFrame,
    TargetClosed,
    Error,
};

struct CapturedFrame {
    CaptureStatus status = CaptureStatus::Error;
    std::vector<std::uint8_t> image_bytes;
    std::string mime_type = "image/png";
    std::uint32_t width = 0;
    std::uint32_t height = 0;
};

class ICaptureSource {
public:
    virtual ~ICaptureSource() = default;
    virtual CapturedFrame capture_frame(ImageFormat format,
                                        std::uint32_t max_dimension,
                                        std::size_t max_bytes) = 0;
};

enum class SessionState {
    Idle,
    Capturing,
    Uploading,
    Paused,
    BackingOff,
    Stopped,
};

const char* to_string(SessionState state) noexcept;

struct SessionOptions {
    std::chrono::milliseconds capture_interval{10000};
    std::chrono::milliseconds base_backoff{2000};
    std::chrono::milliseconds max_backoff{60000};
    std::chrono::milliseconds no_frame_retry_delay{250};
};

struct SessionStats {
    std::uint64_t frames_captured = 0;
    std::uint64_t uploads_succeeded = 0;
    std::uint64_t uploads_failed = 0;
    std::uint64_t ticks_skipped = 0;
    std::uint64_t consecutive_failures = 0;
    int last_http_status = 0;
    ErrorCategory last_error = ErrorCategory::None;
};

struct StatusSnapshot {
    SessionState state = SessionState::Idle;
    SessionStats stats;
    std::string detail;
};

std::chrono::milliseconds compute_next_tick_delay(
    std::chrono::steady_clock::time_point scheduled_tick,
    std::chrono::steady_clock::time_point now,
    std::chrono::milliseconds interval,
    std::uint64_t& skipped_ticks_out);

std::chrono::milliseconds compute_backoff_delay(
    std::uint64_t consecutive_failures,
    std::chrono::milliseconds base_backoff,
    std::chrono::milliseconds max_backoff,
    std::optional<std::uint32_t> retry_after_seconds);

class SessionController {
public:
    using StatusCallback = std::function<void(const StatusSnapshot&)>;

    SessionController(std::shared_ptr<ICaptureSource> capture_source,
                      std::shared_ptr<IApiClient> api_client,
                      std::shared_ptr<Logger> logger,
                      SessionOptions options = {});
    ~SessionController();

    SessionController(const SessionController&) = delete;
    SessionController& operator=(const SessionController&) = delete;

    bool start(ApiContractConfig contract,
               std::string api_credential,
               std::string system_prompt);
    void pause();
    void resume();
    void stop();

    void update_settings(ApiContractConfig contract,
                         std::string api_credential,
                         std::string system_prompt);

    void set_status_callback(StatusCallback cb);
    StatusSnapshot snapshot() const;

private:
    void run_loop(std::stop_token worker_stop);
    void set_state(SessionState state, std::string detail);
    bool wait_interruptibly(std::chrono::steady_clock::time_point until,
                            std::stop_token worker_stop);

    std::shared_ptr<ICaptureSource> capture_source_;
    std::shared_ptr<IApiClient> api_client_;
    std::shared_ptr<Logger> logger_;
    SessionOptions options_;

    mutable std::mutex mutex_;
    std::condition_variable_any cv_;
    ApiContractConfig contract_;
    std::string api_credential_;
    std::string system_prompt_;
    SessionState state_ = SessionState::Idle;
    SessionStats stats_;
    std::string detail_ = "Idle";
    bool paused_ = false;
    bool stop_requested_ = false;
    StatusCallback status_callback_;

    std::stop_source upload_stop_source_;
    std::jthread worker_;
};

}  // namespace stealth::core
