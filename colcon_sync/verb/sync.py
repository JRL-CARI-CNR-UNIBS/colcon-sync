from __future__ import annotations

import argparse
import glob
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional

from colcon_core.plugin_system import satisfies_version
from colcon_core.verb import VerbExtensionPoint


def _require_cmd(cmd: str) -> None:
    r = subprocess.run(
        ["bash", "-lc", f"command -v {shlex.quote(cmd)} >/dev/null 2>&1"]
    )
    if r.returncode != 0:
        raise RuntimeError(f"Required command not found in PATH: {cmd}")


def _run(cmd: List[str], *, cwd: Optional[Path] = None) -> None:
    print("+", " ".join(shlex.quote(c) for c in cmd))
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def _append_multi(cmd: List[str], flag: str, values: Optional[List[str]]) -> None:
    if values:
        cmd += [flag, *values]


class SyncVerb(VerbExtensionPoint):
    """Build workspace (Release) and rsync install/ to a remote directory."""

    def add_arguments(self, *, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("username", help="Remote SSH username")
        parser.add_argument("ip_address", help="Remote host/IP address")

        parser.add_argument(
            "--remote-ws",
            default="~/sync_ws",
            help="Remote workspace path (default: ~/ws). install/ will sync to <remote-ws>/install",
        )
        parser.add_argument(
            "--workspace",
            default=".",
            help="Local workspace root (default: current directory). Must contain src/.",
        )
        parser.add_argument(
            "--rsync-args",
            default="",
            help='Extra rsync args as a single string, e.g. \'--delete --info=progress2\'',
        )
        parser.add_argument(
            "--ssh-args",
            default="",
            help='Extra ssh args as a single string, e.g. \'-p 2222 -i ~/.ssh/id_ed25519\'',
        )
        parser.add_argument(
            "--colcon-args",
            default="",
            help='Extra args appended to "colcon build" as a single string (will reject --symlink-install).',
        )

        # Package selection flags (passed through to `colcon build`)
        g = parser.add_argument_group("Package selection (forwarded to `colcon build`)")
        g.add_argument("--packages-select", nargs="+", dest="packages_select")
        g.add_argument("--packages-up-to", nargs="+", dest="packages_up_to")
        g.add_argument("--packages-skip", nargs="+", dest="packages_skip")
        g.add_argument("--packages-ignore", nargs="+", dest="packages_ignore")
        g.add_argument("--packages-above", nargs="+", dest="packages_above")
        g.add_argument("--packages-select-by-dep", nargs="+", dest="packages_select_by_dep")
        g.add_argument("--packages-skip-by-dep", nargs="+", dest="packages_skip_by_dep")

    def main(self, *, context) -> int:
        args = context.args

        ws = Path(args.workspace).resolve()
        if not (ws / "src").is_dir():
            raise RuntimeError(f"Workspace root must contain src/: {ws}")

        _require_cmd("colcon")
        _require_cmd("rsync")
        _require_cmd("ssh")

        remote = f"{args.username}@{args.ip_address}"
        remote_ws = args.remote_ws.rstrip("/")
        remote_install = f"{remote_ws}/install"

        # ---- Build (Release, explicitly no symlink install) ----
        build_cmd = [
            "colcon",
            "build",
            "--cmake-args",
            "-DCMAKE_BUILD_TYPE=Release",
        ]

        # Forward package selection args
        _append_multi(build_cmd, "--packages-select", args.packages_select)
        _append_multi(build_cmd, "--packages-up-to", args.packages_up_to)
        _append_multi(build_cmd, "--packages-skip", args.packages_skip)
        _append_multi(build_cmd, "--packages-ignore", args.packages_ignore)
        _append_multi(build_cmd, "--packages-above", args.packages_above)
        _append_multi(build_cmd, "--packages-select-by-dep", args.packages_select_by_dep)
        _append_multi(build_cmd, "--packages-skip-by-dep", args.packages_skip_by_dep)

        extra_colcon = shlex.split(args.colcon_args) if args.colcon_args else []
        if "--symlink-install" in extra_colcon:
            raise RuntimeError(
                "This verb enforces non-symlink installs. Remove '--symlink-install' from --colcon-args."
            )
        build_cmd += extra_colcon

        _run(build_cmd, cwd=ws)

        install_dir = ws / "install"
        if not install_dir.is_dir():
            raise RuntimeError(f"Local install/ directory not found at: {install_dir}")

        # ---- Ensure remote dir exists ----
        ssh_cmd = ["ssh"]
        if args.ssh_args:
            ssh_cmd += shlex.split(args.ssh_args)
        ssh_cmd += [remote, f"mkdir -p {shlex.quote(remote_install)}"]
        _run(ssh_cmd)

        # ---- rsync ----
        rsync_cmd_base = ["rsync", "-Laz"]
        if args.rsync_args:
            rsync_cmd_base += shlex.split(args.rsync_args)

        selection_used = any([
            args.packages_select,
            args.packages_up_to,
            args.packages_skip,
            args.packages_ignore,
            args.packages_above,
            args.packages_select_by_dep,
            args.packages_skip_by_dep,
        ])

        only_packages_select = bool(args.packages_select) and not any([
            args.packages_up_to,
            args.packages_skip,
            args.packages_ignore,
            args.packages_above,
            args.packages_select_by_dep,
            args.packages_skip_by_dep,
        ])

        if not selection_used:
            # Build all -> sync all install/
            _run(rsync_cmd_base + [str(install_dir) + "/", f"{remote}:{remote_install}/"])
            return 0

        if only_packages_select:
            # Close match to original script: sync setup/metadata + install/<pkg>/
            root_patterns = [
                ".colcon_install_layout",
                "setup.*",
                "local_setup.*",
                "_local_setup_util*",
            ]
            root_files: List[str] = []
            for pat in root_patterns:
                root_files.extend(glob.glob(str(install_dir / pat)))

            if root_files:
                _run(rsync_cmd_base + root_files + [f"{remote}:{remote_install}/"])

            for pkg in args.packages_select:
                local_pkg_dir = install_dir / pkg
                if not local_pkg_dir.is_dir():
                    print(f"WARNING: {local_pkg_dir} not found; skipping '{pkg}'")
                    continue
                _run(rsync_cmd_base + [str(local_pkg_dir) + "/", f"{remote}:{remote_install}/{pkg}/"])
            return 0

        # For --packages-up-to / skip / ignore / above / *-by-dep, the exact closure can vary;
        # safest is syncing full install/ so the remote env stays coherent.
        _run(rsync_cmd_base + [str(install_dir) + "/", f"{remote}:{remote_install}/"])
        return 0
