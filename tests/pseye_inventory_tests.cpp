// SPDX-License-Identifier: MIT
// Real context/open/count/close bodies with a device-enumeration double.
#include <cstdint>
#include <iostream>
#include <list>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <vector>
namespace ps3eye {
struct PS3EYECam {
    using PS3EYERef=std::shared_ptr<PS3EYECam>;
    enum EOutputFormat { BGR=1 };
    static std::vector<PS3EYERef> physical, cached;
    static int refreshes;
    static const std::vector<PS3EYERef>& getDevices(bool force=false) {
        if(force) { cached=physical; ++refreshes; }
        return cached;
    }
    bool init(int,int,uint8_t,EOutputFormat) { return true; }
    void start() {}
    void stop() {}
};
std::vector<PS3EYECam::PS3EYERef> PS3EYECam::physical,PS3EYECam::cached;
int PS3EYECam::refreshes=0;
}
enum ps3eye_format { FORMAT_BGR=1 };
struct ps3eye_t;
#include "inventory_context.inc"
static ps3eye_context_t* ps3eye_context=nullptr;
static std::mutex ps3eye_context_mutex;
#include "inventory_camera.inc"
#include "inventory_calls.inc"
void check(bool ok,const char* text) { if(!ok) throw std::runtime_error(text); }
int main() {
    using Cam=ps3eye::PS3EYECam;
    try {
        ps3eye_init(); check(ps3eye_count_connected()==0,"empty startup");
        auto a=std::make_shared<Cam>(),b=std::make_shared<Cam>();
        Cam::physical={a,b}; check(ps3eye_count_connected()==2,"0->2 plug refresh");
        auto* open=ps3eye_open(0,640,480,75,FORMAT_BGR);
        check(open && open->eye==a,"selected device identity");
        const auto refreshes=Cam::refreshes;
        Cam::physical={b};
        check(ps3eye_count_connected()==2 && Cam::refreshes==refreshes,"inventory must freeze live indices");
        check(open->eye==a,"hotplug changed active camera");
        ps3eye_close(open);
        check(ps3eye_count_connected()==1,"idle 2->1 refresh");
        auto* reopened=ps3eye_open(0,640,480,75,FORMAT_BGR);
        check(reopened && reopened->eye==b,"idle reopen must not retain removed device");
        ps3eye_uninit(); check(ps3eye_context!=nullptr,"uninit must preserve active handles");
        ps3eye_close(reopened);
        Cam::physical.clear(); check(ps3eye_count_connected()==0,"1->0 unplug refresh");
        check(ps3eye_open(0,640,480,75,FORMAT_BGR)==nullptr,"absent index must not open cached camera");
        Cam::physical={a};
        reopened=ps3eye_open(0,640,480,75,FORMAT_BGR);
        check(reopened && reopened->eye==a,"open alone must refresh after plug");
        ps3eye_close(reopened); ps3eye_uninit();
        check(ps3eye_context==nullptr,"idle uninit cleanup");
        std::cout<<"PASS native inventory transitions, frozen live identity, idle reopen, uninit ownership\n";
        return 0;
    } catch(const std::exception& e) { std::cerr<<"FAIL "<<e.what()<<'\n'; return 1; }
}
