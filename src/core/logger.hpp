#pragma once

#include <chrono>
#include <deque>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace stealth::core {

enum class ErrorCategory {
    None,
    Capture,
    TargetClosed,
    Encoding,
    PayloadTooLarge,
    Network,
    Timeout,
    Auth,
    RateLimit,
    Server,
    Client,
    Cancelled,
    Config,
};

const char* to_string(ErrorCategory category) noexcept;

struct LogEntry {
    std::string timestamp_utc;
    std::optional<int> http_status;
    ErrorCategory category = ErrorCategory::None;
    std::string event;
};

class Logger {
public:
    using Sink = std::function<void(const LogEntry&)>;

    explicit Logger(std::size_t max_entries = 256);

    void set_sink(Sink sink);
    void log(std::string event,
             std::optional<int> http_status = std::nullopt,
             ErrorCategory category = ErrorCategory::None);

    std::vector<LogEntry> recent_entries() const;
    static std::string current_utc_timestamp();

private:
    std::size_t max_entries_;
    mutable std::mutex mutex_;
    std::deque<LogEntry> entries_;
    Sink sink_;
};

}  // namespace stealth::core
