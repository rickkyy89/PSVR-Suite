// SPDX-License-Identifier: MIT
#include "display_windows_abi.hpp"
#include <cstring>
#include <iostream>

namespace {
void check(bool value,const char* message) { if (!value) throw std::runtime_error(message); }
template<class F> F slot(void* object,std::size_t index) {
    return reinterpret_cast<F>((*static_cast<const std::uintptr_t**>(object))[index]);
}
}
int main() {
    try {
        psvr::DisplayConfig config;
        config.enabled=true; config.monitor_path="synthetic";
        config.desktop_x=-1920; config.desktop_y=10;
        config.refresh_numerator=60; config.refresh_denominator=1;
        config.render_width=960; config.render_height=1080;
        config.projection[0]={-1,1,-1,1}; config.projection[1]={-1,1,-1,1};
        psvr::DisplayComponent core(config);
        psvr::WindowsDisplayAbi boundary(core);
        void* object=boundary.runtime_interface();
        const auto original_vtable=*static_cast<const std::uintptr_t**>(object);
        int32_t x=0,y=0; uint32_t w=0,h=0,vx=0,vy=0;
        slot<void(*)(void*,int32_t*,int32_t*,uint32_t*,uint32_t*)>(object,0)(object,&x,&y,&w,&h);
        check(x==-1920 && y==10 && w==1920 && h==1080,"MSVC-shaped bounds slot");
        check(slot<bool(*)(void*)>(object,1)(object) && slot<bool(*)(void*)>(object,2)(object),"boolean slots");
        slot<void(*)(void*,uint32_t*,uint32_t*)>(object,3)(object,&w,&h);
        check(w==960 && h==1080,"render slot");
        slot<void(*)(void*,vr::EVREye,uint32_t*,uint32_t*,uint32_t*,uint32_t*)>(object,4)(object,vr::Eye_Right,&vx,&vy,&w,&h);
        check(vx==960 && vy==0 && w==960 && h==1080,"viewport slot");
        float l=0,r=0,t=0,b=0;
        slot<void(*)(void*,vr::EVREye,float*,float*,float*,float*)>(object,5)(object,vr::Eye_Left,&l,&r,&t,&b);
        check(l==-1 && r==1 && t==-1 && b==1,"projection slot");
        using MsvcSret = vr::DistortionCoordinates_t*(*)(void*,vr::DistortionCoordinates_t*,vr::EVREye,float,float);
        struct Guarded { unsigned before=0x12345678; vr::DistortionCoordinates_t out{}; unsigned after=0xabcdef01; } storage;
        for (int i=0;i<1000;++i) {
            auto* result=slot<MsvcSret>(object,6)(object,&storage.out,vr::Eye_Left,.25f,.75f);
            check(result==&storage.out,"MSVC RAX must return sret buffer");
            check(storage.out.rfRed[0]==.25f && storage.out.rfBlue[1]==.75f,"MSVC-shaped aggregate return correct");
            check(*static_cast<const std::uintptr_t**>(object)==original_vtable,"aggregate must not overwrite this/vtable");
        }
        check(storage.before==0x12345678 && storage.after==0xabcdef01,"sret writes exactly aggregate size");
        vr::HmdVector2_t inverse{};
        check(slot<bool(*)(void*,vr::HmdVector2_t*,vr::EVREye,uint32_t,float,float)>(object,7)(object,&inverse,vr::Eye_Left,2,.25f,.75f),"inverse slot");
        check(inverse.v[0]==.25f && inverse.v[1]==.75f,"inverse output");
        std::cout<<"PASS Windows MSVC-shaped display vtable and aggregate return (1000 calls)\n";
        return 0;
    } catch (const std::exception& error) { std::cerr<<error.what()<<'\n';return 1; }
}
