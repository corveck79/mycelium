//! Top-level `SmbServer` lifecycle: builder integration, accept loop,
//! graceful shutdown.

use std::collections::HashMap;
use std::io;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Weak};

use crate::proto::auth::ntlm::UserCreds;
use thiserror::Error;
use tokio::net::TcpListener;
use tokio::sync::{Notify, RwLock};
use tracing::{Instrument, error, info, info_span};
use uuid::Uuid;

use crate::backend::ShareBackend;
use crate::builder::{Access, Share, SmbServerBuilder};
use crate::conn::connection_loop;
use crate::conn::state::Connection;
use crate::utils::now_filetime;

// ---------------------------------------------------------------------------
// ShareMode / ShareBindings
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShareMode {
    Public,
    PublicReadOnly,
    /// Default — closed share. Only users in the explicit `users` map allowed.
    AuthenticatedOnly,
}

#[derive(Clone)]
pub struct ShareAcl {
    pub mode: ShareMode,
    pub users: HashMap<String, Access>,
}

/// Compiled binding for a single share — the per-server-state form of `Share`.
pub struct ShareBindings {
    pub name: String,
    pub backend: Arc<dyn ShareBackend>,
    pub acl: RwLock<ShareAcl>,
    /// `IPC$` synthetic share. Accepted at TREE_CONNECT for client compatibility
    /// (Windows always probes IPC$ before mounting an actual share). All
    /// downstream ops on an IPC$ tree return `STATUS_NOT_SUPPORTED`.
    pub is_ipc: bool,
}

impl ShareBindings {
    pub(crate) fn new(
        name: String,
        backend: Arc<dyn ShareBackend>,
        mode: ShareMode,
        users: HashMap<String, Access>,
        is_ipc: bool,
    ) -> Arc<Self> {
        Arc::new(Self {
            name,
            backend,
            acl: RwLock::new(ShareAcl { mode, users }),
            is_ipc,
        })
    }

    /// Synthetic IPC$ share. The backend is a no-op; clients that try to
    /// CREATE on it get `STATUS_NOT_SUPPORTED` from the CREATE handler.
    pub fn ipc() -> Arc<Self> {
        Self::new(
            "IPC$".to_string(),
            Arc::new(crate::backend::NotSupportedBackend),
            ShareMode::PublicReadOnly,
            HashMap::new(),
            true,
        )
    }
}

// ---------------------------------------------------------------------------
// ServerConfig / ServerUsers / ServerState
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub listen_addr: SocketAddr,
    pub netbios_name: String,
    pub max_read_size: u32,
    pub max_write_size: u32,
    pub server_guid: Uuid,
}

pub struct ServerUsers {
    /// Username → precomputed NT hash record.
    pub table: RwLock<HashMap<String, UserCreds>>,
}

pub struct ServerShares {
    by_name: RwLock<HashMap<String, Arc<ShareBindings>>>,
}

impl ServerShares {
    pub fn new(shares: Vec<Arc<ShareBindings>>) -> Self {
        let mut by_name = HashMap::with_capacity(shares.len());
        for share in shares {
            by_name.insert(share.name.to_ascii_lowercase(), share);
        }
        Self {
            by_name: RwLock::new(by_name),
        }
    }

    pub async fn find(&self, name: &str) -> Option<Arc<ShareBindings>> {
        self.by_name
            .read()
            .await
            .get(&name.to_ascii_lowercase())
            .cloned()
    }

    pub async fn insert(&self, share: Arc<ShareBindings>) -> Result<(), ConfigError> {
        let key = share.name.to_ascii_lowercase();
        let mut by_name = self.by_name.write().await;
        if by_name.contains_key(&key) {
            return Err(ConfigError::DuplicateShare(share.name.clone()));
        }
        by_name.insert(key, share);
        Ok(())
    }

    pub async fn remove(&self, name: &str) -> Option<Arc<ShareBindings>> {
        self.by_name
            .write()
            .await
            .remove(&name.to_ascii_lowercase())
    }

    pub async fn all(&self) -> Vec<Arc<ShareBindings>> {
        self.by_name.read().await.values().cloned().collect()
    }
}

pub struct ActiveConnections {
    next_id: AtomicU64,
    conns: RwLock<HashMap<u64, Weak<Connection>>>,
}

