// SPDX-License-Identifier: MIT
#pragma once
#include "display_component.hpp"
#include "virtual_display_contract.hpp"
#include <memory>
#include <string>
#include <functional>

namespace psvr::presenter {
// MSVC/Windows implementation. One worker owns window, immediate context and
// swapchain. API callbacks only enqueue/wait/query; no borrowed handle is closed.
class D3D11Presenter {
    struct Impl;
    std::unique_ptr<Impl> impl_;
    void dispose() noexcept;
public:
    using Log = std::function<void(const std::string&)>;
    D3D11Presenter(const DisplayConfig& config, Log log, bool test_pattern=false);
    ~D3D11Presenter();
    D3D11Presenter(const D3D11Presenter&)=delete;
    D3D11Presenter& operator=(const D3D11Presenter&)=delete;
    bool submit(const void* info, std::uint32_t size) noexcept;
    void wait_for_present() noexcept;
    bool time_since_vsync(float* seconds, std::uint64_t* count) noexcept;
    std::uint64_t adapter_luid() const noexcept;
    bool healthy() const noexcept;
    void shutdown() noexcept;
};
} // namespace psvr::presenter
