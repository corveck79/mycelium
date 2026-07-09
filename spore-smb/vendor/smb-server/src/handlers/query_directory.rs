//! QUERY_DIRECTORY handler.

use std::sync::Arc;

use crate::proto::header::Smb2Header;
use crate::proto::messages::{FileInfoClass, QueryDirectoryRequest, QueryDirectoryResponse};

use crate::conn::state::{Connection, DirCursor};
use crate::dispatch::HandlerResponse;
use crate::handlers::shared::{lookup_open, lookup_session_tree};
use crate::info_class::{align8, encode_dir_entry};
use crate::ntstatus;
use crate::server::ServerState;
use crate::utils::utf16le_to_string;

pub async fn handle(
    _server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    hdr: &Smb2Header,
    body: &[u8],
) -> HandlerResponse {
    let req = match QueryDirectoryRequest::parse(body) {
        Ok(r) => r,
        Err(_) => return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    };
    if FileInfoClass::from_u8(req.file_information_class).is_none() {
        return HandlerResponse::err(ntstatus::STATUS_INVALID_INFO_CLASS);
    }
    let class_byte = req.file_information_class;

    let tree_arc = match lookup_session_tree(conn, hdr).await {
        Ok(t) => t,
        Err(s) => return HandlerResponse::err(s),
    };
    let open_arc = match lookup_open(&tree_arc, req.file_id).await {
        Some(o) => o,
        None => return HandlerResponse::err(ntstatus::STATUS_FILE_CLOSED),
    };

    let pattern_str = utf16le_to_string(&req.file_name);
    let pattern: Option<String> = if pattern_str.is_empty() || pattern_str == "*" {
        None
    } else {
        Some(pattern_str)
    };

    let restart = req.flags & QueryDirectoryRequest::FLAG_RESTART_SCANS != 0
        || req.flags & QueryDirectoryRequest::FLAG_REOPEN != 0;
    let single_entry = req.flags & QueryDirectoryRequest::FLAG_RETURN_SINGLE_ENTRY != 0;

    // Populate or refresh the cursor.
    {
        let mut open = open_arc.write().await;
        if !open.is_directory {
            return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER);
        }
        if open.search_state.is_none() || restart {
            let entries = match open.handle.as_ref() {
                Some(h) => h.list_dir(pattern.as_deref()).await,
                None => return HandlerResponse::err(ntstatus::STATUS_FILE_CLOSED),
            };
            let entries = match entries {
                Ok(e) => e,
                Err(e) => return HandlerResponse::err(e.to_nt_status()),
            };
            open.search_state = Some(DirCursor {
                entries,
                next: 0,
                pattern: pattern.clone(),
            });
        }
    }

    // Encode entries into the output buffer.
    let mut buf: Vec<u8> = Vec::new();
    let mut last_offset_pos: Option<usize> = None;
    let cap = req.output_buffer_length as usize;

    {
        let mut open = open_arc.write().await;
        let cursor = open.search_state.as_mut().expect("populated above");
        loop {
            if cursor.next >= cursor.entries.len() {
                break;
            }
            let entry = &cursor.entries[cursor.next];
            let file_index = entry.info.file_index;
            let mut bytes = encode_dir_entry(class_byte, entry, file_index);
            if bytes.is_empty() {
                cursor.next += 1;
                continue;
            }

            // Determine total size with padding for chaining.
            let entry_aligned = align8(bytes.len());
            // If this is *not* the first entry, we already padded the previous
            // entry up to entry_aligned. We commit only if total fits.
            let prev_len = buf.len();
            let total_after = prev_len + entry_aligned;
            if total_after > cap && !buf.is_empty() {
                // No room for this entry; stop.
                break;
            }
            // Patch previous NextEntryOffset.
            if let Some(prev_off) = last_offset_pos {
                let delta = (prev_len - prev_off) as u32;
                buf[prev_off..prev_off + 4].copy_from_slice(&delta.to_le_bytes());
            }
            // Track NextEntryOffset position for the entry we are appending.
            last_offset_pos = Some(prev_len);
            // Append the entry, then pad to 8.
            let target_len = prev_len + entry_aligned;
            buf.append(&mut bytes);
            while buf.len() < target_len {
                buf.push(0);
            }
            cursor.next += 1;
            if single_entry {
                break;
            }
        }
    }
    if buf.is_empty() {
        return HandlerResponse::err(ntstatus::STATUS_NO_MORE_FILES);
    }

    let resp = QueryDirectoryResponse {
        structure_size: 9,
        output_buffer_offset: 64 + 8,
        output_buffer_length: buf.len() as u32,
        buffer: buf,
    };
    let mut out = Vec::new();
    resp.write_to(&mut out).expect("encode");
    HandlerResponse::ok(out)
}
