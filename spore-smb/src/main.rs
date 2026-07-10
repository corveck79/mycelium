// spore-smb exposes Mycelium's virtual library as a read-only, anonymous SMB2/3
// share. It is the SMB twin of spore-nfs (see ../spore-nfs/main.go): it carries
// no media data itself, every open() asks Mycelium's /spore-nfs/tree for the
// virtual listing, every stat() asks /spore-nfs/size/<token> or (once played)
// reuses the real size learned from /spore-stream/<token>, and every read()
// re-issues a Range request against that same endpoint (or a cached CDN url).
//
// Unlike NFSv3, SMB2/3 handles are genuinely stateful (one open() per CREATE,
// held until close()), so per-file position bookkeeping doesn't need to live
// in a side table keyed by token the way spore-nfs's sporeFile does -- but the
// read-ahead window cache still does, since Plex's background analysis pass
// and the main playback stream can open two concurrent handles on the same
// token at different offsets.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use bytes::Bytes;
use serde::Deserialize;
use smb_server::{
    BackendCapabilities, DirEntry, FileInfo, FileTimes, Handle, OpenOptions, Share, ShareBackend,
    SmbError, SmbPath, SmbResult, SmbServer,
};
use tokio::sync::{Mutex, RwLock, Semaphore};

fn env_or(k: &str, def: &str) -> String {
    std::env::var(k).unwrap_or_else(|_| def.to_string())
}

fn to_io_err<E: std::fmt::Display>(e: E) -> SmbError {
    SmbError::Io(std::io::Error::other(e.to_string()))
}

fn basename(p: &str) -> String {
    p.rsplit('/').next().unwrap_or("").to_string()
}

fn dirname(p: &str) -> &str {
    match p.rfind('/') {
        Some(i) => &p[..i],
        None => "",
    }
}

fn clean_path(p: &SmbPath) -> String {
    p.display_backslash()
        .replace('\\', "/")
        .trim_matches('/')
        .to_string()
}

fn file_info(name: &str, size: i64, is_dir: bool) -> FileInfo {
    FileInfo {
        name: name.to_string(),
        end_of_file: size.max(0) as u64,
        allocation_size: size.max(0) as u64,
        creation_time: 0,
        last_access_time: 0,
        last_write_time: 0,
        change_time: 0,
        is_directory: is_dir,
        file_index: 0,
    }
}

// ---- virtual tree -----------------------------------------------------

#[derive(Deserialize)]
struct TreeEntry {
    token: String,
    path: String,
}

#[derive(Deserialize)]
struct TreeResponse {
    entries: Vec<TreeEntry>,
}

struct TreeInner {
    by_path: HashMap<String, String>,
    dirs: std::collections::HashSet<String>,
    fetched_at: Option<Instant>,
}

struct Tree {
    inner: RwLock<TreeInner>,
    ttl: Duration,
}

impl Tree {
    fn new() -> Self {
        let mut dirs = std::collections::HashSet::new();
        dirs.insert(String::new());
        Tree {
            inner: RwLock::new(TreeInner {
                by_path: HashMap::new(),
                dirs,
                fetched_at: None,
            }),
            ttl: Duration::from_secs(10),
        }
    }

    async fn refresh_if_stale(&self, state: &AppState) {
        let stale = {
            let g = self.inner.read().await;
            match g.fetched_at {
                None => true,
                Some(t) => t.elapsed() > self.ttl,
            }
        };
        if stale {
            self.refresh(state).await;
        }
    }

    async fn refresh(&self, state: &AppState) {
        let url = format!("{}/spore-nfs/tree", state.base_url);
        let resp = match state.client.get(&url).send().await {
            Ok(r) => r,
            Err(e) => {
                eprintln!("tree refresh: {e}");
                return;
            }
        };
        if !resp.status().is_success() {
            eprintln!("tree refresh: unexpected status {}", resp.status());
            return;
        }
        let parsed: TreeResponse = match resp.json().await {
            Ok(v) => v,
            Err(e) => {
                eprintln!("tree refresh decode: {e}");
                return;
            }
        };

        let mut by_path = HashMap::new();
        let mut dirs = std::collections::HashSet::new();
        dirs.insert(String::new());
        for e in parsed.entries {
            let clean = e.path.trim_matches('/').to_string();
            let mut dir = dirname(&clean).to_string();
            loop {
                if dir.is_empty() {
                    break;
                }
                if !dirs.insert(dir.clone()) {
                    break;
                }
                dir = dirname(&dir).to_string();
            }
            by_path.insert(clean, e.token);
        }

        let count = by_path.len();
        let dcount = dirs.len();
        let mut g = self.inner.write().await;
        g.by_path = by_path;
        g.dirs = dirs;
        g.fetched_at = Some(Instant::now());
        drop(g);
        eprintln!("tree refreshed: {count} files, {dcount} dirs");
    }

