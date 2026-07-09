//! ECHO Request/Response (MS-SMB2 §2.2.28).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use crate::proto::error::ProtoResult;

#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EchoRequest {
    pub structure_size: u16,
    pub reserved: u16,
}

impl Default for EchoRequest {
    fn default() -> Self {
        Self {
            structure_size: 4,
            reserved: 0,
        }
    }
}

#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EchoResponse {
    pub structure_size: u16,
    pub reserved: u16,
}

impl Default for EchoResponse {
    fn default() -> Self {
        Self {
            structure_size: 4,
            reserved: 0,
        }
    }
}

impl EchoRequest {
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

impl EchoResponse {
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
        let req = EchoRequest::default();
        let mut buf = Vec::new();
        req.write_to(&mut buf).unwrap();
        assert_eq!(buf.len(), 4);
        assert_eq!(EchoRequest::parse(&buf).unwrap(), req);

        let resp = EchoResponse::default();
        let mut buf = Vec::new();
        resp.write_to(&mut buf).unwrap();
        assert_eq!(EchoResponse::parse(&buf).unwrap(), resp);
    }
}
