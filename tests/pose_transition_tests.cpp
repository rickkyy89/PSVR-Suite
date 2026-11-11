// SPDX-License-Identifier: MIT
#include "pose_transition_trace.hpp"
#include <iostream>
#include <stdexcept>

void check(bool value,const char* message) { if (!value) throw std::runtime_error(message); }
int main() {
    try {
        using namespace psvr;
        const DriverClock::time_point start{};
        TrackingSnapshot s; s.connected=true; s.state=ImuState::tracking; s.has_sample=true;
        s.orientation={}; s.sampled_at=start;
        const auto good=driver_pose(s,start);
        PoseTransitionTrace trace;
        trace.observe(s,good,start,start,start,100,true);
        trace.observe(s,good,start,start,start,100,true);
        std::array<PoseTransition,128> events;
        check(trace.drain(events)==1 && events[0].reason==PoseReason::valid,"unchanged states are not logged per frame");
        s.sampled_at=start+std::chrono::microseconds(50);
        const auto future=driver_pose(s,start);
        trace.observe(s,future,start,start+std::chrono::microseconds(100),start,100,true);
        check(trace.drain(events)==1 && events[0].reason==PoseReason::future_sample && events[0].sample_age_ms<0
              && events[0].sample_age_after_copy_ms>0,"query/copy timing race evidence is preserved without changing the pose");
        const auto after_copy=start+std::chrono::microseconds(100);
        check(driver_pose(s,after_copy).poseIsValid,"sampling time after snapshot copy prevents the false-future rejection");
        check(!driver_pose(s,start).poseIsValid,"genuinely future query remains rejected: no tolerance/clamping workaround");
        s.sampled_at=start;
        const auto late=start+std::chrono::milliseconds(101);
        check(pose_reason(s,late,100,true,false)==PoseReason::stale_sample,"staleness distinct from reader failure");
        check(pose_reason(s,start,100,false,false)==PoseReason::display_invalid,"display invalidation takes precedence");
        trace.reset();
        for (int i=0;i<132;++i) trace.observe(s,i%2 ? future : good,start,start,start,100,true);
        check(trace.drain(events)==128 && trace.dropped()==4,"bounded trace reports overflow rather than blocking or allocating");
        trace.reset();
        check(trace.drain(events)==0 && trace.dropped()==0,"reset clears pending events and counters");
        s.orientation.w=2;
        check(pose_reason(s,start,100,true,false)==PoseReason::nonunit_orientation,"bad rotation reason");
        std::cout << "PASS pose validity transitions, timing-race evidence, bounded buffer and reset\n";
    } catch(const std::exception& e) { std::cerr<<e.what()<<'\n'; return 1; }
}
