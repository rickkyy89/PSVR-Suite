// SPDX-License-Identifier: MIT
#pragma once
#include "position_receiver.hpp" // Winsock must precede Windows headers
#include "imu_export_contract.hpp"
#include <condition_variable>
#include <thread>

namespace psvr {
// Single owner; callback copies only. Destruction joins before closing socket.
class ImuExporter {
public:
    using Source = std::function<TrackingSnapshot()>;
    ImuExporter(ImuExportConfig config, std::string session, Source source, int stale_ms = 100)
        : config_(config), session_(std::move(session)), source_(std::move(source)), stale_ms_(stale_ms) {
        if (!config_.enabled) return;
        if (!config_.port || config_.hz < 1 || config_.hz > 1000 || !winsock_.ok())
            throw std::runtime_error("invalid IMU export configuration");
        socket_ = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        u_long nonblocking = 1;
        if (socket_ == INVALID_SOCKET || ::ioctlsocket(socket_,FIONBIO,&nonblocking) != 0) {
            close(); throw std::runtime_error("IMU export socket setup failed");
        }
        destination_.sin_family = AF_INET;
        destination_.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        destination_.sin_port = htons(config_.port);
        try { thread_ = std::thread([this] { run(); }); }
        catch (...) { close(); throw; }
    }
    ~ImuExporter() { stop(); close(); }
    ImuExporter(const ImuExporter&) = delete;
    ImuExporter& operator=(const ImuExporter&) = delete;
    void stop() {
        { std::lock_guard<std::mutex> lock(wait_mutex_); stopped_ = true; }
        wait_.notify_all();
        if (thread_.joinable()) thread_.join();
    }
private:
    void close() { if (socket_ != INVALID_SOCKET) { ::closesocket(socket_); socket_ = INVALID_SOCKET; } }
    void run() noexcept {
        const auto period = std::chrono::duration<double>(1.0/config_.hz);
        std::uint64_t seq = 0;
        for (;;) {
            { std::lock_guard<std::mutex> lock(wait_mutex_); if (stopped_) return; }
            try {
                const auto s = source_(); // lock released before codec/send
                const auto text = encode_driver_imu(s,session_,++seq,DriverClock::now(),stale_ms_);
                if (!text.empty()) ::sendto(socket_,text.data(),static_cast<int>(text.size()),0,
                    reinterpret_cast<const sockaddr*>(&destination_),sizeof(destination_));
            } catch (...) {} // Transport/codec faults never affect 3DoF.
            std::unique_lock<std::mutex> lock(wait_mutex_);
            // No catch-up bursts after stalls.
            if (wait_.wait_for(lock,period,[this] { return stopped_; })) return;
        }
    }
    ImuExportConfig config_;
    std::string session_;
    Source source_;
    int stale_ms_;
    WinsockSession winsock_;
    SOCKET socket_ = INVALID_SOCKET;
    sockaddr_in destination_{};
    std::thread thread_;
    std::mutex wait_mutex_;
    std::condition_variable wait_;
    bool stopped_ = false;
};
} // namespace psvr
