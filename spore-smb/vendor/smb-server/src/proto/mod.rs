//! SMB2/3 wire-format types, framing, signing, and authentication primitives.
//!
//! Layered into:
//! * [`framing`] — Direct-TCP/NetBIOS transport framing.
//! * [`header`]  — SMB2 64-byte fixed header.
//! * [`messages`] — Per-command request/response structs.
//! * [`auth`]    — NTLMv2 server-side authentication and minimal SPNEGO.
//! * [`crypto`]  — Signing, key derivation, pre-auth integrity.
//! * [`error`]   — Crate-wide error type.

pub mod auth;
pub mod crypto;
pub mod error;
pub mod framing;
pub mod header;
pub mod messages;
