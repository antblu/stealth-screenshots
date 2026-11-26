#include "core/api_contract.hpp"

#include "core/base64.hpp"

#include <cctype>
#include <charconv>
#include <cstdio>

namespace stealth::core {

std::string json_escape(std::string_view input) {
    std::string out;
    out.reserve(input.size() + 8);
    for (unsigned char c : input) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[7]{};
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out.push_back(static_cast<char>(c));
                }
                break;
        }
    }
    return out;
}

std::optional<std::string> serialize_llm_request_json(
    const ApiRequestPayload& payload,
    const ApiContractConfig& config) {
    if (payload.image_bytes.empty() ||
        payload.image_bytes.size() > config.max_image_bytes) {
        return std::nullopt;
    }

    const std::string encoded_image = base64_encode(payload.image_bytes);
    const std::string mime =
        payload.mime_type.empty() ? "image/png" : payload.mime_type;

    std::string json;
    json.reserve(encoded_image.size() + payload.system_prompt.size() + 256);

    json += "{\"model\":\"";
    json += json_escape(config.model);
    json += "\",\"messages\":[";

    json += "{\"role\":\"system\",\"content\":\"";
    json += json_escape(payload.system_prompt);
    json += "\"},";

    json += "{\"role\":\"user\",\"content\":[";
    json += "{\"type\":\"text\",\"text\":\"Screenshot captured at ";
    json += json_escape(payload.captured_at_utc);
    json += "\"},";
    json += "{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:";
    json += json_escape(mime);
    json += ";base64,";
    json += encoded_image;
    json += "\"}}";
    json += "]}";

    json += "]}";
    return json;
}

ErrorCategory classify_http_status(int status_code) noexcept {
    if (status_code >= 200 && status_code < 300) {
        return ErrorCategory::None;
    }
    if (status_code == 401 || status_code == 403) {
        return ErrorCategory::Auth;
    }
    if (status_code == 429) {
        return ErrorCategory::RateLimit;
    }
    if (status_code == 408) {
        return ErrorCategory::Timeout;
    }
    if (status_code >= 400 && status_code < 500) {
        return ErrorCategory::Client;
    }
    if (status_code >= 500 && status_code < 600) {
        return ErrorCategory::Server;
    }
    return ErrorCategory::Network;
}

std::optional<std::uint32_t> parse_retry_after_seconds(std::string_view header_value) {
    while (!header_value.empty() &&
           std::isspace(static_cast<unsigned char>(header_value.front()))) {
        header_value.remove_prefix(1);
    }
    while (!header_value.empty() &&
           std::isspace(static_cast<unsigned char>(header_value.back()))) {
        header_value.remove_suffix(1);
    }
    if (header_value.empty()) {
        return std::nullopt;
    }

    std::uint32_t seconds = 0;
    const auto [ptr, ec] = std::from_chars(
        header_value.data(), header_value.data() + header_value.size(), seconds);
    if (ec != std::errc{} || ptr != header_value.data() + header_value.size()) {
        return std::nullopt;
    }
    return seconds;
}

}  // namespace stealth::core
