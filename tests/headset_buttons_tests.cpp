// SPDX-License-Identifier: MIT
// Offline tests for headset button edge/debounce logic and the audio-sink
// mapping. Windows COM init/uninit, discovery and endpoint calls are injected
// fakes: no real COM apartment, PnP enumeration or audio endpoint is touched.
#include "headset_buttons.hpp"
#include "headset_audio.hpp"
#include "../tools/psvr_diag/sensor_packet.hpp"

#include <array>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
using namespace psvr;
void check(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}

struct MockAudio : IHeadsetAudioControl {
    const char* name() const override { return "mock"; }
    int up = 0, down = 0, mute = 0;
    bool fail = false, explode = false;
    bool volume_step(bool is_up) override {
        if (explode) throw std::runtime_error("mock audio failure");
        if (fail) return false;
        if (is_up) ++up;
        else ++down;
        return true;
    }
    bool toggle_mute() override {
        if (explode) throw std::runtime_error("mock audio failure");
        if (fail) return false;
        ++mute;
        return true;
    }
};

std::string names(const std::vector<HeadsetButtonAction>& actions) {
    std::string out;
    for (const auto action : actions) {
        if (!out.empty()) out += ",";
        out += action == HeadsetButtonAction::VolumeUp      ? "up"
               : action == HeadsetButtonAction::VolumeDown  ? "down"
                                                             : "mute";
    }
    return out;
}

// Feeds (byte0, now_ms) frames through a fresh detector, returns all actions.
std::vector<HeadsetButtonAction> run(const std::vector<std::pair<std::uint8_t, std::uint64_t>>& frames) {
    HeadsetButtonEdgeState state;
    std::vector<HeadsetButtonAction> all;
    for (const auto& frame : frames) {
        const auto actions = poll_headset_button_edges(frame.first, frame.second, state);
        all.insert(all.end(), actions.begin(), actions.end());
    }
    return all;
}

#ifdef _WIN32
HRESULT init_result = S_OK;
unsigned init_calls = 0, uninit_calls = 0, enumerator_calls = 0, present_calls = 0;
bool interface_present = false;
HRESULT enumerator_result = E_FAIL;
bool return_fake_enumerator = false;
struct EmptyEndpoints final : IMMDeviceCollection {
    ULONG references = 1;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID,void** value) override {
        *value = nullptr; return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++references; }
    ULONG STDMETHODCALLTYPE Release() override { return --references; }
    HRESULT STDMETHODCALLTYPE GetCount(UINT* count) override { *count = 0; return S_OK; }
    HRESULT STDMETHODCALLTYPE Item(UINT,IMMDevice** value) override { *value = nullptr; return E_INVALIDARG; }
} empty_endpoints;
struct MissingDeviceEnumerator final : IMMDeviceEnumerator {
    ULONG references = 1;
    unsigned get_device_calls = 0, enumerate_calls = 0;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID,void** value) override {
        *value = nullptr; return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++references; }
    ULONG STDMETHODCALLTYPE Release() override { return --references; }
    HRESULT STDMETHODCALLTYPE EnumAudioEndpoints(EDataFlow flow,DWORD state,IMMDeviceCollection** value) override {
        check(flow == eCapture && state == DEVICE_STATE_ACTIVE, "capture discovery args wrong");
        ++enumerate_calls; empty_endpoints.AddRef(); *value = &empty_endpoints;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetDefaultAudioEndpoint(EDataFlow,ERole,IMMDevice** value) override {
        *value = nullptr; throw std::runtime_error("default audio endpoint must not be used");
    }
    HRESULT STDMETHODCALLTYPE GetDevice(LPCWSTR id,IMMDevice** value) override {
        check(std::wstring(id) == CoreAudioHeadsetControl::kRenderEndpointId, "unexpected render endpoint selected");
        ++get_device_calls; *value = nullptr; return HRESULT_FROM_WIN32(ERROR_NOT_FOUND);
    }
    HRESULT STDMETHODCALLTYPE RegisterEndpointNotificationCallback(IMMNotificationClient*) override {
        throw std::runtime_error("notification callback must not be registered");
    }
    HRESULT STDMETHODCALLTYPE UnregisterEndpointNotificationCallback(IMMNotificationClient*) override {
        throw std::runtime_error("notification callback must not be unregistered");
    }
} missing_device_enumerator;
HRESULT WINAPI fake_com_initialize(LPVOID reserved, DWORD flags) {
    check(reserved == nullptr && flags == COINIT_MULTITHREADED, "COM init arguments wrong");
    ++init_calls;
    return init_result;
}
void WINAPI fake_com_uninitialize() { ++uninit_calls; }
HRESULT fake_enumerator(IMMDeviceEnumerator** output) {
    ++enumerator_calls;
    *output = nullptr;
    if (return_fake_enumerator && SUCCEEDED(enumerator_result)) {
        missing_device_enumerator.AddRef(); *output = &missing_device_enumerator;
    }
    return enumerator_result;
}
bool fake_present() { ++present_calls; return interface_present; }

