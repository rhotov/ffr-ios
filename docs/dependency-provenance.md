# Native dependency provenance

The native compatibility engine was copied from the locally available
OT2 Mobile checkout, upstream commit
36e1d18f30ec4a4d046ffa76a2c1f073b572576b, on 2026-11-03.
That checkout already contained local changes to OT2 profiles, its allocator,
iPad UI and build scripts. These local changes are preserved in the vendored
copy.
Source: https://github.com/leeashine/ot2-mobile
Its nxnative upstream: https://github.com/seedds/nxnative

The engine's C/Objective-C execution, kernel/services, rewriter and Metal
implementation are reused. They retain their existing licenses and notices
under vendor/nxnative. New resource identity, module seals and launch validation
live in the Rust workspace, connected to the engine through nxabi's C ABI.
Swift provides the local macOS launcher. Python only prepares resources and
coordinates offline tools.

FFR changes inside the dependency:

- HK profile and measured, build-gated allocator/FPS registrations.
- Optional Rust validation library in the native host's build and entry point.
- Include the Rust static library's contents in the host cache key.
- Backport dynamic texture/sampler argument arrays and c2 slot tables into
  the classic Metal backend, with resource lifetime and read/store handling.
- Map IJKL to the right stick on macOS, releasing both sticks on focus loss.
- Select native MSL 3.1 image atomics from actual SPIR-V instructions, and
  support their texture descriptors and reflection in both backends, including
  coherent pool-backed signed and unsigned buffers in the selected Metal 4 path.
- Parallelize independent C1 proofs while retaining the prior proof logic,
  deterministic output and failed-worker behavior.
- CPU and real-GPU regressions for the affected graphics paths.
- Default-disabled, bounded GPU diagnostics for indirect particle arguments,
  exact referenced vertex outputs and original sampled texture views.
- Preserve true SSA Phi incoming edges and prove guarded resource choices,
  including current-loop values, globally invariant components and fixed-word
  private-memory spills. Reachable unresolved handles fail translation. The
  shader-only build embeds all shader sources and its recipe for tool-lock
  checks, and a failed full batch retains independently runnable inputs.

The iPad target and its macOS preparation step select the Metal 4 NVN backend.
The repository preserves the vendored licenses and third-party notices.
The shader translator builds locally from the included source, with the
vendored Ryujinx.Common and Spv.Generator assemblies as dependencies.
Game files, keys, shaders, signing settings and application bundles are
local build inputs or outputs.
