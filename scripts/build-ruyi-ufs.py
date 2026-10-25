#!/usr/bin/env python3
"""Build the official UniXNU NativeUfs block driver for the ruyi port.

NativeUfs (drivers/NativeUfs, the UniXNU submodule) is the read-only UFS block
driver that exposes windows of the userdata LUN 0 as IOBlockStorageDevice
children, so the iOS system volume can be mounted from a UFS partition instead
of living in RAM.  It has two kinds of link inputs, handled here:

  * IOStorageFamily: the class IOBlockStorageDevice, its protected constructor,
    destructor, metaclass, vtable and the original methods of its 190-slot
    dispatch table live in the prelinked IOStorageFamily kext, not in the native
    kernel.  drivers/RuyiUfs/storage-abi-bindings.json pins those exact
    original addresses for this prelink layout (iPad7,1 / 17.7.11 / 21H461,
    slide -0x1000000).  The addresses were derived with the same qualification
    as scripts/audit-storage-interface.py, but from a prelinked ruyi image of
    this kernelcache: the public IOBlockStorageDevice.cpp from
    refer/IOStorageFamily is compiled with kernel flags to enumerate the 190
    reference names, the original table is then identified in that image by the
    175 inherited entries the prelink rebinds to native kernel functions, and
    the 14 original implementations plus the class identity symbols are
    recorded.  This builder only binds the symbols the compiled subclass
    itself imports, and refuses to build if the table is missing one.

  * the owner-signed verified-root policy material, which the sheng device
    repository supplies.  ruyi has none, so drivers/RuyiUfs/RuyiUfsGlue.cpp
    exports an empty policy set: the driver's root publication refuses every
    window (fail closed, no block device) while still bringing the UFS host up
    and reading the GPT/layout on the hardware.  The real verified-root kit
    (kits/VerifiedRoot: vr_open/vr_verify_block/vr_xnu_crypto) is compiled in,
    so no crypto is stubbed; only the policy data is empty.

The driver sources are compiled with the kernel's own compile records: C++ with
iokit/DEVELOPMENT/IOPlatformExpert.cpo.json, the verified-root kit's C files
with bsd/DEVELOPMENT/bsd_init.o.json.  NativeUfs's C++ static initialiser stays
in __DATA,__mod_init_func: the class is a plain IOService subclass and must be
registered at kernel C++ runtime init so the KernelConfigTables NativeUfs
personalities (XNU_NATIVE_UFS=1) can match it.  (Only the display driver needed
its initialisers deferred, because it subclasses a prelinked kext class.)

Outputs:
    build/native-ufs/libNativeUfs.a     (the path build-xnu-4k-baseline.sh force-loads)
    build/native-ufs/build-report.json

Run by `unixnu build kernel` through [kernel] archives of board.toml, with the
build tree as the working directory; the kernel bootstrap pass must have written
the compile records and the kernel step must have prepared the IOStorageFamily
headers first.
"""
import hashlib
import json
import pathlib
import shutil
import struct
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "native-ufs"
ARCHIVE = OUT / "libNativeUfs.a"
REPORT = OUT / "build-report.json"
DRIVER = ROOT / "drivers" / "NativeUfs"                       # UniXNU submodule (NativeUfs)
VERIFIED_ROOT = ROOT / "kits" / "VerifiedRoot"                # UniXNU submodule (VerifiedRoot)
GLUE = ROOT / "drivers" / "RuyiUfs"                           # device repository: policy glue + bindings
BINDINGS = GLUE / "storage-abi-bindings.json"
STORAGE_INCLUDE = ROOT / "build" / "storage-abi" / "include"

CPP_RECORD = ROOT / "build" / "xnu" / "ios-obj" / "DEVELOPMENT_ARM64_BCM2837" / "iokit" / "DEVELOPMENT" / "IOPlatformExpert.cpo.json"
C_RECORD = ROOT / "build" / "xnu" / "ios-obj" / "DEVELOPMENT_ARM64_BCM2837" / "bsd" / "DEVELOPMENT" / "bsd_init.o.json"

# Qualified facts about the original IOStorageFamily layout of the pinned
# kernelcache; the committed binding table must agree with them.
STORAGE_FAMILY_TEXT_SHA256 = "852b7846a9039c58fde08478416080af38ffcd5b47c08c889b28497f5f5b4061"
KERNELCACHE_SHA256 = "f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7"
SOURCE_REVISION = "e45fbe30a7879e0b16e26dddb78538f1b00e0bff"
METHOD_SLOTS = 190
PURE_VIRTUAL_SLOTS = 14

# The original IOBlockStorageDevice methods the compiled subclass imports; each one
# must be in the committed binding table (see the module docstring).
BINDING_PREFIXES = ("__ZN20IOBlockStorageDevice", "__ZNK20IOBlockStorageDevice", "__ZTV20IOBlockStorageDevice")


def fail(message):
    print(f"ufs: {message}", file=sys.stderr, flush=True)
    raise SystemExit(message)


