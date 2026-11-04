#!/usr/bin/env python3
"""Build FFR HK for a Metal 4 iPad from this project's prepared Mac inputs."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
from prepare_macos import ROOT, TITLE, package_notices, shader_ready


VENDOR = ROOT / 'vendor/nxnative'
DM = VENDOR / 'dm'
OUT = ROOT / 'build-ipad'
sys.path[:0] = [str(DM / 'gfx/tools'), str(DM / 'ios/tools')]
import dmsh_fetch
import dmsh_vin
import ios_metal
from xcode_projects import signing_settings


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def xcrun(*args):
    return run('/usr/bin/xcrun', '-sdk', 'iphoneos', *args, capture_output=True, text=True).stdout.strip()


def read_tsv(path):
    return dict(line.split('\t', 1) for line in path.read_text().splitlines() if line and not line.startswith('#'))


def find_metal():
    metal = os.environ.get('FFR_METAL_TOOLCHAIN')
    if metal:
        return Path(metal) / 'usr/bin/metal'
    try:
        xcrun('metal', '--version')
        return Path(xcrun('--find', 'metal'))
    except subprocess.CalledProcessError:
        mounted = Path('/private/var/run/com.apple.security.cryptexd/mnt')
        tools = sorted(mounted.glob('com.apple.MobileAsset.MetalToolchain-*/Metal.xctoolchain/usr/bin/metal'))
        if tools:
            return tools[-1]
        raise RuntimeError('Install the Xcode Metal Toolchain with xcodebuild -downloadComponent MetalToolchain.') from None


def build_shaders(source, target, profile, chain, workers):
    out = target / f'b-{chain}'
    out.mkdir(parents=True, exist_ok=True)
    metal = find_metal()
    sdk = xcrun('--show-sdk-path')
    # The Metal driver compiles and links in one invocation. Keep the shared
    # iOS shader options and direct all transient files into the iPad build.
    options = [arg for arg in ios_metal.METAL[4:] if arg != '-c']
    modules = OUT / 'metal-module-cache'
    command = [str(metal), '-isysroot', sdk, f'-fmodules-cache-path={modules}', *options]
    version = run(*command, '--version', capture_output=True, text=True).stdout
    recipe = 'metal ' + ' '.join(options)
    compiler = '\n'.join([recipe, xcrun('--show-sdk-build-version'), *version.splitlines()[:3]])
    fingerprint = hashlib.sha256((compiler + (source / 'inputs.tsv').read_text()).encode()).hexdigest()
    keyfile = target / 'shader-build-key'
    if not keyfile.exists() or keyfile.read_text() != fingerprint:
        shutil.rmtree(out)
        out.mkdir()
        keyfile.write_text(fingerprint)

    names = set(os.listdir(source))
    keys = {row.split('\t')[3] for row in (profile / 'shader_map.tsv').read_text().splitlines()
            if re.fullmatch(r'(?:[^\t]*\t){3}[0-9a-f]{16}(?:\t.*)?', row) and not row.startswith('#')}
    base_metadata = {name[:-7] for name in names if name.endswith('_b.json')}
    if not keys or keys != base_metadata or any(not shader_ready(source, key, chain) for key in keys):
        raise RuntimeError('The prepared Mac shader set is incomplete. Rebuild the Mac inputs first.')
    libraries = sorted(name[:-9] for name in names if name.endswith('.metallib'))
    metadata_names = sorted(name for name in names if re.fullmatch(r'[0-9a-f]{16}_b\w*\.json', name))
    outputs = set(os.listdir(out))
    pending = [stem for stem in libraries if f'{stem}.metallib' not in outputs]
    stamp = out / 'inputs.tsv'
    if not pending and stamp.is_file() and set(metadata_names) <= outputs:
        print(f'iPad shaders: {len(libraries):,} libraries ready, reused.', flush=True)
        return
    stamp.unlink(missing_ok=True)
    bf_want = {line.split()[0] for line in (profile / 'bf_want.tsv').read_text().splitlines()
               if line.strip() and not line.startswith('#')}
    temp = target / 'tmp'
    temp.mkdir(exist_ok=True)

    def compile_shader(stem):
        msl = source / f'{stem}.metal'
        temp_msl = temp / msl.name
        temp_lib = temp / f'{stem}.metallib'
        if msl.name not in names:
            key, suffix = stem.rsplit('_', 1)
            base = (source / f'{key}_b.metal').read_text()
            metadata = json.loads((source / f'{key}_b.json').read_text())
            if suffix == 'bf':
                text, info = dmsh_fetch.variant(base, metadata['bindings'], om_only_ok=key in bf_want)
                expected = metadata['fetch']
            elif suffix == 'bv':
                text, info = dmsh_vin.variant(base)
                expected = metadata['vin']['shadow']
            else:
                raise RuntimeError(f'Missing Metal source for {stem}.')
            if not text or info != expected:
                raise RuntimeError(f'Shader variant {stem} does not match its prepared reflection.')
            temp_msl.write_text(text)
            msl = temp_msl
        try:
            result = subprocess.run([*command, str(msl), '-o', str(temp_lib)], capture_output=True)
            if result.returncode:
                raise RuntimeError(f'{stem}: {result.stderr.decode(errors="replace")}')
            temp_lib.replace(out / temp_lib.name)
        finally:
            temp_msl.unlink(missing_ok=True)
            temp_lib.unlink(missing_ok=True)

    print(f'iPad shaders: {len(libraries):,} libraries, {len(pending):,} to compile ({workers} workers).', flush=True)
    failures = []
    with ThreadPoolExecutor(workers) as executor:
        for offset in range(0, len(pending), 1000):
            batch = [executor.submit(compile_shader, stem) for stem in pending[offset:offset + 1000]]
            for task in as_completed(batch):
                if error := task.exception():
                    failures.append(str(error))
                    print(error, file=sys.stderr, flush=True)
            count = offset + len(batch)
            print(f'iPad shaders: {count:,}/{len(pending):,} processed, {len(failures):,} failed.', flush=True)
    if failures:
        raise RuntimeError(f'{len(failures)} iPad shader libraries failed to compile.')
    for name in metadata_names:
        meta = json.loads((source / name).read_text())
        meta['chain_metal'] = recipe
        (out / name).write_text(json.dumps(meta, separators=(',', ':')))
    shutil.copyfile(source / 'inputs.tsv', temp / 'inputs.tsv')
    (temp / 'inputs.tsv').replace(stamp)


def prepare_project(output, work, config, profile, patched, shaders):
    project = output / 'xcode/DmIOS.xcodeproj'
    derived = output / 'DerivedData'
    signing = signing_settings(project)
    profiles = output / 'profiles'
    profiles.mkdir(exist_ok=True)
    staged_profile = profiles / TITLE
    # game.py uses the resolved directory name as the game's runtime ID.
    if staged_profile.is_symlink():
        staged_profile.unlink()
    elif staged_profile.exists():
        shutil.rmtree(staged_profile)
    shutil.copytree(profile, staged_profile)
    romfs = work / config['romfs']
    aoc = work / 'aoc'
    env = dict(os.environ, GAME=TITLE, GFXAPI='nvn', DM_DEVICE_FAMILY='2', DM_IOS_METAL4='1',
               DM_GAMES_FIRST='1', DM_GAMES_DIR=str(profiles), DM_LANG='zh-Hans',
               DM_DISPLAY_NAME='FINAL FANTASY RESONANCE', DM_APP_VERSION=config['version'],
               DM_BUNDLE_ID=os.environ.get('DM_BUNDLE_ID', signing.get('PRODUCT_BUNDLE_IDENTIFIER', 'org.ffr.hk.ipad')),
               DM_TEAM_ID=os.environ.get('DM_TEAM_ID', signing.get('DEVELOPMENT_TEAM', '')),
               DM_XCODE_PROJECT_DIR=str(project.parent), DM_XCODE_DERIVED_DATA=str(derived),
               W=str(output), PATCHED=str(patched), ROMFS=str(romfs), MSL_IOS=str(shaders),
               AOC=str(aoc) if aoc.is_dir() else '', FONT_PY=sys.executable,
               CLANG_MODULE_CACHE_PATH=str(output / 'clang-module-cache'), PYTHONDONTWRITEBYTECODE='1')
    env.pop('FFR_RUST_LIB', None)  # The Mac static library cannot be linked into an iOS executable.
    run('bash', DM / 'ios/tools/build_ipa.sh', '--prepare-xcode', env=env)
    package_notices(project.parent / 'Gen/dm')
    return project, derived, env


def package(work, config, profile, patched):
    shaders = OUT / 'shaders'
    project, derived, env = prepare_project(OUT, work, config, profile, patched, shaders)
    print(f'Building unsigned iPad application. See {OUT / "xcodebuild.log"}.', flush=True)
    with (OUT / 'xcodebuild.log').open('w') as log:
        run('xcodebuild', '-project', project, '-scheme', f'{env["DM_DISPLAY_NAME"]} ({TITLE})',
            '-configuration', 'Release', '-destination', 'generic/platform=iOS', '-derivedDataPath', derived,
            'CODE_SIGNING_ALLOWED=NO', 'CODE_SIGNING_REQUIRED=NO', 'DM_COPY_GAME_RESOURCES=NO', 'build',
            env=env, stdout=log, stderr=subprocess.STDOUT)
    app = derived / 'Build/Products/Release-iphoneos/DmIOS.app'
    name = 'Payload/DmIOS.app'
    folder = f'b-{config["chain"]}'
    maps = [f'{name}={app}', f'{name}/msl/{folder}={shaders / folder}', f'{name}/romfs={env["ROMFS"]}']
    if env['AOC']:
        maps.append(f'{name}/aoc={env["AOC"]}')
    skip = [arg for entry in ('_CodeSignature', 'embedded.mobileprovision', 'romfs', 'msl', 'aoc')
            for arg in ('--skip', f'{name}/{entry}')]
    ipa = OUT / f'FinalFantasyResonance-HK-{config["version"]}-iPad.ipa'
    run(sys.executable, DM / 'ios/tools/pack_ipa.py', ipa, '--store', '.pak:.ttf:.txt:.ucas:.utoc:.metallib',
        *skip, *maps, env=env)
    print(f'Xcode project: {project}\nUnsigned IPA: {ipa}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=min(os.cpu_count() or 8, 12))
    parser.add_argument('--shaders-only', action='store_true')
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    if not args.shaders_only:
        if not shutil.which('xcodegen'):
            parser.error('XcodeGen is required (brew install xcodegen).')
        try:
            import fontTools
        except ImportError:
            parser.error('fonttools is required in the Python environment running this script.')
    OUT.mkdir(exist_ok=True)
    temp = OUT / 'tmp'
    temp.mkdir(exist_ok=True)
    os.environ['TMPDIR'] = str(temp)
    with (OUT / '.build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepare(args)


def prepare(args):
    work = (ROOT / 'data/current').resolve(strict=True)
    config = read_tsv(work / 'macos-runtime.tsv')
    chain = config['chain']
    header = (DM / 'gfx/nvn/dmsh_chain.h').read_text()
    if config['title_id'] != TITLE or f'#define DMSH_CHAIN "{chain}"' not in header:
        raise RuntimeError('The prepared Mac build does not match this source tree. Run scripts/build_macos.sh.')
    patched = work / config['patched']
    run(ROOT / 'target/release/nxport', 'verify', patched)
    profile = work / config['profile']
    for name, digest in config.items():
        if name.startswith('sha256:') and hashlib.sha256((profile / name[7:]).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'The prepared profile changed: {name[7:]}')
    source = work / config['msl'] / f'b-{chain}'
    if read_tsv(source / 'inputs.tsv') != {'game': TITLE, 'inputs': config['shader_inputs']}:
        raise RuntimeError('The prepared shader stamp does not match the Mac build.')
    # Any re-preparation of the same Mac input version takes this lock exclusively.
    with (work / 'run/ffr.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        build_shaders(source, OUT / 'shaders', profile, chain, args.jobs)
        if not args.shaders_only:
            package(work, config, profile, patched)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f'iPad build failed: {error}')
