#include "core/base64.hpp"

namespace stealth::core {

std::string base64_encode(std::span<const std::uint8_t> data) {
    static constexpr char kAlphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

    if (data.empty()) {
        return {};
    }

    std::string out;
    out.reserve(((data.size() + 2) / 3) * 4);

    std::size_t i = 0;
    while (i + 3 <= data.size()) {
        const std::uint32_t triple =
            (static_cast<std::uint32_t>(data[i]) << 16) |
            (static_cast<std::uint32_t>(data[i + 1]) << 8) |
            static_cast<std::uint32_t>(data[i + 2]);
        out.push_back(kAlphabet[(triple >> 18) & 0x3F]);
        out.push_back(kAlphabet[(triple >> 12) & 0x3F]);
        out.push_back(kAlphabet[(triple >> 6) & 0x3F]);
        out.push_back(kAlphabet[triple & 0x3F]);
        i += 3;
    }

    const std::size_t remaining = data.size() - i;
    if (remaining == 1) {
        const std::uint32_t single = static_cast<std::uint32_t>(data[i]) << 16;
        out.push_back(kAlphabet[(single >> 18) & 0x3F]);
        out.push_back(kAlphabet[(single >> 12) & 0x3F]);
        out.push_back('=');
        out.push_back('=');
    } else if (remaining == 2) {
        const std::uint32_t pair =
            (static_cast<std::uint32_t>(data[i]) << 16) |
            (static_cast<std::uint32_t>(data[i + 1]) << 8);
        out.push_back(kAlphabet[(pair >> 18) & 0x3F]);
        out.push_back(kAlphabet[(pair >> 12) & 0x3F]);
        out.push_back(kAlphabet[(pair >> 6) & 0x3F]);
        out.push_back('=');
    }

    return out;
}

}  // namespace stealth::core
