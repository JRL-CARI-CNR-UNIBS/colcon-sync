# colcon-sync

A `colcon` verb that builds a workspace (Release-style, non-symlink install) and
rsyncs the resulting `install/` directory to a remote host over SSH.

Useful for cross-compiling or building on a dev machine and deploying straight
to a robot, embedded board, or any remote machine reachable over SSH.

## Requirements

- `colcon` (with `colcon-core >= 0.7.0`)
- `rsync`
- `ssh`

All three are checked for on `PATH` before anything runs.

## Install

```bash
pip install -e .
```

This registers `sync` as a `colcon` verb via the `colcon_core.verb` entry point.

## Usage

Run from your colcon workspace root (it must contain a `src/` directory):

```bash
colcon sync <username> <ip_address> [options]
```

Example:

```bash
colcon sync pi 192.168.1.50
```

This will:

1. Run `colcon build` (with `-DCMAKE_BUILD_TYPE=RelWithDebInfo`, non-symlink install).
2. rsync the local `install/` directory to `<remote-ws>/install` on the remote
   host (default `--remote-ws` is `~/sync_ws`), creating the remote directory
   if needed.

## Options

| Flag | Description |
|---|---|
| `--workspace PATH` | Local workspace root (default: `.`). Must contain `src/`. |
| `--remote-ws PATH` | Remote workspace root (default: `~/sync_ws`). `install/` syncs to `<remote-ws>/install`. |
| `--ssh-args '...'` | Extra SSH args as a single string, e.g. `'-p 2222 -i ~/.ssh/id_ed25519'`. Applied to both the SSH connection and rsync's remote shell. |
| `--rsync-args '...'` | Extra rsync args as a single string, e.g. `'--delete --info=progress2'`. |
| `--colcon-args '...'` | Extra args appended to `colcon build` as a single string. `--symlink-install` is rejected since this verb requires a real install for rsync. |

### Package selection

These are forwarded to `colcon build`:

- `--packages-select PKG [PKG ...]`
- `--packages-up-to PKG [PKG ...]`
- `--packages-skip PKG [PKG ...]`
- `--packages-ignore PKG [PKG ...]`
- `--packages-above PKG [PKG ...]`
- `--packages-select-by-dep PKG [PKG ...]`
- `--packages-skip-by-dep PKG [PKG ...]`

**Sync behavior:**

- No selection flags → the entire `install/` directory is synced.
- `--packages-select` used **alone** → only root install metadata
  (`.colcon_install_layout`, `setup.*`, `local_setup.*`, `_local_setup_util*`)
  plus the selected package directories are synced; everything else is
  excluded. This is the fast path for iterating on one or two packages.
- Any other selection flag (`--packages-up-to`, `--packages-skip`, etc.),
  alone or combined — the full `install/` directory is synced, since a
  partial dependency graph isn't safe to sync selectively.

## Examples

Sync everything:

```bash
colcon sync pi 192.168.1.50
```

Build and sync only specific packages, with a custom remote workspace and SSH key:

```bash
colcon sync pi 192.168.1.50 \
  --remote-ws ~/robot_ws \
  --packages-select my_pkg my_pkg_msgs \
  --ssh-args '-i ~/.ssh/id_ed25519' \
  --rsync-args '--delete'
```

Build up to a package, syncing the full install tree:

```bash
colcon sync pi 192.168.1.50 --packages-up-to my_pkg
```
