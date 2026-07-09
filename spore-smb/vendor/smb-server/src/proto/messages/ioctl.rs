//! IOCTL Request/Response (MS-SMB2 §2.2.31 / §2.2.32).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use super::create::FileId;
use crate::proto::error::ProtoResult;

/// File-system control codes we recognize at the wire layer.
///
/// MS-FSCC catalogues the FSCTL codes; we only enumerate the ones referenced
/// in the spec for v1. Unknown codes round-trip via [`Fsctl::Other`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fsctl {
    /// `FSCTL_VALIDATE_NEGOTIATE_INFO` — required handler in v1.
    ValidateNegotiateInfo,
    /// `FSCTL_DFS_GET_REFERRALS`.
    DfsGetReferrals,
    /// `FSCTL_DFS_GET_REFERRALS_EX`.
    DfsGetReferralsEx,
    /// `FSCTL_PIPE_TRANSCEIVE`.
    PipeTranscede,
    /// `FSCTL_PIPE_PEEK`.
    PipePeek,
    /// `FSCTL_PIPE_WAIT`.
    PipeWait,
    /// `FSCTL_LMR_REQUEST_RESILIENCY`.
    LmrRequestResiliency,
    /// `FSCTL_QUERY_NETWORK_INTERFACE_INFO`.
    QueryNetworkInterfaceInfo,
    /// Anything else.
    Other(u32),
}

impl Fsctl {
    pub const VALIDATE_NEGOTIATE_INFO: u32 = 0x0014_0204;
    pub const DFS_GET_REFERRALS: u32 = 0x0006_0194;
    pub const DFS_GET_REFERRALS_EX: u32 = 0x0006_0198;
    pub const PIPE_TRANSCEIVE: u32 = 0x0011_C017;
    pub const PIPE_PEEK: u32 = 0x0011_400C;
    pub const PIPE_WAIT: u32 = 0x0011_C018;
    pub const LMR_REQUEST_RESILIENCY: u32 = 0x001C_0017;
    pub const QUERY_NETWORK_INTERFACE_INFO: u32 = 0x001F_C017;

    pub fn from_u32(code: u32) -> Self {
        match code {
            Self::VALIDATE_NEGOTIATE_INFO => Self::ValidateNegotiateInfo,
            Self::DFS_GET_REFERRALS => Self::DfsGetReferrals,
            Self::DFS_GET_REFERRALS_EX => Self::DfsGetReferralsEx,
            Self::PIPE_TRANSCEIVE => Self::PipeTranscede,
            Self::PIPE_PEEK => Self::PipePeek,
            Self::PIPE_WAIT => Self::PipeWait,
            Self::LMR_REQUEST_RESILIENCY => Self::LmrRequestResiliency,
            Self::QUERY_NETWORK_INTERFACE_INFO => Self::QueryNetworkInterfaceInfo,
            other => Self::Other(other),
        }
    }

    pub fn as_u32(self) -> u32 {
        match self {
            Self::ValidateNegotiateInfo => Self::VALIDATE_NEGOTIATE_INFO,
            Self::DfsGetReferrals => Self::DFS_GET_REFERRALS,
            Self::DfsGetReferralsEx => Self::DFS_GET_REFERRALS_EX,
            Self::PipeTranscede => Self::PIPE_TRANSCEIVE,
            Self::PipePeek => Self::PIPE_PEEK,
            Self::PipeWait => Self::PIPE_WAIT,
            Self::LmrRequestResiliency => Self::LMR_REQUEST_RESILIENCY,
            Self::QueryNetworkInterfaceInfo => Self::QUERY_NETWORK_INTERFACE_INFO,
            Self::Other(c) => c,
        }
    }
}

/// SMB2_IOCTL_REQUEST (MS-SMB2 §2.2.31).
///
/// `input_offset` and `output_offset` are absolute (from the start of the
/// SMB2 header). We model the input buffer immediately following the fixed
/// prefix; the output buffer area is unused on requests but kept for round
/// tripping and extension scenarios.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IoctlRequest {
    pub structure_size: u16,
    pub reserved: u16,
    pub ctl_code: u32,
    pub file_id: FileId,
    pub input_offset: u32,
    pub input_count: u32,
    pub max_input_response: u32,
    pub output_offset: u32,
    pub output_count: u32,
    pub max_output_response: u32,
    pub flags: u32,
    pub reserved2: u32,
    #[br(count = input_count as usize)]
    pub input: Vec<u8>,
}

impl IoctlRequest {
    /// Flag: SMB2_0_IOCTL_IS_FSCTL.
    pub const FLAG_IS_FSCTL: u32 = 0x0000_0001;

    pub fn fsctl(&self) -> Fsctl {
        Fsctl::from_u32(self.ctl_code)
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

/// SMB2_IOCTL_RESPONSE (MS-SMB2 §2.2.32).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IoctlResponse {
    pub structure_size: u16,
    pub reserved: u16,
    pub ctl_code: u32,
    pub file_id: FileId,
    pub input_offset: u32,
    pub input_count: u32,
    pub output_offset: u32,
    pub output_count: u32,
    pub flags: u32,
    pub reserved2: u32,
    /// Output buffer immediately following the fixed prefix.
    #[br(count = output_count as usize)]
    pub output: Vec<u8>,
}

impl IoctlResponse {
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
    fn fsctl_decode_known() {
        assert_eq!(Fsctl::from_u32(0x0014_0204), Fsctl::ValidateNegotiateInfo);
        assert_eq!(Fsctl::from_u32(0xDEAD_BEEF), Fsctl::Other(0xDEAD_BEEF));
        assert_eq!(Fsctl::ValidateNegotiateInfo.as_u32(), 0x0014_0204);
        assert_eq!(Fsctl::Other(0xDEAD_BEEF).as_u32(), 0xDEAD_BEEF);
    }

    #[test]
    fn request_round_trips() {
        let r = IoctlRequest {
            structure_size: 57,
            reserved: 0,
            ctl_code: Fsctl::VALIDATE_NEGOTIATE_INFO,
            file_id: FileId::any(),
            input_offset: 0x78,
            input_count: 4,
            max_input_response: 0,
            output_offset: 0,
            output_count: 0,
            max_output_response: 0x1000,
            flags: IoctlRequest::FLAG_IS_FSCTL,
            reserved2: 0,
            input: vec![0xCA, 0xFE, 0xBA, 0xBE],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        let decoded = IoctlRequest::parse(&buf).unwrap();
        assert_eq!(decoded, r);
        assert_eq!(decoded.fsctl(), Fsctl::ValidateNegotiateInfo);
    }

    #[test]
    fn response_round_trips() {
        let r = IoctlResponse {
            structure_size: 49,
            reserved: 0,
            ctl_code: Fsctl::VALIDATE_NEGOTIATE_INFO,
            file_id: FileId::any(),
            input_offset: 0,
            input_count: 0,
            output_offset: 0x70,
            output_count: 4,
            flags: 0,
            reserved2: 0,
            output: vec![1, 2, 3, 4],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(IoctlResponse::parse(&buf).unwrap(), r);
    }
}
