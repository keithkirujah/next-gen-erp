"""Qt UI for a read-only mirror of the shop's online product catalogue."""
from __future__ import annotations

import hashlib
import os
import webbrowser
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta

from PySide6.QtCore import QThread, Signal, Qt, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QAbstractItemView,
)

from website_catalog import fetch_catalog, initialize_catalog_cache, list_catalog_cache, replace_catalog_cache


class ClickableImageLabel(QLabel):
    """A product photo thumbnail that opens a larger preview on click."""
    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and callable(self._callback):
            self._callback()
        super().mousePressEvent(event)


def _stamp():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class WebsiteCatalogWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, source: str, url: str, parent=None):
        super().__init__(parent)
        self.source = source
        self.url = url

    def run(self):
        try:
            self.succeeded.emit(fetch_catalog(self.source, self.url))
        except Exception as exc:
            self.failed.emit(str(exc)[:1000] or exc.__class__.__name__)


class WebsiteCatalogDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.worker = None
        self.image_manager = QNetworkAccessManager(self)
        self.image_manager.finished.connect(self._on_image_loaded)
        self._requested_images = set()
        self._image_pixmaps = {}
        self._image_queue = []
        self._image_inflight = set()
        self._image_downloaded = 0
        self._image_failed = 0
        self._catalog_change_summary = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0}
        # Keep user data outside the installation folder so updates/re-extraction do
        # not discard the offline image cache. Match main.py's APP_DIR location.
        self._image_cache_dir = Path(os.getenv("APPDATA", str(Path.home()))) / "NextGenERP" / "catalog_images"
        self._image_cache_dir.mkdir(parents=True, exist_ok=True)
        self._silent_refresh = False
        self.is_admin = str(getattr(parent, "role", "")) == "Admin"
        initialize_catalog_cache(self.store.workspace_conn)
        self.setWindowTitle("Website catalogue · read-only mirror")
        self.setMinimumSize(980, 630)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)
        root.addWidget(QLabel("<span style='font-size:18pt;font-weight:800'>Website catalogue</span>"))
        summary = QLabel(
            "Browse published products mirrored from the shop website. This catalogue is read-only: "
            "it does not create stock, change local selling prices, or issue sales."
        )
        summary.setWordWrap(True)
        root.addWidget(summary)
        configured_url = self.store.setting("website_catalog_url", "")
        source = self.store.setting("website_catalog_source", "woocommerce")
        self.source_label = QLabel(f"Source: {source} · {configured_url or 'No website configured'}")
        self.source_label.setWordWrap(True)
        root.addWidget(self.source_label)
        self.sync_status_label = QLabel("")
        self.sync_status_label.setWordWrap(True)
        self.sync_status_label.setObjectName("panelAlt")
        self.sync_status_label.setStyleSheet("padding:8px 10px;border-radius:8px;")
        root.addWidget(self.sync_status_label)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search website product, SKU, category or description…")
        self.search.textChanged.connect(self.reload_table)
        root.addWidget(self.search)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["IMAGE", "PRODUCT", "BRAND", "CATEGORY", "WEBSITE PRICE", "AVAILABILITY", "SKU", "LAST SYNC"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(64)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setColumnWidth(0, 132)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.cellDoubleClicked.connect(lambda *_: self.open_selected())
        root.addWidget(self.table, 1)
        self.count_label = QLabel("")
        root.addWidget(self.count_label)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh from website")
        self.refresh_button.setObjectName("primary")
        self.refresh_button.setEnabled(self.is_admin)
        self.refresh_button.setToolTip("Refresh the cached website mirror. Admin access is required to manage external connections.")
        self.refresh_button.clicked.connect(self.start_refresh)
        buttons.addWidget(self.refresh_button)
        self.images_button = QPushButton("Download missing images")
        self.images_button.setToolTip("Download product photos for offline use, with a small bounded number of simultaneous downloads.")
        self.images_button.setEnabled(self.is_admin)
        self.images_button.clicked.connect(self.queue_all_missing_images)
        buttons.addWidget(self.images_button)
        self.open_button = QPushButton("Open selected on website")
        self.open_button.clicked.connect(self.open_selected)
        buttons.addWidget(self.open_button)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)
        for button in self.findChildren(QPushButton):
            button.setCursor(Qt.PointingHandCursor)
        if not self.is_admin:
            hint = QLabel("Only an administrator can refresh the online catalogue. You can browse the last successful cached copy.")
            hint.setWordWrap(True)
            root.addWidget(hint)
        self.reload_table()
        self.refresh_sync_status()
        # Admin sessions refresh stale catalogue metadata in the background; failures
        # retain the last good cache and never block browsing.
        if self.is_admin:
            last = str(self.store.setting("website_catalog_last_sync", "") or "")
            try:
                stale = not last or datetime.strptime(last, "%Y-%m-%d %H:%M:%S") < datetime.now() - timedelta(hours=12)
            except ValueError:
                stale = True
            if stale and configured_url.strip():
                self._silent_refresh = True
                self.start_refresh()

    def _local_image_path(self, url):
        suffix = Path(QUrl(url).path()).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
            suffix = ".img"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._image_cache_dir / (digest + suffix)

    def reload_table(self, *_):
        rows = list_catalog_cache(self.store.workspace_conn, self.search.text().strip())
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            try:
                import json
                image_urls = json.loads(row["image_urls"] or "[]")
            except Exception:
                image_urls = []
            image_url = str(row["image_url"] or (image_urls[0] if image_urls else ""))
            image_label = ClickableImageLabel(lambda u=image_url, n=str(row["name"] or "Product"): self.open_image_preview(u, n))
            image_label.setAlignment(Qt.AlignCenter)
            image_label.setFixedSize(120, 92)
            image_label.setToolTip("Website product image" if image_url else "No product image supplied")
            self.table.setCellWidget(row_index, 0, image_label)
            if image_url:
                image_label.setProperty("image_url", image_url)
                if image_url in self._image_pixmaps:
                    image_label.setPixmap(self._image_pixmaps[image_url].scaled(114, 86, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    image_label.setText("")
                else:
                    local_path = self._local_image_path(image_url)
                    cached_pixmap = QPixmap(str(local_path)) if local_path.exists() else QPixmap()
                    if not cached_pixmap.isNull():
                        self._image_pixmaps[image_url] = cached_pixmap
                        image_label.setPixmap(cached_pixmap.scaled(114, 86, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                        image_label.setText("")
                    elif image_url not in self._requested_images:
                        self._queue_image_download(image_url)
            price = ""
            if row["price"]:
                price = (str(row["currency"]) + " " if row["currency"] else "") + str(row["price"])
            sale_price = str(row["sale_price"] or "")
            if sale_price and sale_price != str(row["price"] or ""):
                price += "  |  SALE: " + sale_price
            values = [row["name"], row["brand"], row["category"], price, row["availability"], row["sku"], row["updated_at"]]
            for col, value in enumerate(values, start=1):
                cell = QTableWidgetItem(str(value or ""))
                if col == 1:
                    cell.setData(Qt.UserRole, str(row["product_url"] or ""))
                    cell.setToolTip(str(row["description"] or row["name"]))
                self.table.setItem(row_index, col, cell)
            self.table.setRowHeight(row_index, 98)
        last_sync = self.store.setting("website_catalog_last_sync", "") or "not yet synchronized"
        source = self.store.setting("website_catalog_source", "99computers")
        url = self.store.setting("website_catalog_url", "") or "No website configured"
        self.source_label.setText(f"Source: {source} · {url} · Last successful sync: {last_sync}")
        self.count_label.setText(f"{len(rows):,} product(s) shown from the local cache. Website photos and catalogue remain available from the last successful sync while offline.")
        self.refresh_sync_status()

    def open_image_preview(self, url, product_name="Product"):
        """Show the selected product photo in a large, screen-fitted preview."""
        url = str(url or "").strip()
        if not url:
            QMessageBox.information(self, "Product image", "No image is available for this product.")
            return
        pixmap = self._image_pixmaps.get(url)
        if pixmap is None or pixmap.isNull():
            local_path = self._local_image_path(url)
            if local_path.exists():
                pixmap = QPixmap(str(local_path))
        if pixmap is None or pixmap.isNull():
            self._queue_image_download(url)
            QMessageBox.information(self, "Product image", "This image is not loaded yet. It has been queued for download; try again in a moment.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Product image — {product_name}")
        dlg.setMinimumSize(520, 420)
        dlg.resize(900, 700)
        layout = QVBoxLayout(dlg)
        title = QLabel(product_name)
        title.setWordWrap(True)
        title.setStyleSheet("font-size:14pt;font-weight:700;padding:4px")
        layout.addWidget(title)
        preview = QLabel()
        preview.setAlignment(Qt.AlignCenter)
        preview.setMinimumSize(400, 300)
        preview.setStyleSheet("background:#f3f3f3;border:1px solid #d5d5d5;border-radius:8px")
        screen = self.screen()
        available = screen.availableGeometry() if screen else None
        max_w = min(1200, int(available.width() * 0.82)) if available else 1000
        max_h = min(850, int(available.height() * 0.78)) if available else 700
        preview.setPixmap(pixmap.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(preview, 1)
        hint = QLabel("Click another product photo to inspect it, or close this preview to return to the catalogue.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        close = QPushButton("Close preview")
        close.clicked.connect(dlg.accept)
        close.setCursor(Qt.PointingHandCursor)
        layout.addWidget(close, alignment=Qt.AlignRight)
        dlg.exec()

    def _queue_image_download(self, url):
        """Queue an image once; downloads are throttled to avoid flooding the network."""
        url = str(url or "").strip()
        if not url or url in self._requested_images:
            return
        try:
            parsed = QUrl(url)
            if parsed.scheme().lower() != "https" or not parsed.host():
                self._image_failed += 1
                self.refresh_sync_status()
                return
            local_path = self._local_image_path(url)
            if local_path.exists() and local_path.stat().st_size > 0:
                self._requested_images.add(url)
                return
        except (OSError, ValueError):
            pass
        self._requested_images.add(url)
        self._image_queue.append(url)
        self._pump_image_queue()

    def _pump_image_queue(self):
        while self._image_queue and len(self._image_inflight) < 3:
            url = self._image_queue.pop(0)
            try:
                request = QNetworkRequest(QUrl(url))
                request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
                request.setTransferTimeout(15000)
                reply = self.image_manager.get(request)
                self._image_inflight.add(url)
                reply.setProperty("catalog_image_url", url)
            except Exception:
                self._image_failed += 1
        self.refresh_sync_status()

    def queue_all_missing_images(self):
        if not self.is_admin:
            QMessageBox.warning(self, "Administrator required", "Only an administrator can download the complete image set for offline use.")
            return
        import json
        rows = list_catalog_cache(self.store.workspace_conn, limit=2000)
        candidates = []
        for row in rows:
            try:
                urls = json.loads(row["image_urls"] or "[]")
            except Exception:
                urls = []
            if row["image_url"]:
                urls.insert(0, row["image_url"])
            for url in urls:
                if url and not self._local_image_path(url).exists():
                    candidates.append(url)
        for url in dict.fromkeys(candidates):
            self._queue_image_download(url)
        self.images_button.setText("Downloading images…" if self._image_queue or self._image_inflight else "Images cached")
        self.refresh_sync_status()

    def _on_image_loaded(self, reply):
        try:
            url = str(reply.property("catalog_image_url") or reply.url().toString())
            self._image_inflight.discard(url)
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self._image_failed += 1
                return
            data = bytes(reply.readAll())
            if not data or len(data) > 12 * 1024 * 1024:
                self._image_failed += 1
                return
            pixmap = QPixmap()
            if not pixmap.loadFromData(data):
                self._image_failed += 1
                return
            # Persist original bytes atomically so interrupted writes cannot corrupt the cache.
            try:
                local_path = self._local_image_path(url)
                temp_path = local_path.with_suffix(local_path.suffix + ".part")
                temp_path.write_bytes(data)
                temp_path.replace(local_path)
            except OSError:
                self._image_failed += 1
            else:
                self._image_downloaded += 1
            self._image_pixmaps[url] = pixmap
            thumbnail = pixmap.scaled(114, 86, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            for row in range(self.table.rowCount()):
                widget = self.table.cellWidget(row, 0)
                if isinstance(widget, QLabel) and widget.property("image_url") == url:
                    widget.setPixmap(thumbnail)
                    widget.setText("")
        finally:
            reply.deleteLater()
            self._pump_image_queue()
            if not self._image_queue and not self._image_inflight and hasattr(self, "images_button"):
                self.images_button.setText("Download missing images")

    def _image_cache_stats(self):
        cached = 0
        size_bytes = 0
        try:
            for path in self._image_cache_dir.iterdir():
                if path.is_file() and not path.name.endswith(".part"):
                    cached += 1
                    try:
                        size_bytes += path.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return cached, size_bytes

    def refresh_sync_status(self):
        try:
            total = len(list_catalog_cache(self.store.workspace_conn, limit=2000))
        except Exception:
            total = 0
        cached, size_bytes = self._image_cache_stats()
        size_mb = size_bytes / (1024 * 1024)
        last = str(self.store.setting("website_catalog_last_sync", "") or "Never")
        changes = self._catalog_change_summary
        active = len(self._image_queue) + len(self._image_inflight)
        self.sync_status_label.setText(
            f"Sync: {last}  ·  {total:,} cached products  ·  "
            f"Last comparison: +{changes['added']} added, {changes['changed']} changed, "
            f"-{changes['removed']} removed, {changes['unchanged']} unchanged  ·  "
            f"Images: {cached:,} saved ({size_mb:.1f} MB), {active} queued/in progress, "
            f"{self._image_downloaded} downloaded this session, {self._image_failed} failed"
        )

    def start_refresh(self):
        if not self.is_admin:
            QMessageBox.warning(self, "Administrator required", "Ask an administrator to refresh the website catalogue.")
            return
        if self.worker is not None and self.worker.isRunning():
            return
        source = self.store.setting("website_catalog_source", "woocommerce") or "woocommerce"
        url = self.store.setting("website_catalog_url", "").strip()
        if not url:
            QMessageBox.information(self, "Website not configured", "An administrator must configure the website URL under Administration → Integrations first.")
            return
        self._pre_refresh_snapshot = {}
        try:
            for old in list_catalog_cache(self.store.workspace_conn, limit=2000):
                self._pre_refresh_snapshot[str(old["external_id"])] = tuple(str(old[key] or "") for key in ("name", "sku", "description", "price", "sale_price", "category", "availability", "image_url"))
        except Exception:
            self._pre_refresh_snapshot = {}
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Connecting…")
        self.worker = WebsiteCatalogWorker(source, url, self)
        self.worker.succeeded.connect(self.on_refresh_succeeded)
        self.worker.failed.connect(self.on_refresh_failed)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def on_refresh_succeeded(self, items):
        source = self.store.setting("website_catalog_source", "woocommerce") or "woocommerce"
        try:
            old_snapshot = getattr(self, "_pre_refresh_snapshot", {})
            new_snapshot = {str(item.get("external_id", "")): tuple(str(item.get(key, "") or "") for key in ("name", "sku", "description", "price", "sale_price", "category", "availability", "image_url")) for item in items}
            old_ids, new_ids = set(old_snapshot), set(new_snapshot)
            self._catalog_change_summary = {
                "added": len(new_ids - old_ids),
                "removed": len(old_ids - new_ids),
                "changed": sum(1 for key in old_ids & new_ids if old_snapshot[key] != new_snapshot[key]),
                "unchanged": sum(1 for key in old_ids & new_ids if old_snapshot[key] == new_snapshot[key]),
            }
            count = replace_catalog_cache(self.store.workspace_conn, source, items)
            synced_at = _stamp()
            self.store.set_setting("website_catalog_last_sync", synced_at)
            username = getattr(self.parent(), "username", "system")
            self.store.log(username, "Synchronized website catalogue", f"{count} website products from {source}")
            self.reload_table()
            self.queue_all_missing_images()
            self.refresh_sync_status()
            summary = self._catalog_change_summary
            QMessageBox.information(self, "Catalogue refreshed", f"Loaded {count:,} products from the website.\n\nAdded: {summary['added']}\nChanged: {summary['changed']}\nRemoved: {summary['removed']}\nUnchanged: {summary['unchanged']}\n\nImage downloads will continue in the background. Local stock and prices were not changed.")
        except Exception as exc:
            QMessageBox.critical(self, "Catalogue could not be saved", f"Website data was downloaded, but the local cache could not be updated.\n\n{exc}")

    def on_refresh_failed(self, error):
        if not self._silent_refresh:
            QMessageBox.warning(self, "Website connection failed", str(error) + "\n\nThe last successful catalogue and locally cached images have been retained.")
        self._silent_refresh = False
        self.refresh_sync_status()

    def on_worker_finished(self):
        self.refresh_button.setEnabled(self.is_admin)
        self.refresh_button.setText("Refresh from website")

    def open_selected(self):
        row = self.table.currentRow()
        item = self.table.item(row, 1) if row >= 0 else None
        url = str(item.data(Qt.UserRole) or "").strip() if item else ""
        if not url:
            QMessageBox.information(self, "No website link", "This product does not provide a public product URL.")
            return
        # Feeds sometimes return relative URLs or whitespace/escaped values. Resolve
        # them against the configured website before asking Windows to open a browser.
        base = str(self.store.setting("website_catalog_url", "") or "").strip()
        try:
            if base:
                url = urllib.parse.urljoin(base.rstrip("/") + "/", url)
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("The product link is not a valid HTTP or HTTPS URL.")
            if parsed.username or parsed.password:
                raise ValueError("The product link contains embedded credentials and was blocked.")
            url = urllib.parse.urlunsplit(parsed)
            opened = QDesktopServices.openUrl(QUrl.fromUserInput(url))
            if not opened:
                # QDesktopServices can fail on Windows when the default browser
                # association is unusual; try the standard OS browser hand-off too.
                opened = webbrowser.open(url, new=2)
            if not opened:
                QMessageBox.warning(self, "Could not open product page", f"Windows could not open this link. Copy it into your browser:\n\n{url}")
        except Exception as exc:
            QMessageBox.warning(self, "Invalid product link", f"The selected product link could not be opened.\n\n{exc}\n\nLink: {url}")

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Refresh in progress", "Wait for the website catalogue refresh to finish before closing this window.")
            event.ignore()
            return
        event.accept()
