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

        cmake_args = [
            "--cmake-args",
            # "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
        ]

        build_cmd += cmake_args

        _run(build_cmd, cwd=ws)

        install_dir = ws / "install"
        if not install_dir.is_dir():
            raise RuntimeError(f"Local install/ directory not found at: {install_dir}")

        # ---- Ensure remote dir exists ----
        # ssh_cmd = ["ssh"]
        # if args.ssh_args:
        #     ssh_cmd += shlex.split(args.ssh_args)
        # ssh_cmd += [remote, f"mkdir -p {shlex.quote(remote_install)}"]
        # _run(ssh_cmd)

                # ---- rsync ----
        rsync_cmd_base = ["rsync", "-Laz"]

        # Make rsync use the same SSH options
        if args.ssh_args:
            ssh_remote_shell = "ssh " + " ".join(
                shlex.quote(x) for x in shlex.split(args.ssh_args)
            )
            rsync_cmd_base += ["-e", ssh_remote_shell]

        # list changes
        rsync_cmd_base += ["--itemize-changes", "--out-format=%n"]

        # Create remote destination within the same rsync remote session
        rsync_cmd_base += [
            "--rsync-path",
            f"mkdir -p {shlex.quote(remote_install)} && rsync",
        ]

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
            # Single rsync: include root metadata + selected package dirs, exclude everything else
            rsync_cmd = list(rsync_cmd_base)

            rsync_cmd += [
                "--include=/.colcon_install_layout",
                "--include=/setup.*",
                "--include=/local_setup.*",
                "--include=/_local_setup_util*",
            ]

            found_any_pkg = False
            for pkg in args.packages_select:
                local_pkg_dir = install_dir / pkg
                if not local_pkg_dir.is_dir():
                    print(f"WARNING: {local_pkg_dir} not found; skipping '{pkg}'")
                    continue

                found_any_pkg = True
                rsync_cmd += [f"--include=/{pkg}/***"]

            if not found_any_pkg:
                print("WARNING: no selected package directories found in install/")

            rsync_cmd += [
                "--exclude=*",
                str(install_dir) + "/",
                f"{remote}:{remote_install}/",
            ]
            _run(rsync_cmd)
            return 0

        # For --packages-up-to / skip / ignore / above / *-by-dep, safest is syncing full install/
        _run(rsync_cmd_base + [str(install_dir) + "/", f"{remote}:{remote_install}/"])
        return 0
