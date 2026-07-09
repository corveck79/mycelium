//! LOCK Request/Response (MS-SMB2 §2.2.26 / §2.2.27).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use super::create::FileId;
use crate::proto::error::ProtoResult;

/// SMB2_LOCK_ELEMENT (MS-SMB2 §2.2.26.1) — exactly 24 bytes.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockElement {
    pub offset: u64,
    pub length: u64,
    pub flags: u32,
    pub reserved: u32,
}

impl LockElement {
    pub const FLAG_SHARED_LOCK: u32 = 0x0000_0001;
    pub const FLAG_EXCLUSIVE_LOCK: u32 = 0x0000_0002;
    pub const FLAG_UNLOCK: u32 = 0x0000_0004;
    pub const FLAG_FAIL_IMMEDIATELY: u32 = 0x0000_0010;
}

#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockRequest {
    pub structure_size: u16,
    pub lock_count: u16,
    pub lock_sequence: u32,
    pub file_id: FileId,
    #[br(count = lock_count as usize)]
    pub locks: Vec<LockElement>,
}

impl LockRequest {
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

#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockResponse {
    pub structure_size: u16,
    pub reserved: u16,
}

impl Default for LockResponse {
    fn default() -> Self {
        Self {
            structure_size: 4,
            reserved: 0,
        }
    }
}

impl LockResponse {
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
        let r = LockRequest {
            structure_size: 48,
            lock_count: 2,
            lock_sequence: 0,
            file_id: FileId::new(1, 2),
            locks: vec![
                LockElement {
                    offset: 0,
                    length: 16,
                    flags: LockElement::FLAG_EXCLUSIVE_LOCK,
                    reserved: 0,
                },
                LockElement {
                    offset: 0,
                    length: 16,
                    flags: LockElement::FLAG_UNLOCK,
                    reserved: 0,
                },
            ],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(LockRequest::parse(&buf).unwrap(), r);
    }

    #[test]
    fn response_round_trips() {
        let r = LockResponse::default();
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(LockResponse::parse(&buf).unwrap(), r);
    }
}