struct FakeMuteEndpoint {
    BOOL muted = FALSE;
    HRESULT get_result = S_OK, set_result = S_OK;
    unsigned gets = 0, sets = 0;
    BOOL last_set = FALSE;
    HRESULT GetMute(BOOL* value) {
        ++gets;
        if (SUCCEEDED(get_result)) *value = muted;
        return get_result;
    }
    HRESULT SetMute(BOOL value, LPCGUID context) {
        check(context == nullptr, "unexpected mute event context");
        ++sets; last_set = value;
        if (SUCCEEDED(set_result)) muted = value;
        return set_result;
    }
};

void test_com_ownership_and_mute() {
    for (const HRESULT result : {S_OK, S_FALSE, RPC_E_CHANGED_MODE, E_FAIL, E_OUTOFMEMORY}) {
        init_result = result; init_calls = 0; uninit_calls = 0;
        {
            ComScope scope(&fake_com_initialize,&fake_com_uninitialize);
            check(scope.result() == result, "HRESULT was narrowed/lost");
            check(scope.ok() == (SUCCEEDED(result) != 0), "COM readiness wrong");
            check(scope.owns_initialization() == (SUCCEEDED(result) != 0), "COM ownership wrong");
            check(init_calls == 1 && uninit_calls == 0, "COM uninitialized before scope exit");
        }
        check(uninit_calls == (SUCCEEDED(result) ? 1u : 0u), "COM init/uninit pairing wrong");
    }
    // A failed nested MTA init must not release an existing apartment owner.
    uninit_calls = 0; init_result = S_OK;
    {
        ComScope owner(&fake_com_initialize,&fake_com_uninitialize);
        init_result = RPC_E_CHANGED_MODE;
        { ComScope rejected(&fake_com_initialize,&fake_com_uninitialize); }
        check(uninit_calls == 0 && owner.ok(), "changed-mode released another owner's COM ref");
    }
    check(uninit_calls == 1, "owner did not release its own COM ref");
    // Both S_OK and nested S_FALSE acquire one independently balanced reference.
    uninit_calls = 0; init_result = S_OK;
    {
        ComScope outer(&fake_com_initialize,&fake_com_uninitialize);
        init_result = S_FALSE;
        { ComScope inner(&fake_com_initialize,&fake_com_uninitialize); }
        check(uninit_calls == 1, "S_FALSE reference not balanced");
    }
    check(uninit_calls == 2, "successful nested references not paired");
    // Stack unwinding must also balance the successful initialization.
    uninit_calls = 0; init_result = S_OK;
    try {
        ComScope scope(&fake_com_initialize,&fake_com_uninitialize);
        throw std::runtime_error("synthetic unwind");
    } catch (const std::runtime_error&) {}
    check(uninit_calls == 1, "COM reference leaked on stack unwind");

    FakeMuteEndpoint endpoint;
    const char* failure = nullptr;
    check(toggle_headset_endpoint_mute(endpoint,failure) && !failure && endpoint.muted == TRUE,
          "false -> true mute toggle failed");
    check(toggle_headset_endpoint_mute(endpoint,failure) && !failure && endpoint.muted == FALSE,
          "true -> false mute toggle failed");
    check(endpoint.gets == 2 && endpoint.sets == 2, "toggle must query and set once per edge");
    endpoint.get_result = E_FAIL;
    check(!toggle_headset_endpoint_mute(endpoint,failure) && failure && endpoint.sets == 2,
          "failed GetMute still called SetMute");
    check(std::string(failure).find("query failed") != std::string::npos, "GetMute failure reason missing");
    endpoint.get_result = S_OK; endpoint.set_result = E_FAIL;
    check(!toggle_headset_endpoint_mute(endpoint,failure) && failure && endpoint.muted == FALSE,
          "failed SetMute was reported as success");
    check(std::string(failure).find("toggle failed") != std::string::npos, "SetMute failure reason missing");
    endpoint.get_result = S_FALSE; endpoint.set_result = S_FALSE;
    check(toggle_headset_endpoint_mute(endpoint,failure) && !failure && endpoint.muted == TRUE,
          "successful nonzero HRESULT treated as failure");
    endpoint.muted = static_cast<BOOL>(-1);
    check(toggle_headset_endpoint_mute(endpoint,failure) && endpoint.last_set == FALSE,
          "noncanonical nonzero BOOL did not toggle to false");

    enumerator_calls = 0; present_calls = 0;
    unsigned logs = 0;
    CoreAudioHeadsetControl blocked(false,[&](const std::string&) { ++logs; },
                                   &fake_enumerator,&fake_present);
    check(!dispatch_headset_button_action(HeadsetButtonAction::VolumeUp,blocked), "COM failure forwarded volume");
    check(!dispatch_headset_button_action(HeadsetButtonAction::MuteToggle,blocked), "COM failure forwarded mute");
    check(!blocked.volume_step(false) && enumerator_calls == 0 && present_calls == 0,
          "COM failure accessed discovery/endpoints");
    check(logs == 1 && std::string(blocked.last_failure()).find("COM initialization") != std::string::npos,
          "COM failure diagnostic missing/not bounded");

    logs = 0; interface_present = false;
    CoreAudioHeadsetControl missing(true,[&](const std::string&) { ++logs; },
                                   &fake_enumerator,&fake_present);
    check(!missing.volume_step(true) && enumerator_calls == 1, "missing enumerator not fail-closed");
    check(!missing.volume_step(false) && enumerator_calls == 2 && logs == 1,
          "endpoint failure log not once-only / retry not edge-driven");
    check(!missing.toggle_mute() && present_calls == 1 && enumerator_calls == 2,
          "absent MI_01 still resolved capture endpoint");
    check(std::string(missing.last_failure()).find("MI_01") != std::string::npos, "MI_01 failure reason missing");
    interface_present = true;
    check(!missing.toggle_mute() && enumerator_calls == 3, "capture enumerator failure not fail-closed");
    check(std::string(missing.last_failure()).find("enumerator unavailable") != std::string::npos,
          "per-action failure reason stale after once-only log");
    enumerator_result = S_OK; // Defensive: even a success HRESULT needs a non-null object.
    check(!missing.volume_step(true) && enumerator_calls == 4, "null successful enumerator was dereferenced");
    return_fake_enumerator = true;
    {
        CoreAudioHeadsetControl no_endpoints(true,{},&fake_enumerator,&fake_present);
        check(!no_endpoints.volume_step(true) && missing_device_enumerator.get_device_calls == 1,
              "missing render endpoint did not fail closed");
        check(std::string(no_endpoints.last_failure()).find("render endpoint not found") != std::string::npos,
              "missing render endpoint reason absent");
        check(!no_endpoints.toggle_mute() && missing_device_enumerator.enumerate_calls == 1,
              "empty capture collection did not fail closed");
        check(std::string(no_endpoints.last_failure()).find("capture endpoint not found") != std::string::npos,
              "missing capture endpoint reason absent");
    }
    check(missing_device_enumerator.references == 1 && empty_endpoints.references == 1,
          "discovery failure leaked COM object references");
    return_fake_enumerator = false;
    enumerator_result = E_FAIL;
}
#endif
} // namespace

