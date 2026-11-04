//! Checked resource identity and cache seals for the HK 1.0.1 port.
//! This crate never writes to an input game package or key file.
use std::{
    collections::BTreeMap,
    fs,
    io::{Read, Write},
    path::Path,
};

mod sha256;
pub use sha256::{hash_file, hex};

pub const TITLE_ID: u64 = 0x0100_B8A0_26F1_4000;
pub const VERSION: &str = "1.0.1";
pub const MODULES: [(&str, &str); 3] = [
    (
        "main",
        "b7f965b9d7227db8e29f04e84ea7331b00000000000000000000000000000000",
    ),
    (
        "subsdk0",
        "ab62fe8edd07a6a293f885d54359e5599ccada89000000000000000000000000",
    ),
    (
        "sdk",
        "0023a1b06130cd795f7bc4613772af0af71c8cfd000000000000000000000000",
    ),
];
pub type Result<T> = std::result::Result<T, String>;

fn u32_at(b: &[u8], at: usize) -> Result<u32> {
    let s = b
        .get(at..at.checked_add(4).ok_or("offset overflow")?)
        .ok_or("truncated u32")?;
    Ok(u32::from_le_bytes(s.try_into().map_err(|_| "invalid u32")?))
}
fn u64_at(b: &[u8], at: usize) -> Result<u64> {
    let s = b
        .get(at..at.checked_add(8).ok_or("offset overflow")?)
        .ok_or("truncated u64")?;
    Ok(u64::from_le_bytes(s.try_into().map_err(|_| "invalid u64")?))
}
fn bounded(at: u64, size: u64, total: u64, what: &str) -> Result<()> {
    if at.checked_add(size).is_none_or(|end| end > total) {
        return Err(format!("{what}: range outside file"));
    }
    Ok(())
}

#[derive(Debug, Clone)]
pub struct Nso {
    pub build_id: String,
    pub memory_end: u64,
}
pub fn nso_header(b: &[u8], file_size: u64) -> Result<Nso> {
    if b.len() < 0x100 || &b[..4] != b"NSO0" {
        return Err("not a complete NSO0 header".into());
    }
    let flags = u32_at(b, 0xc)?;
    let mut previous_end = 0;
    for i in 0..3 {
        let at = 0x10 + i * 0x10;
        let file = u32_at(b, at)? as u64;
        let memory = u32_at(b, at + 4)? as u64;
        let size = u32_at(b, at + 8)? as u64;
        let stored = if flags & (1 << i) != 0 {
            u32_at(b, 0x60 + i * 4)? as u64
        } else {
            size
        };
        if file < 0x100 || memory < previous_end {
            return Err("overlapping NSO segments or segment inside header".into());
        }
        bounded(file, stored, file_size, "NSO segment")?;
        previous_end = memory.checked_add(size).ok_or("NSO memory overflow")?;
    }
    Ok(Nso {
        build_id: hex(&b[0x40..0x60]),
        memory_end: previous_end
            .checked_add(u32_at(b, 0x3c)? as u64)
            .ok_or("BSS overflow")?,
    })
}

#[derive(Debug)]
pub struct Npdm {
    pub program_id: u64,
    pub system_resource: u32,
    pub aarch64: bool,
}
pub fn npdm(b: &[u8]) -> Result<Npdm> {
    if b.len() < 0x80 || &b[..4] != b"META" {
        return Err("not a complete META/NPDM".into());
    }
    let aci = u32_at(b, 0x70)? as usize;
    let size = u32_at(b, 0x74)? as u64;
    bounded(aci as u64, size, b.len() as u64, "ACI0")?;
    if size < 0x40 || b.get(aci..aci + 4) != Some(b"ACI0") {
        return Err("invalid ACI0 header".into());
    }
    Ok(Npdm {
        program_id: u64_at(b, aci + 0x10)?,
        system_resource: u32_at(b, 0x14)?,
        aarch64: b[0xc] & 1 != 0,
    })
}

