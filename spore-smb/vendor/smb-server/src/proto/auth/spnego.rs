//! Minimal hand-rolled DER codec for SPNEGO (MS-SPNG / RFC 4178).
//!
//! v1 advertises **only** the NTLMSSP mechanism. We don't pull in a full
//! ASN.1 crate; this is a tiny subset of DER for the few SPNEGO tokens we
//! need to encode/decode during SESSION_SETUP.
//!
//! ASN.1 sketch:
//!
//! ```text
//! GSSAPI-Token (RFC 2743) ::= [APPLICATION 0] IMPLICIT SEQUENCE {
//!     thisMech        OBJECT IDENTIFIER,    -- SPNEGO 1.3.6.1.5.5.2
//!     innerContextToken ANY DEFINED BY thisMech
//! }
//!
//! NegotiationToken ::= CHOICE {
//!     negTokenInit    [0] NegTokenInit,
//!     negTokenResp    [1] NegTokenResp
//! }
//!
//! NegTokenInit ::= SEQUENCE {
//!     mechTypes       [0] MechTypeList,
//!     reqFlags        [1] ContextFlags  OPTIONAL,
//!     mechToken       [2] OCTET STRING  OPTIONAL,
//!     mechListMIC     [3] OCTET STRING  OPTIONAL
//! }
//!
//! NegTokenResp ::= SEQUENCE {
//!     negState        [0] ENUMERATED OPTIONAL,
//!     supportedMech   [1] OBJECT IDENTIFIER OPTIONAL,
//!     responseToken   [2] OCTET STRING OPTIONAL,
//!     mechListMIC     [3] OCTET STRING OPTIONAL
//! }
//! ```

use crate::proto::error::{ProtoError, ProtoResult};

// --- Universal & well-known tags --------------------------------------------

const TAG_SEQUENCE: u8 = 0x30; // SEQUENCE OF / SEQUENCE (constructed)
const TAG_OBJECT: u8 = 0x06; // OBJECT IDENTIFIER
const TAG_OCTET: u8 = 0x04; // OCTET STRING
const TAG_ENUMERATED: u8 = 0x0a; // ENUMERATED

const TAG_APP_0: u8 = 0x60; // [APPLICATION 0] IMPLICIT — GSS-API outer
const TAG_CTX_0: u8 = 0xa0;
const TAG_CTX_1: u8 = 0xa1;
const TAG_CTX_2: u8 = 0xa2;
const TAG_CTX_3: u8 = 0xa3;

// --- OIDs ------------------------------------------------------------------

/// SPNEGO `1.3.6.1.5.5.2` encoded as the *content* of an OBJECT IDENTIFIER
/// (i.e. **without** the leading 0x06 tag + length).
pub const OID_SPNEGO: &[u8] = &[0x2b, 0x06, 0x01, 0x05, 0x05, 0x02];

/// NTLMSSP `1.3.6.1.4.1.311.2.2.10` encoded as OID *content*.
pub const OID_NTLMSSP: &[u8] = &[0x2b, 0x06, 0x01, 0x04, 0x01, 0x82, 0x37, 0x02, 0x02, 0x0a];

// --- NegState --------------------------------------------------------------

/// Values of the `negState` field in NegTokenResp.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum NegState {
    AcceptCompleted = 0,
    AcceptIncomplete = 1,
    Reject = 2,
    RequestMic = 3,
}

impl NegState {
    fn from_byte(b: u8) -> ProtoResult<Self> {
        match b {
            0 => Ok(NegState::AcceptCompleted),
            1 => Ok(NegState::AcceptIncomplete),
            2 => Ok(NegState::Reject),
            3 => Ok(NegState::RequestMic),
            _ => Err(ProtoError::Auth("invalid NegState")),
        }
    }
}

// --- DER length helpers ----------------------------------------------------

/// Encode a DER length (definite-length form, MS-SPNG always uses definite).
fn der_len(n: usize, out: &mut Vec<u8>) {
    if n < 0x80 {
        out.push(n as u8);
        return;
    }
    // Long form. Find minimum number of bytes.
    let mut tmp = [0u8; 8];
    let mut nb = 0;
    let mut v = n;
    while v > 0 {
        tmp[nb] = (v & 0xff) as u8;
        v >>= 8;
        nb += 1;
    }
    out.push(0x80 | nb as u8);
    for i in (0..nb).rev() {
        out.push(tmp[i]);
    }
}

