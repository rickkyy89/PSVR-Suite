// SPDX-License-Identifier: MIT
// Original hardware-free synthetic loader tests; no calibration claims.
#include "optical_profile.hpp"
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <type_traits>

namespace {
using namespace psvr::optics;
using Json = nlohmann::json;
unsigned checks = 0;
void check(bool condition, const std::string& message) {
    ++checks;
    if (!condition) throw std::runtime_error(message);
}
template<class Function>
void rejects(Function function, const std::string& message, const std::string& diagnostic = {}) {
    try { function(); }
    catch (const std::runtime_error& e) {
        check(std::string(e.what()).find("optical profile:") != std::string::npos, message + " useful error");
        check(diagnostic.empty() || std::string(e.what()).find(diagnostic) != std::string::npos, message + " diagnostic");
        return;
    } catch (const std::invalid_argument& e) {
        check(std::string(e.what()).find("optical profile:") != std::string::npos, message + " useful error");
        check(diagnostic.empty() || std::string(e.what()).find(diagnostic) != std::string::npos, message + " diagnostic");
        return;
    }
    check(false, message + " accepted invalid input");
}
Json identity() {
    const Json eye = {{"center", {0.5, 0.5}}, {"axis_scale", {1, 1}},
                      {"polynomial", {{"a", 0}, {"b", 0}, {"c", 0}, {"d", 1}}},
                      {"rgb_scale", {{"red", 1}, {"green", 1}, {"blue", 1}}}};
    return {{"schema_version", 1}, {"name", "Synthetic identity unverified"},
            {"uv_convention", "eye-local-top-left"}, {"mapping", "display-to-render"},
            {"eyes", {{"left", eye}, {"right", eye}}}};
}
void reject_json(const Json& value, const std::string& message) {
    rejects([&] { (void)parse_profile(value.dump()); }, message);
}
void valid_profiles() {
    static_assert(std::is_copy_constructible<LoadedProfile>::value, "parent can copy snapshots");
    static_assert(!std::is_default_constructible<LoadedProfile>::value, "no unvalidated mapper");
    static_assert(std::is_const<decltype(LoadedProfile::profile)>::value, "immutable profile");
    static_assert(std::is_const<decltype(LoadedProfile::mapper)>::value, "immutable mapper");
    auto json = identity();
    const auto loaded = parse_profile(json.dump());
    const LoadedProfile copy = loaded;
    check(copy.name == "Synthetic identity unverified", "name copied");
    for (auto eye : {psvr::distortion::Eye::left, psvr::distortion::Eye::right}) {
        for (int y = 0; y <= 10; ++y) for (int x = 0; x <= 10; ++x) {
            const psvr::distortion::UV uv{x / 10.0, y / 10.0};
            const auto rgb = copy.mapper.forward_rgb(eye, uv);
            check(rgb.has_value(), "identity RGB available");
            for (const auto& channel : *rgb)
                check(std::abs(channel.u - uv.u) < 1e-14 && std::abs(channel.v - uv.v) < 1e-14,
                      "identity grid");
        }
    }
    json["eyes"]["left"]["center"] = {0.31, 0.63};
    json["eyes"]["left"]["axis_scale"] = {1.7, 0.8};
    json["eyes"]["left"]["polynomial"] = {{"a", .025}, {"b", .05}, {"c", -.02}, {"d", .95}};
    json["eyes"]["left"]["rgb_scale"] = {{"red", 1.03}, {"green", 1.0}, {"blue", .97}};
    const auto asymmetric = parse_profile(json.dump());
    const auto& p = asymmetric.profile.eyes[0];
    check(p.center.u == .31 && p.center.v == .63 && p.scale_x == 1.7 && p.scale_y == .8,
          "center and axis order");
    check(p.a == .025 && p.b == .05 && p.c == -.02 && p.d == .95 &&
          p.rgb[0] == 1.03 && p.rgb[1] == 1 && p.rgb[2] == .97, "coefficient and RGB order");
    check(asymmetric.profile.eyes[1].center.u == .5 && asymmetric.profile.eyes[1].d == 1,
          "eyes are independent");
    const auto mapped = asymmetric.mapper.forward_rgb(psvr::distortion::Eye::left, {.91, .13});
    const double r = std::hypot((.91-.31)*1.7, (.13-.63)*.8);
    const double gain = .95-.02*r+.05*r*r+.025*r*r*r;
    check(mapped && std::abs((*mapped)[0].u - (.31+(.91-.31)*gain*1.03)) < 1e-14,
          "forward display-to-render not inverse");
    json["eyes"]["left"]["polynomial"] = {{"a", 0}, {"b", 0}, {"c", 0}, {"d", 2}};
    const auto expanded = parse_profile(json.dump()).mapper.forward_rgb(psvr::distortion::Eye::left, {1, 0});
    check(expanded && (*expanded)[0].u > 1 && (*expanded)[0].v < 0, "no sampling UV clamp");
    for (const std::string& name : {std::string(1, 'a'), std::string(128, 'a'), std::string(u8"Synthetic \u00e9 \u6e2c\u8a66")}) {
        json["name"] = name;
        check(parse_profile(json.dump()).name == name, "valid name boundaries and Unicode");
    }
    std::string unicode_name;
    for (int i = 0; i < 128; ++i) unicode_name += u8"\u00e9";
    json["name"] = unicode_name;
    check(parse_profile(json.dump()).name == unicode_name, "name length counts Unicode characters");
}
void strict_schema() {
    const auto base = identity();
    // Every object level: missing, unknown, wrong container type and duplicate
    // decoded keys. JSON pointers keep these tests independent of map ordering.
    for (const std::string pointer : {"", "/eyes", "/eyes/left", "/eyes/right",
                                      "/eyes/left/polynomial", "/eyes/right/polynomial",
                                      "/eyes/left/rgb_scale", "/eyes/right/rgb_scale"}) {
        const Json::json_pointer path(pointer);
        const auto object = base.at(path);
        for (auto it = object.begin(); it != object.end(); ++it) {
            auto modified = base; modified.at(path).erase(it.key());
            reject_json(modified, "missing " + pointer + "/" + it.key());
            // Replacing an object member in its serialized object with a pair
            // preserves surrounding valid schema while testing parser behavior.
            const auto original = object.dump();
            const auto duplicate = "{" + Json(it.key()).dump() + ":null," + original.substr(1);
            auto text = base.dump();
            text.replace(text.find(original), original.size(), duplicate);
            rejects([&] { (void)parse_profile(text); }, "duplicate " + pointer + "/" + it.key(), "duplicate key");
        }
        auto modified = base; modified.at(path)["unexpected"] = 0;
        reject_json(modified, "unknown key " + pointer);
        for (const Json& wrong : {Json(nullptr), Json::array(), Json(1), Json("object"), Json(true)}) {
            modified = base; modified.at(path) = wrong;
            reject_json(modified, "object type " + pointer);
        }
    }
    for (const Json& wrong : {Json(nullptr), Json(true), Json("1"), Json(1.0), Json(0), Json(2), Json(-1), Json::array()}) {
        auto j = base; j["schema_version"] = wrong; reject_json(j, "schema version type/value");
    }
    for (const std::string key : {"name", "uv_convention", "mapping"}) {
        for (const Json& wrong : {Json(nullptr), Json(1), Json(true), Json::array(), Json::object()}) {
            auto j = base; j[key] = wrong; reject_json(j, "string type " + key);
        }
    }
    for (const std::string key : {"uv_convention", "mapping"}) {
        auto j = base; j[key] = "unsupported"; reject_json(j, "unsupported " + key);
    }
    for (const std::string& name : {std::string(), std::string(129, 'x'), std::string("a\n"),
                                  std::string("a\0b", 3), std::string(1, '\x7f'), std::string(u8"\u0085")}) {
        auto j = base; j["name"] = name; reject_json(j, "invalid name");
    }
    for (const std::string eye : {"left", "right"}) {
        for (const std::string field : {"center", "axis_scale"}) {
            for (const Json& wrong : {Json(nullptr), Json::object(), Json(1), Json::array(),
                                     Json::array({1}), Json::array({1, 1, 1}), Json::array({true, 1}),
                                     Json::array({1, "1"})}) {
                auto j = base; j["eyes"][eye][field] = wrong; reject_json(j, "array shape/types");
            }
        }
        for (const std::string object : {"polynomial", "rgb_scale"}) {
            for (auto it = base["eyes"][eye][object].begin(); it != base["eyes"][eye][object].end(); ++it) {
                for (const Json& wrong : {Json(nullptr), Json(true), Json("1"), Json::array(), Json::object()}) {
                    auto j = base; j["eyes"][eye][object][it.key()] = wrong; reject_json(j, "numeric type");
                }
            }
        }
        for (const Json& bad : {Json::array({-.01, .5}), Json::array({.5, 1.01})}) {
            auto j = base; j["eyes"][eye]["center"] = bad; reject_json(j, "center bounds");
        }
        for (double bad : {0.0, -1.0, 1e-4, 101.0}) {
            auto j = base; j["eyes"][eye]["axis_scale"] = {bad, 1}; reject_json(j, "scale bounds");
            j = base; j["eyes"][eye]["rgb_scale"]["blue"] = bad; reject_json(j, "RGB bounds");
        }
        auto j = base; j["eyes"][eye]["polynomial"]["a"] = 101; reject_json(j, "coefficient bound");
        j = base; j["eyes"][eye]["polynomial"]["d"] = 0; reject_json(j, "singular center");
        j = base; j["eyes"][eye]["polynomial"] = {{"a", 0}, {"b", 1.0/3.0}, {"c", -.351}, {"d", .351*.351-1e-6}};
        reject_json(j, "narrow interior fold despite positive endpoints");
    }
}
void invalid_text_and_limits() {
    const auto valid = identity().dump();
    for (const std::string& text : {std::string(), std::string("{"), valid.substr(0, valid.size()-1),
                                  valid + " {}", valid + std::string(1, '\0'), std::string("/*comment*/") + valid,
                                  std::string("{\"name\":\"\\uD800\"}"), std::string("{\"name\":\"") + '\xff' + "\"}",
                                  std::string("{\"x\":NaN}"), std::string("{\"x\":Infinity}"), std::string("{\"x\":1e9999}")})
        rejects([&] { (void)parse_profile(text); }, "invalid JSON text");
    auto text = valid;
    const auto position = text.find("\"schema_version\":1");
    text.replace(position, std::string("\"schema_version\":1").size(), "\"schema_version\":1,\"schema_versio\\u006e\":1");
    rejects([&] { (void)parse_profile(text); }, "escaped duplicate key", "duplicate key");
    for (const std::string token : {"NaN", "Infinity", "-Infinity", "1e9999"}) {
        text = valid;
        const auto d = text.find("\"d\":1");
        text.replace(d, 5, "\"d\":" + token);
        rejects([&] { (void)parse_profile(text); }, "nonfinite coefficient");
    }
    text = valid + std::string(max_profile_bytes-valid.size(), ' ');
    check(parse_profile(text).name == identity()["name"], "exact byte limit allowed");
    text += ' ';
    rejects([&] { (void)parse_profile(text); }, "byte limit", "65536");
    const auto nested = [](unsigned depth) { return std::string(depth, '[') + "0" + std::string(depth, ']'); };
    rejects([&] { (void)parse_profile(nested(16)); }, "depth 16 parsed but wrong root", "root must be an object");
    rejects([&] { (void)parse_profile(nested(17)); }, "depth 17", "depth 16");
    rejects([&] { (void)parse_profile(nested(1000)); }, "extreme depth", "depth 16");
}
struct TemporaryDirectory {
    std::filesystem::path path;
    TemporaryDirectory() {
        const auto parent = std::filesystem::temp_directory_path();
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        for (unsigned i = 0; i < 100; ++i) {
            path = parent / ("psvr-optical-tests-" + std::to_string(stamp) + "-" + std::to_string(i));
            if (std::filesystem::create_directory(path)) return;
        }
        throw std::runtime_error("cannot create temporary test directory");
    }
    ~TemporaryDirectory() { std::error_code error; std::filesystem::remove_all(path, error); }
};
void write_file(const std::filesystem::path& path, const std::string& text) {
    std::ofstream output(path, std::ios::binary);
    output.write(text.data(), static_cast<std::streamsize>(text.size()));
    output.close();
    check(!output.fail(), "test fixture write");
}
void file_tests(const std::filesystem::path& fixture) {
    TemporaryDirectory temp;
    const auto file = temp.path / "profile.json";
    const auto valid = identity().dump();
    write_file(file, valid);
    check(load_profile(file).name == identity()["name"], "binary file load");
    write_file(file, valid + std::string(max_profile_bytes-valid.size(), ' '));
    check(load_profile(file).profile.eyes[1].d == 1, "exact file limit");
    write_file(file, valid + std::string(max_profile_bytes+1-valid.size(), ' '));
    rejects([&] { (void)load_profile(file); }, "oversized file", "65536");
    write_file(file, std::string(1024*1024, ' '));
    rejects([&] { (void)load_profile(file); }, "large file bounded read", "65536");
    write_file(file, ""); rejects([&] { (void)load_profile(file); }, "empty file");
    write_file(file, valid.substr(0, valid.size()-1));
    rejects([&] { (void)load_profile(file); }, "truncated file");
    rejects([&] { (void)load_profile(temp.path / "missing.json"); }, "missing file", "cannot open");
    rejects([&] { (void)load_profile(temp.path); }, "directory not profile");
    const auto unicode = temp.path / std::filesystem::u8path(u8"profil-\u00e9-\u6e2c\u8a66.json");
    write_file(unicode, valid);
    check(load_profile(unicode).name == identity()["name"], "native Unicode filename load");
    const auto resource = load_profile(fixture);
    check(resource.name.find("unverified") != std::string::npos, "resource explicitly unverified");
    for (const auto& eye : resource.profile.eyes)
        check(eye.center.u == .5 && eye.center.v == .5 && eye.scale_x == 1 && eye.scale_y == 1 &&
              eye.a == 0 && eye.b == 0 && eye.c == 0 && eye.d == 1 &&
              eye.rgb == std::array<double, 3>{{1, 1, 1}}, "explicit diagnostic resource identity");
}
} // namespace

int main(int argc, char** argv) {
    try {
        valid_profiles(); strict_schema(); invalid_text_and_limits();
        file_tests(argc > 1 ? std::filesystem::path(argv[1]) :
                   std::filesystem::path("driver/package/resources/profiles/identity_unverified.json"));
        std::cout << "PASS optical profile hardware-free tests: " << checks << " checks\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& e) {
        std::cerr << "FAIL after " << checks << " checks: " << e.what() << '\n';
        return EXIT_FAILURE;
    }
}
