use std::os::unix::process::CommandExt;
use std::{
    collections::BTreeMap,
    env, fs,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};

mod runlock;

const PROFILE_FILES: [&str; 7] = [
    "game.tsv",
    "shader_map.tsv",
    "shader_facts.tsv",
    "shader_pairs.tsv",
    "bf_want.tsv",
    "c1_full.tsv",
    "cb_extent.tsv",
];

fn parse_config(text: &str) -> Result<BTreeMap<&str, &str>, String> {
    let mut out = BTreeMap::new();
    for row in text
        .lines()
        .filter(|l| !l.starts_with('#') && !l.is_empty())
    {
        let (key, value) = row
            .split_once('\t')
            .ok_or("invalid runtime configuration")?;
        if out.insert(key, value).is_some() {
            return Err("duplicate runtime configuration key".into());
        }
    }
    Ok(out)
}

fn verify_profile(
    profile: &Path,
    config: &BTreeMap<&str, &str>,
    chain: &str,
    inputs: &str,
) -> Result<(), String> {
    for name in PROFILE_FILES {
        if !config.contains_key(format!("sha256:{name}").as_str()) {
            return Err(format!("unsealed shader profile table {name}"));
        }
    }
    for (key, digest) in config {
        let Some(name) = key.strip_prefix("sha256:") else {
            continue;
        };
        if !name.ends_with(".tsv") || name.contains(['/', '\\']) || name.contains("..") {
            return Err("invalid shader profile table name".into());
        }
        if nxfs::hex(&nxfs::hash_file(&profile.join(name))?) != *digest {
            return Err(format!(
                "shader profile table changed: {name}; prepare this build again"
            ));
        }
    }
    for entry in fs::read_dir(profile).map_err(|e| e.to_string())? {
        let name = entry
            .map_err(|e| e.to_string())?
            .file_name()
            .to_string_lossy()
            .into_owned();
        if name.ends_with(".tsv") && !config.contains_key(format!("sha256:{name}").as_str()) {
            return Err(format!("unsealed shader profile table {name}"));
        }
    }
    let expected = format!("# chain b-{chain} inputs {inputs} ");
    for name in ["c1_full.tsv", "cb_extent.tsv"] {
        let text = fs::read_to_string(profile.join(name)).map_err(|e| e.to_string())?;
        if !text
            .lines()
            .next()
            .is_some_and(|line| line.starts_with(&expected))
        {
            return Err(format!("{name}: shader chain or input stamp differs"));
        }
    }
    Ok(())
}