    async fn token_for(&self, state: &AppState, p: &str) -> Option<String> {
        self.refresh_if_stale(state).await;
        self.inner.read().await.by_path.get(p).cloned()
    }

    async fn is_dir(&self, state: &AppState, p: &str) -> bool {
        self.refresh_if_stale(state).await;
        self.inner.read().await.dirs.contains(p)
    }

    async fn children(&self, state: &AppState, dir: &str) -> Vec<String> {
        self.refresh_if_stale(state).await;
        let g = self.inner.read().await;
        let mut seen = std::collections::HashSet::new();
        let mut out = Vec::new();
        let mut add = |name: String| {
            if !name.is_empty() && seen.insert(name.clone()) {
                out.push(name);
            }
        };
        for p in g.by_path.keys() {
            let d = dirname(p);
            if d == dir || (dir.is_empty() && !p.contains('/')) {
                add(basename(p));
            }
        }
        for d in g.dirs.iter() {
            if d.is_empty() {
                continue;
            }
            let parent = dirname(d);
            if parent == dir || (dir.is_empty() && !d.contains('/')) {
                add(basename(d));
            }
        }
        out
    }
}

// ---- HTTP-backed size / content ----------------------------------------

struct AppState {
    base_url: String,
    client: reqwest::Client,
    no_redirect_client: reqwest::Client,
    tree: Tree,
    real_size_cache: RwLock<HashMap<String, (i64, Instant)>>,
    cdn_url_cache: RwLock<HashMap<String, (String, Instant)>>,
    read_aheads: RwLock<HashMap<String, Arc<Mutex<ReadAheadSet>>>>,
    // Now that dispatch is concurrent (see vendor/smb-server's reader.rs),
    // several reads -- from one client's read-ahead, or several concurrent
    // viewers sharing the connection -- can hit read_range() at once. Left
    // unbounded, a burst of fresh-window fetches has been observed to trip
    // TorBox's CDN rate limit (HTTP 429) under smbclient's own parallel_read
    // acceleration. Cap concurrent CDN/backend fetches so bursts queue
    // client-side instead of hammering the CDN.
    fetch_limiter: Semaphore,
}

impl AppState {
    async fn cached_real_size(&self, token: &str) -> Result<i64, SmbError> {
        if let Some(sz) = self.peek_real_size_valid(token).await {
            return Ok(sz);
        }
        let size = self.real_size(token).await?;
        let mut g = self.real_size_cache.write().await;
        g.insert(token.to_string(), (size, Instant::now() + Duration::from_secs(30 * 60)));
        Ok(size)
    }

    async fn peek_real_size_valid(&self, token: &str) -> Option<i64> {
        let g = self.real_size_cache.read().await;
        let (size, exp) = g.get(token)?;
        if Instant::now() < *exp {
            Some(*size)
        } else {
            None
        }
    }