/// Read a DER length from `buf` starting at `pos`. Returns `(length, next_pos)`.
fn read_len(buf: &[u8], pos: usize) -> ProtoResult<(usize, usize)> {
    if pos >= buf.len() {
        return Err(ProtoError::Auth("DER length truncated"));
    }
    let first = buf[pos];
    if first < 0x80 {
        return Ok((first as usize, pos + 1));
    }
    let nb = (first & 0x7f) as usize;
    if nb == 0 || nb > 4 {
        // Indefinite (nb=0) — never used by SPNEGO.
        // We cap at 4 bytes (max ~4 GiB), more than enough for tokens.
        return Err(ProtoError::Auth("DER length form unsupported"));
    }
    if pos + 1 + nb > buf.len() {
        return Err(ProtoError::Auth("DER length truncated"));
    }
    let mut v = 0usize;
    for i in 0..nb {
        v = (v << 8) | buf[pos + 1 + i] as usize;
    }
    Ok((v, pos + 1 + nb))
}

/// Read `(tag, content_slice, next_pos)` at `pos`. Verifies the expected tag.
fn read_tlv(buf: &[u8], pos: usize, expected_tag: u8) -> ProtoResult<(&[u8], usize)> {
    if pos >= buf.len() {
        return Err(ProtoError::Auth("DER tag truncated"));
    }
    if buf[pos] != expected_tag {
        return Err(ProtoError::Auth("unexpected DER tag"));
    }
    let (len, after_len) = read_len(buf, pos + 1)?;
    let end = after_len + len;
    if end > buf.len() {
        return Err(ProtoError::Auth("DER content truncated"));
    }
    Ok((&buf[after_len..end], end))
}

/// Read any TLV (returning its tag plus the content slice & end position).
fn read_any_tlv(buf: &[u8], pos: usize) -> ProtoResult<(u8, &[u8], usize)> {
    if pos >= buf.len() {
        return Err(ProtoError::Auth("DER tag truncated"));
    }
    let tag = buf[pos];
    let (len, after_len) = read_len(buf, pos + 1)?;
    let end = after_len + len;
    if end > buf.len() {
        return Err(ProtoError::Auth("DER content truncated"));
    }
    Ok((tag, &buf[after_len..end], end))
}

// --- TLV writer helper -----------------------------------------------------

fn write_tlv(tag: u8, content: &[u8], out: &mut Vec<u8>) {
    out.push(tag);
    der_len(content.len(), out);
    out.extend_from_slice(content);
}

// --- Public API ------------------------------------------------------------

/// Decoded `NegTokenInit` payload — only the bits we care about.
#[derive(Debug, Clone)]
pub struct NegTokenInit {
    /// List of mechanism OIDs (each entry is the OID content bytes, no 0x06 tag).
    pub mech_types: Vec<Vec<u8>>,
    /// `mechToken [2]` if present — typically the NTLMSSP NEGOTIATE_MESSAGE bytes.
    pub mech_token: Option<Vec<u8>>,
}

/// Decoded `NegTokenResp` payload.
#[derive(Debug, Clone, Default)]
pub struct NegTokenResp {
    pub neg_state: Option<NegState>,
    /// `supportedMech [1]` (OID content bytes).
    pub supported_mech: Option<Vec<u8>>,
    /// `responseToken [2]` — typically inner NTLMSSP CHALLENGE/AUTHENTICATE bytes.
    pub response_token: Option<Vec<u8>>,
    pub mech_list_mic: Option<Vec<u8>>,
}

/// Decode the **initial** SPNEGO blob from the client. This is wrapped in
/// the GSS-API outer `[APPLICATION 0]` tag, contains a `thisMech` OID
/// (SPNEGO), and a `[0] NegTokenInit`.
///
/// Returns the parsed `NegTokenInit`.
pub fn decode_init_token(buf: &[u8]) -> ProtoResult<NegTokenInit> {
    // [APPLICATION 0] IMPLICIT SEQUENCE { thisMech OID, NegotiationToken }
    let (gss_inner, _end) = read_tlv(buf, 0, TAG_APP_0)?;

    // thisMech
    let (mech, after_mech) = read_tlv(gss_inner, 0, TAG_OBJECT)?;
    if mech != OID_SPNEGO {
        return Err(ProtoError::Auth("not an SPNEGO token"));
    }

    // NegotiationToken — choice tagged [0] for init.
    let (init_inner, _) = read_tlv(gss_inner, after_mech, TAG_CTX_0)?;
    parse_neg_token_init_body(init_inner)
}