impl ActiveConnections {
    pub fn new() -> Self {
        Self {
            next_id: AtomicU64::new(1),
            conns: RwLock::new(HashMap::new()),
        }
    }

    pub async fn register(&self, conn: &Arc<Connection>) -> u64 {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        self.conns.write().await.insert(id, Arc::downgrade(conn));
        id
    }

    pub async fn unregister(&self, id: u64) {
        self.conns.write().await.remove(&id);
    }

    pub async fn live(&self) -> Vec<Arc<Connection>> {
        let mut live = Vec::new();
        let mut conns = self.conns.write().await;
        conns.retain(|_, weak| {
            if let Some(conn) = weak.upgrade() {
                live.push(conn);
                true
            } else {
                false
            }
        });
        live
    }
}

impl Default for ActiveConnections {
    fn default() -> Self {
        Self::new()
    }
}

/// Top-level immutable-ish state shared across connections.
pub struct ServerState {
    pub config: ServerConfig,
    pub users: ServerUsers,
    pub shares: ServerShares,
    pub active_connections: ActiveConnections,
    pub server_start_filetime: u64,
    /// Set when `shutdown()` is invoked; the accept loop stops on the next
    /// iteration and connection loops abandon their next read.
    pub shutdown: Arc<Notify>,
    pub shutting_down: Arc<AtomicBool>,
}

