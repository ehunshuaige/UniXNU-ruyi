#!/usr/bin/env python3
"""Build the ruyi display driver archive the kernel link expects.

The kernel's iokit/Kernel/IOPlatformExpert.cpp is not the driver -- it is only the
compile line borrowed here: XNU's own build records the exact compiler command for every
object as a clang `-MJ` fragment (IOPlatformExpert.cpo.json, left behind by the kernel
bootstrap pass), and this builder runs that command with the source and output swapped
for each of the driver's translation units.  That way the display code is compiled with
the same target, headers, ABI flags and deployment target as the kernel it links into,
which matters for C++ classes registered with the kernel's own meta-class system.

The archive is then post-processed and reported the way the kernel build verifies it:

    build/native-legacy-display/libNativeLegacyDisplay.late.a
    build/native-legacy-display/build-report.json
        archive_sha256   sha256 of the archive
        source_sha256    {path relative to the build tree: sha256}

The class files' C++ static initialisers are taken out of __DATA,__mod_init_func and
globalised: OSRuntimeInitializeCPP() runs the main kernel's __mod_init_func section at
OSlibkernInit(), long before IOKitBSDInit(), but these classes must be registered from
native_legacy_display_late_probe() (bsd_autoconf, after IOKitBSDInit) because the
services they define are published into the live registry.  RuyiDisplayEntry.cpp calls
the two initialisers by hand between OSMetaClass::preModLoad() and postModLoad().
(scripts/build-native-mobile-display.py and the sheng builder do the same.)

Run by `unixnu build kernel` through [kernel] archives of board.toml, with the build
tree as the working directory; the kernel bootstrap pass must have written the compile
record first.
"""
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "native-legacy-display"
DRIVER = ROOT / "drivers" / "RuyiDisplay"
ARCHIVE = OUT / "libNativeLegacyDisplay.late.a"
REPORT = OUT / "build-report.json"

# Where the kernel build leaves the compile records of the ruyi target.
RECORD = ROOT / "build" / "xnu" / "ios-obj" / "DEVELOPMENT_ARM64_BCM2837" / "iokit" / "DEVELOPMENT" / "IOPlatformExpert.cpo.json"

# (source, object stem, C++ static initialiser to move out of __mod_init_func, or None)
SOURCES = [
    ("RuyiIOSurface.cpp", "RuyiIOSurface", "__GLOBAL__sub_I_RuyiIOSurface.cpp"),
    ("RuyiDisplay.cpp", "RuyiDisplay", "__GLOBAL__sub_I_RuyiDisplay.cpp"),
    ("RuyiDisplayEntry.cpp", "RuyiDisplayEntry", None),
]


def fail(message):
    print(f"display: {message}", file=sys.stderr, flush=True)
    raise SystemExit(message)