int main() {
    try {
        // Bit semantics must match docs/HEADSET_BUTTONS_20261018.md exactly.
        check(kHeadsetVolumeUpMask == 0x02 && kHeadsetVolumeDownMask == 0x04 &&
                  kHeadsetMuteMask == 0x08,
              "button bit semantics diverged from documented 2/4/8");
        check(kHeadsetButtonDebounceMs == 150, "debounce window changed");

        // Single press: one edge, one action.
        check(names(run({{0, 0}, {2, 10}, {2, 20}, {0, 30}})) == "up", "single press wrong");
        check(names(run({{0, 0}, {4, 10}, {4, 20}, {0, 30}})) == "down", "single down wrong");
        check(names(run({{0, 0}, {8, 10}, {8, 20}, {0, 30}})) == "mute", "single mute wrong");

        // Held button: no repeat while the bit stays set.
        check(names(run({{0, 0}, {2, 10}, {2, 20}, {2, 500}, {2, 900}, {0, 1000}})) == "up",
              "held button repeated");

        // Bounce: falling/rising inside the 150 ms guard emits nothing new.
        check(names(run({{0, 0}, {2, 10}, {0, 20}, {2, 100}, {0, 110}})) == "up",
              "bounce inside debounce window fired");
        // Same pattern after the window expires fires again.
        check(names(run({{0, 0}, {2, 10}, {0, 20}, {2, 100}, {0, 110}, {2, 400}, {0, 410}})) ==
                  "up,up",
              "re-press after debounce window did not fire");
        // Exact boundary (150 ms) is outside the suppression window: fires.
        check(names(run({{0, 0}, {2, 0}, {2, 10}, {0, 20}, {2, 160}})) == "up,up",
              "boundary re-press at 150 ms did not fire");

        // Simultaneous combination: one action per rising bit, fixed order.
        check(names(run({{0, 0}, {0x0E, 10}})) == "up,down,mute", "combination order wrong");
        // Partial combination with one bit already held: only new edges fire.
        check(names(run({{0, 0}, {2, 10}, {0x06, 20}})) == "up,down",
              "partial combination fired stale edge");

        // Debounce is per button: mute edge right after volume is not suppressed.
        check(names(run({{0, 0}, {2, 10}, {0x0A, 20}, {0, 30}})) == "up,mute",
              "independent per-button debounce wrong");

        // Unknown bits (0/4/5/6/7 here) are ignored.
        check(run({{0, 0}, {0xF1, 10}, {0xF1, 20}, {0, 30}}).empty(), "unknown bits fired");

        // First report only arms: a button held at activation emits nothing.
        check(run({{2, 0}, {2, 10}, {2, 200}}).empty(), "first-report held button fired");
        // Release then press after arming works.
        check(names(run({{2, 0}, {0, 10}, {2, 200}})) == "up", "post-arm press did not fire");

        // Mapping: action -> sink call, order preserved.
        {
            MockAudio mock;
            check(dispatch_headset_button_action(HeadsetButtonAction::VolumeUp, mock), "up dispatch failed");
            check(dispatch_headset_button_action(HeadsetButtonAction::VolumeDown, mock), "down dispatch failed");
            check(dispatch_headset_button_action(HeadsetButtonAction::MuteToggle, mock), "mute dispatch failed");
            check(mock.up == 1 && mock.down == 1 && mock.mute == 1, "sink call counts wrong");
        }
        // Fail-closed: sink failure and sink exceptions never propagate.
        {
            MockAudio failing;
            failing.fail = true;
            check(!dispatch_headset_button_action(HeadsetButtonAction::VolumeUp, failing),
                  "sink failure not reported");
            check(!dispatch_headset_button_action(HeadsetButtonAction::MuteToggle, failing),
                  "mute failure not reported");
            MockAudio exploding;
            exploding.explode = true;
            check(!dispatch_headset_button_action(HeadsetButtonAction::VolumeDown, exploding),
                  "sink exception propagated");
            check(!dispatch_headset_button_action(HeadsetButtonAction::MuteToggle, exploding),
                  "mute exception propagated");
        }

        // Match the driver's decoder gate: invalid reports cannot arm/advance
        // edges. Baseline, held, short report, release, valid re-press.
        {
            HeadsetButtonEdgeState state;
            std::array<unsigned char,65> bytes{};
            auto feed = [&](std::size_t size, std::uint64_t ms) {
                const auto packet = decode_sensor(bytes.data(),size);
                return packet ? poll_headset_button_edges(packet->buttons_raw,ms,state)
                              : std::vector<HeadsetButtonAction>{};
            };
            bytes[0] = 2;
            check(feed(63,0).empty() && !state.initialized, "short report armed detector");
            check(feed(65,1).empty() && !state.initialized, "long report armed detector");
            check(!decode_sensor(nullptr,64), "null sensor report accepted");
            check(feed(64,10).empty() && state.initialized, "first valid held report fired");
            bytes[0] = 0;
            check(feed(63,20).empty() && state.previous == 2, "invalid report changed held state");
            check(feed(64,30).empty(), "valid release fired");
            bytes[0] = 2;
            check(names(feed(64,200)) == "up", "valid report after rejection did not dispatch");
        }
        {
            HeadsetButtonDiagnostics diagnostics;
            check(diagnostics.observe(0,20,16), "baseline diagnostic missing");
            check(!diagnostics.observe(0,20,16), "unchanged report logged per frame");
            check(diagnostics.observe(2,20,16) && diagnostics.observe(2,21,16) &&
                  diagnostics.observe(2,21,24), "raw/volume/flags transitions missing");
            for (unsigned i=0;i<16;++i) check(diagnostics.allow_log(i), "diagnostic burst limit too low");
            check(!diagnostics.allow_log(16) && diagnostics.suppressed == 1, "burst not bounded");
            check(diagnostics.allow_log(1000), "diagnostic rate window did not reopen");
            for (unsigned i=17;i<256;++i)
                check(diagnostics.allow_log(static_cast<std::uint64_t>(i)*1000), "lifetime cap too low");
            check(!diagnostics.allow_log(300000) && diagnostics.emitted == 256, "lifetime log cap missing");
            check(diagnostics.take_limit_notice() && !diagnostics.take_limit_notice(), "limit notice not once-only");
            check(names(run({{0,0},{2,10},{2,500},{0,600},{2,800}})) == "up,up",
                  "diagnostics must not change hold/debounce/dispatch semantics");
        }
#ifdef _WIN32
        test_com_ownership_and_mute();
        std::cout << "PASS mock COM: HRESULT, pairing, changed-mode, unwind, fail-closed discovery\n"
                     "PASS mock mute backend: false/true/false, query/set failures, missing endpoint\n";
#endif

        std::cout << "PASS buttons: single press, hold, bounce/debounce, combinations\n"
                     "PASS buttons: bit semantics, arming, unknown-bit ignore\n"
                     "PASS audio mapping: mock sink order/counts, fail-closed on error\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "FAIL: " << e.what() << '\n';
        return 1;
    }
}
