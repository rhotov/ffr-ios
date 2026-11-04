//! The only new Rust/C boundary. The existing native execution engine owns the
//! guest context; this library validates its sealed input before any guest code.
use std::{
    ffi::{CStr, c_char, c_int},
    panic::{AssertUnwindSafe, catch_unwind},
    path::Path,
};

/// Validate all rewritten module bytes and identities.
///
/// # Safety
/// The directory must point to a live, NUL-terminated UTF-8 string. If non-null,
/// the error pointer must reference capacity writable bytes. Pointers are
/// borrowed only for this call; none are retained. No panic crosses the ABI.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn ffr_validate_runtime(
    directory: *const c_char,
    error: *mut c_char,
    capacity: usize,
) -> c_int {
    let result = catch_unwind(AssertUnwindSafe(|| -> Result<(), String> {
        if directory.is_null() {
            return Err("null module directory".into());
        }
        // SAFETY: the C caller provides a live NUL-terminated string as above.
        let dir = unsafe { CStr::from_ptr(directory) }
            .to_str()
            .map_err(|_| "module path is not UTF-8")?;
        nxfs::verify(Path::new(dir))
    }))
    .unwrap_or_else(|_| Err("Rust resource validation panicked".into()));
    match result {
        Ok(()) => 0,
        Err(message) => {
            if !error.is_null() && capacity > 0 {
                let n = message.len().min(capacity - 1);
                // SAFETY: the caller supplies capacity bytes; n < capacity.
                unsafe {
                    std::ptr::copy_nonoverlapping(message.as_ptr(), error.cast::<u8>(), n);
                    *error.add(n) = 0;
                }
            }
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ffi_reports_failure_with_a_bounded_terminated_buffer() {
        let mut message = [0x7fi8; 6];
        // SAFETY: the output is a six-byte live buffer, and null input is supported.
        assert_eq!(
            unsafe { ffr_validate_runtime(std::ptr::null(), message.as_mut_ptr(), message.len()) },
            1
        );
        assert_eq!(message[5], 0);
        assert_eq!(
            unsafe { ffr_validate_runtime(std::ptr::null(), std::ptr::null_mut(), 0) },
            1
        );
    }
}