def run(command, cwd=None):
    """Run and surface the child's output when it fails; the CI log is the only view."""
    result = subprocess.run([str(part) for part in command], cwd=cwd, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        print(f"display: command failed ({result.returncode}): {' '.join(str(part) for part in command[:6])} ...",
              file=sys.stderr, flush=True)
        print((result.stdout or "")[-6000:], file=sys.stderr, flush=True)
        print((result.stderr or "")[-6000:], file=sys.stderr, flush=True)
        raise SystemExit(f"display: build step failed ({result.returncode})")
    return result.stdout


def sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def find_record():
    if RECORD.is_file():
        return RECORD
    candidates = sorted((ROOT / "build" / "xnu" / "ios-obj").glob("*/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json"))
    printed = ", ".join(str(c.relative_to(ROOT)) for c in candidates) or "none"
    fail(f"no compile record at {RECORD.relative_to(ROOT)} (candidates: {printed}); "
         "the kernel bootstrap pass did not run")


def llvm_tool(name):
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/opt/llvm", "/usr/local/opt/llvm"):
        candidate = pathlib.Path(prefix) / "bin" / name
        if candidate.is_file():
            return str(candidate)
    fail(f"{name} is required (brew install llvm)")


def compiler_command(record, source, output):
    """The kernel's own command for IOPlatformExpert.cpp, with our file and output."""
    arguments = record["arguments"]
    command, used_source, used_output, index = [], False, False, 0
    while index < len(arguments):
        value = arguments[index]
        if value == record["file"] or (not used_source and value.endswith("IOPlatformExpert.cpp")):
            command.append(str(source))
            used_source = True
        elif value == "-o":
            command.extend(["-o", str(output)])
            used_output = True
            index += 1
        elif value in ("-MJ", "-MF", "-MT"):
            # Compile-database and dependency artefacts of the kernel's own object: do not
            # overwrite the record every later archive builder reads.
            index += 1
        elif value in ("-MD", "-MP"):
            pass     # (their arguments, if any, are handled by the flags above)
        else:
            command.append(value)
        index += 1
    if not used_source:
        fail(f"the compile record names no source to replace: {record['file']}")
    if not used_output:
        fail("the compile record has no -o to replace")
    return command


def compile_source(record, filename, stem):
    source = DRIVER / filename
    if not source.is_file():
        fail(f"{source.relative_to(ROOT)} is missing")
    raw = OUT / f"{stem}.o"
    command = compiler_command(record, source, raw)
    # The archive is force-loaded, not part of the kernel's own warning sweep, and the
    # driver keeps the HackiOS sources' warning level.  Add the driver directory for
    # "RuyiIOSurface.h" and the osfmk source tree like the sheng display builder does.
    command.extend([
        "-I", str(DRIVER),
        "-I", str(ROOT / ".cache/xnu-work/osfmk"),
        "-w",
    ])
    print(f"display: compile {filename}")
    run(command, cwd=record["directory"])
    if not raw.is_file():
        fail(f"the compiler did not produce {raw}")
    return raw


def static_initialiser(object_file, expected):
    """The object's only C++ static initialiser, checked against the name the entry file calls."""
    nm = llvm_tool("llvm-nm")
    output = run([nm, "--defined-only", object_file])
    found = sorted({line.split()[-1] for line in output.splitlines()
                    if line.split() and "__GLOBAL__sub_I_" in line.split()[-1]})
    if expected not in found:
        fail(f"{object_file.name}: expected static initialiser {expected}, found {found or 'none'}; "
             "the entry file's symbol names must match the compiler's")
    return expected


def late_object(raw, symbol):
    """Remove __DATA,__mod_init_func and expose the initialiser, as a kext's own would be."""
    late = raw.with_name(raw.stem + ".late.o")
    objcopy = llvm_tool("llvm-objcopy")
    run([objcopy, "--remove-section=__DATA,__mod_init_func", f"--globalize-symbol={symbol}", raw, late])
    if not late.is_file():
        fail(f"llvm-objcopy did not produce {late}")
    return late


def build_archive(objects):
    archive = shutil.which("xcrun")
    command = [archive, "ar", "rcs", str(ARCHIVE)] if archive else ["ar", "rcs", str(ARCHIVE)]
    ARCHIVE.unlink(missing_ok=True)
    run(command + [str(obj) for obj in objects])
    if not ARCHIVE.is_file():
        fail(f"no archive was produced at {ARCHIVE}")
    return ARCHIVE


def report():
    sources = sorted(path.name for path in DRIVER.iterdir() if path.is_file())
    document = {
        "archive_sha256": sha256(ARCHIVE),
        "source_sha256": {f"drivers/RuyiDisplay/{name}": sha256(DRIVER / name) for name in sources},
        "note": "ruyi display driver: MiniIOMobileFramebuffer + MiniIOSurface adapted from HackiOS, "
                "backed by the boot frame buffer (vc_info vinfo)",
    }
    REPORT.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"display: {ARCHIVE.relative_to(ROOT)} ({ARCHIVE.stat().st_size} bytes), "
          f"{len(sources)} sources, sha256 {document['archive_sha256']}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    record_path = find_record()
    record = json.loads(record_path.read_text().strip().rstrip(","))
    print(f"display: compile record {record_path.relative_to(ROOT)} "
          f"({len(record['arguments'])} arguments, directory {record['directory']})")
    objects = []
    for filename, stem, initialiser in SOURCES:
        raw = compile_source(record, filename, stem)
        if initialiser is None:
            objects.append(raw)
        else:
            symbol = static_initialiser(raw, initialiser)
            objects.append(late_object(raw, symbol))
    build_archive(objects)
    report()


if __name__ == "__main__":
    main()