    async fn real_size(&self, token: &str) -> Result<i64, SmbError> {
        let url = format!("{}/spore-stream/{}", self.base_url, token);
        let resp = self
            .client
            .head(&url)
            .send()
            .await
            .map_err(to_io_err)?;
        let status = resp.status();
        if !status.is_success() && status.as_u16() != 302 {
            return Err(to_io_err(format!("HEAD {token}: status {status}")));
        }
        let cl = resp
            .headers()
            .get(reqwest::header::CONTENT_LENGTH)
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| to_io_err(format!("HEAD {token}: no Content-Length")))?;
        cl.parse::<i64>().map_err(to_io_err)
    }

    // Non-materializing size lookup, used for library scans (ReadDir/Stat) so
    // that browsing the tree doesn't add every scanned item to TorBox just to
    // learn its size.
    async fn cheap_size(&self, token: &str) -> Result<i64, SmbError> {
        let url = format!("{}/spore-nfs/size/{}", self.base_url, token);
        let resp = self.client.get(&url).send().await.map_err(to_io_err)?;
        if !resp.status().is_success() {
            return Err(to_io_err(format!("size lookup {token}: status {}", resp.status())));
        }
        #[derive(Deserialize)]
        struct Out {
            size: i64,
        }
        let out: Out = resp.json().await.map_err(to_io_err)?;
        Ok(out.size)
    }

    // Issues a Range GET, retrying with backoff on 429 instead of surfacing
    // it immediately. Concurrent dispatch means several reads can land on
    // TorBox's CDN at once even with fetch_limiter capping how many spore-smb
    // itself has in flight -- a 429 means the CDN's own rate limit needs a
    // moment to clear, not that the request is doomed. Without this, a rate
    // limit hit turned into a hard read error that the SMB client (which has
    // no backoff of its own) just retried into immediately, going nowhere.
    async fn range_get_with_retry(
        &self,
        client: &reqwest::Client,
        url: &str,
        offset: i64,
        length: i64,
    ) -> Result<reqwest::Response, SmbError> {
        let range = format!("bytes={}-{}", offset, offset + length - 1);
        let mut attempt = 0u32;
        loop {
            let resp = client
                .get(url)
                .header(reqwest::header::RANGE, range.clone())
                .send()
                .await
                .map_err(to_io_err)?;
            if resp.status().as_u16() == 429 && attempt < MAX_429_RETRIES {
                let backoff = RETRY_BASE_DELAY * 2u32.pow(attempt);
                eprintln!(
                    "range GET {url}: 429 rate limited, retrying in {backoff:?} (attempt {}/{MAX_429_RETRIES})",
                    attempt + 1
                );
                tokio::time::sleep(backoff).await;
                attempt += 1;
                continue;
            }
            return Ok(resp);
        }
    }

    async fn fetch_range(&self, client: &reqwest::Client, url: &str, offset: i64, length: i64) -> Result<Bytes, SmbError> {
        let resp = self.range_get_with_retry(client, url, offset, length).await?;
        let status = resp.status();
        if status.as_u16() != 206 && status.as_u16() != 200 {
            return Err(to_io_err(format!("range GET {url}: status {status}")));
        }
        resp.bytes().await.map(|b| b.slice(0..(b.len().min(length as usize)))).map_err(to_io_err)
    }

    // Same self-healing behaviour as spore-nfs's readRange: a cached CDN url
    // can silently expire (TorBox rotates it, or catbox's own idle cleanup
    // fires) well before our TTL does. A failed read against a cached url
    // drops it and re-resolves via /spore-stream/<token> instead of
    // surfacing an opaque I/O error to the SMB client with nothing logged.
    async fn read_range(&self, token: &str, offset: i64, length: i64) -> Result<Bytes, SmbError> {
        let _permit = self
            .fetch_limiter
            .acquire()
            .await
            .expect("fetch_limiter is never closed");
        let cached = {
            let g = self.cdn_url_cache.read().await;
            g.get(token).cloned()
        };
        if let Some((url, exp)) = cached {
            if Instant::now() < exp {
                match self.fetch_range(&self.client, &url, offset, length).await {
                    Ok(b) => return Ok(b),
                    Err(e) => {
                        eprintln!("cached CDN url for {token} failed ({e:?}), re-resolving via spore-stream");
                        self.cdn_url_cache.write().await.remove(token);
                    }
                }
            }
        }

        let target = format!("{}/spore-stream/{}", self.base_url, token);
        let resp = self
            .range_get_with_retry(&self.no_redirect_client, &target, offset, length)
            .await?;

        let status = resp.status();
        if status.as_u16() == 302 || status.as_u16() == 301 {
            let loc = resp
                .headers()
                .get(reqwest::header::LOCATION)
                .and_then(|v| v.to_str().ok())
                .ok_or_else(|| to_io_err(format!("range GET {token}: redirect with no Location")))?
                .to_string();
            self.cdn_url_cache.write().await.insert(
                token.to_string(),
                (loc.clone(), Instant::now() + Duration::from_secs(50 * 60)),
            );
            return self.fetch_range(&self.client, &loc, offset, length).await;
        }

        if status.as_u16() != 206 && status.as_u16() != 200 {
            return Err(to_io_err(format!("range GET {token}: status {status}")));
        }
        resp.bytes().await.map(|b| b.slice(0..(b.len().min(length as usize)))).map_err(to_io_err)
    }
}

// ---- read-ahead ---------------------------------------------------------

const READ_AHEAD_SIZE: i64 = 16 << 20;
const READ_AHEAD_WINDOWS: usize = 3;
const SCAN_PROBE_THRESHOLD: i64 = 256 << 10;
const PROBE_MIN_FETCH: i64 = 1 << 20;

// ---- CDN rate-limit backoff ----------------------------------------------

const MAX_429_RETRIES: u32 = 4;
const RETRY_BASE_DELAY: Duration = Duration::from_millis(300);

#[derive(Clone)]
struct Window {
    data: Bytes,
    start: i64,
    used: i64,
}

struct ReadAheadSet {
    windows: Vec<Option<Window>>,
    clock: i64,
    pending: std::collections::HashSet<i64>,
}

