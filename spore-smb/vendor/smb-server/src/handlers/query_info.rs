//! QUERY_INFO handler.

use std::sync::Arc;

use crate::proto::header::Smb2Header;
use crate::proto::messages::{InfoType, QueryInfoRequest, QueryInfoResponse};

use crate::conn::state::Connection;
use crate::dispatch::HandlerResponse;
use crate::handlers::shared::{lookup_open, lookup_session_tree};
use crate::info_class as ic;
use crate::ntstatus;
use crate::server::ServerState;

const FILE_DEVICE_DISK: u32 = 0x0000_0007;
const FILE_REMOTE_DEVICE: u32 = 0x0000_0010;

// FS attribute flags (MS-FSCC §2.5.1)
const FILE_CASE_SENSITIVE_SEARCH: u32 = 0x0000_0001;
const FILE_CASE_PRESERVED_NAMES: u32 = 0x0000_0002;
const FILE_UNICODE_ON_DISK: u32 = 0x0000_0004;
const FILE_PERSISTENT_ACLS: u32 = 0x0000_0008;
const FILE_FILE_COMPRESSION: u32 = 0x0000_0010;
const FILE_SUPPORTS_HARD_LINKS: u32 = 0x0040_0000;
const FILE_SUPPORTS_EXTENDED_ATTRIBUTES: u32 = 0x0080_0000;

pub async fn handle(
    _server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    hdr: &Smb2Header,
    body: &[u8],
) -> HandlerResponse {
    let req = match QueryInfoRequest::parse(body) {
        Ok(r) => r,
        Err(_) => return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    };
    let info_type = match req.info_type_enum() {
        Some(t) => t,
        None => return HandlerResponse::err(ntstatus::STATUS_INVALID_INFO_CLASS),
    };

    let tree_arc = match lookup_session_tree(conn, hdr).await {
        Ok(t) => t,
        Err(s) => return HandlerResponse::err(s),
    };
    let open_arc = match lookup_open(&tree_arc, req.file_id).await {
        Some(o) => o,
        None => return HandlerResponse::err(ntstatus::STATUS_FILE_CLOSED),
    };

    // Pull the file index (we use FileId.volatile as the unique handle id).
    let (file_index, info_res) = {
        let open = open_arc.read().await;
        let fid = open.file_id;
        match open.handle.as_ref() {
            Some(h) => (fid.volatile, h.stat().await),
            None => return HandlerResponse::err(ntstatus::STATUS_FILE_CLOSED),
        }
    };

    let buf: Vec<u8> = match info_type {
        InfoType::File => {
            let info = match info_res {
                Ok(i) => i,
                Err(e) => return HandlerResponse::err(e.to_nt_status()),
            };
            match req.file_information_class {
                ic::FILE_BASIC_INFORMATION => ic::encode_file_basic_information(&info),
                ic::FILE_STANDARD_INFORMATION => ic::encode_file_standard_information(&info),
                ic::FILE_INTERNAL_INFORMATION => ic::encode_file_internal_information(file_index),
                ic::FILE_EA_INFORMATION => ic::encode_file_ea_information(),
                ic::FILE_FULL_EA_INFORMATION => {
                    return HandlerResponse::err(ntstatus::STATUS_NO_EAS_ON_FILE);
                }
                ic::FILE_ACCESS_INFORMATION => ic::encode_file_access_information(0x001F_01FF),
                ic::FILE_POSITION_INFORMATION => ic::encode_file_position_information(),
                ic::FILE_MODE_INFORMATION => ic::encode_file_mode_information(0),
                ic::FILE_ALIGNMENT_INFORMATION => ic::encode_file_alignment_information(),
                ic::FILE_NAME_INFORMATION => ic::encode_file_name_information(&info.name),
                ic::FILE_ALL_INFORMATION => {
                    ic::encode_file_all_information(&info, file_index, 0x001F_01FF)
                }
                ic::FILE_NETWORK_OPEN_INFORMATION => {
                    ic::encode_file_network_open_information(&info)
                }
                ic::FILE_STREAM_INFORMATION => ic::encode_file_stream_information(&info),
                _ => return HandlerResponse::err(ntstatus::STATUS_INVALID_INFO_CLASS),
            }
        }
        InfoType::FileSystem => {
            // For FS info we use the open's tree's backend for context.
            let creation_time = info_res.as_ref().map(|i| i.creation_time).unwrap_or(0);
            match req.file_information_class {
                ic::FS_VOLUME_INFORMATION => {
                    ic::encode_fs_volume_information(creation_time, 0xCAFE_BABE, "smb-server")
                }
                ic::FS_SIZE_INFORMATION => {
                    // 1 PiB free pseudo-volume, 4 KiB cluster.
                    ic::encode_fs_size_information(
                        1u64 << 40, // total
                        1u64 << 39, // free
                        1,          // sectors per cluster
                        4096,       // bytes per sector
                    )
                }
                ic::FS_DEVICE_INFORMATION => {
                    ic::encode_fs_device_information(FILE_DEVICE_DISK, FILE_REMOTE_DEVICE)
                }
                ic::FS_ATTRIBUTE_INFORMATION => ic::encode_fs_attribute_information(
                    FILE_CASE_SENSITIVE_SEARCH
                        | FILE_CASE_PRESERVED_NAMES
                        | FILE_UNICODE_ON_DISK
                        | FILE_PERSISTENT_ACLS
                        | FILE_FILE_COMPRESSION
                        | FILE_SUPPORTS_HARD_LINKS
                        | FILE_SUPPORTS_EXTENDED_ATTRIBUTES,
                    255,
                    "NTFS",
                ),
                ic::FS_FULL_SIZE_INFORMATION => {
                    ic::encode_fs_full_size_information(1u64 << 40, 1u64 << 39, 1u64 << 39, 1, 4096)
                }
                _ => return HandlerResponse::err(ntstatus::STATUS_INVALID_INFO_CLASS),
            }
        }
        InfoType::Security => ic::encode_minimal_security_descriptor(),
        InfoType::Quota => return HandlerResponse::err(ntstatus::STATUS_NOT_SUPPORTED),
    };

    if buf.len() as u32 > req.output_buffer_length {
        return HandlerResponse::err(ntstatus::STATUS_INFO_LENGTH_MISMATCH);
    }

    let resp = QueryInfoResponse {
        structure_size: 9,
        output_buffer_offset: 64 + 8,
        output_buffer_length: buf.len() as u32,
        buffer: buf,
    };
    let mut out = Vec::new();
    resp.write_to(&mut out)
        .expect("QUERY_INFO response encodes");
    HandlerResponse::ok(out)
}
