QUEST 3 DUAL-PANE FILE MANAGER
==============================
Author: Michael Forde
Ver: 1.1

Files in this package
---------------------
Quest3_Dual_Pane_File_Manager.py     Main app (Python 3.9+, Tkinter)
Start_Quest3_Dual_Pane_File_Manager.cmd   Windows launcher (auto setup)
Setup_Quest3_Dual_Pane_File_Manager.ps1   PowerShell setup helper (required)
README_Quest3_Dual_Pane_File_Manager.txt  This guide

AUTOMATIC INSTALLATION / FIRST LAUNCH (WINDOWS 10/11)
----------------------------------------------------
1. Extract ALL FOUR files from this ZIP into ANY local folder. You can use
   your existing F:\VR-Q3-autoexplorer\Quest3-Storage-Explorer-AutoSetup\
   folder, or a completely new folder.

2. Double-click Start_Quest3_Dual_Pane_File_Manager.cmd.
   The launcher runs its adjacent Setup_Quest3_Dual_Pane_File_Manager.ps1.

3. The first launch automatically downloads Google's official Windows
   Android SDK Platform-Tools into .\platform-tools (if missing or broken).
   This includes adb.exe AND its required companion files/DLLs.

4. The setup looks for a compatible Python (3.9+ with Tkinter), first in
   .\python-runtime, then .\venv, then an installed Windows Python.
   If none is usable, it automatically downloads and verifies the official
   Python 3.13.14 64-bit installer and installs Python + Tkinter + pip into
   .\python-runtime beside this launcher. No admin rights or system PATH
   changes are required for that installation.

5. The application launches. Later runs reuse ADB/Python and skip downloads.
   All other application dependencies are from the Python standard library;
   no pip install step is necessary.

6. Connect the Quest 3 by USB, unlock it, and approve USB debugging when
   prompted. Quest Developer Mode / USB debugging must already be enabled.
   Windows may require appropriate USB drivers on some PCs; ADB installation
   does not enable Developer Mode or bypass device authorisation.

The first setup requires internet access. The launcher always uses the folder
containing the CMD, rather than the current working directory. Leave the CMD,
PowerShell helper and Python script together.

LAUNCHER FIX (23 November 2026)
-------------------------------
Fixed "GetFullPath: Illegal characters in path" during first launch. The CMD
no longer passes its directory (ending in a backslash) as a quoted PowerShell
argument. The PowerShell setup reads its own folder using $PSScriptRoot.
Replace BOTH the CMD and PS1 files from the updated ZIP to apply this fix.
Any correctly installed platform-tools/python-runtime folders are preserved.

DOWNLOAD SOURCES:
  ADB: https://dl.google.com/android/repository/platform-tools-latest-windows.zip
  Python (only if needed):
  https://www.python.org/ftp/python/3.13.14/python-3.13.14-amd64.exe

HOW TO USE
----------
LEFT:  PC folders and files (start with Home and This PC/drives).
RIGHT: Quest folders and files (start at /sdcard).

Click the triangle beside a directory to expand/collapse it. Hold Ctrl or
Shift to select multiple files/folders on the SOURCE side.

UPLOAD (PC TO QUEST)
  Select source items on the LEFT.
  Select exactly one destination folder OR an existing file on the RIGHT.
  Click "TRANSFER ->". If a file is selected as the destination, the app
  copies into its PARENT folder (not into the file).

DOWNLOAD (QUEST TO PC)
  Select source items on the RIGHT.
  Select exactly one destination folder OR existing file on the LEFT.
  Click "<- TRANSFER". A selected file means use that file's PARENT folder.

FOLDERS
  Folders are copied recursively with their existing subdirectory names and
  files. You can copy a mix of files and folders at once. Files/folders with
  matching destination names may be overwritten or merged; a confirmation
  is displayed before transfer.

DELETE
  Use the redirection-safe DELETE button under the side you want to alter.
  Every delete requires an "ARE YOU SURE?" confirmation. Recursive folder
  deletion is permanent: it does NOT use the Windows Recycle Bin.
  Quest deletion is restricted to descendants of /sdcard or
  /storage/emulated/0 to protect device system locations. These roots
  themselves cannot be deleted. Local home and drive roots are protected.

MORE NAVIGATION
  "Open PC Folder..." adds another local folder as a browsable tree root.
  "Go to Quest Path..." adds a custom Android folder path, e.g.
       /sdcard/Android/data
       /sdcard/Android/obb
       /data/local/tmp
  "Refresh" reloads the selected directory.
  "Reconnect Quest" retries ADB detection.

LONG OPERATIONS
  File transfers and recursive delete run off the GUI thread. The bottom
  shows status, the current top-level item and ADB output. You can cancel
  an operation; partial files can remain at the destination after canceling.

LIMITS & SAFETY
---------------
* The Quest must be connected and authorized through ADB. Some Android
  directories may be inaccessible because of filesystem permissions.
* This app intentionally does NOT follow/copy/delete symbolic links.
* ADB push/pull can replace files of the same name; read the confirmation.
* Remote space and PC free-space are not pre-checked.
* The progress bar means "busy" (not an exact byte percentage).
* Tested here with headless Tkinter, shell-path parsing, and a simulated
  ADB backend for recursive upload, download, and both delete operations.
  Actual USB/Quest 3 transfer behavior still needs a device test.

ADB reference:
https://developer.android.com/tools/adb
