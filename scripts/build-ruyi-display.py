#!/usr/bin/env python3
"""Build the official UniXNU GopFramebuffer display driver for the ruyi port.

This is the ruyi port of the reference recipe in scripts/build-native-legacy-display.py
(the sheng one): a concrete backend bound to the kernel's genuine original
IOMobileFramebuffer ("Legacy") implementation, with the UEFI GOP frame buffer as
scanout memory.  What it does, and where the inputs come from:

  * The original class ABI is audited, not invented: scripts/audit-mobile-graphics-
    legacy.py (in the UniXNU tree) locates the original IOMobileGraphicsFamily
    executable in a prelinked image of this *same* kernelcache (iPad7,1 / 17.7.11
    / 21H461, sha256 f18b4df8...) and writes build/display-analysis/
    mobile-graphics-legacy-abi.json: the 358 vtable slots, the nine abstract
    leaves, and the five object-layout facts the backend needs.  KiCad: that audit
    is run once, on the ruyi prelink image, and its output is committed next to
    this driver (drivers/RuyiDisplay/mobile-graphics-legacy-abi.json) together
    with the candidate image hash, so this builder can regenerate the opaque
    header `OriginalLegacyFramebufferABI.h` in CI without a second prelink pass.
    The values are checked below against the qualified family layout; a different
    kernelcache fails the build instead of producing a silently wrong vtable.

  * drivers/NativeMobileDisplay/ is the UniXNU submodule (GopFramebuffer): the
    unmodified NativeLegacyDisplay.cpp and NativeGOPPresenter.cpp, version-pinned
    by UniXNU's .gitmodules.  They are compiled with the kernel's own compile
    record (IOPlatformExpert.cpo.json, left by the kernel bootstrap pass) and the
    generated header, then the two C++ static initialisers are moved out of
    __DATA,__mod_init_func (OSRuntimeInitializeCPP() runs the main kernel's
    section before IOKit is up; these classes are registered later, from the
    bsd_init probe, exactly like the reference port does).

  * drivers/RuyiDisplay/RuyiDisplayProbe.cpp is the machine glue: it registers the
    classes, loads Apple's IOMobileGraphicsFamily (bsd_init) and IOSurface
    (bsd_autoconf) kexts, publishes the GOP frame buffer as an IOPlatformDevice,
    attaches/starts the display on it, attaches the display-subsystem node to
    /arm-io, and provides the driver's lifecycle observation hooks.  It is
    adapted from sheng's package/LegacyDisplayProbe.cpp; the sheng-only input,
    backlight, power and keystore probes are not called here.

  * original-bindings.S is generated here: every IOMobileFramebufferLegacy method
    the compiled driver does not override is bound to its exact original address
    (the nine abstract leaves are overridden by the driver's guarded LEAFs), plus
    the IOSurface getters, the vsync sender and setDisplayRefreshProperties.  No
    original instruction is modified.

Outputs, checked by the kernel build before it force-loads the archive:

    build/native-legacy-display/libNativeLegacyDisplay.late.a
    build/native-legacy-display/build-report.json
        archive_sha256   sha256 of the archive
        source_sha256    {path relative to the build tree: sha256}

Run by `unixnu build kernel` through [kernel] archives of board.toml, with the
build tree as the working directory; the kernel bootstrap pass must have written
the compile record first.
"""
import hashlib
import json
import pathlib
import shutil
import struct
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "native-legacy-display"
DRIVER = ROOT / "drivers" / "NativeMobileDisplay"        # UniXNU submodule (GopFramebuffer)
GLUE = ROOT / "drivers" / "RuyiDisplay"                  # device repository: probe + audit data
ARCHIVE = OUT / "libNativeLegacyDisplay.late.a"
REPORT = OUT / "build-report.json"
AUDIT = GLUE / "mobile-graphics-legacy-abi.json"

# The kernel's compile record for the ruyi target: the same file the other archive
# builders borrow (build-arm-driver.py, the caihong/sheng native display builders).
RECORD = ROOT / "build" / "xnu" / "ios-obj" / "DEVELOPMENT_ARM64_BCM2837" / "iokit" / "DEVELOPMENT" / "IOPlatformExpert.cpo.json"

