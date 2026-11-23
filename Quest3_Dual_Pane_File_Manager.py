#!/usr/bin/env python3
"""Quest 3 Dual-Pane File Manager — Windows / Python 3.9+, standard library only.
Author - Michael Forde
Ver - 1.1

Local PC (left) <-> Meta Quest using Android Debug Bridge (right).

- Expandable, lazy-loaded directory trees (Ctrl/Shift multi-selection).
- Recursive ADB push/pull, preserving the name and hierarchy of each folder.
- Target = selected destination folder, or parent of a selected destination file.
- Deliberately separate directional transfer and deletion buttons.
- Confirmation for all deletions and transfers that may overwrite existing files.
- Runs long operations off the Tkinter UI thread and can cancel a transfer.
- Shows live ADB transfer percentages in the log and progress bar.

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
        self._write_log("Select a source on one side and a destination on the other. Ctrl/Shift selects multiple items.")

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
        ttk.Label(center, text="Select source(s)\nand a destination\non opposite sides", justify="center").pack(pady=(45, 0))

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
                self.tree["quest"].see(node)
                self.tree["quest"].item(node, open=True)
                self._load_dir("quest", node)
                return
        node = self._add_entry("quest", "", FileEntry(path, "dir", f"Quest — {path}"))
        self.root_nodes["quest"].append(node)
        self.tree["quest"].item(node, open=True)
        self.tree["quest"].selection_set(node)
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
        warning = (f"{direction}\n\nSource items:\n{listed}\n\nDestination folder:\n{destination}\n\n"
                   "Folders will be copied recursively. If destination files already exist, ADB may overwrite "
                   "them; existing folders may be merged.\n\nProceed?")
        if not messagebox.askyesno("Confirm file transfer", warning, icon="question"):
            return
        self._set_busy(True)
        self.stop_transfer.clear()
        self.status.set(f"Starting {direction} ({len(sources)} items)…")
        self._write_log(f"START {direction} → {destination} ({len(sources)} top-level items)")
        self.pool.submit(self._transfer_worker, source, sources, destination)

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
                if source == "local":
                    if not Path(entry.path).exists():
                        raise RuntimeError(f"Source no longer exists: {entry.path}")
                    # -p requests progress on older ADB versions; modern ADB
                    # reports it by default and accepts -p for compatibility.
                    command = [str(self.adb), "-s", self.serial, "push", "-p", entry.path, destination + "/"]
                else:
                    if not Path(destination).is_dir():
                        raise RuntimeError(f"PC destination no longer exists: {destination}")
                    command = [str(self.adb), "-s", self.serial, "pull", "-p", entry.path, destination]
                self._run_transfer_command(command, n, len(entries), entry.name)
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
                              item_total: int = 1, item_name: str = "") -> None:
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
                    self.events.put(("transfer_progress", item_n, item_total, item_name,
                                     percentage, filename, file_percentage))
                    last_posted = now
            else:
                tail.append(line)
                del tail[:-8]
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
                raise RuntimeError("ADB transfer failed (exit code " + str(code) + ").\n" + "\n".join(tail[-5:]))
            # Some versions don't emit a final 100%, especially for tiny files.
            if latest_percent is not None and latest_percent != 100:
                self.events.put(("transfer_progress", item_n, item_total, item_name,
                                 100, latest_filename, 100 if latest_file_percent is not None else None))
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
        self.status.set("Cancelling transfer…")
        with self.process_lock:
            process = self.active_process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        self._write_log("Cancellation requested; partially copied files may remain on the destination.")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.upload_btn.configure(state="disabled" if busy else "normal")
        self.download_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if busy:
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="indeterminate", value=0)

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
                                       "Partially copied files may remain on the destination."):
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
