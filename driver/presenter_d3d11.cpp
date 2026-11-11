// SPDX-License-Identifier: MIT
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "presenter_d3d11.hpp"
#include "presenter_backend_contract.hpp"
#include "virtual_display_lifecycle.hpp"
#include "display_selection.hpp"
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_3.h>
#include <wrl/client.h>
#include <nlohmann/json.hpp>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <sstream>
#include <thread>
#include <unordered_map>
#include <filesystem>
#include <vector>
#include <stdexcept>

namespace psvr::presenter {
using Microsoft::WRL::ComPtr;
namespace {
std::int64_t qpc() { LARGE_INTEGER v; QueryPerformanceCounter(&v); return v.QuadPart; }
std::int64_t frequency() { LARGE_INTEGER v; QueryPerformanceFrequency(&v); return v.QuadPart; }
std::string hr_text(HRESULT hr) { std::ostringstream s; s<<"0x"<<std::hex<<static_cast<std::uint32_t>(hr);return s.str(); }
void require_hr(HRESULT hr,const char* stage) { if(FAILED(hr))throw std::runtime_error(std::string(stage)+" HRESULT="+hr_text(hr)); }
bool rgba_family(DXGI_FORMAT f) {return f==DXGI_FORMAT_R8G8B8A8_UNORM||f==DXGI_FORMAT_R8G8B8A8_UNORM_SRGB||f==DXGI_FORMAT_R8G8B8A8_TYPELESS;}
}
struct D3D11Presenter::Impl {
    DisplayConfig config;
    Log log;
    bool pattern;
    const std::int64_t hz=frequency();
    RefreshTimingTrust timing;
    PresentLifecycle lifecycle{2};
    std::mutex mutex;
    std::mutex shutdown_mutex;
    std::condition_variable cv;
    std::thread worker;
    std::atomic<bool> stop{false},good{false};
    bool initialized=false;
    std::string error;
    std::uint64_t luid=0,last_wait_ordinal=0,scanout_ordinal=0,copied_ordinal=0;
    struct PendingScanout {std::uint64_t ordinal;UINT target_present;};
    std::deque<PendingScanout> pending_;
    struct Request {std::uint64_t ticket,ordinal;vr::PresentInfo_t info;};
    std::deque<Request> queue;
    std::uint64_t accepted=0,rejected=0,copies=0,presented=0,confirmed=0,timing_failures=0;
    std::int64_t last_report=0,rate_qpc=0;
    std::uint32_t rate_count=0;
    bool rate_validated=false;
    HWND window=nullptr;
    std::wstring class_name;
    ComPtr<IDXGIAdapter1> adapter;
    ComPtr<IDXGIOutput> output;
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    ComPtr<IDXGISwapChain1> chain;
    ComPtr<ID3D11Texture2D> private_frame;
    ComPtr<ID3D11Query> fence;
    struct Shared {ComPtr<ID3D11Texture2D> texture;ComPtr<IDXGIKeyedMutex> keyed;};
    std::unordered_map<std::uint64_t,Shared> imports;
    // A failed GPU completion must not be reported as source release/completion.
    ComPtr<ID3D11Texture2D> unresolved_source;
    ComPtr<IDXGIKeyedMutex> unresolved_key;
    bool unresolved_copy=false;

