#include "core/logger.hpp"

#include <cstdio>
#include <ctime>

namespace stealth::core {

const char* to_string(ErrorCategory category) noexcept {
    switch (category) {
        case ErrorCategory::None: return "none";
        case ErrorCategory::Capture: return "capture";
        case ErrorCategory::TargetClosed: return "target_closed";
        case ErrorCategory::Encoding: return "encoding";
        case ErrorCategory::PayloadTooLarge: return "payload_too_large";
        case ErrorCategory::Network: return "network";
        case ErrorCategory::Timeout: return "timeout";
        case ErrorCategory::Auth: return "auth";
        case ErrorCategory::RateLimit: return "rate_limit";
        case ErrorCategory::Server: return "server";
        case ErrorCategory::Client: return "client";
        case ErrorCategory::Cancelled: return "cancelled";
        case ErrorCategory::Config: return "config";
    }
    return "unknown";
}

Logger::Logger(std::size_t max_entries) : max_entries_(max_entries) {}

void Logger::set_sink(Sink sink) {
    std::lock_guard lock(mutex_);
    sink_ = std::move(sink);
}

void Logger::log(std::string event,
                 std::optional<int> http_status,
                 ErrorCategory category) {
    LogEntry entry{
        .timestamp_utc = current_utc_timestamp(),
        .http_status = http_status,
        .category = category,
        .event = std::move(event),
    };

    Sink sink_copy;
    {
        std::lock_guard lock(mutex_);
        if (entries_.size() >= max_entries_ && max_entries_ > 0) {
            entries_.pop_front();
        }
        if (max_entries_ > 0) {
            entries_.push_back(entry);
        }
        sink_copy = sink_;
    }

    if (sink_copy) {
        sink_copy(entry);
    }
}

std::vector<LogEntry> Logger::recent_entries() const {
    std::lock_guard lock(mutex_);
    return {entries_.begin(), entries_.end()};
}

std::string Logger::current_utc_timestamp() {
    const auto now = std::chrono::system_clock::now();
    const std::time_t t = std::chrono::system_clock::to_time_t(now);
    std::tm tm_utc{};
#if defined(_WIN32)
    gmtime_s(&tm_utc, &t);
#else
    gmtime_r(&t, &tm_utc);
#endif
    char buf[80]{};
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ",
                  tm_utc.tm_year + 1900, tm_utc.tm_mon + 1, tm_utc.tm_mday,
                  tm_utc.tm_hour, tm_utc.tm_min, tm_utc.tm_sec);
    return std::string(buf);
}

}  // namespace stealth::core
