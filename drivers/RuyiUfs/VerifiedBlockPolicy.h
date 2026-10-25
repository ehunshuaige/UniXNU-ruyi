// SPDX-License-Identifier: BSD-2-Clause-Patent
// The board's owner-signed verified-root policy hook, which the sheng device repository
// supplies together with its signed manifests and trusted key. This port has no signed
// root material yet, so drivers/RuyiUfs/RuyiUfsGlue.cpp exports an empty policy set:
// NativeUfsBlock::configureVerification() then refuses to publish any window (fail
// closed, no block device), while the driver still brings the UFS host up and reads the
// GPT/layout on the test hardware.
//
// Type and symbols are the ones NativeUfs.cpp (UniXNU/NativeUfs) uses:
//   verified_root_policy_count   number of policies; 0..16
//   verified_root_policies[]     one immutable owner-signed envelope per window
#pragma once
#include <stddef.h>
#include <stdint.h>

struct NativeVerifiedBlockPolicy {
    const uint8_t *manifest;        /* owner-signed envelope bytes (immutable) */
    uint64_t       manifestBytes;   /* envelope length */
};

#ifdef __cplusplus
extern "C" {
#endif
extern const struct NativeVerifiedBlockPolicy verified_root_policies[];
extern const uint64_t verified_root_policy_count;
/* The trusted key NativeUfs copies after a policy matched; declared here so the
 * zero-filled definition below gets the external (C) linkage the driver imports. */
extern const uint8_t verified_root_key[];
#ifdef __cplusplus
}
#endif
