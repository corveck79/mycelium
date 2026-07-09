//! Cross-stack integration test: drive a real `SmbServer` backed by
//! `LocalFsBackend` over a TCP loopback through the full
//! NEGOTIATE → SESSION_SETUP (anonymous) → TREE_CONNECT → CREATE → READ →
//! CLOSE → TREE_DISCONNECT → LOGOFF flow.
//!
//! Hand-crafts the request bytes since the workspace does not depend on an SMB
//! client crate.

mod common;

use common::encode_frame;
use common::{
    STATUS_SUCCESS, anonymous_session_setup, build_header, negotiate, parse_response_header,
    read_frame, tree_connect, utf16le, write_frame,
};
use smb_server::LocalFsBackend;
use smb_server::wire::header::{Command, SMB2_FLAGS_RELATED_OPERATIONS, Smb2Header};
use smb_server::wire::messages::{
    CloseRequest, CloseResponse, CreateRequest, CreateResponse, FileId, InfoType, LogoffRequest,
    LogoffResponse, QueryDirectoryRequest, QueryDirectoryResponse, QueryInfoRequest,
    QueryInfoResponse, ReadRequest, ReadResponse, TreeDisconnectRequest, TreeDisconnectResponse,
};
use smb_server::{Share, SmbServer};
use tempfile::tempdir;
use tokio::io::AsyncWriteExt;
use tokio::net::TcpStream;

