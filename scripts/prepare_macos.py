#!/usr/bin/env python3
"""Prepare the measured HK release, then package a local macOS application."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
TITLE = "0100B8A026F14000"

def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()

def command(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)

def atomic_link(path, target):
    temp = path.with_name(path.name + ".partial")
    if temp.is_symlink():
        temp.unlink()
    temp.symlink_to(os.path.relpath(target, path.parent), target_is_directory=target.is_dir())
    os.replace(temp, path)

def stage_inputs(source):
    packages = []
    for path in sorted(source.glob("*.nsp")):
        ids = re.findall(r"\[([0-9a-fA-F]{16})\]", path.name)
        if not ids:
            raise RuntimeError(f"NSP has no title ID tag: {path.name}")
        title = ids[0].upper()
        if title not in (TITLE, "0100B8A026F14800", "0100B8A026F15002"):
            raise RuntimeError(f"Unexpected game package: {path.name}")
        with path.open("rb") as f:
            if f.read(4) != b"PFS0":
                raise RuntimeError(f"Invalid NSP header: {path.name}")
        print(f"Fingerprinting {path.name}", flush=True)
        packages.append(dict(source=str(path.resolve()), title=title, size=path.stat().st_size, sha256=sha(path)))
    titles = [p["title"] for p in packages]
    if titles.count(TITLE) != 1 or titles.count("0100B8A026F14800") != 1 or len(titles) != len(set(titles)):
        raise RuntimeError("Need exactly one HK base NSP and the measured 1.0.1 update; DLC is optional.")
    digest = hashlib.sha256(json.dumps(packages, sort_keys=True).encode()).hexdigest()
    cache = ROOT / "data/builds" / digest[:16]
    inputs = cache / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    for p in packages:
        version = "v65536" if p["title"].endswith("4800") else "v0"
        dest = inputs / f'FFR [{p["title"]}][{version}].nsp'
        atomic_link(dest, Path(p["source"]))
    (cache / "source-packages.json").write_text(json.dumps(dict(digest=digest, packages=packages), indent=2) + "\n")
    return inputs, inputs / f"{TITLE}_build"

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=ROOT)
    ap.add_argument("--keys", type=Path)
    ap.add_argument("--refresh-shader-tables", action="store_true")
    args = ap.parse_args()
    if platform.machine() != "arm64" or sys.platform != "darwin":
        raise RuntimeError("This port requires native arm64 Python on an Apple silicon Mac.")
    keys = (args.keys or args.source / "prod.keys").resolve()
    if not keys.is_file():
        raise RuntimeError(f"Missing local keys file: {keys}")
    nxport = ROOT / "target/release/nxport"
    rustlib = ROOT / "target/release/libnxabi.a"
    if not nxport.is_file() or not rustlib.is_file():
        raise RuntimeError("Build the Rust workspace in release mode first.")
    inputs, work = stage_inputs(args.source.resolve())
    (work / "run").mkdir(parents=True, exist_ok=True)
    work_lock = (work / "run/ffr.lock").open("a+")
    try:
        fcntl.flock(work_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("请先退出当前游戏或另一项构建，再准备资源。") from None
    vendor = ROOT / "vendor/nxnative"
    env = os.environ
    env["FFR_RUST_LIB"] = str(rustlib)
    # The Mac target uses the complete upstream Metal 4 NVN backend.
    # An empty override selects it rather than the OT2 classic-Metal trial.
    env["M3_GFX_DIR"] = ""
    runtimes = subprocess.check_output(["dotnet", "--list-runtimes"], text=True)
    runtime = next((row.split("[", 1)[1].rstrip("]") for row in runtimes.splitlines()
                    if row.startswith("Microsoft.NETCore.App ")), None)
    if runtime is None:
        raise RuntimeError(".NET 10 runtime is missing.")
    env["DOTNET_ROOT"] = str(Path(runtime).parent.parent)
    env["DOTNET_CLI_HOME"] = str(ROOT / "data/tools/dotnet-home")
    env["NUGET_PACKAGES"] = str(ROOT / "data/tools/nuget")
    env["NuGetAudit"] = "false"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["J"] = env.get("J", "8")
    module_cache = ROOT / "data/tools/metal-module-cache"
    module_cache.mkdir(parents=True, exist_ok=True)
    env["FFR_METAL_MODULE_CACHE"] = str(module_cache)
    env["CLANG_MODULE_CACHE_PATH"] = str(module_cache)
    # Both the predecoded shader archives and additional IoStore/pak entries
    # may need the game's own Oodle decoder during the subsequent split.
    env["DM_OODLE_HOST"] = str(work / "tools/dmhost")
    env["DM_OODLE_MODULES"] = str(work / "patched_v65536")
    if not env.get("DEVELOPER_DIR"):
        candidates = [Path("/Applications/Xcode.app/Contents/Developer"),
                      ROOT.parents[1] / "Applications/Xcode.app/Contents/Developer"]
        selected = next((p for p in candidates if p.is_dir()), None)
        if selected is not None:
            env["DEVELOPER_DIR"] = str(selected)
    metal = subprocess.run(["/usr/bin/xcrun", "-sdk", "macosx", "metal", "--version"], capture_output=True)
    if metal.returncode:
        toolchain = env.get("FFR_METAL_TOOLCHAIN")
        if not toolchain:
            mounts = Path("/private/var/run/com.apple.security.cryptexd/mnt")
            candidates = sorted(mounts.glob("com.apple.MobileAsset.MetalToolchain-*/Metal.xctoolchain"))
            toolchain = str(candidates[-1]) if candidates else ""
        if not toolchain or not (Path(toolchain) / "usr/bin/metal").is_file():
            raise RuntimeError("Install the Metal Toolchain component in the selected Xcode.")
        command(Path(toolchain) / "usr/bin/metal", "--version")
        env["FFR_METAL_TOOLCHAIN"] = toolchain
        env["FFR_MACOS_SDKROOT"] = subprocess.check_output(["/usr/bin/xcrun", "-sdk", "macosx", "--show-sdk-path"], text=True).strip()
        env["PATH"] = str(ROOT / "scripts/toolshim") + os.pathsep + env.get("PATH", "")
        print("Using the installed Metal toolchain directly for this build.", flush=True)
    spec = importlib.util.spec_from_file_location("ffr_upstream_builder", vendor / "scripts/build_ipa_from_nsp.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    # Native interpreter identity avoids a sysctl probe denied in restricted builds.
    builder.apple_silicon = lambda: platform.machine() == "arm64"
    original_rewrite = builder.rewrite
    def rewrite(py, repo, exefs, patched, child_env, logs, profile):
        command(nxport, "inspect", exefs)
        original_rewrite(py, repo, exefs, patched, child_env, logs, profile)
        command(nxport, "seal", patched, exefs)
    builder.rewrite = rewrite
    original_split = builder.split_shaders
    def split(py, repo, roms, mains, package, child_env, logs, profile, provisional=False, oodle=None, allow_empty=False):
        table = profile.table("shader_map.tsv")
        empty = not table.exists() or not any(
            len(c := row.split("\t")) >= 4 and re.fullmatch(r"[0-9a-f]{16}", c[3])
            for row in table.read_text().splitlines() if not row.startswith("#"))
        # Adopt generated tables exactly once from this release's own archives.
        # Later runs compare all rows with the registered tables.
        result = original_split(py, repo, roms, mains, package, child_env, logs, profile,
                                provisional or empty or args.refresh_shader_tables, oodle, allow_empty)
        # Reuse the measured integer-output fact only for the exact shader key
        # actually extracted here. The source table remains reproducible.
        key = "c254eaa40e0c4f1e"
        if any(len(c := row.split("\t")) >= 4 and c[3] == key
               for row in table.read_text().splitlines() if not row.startswith("#")):
            manual = profile.table("shader_facts_manual.tsv")
            lines = manual.read_text().splitlines()
            if not any(row.startswith(key + "\t") for row in lines):
                with manual.open("a") as f:
                    f.write(key + '\t{"fout":{"0":"uint"}}\n')
                facts = profile.table("shader_facts.tsv")
                existing = facts.read_text().splitlines()
                header = [row for row in existing if row.startswith("#")]
                rows = dict(row.split("\t", 1) for row in existing if row.strip() and not row.startswith("#"))
                value = json.loads(rows.get(key, "{}"))
                value["fout"] = {"0": "uint"}
                rows[key] = json.dumps(value, sort_keys=True, separators=(",", ":"))
                facts.write_text("\n".join(header + [k + "\t" + rows[k] for k in sorted(rows)]) + "\n")
        return result
    builder.split_shaders = split
    command(sys.executable, vendor / "dm/gfx/tools/dmsh_chain.py", "--update-lock")
    command(sys.executable, vendor / "dm/gfx/tools/dmsh_chain.py", "--header")
    code = builder.cli([str(inputs), "-k", str(keys), "--platform", "mac", "--package", "v65536",
                        "--language", "zh-Hans", "--progress", "lines"])
    if code:
        raise RuntimeError(f"Mac preparation failed (exit {code}); see {work / 'logs'}.")
    publish(work, nxport, vendor)
    work_lock.close()

def publish(work, nxport, vendor):
    """Publish only validated outputs; caller holds run/ffr.lock."""
    patched = work / "patched_v65536"
    command(nxport, "verify", patched)
    chain = subprocess.check_output([sys.executable, str(vendor / "dm/gfx/tools/dmsh_chain.py")], text=True).strip()
    libraries = work / "shader/cache" / f"b-{chain}"
    report = json.loads((libraries / "_b_build.json").read_text())
    failed = [row for row in report if row.get("st") != "ok"]
    expected = {c[3] for row in (vendor / "dm/games" / TITLE / "shader_map.tsv").read_text().splitlines()
                if len(c := row.split("\t")) >= 4 and re.fullmatch(r"[0-9a-f]{16}", c[3])}
    missing = [key for key in expected if not shader_ready(libraries, key, chain)]
    if not report or failed or missing or len(report) != len(expected) or {row.get("key") for row in report} != expected:
        (work / "shader" / f"mac-b-{chain}.key").unlink(missing_ok=True)
        raise RuntimeError(f"Shader build is incomplete: {len(failed)} failures, {len(missing)} missing outputs; no application published. See {work / 'logs/dmsh_build.log'}.")
    stamp = dict(row.split("\t", 1) for row in (libraries / "inputs.tsv").read_text().splitlines()
                 if row.strip() and not row.startswith("#"))
    if stamp.get("game") != TITLE or not stamp.get("inputs"):
        raise RuntimeError("Shader library stamp does not match this game.")
    validate_shader_metadata(work, vendor, libraries, len(expected))
    host = package_runtime(work)
    profile = vendor / "dm/games" / TITLE
    snapshot = work / "runtime-profile"
    partial = work / "runtime-profile.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()
    for path in profile.glob("*.tsv"):
        shutil.copy2(path, partial / path.name)
    if snapshot.exists():
        shutil.rmtree(snapshot)
    partial.rename(snapshot)
    config = dict(title_id=TITLE, version="1.0.1", platform="macos-arm64",
                  renderer="metal4", minimum_macos="26.0",
                  host=os.path.relpath(host, work), host_sha256=sha(host), patched="patched_v65536",
                  romfs="extract/v65536/RomFS", msl="shader/cache", font="font",
                  profile="runtime-profile", chain=chain, shader_inputs=stamp["inputs"])
    config.update({f"sha256:{p.name}": sha(p) for p in sorted(snapshot.glob("*.tsv"))})
    runtime_temp = work / "macos-runtime.partial.tsv"
    runtime_temp.write_text("".join(f"{k}\t{v}\n" for k,v in config.items()))
    runtime_temp.replace(work / "macos-runtime.tsv")
    package_app(work, nxport)
    atomic_link(ROOT / "data/current", work)
    print(f"Prepared: {ROOT / 'dist/FINAL FANTASY RESONANCE.app'}", flush=True)

def shader_ready(libraries, key, chain):
    metadata = libraries / f"{key}_b.json"
    if not metadata.is_file():
        return False
    if (libraries / f"{key}_b.metallib").is_file():
        return True
    # A proven layer-only geometry stage runs in the paired vertex entry.
    # Match the classic runtime's accepted metadata rather than require a
    # standalone Metal geometry library, which does not exist for this case.
    data = json.loads(metadata.read_text())
    layer = data.get("gs_slice", {})
    return (data.get("chain") == chain and data.get("stage") == "gs"
            and isinstance(layer.get("loc"), int) and 0 <= layer["loc"] < 32
            and layer.get("comp") in ("x", "y", "z", "w"))

def validate_shader_metadata(work, vendor, libraries, expected_count):
    """Use the production native parser before publishing either application."""
    checker = work / "tools/validate-macos-shaders"
    report = work / "logs/native-shader-metadata.json"
    command("xcrun", "-sdk", "macosx", "clang", "-O2", "-fobjc-arc",
            "-mmacosx-version-min=26.0",
            "-fmodules-cache-path=" + str(ROOT / "data/tools/clang-module-cache"),
            "-I" + str(vendor / "dm/gfx/nvn"),
            ROOT / "platform/macos/ValidateShaders.m", vendor / "dm/gfx/nvn/nvn_refl.m",
            "-framework", "Foundation", "-framework", "Metal", "-o", checker)
    command(checker, libraries.resolve(), report.resolve())
    result = json.loads(report.read_text())
    if result.get("metadata_count") != expected_count or result.get("failure_count") != 0:
        raise RuntimeError(f"Native shader metadata validation failed; no application published. See {report}.")


def package_notices(resources):
    """Retain dependency copyright and license texts with the local binaries."""
    vendor = ROOT / "vendor/nxnative"
    paths = ["LICENSE", "THIRD-PARTY-NOTICES.md",
             "dm/third_party/vulkan-headers/LICENSE.md",
             "dm/third_party/macnativegames/LICENSE",
             "dm/third_party/stb_vorbis/LICENSE",
             "dm/host/m3gfx/third_party/bcdec/LICENSE",
             "dm/third_party/libvpx/LICENSE",
             "dm/third_party/ryujinx/src/Spv.Generator/LICENSE",
             "dm/third_party/ryujinx/LICENSE.txt",
             "dm/third_party/flip/LICENSE"]
    for name in paths:
        dest = resources / "Licenses/nxnative" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vendor / name, dest)


def package_runtime(work):
    """Give the native AppKit process a stable macOS bundle identity."""
    app = work / "tools/FFR Runtime.app"
    partial = work / "tools/FFR Runtime.partial.app"
    if partial.exists():
        shutil.rmtree(partial)
    executable = partial / "Contents/MacOS/FFRGame"
    executable.parent.mkdir(parents=True)
    resources = partial / "Contents/Resources"
    resources.mkdir()
    package_notices(resources)
    shutil.copy2(work / "tools/dmhost", executable)
    has_icon = package_icon(work, resources)
    info = dict(CFBundleName="FINAL FANTASY RESONANCE",
                CFBundleDisplayName="FINAL FANTASY RESONANCE",
                CFBundleIdentifier="local.ffr.hk.runtime",
                CFBundleVersion="1", CFBundleShortVersionString="1.0.1",
                CFBundleExecutable="FFRGame", CFBundlePackageType="APPL",
                LSMinimumSystemVersion="26.0", NSHighResolutionCapable=True,
                LSApplicationCategoryType="public.app-category.games")
    if has_icon:
        info["CFBundleIconFile"] = "GameIcon.icns"
    with (partial / "Contents/Info.plist").open("wb") as f:
        plistlib.dump(info, f)
    command("codesign", "--force", "--sign", "-", partial)
    command("codesign", "--verify", "--deep", "--strict", partial)
    if app.exists():
        shutil.rmtree(app)
    partial.rename(app)
    return app / "Contents/MacOS/FFRGame"

def package_icon(work, resources):
    source = work / "extract/v65536/icon.jpg"
    if not source.is_file():
        return False
    cache = work / "tools/GameIcon.icns"
    stamp = cache.with_suffix(".sha256")
    digest = sha(source)
    if not cache.is_file() or not stamp.is_file() or stamp.read_text().strip() != digest:
        iconset = work / "tools/GameIcon.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = size * scale
                name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
                command("sips", "-s", "format", "png", "-z", pixels, pixels,
                        source, "--out", iconset / name, stdout=subprocess.DEVNULL)
        command("iconutil", "-c", "icns", "-o", cache, iconset)
        stamp.write_text(digest + "\n")
    shutil.copy2(cache, resources / cache.name)
    return True

def package_app(work, nxport):
    dest = ROOT / "dist"
    app = dest / "FINAL FANTASY RESONANCE.app"
    temporary = dest / "FINAL FANTASY RESONANCE.partial.app"
    if temporary.exists():
        shutil.rmtree(temporary)
    macos = temporary / "Contents/MacOS"
    resources = temporary / "Contents/Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    package_notices(resources)
    shutil.copy2(nxport, macos / "nxport")
    command("xcrun", "swiftc", "-O", "-target", "arm64-apple-macos26.0",
            "-module-cache-path", ROOT / "data/tools/swift-module-cache",
            ROOT / "platform/macos/Launcher.swift", "-o", macos / "FFRLauncher")
    has_icon = package_icon(work, resources)
    info = dict(CFBundleName="FINAL FANTASY RESONANCE", CFBundleDisplayName="FINAL FANTASY RESONANCE",
                CFBundleIdentifier="local.ffr.hk.macos", CFBundleVersion="1",
                CFBundleShortVersionString="1.0.1", CFBundleExecutable="FFRLauncher",
                CFBundlePackageType="APPL", LSMinimumSystemVersion="26.0",
                LSApplicationCategoryType="public.app-category.games",
                NSHighResolutionCapable=True, LSUIElement=True,
                FFRGameDataPath=str(work.resolve()))
    if has_icon:
        info["CFBundleIconFile"] = "GameIcon.icns"
    with (temporary / "Contents/Info.plist").open("wb") as f:
        plistlib.dump(info, f)
    command("codesign", "--force", "--sign", "-", macos / "nxport")
    command("codesign", "--force", "--sign", "-", temporary)
    command("codesign", "--verify", "--deep", "--strict", temporary)
    old = dest / "FINAL FANTASY RESONANCE.previous.app"
    if old.exists():
        shutil.rmtree(old)
    if app.exists():
        app.rename(old)
    temporary.rename(app)
    if old.exists():
        shutil.rmtree(old)

if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        print(f"[FFR] {exc}", file=sys.stderr)
        sys.exit(1)
