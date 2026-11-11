#pragma once
#include <stdint.h>
#include <wchar.h>
#ifdef _WIN32
#define PSVR_API __declspec(dllexport)
#else
#define PSVR_API
#endif
#ifdef __cplusplus
extern "C" {
#endif
/* x64 C ABI v2, cdecl, HRESULT int32. All session calls on creator thread.
   S_FALSE from read = timeout. No exception crosses the ABI.
   open selects a moniker DISPLAY NAME, never a numeric camera index.
   No default settings are written. close restores explicitly changed settings.
   A failed close keeps the handle alive for diagnosis; do NOT free/unload it. */
typedef struct psvr_dshow_session psvr_dshow_session;
/* Pure atomic completion storage: 16 bytes, alignment >=8, no COM lifetime.
   phase: 0 pending, 1 committed/success, 2 cancelled, 3 failed (HRESULT).
   Status/cancel are thread-safe and never wait for a camera/owner lock.
   Allocate a NEW decision per command; never reinitialize/reuse in-flight storage. */
typedef struct psvr_dshow_decision psvr_dshow_decision;
PSVR_API int32_t psvr_dshow_decision_init(void* aligned_storage, uint32_t size);
PSVR_API uint32_t psvr_dshow_decision_status(psvr_dshow_decision*, int32_t* error);
PSVR_API uint32_t psvr_dshow_decision_cancel(psvr_dshow_decision*);
typedef struct psvr_dshow_control {
    int32_t minimum, maximum, step, default_value, capabilities, value, flags;
} psvr_dshow_control;
typedef void (*psvr_dshow_enum_cb)(const wchar_t* display_name, void* context);
PSVR_API uint32_t psvr_dshow_abi(void);
PSVR_API int32_t psvr_dshow_enumerate(psvr_dshow_enum_cb callback, void* context);
PSVR_API int32_t psvr_dshow_open(const wchar_t* display_name, psvr_dshow_session** out);
/* Index is selection only: enumerate once, bind once, control THAT filter. */
PSVR_API int32_t psvr_dshow_open_index(int32_t index, psvr_dshow_session** out);
/* Retired unsafe index-only probe: always E_INVALIDARG, no allocation/bind. */
PSVR_API int32_t psvr_dshow_inspect_index(int32_t index, psvr_dshow_session** out);
/* Read-only candidate inspection on the same selected PS4 filter. Requires an
   exact IMoniker::GetDisplayName obtained before this call via metadata-only
   enumeration. Not IPropertyBag DevicePath or PnP ID. Index AND name must match
   before BindToObject; missing/mismatch never binds any device. No control
   interfaces/ranges, SetFormat, ConnectDirect, callbacks or Run. Caller owns
   returned handle even on failure and must close on its creator thread. */
PSVR_API int32_t psvr_dshow_inspect_index_exact_moniker(int32_t index,
    const wchar_t* expected_moniker, psvr_dshow_session** out);
/* Owner-only metadata copy, JSONL <=65535 bytes, no COM/device interrogation.
   Caller supplies >=65536 bytes. No monikers, paths, serials or image data. */
PSVR_API int32_t psvr_dshow_diagnostics(psvr_dshow_session*, char* dst, uint32_t capacity);
PSVR_API int32_t psvr_dshow_read(psvr_dshow_session*, uint8_t* dst, uint32_t capacity,
    uint32_t timeout_ms, uint64_t* sequence);
/* property: 0 exposure, 1 gain, 2 brightness. Manual flag is always 2. */
PSVR_API int32_t psvr_dshow_get_control(psvr_dshow_session*, uint32_t property,
    psvr_dshow_control* out);
PSVR_API int32_t psvr_dshow_set_manual(psvr_dshow_session*, uint32_t property, int32_t value);
/* ABI v2 transaction: bit 0 exposure, 1 gain, 2 brightness, targets[3].
   Begin validates ALL requested ranges before ANY Set; leaves a transaction
   active even on partial Set failure, for owner-only best-effort rollback.
   Begin/commit barriers discard the native latest slot and return its sequence.
   Caller must drain unique fresh frames and verify settling before commit.
   finish(persist=1) explicitly retains a successful Suite profile on normal close;
   failed/uncommitted transactions are ALWAYS rolled back, including Auto flags.
   Rollback verifies all requested properties and reports confirmed bits even
   on HRESULT failure. A failed rollback retains the transaction for close retry. */
PSVR_API int32_t psvr_dshow_begin_controls(psvr_dshow_session*, uint32_t mask,
    const int32_t* targets, uint64_t* boundary_sequence);
PSVR_API int32_t psvr_dshow_commit_controls(psvr_dshow_session*, uint64_t* boundary_sequence);
/* Non-blocking owner-only acknowledgement, after caller deadline/cancel check.
   No COM calls or mailbox locks; transaction remains rollbackable until here. */
PSVR_API int32_t psvr_dshow_finish_controls(psvr_dshow_session*, uint32_t persist);
/* Readiness extension: finish(persist=0) keeps lifetime originals. ONLY the
   owner may accept persistence after a validated post-commit frame has been
   delivered AND decoded/accepted by the pump, with shared cancellation checked.
   Sequence must exceed that transaction's commit barrier and have been read. */
PSVR_API int32_t psvr_dshow_accept_persistence(psvr_dshow_session*, uint64_t frame_sequence);
/* Required atomic-outcome extensions. Caller prepares immutable success result
   before entry. Successful decision CAS is the completion linearization point;
   no fallible operation/COM follows it. Notification may arrive later. */
PSVR_API int32_t psvr_dshow_accept_persistence_decided(psvr_dshow_session*, uint64_t,
    psvr_dshow_decision*);
PSVR_API int32_t psvr_dshow_accept_readiness_decided(psvr_dshow_session*, uint64_t,
    uint32_t persist, psvr_dshow_decision*);
PSVR_API int32_t psvr_dshow_finish_controls_decided(psvr_dshow_session*, uint32_t,
    psvr_dshow_decision*);
PSVR_API int32_t psvr_dshow_rollback_controls(psvr_dshow_session*, uint32_t* confirmed_mask);
PSVR_API int32_t psvr_dshow_close(psvr_dshow_session*);
#ifdef __cplusplus
}
#endif
