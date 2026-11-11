// SPDX-License-Identifier: MIT
// M9 loopback UDP receiver. Winsock RAII, loopback-only bind, bounded wait so
// the owning thread can observe stop/recenter promptly, and strict parsing of
// the versioned metric protocol in position_protocol.hpp.
//
// IMPORTANT: include this header before anything that includes <windows.h>
// (for example hidapi.h). <winsock2.h> must precede <windows.h>.
#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>

#include "position_protocol.hpp"
#include <array>
#include <atomic>
#include <cstdint>
#include <functional>
#include <stdexcept>
#include <string>

namespace psvr {

// Process-wide Winsock lifetime. Reference-counted by Windows, so multiple
// instances (driver + tests) are safe.
class WinsockSession {
    bool started_ = false;
public:
    WinsockSession() {
        WSADATA data{};
        started_ = WSAStartup(MAKEWORD(2, 2), &data) == 0;
    }
    ~WinsockSession() { if (started_) WSACleanup(); }
    bool ok() const { return started_; }
    WinsockSession(const WinsockSession&) = delete;
    WinsockSession& operator=(const WinsockSession&) = delete;
};

// Receive-only UDP socket bound to 127.0.0.1. `poll` waits at most
// `timeout_ms` and never blocks longer than that, so a stop flag or a recenter
// request is serviced on every iteration.
class PositionReceiver {
public:
    using Sink = std::function<void(const PositionSample&)>;

    explicit PositionReceiver(const PositionConfig& config, Sink sink)
        : config_(config), sink_(std::move(sink)) {
        if (!winsock_.ok()) throw std::runtime_error("position: Winsock initialization failed");
        socket_ = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (socket_ == INVALID_SOCKET) throw std::runtime_error("position: UDP socket creation failed");
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        address.sin_port = htons(config_.port);
        if (::bind(socket_, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == SOCKET_ERROR) {
            const int error = WSAGetLastError();
            ::closesocket(socket_); socket_ = INVALID_SOCKET;
            throw std::runtime_error("position: loopback bind failed on port " +
                                     std::to_string(config_.port) + " (WSA " + std::to_string(error) + ")");
        }
        int length = sizeof(address);
        if (::getsockname(socket_, reinterpret_cast<sockaddr*>(&address), &length) == 0)
            local_port_ = ntohs(address.sin_port);
        else
            local_port_ = config_.port;
    }

    ~PositionReceiver() { if (socket_ != INVALID_SOCKET) ::closesocket(socket_); }
    PositionReceiver(const PositionReceiver&) = delete;
    PositionReceiver& operator=(const PositionReceiver&) = delete;

    std::uint16_t local_port() const { return local_port_; }

    // Wait up to timeout_ms for one datagram. Returns 1 when a datagram was
    // consumed (valid or not), 0 on timeout/transient error, -1 on a fatal
    // socket error.
    int poll(int timeout_ms) {
        fd_set readable;
        FD_ZERO(&readable);
        FD_SET(socket_, &readable);
        timeval timeout{};
        timeout.tv_sec = timeout_ms / 1000;
        timeout.tv_usec = (timeout_ms % 1000) * 1000;
        const int ready = ::select(0, &readable, nullptr, nullptr, &timeout);
        if (ready == 0) return 0;
        if (ready == SOCKET_ERROR) {
            const int error = WSAGetLastError();
            if (error == WSAEINTR) return 0;
            ++fatal_;
            return -1;
        }
        std::array<char, 2048> buffer{};
        sockaddr_in from{};
        int from_length = sizeof(from);
        const int received = ::recvfrom(socket_, buffer.data(), static_cast<int>(buffer.size()), 0,
                                        reinterpret_cast<sockaddr*>(&from), &from_length);
        if (received == SOCKET_ERROR) {
            const int error = WSAGetLastError();
            // UDP on Windows surfaces ICMP port-unreachable as WSAECONNRESET;
            // that is not a receiver failure. WSAEMSGSIZE means truncation.
            if (error == WSAECONNRESET || error == WSAEINTR || error == WSAETIMEDOUT) return 0;
            if (error == WSAEMSGSIZE) { ++datagrams_; ++oversized_; return 1; }
            if (error == WSAENOTSOCK) { ++fatal_; return -1; }
            ++errors_;
            return 0;
        }
        if (received <= 0) return 0;
        ++datagrams_;
        if (received >= static_cast<int>(buffer.size()) ||
            received > static_cast<int>(position_max_packet_bytes)) {
            ++oversized_;
            return 1;
        }
        const auto now = PositionClock::now();
        const auto parsed = parse_position_packet(
            std::string_view(buffer.data(), static_cast<std::size_t>(received)), config_, now);
        if (!parsed) { ++malformed_; return 1; }
        ++parsed_;
        if (sink_) sink_(*parsed);
        return 1;
    }

    // Serve until `stop` becomes true. Fatal socket errors return; the caller
    // marks the position faulted.
    void run(const std::atomic<bool>& stop) {
        while (!stop.load(std::memory_order_relaxed)) {
            if (poll(50) < 0) return;
        }
    }

    struct Counters {
        std::uint64_t datagrams = 0, parsed = 0, malformed = 0,
                      oversized = 0, errors = 0, fatal = 0;
    };
    Counters counters() const {
        return {datagrams_.load(), parsed_.load(), malformed_.load(),
                oversized_.load(), errors_.load(), fatal_.load()};
    }

private:
    WinsockSession winsock_;
    PositionConfig config_;
    Sink sink_;
    SOCKET socket_ = INVALID_SOCKET;
    std::uint16_t local_port_ = 0;
    std::atomic<std::uint64_t> datagrams_{0}, parsed_{0}, malformed_{0},
                               oversized_{0}, errors_{0}, fatal_{0};
};

} // namespace psvr
