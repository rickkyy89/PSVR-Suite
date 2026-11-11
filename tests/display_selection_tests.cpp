// SPDX-License-Identifier: MIT
#include "display_selection.hpp"
#include <iostream>
#include <stdexcept>

namespace {
std::size_t checks = 0;
void check(bool value,const char* message) { ++checks; if (!value) throw std::runtime_error(message); }
}
int main() {
    try {
        using namespace psvr;
        DisplayConfig config;
        config.enabled=true; config.monitor_path="exact-path";
        config.desktop_x=-1920; config.desktop_y=20;
        config.refresh_numerator=60000; config.refresh_denominator=1001;
        config.render_width=1200; config.render_height=1350;
        config.projection[0]={-1,1,-1,1}; config.projection[1]={-1,1,-1,1};
        display::Display selected;
        selected.active=selected.target_available=true;
        selected.monitor_device_path=config.monitor_path;
        selected.source=display::SourceGeometry{-1920,20,1920,1080};
        selected.target=display::TargetGeometry{1920,1080,{120000,2002},1};
        selected.refresh={60000,1001}; selected.scaling=selected.rotation=selected.scanline_ordering=1;
        check(matches_display_config(config,{selected}),"matching fractional display geometry");
        const auto verify = [&](const DisplayConfig& c) { return matches_display_config(c,{selected}); };
        check(make_verified_display(config,verify)!=nullptr,"inventory gate produces verified component");
        auto changed=selected;
        changed.monitor_device_path="substring-exact-path";
        check(!matches_display_config(config,{changed}),"exact identity no substring fallback");
        changed=selected; changed.source->x=0;
        check(!matches_display_config(config,{changed}),"desktop rearrangement invalidates");
        changed=selected; changed.source->y=0;
        check(!matches_display_config(config,{changed}),"vertical origin change invalidates");
        changed=selected; changed.refresh={60,1}; changed.target->signal_refresh={60,1};
        check(!matches_display_config(config,{changed}),"rounded Hz cannot replace fractional rate");
        changed=selected; changed.target->signal_refresh={90,1};
        check(!matches_display_config(config,{changed}),"scanout mismatch invalidates");
        changed=selected; changed.scaling=128;
        check(!matches_display_config(config,{changed}),"custom scaling refused");
        changed=selected; changed.monitor_device_path="other";
        check(!matches_display_config(config,{selected,changed}),"clone source invalidates");
        check(!matches_display_config(config,{}),"unplug invalidates");
        auto invalid=config; invalid.enabled=false;
        check(!matches_display_config(invalid,{selected}),"disabled config cannot pass selection");
        invalid=config; invalid.projection[0]={};
        check(!matches_display_config(invalid,{selected}),"unknown projection refused");
        DisplayTopologyGuard guard;
        check(guard.update(true),"initial verification valid");
        check(!guard.update(false),"topology loss invalidates");
        check(!guard.update(true) && !guard.valid(),"reappearing monitor cannot resurrect immutable component");
        // The HMD retains this guard across Deactivate/Activate. A matching
        // snapshot on a later activation cannot reset the same lifetime latch.
        check(!guard.valid() && !guard.update(matches_display_config(config,{selected})),
              "restored topology after deactivation still refuses same-HMD activation");
        DisplayTopologyGuard restarted_guard;
        check(restarted_guard.update(matches_display_config(config,{selected})),
               "new runtime/HMD lifetime may verify restored topology");
        auto native=config; native.native_direct_mode=true;
        native.edid_vendor_id=0xd94d; native.edid_product_id=0x6a04;
        native.desktop_membership=[] { return false; };
        // B9-01 fallback: snapshot semantics are unchanged until the production
        // membership callback can distinguish unknown identity from absence.
        check(matches_display_config(native,{}),"expected GPU hide is not a desktop topology fault or proof of scanout");
        selected.edid_ids=display::EdidIds{0xd94d,0x6a04,true};
        check(matches_display_config(native,{selected}),"matching visible identity allowed before native transition");
        changed=selected; changed.monitor_device_path="other-instance";
        check(!matches_display_config(native,{selected,changed}),"GPU EDID selection rejects duplicate physical identity");
        changed=selected; changed.edid_ids->product=0xc207;
        check(!matches_display_config(native,{changed}),"exact path with contradictory EDID rejected");
        auto unidentified=selected;
        unidentified.monitor_device_path.reset(); unidentified.edid_ids.reset();
        check(matches_display_config(native,{unidentified}),
              "B9-01 remains open: snapshot cannot exclude an active target with unreadable identity");
        int membership_calls=0;
        native.desktop_membership=[&] { ++membership_calls; return false; };
        check(native_desktop_absence_verified(native) && membership_calls==1,
              "separate helper accepts confirmed absence from a contract-compliant query");
        const auto absence_verify = [&](const DisplayConfig& c) {
            return matches_display_config(c,{}) && native_desktop_absence_verified(c);
        };
        check(make_verified_display(native,absence_verify)!=nullptr,
              "synthetic trusted verifier can AND snapshot consistency and absence");
        native.desktop_membership=[] { return true; };
        check(!native_desktop_absence_verified(native),"separate helper refuses desktop presence or unknown");
        bool refused=false;
        try { make_verified_display(native,absence_verify); }
        catch (const std::runtime_error&) { refused=true; }
        check(refused,"synthetic AND verifier refuses component exposure while on desktop");
        check(matches_display_config(native,{}),"snapshot alone remains non-authorizing and ignores membership");
        native.desktop_membership=[]() -> bool { throw std::runtime_error("synthetic query failure"); };
        check(!native_desktop_absence_verified(native),"separate helper fails closed on standard exceptions");
        native.desktop_membership=[]() -> bool { throw 7; };
        check(!native_desktop_absence_verified(native),"separate helper fails closed on nonstandard exceptions");
        native.desktop_membership={};
        check(!native_desktop_absence_verified(native),"separate helper fails closed on missing query");
        native.native_direct_mode=false;
        native.desktop_membership=[&] { ++membership_calls; return false; };
        const auto before=membership_calls;
        check(!native_desktop_absence_verified(native) && membership_calls==before,
              "extended mode never queries native absence");
        std::cout << "PASS " << checks << " display selection/config integration, topology loss latch and separate absence helper checks (synthetic only)\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
