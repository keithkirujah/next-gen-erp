# Build NextGen ERP as a Windows desktop EXE

The application is a native **Windows desktop app** (PySide6). A ready-to-run `.exe`
must be built **on a Windows 10/11 PC** — this cannot be cross-compiled from Linux.

## Quick build (recommended)

1. Install **Python 3.10+** from https://www.python.org/downloads/windows/  
   (check **Add Python to PATH**).
2. Extract this project folder.
3. Double-click:

   ```
   packaging\build_windows.bat
   ```

4. When it finishes, run:

   ```
   dist\NextGenERP\NextGenERP.exe
   ```

5. To give the app to someone else, copy the **entire** `dist\NextGenERP\` folder
   (not only the `.exe`). Qt libraries live beside the executable.

## Optional installer

If [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed, the build script
also compiles `packaging\installer.iss` into a setup program.

## Without building an EXE

You can still run from source on Windows:

```
START_ERP.bat
```

That installs dependencies on first launch and starts the app.

## Notes

- First PyInstaller build can take 5–15 minutes and needs internet for packages.
- Antivirus may scan the new folder the first time — allow it.
- Data (SQLite workspaces) is stored under the user’s AppData / app data folder,
  not inside `dist\NextGenERP\`, so updates won’t wipe business data.
