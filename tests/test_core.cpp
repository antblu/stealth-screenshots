#include "core/api_contract.hpp"
#include "core/base64.hpp"
#include "core/logger.hpp"
#include "core/session_controller.hpp"
#include "core/url_utils.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

using namespace std::chrono_literals;
using namespace stealth::core;

int g_failures = 0;

#define CHECK(expr)                                                                 \
    do {                                                                            \
        if (!(expr)) {                                                              \
            std::cerr << "CHECK failed at " << __FILE__ << ":" << __LINE__ << ": " \
                      << #expr << "\n";                                             \
            ++g_failures;                                                           \
        }                                                                           \
    } while (0)

class FakeCaptureSource final : public ICaptureSource {
public:
    std::atomic<int> calls{0};
    std::atomic<bool> close_on_next{false};

    CapturedFrame capture_frame(ImageFormat,
                                std::uint32_t,
                                std::size_t) override {
        const int n = ++calls;
        if (close_on_next.load()) {
            return {CaptureStatus::TargetClosed, {}, {}, 0, 0};
        }
        return CapturedFrame{
            .status = CaptureStatus::Ok,
            .image_bytes = {0x89, 0x50, 0x4E, 0x47, static_cast<std::uint8_t>(n & 0xFF)},
            .mime_type = "image/png",
            .width = 1280,
            .height = 720,
        };
    }
};

class FakeApiClient final : public IApiClient {
public:
    std::atomic<int> calls{0};
    std::atomic<int> in_flight{0};
    std::atomic<int> max_in_flight{0};
    std::atomic<int> delay_ms{0};
    std::mutex mutex;
    std::vector<ApiResponse> scripted_responses;
    std::string last_credential;
    std::string last_system_prompt;
    std::vector<std::uint8_t> last_image_bytes;

    ApiResponse send_screenshot(const ApiContractConfig&,
                                const ApiRequestPayload& payload,
                                const std::string& api_credential,
                                std::stop_token stop_token) override {
        const int current = ++in_flight;
        int prev_max = max_in_flight.load();
        while (current > prev_max &&
               !max_in_flight.compare_exchange_weak(prev_max, current)) {
        }

        ++calls;
        {
            std::lock_guard lock(mutex);
            last_credential = api_credential;
            last_system_prompt = payload.system_prompt;
            last_image_bytes = payload.image_bytes;
        }

        const int sleep_ms = delay_ms.load();
        if (sleep_ms > 0) {
            const auto end = std::chrono::steady_clock::now() +
                             std::chrono::milliseconds(sleep_ms);
            while (std::chrono::steady_clock::now() < end) {
                if (stop_token.stop_requested()) {
                    --in_flight;
                    return {0, ErrorCategory::Cancelled, std::nullopt};
                }
                std::this_thread::sleep_for(5ms);
            }
        }

        --in_flight;

        std::lock_guard lock(mutex);
        if (!scripted_responses.empty()) {
            ApiResponse r = scripted_responses.front();
            scripted_responses.erase(scripted_responses.begin());
            return r;
        }
        return {200, ErrorCategory::None, std::nullopt};
    }
};

void test_base64() {
    CHECK(base64_encode({}).empty());
    const std::vector<std::uint8_t> a{'f'};
    CHECK(base64_encode(a) == "Zg==");
    const std::vector<std::uint8_t> ab{'f', 'o'};
    CHECK(base64_encode(ab) == "Zm8=");
    const std::vector<std::uint8_t> abc{'f', 'o', 'o'};
    CHECK(base64_encode(abc) == "Zm9v");
}

void test_url_parsing() {
    CHECK(!parse_endpoint_url("").has_value());
    CHECK(!parse_endpoint_url("http://api.example.com/v1/chat").has_value());
    CHECK(!parse_endpoint_url("http://192.168.1.10:8080/v1/chat", true).has_value());

    const auto https_url = parse_endpoint_url("https://llm.example.com:8443/v1/chat?k=1");
    CHECK(https_url.has_value());
    CHECK(https_url->is_https);
    CHECK(https_url->host == "llm.example.com");
    CHECK(https_url->port == 8443);
    CHECK(https_url->path_and_query == "/v1/chat?k=1");

    const auto local_url = parse_endpoint_url("http://127.0.0.1:8080/v1/chat", true);
    CHECK(local_url.has_value());
    CHECK(!local_url->is_https);
    CHECK(local_url->host == "127.0.0.1");
    CHECK(local_url->port == 8080);
}

void test_json_serialization_and_status() {
    ApiContractConfig cfg;
    cfg.endpoint_url = "https://api.example.com/v1/chat";
    cfg.model = "vision-v1";
    cfg.max_image_bytes = 16;

    ApiRequestPayload payload;
    payload.system_prompt = "Line 1\n\"Quoted\"";
    payload.image_bytes = {1, 2, 3};
    payload.mime_type = "image/png";
    payload.captured_at_utc = "2026-11-25T00:00:00Z";

    const auto json = serialize_llm_request_json(payload, cfg);
    CHECK(json.has_value());
    CHECK(json->find("\"role\":\"system\",\"content\":\"Line 1\\n\\\"Quoted\\\"\"") !=
          std::string::npos);
    CHECK(json->find("data:image/png;base64,AQID") != std::string::npos);

    payload.image_bytes.assign(32, 0xFF);
    CHECK(!serialize_llm_request_json(payload, cfg).has_value());

    CHECK(classify_http_status(200) == ErrorCategory::None);
    CHECK(classify_http_status(401) == ErrorCategory::Auth);
    CHECK(classify_http_status(403) == ErrorCategory::Auth);
    CHECK(classify_http_status(429) == ErrorCategory::RateLimit);
    CHECK(classify_http_status(503) == ErrorCategory::Server);
    CHECK(parse_retry_after_seconds(" 15 ") == 15u);
    CHECK(!parse_retry_after_seconds("abc").has_value());
}

