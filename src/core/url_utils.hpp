#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace stealth::core {

struct ParsedUrl {
    bool is_https = true;
    std::string host;
    std::uint16_t port = 443;
    std::string path_and_query = "/";
};

std::optional<ParsedUrl> parse_endpoint_url(std::string_view url,
                                            bool allow_http_localhost = false);

}  // namespace stealth::core
