#pragma once

#include "core/logger.hpp"

#include <cstdint>
#include <optional>
#include <stop_token>
#include <string>
#include <string_view>
#include <vector>

namespace stealth::core {

enum class ImageFormat {
    Png,
    Jpeg,
};

struct ApiContractConfig {
    std::string endpoint_url;
    std::string model = "default";
    std::string auth_header_name = "Authorization";
    std::string auth_scheme = "Bearer";
    ImageFormat image_format = ImageFormat::Png;
    std::size_t max_image_bytes = 4 * 1024 * 1024;  // 4 MiB raw encoded image limit
    std::uint32_t max_dimension = 1920;
    std::uint32_t request_timeout_ms = 15000;
    bool allow_http_localhost = false;
};

struct ApiRequestPayload {
    std::string system_prompt;
    std::vector<std::uint8_t> image_bytes;
    std::string mime_type = "image/png";
    std::uint32_t width = 0;
    std::uint32_t height = 0;
    std::string captured_at_utc;
};

struct ApiResponse {
    int http_status = 0;
    ErrorCategory error_category = ErrorCategory::None;
    std::optional<std::uint32_t> retry_after_seconds;

    bool ok() const noexcept {
        return error_category == ErrorCategory::None &&
               http_status >= 200 && http_status < 300;
    }
};

std::string json_escape(std::string_view input);

std::optional<std::string> serialize_llm_request_json(
    const ApiRequestPayload& payload,
    const ApiContractConfig& config);

ErrorCategory classify_http_status(int status_code) noexcept;

std::optional<std::uint32_t> parse_retry_after_seconds(std::string_view header_value);

class IApiClient {
public:
    virtual ~IApiClient() = default;
    virtual ApiResponse send_screenshot(const ApiContractConfig& config,
                                        const ApiRequestPayload& payload,
                                        const std::string& api_credential,
                                        std::stop_token stop_token) = 0;
};

}  // namespace stealth::core
