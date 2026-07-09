//! CLOSE Request/Response (MS-SMB2 §2.2.15 / §2.2.16).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use super::create::FileId;
use crate::proto::error::ProtoResult;

#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CloseRequest {
    pub structure_size: u16,
    pub flags: u16,
    pub reserved: u32,
    pub file_id: FileId,
}

impl CloseRequest {
    /// Flag: SMB2_CLOSE_FLAG_POSTQUERY_ATTRIB.
    pub const FLAG_POSTQUERY_ATTRIB: u16 = 0x0001;

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
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CloseResponse {
    pub structure_size: u16,
    pub flags: u16,
    pub reserved: u32,
    pub creation_time: u64,
    pub last_access_time: u64,
    pub last_write_time: u64,
    pub change_time: u64,
    pub allocation_size: u64,
    pub end_of_file: u64,
    pub file_attributes: u32,
}

impl CloseResponse {
    pub fn new() -> Self {
        Self {
            structure_size: 60,
            ..Default::default()
        }
    }

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
    fn round_trips() {
        let r = CloseRequest {
            structure_size: 24,
            flags: CloseRequest::FLAG_POSTQUERY_ATTRIB,
            reserved: 0,
            file_id: FileId::new(0x1, 0x2),
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(CloseRequest::parse(&buf).unwrap(), r);

        let r = CloseResponse {
            structure_size: 60,
            ..CloseResponse::new()
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(CloseResponse::parse(&buf).unwrap(), r);
    }
}
