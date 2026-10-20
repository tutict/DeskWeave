//! Job Object supervision for the Python runtime child process (Windows).
//!
//! `child.kill()` only terminates the process we spawned. The runtime spawns
//! children of its own (Chromium, Blender), so a hard-killed shell could leave a
//! whole subtree behind. A Job Object with
//! `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` makes the kernel terminate every process
//! in the job when the last handle to it is closed — including when this process
//! dies unexpectedly and the handle is released by the OS.
//!
//! Limits of this approach, stated honestly:
//! * the child is assigned **after** `spawn` returns, so anything it created in
//!   that window would escape. The Python runtime spends >100 ms importing
//!   before it can start a browser or Blender, so the window is empty in
//!   practice; closing it completely would need `CREATE_SUSPENDED` plus a thread
//!   handle, which `std::process` does not expose.
//! * assignment can fail when the current process is already in a job that
//!   forbids nesting. The caller logs and continues without supervision rather
//!   than refusing to start.

#[cfg(windows)]
pub use windows_impl::ChildJob;

#[cfg(windows)]
mod windows_impl {
    use std::ffi::c_void;
    use std::io;
    use std::mem::size_of;
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;

    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    /// Owns a Job Object handle. Dropping it terminates every process in the job.
    ///
    /// The handle is stored as `isize` rather than `HANDLE` (`*mut c_void`) so this
    /// type stays `Send`, which `tauri::State` requires. Kernel handles are
    /// process-wide values; using one from another thread is allowed.
    pub struct ChildJob {
        handle: isize,
    }

    impl ChildJob {
        /// Create a job that kills its members when the last handle closes.
        pub fn create() -> io::Result<Self> {
            // SAFETY: no attributes and no name; the returned handle is checked
            // for null before use and closed exactly once (in `Drop`).
            let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
            if handle.is_null() {
                return Err(io::Error::last_os_error());
            }
            let handle = handle as isize;

            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            // SAFETY: `info` is a fully initialised struct of the exact type the
            // information class expects, and its size is passed alongside it.
            let ok = unsafe {
                SetInformationJobObject(
                    handle as HANDLE,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const c_void,
                    size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                )
            };
            if ok == 0 {
                let err = io::Error::last_os_error();
                unsafe { CloseHandle(handle as HANDLE) };
                return Err(err);
            }
            Ok(Self { handle })
        }

        /// Put an already-spawned child (and, later, its children) into the job.
        pub fn assign(&self, child: &Child) -> io::Result<()> {
            // SAFETY: both handles are valid for the duration of the call — the
            // job handle is owned by `self`, the process handle by `child`.
            let ok = unsafe {
                AssignProcessToJobObject(self.handle as HANDLE, child.as_raw_handle() as HANDLE)
            };
            if ok == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        }
    }

    impl Drop for ChildJob {
        fn drop(&mut self) {
            // Closing the last handle triggers KILL_ON_JOB_CLOSE.
            unsafe { CloseHandle(self.handle as HANDLE) };
        }
    }
}

#[cfg(not(windows))]
pub use noop::ChildJob;

#[cfg(not(windows))]
mod noop {
    use std::io;
    use std::process::Child;

    /// Placeholder so the supervision field compiles on platforms without jobs.
    pub struct ChildJob;

    impl ChildJob {
        pub fn create() -> io::Result<Self> {
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "job objects are a Windows feature",
            ))
        }

        pub fn assign(&self, _child: &Child) -> io::Result<()> {
            Ok(())
        }
    }
}

#[cfg(all(test, windows))]
mod tests {
    use std::process::{Command, Stdio};
    use std::thread::sleep;
    use std::time::{Duration, Instant};

    use super::ChildJob;

    /// A child that stays alive until it is killed, without needing Python.
    fn sleeper() -> std::process::Child {
        Command::new("ping")
            .args(["-n", "60", "127.0.0.1"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("could not spawn the test child")
    }

    #[test]
    fn closing_the_job_kills_its_members() {
        let mut child = sleeper();
        let job = ChildJob::create().expect("could not create a job object");
        job.assign(&child)
            .expect("could not assign the child to the job");
        assert!(
            child.try_wait().expect("try_wait failed").is_none(),
            "the test child exited before the job was closed"
        );

        drop(job);

        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if child.try_wait().expect("try_wait failed").is_some() {
                return;
            }
            sleep(Duration::from_millis(50));
        }
        let _ = child.kill();
        panic!("the child survived the job handle being closed");
    }
}