fn parse_neg_token_init_body(inner: &[u8]) -> ProtoResult<NegTokenInit> {
    // Inner is a SEQUENCE.
    let (seq_body, _) = read_tlv(inner, 0, TAG_SEQUENCE)?;
    let mut pos = 0usize;
    let mut mech_types: Vec<Vec<u8>> = Vec::new();
    let mut mech_token: Option<Vec<u8>> = None;

    while pos < seq_body.len() {
        let (tag, content, next) = read_any_tlv(seq_body, pos)?;
        match tag {
            TAG_CTX_0 => {
                // mechTypes [0] MechTypeList ::= SEQUENCE OF MechType (OID)
                let (mt_seq, _) = read_tlv(content, 0, TAG_SEQUENCE)?;
                let mut p = 0usize;
                while p < mt_seq.len() {
                    let (oid, e) = read_tlv(mt_seq, p, TAG_OBJECT)?;
                    mech_types.push(oid.to_vec());
                    p = e;
                }
            }
            TAG_CTX_1 => {
                // reqFlags — ignored.
            }
            TAG_CTX_2 => {
                // mechToken [2] OCTET STRING
                let (oct, _) = read_tlv(content, 0, TAG_OCTET)?;
                mech_token = Some(oct.to_vec());
            }
            TAG_CTX_3 => {
                // mechListMIC — ignored on init.
            }
            _ => {
                // Unknown — skip silently (forward-compat).
            }
        }
        pos = next;
    }

    Ok(NegTokenInit {
        mech_types,
        mech_token,
    })
}

/// Decode a subsequent `NegTokenResp`. These are sent without the GSS-API
/// outer wrapper — they begin directly with the `[1]` choice tag.
pub fn decode_resp_token(buf: &[u8]) -> ProtoResult<NegTokenResp> {
    let (resp_inner, _) = read_tlv(buf, 0, TAG_CTX_1)?;
    let (seq_body, _) = read_tlv(resp_inner, 0, TAG_SEQUENCE)?;
    let mut pos = 0usize;
    let mut out = NegTokenResp::default();

    while pos < seq_body.len() {
        let (tag, content, next) = read_any_tlv(seq_body, pos)?;
        match tag {
            TAG_CTX_0 => {
                let (en, _) = read_tlv(content, 0, TAG_ENUMERATED)?;
                if en.len() != 1 {
                    return Err(ProtoError::Auth("NegState ENUMERATED not 1 byte"));
                }
                out.neg_state = Some(NegState::from_byte(en[0])?);
            }
            TAG_CTX_1 => {
                let (oid, _) = read_tlv(content, 0, TAG_OBJECT)?;
                out.supported_mech = Some(oid.to_vec());
            }
            TAG_CTX_2 => {
                let (oct, _) = read_tlv(content, 0, TAG_OCTET)?;
                out.response_token = Some(oct.to_vec());
            }
            TAG_CTX_3 => {
                let (oct, _) = read_tlv(content, 0, TAG_OCTET)?;
                out.mech_list_mic = Some(oct.to_vec());
            }
            _ => {}
        }
        pos = next;
    }

    Ok(out)
}

/// Encode the **initial** server response to NEGOTIATE — a GSS-API-wrapped
/// `NegTokenInit` advertising NTLMSSP only. Used during SMB2 NEGOTIATE
/// when the server publishes its security blob.
pub fn encode_init_response() -> Vec<u8> {
    // mechTypes SEQUENCE { OID NTLMSSP }
    let mut mech_types_seq = Vec::new();
    write_tlv(TAG_OBJECT, OID_NTLMSSP, &mut mech_types_seq);
    let mut mech_types_outer = Vec::new();
    write_tlv(TAG_SEQUENCE, &mech_types_seq, &mut mech_types_outer);
    // mechTypes is [0] tagged.
    let mut mech_types_ctx0 = Vec::new();
    write_tlv(TAG_CTX_0, &mech_types_outer, &mut mech_types_ctx0);

    // NegTokenInit SEQUENCE { mechTypes [0] }
    let mut neg_token_init = Vec::new();
    write_tlv(TAG_SEQUENCE, &mech_types_ctx0, &mut neg_token_init);

    // [0] NegTokenInit (negotiationToken choice)
    let mut choice_init = Vec::new();
    write_tlv(TAG_CTX_0, &neg_token_init, &mut choice_init);

    // Inside [APPLICATION 0]: { OID SPNEGO, [0] NegTokenInit }
    let mut gss_inner = Vec::new();
    write_tlv(TAG_OBJECT, OID_SPNEGO, &mut gss_inner);
    gss_inner.extend_from_slice(&choice_init);

    let mut out = Vec::new();
    write_tlv(TAG_APP_0, &gss_inner, &mut out);
    out
}