pub fn check_exefs(dir: &Path) -> Result<Vec<(String, Nso)>> {
    let meta = npdm(&fs::read(dir.join("main.npdm")).map_err(|e| format!("NPDM: {e}"))?)?;
    if !meta.aarch64 {
        return Err("AArch32 input cannot use the AArch64 native runtime".into());
    }
    if meta.program_id != TITLE_ID {
        return Err(format!(
            "unsupported program {:016X}; expected {TITLE_ID:016X}",
            meta.program_id
        ));
    }
    if meta.system_resource != 0x1400000 {
        return Err("NPDM system resource differs from measured profile".into());
    }
    let mut out = Vec::new();
    for (name, build) in MODULES {
        let p = dir.join(name);
        let mut f = fs::File::open(&p).map_err(|e| format!("{name}: {e}"))?;
        let mut header = [0; 0x100];
        f.read_exact(&mut header)
            .map_err(|e| format!("{name}: {e}"))?;
        let n = nso_header(&header, f.metadata().map_err(|e| e.to_string())?.len())?;
        if n.build_id != build {
            return Err(format!(
                "{name}: unregistered build {}; refusing mixed or unknown modules",
                n.build_id
            ));
        }
        out.push((name.to_owned(), n));
    }
    Ok(out)
}

pub const SEAL_NAME: &str = "module-manifest.tsv";
fn files(dir: &Path) -> Result<BTreeMap<String, u64>> {
    let mut out = BTreeMap::new();
    for entry in fs::read_dir(dir).map_err(|e| e.to_string())? {
        let p = entry.map_err(|e| e.to_string())?.path();
        let name = p
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or("invalid filename")?;
        if name == SEAL_NAME {
            continue;
        }
        if name.contains(['\t', '\n', '\r']) {
            return Err("invalid module filename".into());
        }
        let m = fs::symlink_metadata(&p).map_err(|e| e.to_string())?;
        if !m.is_file() {
            return Err(format!("{name}: expected a regular module file"));
        }
        out.insert(name.to_owned(), m.len());
    }
    if out.is_empty() {
        return Err("empty rewritten module directory".into());
    }
    Ok(out)
}
fn check_layout_ids(dir: &Path) -> Result<()> {
    for (name, full) in MODULES {
        let p = dir.join(format!("{name}.fixups.tsv"));
        let text = fs::read_to_string(&p).map_err(|e| format!("{name} layout: {e}"))?;
        let ids: Vec<_> = text
            .lines()
            .filter_map(|l| l.strip_prefix("# BUILD_ID\t"))
            .collect();
        if ids != [&full[..32]] {
            return Err(format!("{name}: rewritten build ID is not registered"));
        }
        for suffix in ["patched.bin", "gates.tsv", "cuts.tsv"] {
            if !dir.join(format!("{name}.{suffix}")).is_file() {
                return Err(format!("{name}: missing {suffix}"));
            }
        }
    }
    Ok(())
}
pub fn seal(dir: &Path, exefs: &Path) -> Result<()> {
    check_exefs(exefs)?;
    check_layout_ids(dir)?;
    let mut text = format!("FFR-MODULES\t1\t{TITLE_ID:016X}\t{VERSION}\n");
    for (name, build) in MODULES {
        text.push_str(&format!("module\t{name}\t{build}\n"));
    }
    for (name, size) in files(dir)? {
        text.push_str(&format!(
            "file\t{name}\t{size}\t{}\n",
            hex(&hash_file(&dir.join(&name))?)
        ));
    }
    let tmp = dir.with_extension("manifest.partial");
    let mut f = fs::File::create(&tmp).map_err(|e| e.to_string())?;
    f.write_all(text.as_bytes())
        .and_then(|_| f.sync_all())
        .map_err(|e| e.to_string())?;
    fs::rename(&tmp, dir.join(SEAL_NAME)).map_err(|e| e.to_string())?;
    Ok(())
}
pub fn verify(dir: &Path) -> Result<()> {
    let p = dir.join(SEAL_NAME);
    if fs::metadata(&p).map_err(|e| e.to_string())?.len() > 64 * 1024 {
        return Err("oversized module manifest".into());
    }
    let text = fs::read_to_string(&p).map_err(|e| format!("module manifest: {e}"))?;
    let mut lines = text.lines();
    let expected = format!("FFR-MODULES\t1\t{TITLE_ID:016X}\t{VERSION}");
    if lines.next() != Some(expected.as_str()) {
        return Err("incompatible module manifest".into());
    }
    for (name, build) in MODULES {
        if lines.next() != Some(format!("module\t{name}\t{build}").as_str()) {
            return Err("unregistered module identity in manifest".into());
        }
    }
    let mut remaining = files(dir)?;
    for row in lines {
        let c: Vec<_> = row.split('\t').collect();
        if c.len() != 4 || c[0] != "file" {
            return Err("malformed module manifest".into());
        }
        let size = c[2].parse::<u64>().map_err(|_| "invalid manifest length")?;
        // Lookup before joining: traversal, duplicates, and unlisted paths are all rejected.
        if remaining.remove(c[1]) != Some(size) {
            return Err(format!("{}: missing, duplicated, or changed length", c[1]));
        }
        if hex(&hash_file(&dir.join(c[1]))?) != c[3] {
            return Err(format!("{}: content differs from sealed build", c[1]));
        }
    }
    if !remaining.is_empty() {
        return Err("unsealed files in rewritten module directory".into());
    }
    check_layout_ids(dir)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_truncated_and_overflowing_nso() {
        assert!(nso_header(&[0; 8], 8).is_err());
        let mut b = [0; 0x100];
        b[..4].copy_from_slice(b"NSO0");
        for i in 0..3 {
            let at = 0x10 + i * 0x10;
            b[at..at + 4].copy_from_slice(&(0x100u32 + i as u32 * 16).to_le_bytes());
            b[at + 4..at + 8].copy_from_slice(&(i as u32 * 16).to_le_bytes());
            b[at + 8..at + 12].copy_from_slice(&16u32.to_le_bytes());
        }
        assert!(nso_header(&b, 0x130).is_ok());
        assert!(nso_header(&b, 0x12f).is_err());
    }
    #[test]
    fn npdm_uses_declared_aci_offset() {
        let mut b = [0; 0xc0];
        b[..4].copy_from_slice(b"META");
        b[0xc] = 1;
        b[0x70..0x74].copy_from_slice(&0x80u32.to_le_bytes());
        b[0x74..0x78].copy_from_slice(&0x40u32.to_le_bytes());
        b[0x80..0x84].copy_from_slice(b"ACI0");
        b[0x90..0x98].copy_from_slice(&TITLE_ID.to_le_bytes());
        assert_eq!(npdm(&b).unwrap().program_id, TITLE_ID);
        b[0x70..0x74].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(npdm(&b).is_err());
    }
    #[test]
    fn seal_rejects_tampering_and_traversal() {
        let dir = std::env::temp_dir().join(format!("ffr-seal-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let mut text = format!("FFR-MODULES\t1\t{TITLE_ID:016X}\t{VERSION}\n");
        for (name, build) in MODULES {
            text.push_str(&format!("module\t{name}\t{build}\n"));
        }
        fs::write(dir.join("payload"), b"abc").unwrap();
        let hash = hex(&hash_file(&dir.join("payload")).unwrap());
        fs::write(
            dir.join(SEAL_NAME),
            format!("{text}file\t../payload\t3\t{hash}\n"),
        )
        .unwrap();
        assert!(verify(&dir).unwrap_err().contains("missing, duplicated"));
        fs::write(
            dir.join(SEAL_NAME),
            format!("{text}file\tpayload\t3\t{hash}\n"),
        )
        .unwrap();
        fs::write(dir.join("payload"), b"abd").unwrap();
        assert!(verify(&dir).unwrap_err().contains("content differs"));
        fs::remove_dir_all(dir).unwrap();
    }
}
