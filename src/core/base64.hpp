#pragma once

#include <cstdint>
#include <span>
#include <string>

namespace stealth::core {

std::string base64_encode(std::span<const std::uint8_t> data);

}  // namespace stealth::core
