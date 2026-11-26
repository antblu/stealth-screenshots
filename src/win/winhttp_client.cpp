#include "win/winhttp_client.hpp"

#include "core/url_utils.hpp"
#include "win/utf8.hpp"

#include <windows.h>
#include <winhttp.h>

#include <atomic>
#include <mutex>

namespace stealth::win {
namespace {

struct ScopedWinHttpHandle {
    HINTERNET handle = nullptr;
    ScopedWinHttpHandle() = default;
    explicit ScopedWinHttpHandle(HINTERNET h) : handle(h) {}
    ~ScopedWinHttpHandle() { reset(); }
    ScopedWinHttpHandle(const ScopedWinHttpHandle&) = delete;
    ScopedWinHttpHandle& operator=(const ScopedWinHttpHandle&) = delete;

    void reset(HINTERNET h = nullptr) {
        if (handle) {
            WinHttpCloseHandle(handle);
        }
        handle = h;
    }
};

core::ErrorCategory classify_winhttp_error(DWORD err) noexcept {
    if (err == ERROR_WINHTTP_TIMEOUT) {
        return core::ErrorCategory::Timeout;
    }
    if (err == ERROR_WINHTTP_OPERATION_CANCELLED) {
        return core::ErrorCategory::Cancelled;
    }
    return core::ErrorCategory::Network;
}

}  // namespace

core::ApiResponse WinHttpApiClient::send_screenshot(
    const core::ApiContractConfig& config,
    const core::ApiRequestPayload& payload,
    const std::string& api_credential,
    std::stop_token stop_token) {
    if (stop_token.stop_requested()) {
        return {0, core::ErrorCategory::Cancelled, std::nullopt};
    }

    const auto parsed =
        core::parse_endpoint_url(config.endpoint_url, config.allow_http_localhost);
    if (!parsed) {
        return {0, core::ErrorCategory::Config, std::nullopt};
    }

    const auto json_body = core::serialize_llm_request_json(payload, config);
    if (!json_body) {
        return {0, core::ErrorCategory::PayloadTooLarge, std::nullopt};
    }

    ScopedWinHttpHandle session(WinHttpOpen(
        L"StealthScreenshots/0.1",
        WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
        WINHTTP_NO_PROXY_NAME,
        WINHTTP_NO_PROXY_BYPASS,
        0));
    if (!session.handle) {
        return {0, classify_winhttp_error(GetLastError()), std::nullopt};
    }

    const int timeout_ms = static_cast<int>(config.request_timeout_ms);
    WinHttpSetTimeouts(session.handle, timeout_ms, timeout_ms, timeout_ms, timeout_ms);

    const std::wstring w_host = utf8_to_wide(parsed->host);
    const std::wstring w_path = utf8_to_wide(parsed->path_and_query);

    ScopedWinHttpHandle connect(WinHttpConnect(
        session.handle,
        w_host.c_str(),
        static_cast<INTERNET_PORT>(parsed->port),
        0));
    if (!connect.handle) {
        return {0, classify_winhttp_error(GetLastError()), std::nullopt};
    }

    const DWORD flags = parsed->is_https ? WINHTTP_FLAG_SECURE : 0;
    ScopedWinHttpHandle request(WinHttpOpenRequest(
        connect.handle,
        L"POST",
        w_path.c_str(),
        nullptr,
        WINHTTP_NO_REFERER,
        WINHTTP_DEFAULT_ACCEPT_TYPES,
        flags));
    if (!request.handle) {
        return {0, classify_winhttp_error(GetLastError()), std::nullopt};
    }

    std::mutex cancel_mutex;
    std::atomic<bool> cancelled{false};
    std::stop_callback stop_cb(stop_token, [&]() {
        cancelled.store(true);
        std::lock_guard lock(cancel_mutex);
        request.reset();
    });

    std::wstring headers = L"Content-Type: application/json; charset=utf-8\r\n";
    if (!api_credential.empty()) {
        headers += utf8_to_wide(config.auth_header_name);
        headers += L": ";
        if (!config.auth_scheme.empty()) {
            headers += utf8_to_wide(config.auth_scheme);
            headers += L" ";
        }
        headers += utf8_to_wide(api_credential);
        headers += L"\r\n";
    }

    HINTERNET req_handle = nullptr;
    {
        std::lock_guard lock(cancel_mutex);
        req_handle = request.handle;
    }
    if (!req_handle || cancelled.load()) {
        return {0, core::ErrorCategory::Cancelled, std::nullopt};
    }

    const DWORD body_len = static_cast<DWORD>(json_body->size());
    if (!WinHttpSendRequest(
            req_handle,
            headers.c_str(),
            static_cast<DWORD>(headers.size()),
            const_cast<char*>(json_body->data()),
            body_len,
            body_len,
            0)) {
        const DWORD err = GetLastError();
        if (cancelled.load()) {
            return {0, core::ErrorCategory::Cancelled, std::nullopt};
        }
        return {0, classify_winhttp_error(err), std::nullopt};
    }

    if (!WinHttpReceiveResponse(req_handle, nullptr)) {
        const DWORD err = GetLastError();
        if (cancelled.load()) {
            return {0, core::ErrorCategory::Cancelled, std::nullopt};
        }
        return {0, classify_winhttp_error(err), std::nullopt};
    }

    DWORD status_code = 0;
    DWORD status_size = sizeof(status_code);
    if (!WinHttpQueryHeaders(
            req_handle,
            WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
            WINHTTP_HEADER_NAME_BY_INDEX,
            &status_code,
            &status_size,
            WINHTTP_NO_HEADER_INDEX)) {
        return {0, core::ErrorCategory::Network, std::nullopt};
    }

    std::optional<std::uint32_t> retry_after;
    wchar_t retry_buf[64]{};
    DWORD retry_bytes = sizeof(retry_buf);
    if (WinHttpQueryHeaders(
            req_handle,
            WINHTTP_QUERY_RETRY_AFTER,
            WINHTTP_HEADER_NAME_BY_INDEX,
            retry_buf,
            &retry_bytes,
            WINHTTP_NO_HEADER_INDEX)) {
        retry_after = core::parse_retry_after_seconds(wide_to_utf8(retry_buf));
    }

    // Drain small response bodies cleanly without storing or displaying them in v1.
    DWORD available = 0;
    char discard_buf[4096];
    while (WinHttpQueryDataAvailable(req_handle, &available) && available > 0) {
        DWORD read_bytes = 0;
        const DWORD to_read = available < sizeof(discard_buf)
                                  ? available
                                  : static_cast<DWORD>(sizeof(discard_buf));
        if (!WinHttpReadData(req_handle, discard_buf, to_read, &read_bytes) ||
            read_bytes == 0) {
            break;
        }
    }

    const int status_int = static_cast<int>(status_code);
    return core::ApiResponse{
        .http_status = status_int,
        .error_category = core::classify_http_status(status_int),
        .retry_after_seconds = retry_after,
    };
}

}  // namespace stealth::win
