use std::collections::HashMap;
use std::sync::Mutex;

use crate::backend::{
    BackendCapabilities, DirEntry, FileInfo, FileTimes, Handle, OpenIntent, OpenOptions,
    ShareBackend,
};
use crate::error::{SmbError, SmbResult};
use crate::path::SmbPath;
use async_trait::async_trait;
use bytes::Bytes;

/// Minimal in-memory FS used by integration tests. Files are byte vectors,
/// directories are sets of names. Not threadsafe across workers — only used
/// within one test.
pub struct MemFsBackend {
    inner: std::sync::Arc<Mutex<MemInner>>,
}

#[derive(Default)]
struct MemInner {
    files: HashMap<String, Vec<u8>>,
    /// All directories present (always includes "" for the root). Each
    /// directory is keyed by canonical path string.
    dirs: HashMap<String, ()>,
}

impl Default for MemFsBackend {
    fn default() -> Self {
        Self::new()
    }
}

impl MemFsBackend {
    pub fn new() -> Self {
        let mut inner = MemInner::default();
        inner.dirs.insert(String::new(), ());
        Self {
            inner: std::sync::Arc::new(Mutex::new(inner)),
        }
    }

    pub fn with_file(self, path: &str, contents: &[u8]) -> Self {
        {
            let mut g = self.inner.lock().unwrap();
            g.files.insert(path.to_string(), contents.to_vec());
        }
        self
    }
}

fn key(path: &SmbPath) -> String {
    path.display_backslash()
}

#[async_trait]
impl ShareBackend for MemFsBackend {
    async fn open(&self, path: &SmbPath, opts: OpenOptions) -> SmbResult<Box<dyn Handle>> {
        let k = key(path);
        let mut g = self.inner.lock().unwrap();
        let exists_file = g.files.contains_key(&k);
        let exists_dir = g.dirs.contains_key(&k);

        if opts.directory {
            if exists_file {
                return Err(SmbError::NotADirectory);
            }
            if !exists_dir {
                if matches!(opts.intent, OpenIntent::Create | OpenIntent::OpenOrCreate) {
                    g.dirs.insert(k.clone(), ());
                } else {
                    return Err(SmbError::NotFound);
                }
            }
            return Ok(Box::new(MemHandle::dir(self.inner.clone(), k)));
        }

        if exists_dir {
            return Err(SmbError::IsDirectory);
        }
        match opts.intent {
            OpenIntent::Open => {
                if !exists_file {
                    return Err(SmbError::NotFound);
                }
            }
            OpenIntent::Create => {
                if exists_file {
                    return Err(SmbError::Exists);
                }
                g.files.insert(k.clone(), Vec::new());
            }
            OpenIntent::OpenOrCreate => {
                g.files.entry(k.clone()).or_default();
            }
            OpenIntent::Truncate => {
                if !exists_file {
                    return Err(SmbError::NotFound);
                }
                g.files.insert(k.clone(), Vec::new());
            }
            OpenIntent::OverwriteOrCreate => {
                g.files.insert(k.clone(), Vec::new());
            }
        }
        Ok(Box::new(MemHandle::file(self.inner.clone(), k)))
    }

    async fn unlink(&self, path: &SmbPath) -> SmbResult<()> {
        let k = key(path);
        let mut g = self.inner.lock().unwrap();
        if g.files.remove(&k).is_some() {
            return Ok(());
        }
        if g.dirs.remove(&k).is_some() {
            return Ok(());
        }
        Err(SmbError::NotFound)
    }

    async fn rename(&self, from: &SmbPath, to: &SmbPath) -> SmbResult<()> {
        let kf = key(from);
        let kt = key(to);
        let mut g = self.inner.lock().unwrap();
        if g.files.contains_key(&kt) || g.dirs.contains_key(&kt) {
            return Err(SmbError::Exists);
        }
        if let Some(data) = g.files.remove(&kf) {
            g.files.insert(kt, data);
            return Ok(());
        }
        if g.dirs.remove(&kf).is_some() {
            g.dirs.insert(kt, ());
            return Ok(());
        }
        Err(SmbError::NotFound)
    }

    fn capabilities(&self) -> BackendCapabilities {
        BackendCapabilities {
            is_read_only: false,
            case_sensitive: false,
        }
    }
}

pub struct MemHandle {
    inner: std::sync::Arc<Mutex<MemInner>>,
    key: String,
    is_dir: bool,
}

impl MemHandle {
    fn file(inner: std::sync::Arc<Mutex<MemInner>>, key: String) -> Self {
        Self {
            inner,
            key,
            is_dir: false,
        }
    }

