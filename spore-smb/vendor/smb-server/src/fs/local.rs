//! `LocalFsBackend` — a `ShareBackend` backed by a real on-disk directory.
//!
//! The share root is opened once via `cap_std::fs::Dir::open_ambient_dir` and
//! kept as the sole authority handle. All subsequent path operations are
//! resolved relative to that handle, so a malicious symlink or `..` smuggled
//! through `SmbPath` cannot escape the sandbox — `cap-std` enforces this at
//! every step.
//!
//! Per the v1 design (spec §3.4) this backend is intentionally minimal:
//!
//! - Sync FS calls are wrapped in `tokio::task::spawn_blocking` so the async
//!   `ShareBackend`/`Handle` methods integrate cleanly with the dispatcher.
//! - `read_only()` flips a flag that makes write-class opens reject early
//!   with `SmbError::AccessDenied`.
//! - DOS-style glob matching for `list_dir` is handled here (case-insensitive,
//!   `?` and `*`), since cap-std only provides raw `entries()`.

use std::io;
use std::os::unix::fs::FileExt as _;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use bytes::Bytes;
use cap_std::ambient_authority;
use cap_std::fs::{Dir, OpenOptions as CapOpenOptions};
use tokio::task::spawn_blocking;

use crate::backend::{
    BackendCapabilities, DirEntry as SmbDirEntry, FileInfo, FileTimes, Handle, OpenIntent,
    OpenOptions, ShareBackend,
};
use crate::error::{SmbError, SmbResult};
use crate::path::SmbPath;

// ---------------------------------------------------------------------------
// Backend
// ---------------------------------------------------------------------------

/// Local-filesystem backend, sandboxed at a single root directory.
///
/// Cheap to clone: internally an `Arc<cap_std::fs::Dir>` plus a flag.
pub struct LocalFsBackend {
    root: Arc<Dir>,
    read_only: bool,
}

impl LocalFsBackend {
    /// Open `path` as the share root. Errors if the path does not exist or is
    /// not a directory.
    pub fn new(path: impl AsRef<Path>) -> io::Result<Self> {
        let dir = Dir::open_ambient_dir(path, ambient_authority())?;
        Ok(Self {
            root: Arc::new(dir),
            read_only: false,
        })
    }

    /// Mark the backend as read-only. All write-class opens and writes return
    /// an access-denied SMB error.
    #[must_use]
    pub fn read_only(mut self) -> Self {
        self.read_only = true;
        self
    }
}

// ---------------------------------------------------------------------------
// Path translation
// ---------------------------------------------------------------------------

/// Convert a validated `SmbPath` into a relative `PathBuf` suitable for
/// `cap_std::fs::Dir` lookups.
///
/// `SmbPath` is already validated (no `..`, no forbidden chars, no doubled
/// separators), so this is purely a join. The empty `SmbPath` (root) yields
/// `PathBuf::from(".")` — cap-std accepts this for `metadata` etc.
fn to_rel_path(path: &SmbPath) -> PathBuf {
    if path.is_root() {
        return PathBuf::from(".");
    }
    let mut out = PathBuf::new();
    for c in path.components() {
        out.push(c);
    }
    out
}

// ---------------------------------------------------------------------------
// Error mapping
// ---------------------------------------------------------------------------

fn io_to_smb(err: io::Error) -> SmbError {
    use io::ErrorKind::*;
    match err.kind() {
        NotFound => SmbError::NotFound,
        PermissionDenied => SmbError::AccessDenied,
        AlreadyExists => SmbError::Exists,
        DirectoryNotEmpty => SmbError::NotEmpty,
        IsADirectory => SmbError::IsDirectory,
        NotADirectory => SmbError::NotADirectory,
        InvalidInput | InvalidFilename => SmbError::NameInvalid,
        _ => SmbError::Io(err),
    }
}

