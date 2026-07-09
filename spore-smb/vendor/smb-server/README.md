# rust-smb-server

SMB server in Rust.

## Install

```sh
cargo add smb-server
```

Or in `Cargo.toml`:

```toml
[dependencies]
smb-server = "0.4"
```

## Run the example

```sh
cargo run -p minimal-smb-example
```

Listens on `0.0.0.0:4445`. Override with `SMB_LISTEN` and `SMB_ROOT`.

Shares: `public` (anon rw), `media` (anon ro), `home` (alice rw, bob ro).

## Embed

```rust
use smb_server::{Access, LocalFsBackend, Share, SmbServer};

let server = SmbServer::builder()
    .listen("0.0.0.0:4445".parse()?)
    .user("alice", "password")
    .share(
        Share::new("home", LocalFsBackend::new("/srv/home")?)
            .user("alice", Access::ReadWrite),
    )
    .build()?;

server.bind().await?;
server.serve().await?;
```

## Runtime config

Create a `ConfigHandle` before `serve()` and keep it in the task that owns admin/config updates:

```rust
use smb_server::{Access, LocalFsBackend, Share, SmbServer};

let server = SmbServer::builder()
    .listen("0.0.0.0:4445".parse()?)
    .share(Share::new("public", LocalFsBackend::new("/srv/public")?).public())
    .build()?;

let config = server.config_handle();

config.add_user("alice", "password").await?;
let home = Share::new("home", LocalFsBackend::new("/srv/home")?)
    .user("alice", Access::ReadWrite);
config
    .add_share(home)
    .await?;
config.revoke_share_user("home", "alice").await?;
config.remove_user("alice").await?;

server.serve().await?;
```

Removing a user revokes that user's active sessions. Removing a share or revoking share access closes matching active tree connections and open handles.

## License

MIT.