impl ReadAheadSet {
    fn new() -> Self {
        ReadAheadSet {
            windows: vec![None; READ_AHEAD_WINDOWS],
            clock: 0,
            pending: std::collections::HashSet::new(),
        }
    }

    fn find(&self, start: i64) -> Option<Window> {
        self.windows
            .iter()
            .flatten()
            .find(|w| w.start == start)
            .cloned()
    }

    fn store(&mut self, w: Window) {
        let mut lru = 0;
        let mut lru_used = i64::MAX;
        for (i, slot) in self.windows.iter().enumerate() {
            let used = slot.as_ref().map(|w| w.used).unwrap_or(i64::MIN);
            if used < lru_used {
                lru_used = used;
                lru = i;
            }
        }
        self.windows[lru] = Some(w);
    }
}

fn grid_start(offset: i64) -> i64 {
    (offset / READ_AHEAD_SIZE) * READ_AHEAD_SIZE
}

async fn get_read_ahead_set(state: &Arc<AppState>, token: &str) -> Arc<Mutex<ReadAheadSet>> {
    if let Some(s) = state.read_aheads.read().await.get(token) {
        return s.clone();
    }
    let mut g = state.read_aheads.write().await;
    g.entry(token.to_string())
        .or_insert_with(|| Arc::new(Mutex::new(ReadAheadSet::new())))
        .clone()
}

async fn prefetch(state: Arc<AppState>, set: Arc<Mutex<ReadAheadSet>>, token: String, start: i64, file_size: i64) {
    let mut fetch_len = READ_AHEAD_SIZE;
    if start + fetch_len > file_size {
        fetch_len = file_size - start;
    }
    let result = state.read_range(&token, start, fetch_len).await;
    let mut s = set.lock().await;
    s.pending.remove(&start);
    if let Ok(data) = result {
        s.clock += 1;
        let clock = s.clock;
        s.store(Window { data, start, used: clock });
    }
}

async fn buffered_read(state: &Arc<AppState>, token: &str, offset: i64, want: i64, file_size: i64) -> Result<Bytes, SmbError> {
    let set = get_read_ahead_set(state, token).await;
    let start = grid_start(offset);

    let mut s = set.lock().await;
    s.clock += 1;
    let clock = s.clock;

    let window = match s.find(start) {
        Some(w) => {
            drop(s);
            w
        }
        None => {
            // Genuine miss: unavoidably blocks this reader. Small probe-sized
            // reads (Plex's scanner peeking a header) get a fetch close to
            // what they actually asked for instead of a full 16MB window, so
            // they don't time out waiting on it -- see spore-nfs's
            // bufferedRead for the same reasoning.
            let mut fetch_len = READ_AHEAD_SIZE;
            if want < SCAN_PROBE_THRESHOLD {
                fetch_len = want.max(PROBE_MIN_FETCH);
            }
            if offset + want - start > fetch_len {
                fetch_len = offset + want - start;
            }
            if start + fetch_len > file_size {
                fetch_len = file_size - start;
            }
            drop(s);
            let data = state.read_range(token, start, fetch_len).await?;
            let mut s2 = set.lock().await;
            let w = Window { data, start, used: clock };
            s2.store(w.clone());
            w
        }
    };

    let rel = offset - window.start;
    let mut end = rel + want;
    if end > window.data.len() as i64 {
        end = window.data.len() as i64;
    }

    // Past the midpoint of this grid cell: kick off the next window now, in
    // the background, so a sequential reader doesn't block on it later.
    let mut s3 = set.lock().await;
    if rel > window.data.len() as i64 / 2 {
        let next = start + READ_AHEAD_SIZE;
        if next < file_size && s3.find(next).is_none() && !s3.pending.contains(&next) {
            s3.pending.insert(next);
            tokio::spawn(prefetch(state.clone(), set.clone(), token.to_string(), next, file_size));
        }
    }
    drop(s3);

    Ok(window.data.slice((rel.max(0) as usize)..(end.max(0) as usize)))
}

// ---- ShareBackend / Handle ----------------------------------------------

struct SporeBackend {
    state: Arc<AppState>,
}

#[async_trait]
impl ShareBackend for SporeBackend {
    async fn open(&self, path: &SmbPath, opts: OpenOptions) -> SmbResult<Box<dyn Handle>> {
        if opts.write {
            return Err(SmbError::AccessDenied);
        }
        let p = clean_path(path);
        if let Some(token) = self.state.tree.token_for(&self.state, &p).await {
            let size = self.state.cached_real_size(&token).await?;
            return Ok(Box::new(SporeHandle {
                state: self.state.clone(),
                kind: HandleKind::File { token, size },
            }));
        }
        if p.is_empty() || self.state.tree.is_dir(&self.state, &p).await {
            return Ok(Box::new(SporeHandle {
                state: self.state.clone(),
                kind: HandleKind::Dir(p),
            }));
        }
        Err(SmbError::NotFound)
    }

