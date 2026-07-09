//! SESSION_SETUP Request/Response (MS-SMB2 §2.2.5 / §2.2.6).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use crate::proto::error::ProtoResult;

/// SMB2_SESSION_SETUP_REQUEST (MS-SMB2 §2.2.5).
///
/// `security_buffer` is opaque GSS-API/SPNEGO data — the auth agent decodes it.
/// The wire offset is from the start of the SMB2 header; we encode/decode it
/// as length-counted data immediately following the fixed prefix, which is
/// the canonical layout. Server crate may patch the offset if it needs an
/// unusual layout.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionSetupRequest {
    pub structure_size: u16,
    pub flags: u8,
    pub security_mode: u8,
    pub capabilities: u32,
    pub channel: u32,
    pub security_buffer_offset: u16,
    pub security_buffer_length: u16,
    pub previous_session_id: u64,
    #[br(count = security_buffer_length as usize)]
    pub security_buffer: Vec<u8>,
}

impl SessionSetupRequest {
    /// Flag: SMB2_SESSION_FLAG_BINDING — bind to existing session (3.x).
    pub const FLAG_BINDING: u8 = 0x01;

    pub fn parse(buf: &[u8]) -> ProtoResult<Self> {
        Ok(Self::read(&mut Cursor::new(buf))?)
    }
    pub fn write_to(&self, out: &mut Vec<u8>) -> ProtoResult<()> {
        let mut c = Cursor::new(Vec::new());
        BinWrite::write(self, &mut c)?;
        out.extend_from_slice(&c.into_inner());
        Ok(())
    }
}

/// SMB2_SESSION_SETUP_RESPONSE (MS-SMB2 §2.2.6).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionSetupResponse {
    pub structure_size: u16,
    pub session_flags: u16,
    pub security_buffer_offset: u16,
    pub security_buffer_length: u16,
    #[br(count = security_buffer_length as usize)]
    pub security_buffer: Vec<u8>,
}

impl SessionSetupResponse {
    /// Session flag: IS_GUEST.
    pub const FLAG_IS_GUEST: u16 = 0x0001;
    /// Session flag: IS_NULL (anonymous).
    pub const FLAG_IS_NULL: u16 = 0x0002;
    /// Session flag: ENCRYPT_DATA.
    pub const FLAG_ENCRYPT_DATA: u16 = 0x0004;

    pub fn parse(buf: &[u8]) -> ProtoResult<Self> {
        Ok(Self::read(&mut Cursor::new(buf))?)
    }
    pub fn write_to(&self, out: &mut Vec<u8>) -> ProtoResult<()> {
        let mut c = Cursor::new(Vec::new());
        BinWrite::write(self, &mut c)?;
        out.extend_from_slice(&c.into_inner());
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_round_trips() {
        let r = SessionSetupRequest {
            structure_size: 25,
            flags: 0,
            security_mode: 0x01,
            capabilities: 0x01,
            channel: 0,
            security_buffer_offset: 0x58,
            security_buffer_length: 6,
            previous_session_id: 0,
            security_buffer: vec![0xDE, 0xAD, 0xBE, 0xEF, 0x01, 0x02],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(SessionSetupRequest::parse(&buf).unwrap(), r);
    }

    #[test]
    fn response_round_trips() {
        let r = SessionSetupResponse {
            structure_size: 9,
            session_flags: SessionSetupResponse::FLAG_IS_GUEST,
            security_buffer_offset: 0x48,
            security_buffer_length: 4,
            security_buffer: vec![1, 2, 3, 4],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(SessionSetupResponse::parse(&buf).unwrap(), r);
    }
}
