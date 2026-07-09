//! Crate-wide error type for the internal SMB protocol layer.

use thiserror::Error;

pub type ProtoResult<T> = Result<T, ProtoError>;

#[derive(Debug, Error)]
pub enum ProtoError {
    #[error("malformed wire frame: {0}")]
    Malformed(&'static str),

    #[error("unsupported dialect: 0x{0:04x}")]
    UnsupportedDialect(u16),

    #[error("auth failure: {0}")]
    Auth(&'static str),

    #[error("crypto failure: {0}")]
    Crypto(&'static str),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("binrw error: {0}")]
    Binrw(#[from] binrw::Error),
}