/// Encode a `NegTokenResp` wrapping the server's response token (typically
/// the NTLMSSP CHALLENGE_MESSAGE or a final empty-token AcceptCompleted).
///
/// `supported_mech` is included only with `AcceptIncomplete` (i.e. the very
/// first response to a NegTokenInit) — per RFC 4178 §4.2.2.
pub fn encode_resp_token(
    state: NegState,
    supported_mech: Option<&[u8]>,
    response_token: Option<&[u8]>,
    mech_list_mic: Option<&[u8]>,
) -> Vec<u8> {
    let mut seq = Vec::new();

    // [0] negState
    {
        let mut en = Vec::new();
        write_tlv(TAG_ENUMERATED, &[state as u8], &mut en);
        write_tlv(TAG_CTX_0, &en, &mut seq);
    }
    // [1] supportedMech
    if let Some(oid) = supported_mech {
        let mut o = Vec::new();
        write_tlv(TAG_OBJECT, oid, &mut o);
        write_tlv(TAG_CTX_1, &o, &mut seq);
    }
    // [2] responseToken
    if let Some(tok) = response_token {
        let mut o = Vec::new();
        write_tlv(TAG_OCTET, tok, &mut o);
        write_tlv(TAG_CTX_2, &o, &mut seq);
    }
    // [3] mechListMIC
    if let Some(mic) = mech_list_mic {
        let mut o = Vec::new();
        write_tlv(TAG_OCTET, mic, &mut o);
        write_tlv(TAG_CTX_3, &o, &mut seq);
    }

    let mut inner = Vec::new();
    write_tlv(TAG_SEQUENCE, &seq, &mut inner);
    let mut out = Vec::new();
    write_tlv(TAG_CTX_1, &inner, &mut out);
    out
}

