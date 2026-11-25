#!/usr/bin/env python3
"""Quest 3 Dual-Pane File Manager — Windows / Python 3.9+, standard library only.
Author - Michael Forde
Ver - 1.5

Local PC (left) <-> Meta Quest using Android Debug Bridge (right).

- Expandable, lazy-loaded directory trees (Ctrl/Shift multi-selection).
- Recursive ADB push/pull; same-named destination folders are merged, not duplicated.
- Reliably uploads protected Android/data/obb folders file by file via ADB,
  avoiding recursive adb push secure_mkdirs failures; keeps overall progress.
- Pre-creates Quest upload directories via adb shell mkdir -p (ADB secure_mkdirs workaround).
- Target = selected destination folder, or parent of a selected destination file.
- Deliberately separate directional transfer and deletion buttons.
- Confirmation for all deletions and transfers that may overwrite existing files.
- Runs long operations off the Tkinter UI thread and can cancel a transfer.
- Shows live ADB transfer percentages in the log and progress bar.
- Installs a selected APK from either pane onto the connected Quest, with confirmation and detailed logging.

ADB is automatically located beside this script, in a nearby platform-tools
folder (including the earlier Quest Storage Explorer layout), or on PATH.
"""

from __future__ import annotations

import codecs
import ctypes
import os
import posixpath
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk


APP_TITLE = "Quest 3 — Dual-Pane File Manager"
REMOTE_HOME = "/sdcard"

# Current ADB uses e.g. "[  32%] myfile.iso" for one file, and
# "[  32%] folder/myfile.iso: 76%" for multi-file directory transfers.
ADB_PROGRESS_PATTERN = re.compile(r"^\[\s*(\d{1,3})%\]\s*(.*)$")
ADB_FILE_PATTERN = re.compile(r"^(.*):\s*(\d{1,3})%\s*$")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def parse_adb_progress(line: str) -> tuple[int, str, int | None] | None:
    """Parse ADB's overall-per-command %, optional current-file % and name.

    An ADB command can transfer a whole directory; its overall percentage is
    scoped to that command, not necessarily the entire multi-selection batch.
    """
    cleaned = ANSI_ESCAPE_PATTERN.sub("", line).strip()
    match = ADB_PROGRESS_PATTERN.match(cleaned)
    if not match:
        return None
    percent = int(match.group(1))
    if percent > 100:
        return None
    filename = match.group(2).strip()
    file_percent = None
    subfile = ADB_FILE_PATTERN.match(filename)
    if subfile and 0 <= int(subfile.group(2)) <= 100:
        filename = subfile.group(1)
        file_percent = int(subfile.group(2))
    return percent, filename, file_percent


@dataclass(frozen=True)
class FileEntry:
    path: str
    kind: str  # 'dir', 'file', 'link', or 'virtual'
    name: str

    @property
    def is_dir(self) -> bool:
        return self.kind == "dir"


def normalize_remote(path: str) -> str:
    if not path or not path.startswith("/"):
        raise ValueError("Quest paths must be absolute (for example, /sdcard/Download).")
    return posixpath.normpath(path)


def remote_child(parent: str, name: str) -> str:
    return posixpath.join(parent, name)


def target_folder(entry: FileEntry, side: str) -> str:
    if entry.kind == "virtual":
        raise ValueError("Select a real folder or a file, not 'This PC'.")
    if entry.kind == "link":
        raise ValueError("Symbolic links cannot be used as destinations.")
    if entry.kind == "dir":
        return entry.path
    return os.path.dirname(entry.path) if side == "local" else posixpath.dirname(entry.path)


def transfer_destination_for_item(source: str, entry: FileEntry, destination: str) -> tuple[str, bool]:
    """Return the directory to pass to ADB and whether it merges a named folder.

    ADB copies a source *directory* beneath its destination directory. When a
    user selects a directory called GM8E01 on both sides, passing GM8E01 as
    the ADB destination would incorrectly create GM8E01/GM8E01. Instead pass
    the selected directory's parent, so ADB merges with the existing GM8E01.
    For other folder names, retain the original create-subfolder behaviour.
    """
    if source not in ("local", "quest"):
        raise ValueError("Invalid transfer direction")
    if entry.kind != "dir":
        return destination, False
    if source == "local":  # destination is an Android POSIX directory
        normalized = normalize_remote(destination)
        same_name = entry.name == posixpath.basename(normalized)
        parent = posixpath.dirname(normalized)
    else:  # destination is a local PC directory
        normalized = os.path.normpath(destination)
        name = os.path.basename(normalized)
        same_name = os.path.normcase(entry.name) == os.path.normcase(name)
        parent = os.path.dirname(normalized)
    if same_name and parent and parent != normalized:
        return parent, True
    return destination, False


def remote_upload_directories(local_folder: str, remote_folder: str) -> list[str]:
    """Build remote directories to pre-create for a recursive ADB folder push.

    ADB push can fail in its own secure_mkdirs path even if an explicit Android
    shell mkdir would work. Using mkdir -p for all directories also covers
    subfolders inside texture packs, not just their root folder.
    """
    local_root = Path(local_folder)
    if not local_root.is_dir():
        raise RuntimeError(f"PC source folder is no longer available: {local_folder}")
    remote_root = normalize_remote(remote_folder)
    result = [remote_root]

    def onerror(error: OSError) -> None:
        raise error

    for parent, dirs, _files in os.walk(local_folder, followlinks=False, onerror=onerror):
        # Do not pre-create target trees for symlink directories. This also
        # prevents an accidental walk beyond the chosen local folder.
        dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
        for name in dirs:
            relative = Path(parent, name).relative_to(local_root)
            result.append(posixpath.join(remote_root, *relative.parts))
    return result



def is_protected_quest_destination(destination: str) -> bool:
    """Android app-specific external storage: use single-file ADB uploads here.

    Do not mistake a folder named Android elsewhere in the path for the
    Android/data or Android/obb directories rooted at shared storage.
    """
    normalized = normalize_remote(destination)
    storage_roots = ("/sdcard", "/storage/emulated/0")
    return any(normalized.startswith(root + "/Android/data/") or
               normalized.startswith(root + "/Android/obb/")
               for root in storage_roots)


def enumerate_upload_files(local_folder: str, remote_folder: str) -> list[tuple[str, str, int]]:
    """Plan file-by-file upload preserving every relative subdirectory.

    Symlinks are ignored (even those pointing to files), avoiding traversal
    outside the selected folder and accidental unintended uploads.
    """
    root = Path(local_folder)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError(f"PC source folder is not a real directory: {local_folder}")
    remote_root = normalize_remote(remote_folder)
    result: list[tuple[str, str, int]] = []

    def onerror(error: OSError) -> None:
        raise error

    for current, directories, files in os.walk(root, followlinks=False, onerror=onerror):
        directories[:] = sorted((d for d in directories if not (Path(current) / d).is_symlink()),
                                key=str.casefold)
        for filename in sorted(files, key=str.casefold):
            local = Path(current) / filename
            if local.is_symlink() or not local.is_file():
                continue
            relative = local.relative_to(root)
            remote = posixpath.join(remote_root, *relative.parts)
            result.append((str(local), remote, local.stat().st_size))
    return result


def remote_mkdir_batches(paths: list[str], max_length: int = 6500) -> list[str]:
    """Produce bounded, safely shell-quoted mkdir commands for adb shell -T."""
    prefix = "mkdir -p -- "
    batches: list[str] = []
    command = prefix
    for path in paths:
        encoded = shlex.quote(normalize_remote(path))
        if len(command) + len(encoded) + 1 > max_length and command != prefix:
            batches.append(command)
            command = prefix
        command += encoded + " "
    if command != prefix:
        batches.append(command.rstrip())
    return batches


