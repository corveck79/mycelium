//! Direct-TCP / NetBIOS-over-TCP framing for SMB2/3.
//!
//! MS-SMB2 §2.1 requires a 4-byte big-endian length prefix on every TCP frame:
//!
//! ```text
//!   +-------+--------------------------------+
//!   | 0x00  | 24-bit big-endian payload len  |
//!   +-------+--------------------------------+
//!   |              SMB2 packet ...           |
//!   +----------------------------------------+
//! ```
//!
//! The top byte is reserved (must be zero in Direct-TCP transport — it is the
//! NetBIOS session-message-type byte from RFC 1002 §4.3.1). The remaining 24
//! bits encode the payload length, so the absolute maximum on the wire is
//! `2^24 - 1 = 16_777_215` bytes (16 MiB - 1). We enforce that as the cap.
//!
//! This module is async-runtime-agnostic. Only sync helpers operating on byte
//! slices and `Vec<u8>` live here; the server crate wraps these with tokio
//! I/O.

use crate::proto::error::{ProtoError, ProtoResult};

/// Length of the Direct-TCP frame header (4 bytes).
pub const FRAME_HEADER_LEN: usize = 4;

/// Maximum payload size representable by the 3-byte length field.
///
/// MS-SMB2 §2.1 — `2^24 - 1 = 16_777_215` bytes.
pub const MAX_FRAME_PAYLOAD: u32 = 0x00FF_FFFF;

/// Encode a single Direct-TCP frame: 4-byte header + payload.
///
/// Panics in debug if the payload exceeds [`MAX_FRAME_PAYLOAD`]; release builds
/// silently truncate the high byte.
pub fn encode_frame(payload: &[u8], out: &mut Vec<u8>) {
    debug_assert!(
        payload.len() as u64 <= MAX_FRAME_PAYLOAD as u64,
        "frame payload exceeds 16 MiB - 1"
    );
    let len = payload.len() as u32;
    // Top byte is the NetBIOS session-message type (0x00 for Direct-TCP).
    // Lower 3 bytes are payload length, big-endian.
    out.reserve(FRAME_HEADER_LEN + payload.len());
    out.push(0x00);
    out.push(((len >> 16) & 0xFF) as u8);
    out.push(((len >> 8) & 0xFF) as u8);
    out.push((len & 0xFF) as u8);
    out.extend_from_slice(payload);
}

/// Decode the 4-byte frame header, returning the payload length.
///
/// Returns [`ProtoError::Malformed`] if the top byte is non-zero (NetBIOS
/// session-message type other than `SESSION MESSAGE` is not supported in
/// Direct-TCP transport).
pub fn decode_frame_header(bytes: &[u8; FRAME_HEADER_LEN]) -> ProtoResult<u32> {
    if bytes[0] != 0x00 {
        return Err(ProtoError::Malformed(
            "NetBIOS session-message type byte must be 0x00 for Direct-TCP",
        ));
    }
    let len = (u32::from(bytes[1]) << 16) | (u32::from(bytes[2]) << 8) | u32::from(bytes[3]);
    Ok(len)
}

/// Convenience: read one full frame from a contiguous byte slice.
///
/// Returns the payload slice and the remaining bytes after the frame.
#[cfg(test)]
pub fn decode_frame(buf: &[u8]) -> ProtoResult<(&[u8], &[u8])> {
    if buf.len() < FRAME_HEADER_LEN {
        return Err(ProtoError::Malformed("short frame header"));
    }
    let mut hdr = [0u8; FRAME_HEADER_LEN];
    hdr.copy_from_slice(&buf[..FRAME_HEADER_LEN]);
    let len = decode_frame_header(&hdr)? as usize;
    let total = FRAME_HEADER_LEN + len;
    if buf.len() < total {
        return Err(ProtoError::Malformed("truncated frame body"));
    }
    Ok((&buf[FRAME_HEADER_LEN..total], &buf[total..]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_empty_frame() {
        let mut out = Vec::new();
        encode_frame(&[], &mut out);
        assert_eq!(out, [0x00, 0x00, 0x00, 0x00]);
    }

    #[test]
    fn encodes_simple_frame() {
        let mut out = Vec::new();
        encode_frame(&[0xAA, 0xBB, 0xCC], &mut out);
        assert_eq!(out, [0x00, 0x00, 0x00, 0x03, 0xAA, 0xBB, 0xCC]);
    }

    #[test]
    fn round_trips_random_payload() {
        let payload: Vec<u8> = (0u8..=200).collect();
        let mut wire = Vec::new();
        encode_frame(&payload, &mut wire);

        let (decoded, rest) = decode_frame(&wire).unwrap();
        assert_eq!(decoded, payload.as_slice());
        assert!(rest.is_empty());
    }

    #[test]
    fn decodes_header_three_byte_length() {
        // 0x00_12_34_56 -> length 0x123456
        let len = decode_frame_header(&[0x00, 0x12, 0x34, 0x56]).unwrap();
        assert_eq!(len, 0x0012_3456);
    }

    #[test]
    fn decodes_header_max_length() {
        let len = decode_frame_header(&[0x00, 0xFF, 0xFF, 0xFF]).unwrap();
        assert_eq!(len, MAX_FRAME_PAYLOAD);
    }

    #[test]
    fn rejects_nonzero_top_byte() {
        let err = decode_frame_header(&[0x81, 0x00, 0x00, 0x00]).unwrap_err();
        assert!(matches!(err, ProtoError::Malformed(_)));
    }

    #[test]
    fn decode_frame_handles_trailing_data() {
        let mut wire = Vec::new();
        encode_frame(&[1, 2, 3], &mut wire);
        wire.extend_from_slice(&[9, 9, 9]); // simulate a partial second frame

        let (payload, rest) = decode_frame(&wire).unwrap();
        assert_eq!(payload, &[1, 2, 3]);
        assert_eq!(rest, &[9, 9, 9]);
    }

    #[test]
    fn decode_frame_short_header() {
        let err = decode_frame(&[0x00, 0x00]).unwrap_err();
        assert!(matches!(err, ProtoError::Malformed(_)));
    }

    #[test]
    fn decode_frame_truncated_body() {
        let err = decode_frame(&[0x00, 0x00, 0x00, 0x05, 0xAA]).unwrap_err();
        assert!(matches!(err, ProtoError::Malformed(_)));
    }
}
