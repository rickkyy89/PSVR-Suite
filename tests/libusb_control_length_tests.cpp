// SPDX-License-Identifier: MIT
// Compiles the actual patched dependency bodies with device-free Windows doubles.
#include <cstdio>
#include <cstdlib>
using ULONG = unsigned long;
constexpr int SUB_API_LIBUSB0 = 2, LIBUSB_SUCCESS = 0,
    LIBUSB_ERROR_IO = -1, LIBUSB_ERROR_OVERFLOW = -8, ERROR_IO_INCOMPLETE = 996;
constexpr bool FALSE = false;
static bool result_ok;
static ULONG result_length;
static int last_error, queried, completions, freed;
static ULONG published;
struct Overlapped {};
struct Interface { int dev_handle = 42; };
struct Handles { Interface interface_handle[1]; };
static bool GetOverlappedResult(int handle, Overlapped *, ULONG *length, bool wait) {
    if (handle != 42 || wait) std::abort();
    ++queried;
    if (result_ok) *length = result_length;
    return result_ok;
}
static int GetLastError() { return last_error; }
static void windows_force_sync_completion(void *, ULONG length) { ++completions; published = length; }
static int complete(int sub_api, ULONG transferred) {
    Handles handles;
    auto *handle_priv = &handles;
    int current_interface = 0;
    Overlapped ov;
    auto *overlapped = &ov;
    void *itransfer = nullptr;
#include "control_result.inc"
    return LIBUSB_SUCCESS;
}
struct Transfer { int actual_length; };
static void libusb_free_transfer(Transfer *) { ++freed; }
static int guard(int actual, unsigned short wLength) {
    Transfer value{actual}; auto *transfer = &value;
#include "control_copy_guard.inc"
    return 42; // copy permitted, rather than executed against fake buffers
}
static void check(bool condition) { if (!condition) std::abort(); }
int main() {
    result_ok = true; result_length = 1;
    check(complete(SUB_API_LIBUSB0, 499998300) == 0);
    check(queried == 1 && completions == 1 && published == 1);
    queried = completions = 0; result_ok = false; last_error = ERROR_IO_INCOMPLETE;
    check(complete(SUB_API_LIBUSB0, 499998300) == 0);
    check(queried == 1 && completions == 0);
    last_error = 6;
    check(complete(SUB_API_LIBUSB0, 499998300) == LIBUSB_SUCCESS);
    check(completions == 0);
    queried = 0;
    check(complete(0, 4) == 0 && queried == 0 && published == 4);
    check(guard(499998300, 1) == LIBUSB_ERROR_OVERFLOW);
    check(guard(-1, 1) == LIBUSB_ERROR_OVERFLOW && freed == 2);
    check(guard(0, 0) == 42 && guard(1, 1) == 42 && guard(65535, 65535) == 42);
    std::puts("libusb control result/copy bounds: passed");
}