def quest_storage_permission_hint(error: str, source: str, destination: str) -> str:
    """Explain genuine Quest restrictions without claiming they can be bypassed."""
    if (source == "local" and
            ("/Android/data/" in destination or "/Android/obb/" in destination) and
            any(marker in error.lower() for marker in (
                "operation not permitted", "permission denied", "secure_mkdirs"
            ))):
        return ("\n\nQUEST STORAGE PERMISSION: Android denied a write in protected "
                "app storage. The file manager cannot override Quest permissions. "
                "Check that the target folder already exists; if it does and this "
                "still fails, the headset is blocking this ADB operation. "
                "Use the game's supported import method or an on-headset "
                "file manager with access to that app's directory.")
    return ""


def safe_delete_target(entry: FileEntry, side: str) -> bool:
    if entry.kind in ("virtual", "link"):
        return False
    if side == "local":
        p = Path(entry.path)
        if p.parent == p or p == Path.home():
            return False
        # Protect the root of any Windows drive (or Unix filesystem).
        if os.path.normcase(os.path.abspath(entry.path)) == os.path.normcase(os.path.abspath(p.anchor)):
            return False
        return True
    p = normalize_remote(entry.path)
    # Restrict destructive device operations to shared/user storage.
    allowed_roots = ("/sdcard", "/storage/emulated/0")
    return any(p.startswith(root + "/") for root in allowed_roots)


def locate_adb(script_dir: Path) -> Path | None:
    filename = "adb.exe" if os.name == "nt" else "adb"
    places = [
        script_dir / "platform-tools" / filename,
        script_dir / filename,
        script_dir.parent / "platform-tools" / filename,
        script_dir / "Quest3-Storage-Explorer-AutoSetup" / "platform-tools" / filename,
        script_dir.parent / "Quest3-Storage-Explorer-AutoSetup" / "platform-tools" / filename,
    ]
    for item in places:
        if item.is_file():
            return item
    found = shutil.which("adb")
    return Path(found) if found else None


def list_windows_drives() -> list[str]:
    if os.name != "nt":
        return ["/"]
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]


def parse_remote_listing(raw: bytes, folder: str) -> list[FileEntry]:
    """Parse NUL-delimited kind/name pairs. Handles spaces and newlines in names."""
    if not raw:
        return []
    parts = raw.split(b"\0")
    if parts[-1] == b"":
        parts.pop()
    if len(parts) % 2:
        raise RuntimeError("Unexpected directory listing from ADB (incomplete result).")
    found = []
    for i in range(0, len(parts), 2):
        kind_code = parts[i]
        if kind_code not in (b"D", b"F", b"L"):
            raise RuntimeError("Unexpected directory listing from ADB (invalid entry type).")
        name = parts[i + 1].decode("utf-8", errors="replace")
        if name in ("", ".", ".."):
            continue
        kind = {b"D": "dir", b"F": "file", b"L": "link"}[kind_code]
        found.append(FileEntry(remote_child(folder, name), kind, name))
    return sorted(found, key=lambda entry: (entry.kind != "dir", entry.name.casefold()))