    Impl(const DisplayConfig& c,Log l,bool p):config(c),log(std::move(l)),pattern(p),timing(hz,{c.refresh_numerator,c.refresh_denominator}) {}
    void emit(nlohmann::json value) noexcept {try{if(log)log(value.dump());}catch(...){} }
    void fail(const std::string& text) noexcept {
        {std::lock_guard<std::mutex> lock(mutex);error=text;good=false;timing.invalidate();lifecycle.notify_backend_failed();}
        cv.notify_all();emit({{"event","presenter_failure"},{"reason",text}});
    }
    static LRESULT CALLBACK procedure(HWND w,UINT message,WPARAM wp,LPARAM lp) {
        auto* self=reinterpret_cast<Impl*>(GetWindowLongPtrW(w,GWLP_USERDATA));
        if(message==WM_NCCREATE){self=static_cast<Impl*>(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams);SetWindowLongPtrW(w,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(self));}
        if(self&&message==WM_CLOSE){self->stop=true;self->cv.notify_all();return 0;}
        if(self&&message==WM_SIZE&&wp==SIZE_MINIMIZED){self->fail("presenter window minimized");return 0;}
        return DefWindowProcW(w,message,wp,lp);
    }
    display::Display selected() {
        const auto inventory=display::enumerate_displays(false);
        if(!matches_display_config(config,inventory))throw std::runtime_error("exact Sony topology/timing no longer matches");
        for(const auto& d:inventory)if(d.monitor_device_path&&*d.monitor_device_path==config.monitor_path)return d;
        throw std::runtime_error("selected monitor unavailable");
    }
    void initialize() {
        const auto d=selected();
        BackendConfig bc{config.monitor_path,config.panel_width,config.panel_height,config.desktop_x,config.desktop_y,{config.refresh_numerator,config.refresh_denominator}};
        if(validate_config(bc,*d.monitor_device_path)!=ConfigStatus::kValid)throw std::runtime_error("invalid presenter backend config");
        if(!(d.source_adapter==d.target_adapter)||!d.source_gdi_name)throw std::runtime_error("cross-adapter output unsupported");
        luid=pack_adapter_luid(d.source_adapter.low,d.source_adapter.high);
        ComPtr<IDXGIFactory1> factory;require_hr(CreateDXGIFactory1(IID_PPV_ARGS(factory.GetAddressOf())),"CreateDXGIFactory1");
        for(UINT i=0;;++i){ComPtr<IDXGIAdapter1> a;const auto result=factory->EnumAdapters1(i,&a);if(result==DXGI_ERROR_NOT_FOUND)break;require_hr(result,"EnumAdapters1");
            DXGI_ADAPTER_DESC1 desc{};require_hr(a->GetDesc1(&desc),"adapter GetDesc1");
            if(pack_adapter_luid(desc.AdapterLuid.LowPart,desc.AdapterLuid.HighPart)==luid){adapter=a;break;}}
        if(!adapter)throw std::runtime_error("selected output adapter LUID not found");
        const auto expected=std::filesystem::u8path(*d.source_gdi_name).wstring();
        for(UINT i=0;;++i){ComPtr<IDXGIOutput> o;const auto result=adapter->EnumOutputs(i,&o);if(result==DXGI_ERROR_NOT_FOUND)break;require_hr(result,"EnumOutputs");
            DXGI_OUTPUT_DESC desc{};require_hr(o->GetDesc(&desc),"output GetDesc");
            if(desc.AttachedToDesktop&&expected==desc.DeviceName&&desc.DesktopCoordinates.left==config.desktop_x&&desc.DesktopCoordinates.top==config.desktop_y){output=o;break;}}
        if(!output)throw std::runtime_error("exact selected GDI output not found on selected LUID");
        D3D_FEATURE_LEVEL obtained;
        const D3D_FEATURE_LEVEL levels[]={D3D_FEATURE_LEVEL_11_1,D3D_FEATURE_LEVEL_11_0};
        auto result=D3D11CreateDevice(adapter.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            levels,2,D3D11_SDK_VERSION,&device,&obtained,&context);
        if(result==E_INVALIDARG)result=D3D11CreateDevice(adapter.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            levels+1,1,D3D11_SDK_VERSION,&device,&obtained,&context);
        require_hr(result,"D3D11CreateDevice");
        class_name=L"PSVRPC_Presenter_"+std::to_wstring(GetCurrentProcessId())+L"_"+std::to_wstring(reinterpret_cast<std::uintptr_t>(this));
        WNDCLASSW wc{};wc.lpfnWndProc=procedure;wc.hInstance=GetModuleHandleW(nullptr);wc.lpszClassName=class_name.c_str();wc.hCursor=LoadCursorW(nullptr,MAKEINTRESOURCEW(32512));
        if(!RegisterClassW(&wc))throw std::runtime_error("RegisterClass presenter failed");
        window=CreateWindowExW(WS_EX_TOPMOST|WS_EX_TOOLWINDOW,class_name.c_str(),L"PSVR-PC Experimental Presenter",WS_POPUP,
            config.desktop_x,config.desktop_y,static_cast<int>(config.panel_width),static_cast<int>(config.panel_height),nullptr,nullptr,wc.hInstance,this);
        if(!window)throw std::runtime_error("CreateWindow presenter failed");
        ShowWindow(window,SW_SHOWNOACTIVATE);
        RECT rect{};GetClientRect(window,&rect);if(rect.right!=1920||rect.bottom!=1080)throw std::runtime_error("physical client dimensions mismatch");
        ComPtr<IDXGIFactory2> f2;require_hr(factory.As(&f2),"IDXGIFactory2");
        DXGI_SWAP_CHAIN_DESC1 desc{};desc.Width=1920;desc.Height=1080;desc.Format=DXGI_FORMAT_R8G8B8A8_UNORM;desc.SampleDesc.Count=1;
        desc.BufferUsage=DXGI_USAGE_RENDER_TARGET_OUTPUT;desc.BufferCount=2;desc.Scaling=DXGI_SCALING_NONE;desc.SwapEffect=DXGI_SWAP_EFFECT_FLIP_DISCARD;desc.AlphaMode=DXGI_ALPHA_MODE_IGNORE;
        require_hr(f2->CreateSwapChainForHwnd(device.Get(),window,&desc,nullptr,output.Get(),&chain),"CreateSwapChainForHwnd");
        require_hr(factory->MakeWindowAssociation(window,DXGI_MWA_NO_ALT_ENTER),"MakeWindowAssociation");
        D3D11_TEXTURE2D_DESC texture{};texture.Width=1920;texture.Height=1080;texture.MipLevels=1;texture.ArraySize=1;
        texture.Format=DXGI_FORMAT_R8G8B8A8_UNORM;texture.SampleDesc.Count=1;texture.Usage=D3D11_USAGE_DEFAULT;texture.BindFlags=D3D11_BIND_RENDER_TARGET;
        require_hr(device->CreateTexture2D(&texture,nullptr,&private_frame),"private frame texture");
        D3D11_QUERY_DESC query{};query.Query=D3D11_QUERY_EVENT;require_hr(device->CreateQuery(&query,&fence),"copy completion query");
        ComPtr<ID3D11RenderTargetView> rtv;require_hr(device->CreateRenderTargetView(private_frame.Get(),nullptr,&rtv),"private RTV");
        const float black[]={0,0,0,1};context->ClearRenderTargetView(rtv.Get(),black);
        const auto startup_deadline=qpc()+hz*3;
        bool start_trusted=false;
        while(!stop&&qpc()<startup_deadline){
            present_private(vr::VSync_WaitRender,0);
            drain_pending();
            std::lock_guard<std::mutex> lock(mutex);
            start_trusted=rate_validated&&static_cast<bool>(timing.query(qpc(),.05));
            if(start_trusted)break;
        }
        if(!start_trusted)throw std::runtime_error("window/GPU initialized but Sony DXGI timing not trusted");
        {std::lock_guard<std::mutex> lock(mutex);good=true;initialized=true;}
        emit({{"event","presenter_ready"},{"adapter_luid",luid},{"geometry",{config.desktop_x,config.desktop_y,1920,1080}},
            {"refresh",{config.refresh_numerator,config.refresh_denominator}},{"borderless",true},{"exclusive_fullscreen_requested",false},
            {"test_pattern",pattern},{"timing_trusted_at_start",start_trusted}});
        cv.notify_all();
    }
    void present_private(vr::EVSync sync,std::uint64_t ordinal) {
        ComPtr<ID3D11Texture2D> back;require_hr(chain->GetBuffer(0,IID_PPV_ARGS(back.GetAddressOf())),"swapchain backbuffer");
        context->CopyResource(back.Get(),private_frame.Get());
        const auto result=chain->Present(sync==vr::VSync_None ? 0 : 1,0);
        if(result==DXGI_STATUS_OCCLUDED)throw std::runtime_error("presenter output occluded");
        require_hr(result,"DXGI Present");++presented;
        if(!ordinal)return;
        // Asynchronous scanout evidence: never block the worker on DWM
        // scanout here, or sustained throughput drops below 90Hz. The
        // pending entry is confirmed by drain_pending() below.
        UINT target=0;require_hr(chain->GetLastPresentCount(&target),"GetLastPresentCount");
        std::lock_guard<std::mutex> lock(mutex);
        pending_.push_back({ordinal,target});
        if(pending_.size()>64)throw std::runtime_error("scanout confirmations stalled: 64 unconfirmed submissions");
    }
    void drain_pending() {
        // Single statistics poll confirms every pending submission whose
        // present has reached scanout. Never synthesizes delivery.
        DXGI_FRAME_STATISTICS stats{};const auto result=chain->GetFrameStatistics(&stats);
        if(result==DXGI_ERROR_FRAME_STATISTICS_DISJOINT){std::lock_guard<std::mutex> lock(mutex);timing.invalidate();rate_qpc=0;rate_validated=false;++timing_failures;return;}
        if(FAILED(result)){std::lock_guard<std::mutex> lock(mutex);++timing_failures;return;}
        bool timing_ok=false;
        {std::lock_guard<std::mutex> lock(mutex);
            const auto observed=qpc();
            const auto status=timing.observe({stats.SyncRefreshCount,stats.SyncQPCTime.QuadPart,observed,false});
            if(status!=ObservationStatus::kBaseline&&status!=ObservationStatus::kAdvanced&&status!=ObservationStatus::kDuplicate){
                rate_qpc=0;rate_validated=false;++timing_failures;return;
            }
            const auto query=timing.query(observed,.05);
            timing_ok=static_cast<bool>(query);
            if(query){
                const auto event=lifecycle.notify_vsync_observed(lifecycle.session_token(),query.refresh_count,static_cast<double>(stats.SyncQPCTime.QuadPart)/hz);
                if(event!=VsyncEventResult::kAccepted&&event!=VsyncEventResult::kNotMonotonic)throw std::runtime_error("lifecycle vsync observation rejected");
            }
        }
        if(!rate_qpc){rate_qpc=stats.SyncQPCTime.QuadPart;rate_count=stats.SyncRefreshCount;}
        if(stats.SyncQPCTime.QuadPart-rate_qpc>=hz){
            const auto delta=stats.SyncRefreshCount-rate_count;
            const auto measured=static_cast<double>(delta)*hz/(stats.SyncQPCTime.QuadPart-rate_qpc);
            const auto nominal=config.refresh_hz();
            if(std::abs(measured-nominal)>nominal*.05)throw std::runtime_error("DXGI observed refresh does not match selected Sony: "+std::to_string(measured));
            rate_qpc=stats.SyncQPCTime.QuadPart;rate_count=stats.SyncRefreshCount;
            {std::lock_guard<std::mutex> lock(mutex);rate_validated=true;}
        }
        std::lock_guard<std::mutex> lock(mutex);
        while(!pending_.empty()){
            const auto& head=pending_.front();
            if(!(timing_ok&&static_cast<std::int32_t>(stats.PresentCount-head.target_present)>=0
                &&static_cast<std::int32_t>(stats.SyncRefreshCount-stats.PresentRefreshCount)>=0))break;
            scanout_ordinal=head.ordinal;++confirmed;pending_.pop_front();
        }
        if(!pending_.empty())cv.notify_all();
    }
    bool copy_shared(const Request& request) {
        auto found=imports.find(request.info.backbufferTextureHandle);
        if(found==imports.end()) {
            if(imports.size()>=8)imports.clear(); // worker sequential, all normal copies fenced
            Shared item;
            require_hr(device->OpenSharedResource(reinterpret_cast<HANDLE>(static_cast<std::uintptr_t>(request.info.backbufferTextureHandle)),IID_PPV_ARGS(item.texture.GetAddressOf())),"OpenSharedResource");
            require_hr(item.texture.As(&item.keyed),"shared texture keyed mutex");
            found=imports.emplace(request.info.backbufferTextureHandle,std::move(item)).first;
        }
        auto& shared=found->second;D3D11_TEXTURE2D_DESC desc{};shared.texture->GetDesc(&desc);
        if(desc.Width!=1920||desc.Height!=1080||desc.MipLevels!=1||desc.ArraySize!=1||desc.SampleDesc.Count!=1||!rgba_family(desc.Format))
            throw std::runtime_error("shared final frame dimensions/format unsupported: "+std::to_string(desc.Width)+"x"+std::to_string(desc.Height)+" format="+std::to_string(desc.Format));
        const auto acquired=shared.keyed->AcquireSync(0,20);
        if(acquired!=S_OK)throw std::runtime_error("AcquireSync key0 failed: "+hr_text(acquired));
        unresolved_source=shared.texture;unresolved_key=shared.keyed;unresolved_copy=true;
        context->CopyResource(private_frame.Get(),shared.texture.Get());context->End(fence.Get());context->Flush();
        const auto deadline=qpc()+hz;
        HRESULT completion=S_FALSE;
        while(!stop&&qpc()<deadline){completion=context->GetData(fence.Get(),nullptr,0,D3D11_ASYNC_GETDATA_DONOTFLUSH);if(completion!=S_FALSE)break;std::this_thread::sleep_for(std::chrono::milliseconds(1));}
        require_hr(completion,"source GPU completion query");
        if(completion!=S_OK)throw std::runtime_error("source GPU completion timeout; resource ownership unconfirmed");
        require_hr(shared.keyed->ReleaseSync(0),"ReleaseSync key0");unresolved_copy=false;unresolved_key.Reset();unresolved_source.Reset();++copies;
        // Copy completion (fenced GPU read finished, source mutex released)
        // is the WaitForPresent release point: the compositor must not wait
        // for DWM scanout confirmation, which costs whole vsyncs. Scanout
        // confirmation continues asynchronously in present_private for timing.
        {std::lock_guard<std::mutex> lock(mutex);lifecycle.notify_present_completed(request.ticket);lifecycle.reclaim_terminal();copied_ordinal=request.ordinal;}cv.notify_all();
        return true;
    }
    void report() {
        const auto now=qpc();if(now-last_report<hz)return;last_report=now;
        nlohmann::json value;
        {std::lock_guard<std::mutex> lock(mutex);const auto t=timing.query(qpc(),.05);
        value={{"event","presenter_metrics"},{"accepted",accepted},{"rejected",rejected},{"gpu_copies_completed",copies},
            {"dxgi_presents",presented},{"scanout_confirmed_submissions",confirmed},{"pending_scanout",pending_.size()},{"timing_failures",timing_failures},
            {"timing_trusted",static_cast<bool>(t)&&rate_validated},{"seconds_since_observed_vsync",t ? nlohmann::json(t.seconds_since_last_vsync) : nlohmann::json(nullptr)},
            {"observed_refresh_counter",t ? nlohmann::json(t.refresh_count) : nlohmann::json(nullptr)}};}
        emit(std::move(value));
    }
    void run() noexcept {
        const auto previous_dpi=SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        try {
            initialize();auto topology_check=qpc();std::uint64_t pattern_frame=0;
            while(!stop&&good){
                MSG message;while(PeekMessageW(&message,nullptr,0,0,PM_REMOVE)){TranslateMessage(&message);DispatchMessageW(&message);}
                if(!IsWindowVisible(window)||IsIconic(window))throw std::runtime_error("presenter window unavailable");
                if(qpc()-topology_check>=hz){selected();topology_check=qpc();ComPtr<IDXGIOutput> actual;require_hr(chain->GetContainingOutput(&actual),"GetContainingOutput");DXGI_OUTPUT_DESC a{},b{};require_hr(actual->GetDesc(&a),"containing output descriptor");require_hr(output->GetDesc(&b),"selected output descriptor");if(a.Monitor!=b.Monitor)throw std::runtime_error("swapchain migrated off selected monitor");}
                Request request{};bool have=false;
                {std::lock_guard<std::mutex> lock(mutex);if(!queue.empty()){request=queue.front();queue.pop_front();have=true;}}
                if(have){copy_shared(request);present_private(request.info.vsync,request.ordinal);}
                else {
                    // No new compositor frame: do not consume vsync slots with
                    // idle presents. Poll timing for trust upkeep and service
                    // the window message queue; DWM keeps the last frame.
                    if(pattern){ComPtr<ID3D11RenderTargetView> rtv;require_hr(device->CreateRenderTargetView(private_frame.Get(),nullptr,&rtv),"pattern RTV");
                        const float color[]={.015f+float(pattern_frame%90)/9000.f,.025f,.055f,1.f};context->ClearRenderTargetView(rtv.Get(),color);++pattern_frame;
                        present_private(vr::VSync_WaitRender,0);}
                    else std::this_thread::sleep_for(std::chrono::milliseconds(1));
                }
                drain_pending();
                report();
            }
        }catch(const std::exception& e){
            // Policy: every DXGI failure is terminal by choice (permanent
            // fail-closed, restart SteamVR). In-place device/swapchain
            // re-initialization is a future architectural decision, not
            // implemented here: no retry, no ResizeBuffers, no re-creation.
            std::string reason=e.what();
            if(device.Get()!=nullptr){
                // Best-effort diagnostic only: a failed read leaves the
                // original reason unchanged.
                try{
                    const HRESULT removed=device->GetDeviceRemovedReason();
                    if(FAILED(removed))reason+=" device_removed_reason="+hr_text(removed);
                }catch(...){}
            }
            fail(reason);
        }
        {std::lock_guard<std::mutex> lock(mutex);good=false;timing.invalidate();lifecycle.disconnect();initialized=true;}cv.notify_all();
        if(unresolved_copy){
            // Keep polling actual completion on shutdown. Never label abandoned
            // metadata completed just because shutdown was requested.
            const auto deadline=qpc()+hz*2;HRESULT done=S_FALSE;
            while(qpc()<deadline){done=context->GetData(fence.Get(),nullptr,0,0);if(done!=S_FALSE)break;std::this_thread::sleep_for(std::chrono::milliseconds(1));}
            if(done==S_OK||FAILED(device->GetDeviceRemovedReason())){
                const auto release=unresolved_key->ReleaseSync(0);
                if(release==S_OK||FAILED(device->GetDeviceRemovedReason()))unresolved_copy=false;
                else emit({{"event","presenter_resource_quarantine"},{"reason","source mutex release failed"},{"hresult",hr_text(release)}});
            }
            else emit({{"event","presenter_resource_quarantine"},{"reason","GPU source read completion unknown; retained until process exit"}});
        }
        if(window){DestroyWindow(window);window=nullptr;}if(!class_name.empty())UnregisterClassW(class_name.c_str(),GetModuleHandleW(nullptr));
        if(previous_dpi)SetThreadDpiAwarenessContext(previous_dpi);
    }
};
D3D11Presenter::D3D11Presenter(const DisplayConfig& config,Log log,bool pattern):impl_(std::make_unique<Impl>(config,std::move(log),pattern)) {
    impl_->worker=std::thread([this]{impl_->run();});
    std::unique_lock<std::mutex> lock(impl_->mutex);
    if(!impl_->cv.wait_for(lock,std::chrono::seconds(10),[&]{return impl_->initialized||!impl_->error.empty();})||!impl_->good){
        const auto reason=impl_->error.empty()?"presenter initialization timeout":impl_->error;lock.unlock();dispose();throw std::runtime_error(reason);
    }
}
D3D11Presenter::~D3D11Presenter(){dispose();}
void D3D11Presenter::dispose() noexcept {shutdown();if(impl_&&impl_->unresolved_copy){
    // Deliberate fail-closed quarantine, not a successful resource release.
    // Process teardown owns final cancellation if a GPU never acknowledges.
    static std::mutex m;static auto* quarantine=new std::vector<std::unique_ptr<Impl>>;
    try{std::lock_guard<std::mutex> lock(m);quarantine->push_back(std::move(impl_));}catch(...){impl_.release();}
}}
void D3D11Presenter::shutdown() noexcept {
    if(!impl_)return;std::lock_guard<std::mutex> owner(impl_->shutdown_mutex);
    {std::lock_guard<std::mutex> lock(impl_->mutex);impl_->stop=true;impl_->good=false;impl_->timing.invalidate();impl_->lifecycle.disconnect();impl_->last_wait_ordinal=0;}
    impl_->cv.notify_all();if(impl_->worker.joinable())impl_->worker.join();
}
bool D3D11Presenter::submit(const void* bytes,std::uint32_t size) noexcept {
    if(!impl_)return false;
    try{std::lock_guard<std::mutex> lock(impl_->mutex);impl_->last_wait_ordinal=0;
        if(!impl_->good||impl_->stop){++impl_->rejected;return false;}
        std::uint64_t ticket=0;const auto result=impl_->lifecycle.submit(bytes,size,&ticket);
        if(result!=PresentSubmitResult::kAccepted){++impl_->rejected;return false;}
        vr::PresentInfo_t info{};copy_present_info(info,bytes,size);const auto ordinal=++impl_->accepted;
        impl_->queue.push_back({ticket,ordinal,info});impl_->last_wait_ordinal=ordinal;impl_->cv.notify_all();return true;
    }catch(...){impl_->fail("Present enqueue failed");return false;}
}
void D3D11Presenter::wait_for_present() noexcept {
    if(!impl_)return;
    // Fast release on GPU copy completion (source read finished), not on
    // DWM scanout confirmation: waiting full scanouts halved the compositor
    // to 45fps at 90Hz. Scanout evidence still feeds timing asynchronously.
    std::unique_lock<std::mutex> lock(impl_->mutex);const auto target=impl_->last_wait_ordinal;if(!target)return;
    if(!impl_->cv.wait_for(lock,std::chrono::milliseconds(500),[&]{return impl_->stop||!impl_->good||impl_->copied_ordinal>=target;})){
        lock.unlock();impl_->fail("WaitForPresent GPU copy completion timeout");
    }
}
bool D3D11Presenter::time_since_vsync(float* seconds,std::uint64_t* count) noexcept {
    if(!impl_||!seconds||!count)return false;
    std::lock_guard<std::mutex> lock(impl_->mutex);if(!impl_->good||impl_->stop||!impl_->rate_validated)return false;
    const auto result=impl_->timing.query(qpc(),.05);if(!result)return false;
    *seconds=static_cast<float>(result.seconds_since_last_vsync);*count=result.refresh_count;return true;
}
std::uint64_t D3D11Presenter::adapter_luid() const noexcept{return impl_?impl_->luid:0;}
bool D3D11Presenter::healthy() const noexcept{return impl_&&impl_->good;}
} // namespace psvr::presenter
