# FINAL FANTASY RESONANCE · iPadOS

港版 Switch 1.0.1（本体 Title ID `0100B8A026F14000`）的 iPad 构建工程。目标设备为支持 Metal 4、运行 iPadOS 26 或更新版本的 iPad。构建流程在 Apple Silicon Mac 上准备资源，随后生成 iPad 专用的 Metal 着色器库、Xcode 工程和未签名 IPA。

## 准备

- Apple Silicon Mac，macOS 26 或更新版本，完整 Xcode 及 Metal Toolchain。
- Rust、.NET 10 SDK、原生 ARM64 Python 3.14 或更新版本、XcodeGen、SPIRV-Cross 和 zstd。
- 自行提供港版本体、1.0.1 更新包及对应的 `prod.keys`。DLC 可选。输入和生成文件均保存在本地。

如尚未安装 Xcode 的 Metal Toolchain，运行：

```sh
xcodebuild -downloadComponent MetalToolchain
```

## 构建

把本体和更新 NSP 放在同一目录。文件名应分别包含 `[0100B8A026F14000]` 和 `[0100B8A026F14800]`。可选 DLC 文件名应包含 `[0100B8A026F15002]`。以下命令在仓库根目录运行，将示例路径替换为自己的本地路径。

```sh
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

dotnet publish vendor/nxnative/dm/gfx/tools/dmsh_xlate/dmsh_xlate.csproj \
  -c Release -o vendor/nxnative/dm/gfx/tools/dmsh_xlate_bin -p:DebugType=None

bash scripts/build_macos.sh --source /path/to/local-nsp --keys /path/to/prod.keys
python scripts/prepare_ipad.py
```

第一次运行会从本地游戏文件生成着色器表。修改着色器转换工具后，重新编译转换器并运行 Mac 资源准备，随后再构建 iPad 版。iPad 着色器编译可在同一套工具链和输入下续跑。用 `--jobs N` 调整并行任务数，或用 `--shaders-only` 只编译着色器。

## 在 iPad 上运行

打开 `build-ipad/xcode/DmIOS.xcodeproj`，在 Xcode 中选择自己的开发团队、包标识和已连接的 iPad，然后运行 Release 配置。工程请求 Increased Memory Limit。也可以在生成工程前通过 `DM_TEAM_ID` 和 `DM_BUNDLE_ID` 提供自己的签名设置。

未签名 IPA 位于 `build-ipad/FinalFantasyResonance-HK-1.0.1-iPad.ipa`，需要签名后才能在设备上安装。首次构建需要为本地解包、着色器缓存、Xcode 构建和 IPA 留出充足的磁盘空间。

源代码的依赖和许可见[依赖记录](docs/dependency-provenance.md)。
