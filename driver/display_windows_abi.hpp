// SPDX-License-Identifier: MIT
#pragma once
#include "display_component.hpp"
#include <cstdint>
#include <type_traits>

namespace psvr {
// Windows x64 MSVC member-sret ABI: RCX=this, RDX=aggregate result buffer.
// MinGW GCC uses RCX=sret, RDX=this for C++ member aggregate returns. OpenVR's
// ComputeDistortion returns 24 bytes, unlike its other display methods.
// Expose an explicit C-shaped _003 vtable at the runtime boundary; internal
// calls remain within our compiler's native C++ ABI. No RTTI/destructor slots
// are present in IVRDisplayComponent_003. Never use this interface pointer for
// a GNU-C++ aggregate-return call: it is for the MSVC SteamVR host only.
class WindowsDisplayAbi {
    struct Boundary {
        const std::uintptr_t* vtable;
        DisplayComponent* core;
    } boundary_;
    static void bounds(Boundary* b,int32_t* x,int32_t* y,uint32_t* w,uint32_t* h) { b->core->GetWindowBounds(x,y,w,h); }
    static bool desktop(Boundary* b) { return b->core->IsDisplayOnDesktop(); }
    static bool real(Boundary* b) { return b->core->IsDisplayRealDisplay(); }
    static void render(Boundary* b,uint32_t* w,uint32_t* h) { b->core->GetRecommendedRenderTargetSize(w,h); }
    static void viewport(Boundary* b,vr::EVREye e,uint32_t* x,uint32_t* y,uint32_t* w,uint32_t* h) { b->core->GetEyeOutputViewport(e,x,y,w,h); }
    static void projection(Boundary* b,vr::EVREye e,float* l,float* r,float* t,float* d) { b->core->GetProjectionRaw(e,l,r,t,d); }
    static vr::DistortionCoordinates_t* distortion(Boundary* b,vr::DistortionCoordinates_t* out,vr::EVREye e,float u,float v) {
        *out = b->core->ComputeDistortion(e,u,v);
        return out;
    }
    static bool inverse(Boundary* b,vr::HmdVector2_t* out,vr::EVREye e,uint32_t channel,float u,float v) {
        return b->core->ComputeInverseDistortion(out,e,channel,u,v);
    }
    static const std::uintptr_t* table() {
        // B9-08 portability: function-pointer -> uintptr_t via reinterpret_cast
        // is correct only under the host-only MSVC SteamVR contract; validated
        // solely on the current compiler, not portable beyond it.
        static const std::uintptr_t entries[] = {
            reinterpret_cast<std::uintptr_t>(&bounds),reinterpret_cast<std::uintptr_t>(&desktop),
            reinterpret_cast<std::uintptr_t>(&real),reinterpret_cast<std::uintptr_t>(&render),
            reinterpret_cast<std::uintptr_t>(&viewport),reinterpret_cast<std::uintptr_t>(&projection),
            reinterpret_cast<std::uintptr_t>(&distortion),reinterpret_cast<std::uintptr_t>(&inverse)};
        return entries;
    }
public:
    explicit WindowsDisplayAbi(DisplayComponent& core) : boundary_{table(),&core} {
        static_assert(sizeof(void*) == 8,"Windows x64 boundary only");
        static_assert(sizeof(vr::DistortionCoordinates_t) == 24,"SDK distortion aggregate changed");
        static_assert(!std::has_virtual_destructor<vr::IVRDisplayComponent>::value,"SDK vtable changed");
    }
    WindowsDisplayAbi(const WindowsDisplayAbi&) = delete;
    WindowsDisplayAbi& operator=(const WindowsDisplayAbi&) = delete;
    void* runtime_interface() noexcept { return &boundary_; }
};
} // namespace psvr