# Facts of the qualified original-family layout for this kernelcache (iPad7,1
# 17.7.11 21H461).  The audit must agree with all of them; the last three cannot be
# read from the audit and were verified against the same prelinked image (the
# "original_family_text_sha256" pins the executable the addresses were read from).
FAMILY_TEXT_SHA256 = "2e044ee68c1650397e1435234ca6459206262e4c7ed709cb7131c6f664f9bd9b"
LEGACY_VPTR = "0xfffffff005f38570"
LEGACY_INSTANCE_BYTES = 0x18a0
LEGACY_VIRTUAL_ENTRIES = 358
ABSTRACT_SLOTS = [211, 213, 245, 246, 306, 307, 337, 338, 345]

# Symbols bound to exact addresses in the original IOMobileGraphicsFamily kext.
# (name as it appears in the object symbol table: Mach-O already prefixes C symbols)
BASE_BINDINGS = {
    "__ZN25IOMobileFramebufferLegacyC2EPK11OSMetaClass": 0xfffffff0057865cc,
    "__ZN25IOMobileFramebufferLegacyD2Ev": 0xfffffff005786618,
    "__ZN25IOMobileFramebufferLegacy10gMetaClassE": 0xfffffff006b35940,
    "__ZTV25IOMobileFramebufferLegacy": 0xfffffff005f38560,
}
# The original IOSurface getters (read a real IOSurface object the family hands to
# the validator), the original vsync sender and setDisplayRefreshProperties.
EXTRA_BINDINGS = {
    "_native_legacy_surface_descriptor": 0xfffffff004b9ead0,
    "_native_legacy_surface_width": 0xfffffff004b9ebbc,
    "_native_legacy_surface_height": 0xfffffff004b9ebc4,
    "_native_legacy_surface_stride": 0xfffffff004b9ebd4,
    "_native_legacy_surface_allocation": 0xfffffff004b9ebfc,
    "_native_legacy_surface_format": 0xfffffff004b9ebac,
    "_native_legacy_surface_planes": 0xfffffff004b9ebf4,
    "_native_legacy_vsync_notify": 0xfffffff005796720,
    "_native_legacy_set_refresh_properties": 0xfffffff00579ebb8,
}

# Sources compiled from the UniXNU submodule; (file name, object stem, static init symbol).
DRIVER_SOURCES = [
    ("NativeLegacyDisplay.cpp", "NativeLegacyDisplay", "__GLOBAL__sub_I_NativeLegacyDisplay.cpp"),
    ("NativeGOPPresenter.cpp", "NativeGOPPresenter", "__GLOBAL__sub_I_NativeGOPPresenter.cpp"),
]


def fail(message):
    print(f"display: {message}", file=sys.stderr, flush=True)
    raise SystemExit(message)


