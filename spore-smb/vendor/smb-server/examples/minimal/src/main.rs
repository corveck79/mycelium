//! Minimal `smb-server` example.
//!
//! Defaults:
//! - listen on `0.0.0.0:4445` (override with `SMB_LISTEN`),
//! - share root rooted under `$TMPDIR/smb-example` (override with `SMB_ROOT`),
//! - three shares: `public` (anonymous read+write), `media` (anonymous
//!   read-only), `home` (alice = read+write, bob = read).
//!
//! See `docs/SMOKE.md` for cross-client mount instructions.

use std::path::PathBuf;

use smb_server::{Access, LocalFsBackend, Share, SmbServer};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,smb_server=debug,smb_server::proto=info".into()),
        )
        .init();

    let root = std::env::var("SMB_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir().join("smb-example"));
    std::fs::create_dir_all(root.join("public"))?;
    std::fs::create_dir_all(root.join("media"))?;
    std::fs::create_dir_all(root.join("home"))?;
    std::fs::write(root.join("public/hello.txt"), b"hello from smb-server\n")?;

    let listen = std::env::var("SMB_LISTEN")
        .unwrap_or_else(|_| "0.0.0.0:4445".into())
        .parse()?;

    tracing::info!(?listen, ?root, "starting example smb server");

    let server = SmbServer::builder()
        .listen(listen)
        .user("alice", "password")
        .user("bob", "password")
        .share(Share::new("public", LocalFsBackend::new(root.join("public"))?).public())
        .share(
            Share::new(
                "media",
                LocalFsBackend::new(root.join("media"))?.read_only(),
            )
            .public_read_only(),
        )
        .share(
            Share::new("home", LocalFsBackend::new(root.join("home"))?)
                .user("alice", Access::ReadWrite)
                .user("bob", Access::Read),
        )
        .build()?;

    let addr = server.bind().await?;
    tracing::info!(%addr, "listening");
    server.serve().await?;
    Ok(())
}
