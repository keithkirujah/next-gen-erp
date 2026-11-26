"""NextGen ERP offline update package validation and staged installation UI."""
import json, hashlib, os, re, subprocess, tempfile, zipfile
from pathlib import Path
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog, QListWidget, QMessageBox, QPlainTextEdit, QApplication
from PySide6.QtCore import QTimer

CURRENT_VERSION = "2.0.0-dev"
MAX_PACKAGE_BYTES = 250 * 1024 * 1024\nMAX_UNPACKED_BYTES = 500 * 1024 * 1024
SAFE_ROOTS = {"assets", "profiles", "ui", "modules"}
SAFE_EXTS = {".py", ".json", ".svg", ".png", ".ico", ".bat", ".md"}
PROTECTED = {"nextgen_erp.db", "settings.json"}

def _safe_rel(path):
    normalized=str(path).replace("\\","/")
    p=Path(normalized)
    return not p.is_absolute() and ".." not in p.parts and not p.drive and not normalized.startswith("/") and bool(normalized.strip())

def inspect_package(package):
    package=Path(package)
    if package.stat().st_size>MAX_PACKAGE_BYTES: raise ValueError("Package exceeds 250 MB limit.")
    with zipfile.ZipFile(package) as z:
        names=z.namelist()
        if "update_manifest.json" not in names: raise ValueError("Missing update_manifest.json.")
        manifest=json.loads(z.read("update_manifest.json").decode("utf-8"))
        if manifest.get("package_type")!="full_update": raise ValueError("Unsupported package type. Only full application updates are supported.")
        version=str(manifest.get("version","")).strip()
        if not re.fullmatch(r"[0-9A-Za-z.+_-]{1,40}",version): raise ValueError("Invalid package version.")
        files=manifest.get("files")
        if not isinstance(files,dict) or not files: raise ValueError("Manifest must list files and SHA-256 hashes.")
        verified=[]
        for rel,digest in files.items():
            if not _safe_rel(rel) or ":" in str(rel): raise ValueError("Unsafe package path: "+str(rel))
            rp=Path(rel.replace("\\","/"))
            if rp.suffix.lower() not in SAFE_EXTS or rp.name.lower() in PROTECTED: raise ValueError("Protected or unsupported file: "+str(rel))
            if len(rp.parts)>1 and rp.parts[0].lower() not in SAFE_ROOTS: raise ValueError("Path outside permitted app folders: "+str(rel))
            payload="payload/"+rel.replace("\\","/")
            if payload not in names: raise ValueError("Missing payload file: "+str(rel))
            data=z.read(payload)
            if not isinstance(digest,str) or hashlib.sha256(data).hexdigest().lower()!=digest.lower(): raise ValueError("SHA-256 mismatch: "+str(rel))
            verified.append((rel,data))
        notes=z.read("release_notes.md").decode("utf-8",errors="replace") if "release_notes.md" in names else str(manifest.get("release_notes",""))
        return manifest,verified,notes

def _quote(s): return '"'+str(s).replace('"','""')+'"'