fn run(work: &Path) -> Result<(), String> {
    let work = work
        .canonicalize()
        .map_err(|e| format!("game preparation is missing: {e}"))?;
    let run = work.join("run");
    fs::create_dir_all(&run).map_err(|e| e.to_string())?;
    let _run_lock = runlock::acquire(&run.join("ffr.lock"))?;
    let config = fs::read_to_string(work.join("macos-runtime.tsv"))
        .map_err(|_| "Prepare the Mac game before launching it.")?;
    let c = parse_config(&config)?;
    if c.get("title_id") != Some(&"0100B8A026F14000")
        || c.get("version") != Some(&"1.0.1")
        || c.get("platform") != Some(&"macos-arm64")
    {
        return Err("unsupported game/platform runtime configuration".into());
    }
    let get = |key: &str| -> Result<PathBuf, String> {
        let value = c
            .get(key)
            .ok_or_else(|| format!("missing runtime path {key}"))?;
        work.join(value)
            .canonicalize()
            .map_err(|e| format!("{key}: {e}"))
    };
    let patched = get("patched")?;
    nxfs::verify(&patched)?;
    let host = get("host")?;
    let host_hash = nxfs::hex(&nxfs::hash_file(&host)?);
    if c.get("host_sha256") != Some(&host_hash.as_str()) {
        return Err("Mac host differs from the prepared build; rebuild it".into());
    }
    let msl = get("msl")?;
    let chain = c.get("chain").ok_or("missing shader chain")?;
    if chain.len() != 12 || !chain.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("invalid shader chain identity".into());
    }
    let lib = msl.join(format!("b-{chain}"));
    let stamp =
        fs::read_to_string(lib.join("inputs.tsv")).map_err(|_| "missing Metal shader libraries")?;
    if !stamp.lines().any(|l| l == "game\t0100B8A026F14000") {
        return Err("Metal libraries belong to another game".into());
    }
    let inputs = c
        .get("shader_inputs")
        .ok_or("missing shader input identity")?;
    if !stamp
        .lines()
        .any(|line| line == format!("inputs\t{inputs}"))
    {
        return Err("Metal libraries differ from the prepared shader inputs".into());
    }
    let profile = get("profile")?;
    verify_profile(&profile, &c, chain, inputs)?;
    let save = run.join("save");
    let temp = run.join("temp");
    let logs = run.join("logs");
    for p in [&save, &temp, &logs] {
        fs::create_dir_all(p).map_err(|e| e.to_string())?;
    }
    let id = format!(
        "{}-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_secs(),
        std::process::id()
    );
    let logpath = logs.join(format!("{id}.log"));
    let mut log = fs::File::create(&logpath).map_err(|e| e.to_string())?;
    writeln!(log,"[FFR] run={id} title=0100B8A026F14000 version=1.0.1 platform=macos-arm64 host_sha256={host_hash} shader_chain={chain}").map_err(|e|e.to_string())?;
    writeln!(log, "[FFR] work={} save={}", work.display(), save.display())
        .map_err(|e| e.to_string())?;
    fs::write(
        run.join("latest-log.txt"),
        format!("{}\n", logpath.display()),
    )
    .map_err(|e| e.to_string())?;
    eprintln!("FINAL FANTASY RESONANCE — log: {}", logpath.display());
    let mut cmd = Command::new(host);
    cmd.arg(patched)
        .current_dir(&run)
        .env("DM_ROOT", &work)
        .env("OT2_WORK", &work)
        .env("DM_ROMFS", get("romfs")?)
        .env("DM_SAVE", save)
        .env("DM_TEMP", temp)
        .env("DM_MSL_DIR", msl)
        .env("DM_NVN_SHMAP", profile.join("shader_map.tsv"))
        .env("DM_FONT_DIR", get("font")?)
        .env("DM_WINDOW", "1")
        .env("DM_AUDIO", "1")
        .env(
            "DM_LANG",
            env::var("DM_LANG").unwrap_or_else(|_| "zh-Hans".into()),
        )
        .env("OT2_NVN_HLE", "1")
        .env("OT2_NVN_OFFLINE_SHADERS", "1")
        .stdout(log.try_clone().map_err(|e| e.to_string())?)
        .stderr(log);
    if work.join("aoc").is_dir() {
        cmd.env("DM_AOC_DIR", work.join("aoc"));
    }
    Err(format!("cannot start the native runtime: {}", cmd.exec()))
}
fn main() {
    let args: Vec<_> = env::args_os().skip(1).collect();
    let result: Result<(), String> = (|| match args.first().and_then(|s| s.to_str()) {
        Some("inspect") if args.len() == 2 => {
            for (name, module) in nxfs::check_exefs(Path::new(&args[1]))? {
                println!(
                    "{name}\t{}\timage_end={:#x}",
                    module.build_id, module.memory_end
                );
            }
            Ok(())
        }
        Some("seal") if args.len() == 3 => nxfs::seal(Path::new(&args[1]), Path::new(&args[2])),
        Some("verify") if args.len() == 2 => nxfs::verify(Path::new(&args[1])),
        Some("run") if args.len() == 2 => run(Path::new(&args[1])),
        _ => Err(
            "Usage: nxport inspect EXEFS | seal PATCHED EXEFS | verify PATCHED | run WORK".into(),
        ),
    })();
    if let Err(e) = result {
        eprintln!("[FFR] {e}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(label: &str) -> (PathBuf, String) {
        let path = env::temp_dir().join(format!("ffr-profile-{label}-{}", std::process::id()));
        fs::create_dir_all(&path).unwrap();
        let mut config = String::new();
        for name in PROFILE_FILES {
            let content = if matches!(name, "c1_full.tsv" | "cb_extent.tsv") {
                "# chain b-0123456789ab inputs abcdef012345 fixture\n"
            } else {
                "fixture data\n"
            };
            fs::write(path.join(name), content).unwrap();
            config.push_str(&format!(
                "sha256:{name}\t{}\n",
                nxfs::hex(&nxfs::hash_file(&path.join(name)).unwrap())
            ));
        }
        (path, config)
    }
    #[test]
    fn rejects_changed_tables_even_with_the_same_game_id() {
        let (path, text) = fixture("tamper");
        let config = parse_config(&text).unwrap();
        verify_profile(&path, &config, "0123456789ab", "abcdef012345").unwrap();
        fs::write(path.join("shader_map.tsv"), "another data\n").unwrap();
        assert!(
            verify_profile(&path, &config, "0123456789ab", "abcdef012345")
                .unwrap_err()
                .contains("table changed")
        );
        fs::remove_dir_all(path).unwrap();
    }
    #[test]
    fn rejects_incompatible_library_and_profile_stamps() {
        let (path, text) = fixture("stamp");
        assert!(
            verify_profile(
                &path,
                &parse_config(&text).unwrap(),
                "0123456789ab",
                "000000000000"
            )
            .unwrap_err()
            .contains("stamp differs")
        );
        fs::remove_dir_all(path).unwrap();
    }
}
