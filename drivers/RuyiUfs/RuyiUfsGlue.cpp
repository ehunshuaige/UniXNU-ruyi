// SPDX-License-Identifier: BSD-2-Clause-Patent
// RuyiUfsGlue: the ruyi-side link and diagnostic glue for the official UniXNU NativeUfs
// block driver (drivers/NativeUfs submodule), for the IOS-on-UFS milestone.
//
// The console patch of this port hooks _printf; IOLog output does not reach the screen.
// Everything this file says to the console therefore goes through printf.
//
// What is here and why:
//
//  * the owner-signed verified-root policy set (see VerifiedBlockPolicy.h). The sheng
//    device repository supplies real manifests and a trusted key here; ruyi has no signed
//    root material yet, so the policy count is zero. NativeUfsBlock::configureVerification()
//    then refuses to publish any block device (fail closed). This is not a bypass: nothing
//    is authenticated and nothing is published as a verified root.
//  * the optional boot-progress observer. NativeUfs calls it (weakly) after each verified
//    block read; sheng's boot-progress archive is not linked on ruyi. The weak definition
//    below keeps the hook linkable and prints a bounded trace of verified reads, so the
//    panel shows whether the UFS block layer ever reached that path. A real boot-progress
//    archive, when one is linked, overrides this weak definition.
//
// Nothing else is stubbed: the transport, the UFS host bring-up, the layout/GPT reader,
// the root publication coordinator and the block device are the unmodified upstream code.
#include <stdint.h>
#include <stddef.h>
#include <pexpert/pexpert.h>

#include "VerifiedBlockPolicy.h"
#include "verified_root.h"

/* The kernel console printer. misc_protos.h is a C translation-unit header in this
 * tree, so declare it with C linkage here rather than through it. */
extern "C" int printf(const char *format, ...) __printflike(1, 2);

extern "C" {

/* No owner-signed policy material is in this workspace. */
const struct NativeVerifiedBlockPolicy verified_root_policies[] = {
    { nullptr, 0 },
};
const uint64_t verified_root_policy_count = 0;

/* The trusted key encloses the signed envelope unchecked; zero-filled until the board
 * actually carries an owner key. It is never read while the policy count is zero. */
const uint8_t verified_root_key[VR_KEY_BYTES] = {};

/* Optional sheng boot-progress observer: absent in an ordinary build, so a weak
 * definition keeps NativeUfs' optional call linkable. Prints the first few calls. */
void boot_progress_poll(void) __attribute__((weak));
void boot_progress_poll(void)
{
    static unsigned calls;
    if (calls < 4) {
        calls++;
        printf("RuyiUfs: verified-block read progress (boot_progress_poll call %u)\n", calls);
    }
}

}