/// Convert a panic from `spawn_blocking` into an `io::Error`. Panics in the
/// blocking pool are exotic; we surface them as a generic `Other` rather than
/// re-panicking on the async side.
fn join_to_io(_e: tokio::task::JoinError) -> io::Error {
    io::Error::other("blocking task panicked or was cancelled")
}

// ---------------------------------------------------------------------------
// FILETIME conversion
// ---------------------------------------------------------------------------

/// Number of 100-nanosecond intervals between 1601-01-01 (Windows FILETIME
/// epoch) and 1970-01-01 (UNIX epoch).
const FILETIME_OFFSET: u64 = 116_444_736_000_000_000;

fn system_time_to_filetime(t: SystemTime) -> u64 {
    match t.duration_since(UNIX_EPOCH) {
        Ok(d) => FILETIME_OFFSET + (d.as_secs() * 10_000_000) + u64::from(d.subsec_nanos() / 100),
        Err(_) => 0,
    }
}

fn filetime_to_system_time(ft: u64) -> Option<SystemTime> {
    if ft < FILETIME_OFFSET {
        return None;
    }
    let unix_100ns = ft - FILETIME_OFFSET;
    let secs = unix_100ns / 10_000_000;
    let nanos = ((unix_100ns % 10_000_000) * 100) as u32;
    UNIX_EPOCH.checked_add(Duration::new(secs, nanos))
}

// ---------------------------------------------------------------------------
// FileInfo construction
// ---------------------------------------------------------------------------

fn file_info_from_metadata(name: String, md: &cap_std::fs::Metadata) -> FileInfo {
    let len = md.len();
    let modified = md.modified().ok().map(|t| t.into_std());
    let accessed = md.accessed().ok().map(|t| t.into_std());
    let created = md.created().ok().map(|t| t.into_std());

    // Fall back: if a particular timestamp isn't available on the platform,
    // use whichever timestamp is available, then `now()` as last resort. SMB
    // clients tolerate equal timestamps fine.
    let modified = modified
        .or(created)
        .or(accessed)
        .unwrap_or(SystemTime::UNIX_EPOCH);
    let accessed = accessed.unwrap_or(modified);
    let created = created.unwrap_or(modified);

    FileInfo {
        name,
        end_of_file: len,
        allocation_size: len,
        creation_time: system_time_to_filetime(created),
        last_access_time: system_time_to_filetime(accessed),
        last_write_time: system_time_to_filetime(modified),
        change_time: system_time_to_filetime(modified),
        is_directory: md.is_dir(),
        // `cap-std` does not expose a stable inode-style identifier in its
        // public API; the dispatcher substitutes the FileId where needed.
        file_index: 0,
    }
}

// ---------------------------------------------------------------------------
// DOS glob matching
// ---------------------------------------------------------------------------

/// Match `name` against a DOS-style pattern. `?` matches any single char,
/// `*` matches any sequence (possibly empty). Comparison is case-insensitive
/// (ASCII fold) — sufficient for the v1 use-case where names are validated to
/// be free of weird Unicode tricks.
fn glob_match(pattern: &str, name: &str) -> bool {
    // Walk both strings as char vectors so `?` matches a char rather than a
    // byte, without going through grapheme territory.
    let p: Vec<char> = pattern.chars().collect();
    let n: Vec<char> = name.chars().collect();
    glob_match_inner(&p, &n)
}