    async fn unlink(&self, _path: &SmbPath) -> SmbResult<()> {
        Err(SmbError::NotSupported)
    }

    async fn rename(&self, _from: &SmbPath, _to: &SmbPath) -> SmbResult<()> {
        Err(SmbError::NotSupported)
    }

    fn capabilities(&self) -> BackendCapabilities {
        BackendCapabilities {
            is_read_only: true,
            case_sensitive: false,
        }
    }
}

enum HandleKind {
    File { token: String, size: i64 },
    Dir(String),
}

struct SporeHandle {
    state: Arc<AppState>,
    kind: HandleKind,
}

#[async_trait]
impl Handle for SporeHandle {
    async fn read(&self, offset: u64, len: u32) -> SmbResult<Bytes> {
        match &self.kind {
            HandleKind::File { token, size } => {
                let offset = offset as i64;
                if offset >= *size {
                    return Ok(Bytes::new());
                }
                let want = (len as i64).min(size - offset);
                buffered_read(&self.state, token, offset, want, *size).await
            }
            HandleKind::Dir(_) => Err(SmbError::IsDirectory),
        }
    }

    async fn write(&self, _offset: u64, _data: &[u8]) -> SmbResult<u32> {
        Err(SmbError::AccessDenied)
    }

    async fn flush(&self) -> SmbResult<()> {
        Ok(())
    }

    async fn stat(&self) -> SmbResult<FileInfo> {
        match &self.kind {
            HandleKind::File { token: _, size } => Ok(file_info("", *size, false)),
            HandleKind::Dir(p) => Ok(file_info(&basename(p), 0, true)),
        }
    }

    async fn set_times(&self, _times: FileTimes) -> SmbResult<()> {
        Ok(())
    }

    async fn truncate(&self, _len: u64) -> SmbResult<()> {
        Err(SmbError::AccessDenied)
    }

    async fn list_dir(&self, _pattern: Option<&str>) -> SmbResult<Vec<DirEntry>> {
        let p = match &self.kind {
            HandleKind::Dir(p) => p.clone(),
            HandleKind::File { .. } => return Err(SmbError::NotADirectory),
        };
        let mut out = Vec::new();
        for name in self.state.tree.children(&self.state, &p).await {
            let child = if p.is_empty() {
                name.clone()
            } else {
                format!("{p}/{name}")
            };
            if self.state.tree.is_dir(&self.state, &child).await {
                out.push(DirEntry {
                    info: file_info(&name, 0, true),
                });
                continue;
            }
            if let Some(token) = self.state.tree.token_for(&self.state, &child).await {
                let size = self.state.cheap_size(&token).await.unwrap_or(0);
                out.push(DirEntry {
                    info: file_info(&name, size, false),
                });
            }
        }
        Ok(out)
    }

    async fn close(self: Box<Self>) -> SmbResult<()> {
        Ok(())
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let base_url = env_or("MYCELIUM_BASE", "http://127.0.0.1:8088");
    let listen: std::net::SocketAddr = env_or("LISTEN_ADDR", "0.0.0.0:445").parse()?;
    let max_concurrent_fetches: usize = env_or("MAX_CONCURRENT_FETCHES", "4").parse()?;

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()?;
    let no_redirect_client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;

    let state = Arc::new(AppState {
        base_url,
        client,
        no_redirect_client,
        tree: Tree::new(),
        real_size_cache: RwLock::new(HashMap::new()),
        cdn_url_cache: RwLock::new(HashMap::new()),
        read_aheads: RwLock::new(HashMap::new()),
        fetch_limiter: Semaphore::new(max_concurrent_fetches),
    });

    state.tree.refresh(&state).await;

    // Retry independently of incoming requests: a fresh container can win
    // the race against mycelium's own startup (same reasoning as spore-nfs).
    {
        let state = state.clone();
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(Duration::from_secs(10));
            loop {
                ticker.tick().await;
                state.tree.refresh(&state).await;
            }
        });
    }

    let backend = SporeBackend { state: state.clone() };
    let server = SmbServer::builder()
        .listen(listen)
        .share(Share::new("media", backend).public_read_only())
        .build()?;

    eprintln!("spore-smb listening on {listen}, backing store = {}", state.base_url);
    server.bind().await?;
    server.serve().await?;
    Ok(())
}
