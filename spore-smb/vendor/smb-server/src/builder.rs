//! Public builder API for `SmbServer` and `Share`.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;

use thiserror::Error;
use uuid::Uuid;

use crate::backend::ShareBackend;
use crate::server::{ServerConfig, ServerState, ServerUsers, ShareBindings, ShareMode, SmbServer};

// ---------------------------------------------------------------------------
// Access
// ---------------------------------------------------------------------------

/// Access level granted to a user on a share, or to anonymous on a public
/// share.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Access {
    Read,
    ReadWrite,
}

impl Access {
    pub fn allows_write(self) -> bool {
        matches!(self, Access::ReadWrite)
    }

    pub fn clamp_to(self, cap: Access) -> Access {
        match (self, cap) {
            (Access::ReadWrite, Access::ReadWrite) => Access::ReadWrite,
            _ => Access::Read,
        }
    }
}

// ---------------------------------------------------------------------------
// Share
// ---------------------------------------------------------------------------

/// One share definition, attached to a single backend.
pub struct Share {
    pub(crate) name: String,
    pub(crate) backend: Arc<dyn ShareBackend>,
    pub(crate) mode: ShareMode,
    pub(crate) users: HashMap<String, Access>,
}

impl Share {
    /// Build a new share with the given name and backend.
    pub fn new(name: impl Into<String>, backend: impl ShareBackend) -> Self {
        Self {
            name: name.into(),
            backend: Arc::new(backend),
            mode: ShareMode::AuthenticatedOnly,
            users: HashMap::new(),
        }
    }

    /// Anonymous + authenticated read+write.
    pub fn public(mut self) -> Self {
        self.mode = ShareMode::Public;
        self
    }

    /// Anonymous + authenticated read-only.
    pub fn public_read_only(mut self) -> Self {
        self.mode = ShareMode::PublicReadOnly;
        self
    }

    /// Grant `access` to the given (already-registered) user. Multiple calls
    /// accumulate.
    pub fn user(mut self, name: impl Into<String>, access: Access) -> Self {
        self.users.insert(name.into(), access);
        self
    }
}

// ---------------------------------------------------------------------------
// BuildError
// ---------------------------------------------------------------------------

/// Errors raised by `SmbServerBuilder::build`.
#[derive(Debug, Error)]
pub enum BuildError {
    #[error("listen address must be set")]
    MissingListenAddr,
    #[error("share `{0}` is declared more than once")]
    DuplicateShare(String),
    #[error("share `{0}` mixes .public()/.public_read_only() with explicit .user(...) entries")]
    PublicMixedWithUsers(String),
    #[error("share `{0}` calls `.public*()` more than once")]
    DoublePublic(String),
    #[error("share `{share}` references unknown user `{user}`")]
    UnknownUser { share: String, user: String },
    #[error("user `{0}` is registered twice")]
    DuplicateUser(String),
    #[error("user name `{0}` is reserved (use .public()/.public_read_only() for anonymous)")]
    ReservedUserName(String),
    #[error("user name must be non-empty")]
    EmptyUserName,
}

// ---------------------------------------------------------------------------
// SmbServerBuilder
// ---------------------------------------------------------------------------

/// Builder for `SmbServer`. See `SmbServer::builder`.
pub struct SmbServerBuilder {
    listen_addr: Option<SocketAddr>,
    users: HashMap<String, String>, // name -> password
    user_order: Vec<String>,
    shares: Vec<Share>,
    netbios_name: Option<String>,
    max_read_size: u32,
    max_write_size: u32,
    server_guid: Option<Uuid>,
}

impl Default for SmbServerBuilder {
    fn default() -> Self {
        Self::new()
    }
}

impl SmbServerBuilder {
    pub(crate) fn new() -> Self {
        Self {
            listen_addr: None,
            users: HashMap::new(),
            user_order: Vec::new(),
            shares: Vec::new(),
            netbios_name: None,
            max_read_size: 1024 * 1024,
            max_write_size: 1024 * 1024,
            server_guid: None,
        }
    }

    pub fn listen(mut self, addr: SocketAddr) -> Self {
        self.listen_addr = Some(addr);
        self
    }

    pub fn user(mut self, name: impl Into<String>, password: impl Into<String>) -> Self {
        let n = name.into();
        if !self.users.contains_key(&n) {
            self.user_order.push(n.clone());
        }
        self.users.insert(n, password.into());
        self
    }

    pub fn share(mut self, share: Share) -> Self {
        self.shares.push(share);
        self
    }

    pub fn netbios_name(mut self, name: impl Into<String>) -> Self {
        self.netbios_name = Some(name.into());
        self
    }

    pub fn max_read_size(mut self, bytes: u32) -> Self {
        self.max_read_size = bytes;
        self
    }

    pub fn max_write_size(mut self, bytes: u32) -> Self {
        self.max_write_size = bytes;
        self
    }

    /// Override the random per-process server GUID. Mostly useful in tests.
    pub fn server_guid(mut self, guid: Uuid) -> Self {
        self.server_guid = Some(guid);
        self
    }

    pub fn build(self) -> Result<SmbServer, BuildError> {
        // 1. Validate users.
        for name in &self.user_order {
            if name.is_empty() {
                return Err(BuildError::EmptyUserName);
            }
            if name.eq_ignore_ascii_case("anonymous") {
                return Err(BuildError::ReservedUserName(name.clone()));
            }
        }

        // 2. Validate shares.
        let mut seen_names = std::collections::HashSet::new();
        for share in &self.shares {
            if !seen_names.insert(share.name.to_ascii_lowercase()) {
                return Err(BuildError::DuplicateShare(share.name.clone()));
            }
            // Public-vs-users mutual exclusivity.
            let is_public = matches!(share.mode, ShareMode::Public | ShareMode::PublicReadOnly);
            if is_public && !share.users.is_empty() {
                return Err(BuildError::PublicMixedWithUsers(share.name.clone()));
            }
            // Each per-share user must exist in the global user table.
            for u in share.users.keys() {
                if !self.users.contains_key(u) {
                    return Err(BuildError::UnknownUser {
                        share: share.name.clone(),
                        user: u.clone(),
                    });
                }
            }
        }

        // 3. Listen address required.
        let listen = self.listen_addr.ok_or(BuildError::MissingListenAddr)?;

        // 4. Decide NetBIOS name.
        let netbios = self.netbios_name.unwrap_or_else(|| {
            // Hostname or "SMBSERVER".
            std::env::var("HOSTNAME")
                .ok()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "SMBSERVER".to_string())
        });

        // 5. Build ShareBindings — keep mode + users + backend together.
        let mut share_bindings: Vec<Arc<ShareBindings>> = Vec::with_capacity(self.shares.len());
        for s in self.shares {
            share_bindings.push(ShareBindings::new(
                s.name, s.backend, s.mode, s.users, false,
            ));
        }

        // 6. Materialize the user table (precompute NT hashes to avoid retaining plaintext).
        let mut user_table = HashMap::new();
        for name in &self.user_order {
            let pw = &self.users[name];
            let creds = crate::proto::auth::ntlm::UserCreds::from_password(pw);
            user_table.insert(name.clone(), creds);
        }

        let server_guid = self.server_guid.unwrap_or_else(Uuid::new_v4);

        let cfg = ServerConfig {
            listen_addr: listen,
            netbios_name: netbios,
            max_read_size: self.max_read_size,
            max_write_size: self.max_write_size,
            server_guid,
        };
        let users = ServerUsers {
            table: tokio::sync::RwLock::new(user_table),
        };

        let state = ServerState::new(cfg, users, share_bindings);
        Ok(SmbServer::from_state(state))
    }
}
