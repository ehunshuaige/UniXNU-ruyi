/*
 * RuyiDisplayEntry: the boot entry points of the ruyi display driver.
 *
 * The XNU patches of this port call native_legacy_display_probe() from an optional
 * offline component instrument and native_legacy_display_late_probe() from
 * bsd_autoconf(), right after IOKitBSDInit() and before user space starts.  The late
 * call is where the display is actually brought up:
 *
 *   1. register the C++ classes of RuyiDisplay.cpp and RuyiIOSurface.cpp with the
 *      meta-class system (they are linked straight into the kernel, not loaded as a
 *      kext, so nothing else registers them);
 *   2. publish the IOSurface root service (the window server needs it before it can
 *      make the pixel buffers it hands to the framebuffer);
 *   3. publish and start the IOMobileFramebuffer service on the platform's service
 *      tree, backed by the boot frame buffer.
 *
 * Every step is logged with IOLog("RuyiDisplay: ..."): the panel this boot draws on is
 * the only console the phone has.
 */
#include <IOKit/IOService.h>
#include <IOKit/IOPlatformExpert.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <IOKit/IOLib.h>
#include <libkern/c++/OSMetaClass.h>
#include <libkern/OSReturn.h>
#include <mach/kmod.h>
#include <pexpert/pexpert.h>

#include "RuyiIOSurface.h"

extern "C" kmod_info_t g_kernel_kmod_info;

/*
 * The class files' C++ static initialisers.  scripts/build-ruyi-display.py removes
 * them from __DATA,__mod_init_func and globalises the symbols: OSRuntimeInitializeCPP()
 * runs the main kernel's __mod_init_func section before IOKit is up, and these classes
 * have to be registered after IOKitBSDInit(), from the late probe.  Calling them by
 * hand between OSMetaClass::preModLoad() and OSMetaClass::postModLoad() is the same
 * sequence a kext load uses (see the sheng/caihong native-driver builders).
 */
extern "C" void ruyi_iosurface_static_init(void) __asm__("__GLOBAL__sub_I_RuyiIOSurface.cpp");
extern "C" void ruyi_display_static_init(void) __asm__("__GLOBAL__sub_I_RuyiDisplay.cpp");

static unsigned ruyiDisplayStage;

/* The service the display and the surface root attach to: the platform's resource
 * service, else the platform expert, else whatever service /arm-io carries. */
static IOService *
ruyiDisplayProvider(void)
{
	IOService * provider;
	IOPlatformExpert * platform;
	IORegistryEntry * armio;

	provider = IOService::getResourceService();
	if (provider != NULL) {
		IOLog("RuyiDisplay: provider is the resource service %s\n", provider->getName());
		return provider;
	}
	platform = IOService::getPlatform();
	if (platform != NULL) {
		IOLog("RuyiDisplay: provider is the platform expert\n");
		return platform;
	}
	armio = IORegistryEntry::fromPath("/arm-io", gIODTPlane);
	provider = OSDynamicCast(IOService, armio);
	if (provider != NULL) {
		IOLog("RuyiDisplay: provider is the /arm-io service %s\n", provider->getName());
		return provider;
	}
	OSSafeReleaseNULL(armio);
	IOLog("RuyiDisplay: no platform service to attach the display to "
	    "(resource service, platform expert and /arm-io are all absent)\n");
	return NULL;
}

extern "C" {

/* Called from bsd_init()'s optional offline component instrument, before IOKit starts.
 * There is no registry to publish into yet; the work happens in the late probe. */
void
native_legacy_display_probe(void)
{
	IOLog("RuyiDisplay: early probe (bsd_init): the display starts after IOKitBSDInit\n");
}

/* Called from bsd_autoconf() right after IOKitBSDInit(). */
void
native_legacy_display_late_probe(void)
{
	IOService * provider;
	void * handle;
	OSReturn status;

	IOLog("RuyiDisplay: late probe: registering the display classes\n");
	if (ruyiDisplayStage != 0) {
		IOLog("RuyiDisplay: late probe already ran (stage %u)\n", ruyiDisplayStage);
		return;
	}
	ruyiDisplayStage = 1;
	if (g_kernel_kmod_info.name[0] == 0) {
		IOLog("RuyiDisplay: the kernel's kmod name is empty; cannot register classes\n");
		return;
	}
	handle = OSMetaClass::preModLoad(g_kernel_kmod_info.name);
	if (handle == NULL) {
		IOLog("RuyiDisplay: OSMetaClass::preModLoad(%s) refused\n", g_kernel_kmod_info.name);
		return;
	}
	ruyiDisplayStage = 2;
	ruyi_iosurface_static_init();
	ruyi_display_static_init();
	status = OSMetaClass::postModLoad(handle);
	IOLog("RuyiDisplay: %d classes registered under %s (postModLoad = %d)\n",
	    status == kOSReturnSuccess ? 7 : 0, g_kernel_kmod_info.name, (int) status);
	if (status != kOSReturnSuccess) {
		return;
	}
	ruyiDisplayStage = 3;
	provider = ruyiDisplayProvider();
	if (provider == NULL) {
		return;
	}
	ruyiDisplayStage = 4;
	RuyiIOSurfaceStart(provider);
	ruyiDisplayStage = 5;
	RuyiDisplayStart(provider);
	ruyiDisplayStage = 6;
	IOLog("RuyiDisplay: late probe done (stage %u)\n", ruyiDisplayStage);
}

} /* extern "C" */
