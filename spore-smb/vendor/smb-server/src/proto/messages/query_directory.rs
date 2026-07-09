//! QUERY_DIRECTORY Request/Response (MS-SMB2 §2.2.33 / §2.2.34).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use super::create::FileId;
use crate::proto::error::ProtoResult;

/// File-info-class identifiers used in QUERY_DIRECTORY (MS-SMB2 §2.2.33
/// FileInformationClass).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum FileInfoClass {
    FileDirectoryInformation = 0x01,
    FileFullDirectoryInformation = 0x02,
    FileBothDirectoryInformation = 0x03,
    FileNamesInformation = 0x0C,
    FileIdBothDirectoryInformation = 0x25,
    FileIdFullDirectoryInformation = 0x26,
}

impl FileInfoClass {
    pub fn from_u8(v: u8) -> Option<Self> {
        Some(match v {
            0x01 => Self::FileDirectoryInformation,
            0x02 => Self::FileFullDirectoryInformation,
            0x03 => Self::FileBothDirectoryInformation,
            0x0C => Self::FileNamesInformation,
            0x25 => Self::FileIdBothDirectoryInformation,
            0x26 => Self::FileIdFullDirectoryInformation,
            _ => return None,
        })
    }
}

/// SMB2_QUERY_DIRECTORY_REQUEST (MS-SMB2 §2.2.33).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QueryDirectoryRequest {
    pub structure_size: u16,
    pub file_information_class: u8,
    pub flags: u8,
    pub file_index: u32,
    pub file_id: FileId,
    pub file_name_offset: u16,
    pub file_name_length: u16,
    pub output_buffer_length: u32,
    /// UTF-16LE search pattern (e.g. "*").
    #[br(count = file_name_length as usize)]
    pub file_name: Vec<u8>,
}

impl QueryDirectoryRequest {
    pub const FLAG_RESTART_SCANS: u8 = 0x01;
    pub const FLAG_RETURN_SINGLE_ENTRY: u8 = 0x02;
    pub const FLAG_INDEX_SPECIFIED: u8 = 0x04;
    pub const FLAG_REOPEN: u8 = 0x10;

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

/// SMB2_QUERY_DIRECTORY_RESPONSE (MS-SMB2 §2.2.34).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QueryDirectoryResponse {
    pub structure_size: u16,
    /// `OutputBufferOffset` is from the start of the SMB2 header.
    pub output_buffer_offset: u16,
    pub output_buffer_length: u32,
    /// Variable-length info-class-specific buffer.
    #[br(count = output_buffer_length as usize)]
    pub buffer: Vec<u8>,
}

impl QueryDirectoryResponse {
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

    fn utf16le(s: &str) -> Vec<u8> {
        s.encode_utf16().flat_map(u16::to_le_bytes).collect()
    }

    #[test]
    fn request_round_trips() {
        let pat = utf16le("*");
        let r = QueryDirectoryRequest {
            structure_size: 33,
            file_information_class: FileInfoClass::FileIdBothDirectoryInformation as u8,
            flags: QueryDirectoryRequest::FLAG_RESTART_SCANS,
            file_index: 0,
            file_id: FileId::new(1, 2),
            file_name_offset: 0x60,
            file_name_length: pat.len() as u16,
            output_buffer_length: 0x10000,
            file_name: pat,
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(QueryDirectoryRequest::parse(&buf).unwrap(), r);
    }

    #[test]
    fn response_round_trips() {
        let r = QueryDirectoryResponse {
            structure_size: 9,
            output_buffer_offset: 0x48,
            output_buffer_length: 8,
            buffer: vec![1, 2, 3, 4, 5, 6, 7, 8],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(QueryDirectoryResponse::parse(&buf).unwrap(), r);
    }
}
