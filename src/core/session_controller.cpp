#include "core/session_controller.hpp"

#include <algorithm>

namespace stealth::core {

const char* to_string(SessionState state) noexcept {
    switch (state) {
        case SessionState::Idle: return "Idle";
        case SessionState::Capturing: return "Capturing";
        case SessionState::Uploading: return "Uploading";
        case SessionState::Paused: return "Paused";
        case SessionState::BackingOff: return "BackingOff";
        case SessionState::Stopped: return "Stopped";
    }
    return "Unknown";
}

std::chrono::milliseconds compute_next_tick_delay(
    std::chrono::steady_clock::time_point scheduled_tick,
    std::chrono::steady_clock::time_point now,
    std::chrono::milliseconds interval,
    std::uint64_t& skipped_ticks_out) {
    if (interval <= std::chrono::milliseconds::zero()) {
        return std::chrono::milliseconds::zero();
    }
    auto next_tick = scheduled_tick + interval;
    while (next_tick <= now) {
        ++skipped_ticks_out;
        next_tick += interval;
    }
    return std::chrono::duration_cast<std::chrono::milliseconds>(next_tick - now);
}

std::chrono::milliseconds compute_backoff_delay(
    std::uint64_t consecutive_failures,
    std::chrono::milliseconds base_backoff,
    std::chrono::milliseconds max_backoff,
    std::optional<std::uint32_t> retry_after_seconds) {
    const std::uint64_t shift = consecutive_failures > 0
                                    ? std::min<std::uint64_t>(consecutive_failures - 1, 10)
                                    : 0;
    auto exp_delay = base_backoff * (std::uint64_t{1} << shift);
    if (exp_delay > max_backoff) {
        exp_delay = max_backoff;
    }
    if (retry_after_seconds.has_value()) {
        const auto retry_ms = std::chrono::seconds(*retry_after_seconds);
        if (retry_ms > exp_delay) {
            exp_delay = std::min(std::chrono::duration_cast<std::chrono::milliseconds>(retry_ms),
                                 max_backoff);
        }
    }
    return exp_delay;
}

SessionController::SessionController(std::shared_ptr<ICaptureSource> capture_source,
                                     std::shared_ptr<IApiClient> api_client,
                                     std::shared_ptr<Logger> logger,
                                     SessionOptions options)
    : capture_source_(std::move(capture_source)),
      api_client_(std::move(api_client)),
      logger_(std::move(logger)),
      options_(options) {}

SessionController::~SessionController() {
    stop();
}

bool SessionController::start(ApiContractConfig contract,
                              std::string api_credential,
                              std::string system_prompt) {
    stop();

    {
        std::lock_guard lock(mutex_);
        contract_ = std::move(contract);
        api_credential_ = std::move(api_credential);
        system_prompt_ = std::move(system_prompt);
        stats_ = SessionStats{};
        paused_ = false;
        stop_requested_ = false;
        upload_stop_source_ = std::stop_source{};
        state_ = SessionState::Capturing;
        detail_ = "Session started";
    }

    if (logger_) {
        logger_->log("session_started");
    }

    worker_ = std::jthread([this](std::stop_token st) { run_loop(st); });
    return true;
}

void SessionController::pause() {
    StatusCallback cb;
    StatusSnapshot snap;
    {
        std::lock_guard lock(mutex_);
        if (state_ == SessionState::Idle || state_ == SessionState::Stopped || paused_) {
            return;
        }
        paused_ = true;
        upload_stop_source_.request_stop();
        state_ = SessionState::Paused;
        detail_ = "Paused";
        cb = status_callback_;
        snap = StatusSnapshot{state_, stats_, detail_};
    }
    cv_.notify_all();
    if (logger_) {
        logger_->log("session_paused");
    }
    if (cb) {
        cb(snap);
    }
}

void SessionController::resume() {
    StatusCallback cb;
    StatusSnapshot snap;
    {
        std::lock_guard lock(mutex_);
        if (!paused_ || stop_requested_) {
            return;
        }
        paused_ = false;
        upload_stop_source_ = std::stop_source{};
        state_ = SessionState::Capturing;
        detail_ = "Resumed";
        cb = status_callback_;
        snap = StatusSnapshot{state_, stats_, detail_};
    }
    cv_.notify_all();
    if (logger_) {
        logger_->log("session_resumed");
    }
    if (cb) {
        cb(snap);
    }
}

void SessionController::stop() {
    std::jthread old_worker;
    {
        std::lock_guard lock(mutex_);
        stop_requested_ = true;
        paused_ = false;
        upload_stop_source_.request_stop();
        if (worker_.joinable()) {
            worker_.request_stop();
            old_worker = std::move(worker_);
        }
        if (state_ != SessionState::Idle) {
            state_ = SessionState::Stopped;
            detail_ = "Stopped";
        }
    }
    cv_.notify_all();
    if (old_worker.joinable()) {
        old_worker.join();
    }
}

void SessionController::update_settings(ApiContractConfig contract,
                                        std::string api_credential,
                                        std::string system_prompt) {
    std::lock_guard lock(mutex_);
    contract_ = std::move(contract);
    api_credential_ = std::move(api_credential);
    system_prompt_ = std::move(system_prompt);
}

void SessionController::set_status_callback(StatusCallback cb) {
    std::lock_guard lock(mutex_);
    status_callback_ = std::move(cb);
}

StatusSnapshot SessionController::snapshot() const {
    std::lock_guard lock(mutex_);
    return StatusSnapshot{state_, stats_, detail_};
}

void SessionController::set_state(SessionState state, std::string detail) {
    StatusCallback cb;
    StatusSnapshot snap;
    {
        std::lock_guard lock(mutex_);
        state_ = state;
        detail_ = std::move(detail);
        cb = status_callback_;
        snap = StatusSnapshot{state_, stats_, detail_};
    }
    if (cb) {
        cb(snap);
    }
}

bool SessionController::wait_interruptibly(std::chrono::steady_clock::time_point until,
                                           std::stop_token worker_stop) {
    std::unique_lock lock(mutex_);
    return !cv_.wait_until(lock, worker_stop, until, [this, &worker_stop]() {
        return stop_requested_ || paused_ || worker_stop.stop_requested();
    });
}

void SessionController::run_loop(std::stop_token worker_stop) {
    auto scheduled_tick = std::chrono::steady_clock::now();

    while (!worker_stop.stop_requested()) {
        {
            std::unique_lock lock(mutex_);
            if (stop_requested_) {
                break;
            }
            if (paused_) {
                cv_.wait(lock, worker_stop, [this, &worker_stop]() {
                    return !paused_ || stop_requested_ || worker_stop.stop_requested();
                });
                if (stop_requested_ || worker_stop.stop_requested()) {
                    break;
                }
                scheduled_tick = std::chrono::steady_clock::now();
            }
        }

        ApiContractConfig contract;
        std::string credential;
        std::string system_prompt;
        std::stop_token upload_stop;
        {
            std::lock_guard lock(mutex_);
            contract = contract_;
            credential = api_credential_;
            system_prompt = system_prompt_;
            upload_stop = upload_stop_source_.get_token();
        }

        set_state(SessionState::Capturing, "Capturing screenshot");
        CapturedFrame frame = capture_source_->capture_frame(
            contract.image_format, contract.max_dimension, contract.max_image_bytes);

        if (worker_stop.stop_requested()) {
            break;
        }

        if (frame.status == CaptureStatus::TargetClosed) {
            {
                std::lock_guard lock(mutex_);
                stats_.last_error = ErrorCategory::TargetClosed;
            }
            if (logger_) {
                logger_->log("capture_target_closed", std::nullopt, ErrorCategory::TargetClosed);
            }
            set_state(SessionState::Stopped, "Stopped: target window closed");
            return;
        }

        if (frame.status == CaptureStatus::NoNewFrame) {
            const auto retry_at =
                std::chrono::steady_clock::now() + options_.no_frame_retry_delay;
            wait_interruptibly(retry_at, worker_stop);
            continue;
        }

        if (frame.status != CaptureStatus::Ok || frame.image_bytes.empty()) {
            std::uint64_t failures = 0;
            {
                std::lock_guard lock(mutex_);
                ++stats_.consecutive_failures;
                stats_.last_error = ErrorCategory::Capture;
                failures = stats_.consecutive_failures;
            }
            if (logger_) {
                logger_->log("capture_failed", std::nullopt, ErrorCategory::Capture);
            }
            const auto delay = compute_backoff_delay(
                failures, options_.base_backoff, options_.max_backoff, std::nullopt);
            set_state(SessionState::BackingOff, "Capture failed; backing off");
            if (wait_interruptibly(std::chrono::steady_clock::now() + delay, worker_stop)) {
                scheduled_tick = std::chrono::steady_clock::now();
            }
            continue;
        }

        {
            std::lock_guard lock(mutex_);
            ++stats_.frames_captured;
        }

        ApiRequestPayload payload{
            .system_prompt = std::move(system_prompt),
            .image_bytes = std::move(frame.image_bytes),
            .mime_type = std::move(frame.mime_type),
            .width = frame.width,
            .height = frame.height,
            .captured_at_utc = Logger::current_utc_timestamp(),
        };

        set_state(SessionState::Uploading, "Uploading screenshot");
        const ApiResponse response =
            api_client_->send_screenshot(contract, payload, credential, upload_stop);

        if (worker_stop.stop_requested()) {
            break;
        }

        {
            std::lock_guard lock(mutex_);
            if (paused_ || stop_requested_) {
                continue;
            }
        }

        if (response.ok()) {
            std::uint64_t skipped = 0;
            const auto now = std::chrono::steady_clock::now();
            const auto delay = compute_next_tick_delay(
                scheduled_tick, now, options_.capture_interval, skipped);
            {
                std::lock_guard lock(mutex_);
                ++stats_.uploads_succeeded;
                stats_.consecutive_failures = 0;
                stats_.last_http_status = response.http_status;
                stats_.last_error = ErrorCategory::None;
                stats_.ticks_skipped += skipped;
            }
            if (logger_) {
                logger_->log("upload_succeeded", response.http_status, ErrorCategory::None);
            }
            scheduled_tick = now + delay;
            set_state(SessionState::Capturing,
                      "Last upload OK (HTTP " + std::to_string(response.http_status) + ")");
            wait_interruptibly(scheduled_tick, worker_stop);
            continue;
        }

        if (response.error_category == ErrorCategory::Auth) {
            {
                std::lock_guard lock(mutex_);
                ++stats_.uploads_failed;
                stats_.last_http_status = response.http_status;
                stats_.last_error = ErrorCategory::Auth;
            }
            if (logger_) {
                logger_->log("upload_auth_failed", response.http_status, ErrorCategory::Auth);
            }
            set_state(SessionState::Stopped,
                      "Stopped: invalid credentials (HTTP " +
                          std::to_string(response.http_status) + ")");
            return;
        }

        if (response.error_category == ErrorCategory::Cancelled) {
            continue;
        }

        std::uint64_t failures = 0;
        {
            std::lock_guard lock(mutex_);
            ++stats_.uploads_failed;
            ++stats_.consecutive_failures;
            stats_.last_http_status = response.http_status;
            stats_.last_error = response.error_category;
            failures = stats_.consecutive_failures;
        }
        if (logger_) {
            logger_->log("upload_failed",
                         response.http_status > 0 ? std::optional<int>(response.http_status)
                                                  : std::nullopt,
                         response.error_category);
        }

        const auto backoff = compute_backoff_delay(
            failures, options_.base_backoff, options_.max_backoff, response.retry_after_seconds);
        set_state(SessionState::BackingOff,
                  std::string("Backing off (") + to_string(response.error_category) + ")");
        if (wait_interruptibly(std::chrono::steady_clock::now() + backoff, worker_stop)) {
            scheduled_tick = std::chrono::steady_clock::now();
        }
    }

    set_state(SessionState::Stopped, "Stopped");
}

}  // namespace stealth::core