#[tokio::test]
async fn end_to_end_anon_read_localfs() {
    // 1. Pre-populate a temp dir: one file with known contents + one empty subdir.
    let td = tempdir().expect("tempdir");
    std::fs::write(td.path().join("hello.txt"), b"hi").expect("write hello.txt");
    std::fs::create_dir(td.path().join("sub")).expect("mkdir sub");

    // 2. Stand up an `SmbServer` with a single anonymous share over LocalFsBackend.
    let backend = LocalFsBackend::new(td.path()).expect("open root");
    let server = SmbServer::builder()
        .listen("127.0.0.1:0".parse().unwrap())
        .share(Share::new("share", backend).public())
        .netbios_name("TESTSERVER")
        .build()
        .expect("build");

    server.bind().await.expect("bind");
    let addr = server.local_addr().await.expect("addr");
    let handle = tokio::spawn(async move { server.serve().await });
    tokio::task::yield_now().await;

    let mut s = TcpStream::connect(addr).await.expect("connect");

    let _ = negotiate(&mut s).await;
    let session_id = anonymous_session_setup(&mut s).await;
    let tree_id = tree_connect(&mut s, "\\\\127.0.0.1\\share", session_id, 3).await;

    // ---- COMPOUND: CREATE root + QUERY_INFO(FileAll) + CLOSE -------------
    let cr_compound_req = CreateRequest {
        structure_size: 57,
        security_flags: 0,
        requested_oplock_level: 0,
        impersonation_level: 2,
        smb_create_flags: 0,
        reserved: 0,
        desired_access: 0x0012_0089,
        file_attributes: 0,
        share_access: 0x0000_0007,
        create_disposition: 1,
        create_options: 0,
        name_offset: 0x78,
        name_length: 0,
        create_contexts_offset: 0,
        create_contexts_length: 0,
        name: vec![],
        create_contexts: vec![],
    };
    let mut create_body = Vec::new();
    cr_compound_req.write_to(&mut create_body).expect("write");
    let mut create_hdr = build_header(Command::Create, 4, session_id, tree_id);

    let qi_req = QueryInfoRequest {
        structure_size: 41,
        info_type: InfoType::File as u8,
        file_information_class: 0x12,
        output_buffer_length: 4096,
        input_buffer_offset: 0,
        reserved: 0,
        input_buffer_length: 0,
        additional_information: 0,
        flags: 0,
        file_id: FileId::any(),
        input_buffer: vec![],
    };
    let mut qi_body = Vec::new();
    qi_req.write_to(&mut qi_body).expect("write");
    let mut qi_hdr = build_header(Command::QueryInfo, 5, u64::MAX, u32::MAX);
    qi_hdr.flags |= SMB2_FLAGS_RELATED_OPERATIONS;

    let close_req = CloseRequest {
        structure_size: 24,
        flags: 0,
        reserved: 0,
        file_id: FileId::any(),
    };
    let mut close_body = Vec::new();
    close_req.write_to(&mut close_body).expect("write");
    let mut close_hdr = build_header(Command::Close, 6, u64::MAX, u32::MAX);
    close_hdr.flags |= SMB2_FLAGS_RELATED_OPERATIONS;

    let mut compound = Vec::new();
    append_compound_part(&mut compound, &mut create_hdr, &create_body, true);
    append_compound_part(&mut compound, &mut qi_hdr, &qi_body, true);
    append_compound_part(&mut compound, &mut close_hdr, &close_body, false);
    let mut framed = Vec::new();
    encode_frame(&compound, &mut framed);
    s.write_all(&framed).await.expect("write compound");

    let resp = read_frame(&mut s).await;
    let (create_resp_hdr, create_resp_body) = parse_response_header(&resp);
    assert_eq!(create_resp_hdr.command, Command::Create);
    assert_eq!(create_resp_hdr.channel_sequence_status, STATUS_SUCCESS);
    assert_ne!(create_resp_hdr.next_command, 0);
    let create_next = create_resp_hdr.next_command as usize;
    let cr_compound_resp =
        CreateResponse::parse(create_resp_body).expect("parse compound create resp");
    assert_ne!(cr_compound_resp.file_id, FileId::any());

    let (query_resp_hdr, query_resp_body) = parse_response_header(&resp[create_next..]);
    assert_eq!(query_resp_hdr.command, Command::QueryInfo);
    assert_eq!(query_resp_hdr.channel_sequence_status, STATUS_SUCCESS);
    assert_ne!(query_resp_hdr.next_command, 0);
    let qi_resp = QueryInfoResponse::parse(query_resp_body).expect("parse compound query resp");
    assert!(qi_resp.buffer.len() >= 101);

    let close_offset = create_next + query_resp_hdr.next_command as usize;
    let (close_resp_hdr, close_resp_body) = parse_response_header(&resp[close_offset..]);
    assert_eq!(close_resp_hdr.command, Command::Close);
    assert_eq!(close_resp_hdr.channel_sequence_status, STATUS_SUCCESS);
    assert_eq!(close_resp_hdr.next_command, 0);
    let _ = CloseResponse::parse(close_resp_body).expect("parse compound close resp");

    // ---- CREATE share root without FILE_DIRECTORY_FILE -------------------
    // Windows Explorer opens directories this way before issuing
    // FileIdBothDirectoryInformation queries.
    let cr_root_req = CreateRequest {
        structure_size: 57,
        security_flags: 0,
        requested_oplock_level: 0,
        impersonation_level: 2,
        smb_create_flags: 0,
        reserved: 0,
        desired_access: 0x0012_0089, // FILE_GENERIC_READ
        file_attributes: 0,
        share_access: 0x0000_0007,
        create_disposition: 1, // FILE_OPEN
        create_options: 0,
        name_offset: 0x78,
        name_length: 0,
        create_contexts_offset: 0,
        create_contexts_length: 0,
        name: vec![],
        create_contexts: vec![],
    };
    let mut body = Vec::new();
    cr_root_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Create, 7, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::Create);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let cr_root_resp = CreateResponse::parse(rb).expect("parse root create resp");
    let root_dir_id = cr_root_resp.file_id;
    assert_ne!(
        cr_root_resp.file_attributes & 0x10,
        0,
        "root is a directory"
    );

    // ---- QUERY_INFO FileFullEaInformation returns no-EA, not invalid -----
    let ea_req = QueryInfoRequest {
        structure_size: 41,
        info_type: InfoType::File as u8,
        file_information_class: 0x0F,
        output_buffer_length: 4096,
        input_buffer_offset: 0,
        reserved: 0,
        input_buffer_length: 0,
        additional_information: 0,
        flags: 0,
        file_id: root_dir_id,
        input_buffer: vec![],
    };
    let mut body = Vec::new();
    ea_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::QueryInfo, 8, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, _rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::QueryInfo);
    assert_eq!(rh.channel_sequence_status, 0xC000_0052);

    // ---- QUERY_DIRECTORY FileIdBothDirectoryInformation ------------------
    let pat = utf16le("*");
    let qd_req = QueryDirectoryRequest {
        structure_size: 33,
        file_information_class: 0x25, // FileIdBothDirectoryInformation
        flags: QueryDirectoryRequest::FLAG_RESTART_SCANS,
        file_index: 0,
        file_id: root_dir_id,
        file_name_offset: 64 + 32,
        file_name_length: pat.len() as u16,
        output_buffer_length: 4096,
        file_name: pat,
    };
    let mut body = Vec::new();
    qd_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::QueryDirectory, 9, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::QueryDirectory);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let qd_resp = QueryDirectoryResponse::parse(rb).expect("parse query directory resp");
    assert!(qd_resp.output_buffer_length >= 104);
    let names = decode_file_id_both_names(&qd_resp.buffer);
    assert!(names.iter().any(|n| n == "hello.txt"), "names={names:?}");
    assert!(names.iter().any(|n| n == "sub"), "names={names:?}");

    // ---- CLOSE root dir --------------------------------------------------
    let cl_root_req = CloseRequest {
        structure_size: 24,
        flags: 0,
        reserved: 0,
        file_id: root_dir_id,
    };
    let mut body = Vec::new();
    cl_root_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Close, 10, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::Close);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let _ = CloseResponse::parse(rb).expect("parse root close resp");

    // ---- CREATE hello.txt (read-only intent) -----------------------------
    let name_u16 = utf16le("hello.txt");
    let cr_req = CreateRequest {
        structure_size: 57,
        security_flags: 0,
        requested_oplock_level: 0,
        impersonation_level: 2,
        smb_create_flags: 0,
        reserved: 0,
        desired_access: 0x0012_0089, // FILE_GENERIC_READ
        file_attributes: 0,
        share_access: 0x0000_0007, // FILE_SHARE_READ|WRITE|DELETE
        create_disposition: 1,     // FILE_OPEN
        create_options: 0,
        name_offset: 0x78,
        name_length: name_u16.len() as u16,
        create_contexts_offset: 0,
        create_contexts_length: 0,
        name: name_u16,
        create_contexts: vec![],
    };
    let mut body = Vec::new();
    cr_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Create, 11, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::Create);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let cr_resp = CreateResponse::parse(rb).expect("parse create resp");
    let file_id = cr_resp.file_id;
    assert_eq!(
        cr_resp.end_of_file, 2,
        "hello.txt was pre-populated as b\"hi\""
    );

    // ---- READ ------------------------------------------------------------
    let rd_req = ReadRequest {
        structure_size: 49,
        padding: ReadResponse::STANDARD_DATA_OFFSET,
        flags: 0,
        length: 64,
        offset: 0,
        file_id,
        minimum_count: 0,
        channel: 0,
        remaining_bytes: 0,
        read_channel_info_offset: 0,
        read_channel_info_length: 0,
        buffer: vec![0],
    };
    let mut body = Vec::new();
    rd_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Read, 12, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::Read);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let rd_resp = ReadResponse::parse(rb).expect("parse read resp");
    assert_eq!(rd_resp.data, b"hi");

    // ---- CLOSE -----------------------------------------------------------
    let cl_req = CloseRequest {
        structure_size: 24,
        flags: 0,
        reserved: 0,
        file_id,
    };
    let mut body = Vec::new();
    cl_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Close, 13, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::Close);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let _ = CloseResponse::parse(rb).expect("parse close resp");

    // ---- TREE_DISCONNECT -------------------------------------------------
    let td_req = TreeDisconnectRequest::default();
    let mut body = Vec::new();
    td_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::TreeDisconnect, 14, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::TreeDisconnect);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let _ = TreeDisconnectResponse::parse(rb).expect("parse td resp");

    // ---- LOGOFF ----------------------------------------------------------
    let lo_req = LogoffRequest::default();
    let mut body = Vec::new();
    lo_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Logoff, 15, session_id, 0);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::Logoff);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let _ = LogoffResponse::parse(rb).expect("parse logoff resp");

    drop(s);
    handle.abort();
}

