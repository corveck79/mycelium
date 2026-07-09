//! SMB2 ERROR Response (MS-SMB2 §2.2.2).
//!
//! Sent in place of any normal response when the server returns a non-zero
//! NTSTATUS. The SMB2 header carries the NTSTATUS in `channel_sequence_status`;
//! this body provides extended error context if any.

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use crate::proto::error::ProtoResult;

/// MS-SMB2 §2.2.2 ERROR Response.
///
/// `structure_size` is always 9; `byte_count` is the length of `error_data`
/// when there is no structured error context (the common case). When
/// `error_context_count > 0`, `error_data` holds a sequence of
/// [`ErrorContext`] entries (SMB 3.1.1+).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorResponse {
    pub structure_size: u16,
    pub error_context_count: u8,
    pub reserved: u8,
    pub byte_count: u32,
    #[br(count = if byte_count == 0 { 1 } else { byte_count as usize })]
    pub error_data: Vec<u8>,
}

impl ErrorResponse {
    /// Build a minimal ERROR response body for the given NTSTATUS.
    ///
    /// Per MS-SMB2 §2.2.2 a zero-`byte_count` ERROR response still emits a
    /// single byte of `error_data` (the field is mandatory, length 1 when
    /// there is no payload).
    pub fn status(_ntstatus: u32) -> Self {
        Self {
            structure_size: 9,
            error_context_count: 0,
            reserved: 0,
            byte_count: 0,
            error_data: vec![0],
        }
    }

    pub fn parse(buf: &[u8]) -> ProtoResult<Self> {
        let mut c = Cursor::new(buf);
        Ok(Self::read(&mut c)?)
    }

    pub fn write_to(&self, out: &mut Vec<u8>) -> ProtoResult<()> {
        let mut c = Cursor::new(Vec::new());
        BinWrite::write(self, &mut c)?;
        out.extend_from_slice(&c.into_inner());
        Ok(())
    }
}

/// MS-SMB2 §2.2.2.1 ERROR Context Response (3.1.1+).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorContext {
    pub error_data_length: u32,
    pub error_id: u32,
    #[br(count = error_data_length as usize)]
    pub error_context_data: Vec<u8>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_status_helper() {
        let r = ErrorResponse::status(0xC000_0022 /* STATUS_ACCESS_DENIED */);
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        let decoded = ErrorResponse::parse(&buf).unwrap();
        assert_eq!(decoded, r);
        // structure_size, contexts, reserved, bytecount, 1 byte payload = 9 bytes
        assert_eq!(buf.len(), 9);
    }
}
