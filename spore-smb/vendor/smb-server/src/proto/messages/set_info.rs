//! SET_INFO Request/Response (MS-SMB2 §2.2.39 / §2.2.40).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use super::create::FileId;
use crate::proto::error::ProtoResult;

/// SMB2_SET_INFO_REQUEST (MS-SMB2 §2.2.39).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SetInfoRequest {
    pub structure_size: u16,
    pub info_type: u8,
    pub file_information_class: u8,
    pub buffer_length: u32,
    pub buffer_offset: u16,
    pub reserved: u16,
    pub additional_information: u32,
    pub file_id: FileId,
    #[br(count = buffer_length as usize)]
    pub buffer: Vec<u8>,
}

impl SetInfoRequest {
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

/// SMB2_SET_INFO_RESPONSE (MS-SMB2 §2.2.40).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SetInfoResponse {
    pub structure_size: u16,
}

impl Default for SetInfoResponse {
    fn default() -> Self {
        Self { structure_size: 2 }
    }
}

impl SetInfoResponse {
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
        let r = SetInfoRequest {
            structure_size: 33,
            info_type: 0x01,              // File
            file_information_class: 0x14, // FileEndOfFileInformation
            buffer_length: 8,
            buffer_offset: 0x60,
            reserved: 0,
            additional_information: 0,
            file_id: FileId::new(1, 2),
            buffer: vec![0, 0, 0, 0x10, 0, 0, 0, 0],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(SetInfoRequest::parse(&buf).unwrap(), r);
    }

    #[test]
    fn response_round_trips() {
        let r = SetInfoResponse::default();
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(SetInfoResponse::parse(&buf).unwrap(), r);
        assert_eq!(buf.len(), 2);
    }
}
