// SPDX-License-Identifier: MIT
// Legacy export table, no hardware; patch-level marker deliberately absent.
static int initializations=0;
extern "C" {
void ps3eye_init() { ++initializations; }
void ps3eye_uninit() {}
int ps3eye_count_connected() { return 0; }
void* ps3eye_open(int,int,int,int,int) { return nullptr; }
void ps3eye_grab_frame(void*,unsigned char*) {}
void ps3eye_close(void*) {}
int ps3eye_grab_frame_timeout(void*,unsigned char*,int) { return 0; }
void ps3eye_abort_frame(void*) {}
int ps3eye_get_frame_rate(void*) { return 75; }
int fixture_initializations() { return initializations; }
}