// ===========================================================================
// Tests
// ===========================================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn der_len_short() {
        let mut v = Vec::new();
        der_len(0x42, &mut v);
        assert_eq!(v, [0x42]);
    }

    #[test]
    fn der_len_long_one_byte() {
        let mut v = Vec::new();
        der_len(0xC8, &mut v);
        assert_eq!(v, [0x81, 0xC8]);
    }

    #[test]
    fn der_len_long_two_byte() {
        let mut v = Vec::new();
        der_len(0x1234, &mut v);
        assert_eq!(v, [0x82, 0x12, 0x34]);
    }

    #[test]
    fn read_len_round_trip() {
        for n in [0usize, 1, 0x7F, 0x80, 0xFF, 0x100, 0xFFFF, 0x10000] {
            let mut buf = Vec::new();
            der_len(n, &mut buf);
            let (got, next) = read_len(&buf, 0).unwrap();
            assert_eq!(got, n);
            assert_eq!(next, buf.len());
        }
    }

    #[test]
    fn init_response_is_decodable() {
        let blob = encode_init_response();
        // Must start with [APPLICATION 0] (0x60) tag.
        assert_eq!(blob[0], TAG_APP_0);
        // Decode with our own decoder going via decode_init_token.
        // We craft a synthetic "init" by appending an empty mechToken? — not
        // needed; decode_init_token tolerates absence. Test that the OID and
        // the [0] mechTypes are reachable.
        let init = decode_init_token(&blob).unwrap();
        assert_eq!(init.mech_types.len(), 1);
        assert_eq!(init.mech_types[0], OID_NTLMSSP);
        assert!(init.mech_token.is_none());
    }

    #[test]
    fn resp_token_round_trip_with_response() {
        let payload = b"\x01\x02\x03\x04inner-blob";
        let enc = encode_resp_token(
            NegState::AcceptIncomplete,
            Some(OID_NTLMSSP),
            Some(payload),
            None,
        );
        let dec = decode_resp_token(&enc).unwrap();
        assert_eq!(dec.neg_state, Some(NegState::AcceptIncomplete));
        assert_eq!(dec.supported_mech.as_deref(), Some(OID_NTLMSSP));
        assert_eq!(dec.response_token.as_deref(), Some(&payload[..]));
        assert!(dec.mech_list_mic.is_none());
    }

    #[test]
    fn resp_token_round_trip_completed() {
        let enc = encode_resp_token(NegState::AcceptCompleted, None, None, None);
        let dec = decode_resp_token(&enc).unwrap();
        assert_eq!(dec.neg_state, Some(NegState::AcceptCompleted));
        assert!(dec.supported_mech.is_none());
        assert!(dec.response_token.is_none());
    }

    #[test]
    fn resp_token_with_mic() {
        let mic = vec![0xAAu8; 16];
        let enc = encode_resp_token(NegState::AcceptCompleted, None, None, Some(&mic));
        let dec = decode_resp_token(&enc).unwrap();
        assert_eq!(dec.mech_list_mic.as_deref(), Some(mic.as_slice()));
    }

    /// Build a NegTokenInit by hand (containing a mechToken) and decode it.
    #[test]
    fn decode_init_with_mech_token() {
        let inner_token = b"NTLMSSP\x00fakeNegotiate";

        // mechTypes
        let mut mts = Vec::new();
        write_tlv(TAG_OBJECT, OID_NTLMSSP, &mut mts);
        let mut mts_seq = Vec::new();
        write_tlv(TAG_SEQUENCE, &mts, &mut mts_seq);
        let mut mts_ctx0 = Vec::new();
        write_tlv(TAG_CTX_0, &mts_seq, &mut mts_ctx0);

        // mechToken [2] OCTET STRING
        let mut mt_oct = Vec::new();
        write_tlv(TAG_OCTET, inner_token, &mut mt_oct);
        let mut mt_ctx2 = Vec::new();
        write_tlv(TAG_CTX_2, &mt_oct, &mut mt_ctx2);

        // SEQUENCE { [0] mechTypes, [2] mechToken }
        let mut seq = Vec::new();
        seq.extend_from_slice(&mts_ctx0);
        seq.extend_from_slice(&mt_ctx2);

        let mut neg_token_init = Vec::new();
        write_tlv(TAG_SEQUENCE, &seq, &mut neg_token_init);

        let mut choice = Vec::new();
        write_tlv(TAG_CTX_0, &neg_token_init, &mut choice);

        let mut gss_inner = Vec::new();
        write_tlv(TAG_OBJECT, OID_SPNEGO, &mut gss_inner);
        gss_inner.extend_from_slice(&choice);

        let mut blob = Vec::new();
        write_tlv(TAG_APP_0, &gss_inner, &mut blob);

        let dec = decode_init_token(&blob).unwrap();
        assert_eq!(dec.mech_types.len(), 1);
        assert_eq!(dec.mech_types[0], OID_NTLMSSP);
        assert_eq!(dec.mech_token.as_deref(), Some(&inner_token[..]));
    }

    #[test]
    fn rejects_non_spnego_oid() {
        // Build a GSS token with a different OID inside.
        let bad_oid = [0x2bu8, 0x06, 0x01, 0x01, 0x01, 0x01];
        let mut gss_inner = Vec::new();
        write_tlv(TAG_OBJECT, &bad_oid, &mut gss_inner);
        // Empty [0] payload.
        let mut empty = Vec::new();
        write_tlv(TAG_SEQUENCE, &[], &mut empty);
        let mut choice = Vec::new();
        write_tlv(TAG_CTX_0, &empty, &mut choice);
        gss_inner.extend_from_slice(&choice);
        let mut blob = Vec::new();
        write_tlv(TAG_APP_0, &gss_inner, &mut blob);

        let err = decode_init_token(&blob).unwrap_err();
        assert!(matches!(err, ProtoError::Auth(_)));
    }

    #[test]
    fn rejects_truncated_blob() {
        let err = decode_init_token(&[0x60, 0x05, 0xAA, 0xBB]).unwrap_err();
        assert!(matches!(err, ProtoError::Auth(_)));
    }
}
