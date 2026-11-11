// SPDX-License-Identifier: MIT
#include "display_component.hpp"
#include "optical_profile.hpp"
#include <iostream>

namespace {
unsigned checks=0, strict_float_rejections=0;
void check(bool value,const char* message) { ++checks; if (!value) throw std::runtime_error(message); }
void close(double a,double b,const char* message) { check(std::abs(a-b)<2e-5,message); }
void roundtrips(const psvr::DisplayConfig& config) {
    using namespace psvr;
    DisplayComponent display(config);
    for (auto eye : {vr::Eye_Left,vr::Eye_Right}) {
        // Closed grid: every edge and all four corners, all RGB and both eyes.
        for (int row=0; row<=10; ++row) for (int col=0; col<=10; ++col) {
            const float u=col/10.f,v=row/10.f;
            const auto mapping=display.ComputeDistortion(eye,u,v);
            const auto math_eye=eye==vr::Eye_Left?distortion::Eye::left:distortion::Eye::right;
            const auto expected=config.optical_mapper->forward_rgb(math_eye,{u,v});
            check(expected.has_value(),"grid in mapper domain");
            const float* rgb[] = {mapping.rfRed,mapping.rfGreen,mapping.rfBlue};
            for (std::size_t i=0; i<3; ++i) {
                check(rgb[i][0]==static_cast<float>((*expected)[i].u),"exact forward float U, correct direction");
                check(rgb[i][1]==static_cast<float>((*expected)[i].v),"exact forward float V, no Y flip");
            }
            const float* sdk_order[] = {mapping.rfRed,mapping.rfBlue,mapping.rfGreen};
            const distortion::Channel channels[] = {distortion::Channel::red,distortion::Channel::blue,distortion::Channel::green};
            for (uint32_t channel=0; channel<3; ++channel) {
                const distortion::UV target{sdk_order[channel][0],sdk_order[channel][1]};
                const auto strict=config.optical_mapper->inverse(math_eye,channels[channel],target);
                const auto zero=config.optical_mapper->inverse_with_uncertainty(math_eye,channels[channel],target,0.0);
                check(strict.has_value()==zero.has_value(),"zero uncertainty retains strict domain");
                if (strict) check(strict->u==zero->u && strict->v==zero->v,"zero uncertainty retains strict result");
                else ++strict_float_rejections;
                vr::HmdVector2_t recovered{{7,8}};
                check(display.ComputeInverseDistortion(&recovered,eye,channel,sdk_order[channel][0],sdk_order[channel][1]),"inverse closed-grid float roundtrip");
                close(recovered.v[0],u,"SDK red/blue/green inverse U");
                close(recovered.v[1],v,"SDK red/blue/green inverse V");
            }
        }
    }
}
}
int main(int argc,char** argv) {
    try {
        using namespace psvr;
        DisplayConfig config;
        config.enabled=true; config.monitor_path="synthetic";
        config.refresh_numerator=120; config.refresh_denominator=1;
        config.render_width=960; config.render_height=1080;
        config.projection[0]={-1,1,-1,1}; config.projection[1]={-1,1,-1,1};
        config.optical_mode=OpticalMode::profile;
        check(config.validation_error()!=nullptr,"profile without mapper must fail");
        auto profile=distortion::Profile{};
        profile.eyes[0].center={.43,.56}; profile.eyes[1].center={.57,.44};
        profile.eyes[0].scale_x=1.3; profile.eyes[1].scale_y=1.2;
        profile.eyes[0].a=.02; profile.eyes[1].b=.03;
        profile.eyes[0].rgb={1.01,1.02,1.03}; profile.eyes[1].rgb={1.03,1.02,1.01};
        config.optical_mapper=distortion::Mapper::create(profile);
        config.optical_profile_name="synthetic asymmetric RGB";
        check(!config.validation_error(),"explicit valid mapper accepted");
        DisplayComponent display(config);
        roundtrips(config);
        check(strict_float_rejections>0,"regression exercises boundaries rejected by strict double inverse");
        const auto invalid=display.ComputeDistortion(vr::Eye_Left,-.01f,.5f);
        check(invalid.rfRed[0]==-1 && invalid.rfBlue[1]==-1,"out of profile domain no identity fallback");
        const auto nan=std::numeric_limits<float>::quiet_NaN();
        check(display.ComputeDistortion(vr::Eye_Left,nan,.5f).rfGreen[0]==-1,"NaN forward stays finite invalid");
        vr::HmdVector2_t unchanged{{7,8}};
        check(!display.ComputeInverseDistortion(&unchanged,vr::Eye_Left,1,100,100),"inverse outside image rejected");
        check(unchanged.v[0]==7 && unchanged.v[1]==8,"inverse rejection leaves buffer untouched");
        check(!display.ComputeInverseDistortion(&unchanged,vr::Eye_Left,3,.5f,.5f),"bad channel rejected");
        check(!display.ComputeInverseDistortion(&unchanged,static_cast<vr::EVREye>(2),0,.5f,.5f),"bad eye rejected");
        check(!display.ComputeInverseDistortion(&unchanged,vr::Eye_Left,0,nan,.5f),"NaN inverse rejected");
        check(!display.ComputeInverseDistortion(nullptr,vr::Eye_Left,0,.5f,.5f),"null buffer rejected");
        check(unchanged.v[0]==7 && unchanged.v[1]==8,"all invalid calls preserve buffer");

        auto candidate=config;
        auto collapsed=distortion::Profile{};
        for (auto& p:collapsed.eyes) { p.d=2e-8; p.rgb={.001,.001,.001}; }
        candidate.optical_mapper=distortion::Mapper::create(collapsed);
        check(candidate.optical_mapper.has_value(),"collapse example remains valid general math");
        const auto collapse_uv=candidate.optical_mapper->forward(distortion::Eye::left,distortion::Channel::red,{0,1});
        check(collapse_uv && static_cast<float>(collapse_uv->u)==.5f && static_cast<float>(collapse_uv->v)==.5f,"review example collapses in float");
        check(candidate.validation_error()!=nullptr,"SDK rejects collapse numerically");
        bool threw=false;
        try { DisplayComponent refused(candidate); } catch(const std::invalid_argument&) { threw=true; }
        check(threw,"component cannot expose unusable profile");

        // Exercise each absolute polynomial contribution, every eye/channel,
        // and both scale limits independently of general mapper validity.
        for (int eye=0; eye<2; ++eye) for (int field=0; field<7; ++field) {
            auto p=distortion::Profile{};
            auto& e=p.eyes[eye];
            if (field==0) e.scale_x=.249;
            if (field==1) e.scale_y=4.001;
            if (field==2) e.c=6;
            if (field==3) e.b=6;
            if (field==4) e.a=6;
            if (field==5) e.d=.099;
            if (field==6) e.d=8.001;
            candidate.optical_mapper=distortion::Mapper::create(p);
            check(candidate.optical_mapper.has_value(),"general math policy not narrowed");
            check(candidate.validation_error()!=nullptr,"SDK numerical bounds enforced");
        }
        for (int eye=0; eye<2; ++eye) for (int channel=0; channel<3; ++channel) {
            auto p=distortion::Profile{}; p.eyes[eye].rgb[channel]=.099;
            candidate.optical_mapper=distortion::Mapper::create(p);
            check(candidate.validation_error()!=nullptr,"each RGB lower bound enforced");
            p.eyes[eye].rgb[channel]=8.001;
            candidate.optical_mapper=distortion::Mapper::create(p);
            check(candidate.validation_error()!=nullptr,"each RGB upper bound enforced");
        }

        for (double gain : {.100001,.1,8.0,7.999999,2.0}) {
            auto p=distortion::Profile{};
            for (auto& e:p.eyes) { e.d=gain; e.scale_x=.25; e.scale_y=4.0; }
            candidate.optical_mapper=distortion::Mapper::create(p);
            check(!candidate.validation_error(),"SDK near/exact bounds accepted");
            roundtrips(candidate);
            DisplayComponent bounded(candidate);
            const auto corner=bounded.ComputeDistortion(vr::Eye_Right,0,1);
            if (gain>=2) check(corner.rfRed[0]<0 && corner.rfRed[1]>1,"expanded output is not clamped");
            for (auto eye : {vr::Eye_Left,vr::Eye_Right}) for (uint32_t channel=0; channel<3; ++channel) {
                // Well beyond the quantization/conditioning budget, but near
                // the actual image edge; also tests square vs enclosing disk.
                const float outside=static_cast<float>(.5+.5*gain+gain*1e-4);
                vr::HmdVector2_t buffer{{7,8}};
                check(!bounded.ComputeInverseDistortion(&buffer,eye,channel,outside,.5f),"near-edge invalid outside image rejected");
                check(buffer.v[0]==7 && buffer.v[1]==8,"near-edge rejection leaves buffer unchanged");
                check(!bounded.ComputeInverseDistortion(&buffer,eye,channel,outside,outside),"outside image corner rejected");
            }
        }
        auto inconsistent=config; inconsistent.optical_mode=OpticalMode::identity;
        check(inconsistent.validation_error()!=nullptr,"identity mode cannot silently use a profile");
        check(argc==2,"identity resource path required");
        const auto loaded=optics::load_profile(std::filesystem::path(argv[1]));
        config.optical_mapper=loaded.mapper; config.optical_profile_name=loaded.name;
        DisplayComponent identity_profile(config);
        const auto same=identity_profile.ComputeDistortion(vr::Eye_Right,.2f,.7f);
        close(same.rfRed[0],.2f,"JSON identity profile reaches SDK component");
        close(same.rfBlue[1],.7f,"JSON identity profile preserves convention");
        roundtrips(config);
        for (auto eye : {vr::Eye_Left,vr::Eye_Right}) for (uint32_t channel=0; channel<3; ++channel) {
            vr::HmdVector2_t buffer{{7,8}};
            const float just_outside=std::nextafter(1.f,2.f);
            check(!identity_profile.ComputeInverseDistortion(&buffer,eye,channel,just_outside,.5f),"identity profile rejects one whole ULP outside edge");
            check(!identity_profile.ComputeInverseDistortion(&buffer,eye,channel,just_outside,just_outside),"identity profile rejects one whole ULP outside corner");
            check(buffer.v[0]==7 && buffer.v[1]==8,"one-ULP rejection preserves buffer");
        }
        // Strict API still rejects genuinely outside targets; bad uncertainty
        // is rejected even though this API is explicit opt-in.
        const auto& mapper=*config.optical_mapper;
        check(!mapper.inverse(distortion::Eye::left,distortion::Channel::red,{1.0000001,.5}),"strict inverse not globally loosened");
        for (double error : {-1.0,std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN()})
            check(!mapper.inverse_with_uncertainty(distortion::Eye::left,distortion::Channel::red,{.7,.5},error),"invalid uncertainty rejected");
        std::cout<<"PASS optics component: "<<checks<<" checks; "<<strict_float_rejections
                 <<" float targets rejected by strict inverse recovered by SDK uncertainty; closed-grid RGB/eyes, policy, outside-domain and JSON\n";
        return 0;
    } catch(const std::exception& error) { std::cerr<<error.what()<<'\n';return 1; }
}