def run(command, cwd=None):
    result = subprocess.run([str(part) for part in command], cwd=cwd, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        print(f"ufs: command failed ({result.returncode}): {' '.join(str(part) for part in command[:6])} ...",
              file=sys.stderr, flush=True)
        print((result.stdout or "")[-8000:], file=sys.stderr, flush=True)
        print((result.stderr or "")[-8000:], file=sys.stderr, flush=True)
        raise SystemExit(f"ufs: build step failed ({result.returncode})")
    return result.stdout


def sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def record(path):
    if not path.is_file():
        candidates = sorted((ROOT / "build" / "xnu" / "ios-obj").glob("*/*/DEVELOPMENT/*.json"))
        fail(f"no compile record at {path.relative_to(ROOT)} (found: "
             f"{', '.join(str(c.relative_to(ROOT)) for c in candidates[:8]) or 'none'}); "
             "the kernel bootstrap pass did not run")
    return json.loads(path.read_text().strip().rstrip(","))


def compiler_command(compile_record, source, output, includes, defines):
    arguments = compile_record["arguments"]
    command, used_source, used_output, index = [], False, False, 0
    while index < len(arguments):
        value = arguments[index]
        original = compile_record["file"]
        if value == original or (not used_source and value.endswith(pathlib.Path(original).name)):
            command.append(str(source))
            used_source = True
        elif value == "-o":
            command.extend(["-o", str(output)])
            used_output = True
            index += 1
        elif value in ("-MJ", "-MF", "-MT"):
            index += 1          # do not overwrite the records later builders read
        elif value in ("-MD", "-MP"):
            pass
        else:
            command.append(value)
        index += 1
    if not used_source:
        fail(f"the compile record names no source to replace: {compile_record['file']}")
    if not used_output:
        fail("the compile record has no -o to replace")
    for directory in includes:
        command.extend(["-I", str(directory)])
    command.extend(defines)
    # The archive is force-loaded, not part of the kernel's own warning sweep.
    command.extend(["-I", str(ROOT / ".cache/xnu-work/osfmk"), "-w"])
    return command


def compile_source(compile_record, source, output, includes=(), defines=()):
    source = pathlib.Path(source)
    if not source.is_file():
        fail(f"{source} is missing")
    command = compiler_command(compile_record, source, output, includes, defines)
    print(f"ufs: compile {source.name}", flush=True)
    run(command, cwd=compile_record["directory"])
    if not pathlib.Path(output).is_file():
        fail(f"the compiler did not produce {output}")
    return pathlib.Path(output)


def macho_symbols(raw):
    """The object's Mach-O symbol table: (image, [(name, address, kind)])."""
    sys.path.insert(0, str(ROOT / "kits/NativePrelink"))
    from native_prelink import macho

    image = macho(raw)
    symbol_command = next(offset for command, offset, _ in image["commands"] if command == 2)
    symbol_offset, symbol_count, string_offset, _ = struct.unpack_from("<4I", raw, symbol_command + 8)
    symbols = []
    for index in range(symbol_count):
        name_offset, kind, section, _, address = struct.unpack_from("<IBBHQ", raw, symbol_offset + index * 16)
        symbols.append((raw[string_offset + name_offset:raw.index(b"\0", string_offset + name_offset)].decode(),
                        address, kind))
    return image, symbols


def storage_bindings(objects):
    """The subset of the committed original-storage table this build actually imports."""
    document = json.loads(BINDINGS.read_text())
    if document.get("original_kernelcache_sha256") != KERNELCACHE_SHA256:
        fail(f"{BINDINGS.relative_to(ROOT)} was derived from kernelcache "
             f"{document.get('original_kernelcache_sha256')}, board.toml pins {KERNELCACHE_SHA256}")
    if document.get("original_family_text_sha256") != STORAGE_FAMILY_TEXT_SHA256:
        fail(f"{BINDINGS.relative_to(ROOT)} original storage executable "
             f"{document.get('original_family_text_sha256')} != qualified {STORAGE_FAMILY_TEXT_SHA256}")
    if document.get("source_revision") != SOURCE_REVISION:
        fail(f"{BINDINGS.relative_to(ROOT)} was derived from IOStorageFamily "
             f"{document.get('source_revision')}, expected {SOURCE_REVISION}")
    if document.get("method_slots") != METHOD_SLOTS or document.get("pure_virtual_slots") != PURE_VIRTUAL_SLOTS:
        fail("the committed original storage dispatch shape changed")
    table = {name: int(value, 16) for name, value in document["bindings"].items()}

    imports = set()
    for object_file in objects:
        _, symbols = macho_symbols(pathlib.Path(object_file).read_bytes())
        imports |= {name for name, _, kind in symbols if kind == 1 and name.startswith(BINDING_PREFIXES)}
    missing = sorted(imports - set(table))
    if missing:
        fail(f"the compiled driver imports original IOStorageFamily symbols the binding table lacks: {missing}")
    if not imports:
        fail("the compiled driver imports no original IOStorageFamily symbols; the layout or source changed")
    assembly = "".join(f".globl {name}\n.set {name}, {table[name]:#x}\n" for name in sorted(imports))
    print(f"ufs: binding {len(imports)} original IOStorageFamily imports "
          f"(of {len(table)} pinned; vtable {document['vtable']}, metaclass {document['original_metaclass']})", flush=True)
    return assembly, {name: table[name] for name in sorted(imports)}


def compile_bindings(assembly):
    source = OUT / "original-storage-bindings.S"
    source.write_text(assembly)
    sdk = run(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"]).strip()
    target = OUT / "original-storage-bindings.o"
    run(["xcrun", "clang", "-target", "arm64-apple-ios17.0", "-isysroot", sdk, "-c", source, "-o", target])
    return target


def archive(objects):
    ARCHIVE.unlink(missing_ok=True)
    command = (["xcrun"] if shutil.which("xcrun") else []) + ["ar", "rcs", ARCHIVE]
    run(command + [str(obj) for obj in objects])
    if not ARCHIVE.is_file():
        fail(f"no archive was produced at {ARCHIVE}")
    print(f"ufs: {ARCHIVE.relative_to(ROOT)} ({ARCHIVE.stat().st_size} bytes)", flush=True)


def report(bindings, sources):
    document = {
        "archive_sha256": sha256(ARCHIVE),
        "source_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in sources},
        "note": "ruyi NativeUfs block driver from the UniXNU submodule; original IOStorageFamily dispatch "
                "bound to the pinned prelink addresses; verified-root policy set empty (fail closed)",
        "original_storage_bindings_sha256": sha256(BINDINGS),
        "original_family_text_sha256": STORAGE_FAMILY_TEXT_SHA256,
        "verified_root_policy_count": 0,
        "bindings": {name: hex(value) for name, value in sorted(bindings.items())},
    }
    REPORT.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"ufs: {REPORT.relative_to(ROOT)}: archive sha256 {document['archive_sha256']}, "
          f"{len(sources)} sources, {len(bindings)} original bindings", flush=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if not DRIVER.is_dir():
        fail(f"{DRIVER.relative_to(ROOT)} is missing: the UniXNU NativeUfs submodule was not checked out")
    if not (VERIFIED_ROOT / "src/verified_root.c").is_file():
        fail(f"{VERIFIED_ROOT.relative_to(ROOT)} is missing: the UniXNU VerifiedRoot submodule was not checked out")
    if not (STORAGE_INCLUDE / "IOKit/storage/IOBlockStorageDevice.h").is_file():
        fail(f"{STORAGE_INCLUDE.relative_to(ROOT)} is missing; the kernel step prepares it from "
             "refer/IOStorageFamily before the archive builders run (unixnu fetch IOStorageFamily)")
    cpp_record = record(CPP_RECORD)
    c_record = record(C_RECORD)
    print(f"ufs: compile records {CPP_RECORD.name} ({len(cpp_record['arguments'])} arguments) and "
          f"{C_RECORD.name} ({len(c_record['arguments'])} arguments)", flush=True)

    ufs_includes = [STORAGE_INCLUDE, DRIVER, GLUE, VERIFIED_ROOT / "include"]
    ufs_defines = ["-DNATIVE_VERIFIED_BLOCK=1", "-DNATIVE_VERIFIED_BLOCK_MULTI_POLICY=1", "-mgeneral-regs-only"]
    driver = compile_source(cpp_record, DRIVER / "NativeUfs.cpp", OUT / "NativeUfs.o", ufs_includes, ufs_defines)

    kit_sources = [VERIFIED_ROOT / "src/verified_root.c", VERIFIED_ROOT / "adapters/xnu_crypto.c"]
    kit_objects = [
        compile_source(c_record, source, OUT / (source.stem + ".o"),
                       [VERIFIED_ROOT / "include"], []) for source in kit_sources
    ]

    glue = compile_source(cpp_record, GLUE / "RuyiUfsGlue.cpp", OUT / "RuyiUfsGlue.o",
                          [GLUE, VERIFIED_ROOT / "include"], ["-mgeneral-regs-only"])

    assembly, bindings = storage_bindings([driver])
    bindings_object = compile_bindings(assembly)

    archive([driver, glue, *kit_objects, bindings_object])
    sources = [DRIVER / "NativeUfs.cpp", GLUE / "RuyiUfsGlue.cpp", GLUE / "VerifiedBlockPolicy.h",
               BINDINGS, *kit_sources, VERIFIED_ROOT / "include/verified_root.h"]
    sources += sorted(path for path in DRIVER.iterdir() if path.suffix in (".h",) and path.is_file())
    report(bindings, sources)
    print("ufs: official NativeUfs archive ready for the kernel link", flush=True)


if __name__ == "__main__":
    main()
