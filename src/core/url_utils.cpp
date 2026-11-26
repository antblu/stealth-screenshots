#include "core/url_utils.hpp"

#include <algorithm>
#include <cctype>
#include <charconv>

namespace stealth::core {
namespace {

std::string to_lower_ascii(std::string_view sv) {
    std::string out(sv);
    std::transform(out.begin(), out.end(), out.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return out;
}

bool is_loopback_host(std::string_view host) {
    const std::string lower = to_lower_ascii(host);
    return lower == "localhost" || lower == "127.0.0.1" || lower == "::1";
}

}  // namespace

std::optional<ParsedUrl> parse_endpoint_url(std::string_view url,
                                            bool allow_http_localhost) {
    if (url.empty()) {
        return std::nullopt;
    }

    const auto scheme_pos = url.find("://");
    if (scheme_pos == std::string_view::npos) {
        return std::nullopt;
    }

    const std::string scheme = to_lower_ascii(url.substr(0, scheme_pos));
    bool is_https = false;
    std::uint16_t default_port = 443;
    if (scheme == "https") {
        is_https = true;
        default_port = 443;
    } else if (scheme == "http" && allow_http_localhost) {
        is_https = false;
        default_port = 80;
    } else {
        return std::nullopt;
    }

    std::string_view rest = url.substr(scheme_pos + 3);
    if (rest.empty()) {
        return std::nullopt;
    }

    const auto path_pos = rest.find_first_of("/?#");
    const std::string_view authority =
        path_pos == std::string_view::npos ? rest : rest.substr(0, path_pos);
    const std::string_view path_part =
        path_pos == std::string_view::npos ? std::string_view{} : rest.substr(path_pos);

    if (authority.empty() || authority.find('@') != std::string_view::npos) {
        return std::nullopt;
    }

    std::string host;
    std::uint16_t port = default_port;

    if (authority.front() == '[') {
        const auto close_bracket = authority.find(']');
        if (close_bracket == std::string_view::npos) {
            return std::nullopt;
        }
        host = std::string(authority.substr(1, close_bracket - 1));
        if (host.empty()) {
            return std::nullopt;
        }
        if (close_bracket + 1 < authority.size()) {
            if (authority[close_bracket + 1] != ':') {
                return std::nullopt;
            }
            const auto port_sv = authority.substr(close_bracket + 2);
            unsigned int parsed_port = 0;
            const auto [ptr, ec] =
                std::from_chars(port_sv.data(), port_sv.data() + port_sv.size(), parsed_port);
            if (ec != std::errc{} || ptr != port_sv.data() + port_sv.size() ||
                parsed_port == 0 || parsed_port > 65535) {
                return std::nullopt;
            }
            port = static_cast<std::uint16_t>(parsed_port);
        }
    } else {
        const auto colon_pos = authority.rfind(':');
        if (colon_pos != std::string_view::npos) {
            host = std::string(authority.substr(0, colon_pos));
            const auto port_sv = authority.substr(colon_pos + 1);
            unsigned int parsed_port = 0;
            const auto [ptr, ec] =
                std::from_chars(port_sv.data(), port_sv.data() + port_sv.size(), parsed_port);
            if (ec != std::errc{} || ptr != port_sv.data() + port_sv.size() ||
                parsed_port == 0 || parsed_port > 65535) {
                return std::nullopt;
            }
            port = static_cast<std::uint16_t>(parsed_port);
        } else {
            host = std::string(authority);
        }
    }

    if (host.empty()) {
        return std::nullopt;
    }
    if (!is_https && !is_loopback_host(host)) {
        return std::nullopt;
    }

    std::string path_and_query = "/";
    if (!path_part.empty()) {
        if (path_part.front() == '/') {
            path_and_query = std::string(path_part);
        } else {
            path_and_query = "/" + std::string(path_part);
        }
    }

    return ParsedUrl{
        .is_https = is_https,
        .host = std::move(host),
        .port = port,
        .path_and_query = std::move(path_and_query),
    };
}

}  // namespace stealth::core
