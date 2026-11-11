// SPDX-License-Identifier: MIT
#include "device_session.hpp"
#include <iostream>
#include <thread>

int main() {
    try {
        const auto name = L"Local\\PSVR-PC-Offline-Ownership-" + std::to_wstring(GetCurrentProcessId());
        {
            psvr::DeviceSession owner(name);
            bool rejected = false;
            std::thread competitor([&] {
                try { psvr::DeviceSession rival(name); } catch (const std::runtime_error&) { rejected = true; }
            });
            competitor.join();
            if (!rejected) throw std::runtime_error("second owner was not rejected");
            psvr::DeviceSession distinct(name + L"-other");
        }
        { psvr::DeviceSession reacquired(name); }
        const auto abandoned_name = name + L"-abandoned";
        // Keep the object alive after its owning thread exits without release.
        HANDLE keep_alive = CreateMutexW(nullptr, FALSE, abandoned_name.c_str());
        if (!keep_alive) throw std::runtime_error("test mutex creation failed");
        DWORD result = WAIT_FAILED;
        std::thread abandoning([&] { result = WaitForSingleObject(keep_alive, 0); });
        abandoning.join();
        if (result != WAIT_OBJECT_0) { CloseHandle(keep_alive); throw std::runtime_error("test abandonment setup failed"); }
        try { psvr::DeviceSession recovered(abandoned_name); }
        catch (...) { CloseHandle(keep_alive); throw; }
        CloseHandle(keep_alive);
        std::cout << "PASS ownership: 4 cases on isolated per-process names; production mutex unused\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL ownership: " << error.what() << '\n'; return 1;
    }
}