impl ServerState {
    pub fn new(config: ServerConfig, users: ServerUsers, shares: Vec<Arc<ShareBindings>>) -> Self {
        Self {
            config,
            users,
            shares: ServerShares::new(shares),
            active_connections: ActiveConnections::new(),
            server_start_filetime: now_filetime(),
            shutdown: Arc::new(Notify::new()),
            shutting_down: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Find a share by case-insensitive name.
    pub async fn find_share(&self, name: &str) -> Option<Arc<ShareBindings>> {
        self.shares.find(name).await
    }

    /// Look up a user's NT hash by name.
    pub async fn lookup_user(&self, name: &str) -> Option<UserCreds> {
        self.users.table.read().await.get(name).cloned()
    }

    /// Whether anonymous logon is permitted (i.e. at least one share is public).
    pub async fn anonymous_allowed(&self) -> bool {
        for share in self.shares.all().await {
            let acl = share.acl.read().await;
            if matches!(acl.mode, ShareMode::Public | ShareMode::PublicReadOnly) {
                return true;
            }
        }
        false
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ConfigError {
    #[error("user `{0}` does not exist")]
    UnknownUser(String),
    #[error("share `{0}` does not exist")]
    UnknownShare(String),
    #[error("duplicate share `{0}`")]
    DuplicateShare(String),
    #[error("share `{0}` mixes public mode with explicit users")]
    PublicMixedWithUsers(String),
    #[error("user name `{0}` is reserved")]
    ReservedUserName(String),
    #[error("user name must be non-empty")]
    EmptyUserName,
    #[error("share name `{0}` is reserved")]
    ReservedShareName(String),
}

#[derive(Clone)]
pub struct ConfigHandle {
    state: Arc<ServerState>,
}

impl ConfigHandle {
    pub async fn add_user(
        &self,
        name: impl Into<String>,
        password: impl AsRef<str>,
    ) -> Result<(), ConfigError> {
        let name = name.into();
        validate_user_name(&name)?;
        let creds = UserCreds::from_password(password.as_ref());
        self.state.users.table.write().await.insert(name, creds);
        Ok(())
    }

    pub async fn remove_user(&self, name: &str) -> Result<(), ConfigError> {
        validate_user_name(name)?;
        let removed = self.state.users.table.write().await.remove(name);
        if removed.is_none() {
            return Err(ConfigError::UnknownUser(name.to_string()));
        }

        for share in self.state.shares.all().await {
            share.acl.write().await.users.remove(name);
        }

        for conn in self.state.active_connections.live().await {
            conn.close_sessions_for_user(name).await;
        }
        Ok(())
    }

    pub async fn add_share(&self, share: Share) -> Result<(), ConfigError> {
        validate_share_name(&share.name)?;
        let is_public = matches!(share.mode, ShareMode::Public | ShareMode::PublicReadOnly);
        if is_public && !share.users.is_empty() {
            return Err(ConfigError::PublicMixedWithUsers(share.name));
        }
        let users = self.state.users.table.read().await;
        for user in share.users.keys() {
            if !users.contains_key(user) {
                return Err(ConfigError::UnknownUser(user.clone()));
            }
        }

        let binding = ShareBindings::new(share.name, share.backend, share.mode, share.users, false);
        self.state.shares.insert(binding).await
    }

    pub async fn remove_share(&self, name: &str) -> Result<(), ConfigError> {
        validate_share_name(name)?;
        let removed = self.state.shares.remove(name).await;
        if removed.is_none() {
            return Err(ConfigError::UnknownShare(name.to_string()));
        }

        for conn in self.state.active_connections.live().await {
            conn.close_trees_for_share(name).await;
        }
        Ok(())
    }

    pub async fn grant_share_user(
        &self,
        share_name: &str,
        user: &str,
        access: Access,
    ) -> Result<(), ConfigError> {
        validate_user_name(user)?;
        validate_share_name(share_name)?;
        let users = self.state.users.table.read().await;
        if !users.contains_key(user) {
            return Err(ConfigError::UnknownUser(user.to_string()));
        }
        let share = self
            .state
            .find_share(share_name)
            .await
            .ok_or_else(|| ConfigError::UnknownShare(share_name.to_string()))?;
        let mut acl = share.acl.write().await;
        if matches!(acl.mode, ShareMode::Public | ShareMode::PublicReadOnly) {
            return Err(ConfigError::PublicMixedWithUsers(share.name.clone()));
        }
        acl.users.insert(user.to_string(), access);
        Ok(())
    }

    pub async fn revoke_share_user(&self, share_name: &str, user: &str) -> Result<(), ConfigError> {
        validate_user_name(user)?;
        validate_share_name(share_name)?;
        let share = self
            .state
            .find_share(share_name)
            .await
            .ok_or_else(|| ConfigError::UnknownShare(share_name.to_string()))?;
        share.acl.write().await.users.remove(user);

        for conn in self.state.active_connections.live().await {
            conn.close_trees_for_user_share(user, share_name).await;
        }
        Ok(())
    }

    pub async fn set_share_mode(
        &self,
        share_name: &str,
        mode: ShareMode,
    ) -> Result<(), ConfigError> {
        validate_share_name(share_name)?;
        let share = self
            .state
            .find_share(share_name)
            .await
            .ok_or_else(|| ConfigError::UnknownShare(share_name.to_string()))?;
        let mut acl = share.acl.write().await;
        if matches!(mode, ShareMode::Public | ShareMode::PublicReadOnly) && !acl.users.is_empty() {
            return Err(ConfigError::PublicMixedWithUsers(share.name.clone()));
        }
        if acl.mode == mode {
            return Ok(());
        }
        acl.mode = mode;
        drop(acl);

        for conn in self.state.active_connections.live().await {
            conn.close_trees_for_share(share_name).await;
        }
        Ok(())
    }
}

fn validate_user_name(name: &str) -> Result<(), ConfigError> {
    if name.is_empty() {
        return Err(ConfigError::EmptyUserName);
    }
    if name.eq_ignore_ascii_case("anonymous") {
        return Err(ConfigError::ReservedUserName(name.to_string()));
    }
    Ok(())
}

fn validate_share_name(name: &str) -> Result<(), ConfigError> {
    if name.eq_ignore_ascii_case("IPC$") {
        return Err(ConfigError::ReservedShareName(name.to_string()));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// SmbServer
// ---------------------------------------------------------------------------

/// A built but not-yet-running SMB server.
///
/// Use `serve()` to bind the configured listener and run until shutdown.
pub struct SmbServer {
    state: Arc<ServerState>,
    /// The listener is bound lazily inside `serve()` so we can return a
    /// useful `local_addr` only after binding. Pre-bind helpers: `serve` is
    /// the only path that opens the socket.
    bound: tokio::sync::Mutex<Option<TcpListener>>,
    /// Resolved local address once `bind_local()` has been called. Tests
    /// expect to ask for the address before serving (port 0 case).
    local_addr: tokio::sync::Mutex<Option<SocketAddr>>,
}

impl SmbServer {
    pub fn builder() -> SmbServerBuilder {
        SmbServerBuilder::default()
    }

    pub(crate) fn from_state(state: ServerState) -> Self {
        Self {
            state: Arc::new(state),
            bound: tokio::sync::Mutex::new(None),
            local_addr: tokio::sync::Mutex::new(None),
        }
    }

    pub fn config_handle(&self) -> ConfigHandle {
        ConfigHandle {
            state: self.state.clone(),
        }
    }

    /// Bind the configured listen address without yet entering the accept
    /// loop. Required for tests that need the actual port (e.g. when the
    /// builder used port 0).
    pub async fn bind(&self) -> io::Result<SocketAddr> {
        let mut bound = self.bound.lock().await;
        if let Some(l) = bound.as_ref() {
            return l.local_addr();
        }
        let listener = TcpListener::bind(self.state.config.listen_addr).await?;
        let addr = listener.local_addr()?;
        *bound = Some(listener);
        *self.local_addr.lock().await = Some(addr);
        Ok(addr)
    }

    /// Returns the actual bound address. `None` if `bind()`/`serve()` have
    /// not yet been called.
    pub async fn local_addr(&self) -> Option<SocketAddr> {
        *self.local_addr.lock().await
    }

    /// Configured listen address (the *intended* address; may be `0.0.0.0:0`
    /// before binding).
    pub fn configured_addr(&self) -> SocketAddr {
        self.state.config.listen_addr
    }

    /// Initiate a graceful shutdown. Stops the accept loop and lets in-flight
    /// connection tasks complete.
    pub fn shutdown(&self) {
        self.state.shutting_down.store(true, Ordering::Release);
        self.state.shutdown.notify_waiters();
    }

    /// Returns a clonable handle that can request shutdown after `serve()`
    /// has consumed the `SmbServer` value.
    pub fn shutdown_handle(&self) -> ShutdownHandle {
        ShutdownHandle {
            shutdown: self.state.shutdown.clone(),
            shutting_down: self.state.shutting_down.clone(),
        }
    }

    /// Run the accept loop until `shutdown()` is called.
    pub async fn serve(self) -> io::Result<()> {
        // Ensure the listener is bound. (The user may also have called
        // `bind()` to pre-extract `local_addr()` for a test.)
        if self.bound.lock().await.is_none() {
            self.bind().await?;
        }
        let listener = self
            .bound
            .lock()
            .await
            .take()
            .expect("listener bound above");
        let local = listener.local_addr().ok();
        let span = info_span!("smb_server", listen = ?local);
        async move {
            info!("server starting");
            let state = self.state.clone();
            let shutdown = state.shutdown.clone();
            let shutting_down = state.shutting_down.clone();

            loop {
                tokio::select! {
                    biased;
                    _ = shutdown.notified() => {
                        info!("shutdown requested; stopping accept loop");
                        break;
                    }
                    accept = listener.accept() => {
                        match accept {
                            Ok((stream, peer)) => {
                                if shutting_down.load(Ordering::Acquire) {
                                    drop(stream);
                                    break;
                                }
                                let server_state = state.clone();
                                let span = info_span!("conn", peer = %peer);
                                tokio::spawn(async move {
                                    if let Err(e) = connection_loop(stream, server_state).await {
                                        error!(error = %e, "connection loop exited with error");
                                    }
                                }.instrument(span));
                            }
                            Err(e) => {
                                error!(error = %e, "accept failed");
                                if shutting_down.load(Ordering::Acquire) {
                                    break;
                                }
                            }
                        }
                    }
                }
            }
            info!("server stopped");
            Ok::<(), io::Error>(())
        }
        .instrument(span)
        .await
    }

    /// Access shared state for in-crate tests/integrations.
    #[doc(hidden)]
    pub fn state(&self) -> Arc<ServerState> {
        self.state.clone()
    }
}

/// Cheaply-clonable shutdown handle. Outlives `SmbServer::serve` consuming
/// the server.
#[derive(Clone)]
pub struct ShutdownHandle {
    shutdown: Arc<Notify>,
    shutting_down: Arc<AtomicBool>,
}

impl ShutdownHandle {
    /// Request a graceful shutdown.
    pub fn shutdown(&self) {
        self.shutting_down.store(true, Ordering::Release);
        self.shutdown.notify_waiters();
    }
}