class UpdateCentreDialog(QDialog):
    def __init__(self,parent=None,app_root=None,launcher=None):
        super().__init__(parent)
        self.app_root=Path(app_root or Path(__file__).resolve().parent)
        self.launcher=Path(launcher or self.app_root/"START_ERP.bat")
        self.setWindowTitle("NextGen ERP · Update Centre"); self.setMinimumSize(700,520)
        layout=QVBoxLayout(self)
        heading=QLabel("<h2>Update Centre</h2><p>Install verified offline update ZIPs. Business database/workspaces are excluded.</p>"); heading.setWordWrap(True); layout.addWidget(heading)
        self.status=QLabel("Build "+CURRENT_VERSION+" · Offline package updates"); self.status.setStyleSheet("font-weight:600;padding:8px;"); layout.addWidget(self.status)
        self.files=QListWidget(); layout.addWidget(self.files,1)
        self.notes=QPlainTextEdit(); self.notes.setReadOnly(True); self.notes.setMaximumHeight(130); layout.addWidget(self.notes)
        row=QHBoxLayout(); self.pick=QPushButton("Import update ZIP"); self.install=QPushButton("Install and restart"); self.install.setEnabled(False); close=QPushButton("Close")
        row.addWidget(self.pick); row.addWidget(self.install); row.addStretch(); row.addWidget(close); layout.addLayout(row)
        self.pick.clicked.connect(self.pick_package); self.install.clicked.connect(self.install_package); close.clicked.connect(self.reject)
        self.verified=None
    def pick_package(self):
        path,_=QFileDialog.getOpenFileName(self,"Select update package","","Update packages (*.zip)")
        if not path:return
        try: manifest,verified,notes=inspect_package(path)
        except Exception as exc: QMessageBox.critical(self,"Package rejected",str(exc)); self.install.setEnabled(False); return
        self.verified=(manifest,verified); self.files.clear(); self.files.addItem(f"Version {manifest.get('version')} · {len(verified)} file(s)")
        for rel,_ in verified:self.files.addItem(" • "+rel)
        self.notes.setPlainText(notes or "No release notes supplied."); self.status.setText("Hashes passed (package source is not digitally authenticated). Review before installation."); self.install.setEnabled(True)
    def install_package(self):
        if not self.verified:return
        manifest,files=self.verified
        if QMessageBox.warning(self,"Confirm installation",f"Install version {manifest.get('version')}?\n\nApplication files are backed up, then replaced after ERP closes. Business data is not included.",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        try:
            staging=Path(tempfile.mkdtemp(prefix="NextGenERP_update_",dir=os.getenv("TEMP",tempfile.gettempdir())))
            payload=staging/"payload"; payload.mkdir()
            for rel,data in files:
                dest=payload/rel; dest.parent.mkdir(parents=True,exist_ok=True); dest.write_bytes(data)
            backup=Path(os.getenv("APPDATA",str(Path.home())))/"NextGenERP"/"update_backups"/("backup_"+str(manifest["version"])+"_"+__import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S"))
            pairs=[(payload/rel,self.app_root/rel,backup/rel) for rel,_ in files]
            cmd=["@echo off","setlocal","timeout /t 3 /nobreak >nul","mkdir "+_quote(backup)]
            for src,dst,bak in pairs:
                cmd += ["mkdir "+_quote(dst.parent),"mkdir "+_quote(bak.parent),"if exist "+_quote(dst)+" copy /y "+_quote(dst)+" "+_quote(bak)+" >nul","if not exist "+_quote(dst)+" echo new>"+_quote(str(staging/(str(len(cmd))+".new")))]
            for src,dst,bak in pairs: cmd += ["copy /y "+_quote(src)+" "+_quote(dst)+" >nul","if errorlevel 1 goto rollback"]
            cmd += ["start \"\" "+_quote(self.launcher),"goto done",":rollback"]
            for index,(src,dst,bak) in enumerate(pairs):\n                marker=staging/(str(index)+".new")\n                cmd += ["if exist "+_quote(bak)+" copy /y "+_quote(bak)+" "+_quote(dst)+" >nul","if exist "+_quote(marker)+" del "+_quote(dst)]
            cmd += ["echo Install failed; rollback attempted.>>"+_quote(staging/"update.log"),":done","endlocal"]
            script=staging/"apply_update.cmd"; script.write_text("\r\n".join(cmd),encoding="utf-8")
            subprocess.Popen(["cmd.exe","/c","start","",str(script)],creationflags=getattr(subprocess,"CREATE_NEW_CONSOLE",0))
            QMessageBox.information(self,"Update queued","Installer staged. ERP will close, apply files, and reopen. Backup location:\n"+str(backup))
            self.accept(); QTimer.singleShot(300,QApplication.instance().quit)
        except Exception as exc: QMessageBox.critical(self,"Update staging failed",str(exc))