def run(command, cwd=None):
    result = subprocess.run([str(part) for part in command], cwd=cwd, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        print(f"display: command failed ({result.returncode}): {' '.join(str(part) for part in command[:6])} ...",
              file=sys.stderr, flush=True)
        print((result.stdout or "")[-8000:], file=sys.stderr, flush=True)
        print((result.stderr or "")[-8000:], file=sys.stderr, flush=True)
        raise SystemExit(f"display: build step failed ({result.returncode})")
    return result.stdout


def sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def record():
    if not RECORD.is_file():
        candidates = sorted((ROOT / "build" / "xnu" / "ios-obj").glob("*/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json"))
        fail(f"no compile record at {RECORD.relative_to(ROOT)} (candidates: "
             f"{', '.join(str(c.relative_to(ROOT)) for c in candidates) or 'none'}); "
             "the kernel bootstrap pass did not run")
    return json.loads(RECORD.read_text().strip().rstrip(","))


def compiler_command(compile_record, source, output):
    """The kernel's own command for IOPlatformExpert.cpp, with our file and output."""
    arguments = compile_record["arguments"]
    command, used_source, used_output, index = [], False, False, 0
    while index < len(arguments):
        value = arguments[index]
        if value == compile_record["file"] or (not used_source and value.endswith("IOPlatformExpert.cpp")):
            command.append(str(source))
            used_source = True
        elif value == "-o":
            command.extend(["-o", str(output)])
            used_output = True
            index += 1
        elif value in ("-MJ", "-MF", "-MT"):
            # Compile-database/dependency artefacts of the kernel's own object: do not
            # overwrite the record, later archive builders read it too.
            index += 1
        elif value in ("-MD", "-MP"):
            pass
        else:
            command.append(value)
        index += 1
    if not used_source:
        fail(f"the compile record names no source to replace: {compile_record['file']}")
    if not used_output:
        fail("the compile record has no -o to replace")
    return command


def compile_source(compile_record, source, output, includes):
    if not pathlib.Path(source).is_file():
        fail(f"{source} is missing")
    command = compiler_command(compile_record, source, output)
    for directory in includes:
        command.extend(["-I", str(directory)])
    # The archive is force-loaded, not part of the kernel's own warning sweep.
    command.extend(["-I", str(ROOT / ".cache/xnu-work/osfmk"), "-w"])
    print(f"display: compile {pathlib.Path(source).name}", flush=True)
    run(command, cwd=compile_record["directory"])
    if not pathlib.Path(output).is_file():
        fail(f"the compiler did not produce {output}")
    return pathlib.Path(output)


def generate_abi_header(audit):
    """The reference generator: opaque storage for the exact original class."""
    pure = audit["abstract_slots"]
    methods = audit["methods"]
    header = '''// SPDX-License-Identifier: BSD-2-Clause-Patent
// Generated by scripts/build-ruyi-display.py from the audited original IOMobileGraphicsFamily
// layout of the iPad7,1 17.7.11 21H461 kernelcache (drivers/RuyiDisplay/mobile-graphics-legacy-abi.json).
// Opaque ABI storage for the exact original class, not source-level access to its private
// fields. The actual original metadata supplies its full Framebuffer/Service/IOService
// ancestry. Only qualified base prototypes may be called from source; opaque entries are
// original dispatch targets only.
#pragma once
#include <IOKit/IOService.h>
#include <libkern/libkern.h>
struct OriginalLegacySwapRequest;
class IOMobileFramebufferLegacy : public IOService {
 OSDeclareAbstractStructors(IOMobileFramebufferLegacy)
 unsigned char originalPrivate[0x18a0-sizeof(IOService)];
public:
 // Both fields are qualified by the complete original slot-249 getter.
 // This modifies this object's own mode state, never a vtable or executable.
 void installBootDimensions(uint32_t width,uint32_t height) {
  *reinterpret_cast<uint32_t *>(reinterpret_cast<unsigned char *>(this)+5808)=width;
  *reinterpret_cast<uint32_t *>(reinterpret_cast<unsigned char *>(this)+5812)=height;
 }
 const OriginalLegacySwapRequest *pendingSwapRequest() const {
  const OriginalLegacySwapRequest *request=nullptr;
  memcpy(&request,reinterpret_cast<const unsigned char *>(this)+5480,sizeof(request));return request;
 }
 // The real normal scanout backend removes its pending head before calling slot 295. Slot 312
 // is a discard path (terminal state 8), not presentation. The queue is a tail queue (head at
 // +5480, tail pointer at +5488, each request's next at +2960 and previous-link at +2968):
 // taking the head moves the next request, if a client already queued one behind it, up to the
 // head. The original queue's reference moves to the current-swap slot for the completion body.
 bool activateOnlyPendingSwap(const OriginalLegacySwapRequest *request) {
  auto self=reinterpret_cast<unsigned char *>(this);
  auto bytes=reinterpret_cast<const unsigned char *>(request);
  const void *current=nullptr,*head=nullptr,*tail=nullptr,*next=nullptr,*previous=nullptr;
  memcpy(&current,self+5472,8);memcpy(&head,self+5480,8);memcpy(&tail,self+5488,8);
  if(!request || current || head!=request)return false;
  memcpy(&next,bytes+2960,8);memcpy(&previous,bytes+2968,8);
  if(previous!=self+5480)return false;
  if(next) {
   const void *link=self+5480;
   memcpy(const_cast<unsigned char *>(reinterpret_cast<const unsigned char *>(next))+2968,&link,8);
  } else if(tail!=bytes+2960)return false;
  memcpy(self+5472,&request,8);
  if(next) {memcpy(self+5480,&next,8);}
  else {const void *empty=nullptr,*emptyTail=self+5480;memcpy(self+5480,&empty,8);memcpy(self+5488,&emptyTail,8);}
  return true;
 }
 void free() override;
 bool init(OSDictionary *properties=nullptr) override;
 IOReturn setProperties(OSObject *) override;
 void systemWillShutdown(IOOptionBits) override;
 IOService *probe(IOService *,SInt32 *) override;
 bool start(IOService *) override;
 void stop(IOService *) override;
 IOWorkLoop *getWorkLoop() const override;
 IOReturn newUserClient(task_t,void *,UInt32,IOUserClient **) override;
 IOReturn setPowerState(unsigned long,IOService *) override;
'''
    for i in range(168, 358):
        if i == 184:
            header += ' virtual bool isMainDisplay();\n'
        elif i == 188:
            header += ' virtual IOReturn getDotPitch(uint32_t *);\n'
        elif i == 213:
            header += ' virtual IOReturn supportedFrameInfo(uint32_t,uint32_t *,char *,uint32_t *)=0;\n'
        elif i == 249:
            header += ' virtual void getDisplayDimensions(uint32_t *,uint32_t *);\n'
        elif i == 290:
            header += ' virtual void kickSwapQueue();\n'
        elif i == 296:
            header += ' virtual bool validateSwap(const OriginalLegacySwapRequest *);\n'
        elif i == 295:
            header += ' virtual void completeCurrentSwap();\n'
        elif i == 337:
            header += ' virtual IOReturn collectCompletionReports(uint32_t,void *,uint32_t *,uint32_t)=0;\n'
        elif i == 288:
            header += ' virtual bool startHardware();\n'
        elif i == 301:
            header += ' virtual bool getRefreshAttributes(uint64_t *,bool);\n'
        elif i in pure:
            header += f' virtual __attribute__((noreturn)) void opaque{i}()=0;\n'
        else:
            header += f' virtual __attribute__((deprecated("Unclassified ABI: never call from source"))) void opaque{i}();\n'
    header += '};\nstatic_assert(sizeof(IOMobileFramebufferLegacy)==0x18a0,"Actual original instance extent");\n'
    target = OUT / "OriginalLegacyFramebufferABI.h"
    target.write_text(header)
    print(f"display: generated {target.relative_to(ROOT)} ({len(header.splitlines())} lines) "
          f"from the audited original layout", flush=True)
    return target


def audit_abi():
    if not AUDIT.is_file():
        fail(f"the original-family ABI audit {AUDIT.relative_to(ROOT)} is missing")
    audit = json.loads(AUDIT.read_text())
    if audit.get("legacy_virtual_entries") != LEGACY_VIRTUAL_ENTRIES:
        fail(f"audit has {audit.get('legacy_virtual_entries')} virtual entries, expected {LEGACY_VIRTUAL_ENTRIES}")
    if audit.get("legacy_instance_bytes") != LEGACY_INSTANCE_BYTES:
        fail(f"audit instance extent {audit.get('legacy_instance_bytes')} != {LEGACY_INSTANCE_BYTES}")
    if audit.get("legacy_vptr") != LEGACY_VPTR:
        fail(f"audit vtable {audit.get('legacy_vptr')} != qualified {LEGACY_VPTR} (wrong kernelcache?)")
    if audit.get("original_family_text_sha256") != FAMILY_TEXT_SHA256:
        fail(f"audit original family executable {audit.get('original_family_text_sha256')} != qualified "
             f"{FAMILY_TEXT_SHA256} (wrong kernelcache?)")
    if audit.get("abstract_slots") != ABSTRACT_SLOTS:
        fail(f"audit abstract slots {audit.get('abstract_slots')} != qualified {ABSTRACT_SLOTS}")
    methods = audit.get("methods") or []
    if len(methods) != LEGACY_VIRTUAL_ENTRIES:
        fail(f"audit lists {len(methods)} methods, expected {LEGACY_VIRTUAL_ENTRIES}")
    print(f"display: audit {AUDIT.relative_to(ROOT)} qualified: {LEGACY_VIRTUAL_ENTRIES} slots, "
          f"abstract {ABSTRACT_SLOTS}, family text {FAMILY_TEXT_SHA256[:16]}..., "
          f"candidate {audit.get('candidate_sha256', '?')[:16]}...", flush=True)
    return audit


def macho_symbols(raw):
    """The object's Mach-O symbol table: [(name, address, kind)]."""
    sys.path.insert(0, str(ROOT / "kits/NativePrelink"))
    from native_prelink import macho

    image = macho(raw)
    symbol_command = next(offset for command, offset, _ in image["commands"] if command == 2)
    symbol_offset, symbol_count, string_offset, _ = struct.unpack_from("<4I", raw, symbol_command + 8)
    symbols = []
    for index in range(symbol_count):
        name_offset, kind, section, _, address = struct.unpack_from("<IBBHQ", raw, symbol_offset + index * 16)
        name = raw[string_offset + name_offset:raw.index(b"\0", string_offset + name_offset)].decode()
        symbols.append((name, address, kind))
    return image, symbols


def vtable_slots(raw, image, symbols):
    """The compiled vtable's per-slot symbol names for IOMobileFramebufferLegacy's 358 slots."""
    vtable = next(address for name, address, _ in symbols if name == "__ZTV19NativeLegacyDisplay")
    relocations = {}
    for segment in image["segments"]:
        for section in segment["sections"]:
            if section["va"] <= vtable < section["va"] + section["size"]:
                reloc_offset, reloc_count = struct.unpack_from("<2I", raw, section["command_offset"] + 56)
                for index in range(reloc_count):
                    offset, bits = struct.unpack_from("<2I", raw, reloc_offset + index * 8)
                    if bits >> 27 & 1 and bits >> 28 == 0 and bits >> 25 & 3 == 3:
                        relocations[section["va"] + offset] = symbols[bits & 0xffffff][0]
    return [relocations.get(vtable + 16 + index * 8) for index in range(LEGACY_VIRTUAL_ENTRIES)]


def vtable_bindings(binary, audit):
    """Bind every original method the compiled driver does not override to its address."""
    raw = binary.read_bytes()
    image, symbols = macho_symbols(raw)
    slots = vtable_slots(raw, image, symbols)
    defined = {name for name, _, kind in symbols if kind != 1}
    undefined = {name for name, _, kind in symbols if kind == 1}
    if not all(slots) or (len(defined) + len(undefined)) == 0:
        fail("the compiled driver vtable has holes")
    pure = set(audit["abstract_slots"])
    methods = audit["methods"]
    owned_slots, bindings = [], dict(BASE_BINDINGS)
    bindings.update(EXTRA_BINDINGS)
    for index, name in enumerate(slots):
        if name.startswith(("__ZN19NativeLegacyDisplay", "__ZNK19NativeLegacyDisplay")):
            owned_slots.append(index)
        elif "25IOMobileFramebufferLegacy" in name:
            if index in pure:
                fail(f"abstract original slot {index} is not overridden by the driver")
            bindings[name] = int(methods[index]["address"], 16)
    expected_owned = sorted([0, 1, 7, 14, 84, 86, 87, 141, 155, 184, 188, 288, 290, 296, 301] + sorted(pure))
    if owned_slots != expected_owned:
        fail(f"the driver overrides {owned_slots}, expected {expected_owned}")
    imports = {name for name in undefined if "IOMobileFramebufferLegacy" in name}
    if imports != set(bindings) & imports:
        fail(f"unbound original-family imports: {sorted(imports - set(bindings))}")
    print(f"display: vtable: {LEGACY_VIRTUAL_ENTRIES} slots, driver owns {owned_slots}, "
          f"{len(bindings)} original bindings, {LEGACY_VIRTUAL_ENTRIES - len(owned_slots)} inherited slots", flush=True)
    assembly = "".join(f".globl {name}\n.set {name}, {value:#x}\n" for name, value in sorted(bindings.items()))
    return assembly, bindings, symbols, undefined


def llvm_tool(name):
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/opt/llvm", "/usr/local/opt/llvm"):
        candidate = pathlib.Path(prefix) / "bin" / name
        if candidate.is_file():
            return str(candidate)
    fail(f"{name} is required (brew install llvm)")


def compile_bindings(assembly):
    source = OUT / "original-bindings.S"
    source.write_text(assembly)
    sdk = run(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"]).strip()
    target = OUT / "original-bindings.o"
    run(["xcrun", "clang", "-target", "arm64-apple-ios17.0", "-isysroot", sdk, "-c", source, "-o", target])
    return target


def late_object(raw):
    """Move the C++ static initialiser out of __DATA,__mod_init_func and globalise it."""
    _, symbols = macho_symbols(raw.read_bytes())
    symbol = next((name for name, _, kind in symbols
                   if name.startswith("__GLOBAL__sub_I_") and kind != 1), None)
    if not symbol:
        fail(f"{raw.name}: no C++ static initialiser found")
    late = raw.with_name(raw.stem + ".late.o")
    objcopy = llvm_tool("llvm-objcopy")
    run([objcopy, "--remove-section=__DATA,__mod_init_func", f"--globalize-symbol={symbol}", raw, late])
    if not late.is_file():
        fail(f"llvm-objcopy did not produce {late}")
    return late, symbol


def archive(objects):
    ARCHIVE.unlink(missing_ok=True)
    if shutil.which("xcrun"):
        command = ["xcrun", "ar", "rcs", ARCHIVE]
    else:
        command = ["ar", "rcs", ARCHIVE]
    run(command + objects)
    if not ARCHIVE.is_file():
        fail(f"no archive was produced at {ARCHIVE}")
    print(f"display: {ARCHIVE.relative_to(ROOT)} ({ARCHIVE.stat().st_size} bytes)", flush=True)


def report(audit, bindings, sources):
    document = {
        "archive_sha256": sha256(ARCHIVE),
        "source_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in sources},
        "note": "ruyi official GopFramebuffer display: NativeLegacyDisplay + NativeGOPPresenter from the "
                "UniXNU submodule, original-family ABI from the audited prelink layout, ruyi probe glue",
        "original_audit_sha256": sha256(AUDIT),
        "original_family_text_sha256": audit["original_family_text_sha256"],
        "original_candidate_sha256": audit.get("candidate_sha256"),
        "bindings": {name: hex(value) for name, value in sorted(bindings.items())},
        "virtual_entries": LEGACY_VIRTUAL_ENTRIES,
        "abstract_slots": ABSTRACT_SLOTS,
    }
    REPORT.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"display: {REPORT.relative_to(ROOT)}: archive sha256 {document['archive_sha256']}, "
          f"{len(sources)} sources, {len(bindings)} original bindings", flush=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if not DRIVER.is_dir():
        fail(f"{DRIVER.relative_to(ROOT)} is missing: the UniXNU submodule (GopFramebuffer) was not checked "
             "out; run 'git submodule update --init drivers/NativeMobileDisplay' in the UniXNU repository")
    compile_record = record()
    print(f"display: compile record {RECORD.relative_to(ROOT)} ({len(compile_record['arguments'])} arguments, "
          f"directory {compile_record['directory']})", flush=True)
    audit = audit_abi()
    header = generate_abi_header(audit)
    includes = [OUT, DRIVER, GLUE]

    late_objects, sources = [], []
    for filename, stem, _ in DRIVER_SOURCES:
        source = DRIVER / filename
        raw = compile_source(compile_record, source, OUT / f"{stem}.o", includes)
        late, symbol = late_object(raw)
        print(f"display: {raw.name} -> {late.name} (initialiser {symbol} globalised)", flush=True)
        late_objects.append(late)
        sources += [source, DRIVER / ("NativeGOPWire.h" if stem == "NativeGOPPresenter" else "NativeLegacyRequest.h")]

    probe_source = GLUE / "RuyiDisplayProbe.cpp"
    probe = compile_source(compile_record, probe_source, OUT / "RuyiDisplayProbe.o", includes)
    sources.append(probe_source)

    assembly, bindings, symbols, undefined = vtable_bindings(OUT / "NativeLegacyDisplay.o", audit)
    bindings_object = compile_bindings(assembly)
    sources += [AUDIT, header]

    archive(late_objects + [bindings_object, probe])
    report(audit, bindings, sources)
    print("display: official GopFramebuffer display archive ready for the kernel link", flush=True)


if __name__ == "__main__":
    main()
