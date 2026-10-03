// SPDX-License-Identifier: MIT
#pragma once
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace psvr {
constexpr double pi = 3.14159265358979323846;
constexpr double degrees = 180.0 / pi;
struct Vec3 {
    double x = 0, y = 0, z = 0;
    Vec3 operator+(Vec3 b) const { return {x+b.x,y+b.y,z+b.z}; }
    Vec3 operator-(Vec3 b) const { return {x-b.x,y-b.y,z-b.z}; }
    Vec3 operator*(double k) const { return {x*k,y*k,z*k}; }
    double dot(Vec3 b) const { return x*b.x+y*b.y+z*b.z; }
    Vec3 cross(Vec3 b) const { return {y*b.z-z*b.y,z*b.x-x*b.z,x*b.y-y*b.x}; }
    double norm() const { return std::sqrt(dot(*this)); }
};
struct Quaternion {
    double w = 1, x = 0, y = 0, z = 0;
    double norm() const { return std::sqrt(w*w+x*x+y*y+z*z); }
    Quaternion normalized() const {
        const auto n = norm();
        if (!std::isfinite(n) || n < 1e-12) throw std::runtime_error("nonfinite/zero quaternion");
        return {w/n,x/n,y/n,z/n};
    }
    Quaternion conjugate() const { return {w,-x,-y,-z}; }
    Quaternion operator*(Quaternion b) const {
        return {w*b.w-x*b.x-y*b.y-z*b.z, w*b.x+x*b.w+y*b.z-z*b.y,
                w*b.y-x*b.z+y*b.w+z*b.x, w*b.z+x*b.y-y*b.x+z*b.w};
    }
    Vec3 rotate(Vec3 v) const {
        const auto p = (*this) * Quaternion{0,v.x,v.y,v.z} * conjugate();
        return {p.x,p.y,p.z};
    }
};
inline Quaternion rotation_vector(Vec3 vector) {
    const auto angle = vector.norm();
    if (angle < 1e-12) return {};
    const auto s = std::sin(angle/2)/angle;
    return {std::cos(angle/2),vector.x*s,vector.y*s,vector.z*s};
}
inline Quaternion align_up(Vec3 measured) {
    const auto n = measured.norm();
    if (n < 1e-12) throw std::runtime_error("no gravity vector");
    measured = measured*(1/n);
    const Vec3 up{0,1,0};
    const auto d = std::clamp(measured.dot(up),-1.0,1.0);
    if (d < -0.999999) return {0,1,0,0};
    const auto axis = measured.cross(up);
    return Quaternion{1+d,axis.x,axis.y,axis.z}.normalized();
}
struct Angles { double yaw, pitch, roll; };
// Y-X-Z convention: q = Ry(yaw)*Rx(pitch)*Rz(roll), right-handed Y-up.
inline Angles angles(Quaternion q) {
    q = q.normalized();
    return {std::atan2(2*(q.x*q.z+q.w*q.y),1-2*(q.x*q.x+q.y*q.y))*degrees,
            std::asin(std::clamp(2*(q.w*q.x-q.y*q.z),-1.0,1.0))*degrees,
            std::atan2(2*(q.x*q.y+q.w*q.z),1-2*(q.x*q.x+q.z*q.z))*degrees};
}
inline double angle_difference(double a, double b) { return std::remainder(a-b,360.0); }
inline double rotation_distance(Quaternion a, Quaternion b) {
    const auto q = (a.conjugate()*b).normalized();
    return 2*std::atan2(std::sqrt(q.x*q.x+q.y*q.y+q.z*q.z),std::abs(q.w))*degrees;
}
inline Quaternion blend(Quaternion a, Quaternion b, double weight) {
    if (a.w*b.w+a.x*b.x+a.y*b.y+a.z*b.z < 0) b = {-b.w,-b.x,-b.y,-b.z};
    return Quaternion{a.w+(b.w-a.w)*weight,a.x+(b.x-a.x)*weight,
                      a.y+(b.y-a.y)*weight,a.z+(b.z-a.z)*weight}.normalized();
}
struct VectorStats {
    unsigned long long count = 0;
    Vec3 mean{}, m2{};
    void add(Vec3 v) {
        ++count;
        const auto delta = v-mean;
        mean = mean+delta*(1.0/static_cast<double>(count));
        const auto next = v-mean;
        m2 = m2+Vec3{delta.x*next.x,delta.y*next.y,delta.z*next.z};
    }
    Vec3 deviation() const {
        if (count < 2) return {};
        const auto d = static_cast<double>(count-1);
        return {std::sqrt(std::max(0.0,m2.x/d)),std::sqrt(std::max(0.0,m2.y/d)),std::sqrt(std::max(0.0,m2.z/d))};
    }
};
inline double max_axis(Vec3 v) { return std::max({std::abs(v.x),std::abs(v.y),std::abs(v.z)}); }
} // namespace psvr
