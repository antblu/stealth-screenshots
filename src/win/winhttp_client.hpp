#pragma once

#include "core/api_contract.hpp"

namespace stealth::win {

class WinHttpApiClient final : public core::IApiClient {
public:
    core::ApiResponse send_screenshot(const core::ApiContractConfig& config,
                                        const core::ApiRequestPayload& payload,
                                        const std::string& api_credential,
                                        std::stop_token stop_token) override;
};

}  // namespace stealth::win