fn decode_file_id_both_names(mut buf: &[u8]) -> Vec<String> {
    let mut names = Vec::new();
    loop {
        assert!(
            buf.len() >= 104,
            "short FileIdBothDirectoryInformation entry"
        );
        let next = u32::from_le_bytes(buf[0..4].try_into().unwrap()) as usize;
        let name_len = u32::from_le_bytes(buf[60..64].try_into().unwrap()) as usize;
        let name_start = 104;
        let name_end = name_start + name_len;
        assert!(buf.len() >= name_end, "short file name");
        let units: Vec<u16> = buf[name_start..name_end]
            .chunks_exact(2)
            .map(|b| u16::from_le_bytes([b[0], b[1]]))
            .collect();
        names.push(String::from_utf16(&units).expect("utf16 name"));
        if next == 0 {
            break;
        }
        assert!(buf.len() >= next, "invalid NextEntryOffset");
        buf = &buf[next..];
    }
    names
}

fn append_compound_part(
    compound: &mut Vec<u8>,
    header: &mut Smb2Header,
    body: &[u8],
    has_next: bool,
) {
    let message_len = 64 + body.len();
    header.next_command = if has_next {
        align_8(message_len) as u32
    } else {
        0
    };
    header.write(compound).expect("compound header");
    compound.extend_from_slice(body);
    if has_next {
        compound.resize(compound.len() + align_8(message_len) - message_len, 0);
    }
}

const fn align_8(n: usize) -> usize {
    (n + 7) & !7
}