    fn dir(inner: std::sync::Arc<Mutex<MemInner>>, key: String) -> Self {
        Self {
            inner,
            key,
            is_dir: true,
        }
    }
}

#[async_trait]
impl Handle for MemHandle {
    async fn read(&self, offset: u64, len: u32) -> SmbResult<Bytes> {
        if self.is_dir {
            return Err(SmbError::IsDirectory);
        }
        let g = self.inner.lock().unwrap();
        let data = g.files.get(&self.key).ok_or(SmbError::NotFound)?;
        let start = offset as usize;
        if start >= data.len() {
            return Ok(Bytes::new());
        }
        let end = (start + len as usize).min(data.len());
        Ok(Bytes::copy_from_slice(&data[start..end]))
    }

    async fn write(&self, offset: u64, data: &[u8]) -> SmbResult<u32> {
        if self.is_dir {
            return Err(SmbError::IsDirectory);
        }
        let mut g = self.inner.lock().unwrap();
        let buf = g.files.get_mut(&self.key).ok_or(SmbError::NotFound)?;
        let needed = (offset as usize) + data.len();
        if buf.len() < needed {
            buf.resize(needed, 0);
        }
        buf[offset as usize..offset as usize + data.len()].copy_from_slice(data);
        Ok(data.len() as u32)
    }

    async fn flush(&self) -> SmbResult<()> {
        Ok(())
    }

    async fn stat(&self) -> SmbResult<FileInfo> {
        let g = self.inner.lock().unwrap();
        let size = if self.is_dir {
            0
        } else {
            g.files.get(&self.key).ok_or(SmbError::NotFound)?.len() as u64
        };
        let name = self
            .key
            .rsplit_once('\\')
            .map(|(_, n)| n.to_string())
            .unwrap_or_else(|| self.key.clone());
        Ok(FileInfo {
            name,
            end_of_file: size,
            allocation_size: size,
            creation_time: 0x01D9_0000_0000_0000,
            last_access_time: 0x01D9_0000_0000_0000,
            last_write_time: 0x01D9_0000_0000_0000,
            change_time: 0x01D9_0000_0000_0000,
            is_directory: self.is_dir,
            file_index: 0,
        })
    }

    async fn set_times(&self, _times: FileTimes) -> SmbResult<()> {
        Ok(())
    }

    async fn truncate(&self, len: u64) -> SmbResult<()> {
        if self.is_dir {
            return Err(SmbError::IsDirectory);
        }
        let mut g = self.inner.lock().unwrap();
        let buf = g.files.get_mut(&self.key).ok_or(SmbError::NotFound)?;
        buf.resize(len as usize, 0);
        Ok(())
    }

    async fn list_dir(&self, _pattern: Option<&str>) -> SmbResult<Vec<DirEntry>> {
        if !self.is_dir {
            return Err(SmbError::NotADirectory);
        }
        let g = self.inner.lock().unwrap();
        let prefix = if self.key.is_empty() {
            String::new()
        } else {
            format!("{}\\", self.key)
        };
        let mut entries = Vec::new();
        for (k, v) in g.files.iter() {
            if let Some(rest) = k.strip_prefix(&prefix)
                && !rest.contains('\\')
            {
                entries.push(DirEntry {
                    info: FileInfo {
                        name: rest.to_string(),
                        end_of_file: v.len() as u64,
                        allocation_size: v.len() as u64,
                        creation_time: 0x01D9_0000_0000_0000,
                        last_access_time: 0x01D9_0000_0000_0000,
                        last_write_time: 0x01D9_0000_0000_0000,
                        change_time: 0x01D9_0000_0000_0000,
                        is_directory: false,
                        file_index: 0,
                    },
                });
            }
        }
        for k in g.dirs.keys() {
            if let Some(rest) = k.strip_prefix(&prefix)
                && !rest.is_empty()
                && !rest.contains('\\')
            {
                entries.push(DirEntry {
                    info: FileInfo {
                        name: rest.to_string(),
                        end_of_file: 0,
                        allocation_size: 0,
                        creation_time: 0x01D9_0000_0000_0000,
                        last_access_time: 0x01D9_0000_0000_0000,
                        last_write_time: 0x01D9_0000_0000_0000,
                        change_time: 0x01D9_0000_0000_0000,
                        is_directory: true,
                        file_index: 0,
                    },
                });
            }
        }
        Ok(entries)
    }

    async fn close(self: Box<Self>) -> SmbResult<()> {
        Ok(())
    }
}
