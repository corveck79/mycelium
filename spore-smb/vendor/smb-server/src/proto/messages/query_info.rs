//! QUERY_INFO Request/Response (MS-SMB2 §2.2.37 / §2.2.38).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use super::create::FileId;
use crate::proto::error::ProtoResult;

/// `InfoType` values (MS-SMB2 §2.2.37 InfoType field).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum InfoType {
    File = 0x01,
    FileSystem = 0x02,
    Security = 0x03,
    Quota = 0x04,
}

impl InfoType {
    pub fn from_u8(v: u8) -> Option<Self> {
        Some(match v {
            0x01 => Self::File,
            0x02 => Self::FileSystem,
            0x03 => Self::Security,
            0x04 => Self::Quota,
            _ => return None,
        })
    }
}

/// SMB2_QUERY_INFO_REQUEST (MS-SMB2 §2.2.37).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QueryInfoRequest {
    pub structure_size: u16,
    pub info_type: u8,
    pub file_information_class: u8,
    pub output_buffer_length: u32,
    pub input_buffer_offset: u16,
    pub reserved: u16,
    pub input_buffer_length: u32,
    /// `AdditionalInformation`: which fields of the security descriptor to
    /// return when `info_type == Security`. Otherwise an additional info-class
    /// selector for FS info.
    pub additional_information: u32,
    pub flags: u32,
    pub file_id: FileId,
    /// Optional input buffer (used by FILE/FS info classes that need it, e.g.
    /// `FileFullEaInformation` extended-attribute name lists).
    #[br(count = input_buffer_length as usize)]
    pub input_buffer: Vec<u8>,
}

impl QueryInfoRequest {
    /// Flag: SL_RESTART_SCAN.
    pub const FLAG_RESTART_SCAN: u32 = 0x0000_0001;
    /// Flag: SL_RETURN_SINGLE_ENTRY.
    pub const FLAG_RETURN_SINGLE_ENTRY: u32 = 0x0000_0002;
    /// Flag: SL_INDEX_SPECIFIED.
    pub const FLAG_INDEX_SPECIFIED: u32 = 0x0000_0004;

    pub fn info_type_enum(&self) -> Option<InfoType> {
        InfoType::from_u8(self.info_type)
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

/// SMB2_QUERY_INFO_RESPONSE (MS-SMB2 §2.2.38).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QueryInfoResponse {
    pub structure_size: u16,
    pub output_buffer_offset: u16,
    pub output_buffer_length: u32,
    #[br(count = output_buffer_length as usize)]
    pub buffer: Vec<u8>,
}

impl QueryInfoResponse {
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
        let r = QueryInfoRequest {
            structure_size: 41,
            info_type: InfoType::File as u8,
            file_information_class: 0x05, // FileStandardInformation
            output_buffer_length: 0x1000,
            input_buffer_offset: 0,
            reserved: 0,
            input_buffer_length: 0,
            additional_information: 0,
            flags: 0,
            file_id: FileId::new(1, 2),
            input_buffer: vec![],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        let decoded = QueryInfoRequest::parse(&buf).unwrap();
        assert_eq!(decoded, r);
        assert_eq!(decoded.info_type_enum(), Some(InfoType::File));
    }

    #[test]
    fn response_round_trips() {
        let r = QueryInfoResponse {
            structure_size: 9,
            output_buffer_offset: 0x48,
            output_buffer_length: 4,
            buffer: vec![0xAB, 0xCD, 0xEF, 0x01],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(QueryInfoResponse::parse(&buf).unwrap(), r);
    }
}