void test_tick_and_backoff_math() {
    const auto t0 = std::chrono::steady_clock::now();
    std::uint64_t skipped = 0;
    const auto delay1 = compute_next_tick_delay(t0, t0 + 2s, 10s, skipped);
    CHECK(skipped == 0);
    CHECK(delay1 == 8s);

    const auto delay2 = compute_next_tick_delay(t0, t0 + 12s, 10s, skipped);
    CHECK(skipped == 1);
    CHECK(delay2 == 8s);

    CHECK(compute_backoff_delay(1, 2000ms, 60000ms, std::nullopt) == 2000ms);
    CHECK(compute_backoff_delay(2, 2000ms, 60000ms, std::nullopt) == 4000ms);
    CHECK(compute_backoff_delay(1, 2000ms, 60000ms, 10) == 10000ms);
    CHECK(compute_backoff_delay(10, 2000ms, 60000ms, 120) == 60000ms);
}

void test_session_recurring_and_tick_skip() {
    auto cap = std::make_shared<FakeCaptureSource>();
    auto api = std::make_shared<FakeApiClient>();
    auto log = std::make_shared<Logger>();
    api->delay_ms.store(90);

    SessionOptions opts;
    opts.capture_interval = 40ms;
    opts.base_backoff = 20ms;
    opts.max_backoff = 100ms;

    SessionController ctrl(cap, api, log, opts);
    ApiContractConfig cfg;
    cfg.endpoint_url = "https://api.example.com/v1/chat";

    ctrl.start(cfg, "secret-token", "Be concise.");
    std::this_thread::sleep_for(230ms);
    ctrl.stop();

    const auto snap = ctrl.snapshot();
    CHECK(api->max_in_flight.load() == 1);
    CHECK(snap.stats.uploads_succeeded >= 2);
    CHECK(snap.stats.ticks_skipped >= 1);
    CHECK(api->last_credential == "secret-token");
    CHECK(api->last_system_prompt == "Be concise.");

    // Verify logger never recorded credentials or prompts.
    for (const auto& entry : log->recent_entries()) {
        CHECK(entry.event.find("secret-token") == std::string::npos);
        CHECK(entry.event.find("Be concise") == std::string::npos);
    }
}

void test_session_backoff_and_auth_stop() {
    auto cap = std::make_shared<FakeCaptureSource>();
    auto api = std::make_shared<FakeApiClient>();
    auto log = std::make_shared<Logger>();

    api->scripted_responses = {
        {429, ErrorCategory::RateLimit, 0},
        {500, ErrorCategory::Server, std::nullopt},
        {200, ErrorCategory::None, std::nullopt},
        {401, ErrorCategory::Auth, std::nullopt},
    };

    SessionOptions opts;
    opts.capture_interval = 20ms;
    opts.base_backoff = 15ms;
    opts.max_backoff = 50ms;

    SessionController ctrl(cap, api, log, opts);
    ApiContractConfig cfg;
    cfg.endpoint_url = "https://api.example.com/v1/chat";
    ctrl.start(cfg, "bad-token", "Prompt");

    for (int i = 0; i < 40; ++i) {
        if (ctrl.snapshot().state == SessionState::Stopped) {
            break;
        }
        std::this_thread::sleep_for(15ms);
    }

    const auto snap = ctrl.snapshot();
    CHECK(snap.state == SessionState::Stopped);
    CHECK(snap.stats.last_error == ErrorCategory::Auth);
    CHECK(snap.stats.last_http_status == 401);
    CHECK(snap.stats.uploads_succeeded == 1);
    CHECK(snap.stats.uploads_failed == 3);
}

void test_session_pause_resume_and_target_closed() {
    auto cap = std::make_shared<FakeCaptureSource>();
    auto api = std::make_shared<FakeApiClient>();
    auto log = std::make_shared<Logger>();

    SessionOptions opts;
    opts.capture_interval = 25ms;
    SessionController ctrl(cap, api, log, opts);

    ApiContractConfig cfg;
    cfg.endpoint_url = "https://api.example.com/v1/chat";
    ctrl.start(cfg, "tok", "Prompt");

    std::this_thread::sleep_for(40ms);
    ctrl.pause();
    const auto paused_snap = ctrl.snapshot();
    CHECK(paused_snap.state == SessionState::Paused);
    const auto count_while_paused = api->calls.load();
    std::this_thread::sleep_for(70ms);
    CHECK(api->calls.load() == count_while_paused);

    cap->close_on_next.store(true);
    ctrl.resume();
    for (int i = 0; i < 30; ++i) {
        if (ctrl.snapshot().state == SessionState::Stopped) {
            break;
        }
        std::this_thread::sleep_for(10ms);
    }

    const auto final_snap = ctrl.snapshot();
    CHECK(final_snap.state == SessionState::Stopped);
    CHECK(final_snap.stats.last_error == ErrorCategory::TargetClosed);
}

}  // namespace

int run_core_tests() {
    test_base64();
    test_url_parsing();
    test_json_serialization_and_status();
    test_tick_and_backoff_math();
    test_session_recurring_and_tick_skip();
    test_session_backoff_and_auth_stop();
    test_session_pause_resume_and_target_closed();
    return g_failures;
}
