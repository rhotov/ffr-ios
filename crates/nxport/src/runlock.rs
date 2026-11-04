use std::{
    fs::{File, OpenOptions},
    io,
    os::{fd::AsRawFd, raw::c_int, unix::fs::OpenOptionsExt},
    path::Path,
};

unsafe extern "C" {
    fn flock(fd: c_int, operation: c_int) -> c_int;
    fn fcntl(fd: c_int, command: c_int, ...) -> c_int;
}

/// Keep the returned file alive until exec; its descriptor deliberately survives
/// exec so the native runtime owns the same exclusive save transaction lock.
pub fn acquire(path: &Path) -> Result<File, String> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(path)
        .map_err(|e| format!("cannot open the game lock: {e}"))?;
    let fd = file.as_raw_fd();
    // SAFETY: fd is owned by the live File. LOCK_EX | LOCK_NB never blocks.
    if unsafe { flock(fd, 2 | 4) } != 0 {
        let error = io::Error::last_os_error();
        return Err(if error.kind() == io::ErrorKind::WouldBlock {
            "游戏正在运行或准备资源，请先退出当前游戏再重试。".into()
        } else {
            format!("cannot acquire the game lock: {error}")
        });
    }
    // SAFETY: F_GETFD/F_SETFD operate on the same live descriptor. Clear only
    // FD_CLOEXEC; every other descriptor flag is preserved.
    let flags = unsafe { fcntl(fd, 1) };
    if flags < 0 || unsafe { fcntl(fd, 2, flags & !1) } < 0 {
        return Err(format!(
            "cannot retain game lock across exec: {}",
            io::Error::last_os_error()
        ));
    }
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        env, fs,
        os::{fd::FromRawFd, unix::fs::MetadataExt},
        process::Command,
    };

    #[test]
    fn lock_survives_exec_and_rejects_second_process() {
        if let Some(path) = env::var_os("FFR_TEST_INHERITED_LOCK") {
            let fd = env::var("FFR_TEST_LOCK_FD").unwrap().parse().unwrap();
            // SAFETY: this fresh exec receives the deliberately inherited fd.
            let inherited = unsafe { File::from_raw_fd(fd) };
            let actual = inherited.metadata().unwrap();
            let expected = fs::metadata(&path).unwrap();
            assert_eq!(
                (actual.dev(), actual.ino()),
                (expected.dev(), expected.ino())
            );
            assert!(acquire(Path::new(&path)).is_err());
            return;
        }
        let dir = env::temp_dir().join(format!("ffr-lock-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("run.lock");
        let lock = acquire(&path).unwrap();
        let status = Command::new(env::current_exe().unwrap())
            .args([
                "--exact",
                "runlock::tests::lock_survives_exec_and_rejects_second_process",
            ])
            .env("FFR_TEST_INHERITED_LOCK", &path)
            .env("FFR_TEST_LOCK_FD", lock.as_raw_fd().to_string())
            .status()
            .unwrap();
        assert!(status.success());
        drop(lock);
        drop(acquire(&path).expect("lock must be released after the owner exits"));
        fs::remove_dir_all(dir).unwrap();
    }
}