class FileManager:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.minsize(850, 500)
        self._maximize()
        self.script_dir = Path(__file__).resolve().parent
        self.adb: Path | None = locate_adb(self.script_dir)
        self.serial: str | None = None
        self.device_label = tk.StringVar(value="Quest: checking ADB connection…")
        self.status = tk.StringVar(value="Ready")
        self.local_label = tk.StringVar(value="PC: select a folder or file")
        self.quest_label = tk.StringVar(value="Quest: select a folder or file")
        self.events: queue.Queue[tuple] = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="questfile")
        self.tree: dict[str, ttk.Treeview] = {}
        self.entries: dict[str, dict[str, FileEntry]] = {"local": {}, "quest": {}}
        self.dir_state: dict[str, dict[str, str]] = {"local": {}, "quest": {}}
        self.root_nodes: dict[str, list[str]] = {"local": [], "quest": []}
        self.busy = False
        self.operation_kind: str | None = None
        self.active_side: str | None = None
        self.stop_transfer = threading.Event()
        self.process_lock = threading.Lock()
        self.active_process: subprocess.Popen | None = None
        self._closed = False
        self._style()
        self._build_ui()
        self._make_roots()
        self.root.after(70, self._drain_events)
        self.root.after(200, self.connect_device)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _maximize(self) -> None:
        try:
            self.root.state("zoomed")  # Maximized, not fullscreen, on Windows.
        except tk.TclError:
            try:
                self.root.attributes("-zoomed", True)
            except tk.TclError:
                w, h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
                self.root.geometry(f"{max(850, w - 55)}x{max(500, h - 85)}+0+0")

    def _style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("PaneTitle.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Action.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 13))
        style.configure("Small.TButton", padding=(8, 5))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="Quest 3 File Transfer", style="PaneTitle.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.device_label).pack(side="left", padx=25)
        ttk.Button(top, text="Reconnect Quest", command=self.connect_device, style="Small.TButton").pack(side="right")
        ttk.Button(top, text="ADB Location…", command=self.choose_adb, style="Small.TButton").pack(side="right", padx=8)

        panes = ttk.Frame(outer)
        panes.pack(fill="both", expand=True)
        panes.columnconfigure(0, weight=1, uniform="pane")
        panes.columnconfigure(1, weight=0)
        panes.columnconfigure(2, weight=1, uniform="pane")
        panes.rowconfigure(0, weight=1)
        self._build_side(panes, "local", 0, "LOCAL PC", self.local_label)
        self._build_center(panes)
        self._build_side(panes, "quest", 2, "QUEST 3 (ADB)", self.quest_label)

        progress_row = ttk.Frame(outer)
        progress_row.pack(fill="x", pady=(10, 4))
        ttk.Label(progress_row, textvariable=self.status).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(progress_row, mode="indeterminate", length=210)
        self.progress.pack(side="right", padx=(10, 0))
        self.cancel_btn = ttk.Button(progress_row, text="Cancel operation", command=self.cancel_transfer, state="disabled")
        self.cancel_btn.pack(side="right")

        ttk.Label(outer, text="Operation log (long transfers run in the background):").pack(anchor="w", pady=(3, 3))
        self.log = scrolledtext.ScrolledText(outer, height=5, font=("Consolas", 9), wrap="word", state="disabled")
        self.log.pack(fill="x", expand=False)
        self._write_log("Select a source on one side and a destination on the other. Same-named folders merge. Ctrl/Shift selects multiple items.")

    def _build_side(self, parent: ttk.Frame, side: str, col: int, title: str, labelvar: tk.StringVar) -> None:
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.grid(row=0, column=col, sticky="nsew")
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        if side == "local":
            ttk.Button(toolbar, text="Open PC Folder…", command=self.add_local_folder, style="Small.TButton").pack(side="left")
        else:
            ttk.Button(toolbar, text="Go to Quest Path…", command=self.add_quest_path, style="Small.TButton").pack(side="left")
        ttk.Button(toolbar, text="Refresh", command=lambda s=side: self.refresh_selection(s), style="Small.TButton").pack(side="right")

        holder = ttk.Frame(frame)
        holder.grid(row=1, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        tree = ttk.Treeview(holder, show="tree headings", columns=("type",), selectmode="extended")
        tree.heading("#0", text="Folder / file")
        tree.column("#0", minwidth=200, width=390, stretch=True)
        tree.heading("type", text="Type")
        tree.column("type", width=70, minwidth=65, stretch=False, anchor="center")
        tree.grid(row=0, column=0, sticky="nsew")
        sy = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        sy.grid(row=0, column=1, sticky="ns")
        sx = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
        sx.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        tree.bind("<<TreeviewOpen>>", lambda _evt, s=side: self._open_selected(s))
        tree.bind("<<TreeviewSelect>>", lambda _evt, s=side: self._show_selected(s))
        # Track which pane the user actually interacted with, even though both
        # trees retain a selection (e.g. source and destination for transfers).
        tree.bind("<ButtonPress-1>", lambda _evt, s=side: self._set_active_side(s), add="+")
        tree.bind("<FocusIn>", lambda _evt, s=side: self._set_active_side(s), add="+")
        self.tree[side] = tree

        desc = ttk.Label(frame, textvariable=labelvar, wraplength=480)
        desc.grid(row=2, column=0, sticky="ew", pady=(8, 5))
        text = "DELETE FROM PC…" if side == "local" else "DELETE FROM QUEST…"
        ttk.Button(frame, text=text, command=lambda s=side: self.delete_selected(s), style="Small.TButton").grid(row=3, column=0, sticky="ew")

    def _build_center(self, parent: ttk.Frame) -> None:
        center = ttk.Frame(parent, padding=(10, 0))
        center.grid(row=0, column=1, sticky="ns")
        ttk.Label(center, text="TRANSFER", font=("Segoe UI", 11, "bold")).pack(pady=(100, 15))
        self.upload_btn = ttk.Button(center, text="TRANSFER  →", style="Action.TButton",
                                     command=lambda: self.start_transfer("local"))
        self.upload_btn.pack(fill="x", pady=(0, 4))
        ttk.Label(center, text="PC → Quest", anchor="center").pack(fill="x", pady=(0, 27))
        self.download_btn = ttk.Button(center, text="←  TRANSFER", style="Action.TButton",
                                       command=lambda: self.start_transfer("quest"))
        self.download_btn.pack(fill="x", pady=(0, 4))
        ttk.Label(center, text="Quest → PC", anchor="center").pack(fill="x")
        ttk.Separator(center, orient="horizontal").pack(fill="x", pady=(32, 16))
        self.install_btn = ttk.Button(center, text="INSTALL APK", style="Action.TButton",
                                      command=self.install_selected_apk)
        self.install_btn.pack(fill="x", pady=(0, 6))
        self.install_hint = tk.StringVar(value="Click one APK on either side")
        ttk.Label(center, textvariable=self.install_hint, justify="center",
                  wraplength=180).pack(fill="x")
        ttk.Label(center, text="For copying: select source(s)\nand a destination\non opposite sides",
                  justify="center").pack(pady=(24, 0))

    def _set_active_side(self, side: str) -> None:
        """Install APK uses the last pane explicitly clicked or keyboard-focused."""
        self.active_side = side
        self.install_hint.set("Selected pane: PC" if side == "local" else "Selected pane: Quest")

    def _make_roots(self) -> None:
        home = str(Path.home())
        home_id = self._add_entry("local", "", FileEntry(home, "dir", f"Home — {home}"))
        self.root_nodes["local"].append(home_id)
        pc_id = self._add_entry("local", "", FileEntry("", "virtual", "This PC — drives"))
        self.root_nodes["local"].append(pc_id)
        tree = self.tree["local"]
        for drive in list_windows_drives():
            self._add_entry("local", pc_id, FileEntry(drive, "dir", drive))
        tree.item(pc_id, open=True)
        tree.item(home_id, open=True)
        tree.selection_set(home_id)
        self._load_dir("local", home_id)

        quest_id = self._add_entry("quest", "", FileEntry(REMOTE_HOME, "dir", "Quest shared storage — /sdcard"))
        self.root_nodes["quest"].append(quest_id)
        self.tree["quest"].item(quest_id, open=True)
        self.tree["quest"].selection_set(quest_id)

    def _add_entry(self, side: str, parent: str, entry: FileEntry) -> str:
        typ = {"dir": "Folder", "file": "File", "link": "Link", "virtual": "Drives"}[entry.kind]
        node_id = self.tree[side].insert(parent, "end", text=entry.name, values=(typ,))
        self.entries[side][node_id] = entry
        if entry.is_dir:
            self.dir_state[side][node_id] = "unloaded"
            self.tree[side].insert(node_id, "end", text="(expand to browse)")
        return node_id

    def _show_selected(self, side: str) -> None:
        selected = self.tree[side].selection()
        if not selected:
            return
        valid = [self.entries[side][i] for i in selected if i in self.entries[side]]
        if not valid:
            return
        label = self.local_label if side == "local" else self.quest_label
        if len(valid) == 1:
            label.set(valid[0].path or valid[0].name)
        else:
            label.set(f"{len(valid)} items selected. First: {valid[0].path}")

    def _open_selected(self, side: str) -> None:
        item = self.tree[side].focus()
        if item in self.entries[side] and self.entries[side][item].is_dir:
            self._load_dir(side, item)

    def _load_dir(self, side: str, node: str, force: bool = False) -> None:
        entry = self.entries[side].get(node)
        if not entry or not entry.is_dir:
            return
        state = self.dir_state[side].get(node)
        if state == "loading" or (state == "loaded" and not force):
            return
        if side == "quest" and not self.serial:
            return
        self.dir_state[side][node] = "loading"
        self.status.set(f"Reading {side}: {entry.path}")
        self.pool.submit(self._list_worker, side, node, entry.path)

    def _list_worker(self, side: str, node: str, path: str) -> None:
        try:
            if side == "local":
                entries = []
                with os.scandir(path) as scan:
                    for item in scan:
                        kind = "link" if item.is_symlink() else ("dir" if item.is_dir(follow_symlinks=False) else "file")
                        entries.append(FileEntry(item.path, kind, item.name))
                entries.sort(key=lambda it: (it.kind != "dir", it.name.casefold()))
            else:
                entries = self._remote_list(path)
            self.events.put(("listed", side, node, path, entries, None))
        except Exception as exc:
            self.events.put(("listed", side, node, path, [], str(exc)))

    def _remote_list(self, folder: str) -> list[FileEntry]:
        """Use device shell globbing + NUL-delimited pairs (not fragile 'ls -l' parsing).

        Paths are assigned to a quoted shell variable. Even quotes, brackets and
        semicolons inside filenames are treated as ordinary filename characters.
        No root privileges or external Android tools/packages are required.
        """
        folder = normalize_remote(folder)
        script = (
            "p=" + shlex.quote(folder) + "; "
            "if [ ! -d \"$p\" ] || [ ! -r \"$p\" ] || [ ! -x \"$p\" ]; then "
            "printf 'Not a directory or access denied\\n' >&2; exit 2; fi; "
            "for f in \"$p\"/* \"$p\"/.[!.]* \"$p\"/..?*; do "
            "[ -e \"$f\" ] || [ -L \"$f\" ] || continue; "
            "if [ -L \"$f\" ]; then k=L; "
            "elif [ -d \"$f\" ]; then k=D; else k=F; fi; "
            "printf '%s\\0%s\\0' \"$k\" \"${f##*/}\"; "
            "done"
        )
        result = self._adb_run(["shell", "-T", script], timeout=90)
        return parse_remote_listing(result.stdout, folder)

    def _adb_run(self, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
        if not self.adb:
            raise RuntimeError("ADB not found. Click 'ADB Location…' and choose adb.exe from platform-tools.")
        cmd = [str(self.adb)]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += args
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout, **self._popen_options())
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ADB timed out after {timeout} seconds: {args[0]}") from exc
        except OSError as exc:
            raise RuntimeError(f"Could not launch ADB: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or f"ADB returned error code {result.returncode}.")
        return result

    @staticmethod
    def _popen_options() -> dict:
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def _forget_children(self, side: str, node: str) -> None:
        tree = self.tree[side]
        def recurse(item: str) -> None:
            for child in tree.get_children(item):
                recurse(child)
            self.entries[side].pop(item, None)
            self.dir_state[side].pop(item, None)
        for child in tree.get_children(node):
            recurse(child)
            tree.delete(child)

    def _populate_dir(self, side: str, node: str, expected: str, children: list[FileEntry], err: str | None) -> None:
        current = self.entries[side].get(node)
        if not current or current.path != expected:
            return
        self._forget_children(side, node)
        if err:
            self.dir_state[side][node] = "error"
            self.tree[side].insert(node, "end", text="⚠ Unable to read — Refresh to retry", values=("Error",))
            self._write_log(f"ERROR reading {side} folder {expected}: {err}")
            self.status.set(f"Could not read: {expected}")
        else:
            self.dir_state[side][node] = "loaded"
            for child in children:
                self._add_entry(side, node, child)
            self.status.set(f"{side.title()} folder loaded: {expected} ({len(children)} entries)")

    def choose_adb(self) -> None:
        name = filedialog.askopenfilename(title="Choose adb.exe (Android SDK platform-tools)",
                                         filetypes=[("ADB", "adb.exe"), ("All files", "*.*")])
        if name:
            self.adb = Path(name)
            self.serial = None
            self.connect_device()

    def connect_device(self) -> None:
        if self.busy:
            messagebox.showinfo("Transfer in progress", "Finish or cancel the transfer before reconnecting ADB.")
            return
        if not self.adb:
            self.device_label.set("Quest: adb.exe not found — choose ADB Location…")
            self.status.set("ADB is missing")
            return
        self.device_label.set("Quest: checking connection…")
        self.serial = None
        self.pool.submit(self._connect_worker)

    def _connect_worker(self) -> None:
        try:
            assert self.adb is not None
            result = subprocess.run([str(self.adb), "devices", "-l"], capture_output=True, timeout=18,
                                    **self._popen_options())
            output = result.stdout.decode("utf-8", errors="replace")
            if result.returncode:
                raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or "adb devices failed")
            available = []
            waiting = []
            for line in output.splitlines():
                fields = line.split()
                if len(fields) < 2 or fields[0].lower() == "list":
                    continue
                if fields[1] == "device":
                    available.append((fields[0], line))
                elif fields[1] in ("unauthorized", "offline"):
                    waiting.append(f"{fields[0]} ({fields[1]})")
            if len(available) > 1:
                raise RuntimeError("Multiple ADB devices are connected. Disconnect the others to choose the Quest safely.")
            if not available:
                message = ("No authorized Quest detected. Unlock the Quest, enable Developer Mode, "
                           "check USB debugging, and approve the USB debugging prompt inside the headset.")
                if waiting:
                    message += " Devices: " + ", ".join(waiting)
                raise RuntimeError(message)
            self.events.put(("connected", available[0][0], available[0][1], None))
        except Exception as exc:
            self.events.put(("connected", None, "", str(exc)))

    def add_local_folder(self) -> None:
        selected = filedialog.askdirectory(title="Open a PC folder as a tree root", mustexist=True)
        if not selected:
            return
        node = self._add_entry("local", "", FileEntry(selected, "dir", f"📁 {selected}"))
        self.root_nodes["local"].append(node)
        self.tree["local"].item(node, open=True)
        self.tree["local"].selection_set(node)
        self._set_active_side("local")
        self.tree["local"].see(node)
        self._load_dir("local", node)

    def add_quest_path(self) -> None:
        given = simpledialog.askstring("Go to Quest folder", "Enter a full Android folder path:",
                                      initialvalue="/sdcard/Android/data", parent=self.root)
        if given is None:
            return
        try:
            path = normalize_remote(given.strip())
        except ValueError as exc:
            messagebox.showerror("Invalid path", str(exc))
            return
        for node, entry in self.entries["quest"].items():
            if entry.is_dir and entry.path == path:
                self.tree["quest"].selection_set(node)
                self._set_active_side("quest")
                self.tree["quest"].see(node)
                self.tree["quest"].item(node, open=True)
                self._load_dir("quest", node)
                return
        node = self._add_entry("quest", "", FileEntry(path, "dir", f"Quest — {path}"))
        self.root_nodes["quest"].append(node)
        self.tree["quest"].item(node, open=True)
        self.tree["quest"].selection_set(node)
        self._set_active_side("quest")
        self.tree["quest"].see(node)
        self._load_dir("quest", node)

    def refresh_selection(self, side: str) -> None:
        tree = self.tree[side]
        node = tree.focus()
        if not node or node not in self.entries[side]:
            chosen = tree.selection()
            node = next((x for x in chosen if x in self.entries[side]), "")
        if not node:
            node = self.root_nodes[side][0]
        if not self.entries[side][node].is_dir:
            node = tree.parent(node)
        if node in self.entries[side] and self.entries[side][node].is_dir:
            tree.item(node, open=True)
            self._load_dir(side, node, force=True)

    def refresh_path(self, side: str, path: str) -> None:
        # Refresh an existing visible destination folder, regardless of which item is focused.
        for node, entry in list(self.entries[side].items()):
            if entry.kind == "dir" and entry.path == path:
                self._load_dir(side, node, force=True)
                return
        self.refresh_selection(side)

    def _selected_entries(self, side: str) -> list[FileEntry]:
        selected = [i for i in self.tree[side].selection() if i in self.entries[side]]
        chosen = set(selected)
        unique = []
        for item in selected:
            ancestor = self.tree[side].parent(item)
            redundant = False
            while ancestor:
                if ancestor in chosen and self.entries[side].get(ancestor, FileEntry("", "", "")).is_dir:
                    redundant = True
                    break
                ancestor = self.tree[side].parent(ancestor)
            if not redundant:
                unique.append(self.entries[side][item])
        return unique

    def install_selected_apk(self) -> None:
        """Install one selected .apk from the most recently used pane."""
        if self.busy:
            messagebox.showinfo("Operation in progress", "Finish or cancel the current operation first.")
            return
        if not self.adb or not self.serial:
            messagebox.showerror("Quest not connected", "Connect the Quest and approve ADB debugging first.")
            return
        side = self.active_side
        if side not in ("local", "quest"):
            messagebox.showwarning("Select an APK", "Click an APK in either the PC or Quest pane first.")
            return
        selected = self._selected_entries(side)
        if len(selected) != 1 or selected[0].kind != "file" or not selected[0].name.lower().endswith(".apk"):
            messagebox.showwarning(
                "Select one APK",
                f"Select exactly one .apk file in the {'PC' if side == 'local' else 'Quest'} pane.\n"
                "Folders, shortcuts, and multiple selections cannot be installed.",
            )
            return
        apk = selected[0]
        if side == "local" and not Path(apk.path).is_file():
            messagebox.showerror("APK not found", f"This file no longer exists:\n{apk.path}")
            return
        if side == "quest":
            try:
                normalize_remote(apk.path)
            except ValueError as exc:
                messagebox.showerror("Invalid Quest path", str(exc))
                return
        source_label = "PC" if side == "local" else "Quest"
        extra = ("The APK will first be copied into a temporary PC folder, then installed.\n"
                 "That temporary copy will be removed afterward.\n\n") if side == "quest" else ""
        if not messagebox.askyesno(
            "ARE YOU SURE? — Install APK",
            f"Install this APK onto the connected Quest 3?\n\n"
            f"Source: {source_label}\n"
            f"File: {apk.path}\n"
            f"Device: {self.serial}\n\n"
            + extra
            + "ADB will install or update the app (-r), preserving existing app data where Android permits.\n"
              "Android may reject an incompatible APK, an older version, or a signing-key mismatch.\n"
              "This installs one APK only; split APKs and separate OBB/data files are not included.\n\n"
              "Continue?",
            icon="warning", parent=self.root,
        ):
            return
        self.operation_kind = "install"
        self._set_busy(True)
        self.stop_transfer.clear()
        self._finish_progress_log()
        self.status.set(f"Preparing APK installation: {apk.name}")
        self._write_log("=" * 56)
        self._write_log(f"APK INSTALL START — Source: {source_label}; device: {self.serial}")
        self._write_log(f"APK source path: {apk.path}")
        self._write_log("Install options: -r (replace/update, retain app data), -t (allow test APKs)")
        self.pool.submit(self._install_worker, side, apk)

    def _install_worker(self, source: str, apk: FileEntry) -> None:
        start = time.monotonic()
        error: str | None = None
        cancelled = False
        try:
            if source == "quest":
                # adb install needs a PC-local APK. Downloading to an isolated,
                # temporary path is more compatible than shell pm install from
                # shared Android storage (which can be inaccessible to pm).
                with tempfile.TemporaryDirectory(prefix="quest3_apk_install_") as staging_dir:
                    local_apk = Path(staging_dir) / "quest_selected.apk"
                    self.events.put(("install_stage", "Downloading selected Quest APK to temporary PC storage…"))
                    self.events.put(("log", f"Temporary APK staging: {local_apk}"))
                    self._run_transfer_command(
                        [str(self.adb), "-s", self.serial, "pull", "-p", apk.path, str(local_apk)],
                        1, 1, f"Downloading {apk.name}", error_label="ADB APK staging download",
                    )
                    if self.stop_transfer.is_set():
                        raise RuntimeError("Installation cancelled after APK download.")
                    self._install_local_apk(local_apk, apk.name)
                self.events.put(("log", "Temporary APK staging folder cleaned up."))
            else:
                self._install_local_apk(Path(apk.path), apk.name)
        except Exception as exc:
            if self.stop_transfer.is_set():
                cancelled = True
            else:
                error = str(exc)
        finally:
            self.events.put(("install_done", source, apk.path, time.monotonic() - start, cancelled, error))

    def _install_local_apk(self, local_apk: Path, display_name: str) -> None:
        if self.stop_transfer.is_set():
            raise RuntimeError("Installation cancelled before ADB install.")
        self.events.put(("install_stage", f"Checking APK: {display_name}"))
        if not local_apk.is_file():
            raise RuntimeError(f"APK file not found: {local_apk}")
        size = local_apk.stat().st_size
        self.events.put(("log", f"APK size: {size:,} bytes ({size / (1024 * 1024):.2f} MiB)"))
        if not zipfile.is_zipfile(local_apk):
            raise RuntimeError("Selected .apk is not a valid ZIP/APK container (or the download is incomplete).")
        self.events.put(("log", "APK archive format: valid ZIP container (package compatibility checked by Android)."))
        if self.stop_transfer.is_set():
            raise RuntimeError("Installation cancelled before ADB install.")
        self.events.put(("install_stage", f"Installing on Quest: {display_name}…"))
        self.events.put(("log", f"Executing: adb -s {self.serial} install -r -t <selected APK>"))
        # -r updates matching packages without clearing app data; -t permits
        # Android packages flagged android:testOnly (useful for development).
        output = self._run_transfer_command(
            [str(self.adb), "-s", self.serial, "install", "-r", "-t", str(local_apk)],
            1, 1, f"Installing {display_name}", error_label="ADB APK installation",
        )
        if self.stop_transfer.is_set():
            raise RuntimeError("Installation cancelled; the final Android package state is unknown.")
        if not any(re.match(r"^Success\b", line.strip()) for line in output):
            raise RuntimeError("ADB exited without an explicit installation 'Success' response. "
                               "Review the ADB output in the log before assuming it installed.")
        self.events.put(("log", "Android package manager confirmed: Success"))

    def start_transfer(self, source: str) -> None:
        if self.busy:
            messagebox.showinfo("Transfer already running", "Finish or cancel the current transfer first.")
            return
        if not self.serial:
            messagebox.showerror("Quest not connected", "Click 'Reconnect Quest' and confirm ADB authorization first.")
            return
        target = "quest" if source == "local" else "local"
        sources = self._selected_entries(source)
        if not sources or any(x.kind in ("virtual", "link") for x in sources):
            messagebox.showwarning("Select source files", f"Select one or more ordinary files or folders on the {source} side.")
            return
        dest_selection = [self.entries[target][i] for i in self.tree[target].selection()
                          if i in self.entries[target]]
        if len(dest_selection) != 1:
            messagebox.showwarning("Select one destination", f"Select exactly one folder or file on the {target} side.\n"
                                   "A selected file means 'use its parent folder'.")
            return
        try:
            destination = target_folder(dest_selection[0], target)
        except ValueError as exc:
            messagebox.showwarning("Invalid destination", str(exc))
            return
        # An initially selected top-level root can contain hundreds of GB.
        # Require a second, explicit confirmation before copying all of it.
        massive = []
        for entry in sources:
            if source == "quest" and entry.path in ("/sdcard", "/storage/emulated/0"):
                massive.append(entry.path)
            if source == "local" and (Path(entry.path) == Path.home() or
                                      Path(entry.path) == Path(entry.path).parent):
                massive.append(entry.path)
        if massive and not messagebox.askyesno(
            "Copy entire storage?",
            "You selected a top-level home, drive, or Quest storage folder:\n\n" +
            "\n".join(massive) +
            "\n\nThis could contain hundreds of gigabytes and take a long time. Continue?",
            icon="warning",
        ):
            return
        direction = "PC → Quest" if source == "local" else "Quest → PC"
        listed = "\n".join("• " + e.name for e in sources[:8])
        if len(sources) > 8:
            listed += f"\n… and {len(sources) - 8} more"
        matching_folders = [entry.name for entry in sources
                            if transfer_destination_for_item(source, entry, destination)[1]]
        merge_note = (
            "Matching folder detected: contents will merge into the selected folder "
            "(no duplicate nested folder).\n\n"
        ) if matching_folders else ""
        protected_note = (
            "Note: The Quest destination is under Android/data or Android/obb. "
            "The headset may prohibit some writes even when folders are visible.\n\n"
        ) if target == "quest" and (
            "/Android/data/" in destination or "/Android/obb/" in destination
        ) else ""
        safe_mode_note = (
            "Protected Quest folder: the app will create directory structure first, "
            "then transfer each file individually to avoid ADB recursive-copy errors. "
            "The progress percentage will cover the entire selected folder.\n\n"
        ) if target == "quest" and is_protected_quest_destination(destination) and any(
            entry.kind == "dir" for entry in sources
        ) else ""
        warning = (f"{direction}\n\nSource items:\n{listed}\n\nDestination folder:\n{destination}\n\n"
                   + merge_note + protected_note + safe_mode_note +
                   "Folders will be copied recursively. If destination files already exist, ADB may overwrite "
                   "them; existing folders may be merged.\n\nProceed?")
        if not messagebox.askyesno("Confirm file transfer", warning, icon="question"):
            return
        self.operation_kind = "transfer"
        self._set_busy(True)
        self.stop_transfer.clear()
        self.status.set(f"Starting {direction} ({len(sources)} items)…")
        self._write_log(f"START {direction} → {destination} ({len(sources)} top-level items)")
        self.pool.submit(self._transfer_worker, source, sources, destination)

    def _prepare_remote_upload(self, local_folder: str, remote_folder: str) -> str | None:
        """Try a non-destructive mkdir workaround; preserve original push if denied.

        Returns an explanatory error on mkdir failure. Pre-creation is helpful
        for ADB's secure_mkdirs regression but cannot override Android access
        controls, so the caller still tries its normal ADB push.
        """
        directories = remote_upload_directories(local_folder, remote_folder)
        if self.stop_transfer.is_set():
            raise RuntimeError("Transfer cancelled before destination preparation.")
        self.events.put(("log", f"PREPARE Quest folder: {remote_folder}"))
        self.events.put(("log", f"Pre-creating/verifying {len(directories)} Quest director"
                                    + ("y" if len(directories) == 1 else "ies")
                                    + " via adb shell mkdir -p"))
        try:
            for batch in remote_mkdir_batches(directories):
                if self.stop_transfer.is_set():
                    raise RuntimeError("Transfer cancelled during destination preparation.")
                self._adb_run(["shell", "-T", batch], timeout=60)
            self.events.put(("log", f"Quest directory preparation OK ({len(directories)} folders)."))
            return None
        except Exception as exc:
            if self.stop_transfer.is_set():
                raise
            error = str(exc)
            self.events.put(("log", "Quest mkdir pre-creation rejected: " + error))
            self.events.put(("log", "Trying standard ADB push anyway; some devices permit push "
                                    "even if explicit mkdir is blocked."))
            return error

    def _upload_protected_folder_files(self, entry: FileEntry, quest_folder: str,
                                       item_n: int, item_total: int,
                                       mkdir_error: str | None) -> None:
        """Transfer a complete selected directory as individual ADB pushes.

        On some Android builds a recursive `adb push <folder>` fails with
        secure_mkdirs even when the target directory is successfully created,
        yet `adb push <file>` succeeds. This avoids the recursive ADB path.
        The top-level progress remains based on bytes across the WHOLE folder.
        """
        planned = enumerate_upload_files(entry.path, quest_folder)
        count = len(planned)
        total_bytes = sum(size for _, _, size in planned)
        self.events.put(("log", f"SAFE UPLOAD MODE: {entry.name} -> {quest_folder}"))
        self.events.put(("log", f"Found {count:,} ordinary files ({total_bytes / (1024 ** 2):,.2f} MiB). "
                                "Using an individual ADB push for each file."))
        if not planned:
            if mkdir_error:
                raise RuntimeError("Folder has no ordinary files and its Quest directory "
                                   "could not be created: " + mkdir_error)
            self.events.put(("transfer_progress", item_n, item_total, entry.name,
                             100, "(empty folder; directories prepared)", 100))
            return

        completed_bytes = 0
        completed_files = 0
        last_overall = -1
        last_report_time = 0.0
        for index, (local_file, remote_file, file_size) in enumerate(planned, 1):
            if self.stop_transfer.is_set():
                raise RuntimeError(f"Cancelled after {completed_files}/{count} files.")
            relative_name = str(Path(local_file).relative_to(Path(entry.path))).replace(os.sep, "/")
            # Ordinarily pre-created in _prepare_remote_upload. If that failed,
            # retry mkdir only for this file's parent before pushing it.
            if mkdir_error:
                parent = posixpath.dirname(remote_file)
                try:
                    self._adb_run(["shell", "-T", "mkdir -p -- " + shlex.quote(parent)], timeout=30)
                except Exception:
                    pass  # Actual push reports authoritative success or failure.
            # Keep the log readable for large texture packs without losing
            # current-file detail from the live progress entry.
            if index <= 3 or index == count or index % 25 == 0:
                self.events.put(("log", f"Uploading file {index:,}/{count:,}: {relative_name}"))

            def on_file_progress(file_pct: int, _adb_name: str, _nested_pct: int | None) -> None:
                nonlocal last_overall, last_report_time
                partial = file_size * max(0, min(100, file_pct)) / 100.0
                if total_bytes:
                    overall = min(100, int((completed_bytes + partial) * 100 / total_bytes))
                else:
                    overall = min(100, int((completed_files + file_pct / 100.0) * 100 / count))
                now = time.monotonic()
                if (overall != last_overall and
                    (now - last_report_time >= 0.14 or overall == 100 or last_report_time == 0)):
                    last_overall = overall
                    last_report_time = now
                    self.events.put(("transfer_progress", item_n, item_total,
                                     entry.name, overall, relative_name, file_pct))

            remote_parent = posixpath.dirname(remote_file)
            remote_arg = remote_parent.rstrip("/") + "/"
            command = [str(self.adb), "-s", self.serial, "push", "-p", local_file, remote_arg]
            try:
                self._run_transfer_command(command, item_n, item_total, entry.name,
                                           progress_hook=on_file_progress,
                                           suppress_success_output=True)
            except RuntimeError as exc:
                # A failed push may leave the current file partially present.
                hint = quest_storage_permission_hint(str(exc), "local", quest_folder)
                raise RuntimeError(f"Individual upload failed on file {index:,}/{count:,}: "
                                   f"{relative_name}\n{exc}{hint}") from exc

            completed_files += 1
            completed_bytes += file_size
            new_overall = min(100, int(completed_bytes * 100 / total_bytes)) if total_bytes else int(
                completed_files * 100 / count)
            if (new_overall != last_overall or completed_files == count):
                last_overall = new_overall
                last_report_time = time.monotonic()
                self.events.put(("transfer_progress", item_n, item_total,
                                 entry.name, new_overall, relative_name, 100))
            if index <= 3 or index == count or index % 25 == 0:
                self.events.put(("log", f"Uploaded {completed_files:,}/{count:,} files; "
                                        f"{completed_bytes / (1024 ** 2):,.2f} / "
                                        f"{total_bytes / (1024 ** 2):,.2f} MiB"))
        self.events.put(("log", f"SAFE UPLOAD COMPLETE: {completed_files:,}/{count:,} files. "
                                f"{completed_bytes / (1024 ** 2):,.2f} MiB transferred."))

    def _transfer_worker(self, source: str, entries: list[FileEntry], destination: str) -> None:
        done = 0
        error = None
        cancelled = False
        try:
            for n, entry in enumerate(entries, 1):
                if self.stop_transfer.is_set():
                    cancelled = True
                    break
                self.events.put(("transfer_step", n, len(entries), entry.name))
                adb_destination, merging = transfer_destination_for_item(source, entry, destination)
                if merging:
                    self.events.put(("log", f"MERGE existing folder: {entry.name} -> {destination}"))
                    self.events.put(("log", "Using the parent as ADB's transfer destination "
                                              "to avoid a duplicated directory level."))
                mkdir_error: str | None = None
                if source == "local":
                    if not Path(entry.path).exists():
                        raise RuntimeError(f"Source no longer exists: {entry.path}")
                    if entry.kind == "dir":
                        # For a non-merged upload create <destination>/<folder>;
                        # for merged uploads adb_destination is the parent, so
                        # the same calculation still resolves the correct folder.
                        quest_folder = posixpath.join(adb_destination, entry.name)
                        mkdir_error = self._prepare_remote_upload(entry.path, quest_folder)
                    if self.stop_transfer.is_set():
                        raise RuntimeError("Transfer cancelled before ADB push.")
                    if entry.kind == "dir" and is_protected_quest_destination(quest_folder):
                        self._upload_protected_folder_files(entry, quest_folder, n, len(entries), mkdir_error)
                        done += 1
                        self.events.put(("log", f"Completed {done}/{len(entries)}: {entry.name}"))
                        continue
                    # ADB copies a directory *under* its destination directory.
                    # -p requests progress on older ADB versions too.
                    remote_arg = (adb_destination if adb_destination == "/"
                                  else adb_destination.rstrip("/") + "/")
                    command = [str(self.adb), "-s", self.serial, "push", "-p", entry.path, remote_arg]
                else:
                    if not Path(adb_destination).is_dir():
                        raise RuntimeError(f"PC destination no longer exists: {adb_destination}")
                    command = [str(self.adb), "-s", self.serial, "pull", "-p", entry.path, adb_destination]
                self.events.put(("log", f"ADB destination directory: {adb_destination}"))
                try:
                    self._run_transfer_command(command, n, len(entries), entry.name)
                except RuntimeError as exc:
                    hint = quest_storage_permission_hint(str(exc), source, destination)
                    diagnostic = ("\n\nEXPLICIT MKDIR RESULT: " + mkdir_error
                                  if mkdir_error else "")
                    if hint or diagnostic:
                        raise RuntimeError(str(exc) + diagnostic + hint) from exc
                    raise
                done += 1
                self.events.put(("log", f"Completed {done}/{len(entries)}: {entry.name}"))
            if self.stop_transfer.is_set():
                cancelled = True
        except Exception as exc:
            if self.stop_transfer.is_set():
                cancelled = True
            else:
                error = str(exc)
        finally:
            self.events.put(("transfer_done", source, destination, done, len(entries), cancelled, error))

    def _run_transfer_command(self, command: list[str], item_n: int = 1,
                              item_total: int = 1, item_name: str = "",
                              error_label: str = "ADB transfer",
                              progress_hook: Callable[[int, str, int | None], None] | None = None,
                              suppress_success_output: bool = False) -> list[str]:
        """Read ADB output promptly even when progress ends in CR, not LF.

        Windows ADB sends live progress on stderr using carriage-return
        redraws. Text-mode line iteration waits for LF or process exit and
        therefore cannot reliably show transfer percentages during a copy.
        """
        with self.process_lock:
            if self.stop_transfer.is_set():
                raise RuntimeError("Transfer cancelled before starting the next item.")
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    bufsize=0, **self._popen_options())
            self.active_process = proc
        tail: list[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        latest_percent: int | None = None
        latest_filename = ""
        latest_file_percent: int | None = None
        last_posted = 0.0

        def publish(line: str) -> None:
            nonlocal latest_percent, latest_filename, latest_file_percent, last_posted, tail
            line = ANSI_ESCAPE_PATTERN.sub("", line).strip()
            if not line:
                return
            parsed = parse_adb_progress(line)
            if parsed is not None:
                percentage, filename, file_percentage = parsed
                now = time.monotonic()
                changed = (percentage != latest_percent or filename != latest_filename or
                           file_percentage != latest_file_percent)
                latest_percent, latest_filename, latest_file_percent = parsed
                # Throttle GUI work; ensure 100% is always delivered.
                if changed and (now - last_posted >= 0.10 or percentage == 100 or last_posted == 0.0):
                    if progress_hook is not None:
                        progress_hook(percentage, filename, file_percentage)
                    else:
                        self.events.put(("transfer_progress", item_n, item_total, item_name,
                                         percentage, filename, file_percentage))
                    last_posted = now
            else:
                tail.append(line)
                del tail[:-8]
                if (not suppress_success_output or
                    "error" in line.lower() or "failed" in line.lower()):
                    self.events.put(("log", line))

        def consume(text: str) -> None:
            nonlocal pending
            for ch in text:
                if ch in "\r\n":
                    if pending:
                        publish(pending)
                        pending = ""
                else:
                    pending += ch
                    if len(pending) > 32768:
                        publish(pending)
                        pending = ""

        try:
            assert proc.stdout is not None
            while True:
                # A one-byte read is intentional: with pipes some Python/OS
                # combinations wait to fill larger reads, hiding ADB's live
                # CR updates until process exit. ADB emits very little text.
                chunk = proc.stdout.read(1)
                if not chunk:
                    break
                consume(decoder.decode(chunk))
            consume(decoder.decode(b"", final=True))
            if pending:
                publish(pending)
            code = proc.wait()
            if code:
                raise RuntimeError(f"{error_label} failed (exit code {code}).\n" + "\n".join(tail[-8:]))
            # Some versions don't emit a final 100%, especially for tiny files.
            if latest_percent is not None and latest_percent != 100:
                if progress_hook is not None:
                    progress_hook(100, latest_filename, 100 if latest_file_percent is not None else None)
                else:
                    self.events.put(("transfer_progress", item_n, item_total, item_name,
                                     100, latest_filename, 100 if latest_file_percent is not None else None))
            return tail
        finally:
            with self.process_lock:
                if self.active_process is proc:
                    self.active_process = None
            if proc.stdout:
                proc.stdout.close()

    def cancel_transfer(self) -> None:
        if not self.busy:
            return
        self.stop_transfer.set()
        self.status.set("Cancelling APK installation…" if self.operation_kind == "install"
                        else "Cancelling operation…")
        with self.process_lock:
            process = self.active_process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        self._write_log("Cancellation requested. Installation may be incomplete; check the Quest app state."
                        if self.operation_kind == "install"
                        else "Cancellation requested; partially copied files may remain on the destination.")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.upload_btn.configure(state="disabled" if busy else "normal")
        self.download_btn.configure(state="disabled" if busy else "normal")
        self.install_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if busy:
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="indeterminate", value=0)
            self.operation_kind = None

    def delete_selected(self, side: str) -> None:
        if self.busy:
            messagebox.showinfo("Transfer in progress", "Finish or cancel the transfer before deleting files.")
            return
        if side == "quest" and not self.serial:
            messagebox.showerror("Quest not connected", "Reconnect the headset first.")
            return
        selected = self._selected_entries(side)
        if not selected:
            messagebox.showwarning("No selection", f"Select files or folders on the {side} side first.")
            return
        forbidden = [e.path for e in selected if not safe_delete_target(e, side)]
        if forbidden:
            messagebox.showerror("Protected location",
                                 "Deletion is blocked for storage roots, protected system locations and symbolic links:\n\n"
                                 + "\n".join(forbidden[:5]) +
                                 ("\n…" if len(forbidden) > 5 else ""))
            return
        preview = "\n".join("• " + e.path for e in selected[:7])
        if len(selected) > 7:
            preview += f"\n… and {len(selected) - 7} more"
        if not messagebox.askyesno("ARE YOU SURE? — Permanent deletion",
                                   f"Permanently DELETE {len(selected)} selected item(s) from "
                                   f"{'your PC' if side == 'local' else 'your Quest'}?\n\n"
                                   f"{preview}\n\nAny selected folders and ALL their contents will be deleted recursively. "
                                   "This does NOT use the Recycle Bin and cannot be undone.", icon="warning"):
            return
        self.operation_kind = "delete"
        self._set_busy(True)
        self.stop_transfer.clear()
        self._write_log(f"DELETE requested on {side}: {len(selected)} items")
        self.pool.submit(self._delete_worker, side, selected)

    def _delete_worker(self, side: str, entries: list[FileEntry]) -> None:
        done = 0
        errors = []
        parent_paths = set()
        for entry in entries:
            if self.stop_transfer.is_set():
                break
            parent_paths.add(os.path.dirname(entry.path) if side == "local" else posixpath.dirname(entry.path))
            try:
                if side == "local":
                    if entry.kind == "dir":
                        shutil.rmtree(entry.path)
                    else:
                        Path(entry.path).unlink()
                else:
                    # The entire quoted path is a single shell argument, never user code.
                    operation = "rm -r -- " if entry.kind == "dir" else "rm -- "
                    self._adb_run(["shell", "-T", operation + shlex.quote(entry.path)], timeout=180)
                done += 1
                self.events.put(("log", f"Deleted {side}: {entry.path}"))
            except Exception as exc:
                errors.append(f"{entry.path}: {exc}")
                self.events.put(("log", "DELETE ERROR: " + errors[-1]))
        self.events.put(("delete_done", side, done, len(entries), parent_paths, errors))

    def _write_progress_log(self, text: str) -> None:
        """Update the active percentage line, rather than adding 100 lines."""
        self.log.configure(state="normal")
        ranges = self.log.tag_ranges("active_adb_progress")
        if ranges:
            position = str(ranges[0])
            self.log.delete(ranges[0], ranges[1])
            self.log.insert(position, text + "\n", ("active_adb_progress",))
        else:
            self.log.insert("end", text + "\n", ("active_adb_progress",))
        self.log.see("end")
        self.log.configure(state="disabled")

    def _finish_progress_log(self) -> None:
        """Keep the last percentage in the log, but stop editing that line."""
        self.log.tag_remove("active_adb_progress", "1.0", "end")

    def _write_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        # Limit retained log text, to avoid unbounded memory for huge folders.
        if int(self.log.index("end-1c").split(".")[0]) > 1500:
            self.log.delete("1.0", "301.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_events(self) -> None:
        if self._closed:
            return
        count = 0
        while count < 90:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            count += 1
            kind = event[0]
            if kind == "log":
                self._write_log(event[1])
            elif kind == "listed":
                _, side, node, path, entries, err = event
                self._populate_dir(side, node, path, entries, err)
            elif kind == "connected":
                _, serial, info, err = event
                if err:
                    self.serial = None
                    self.device_label.set("Quest: disconnected / not authorized")
                    self.status.set("Quest not available")
                    self._write_log("ADB CONNECTION: " + err)
                else:
                    self.serial = serial
                    self.device_label.set("Quest connected: " + serial)
                    self._write_log("ADB connected: " + info)
                    for node in self.root_nodes["quest"]:
                        if node in self.entries["quest"]:
                            self._load_dir("quest", node, force=True)
            elif kind == "install_stage":
                self._finish_progress_log()
                self.progress.stop()
                self.progress.configure(mode="indeterminate", value=0)
                self.progress.start(12)
                self.status.set(event[1])
                self._write_log("INSTALL STAGE: " + event[1])
            elif kind == "transfer_step":
                _, n, total, name = event
                self._finish_progress_log()
                self.progress.stop()
                self.progress.configure(mode="indeterminate", value=0)
                self.progress.start(12)
                self.status.set(f"Transferring item {n}/{total}: {name}")
                self._write_log(f"Copying item {n}/{total}: {name}")
            elif kind == "transfer_progress":
                _, n, total, item_name, percentage, filename, file_percentage = event
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=100, value=percentage)
                # Percentages apply to the current top-level item / ADB command.
                self.status.set(f"Item {n}/{total}: {percentage}% — {item_name}")
                current = filename if filename else item_name
                # Avoid an extremely long filename making the log hard to scan.
                if len(current) > 120:
                    current = "…" + current[-119:]
                label = f"  [{n}/{total}] {percentage:3d}% — {current}"
                if file_percentage is not None:
                    label += f"  (current file: {file_percentage}%)"
                self._write_progress_log(label)
            elif kind == "transfer_done":
                self._finish_progress_log()
                _, source, destination, done, total, cancelled, err = event
                self._set_busy(False)
                if err:
                    self.status.set(f"Transfer stopped after {done}/{total} items — error")
                    self._write_log("TRANSFER ERROR: " + err)
                    messagebox.showerror("Transfer failed", f"{done} of {total} top-level items completed.\n\n{err}\n\n"
                                         "Any partially transferred files may remain at the destination.")
                elif cancelled:
                    self.status.set(f"Transfer cancelled after {done}/{total} items")
                    self._write_log("Transfer cancelled. Partial files may remain.")
                else:
                    self.status.set(f"Transfer complete: {done}/{total} items")
                    self._write_log(f"SUCCESS: {done}/{total} top-level items transferred")
                    messagebox.showinfo("Transfer complete", f"Copied {done} top-level item(s) successfully.")
                self.refresh_path("quest" if source == "local" else "local", destination)
            elif kind == "install_done":
                _, source, path, elapsed, cancelled, err = event
                self._finish_progress_log()
                self._set_busy(False)
                if cancelled:
                    self.status.set("APK installation cancelled — check app state on Quest")
                    self._write_log(f"INSTALL CANCELLED after {elapsed:.1f} seconds: {path}")
                    messagebox.showwarning("Installation cancelled",
                                           "Installation was cancelled. If Android had already begun installing, "
                                           "the final app state may need checking on the Quest.")
                elif err:
                    self.status.set("APK installation failed")
                    self._write_log(f"INSTALL FAILED after {elapsed:.1f} seconds: {err}")
                    messagebox.showerror("APK installation failed", f"APK: {path}\n\n{err}")
                else:
                    self.status.set(f"APK installed successfully in {elapsed:.1f}s")
                    self._write_log(f"INSTALL SUCCESS: {path} ({elapsed:.1f} seconds)")
                    messagebox.showinfo("APK installed", f"Successfully installed on your Quest 3.\n\n"
                                        f"Source: {'PC' if source == 'local' else 'Quest'}\n"
                                        f"APK: {path}\nElapsed: {elapsed:.1f} seconds")
                self._write_log("=" * 56)
            elif kind == "delete_done":
                _, side, done, total, paths, errors = event
                self._set_busy(False)
                self.status.set(f"Deleted {done}/{total} {side} items" + (" (some failed)" if errors else ""))
                for path in paths:
                    self.refresh_path(side, path)
                if errors:
                    messagebox.showerror("Some deletions failed", "\n\n".join(errors[:6]))
                else:
                    self._write_log(f"SUCCESS: {done}/{total} items deleted from {side}")
        self.root.after(70, self._drain_events)

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("Operation in progress",
                                       "An operation is still running. Stop the operation and close?\n\n"
                                       "An APK installation may be left in an unknown state; "
                                       "partially copied files may remain on the destination."):
                return
            self.cancel_transfer()
        self._closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    FileManager(root)
    root.mainloop()


if __name__ == "__main__":
    main()
