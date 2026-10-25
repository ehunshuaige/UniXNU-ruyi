// SPDX-License-Identifier: BSD-2-Clause-Patent
// RuyiUfsGlue: the ruyi-side link and registration glue for the official UniXNU
// NativeUfs block driver (drivers/NativeUfs submodule), for the IOS-on-UFS milestone.
//
// The console patch of this port hooks _printf; IOLog output does not reach the screen.
// Everything this file says to the console therefore goes through printf.
//
// Nothing here runs at kernel init.  The archive's C++ static initialiser was moved
// out of __DATA,__mod_init_func by scripts/build-ruyi-ufs.py: OSRuntimeInitializeCPP()
// runs that section from InitIOKit(), before the prelinked IOStorageFamily kext (whose
// IOBlockStorageDevice metaclass NativeUfsBlock subclasses) is initialized, and a
// kernel with the constructor linked in dies before its first console line.  Instead
// the kernel's startup hook (patches/xnu/native-ufs-register.patch) parses
// native-ufs-root / native-ufs-probe and calls native_ufs_register() only when one of
// them is set; a boot without those arguments executes no UFS code at all.
//
// What this file provides and why:
//
//  * native_ufs_register(): load Apple's original IOStorageFamily kext (so the
//    IOBlockStorageDevice metaclass is initialized), run the deferred static
//    initialiser between OSMetaClass::preModLoad()/postModLoad() (the ruyi port's
//    established late-registration pattern, see drivers/RuyiDisplay), then drive the
//    same init/attach/start lifecycle the NativeUfsRoot KernelConfigTables personality
//    would drive, with the platform expert device as provider.
//  * the owner-signed verified-root policy set (see VerifiedBlockPolicy.h).  The sheng
//    device repository supplies real manifests and a trusted key here; ruyi has no
//    signed root material yet, so the policy count is zero.  NativeUfsBlock's
//    configureVerification() then refuses to publish any block device (fail closed).
//    This is not a bypass: nothing is authenticated and nothing is published as a
//    verified root; NativeUfs still brings the UFS host up and reads the GPT/layout.
//  * the optional boot-progress observer.  NativeUfs calls it (weakly) after each
//    verified block read; sheng's boot-progress archive is not linked on ruyi.  The
//    weak definition below keeps the hook linkable and prints a bounded trace of
//    verified reads.  A real boot-progress archive overrides this weak definition.
//
// Nothing else is stubbed: the transport, the UFS host bring-up, the layout/GPT reader,
// the root publication coordinator and the block device are the unmodified upstream code.
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <libkern/c++/OSMetaClass.h>
#include <libkern/c++/OSString.h>
#include <pexpert/pexpert.h>
#include <mach/kmod.h>
#include <stdint.h>
#include <stddef.h>
#include "VerifiedBlockPolicy.h"
#include "verified_root.h"

extern "C" kmod_info_t g_kernel_kmod_info;
/* The globalised, deferred C++ static initialiser of drivers/NativeUfs/NativeUfs.cpp. */
extern "C" void native_ufs_static_init(void) __asm__("__GLOBAL__sub_I_NativeUfs.cpp");
extern "C" kern_return_t OSKextLoadKextWithIdentifier(const char *identifier);
/* The kernel console printer. misc_protos.h is a C translation-unit header in this
 * tree, so declare it with C linkage here rather than through it. */
extern "C" int printf(const char *format, ...) __printflike(1, 2);

extern "C" {

/* No owner-signed policy material is in this workspace. */
const struct NativeVerifiedBlockPolicy verified_root_policies[] = {
    { nullptr, 0 },
};
const uint64_t verified_root_policy_count = 0;

/* The trusted key NativeUfs copies after a policy matched; zero-filled until the board
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

// Called by the kernel startup hook (bsd_init, after IOKitBSDInit) only when the
// native-ufs-root or native-ufs-probe boot argument was passed.  Everything the
// driver needs is up by then: IOKit matching has run, kext loading works (the display
// late probe loads IOSurface from the same place), and the platform expert device is
// registered.  The boot args are re-checked here so a stray call can never bring the
// driver up on an ordinary boot.
void native_ufs_register(void)
{
    unsigned rootMode = 0, probeMode = 0;
    PE_parse_boot_argn("native-ufs-root", &rootMode, sizeof(rootMode));
    PE_parse_boot_argn("native-ufs-probe", &probeMode, sizeof(probeMode));
    if (!rootMode && !probeMode) {
        return;
    }
    printf("RuyiUfs: registering NativeUfs (native-ufs-root=%u native-ufs-probe=%u)\n", rootMode, probeMode);

    // The subclass dispatch table is Apple's original IOStorageFamily implementation.
    // Load the kext first so its metaclass objects are initialized before the deferred
    // constructors take IOBlockStorageDevice as the superclass.
    kern_return_t loadStatus = OSKextLoadKextWithIdentifier("com.apple.iokit.IOStorageFamily");
    printf("RuyiUfs: load com.apple.iokit.IOStorageFamily = %d\n", static_cast<int>(loadStatus));
    if (loadStatus != kOSReturnSuccess) {
        printf("RuyiUfs: IOStorageFamily unavailable; NativeUfs not registered\n");
        return;
    }

    void *handle = OSMetaClass::preModLoad(g_kernel_kmod_info.name);
    if (!handle) {
        printf("RuyiUfs: preModLoad(%s) refused; NativeUfs not registered\n", g_kernel_kmod_info.name);
        return;
    }
    native_ufs_static_init();
    OSReturn status = OSMetaClass::postModLoad(handle);
    printf("RuyiUfs: NativeUfs classes registered (postModLoad=%d)\n", static_cast<int>(status));
    if (status != kOSReturnSuccess) {
        return;
    }

    // This is the provider the KernelConfigTables "NativeUfsRoot" personality matches
    // (IOProviderClass IOPlatformExpertDevice); getServiceRoot() is that root device.
    IOService *provider = IOService::getServiceRoot();
    IOService *device = nullptr;
    bool init = false, attached = false, started = false;
    if (!provider || !provider->metaCast("IOPlatformExpertDevice")) {
        printf("RuyiUfs: no platform expert provider (%p); NativeUfs not started\n", provider);
        return;
    }
    device = OSDynamicCast(IOService, OSMetaClass::allocClassWithName("NativeUfs"));
    if (device) {
        init = device->init();
        attached = init && device->attach(provider);
        started = attached && device->start(provider);
    }
    printf("RuyiUfs: driver %p init=%u attach=%u start=%u\n", device, init, attached, started);
    if (started) {
        device->registerService();
        // The driver keeps the root publication state for the rest of the boot.
        device->release();
    } else {
        if (attached) {
            device->detach(provider);
        }
        if (device) {
            device->release();
        }
        printf("RuyiUfs: NativeUfs did not start; see the UFS diagnostic page\n");
    }
}

}
