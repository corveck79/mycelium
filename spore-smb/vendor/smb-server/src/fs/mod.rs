//! Local-filesystem [`ShareBackend`] for `smb-server`, sandboxed via `cap-std`.

mod local;

pub use local::LocalFsBackend;
