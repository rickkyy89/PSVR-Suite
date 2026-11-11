// SPDX-License-Identifier: MIT
// Production bodies from the patched pin; libusb/queue/manager API doubles.
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#define LIBUSB_CALL
#define debug(...) ((void)0)
constexpr int NUM_TRANSFERS=2;
enum libusb_transfer_status { LIBUSB_TRANSFER_COMPLETED, LIBUSB_TRANSFER_CANCELLED, LIBUSB_TRANSFER_ERROR };
struct libusb_transfer {
    void* user_data=nullptr;
    libusb_transfer_status status=LIBUSB_TRANSFER_COMPLETED;
    unsigned char* buffer=nullptr;
    int actual_length=0;
};
int frees=0, cancels=0, submits=0, submit_result=0;
bool in_callback=false;
int libusb_submit_transfer(libusb_transfer*) { ++submits; return submit_result; }
int libusb_cancel_transfer(libusb_transfer*) { ++cancels; return -5; }
void libusb_free_transfer(libusb_transfer*) { ++frees; }
struct FrameQueue { void Abort() {} };
struct USBMgr {
    static USBMgr* instance() { static USBMgr mgr; return &mgr; }
    void cameraStopped() {}
};
struct URBDesc {
    std::mutex num_active_transfers_mutex;
    std::condition_variable num_active_transfers_condition;
    int num_active_transfers=1, late_uses=0;
    libusb_transfer* xfr[NUM_TRANSFERS]{};
    bool transfers_started=true, transfers_stopping=false;
    unsigned char* transfer_buffer=nullptr;
    std::shared_ptr<FrameQueue> queue_=std::make_shared<FrameQueue>();
    std::shared_ptr<FrameQueue> getFrameQueue() {
        if(in_callback && num_active_transfers==0) ++late_uses;
        return queue_;
    }
    void setFrameQueue(std::shared_ptr<FrameQueue> value) { queue_=std::move(value); }
    void pkt_scan(unsigned char*,int) {}
#include "transfer_methods.inc"
};
#include "transfer_callback.inc"
void check(bool ok,const char* text) { if(!ok) throw std::runtime_error(text); }
void callback(libusb_transfer& t) { in_callback=true; transfer_completed_callback(&t); in_callback=false; }
int main() {
    try {
        unsigned cases=0;
        for(auto status : {LIBUSB_TRANSFER_COMPLETED,LIBUSB_TRANSFER_ERROR,LIBUSB_TRANSFER_CANCELLED}) {
            for(auto result : {0,-4}) {
                frees=cancels=submits=0; submit_result=result;
                URBDesc u; libusb_transfer t; t.user_data=&u; t.status=status; u.xfr[0]=&t;
                callback(t);
                const bool retained=status==LIBUSB_TRANSFER_COMPLETED && result==0;
                check(u.num_active_transfers==(retained?1:0),"unbalanced active count");
                check(frees==(retained?0:1) && (u.xfr[0]!=nullptr)==retained,"free/slot ownership");
                check(u.late_uses==0,"callback used owner after final decrement");
                if(retained) { t.status=LIBUSB_TRANSFER_CANCELLED; callback(t); }
                u.close_transfers();
                check(u.num_active_transfers==0 && frees==1,"shutdown cleanup");
                ++cases;
            }
        }
        {
            frees=cancels=submits=0; submit_result=-4;
            URBDesc u; u.num_active_transfers=2;
            libusb_transfer a,b; a.user_data=b.user_data=&u; u.xfr[0]=&a; u.xfr[1]=&b;
            callback(a);
            check(u.num_active_transfers==1 && frees==1 && cancels==1,"failed resubmit must cancel only other slots");
            b.status=LIBUSB_TRANSFER_CANCELLED; callback(b); u.close_transfers();
            check(u.num_active_transfers==0 && frees==2 && u.late_uses==0,"last callback lifetime"); ++cases;
        }
        {
            frees=cancels=submits=0; submit_result=0;
            URBDesc u; libusb_transfer t; t.user_data=&u; u.xfr[0]=&t;
            std::thread closer([&] { u.close_transfers(); });
            for(;;) {
                bool stopping;
                { std::lock_guard<std::mutex> lock(u.num_active_transfers_mutex); stopping=u.transfers_stopping; }
                if(stopping) break;
                std::this_thread::yield();
            }
            callback(t); closer.join();
            check(submits==0 && frees==1 && u.num_active_transfers==0,"shutdown must prevent resubmit after cancellation sweep"); ++cases;
        }
        std::cout<<"PASS native transfer cases="<<cases<<'\n';
        return 0;
    } catch(const std::exception& e) { std::cerr<<"FAIL "<<e.what()<<'\n'; return 1; }
}