fn glob_match_inner(p: &[char], n: &[char]) -> bool {
    let mut pi = 0usize;
    let mut ni = 0usize;
    let mut star: Option<(usize, usize)> = None; // (pi after '*', ni at the time)

    while ni < n.len() {
        if pi < p.len() && (p[pi] == '?' || ascii_eq_ci(p[pi], n[ni])) {
            pi += 1;
            ni += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star = Some((pi + 1, ni));
            pi += 1;
        } else if let Some((sp, sn)) = star {
            pi = sp;
            ni = sn + 1;
            star = Some((sp, sn + 1));
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}

fn ascii_eq_ci(a: char, b: char) -> bool {
    a.eq_ignore_ascii_case(&b)
}

// ---------------------------------------------------------------------------
// ShareBackend impl
// ---------------------------------------------------------------------------

#[async_trait]
impl ShareBackend for LocalFsBackend {
    async fn open(&self, path: &SmbPath, opts: OpenOptions) -> SmbResult<Box<dyn Handle>> {
        // 1. Read-only check: any open that requests creation, write access,
        //    truncation, or overwrite is rejected up front. Pure read opens
        //    pass through.
        let writes = opts.write
            || matches!(
                opts.intent,
                OpenIntent::Create
                    | OpenIntent::OpenOrCreate
                    | OpenIntent::OverwriteOrCreate
                    | OpenIntent::Truncate
            );
        if self.read_only && writes {
            return Err(SmbError::AccessDenied);
        }

        let rel = to_rel_path(path);
        let root = Arc::clone(&self.root);
        let read_only = self.read_only;
        let directory = opts.directory;
        let non_directory = opts.non_directory;

        // For directories, cap-std exposes `open_dir` separately; we don't
        // need an OpenOptions translation in that case.
        if directory {
            // Directory CREATE intents: Create / OpenOrCreate / OverwriteOrCreate
            // imply mkdir; Open / Truncate require existing.
            let intent = opts.intent;
            let dir_handle = spawn_blocking(move || -> io::Result<Dir> {
                match intent {
                    OpenIntent::Open => root.open_dir(&rel),
                    OpenIntent::Create => {
                        root.create_dir(&rel)?;
                        root.open_dir(&rel)
                    }
                    OpenIntent::OpenOrCreate => {
                        if !root.exists(&rel) {
                            root.create_dir(&rel)?;
                        }
                        root.open_dir(&rel)
                    }
                    OpenIntent::Truncate | OpenIntent::OverwriteOrCreate => {
                        // Truncating a directory has no meaning; reject.
                        Err(io::Error::from(io::ErrorKind::InvalidInput))
                    }
                }
            })
            .await
            .map_err(join_to_io)
            .map_err(io_to_smb)?
            .map_err(io_to_smb)?;

            return Ok(Box::new(LocalHandle::Dir {
                name: file_name_for(path),
                dir_handle: Arc::new(dir_handle),
            }));
        }

        let existing_is_dir = {
            let root = Arc::clone(&self.root);
            let rel = rel.clone();
            spawn_blocking(move || -> io::Result<bool> {
                match root.metadata(&rel) {
                    Ok(md) => Ok(md.is_dir()),
                    Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
                    Err(e) => Err(e),
                }
            })
            .await
            .map_err(join_to_io)
            .map_err(io_to_smb)?
            .map_err(io_to_smb)?
        };
        if existing_is_dir {
            if non_directory {
                return Err(SmbError::IsDirectory);
            }
            match opts.intent {
                OpenIntent::Open | OpenIntent::OpenOrCreate => {
                    let root = Arc::clone(&self.root);
                    let rel = rel.clone();
                    let dir_handle = spawn_blocking(move || root.open_dir(&rel))
                        .await
                        .map_err(join_to_io)
                        .map_err(io_to_smb)?
                        .map_err(io_to_smb)?;
                    return Ok(Box::new(LocalHandle::Dir {
                        name: file_name_for(path),
                        dir_handle: Arc::new(dir_handle),
                    }));
                }
                OpenIntent::Create => return Err(SmbError::Exists),
                OpenIntent::Truncate | OpenIntent::OverwriteOrCreate => {
                    return Err(SmbError::IsDirectory);
                }
            }
        }

        // 2. Translate OpenIntent → cap-std OpenOptions.
        let mut cap_opts = CapOpenOptions::new();
        match opts.intent {
            OpenIntent::Open => {
                cap_opts.read(true).write(opts.write);
            }
            OpenIntent::Create => {
                cap_opts.read(opts.read).write(true).create_new(true);
            }
            OpenIntent::Truncate => {
                cap_opts.read(opts.read).write(true).truncate(true);
            }
            OpenIntent::OpenOrCreate => {
                cap_opts.read(opts.read).write(true).create(true);
            }
            OpenIntent::OverwriteOrCreate => {
                cap_opts
                    .read(opts.read)
                    .write(true)
                    .create(true)
                    .truncate(true);
            }
        }

        let cap_file = spawn_blocking(move || root.open_with(&rel, &cap_opts))
            .await
            .map_err(join_to_io)
            .map_err(io_to_smb)?
            .map_err(io_to_smb)?;

        // Convert to a `std::fs::File`. We only need cap-std for the safe
        // *open*; once we hold a verified file handle, std's API gives us
        // `set_times`, `set_len`, `sync_data`, and `FileExt::{read,write}_at`
        // without pulling in extra crates.
        let std_file: std::fs::File = cap_file.into_std();

        Ok(Box::new(LocalHandle::File {
            name: file_name_for(path),
            file: Arc::new(std_file),
            read_only,
        }))
    }

    async fn unlink(&self, path: &SmbPath) -> SmbResult<()> {
        if self.read_only {
            return Err(SmbError::AccessDenied);
        }
        if path.is_root() {
            // Refusing to delete the share root itself.
            return Err(SmbError::AccessDenied);
        }
        let rel = to_rel_path(path);
        let root = Arc::clone(&self.root);

        spawn_blocking(move || -> io::Result<()> {
            match root.remove_file(&rel) {
                Ok(()) => Ok(()),
                Err(e) if e.kind() == io::ErrorKind::IsADirectory => {
                    // Caller's intent was "delete this name"; if it turned
                    // out to be a directory, fall back to remove_dir which
                    // refuses non-empty dirs (mapped to NotEmpty above).
                    root.remove_dir(&rel)
                }
                Err(e) => Err(e),
            }
        })
        .await
        .map_err(join_to_io)
        .map_err(io_to_smb)?
        .map_err(io_to_smb)
    }

    async fn rename(&self, from: &SmbPath, to: &SmbPath) -> SmbResult<()> {
        if self.read_only {
            return Err(SmbError::AccessDenied);
        }
        if from.is_root() || to.is_root() {
            return Err(SmbError::NameInvalid);
        }
        let from = to_rel_path(from);
        let to_path = to_rel_path(to);
        let root = Arc::clone(&self.root);
        let root2 = Arc::clone(&self.root);

        spawn_blocking(move || -> io::Result<()> {
            // Reject overwrite — SMB rename semantics require explicit
            // replace-if-exists which we do not implement in v1.
            if root2.exists(&to_path) {
                return Err(io::Error::from(io::ErrorKind::AlreadyExists));
            }
            root.rename(&from, &root2, &to_path)
        })
        .await
        .map_err(join_to_io)
        .map_err(io_to_smb)?
        .map_err(io_to_smb)
    }

    fn capabilities(&self) -> BackendCapabilities {
        BackendCapabilities {
            is_read_only: self.read_only,
            // POSIX filesystems are typically case-sensitive. We don't try to
            // emulate case-insensitive lookup in v1 (see spec §3.4).
            case_sensitive: cfg!(any(target_os = "linux", target_os = "freebsd")),
        }
    }
}

// ---------------------------------------------------------------------------
// Handle
// ---------------------------------------------------------------------------

/// Internal handle variant. `File` carries a `std::fs::File` (after cap-std
/// has done the safe open); `Dir` keeps the `cap_std::fs::Dir` so we can
/// re-list entries.
enum LocalHandle {
    File {
        name: String,
        file: Arc<std::fs::File>,
        read_only: bool,
    },
    Dir {
        name: String,
        dir_handle: Arc<Dir>,
    },
}

fn file_name_for(path: &SmbPath) -> String {
    path.file_name().unwrap_or("").to_string()
}

#[async_trait]
impl Handle for LocalHandle {
    async fn read(&self, offset: u64, len: u32) -> SmbResult<Bytes> {
        match self {
            LocalHandle::File { file, .. } => {
                let file = Arc::clone(file);
                let n = len as usize;
                let bytes = spawn_blocking(move || -> io::Result<Bytes> {
                    let mut buf = vec![0u8; n];
                    let read = file.read_at(&mut buf, offset)?;
                    buf.truncate(read);
                    Ok(Bytes::from(buf))
                })
                .await
                .map_err(join_to_io)
                .map_err(io_to_smb)?
                .map_err(io_to_smb)?;
                Ok(bytes)
            }
            LocalHandle::Dir { .. } => Err(SmbError::IsDirectory),
        }
    }

    async fn write(&self, offset: u64, data: &[u8]) -> SmbResult<u32> {
        self.write_owned(offset, data.to_vec()).await
    }

    async fn write_owned(&self, offset: u64, data: Vec<u8>) -> SmbResult<u32> {
        match self {
            LocalHandle::File {
                file, read_only, ..
            } => {
                if *read_only {
                    return Err(SmbError::AccessDenied);
                }
                let file = Arc::clone(file);
                let written = spawn_blocking(move || file.write_at(&data, offset))
                    .await
                    .map_err(join_to_io)
                    .map_err(io_to_smb)?
                    .map_err(io_to_smb)?;
                Ok(u32::try_from(written).unwrap_or(u32::MAX))
            }
            LocalHandle::Dir { .. } => Err(SmbError::IsDirectory),
        }
    }

    async fn flush(&self) -> SmbResult<()> {
        match self {
            LocalHandle::File { file, .. } => {
                let file = Arc::clone(file);
                spawn_blocking(move || file.sync_data())
                    .await
                    .map_err(join_to_io)
                    .map_err(io_to_smb)?
                    .map_err(io_to_smb)
            }
            // Flushing a directory is a no-op in SMB semantics.
            LocalHandle::Dir { .. } => Ok(()),
        }
    }

    async fn stat(&self) -> SmbResult<FileInfo> {
        match self {
            LocalHandle::File { file, name, .. } => {
                let file = Arc::clone(file);
                let name = name.clone();
                spawn_blocking(move || -> io::Result<FileInfo> {
                    let std_md = file.metadata()?;
                    // Synthesize a cap-std Metadata from the std one so we
                    // can reuse `file_info_from_metadata`. cap-primitives
                    // exposes `Metadata::from_just_metadata` for this.
                    let md = cap_std::fs::Metadata::from_just_metadata(std_md);
                    Ok(file_info_from_metadata(name, &md))
                })
                .await
                .map_err(join_to_io)
                .map_err(io_to_smb)?
                .map_err(io_to_smb)
            }
            LocalHandle::Dir {
                dir_handle, name, ..
            } => {
                let dir_handle = Arc::clone(dir_handle);
                let name = name.clone();
                spawn_blocking(move || -> io::Result<FileInfo> {
                    let md = dir_handle.dir_metadata()?;
                    Ok(file_info_from_metadata(name, &md))
                })
                .await
                .map_err(join_to_io)
                .map_err(io_to_smb)?
                .map_err(io_to_smb)
            }
        }
    }

    async fn set_times(&self, times: FileTimes) -> SmbResult<()> {
        match self {
            LocalHandle::File {
                file, read_only, ..
            } => {
                if *read_only {
                    return Err(SmbError::AccessDenied);
                }
                let file = Arc::clone(file);
                spawn_blocking(move || -> io::Result<()> {
                    let mut std_times = std::fs::FileTimes::new();
                    if let Some(ft) = times.last_write_time
                        && let Some(t) = filetime_to_system_time(ft)
                    {
                        std_times = std_times.set_modified(t);
                    }
                    if let Some(ft) = times.last_access_time
                        && let Some(t) = filetime_to_system_time(ft)
                    {
                        std_times = std_times.set_accessed(t);
                    }
                    // creation_time / change_time: stable std::fs::FileTimes
                    // does not expose setters for these; silently ignored.
                    file.set_times(std_times)
                })
                .await
                .map_err(join_to_io)
                .map_err(io_to_smb)?
                .map_err(io_to_smb)
            }
            // cap-std's directory handle does not expose set_times in its
            // stable API; mark as unsupported on directories.
            LocalHandle::Dir { .. } => Err(SmbError::NotSupported),
        }
    }

    async fn truncate(&self, len: u64) -> SmbResult<()> {
        match self {
            LocalHandle::File {
                file, read_only, ..
            } => {
                if *read_only {
                    return Err(SmbError::AccessDenied);
                }
                let file = Arc::clone(file);
                spawn_blocking(move || file.set_len(len))
                    .await
                    .map_err(join_to_io)
                    .map_err(io_to_smb)?
                    .map_err(io_to_smb)
            }
            // Protocol layer rejects truncate on dir handles before this; if
            // it ever reaches us, surface as NotSupported.
            LocalHandle::Dir { .. } => Err(SmbError::NotSupported),
        }
    }

    async fn list_dir(&self, pattern: Option<&str>) -> SmbResult<Vec<SmbDirEntry>> {
        match self {
            LocalHandle::File { .. } => Err(SmbError::NotADirectory),
            LocalHandle::Dir { dir_handle, .. } => {
                let dir_handle = Arc::clone(dir_handle);
                let pat = pattern.map(|s| s.to_owned());
                spawn_blocking(move || -> io::Result<Vec<SmbDirEntry>> {
                    let mut out = Vec::new();
                    for entry in dir_handle.entries()? {
                        let entry = entry?;
                        let os_name = entry.file_name();
                        let Some(name) = os_name.to_str().map(str::to_owned) else {
                            // Skip non-UTF-8 names; SMB wire format is UTF-16
                            // and we never want to emit invalid Unicode here.
                            continue;
                        };
                        if let Some(p) = pat.as_deref() {
                            // Empty / "*" / "*.*" all mean "match everything"
                            // in DOS-speak.
                            if !(p.is_empty() || p == "*" || p == "*.*" || glob_match(p, &name)) {
                                continue;
                            }
                        }
                        let md = entry.metadata()?;
                        let info = file_info_from_metadata(name, &md);
                        out.push(SmbDirEntry { info });
                    }
                    Ok(out)
                })
                .await
                .map_err(join_to_io)
                .map_err(io_to_smb)?
                .map_err(io_to_smb)
            }
        }
    }

    async fn close(self: Box<Self>) -> SmbResult<()> {
        // Drop is sufficient — closing the underlying handle is what the OS
        // does when the last `Arc` ref goes away. No flush here: SMB CLOSE
        // does not imply fsync.
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::{OpenIntent, OpenOptions};
    use crate::path::SmbPath;
    use tempfile::tempdir;

    fn p(s: &str) -> SmbPath {
        s.parse::<SmbPath>().unwrap()
    }

    fn opts_create() -> OpenOptions {
        OpenOptions {
            read: true,
            write: true,
            intent: OpenIntent::Create,
            directory: false,
            non_directory: false,
            delete_on_close: false,
        }
    }

    fn opts_open_rw() -> OpenOptions {
        OpenOptions {
            read: true,
            write: true,
            intent: OpenIntent::Open,
            directory: false,
            non_directory: false,
            delete_on_close: false,
        }
    }

    fn opts_open_ro() -> OpenOptions {
        OpenOptions {
            read: true,
            write: false,
            intent: OpenIntent::Open,
            directory: false,
            non_directory: false,
            delete_on_close: false,
        }
    }

    fn opts_open_dir() -> OpenOptions {
        OpenOptions {
            read: true,
            write: false,
            intent: OpenIntent::Open,
            directory: true,
            non_directory: false,
            delete_on_close: false,
        }
    }

    #[tokio::test]
    async fn create_write_read_stat_close() {
        let td = tempdir().unwrap();
        let backend = LocalFsBackend::new(td.path()).unwrap();

        // Create
        let h = backend.open(&p("hello.txt"), opts_create()).await.unwrap();
        let n = h.write(0, b"hello world").await.unwrap();
        assert_eq!(n, 11);
        h.flush().await.unwrap();

        // Stat
        let info = h.stat().await.unwrap();
        assert_eq!(info.name, "hello.txt");
        assert_eq!(info.end_of_file, 11);
        assert!(!info.is_directory);
        assert!(info.last_write_time > 0);
        h.close().await.unwrap();

        // Reopen for read
        let h2 = backend.open(&p("hello.txt"), opts_open_ro()).await.unwrap();
        let bytes = h2.read(0, 1024).await.unwrap();
        assert_eq!(&bytes[..], b"hello world");

        // Short-read past EOF returns truncated
        let bytes = h2.read(6, 1024).await.unwrap();
        assert_eq!(&bytes[..], b"world");

        // Read past EOF returns empty
        let bytes = h2.read(100, 1024).await.unwrap();
        assert!(bytes.is_empty());
        h2.close().await.unwrap();
    }

    #[tokio::test]
    async fn list_dir_finds_created_file() {
        let td = tempdir().unwrap();
        let backend = LocalFsBackend::new(td.path()).unwrap();
        let h = backend.open(&p("a.txt"), opts_create()).await.unwrap();
        h.close().await.unwrap();

        let dir_h = backend
            .open(&SmbPath::root(), opts_open_dir())
            .await
            .unwrap();
        let entries = dir_h.list_dir(None).await.unwrap();
        assert!(entries.iter().any(|e| e.info.name == "a.txt"));
        dir_h.close().await.unwrap();
    }

    #[tokio::test]
    async fn read_only_rejects_writes() {
        let td = tempdir().unwrap();
        // Pre-create a file via a writable backend so we have something to
        // attempt to open RW.
        {
            let writable = LocalFsBackend::new(td.path()).unwrap();
            let h = writable.open(&p("x.txt"), opts_create()).await.unwrap();
            h.close().await.unwrap();
        }

        let backend = LocalFsBackend::new(td.path()).unwrap().read_only();
        assert!(backend.capabilities().is_read_only);

        // RW open should be rejected.
        let err = backend
            .open(&p("x.txt"), opts_open_rw())
            .await
            .err()
            .unwrap();
        assert!(matches!(err, SmbError::AccessDenied));

        // Create should be rejected.
        let err = backend
            .open(&p("y.txt"), opts_create())
            .await
            .err()
            .unwrap();
        assert!(matches!(err, SmbError::AccessDenied));

        // Pure read open is fine.
        let h = backend.open(&p("x.txt"), opts_open_ro()).await.unwrap();
        // Writing through a handle obtained from a read-only backend would
        // already be impossible — but if a backend ever yields one, the
        // check still bites.
        h.close().await.unwrap();

        // unlink rejected.
        let err = backend.unlink(&p("x.txt")).await.err().unwrap();
        assert!(matches!(err, SmbError::AccessDenied));
    }

    #[tokio::test]
    async fn unlink_file_then_nonempty_dir_errors() {
        let td = tempdir().unwrap();
        let backend = LocalFsBackend::new(td.path()).unwrap();

        // Create & remove a file.
        let h = backend.open(&p("doomed.txt"), opts_create()).await.unwrap();
        h.close().await.unwrap();
        backend.unlink(&p("doomed.txt")).await.unwrap();
        assert!(matches!(
            backend.unlink(&p("doomed.txt")).await.err().unwrap(),
            SmbError::NotFound
        ));

        // Create a non-empty directory; unlink should fail with NotEmpty.
        std::fs::create_dir(td.path().join("dir1")).unwrap();
        std::fs::write(td.path().join("dir1").join("inside"), b"x").unwrap();

        let err = backend.unlink(&p("dir1")).await.err().unwrap();
        assert!(
            matches!(err, SmbError::NotEmpty),
            "expected NotEmpty, got {err:?}"
        );

        // Empty it and retry.
        std::fs::remove_file(td.path().join("dir1").join("inside")).unwrap();
        backend.unlink(&p("dir1")).await.unwrap();
    }

    #[tokio::test]
    async fn rename_within_root() {
        let td = tempdir().unwrap();
        let backend = LocalFsBackend::new(td.path()).unwrap();

        let h = backend.open(&p("old.txt"), opts_create()).await.unwrap();
        h.write(0, b"data").await.unwrap();
        h.close().await.unwrap();

        backend.rename(&p("old.txt"), &p("new.txt")).await.unwrap();
        assert!(td.path().join("new.txt").exists());
        assert!(!td.path().join("old.txt").exists());

        // Renaming over an existing target should fail.
        let h = backend.open(&p("other.txt"), opts_create()).await.unwrap();
        h.close().await.unwrap();
        let err = backend
            .rename(&p("other.txt"), &p("new.txt"))
            .await
            .err()
            .unwrap();
        assert!(matches!(err, SmbError::Exists), "got {err:?}");
    }

    #[tokio::test]
    async fn list_dir_pattern_matching() {
        let td = tempdir().unwrap();
        let backend = LocalFsBackend::new(td.path()).unwrap();

        for name in ["a.txt", "b.txt", "c.log", "README"] {
            let h = backend.open(&p(name), opts_create()).await.unwrap();
            h.close().await.unwrap();
        }

        let dir_h = backend
            .open(&SmbPath::root(), opts_open_dir())
            .await
            .unwrap();

        let txts = dir_h.list_dir(Some("*.txt")).await.unwrap();
        let names: Vec<_> = txts.iter().map(|e| e.info.name.as_str()).collect();
        assert_eq!(names.len(), 2, "expected 2 .txt files, got {names:?}");
        assert!(names.contains(&"a.txt"));
        assert!(names.contains(&"b.txt"));

        // Single-char wildcard.
        let one = dir_h.list_dir(Some("?.log")).await.unwrap();
        let names: Vec<_> = one.iter().map(|e| e.info.name.as_str()).collect();
        assert_eq!(names, vec!["c.log"]);

        // Case-insensitive.
        let any_txt = dir_h.list_dir(Some("*.TXT")).await.unwrap();
        assert_eq!(any_txt.len(), 2);

        // "*" matches everything.
        let all = dir_h.list_dir(Some("*")).await.unwrap();
        assert_eq!(all.len(), 4);

        dir_h.close().await.unwrap();
    }

    #[test]
    fn glob_match_basics() {
        assert!(glob_match("*", "anything"));
        assert!(glob_match("*.txt", "foo.txt"));
        assert!(!glob_match("*.txt", "foo.log"));
        assert!(glob_match("a?c", "abc"));
        assert!(!glob_match("a?c", "ac"));
        assert!(glob_match("a*b*c", "axxxbxxxc"));
        assert!(glob_match("FOO", "foo"));
        assert!(glob_match("", ""));
        assert!(!glob_match("", "a"));
    }

    #[test]
    fn filetime_round_trip() {
        let now = SystemTime::now();
        let ft = system_time_to_filetime(now);
        let back = filetime_to_system_time(ft).unwrap();
        let delta = now
            .duration_since(back)
            .or_else(|e| Ok::<_, std::time::SystemTimeError>(e.duration()))
            .unwrap();
        // 100ns granularity — round-trip should be sub-microsecond.
        assert!(delta < Duration::from_micros(1), "delta = {delta:?}");
    }
}
