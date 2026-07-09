//! Public error type for the server, plus the NTSTATUS mapping per spec §8.

use thiserror::Error;

use crate::ntstatus;

pub type SmbResult<T> = Result<T, SmbError>;

/// Errors returned by `ShareBackend` and surfaced through the SMB protocol.
///
/// `to_nt_status` maps each variant onto a single NTSTATUS code per the spec
/// §8 table. Internal protocol-layer failures (malformed frames, signing
/// errors) never become `SmbError`; the connection loop logs them and aborts.
#[derive(Debug, Error)]
pub enum SmbError {
    #[error("not found")]
    NotFound,
    #[error("path not found")]
    PathNotFound,
    #[error("access denied")]
    AccessDenied,
    #[error("exists")]
    Exists,
    #[error("not empty")]
    NotEmpty,
    #[error("is a directory")]
    IsDirectory,
    #[error("not a directory")]
    NotADirectory,
    #[error("name too long / invalid")]
    NameInvalid,
    #[error("sharing violation")]
    Sharing,
    #[error("not supported")]
    NotSupported,
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
}

impl SmbError {
    /// Map this error onto an NTSTATUS code per the v1 spec §8 table.
    pub fn to_nt_status(&self) -> u32 {
        match self {
            SmbError::NotFound => ntstatus::STATUS_OBJECT_NAME_NOT_FOUND,
            SmbError::PathNotFound => ntstatus::STATUS_OBJECT_PATH_NOT_FOUND,
            SmbError::AccessDenied => ntstatus::STATUS_ACCESS_DENIED,
            SmbError::Exists => ntstatus::STATUS_OBJECT_NAME_COLLISION,
            SmbError::NotEmpty => ntstatus::STATUS_DIRECTORY_NOT_EMPTY,
            SmbError::IsDirectory => ntstatus::STATUS_FILE_IS_A_DIRECTORY,
            SmbError::NotADirectory => ntstatus::STATUS_NOT_A_DIRECTORY,
            SmbError::NameInvalid => ntstatus::STATUS_OBJECT_NAME_INVALID,
            SmbError::Sharing => ntstatus::STATUS_SHARING_VIOLATION,
            SmbError::NotSupported => ntstatus::STATUS_NOT_SUPPORTED,
            SmbError::Io(_) => ntstatus::STATUS_UNEXPECTED_IO_ERROR,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nt_status_table_matches_spec() {
        assert_eq!(SmbError::NotFound.to_nt_status(), 0xC000_000F);
        assert_eq!(SmbError::PathNotFound.to_nt_status(), 0xC000_003A);
        assert_eq!(SmbError::AccessDenied.to_nt_status(), 0xC000_0022);
        assert_eq!(SmbError::Exists.to_nt_status(), 0xC000_0035);
        assert_eq!(SmbError::NotEmpty.to_nt_status(), 0xC000_0101);
        assert_eq!(SmbError::IsDirectory.to_nt_status(), 0xC000_00BA);
        assert_eq!(SmbError::NotADirectory.to_nt_status(), 0xC000_0103);
        assert_eq!(SmbError::NameInvalid.to_nt_status(), 0xC000_0033);
        assert_eq!(SmbError::Sharing.to_nt_status(), 0xC000_0043);
        assert_eq!(SmbError::NotSupported.to_nt_status(), 0xC000_00BB);

        let io_err = SmbError::Io(std::io::Error::other("boom"));
        assert_eq!(io_err.to_nt_status(), 0xC000_009C);
    }

    #[test]
    fn io_err_from_blanket_works() {
        let io: std::io::Error = std::io::Error::other("x");
        let smb: SmbError = io.into();
        assert_eq!(smb.to_nt_status(), 0xC000_009C);
    }
}
