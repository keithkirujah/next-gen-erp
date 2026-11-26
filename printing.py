"""Universal printing for NextGen ERP.

One print path for every document (invoices, quotes, receipts, job sheets, records):

* any installed printer (laser, inkjet, thermal, dot-matrix, network, "Print to PDF"),
  or "Save as PDF file" when no printer is available;
* any paper: A3/A4/A5/A6/B5/Letter/Legal/Tabloid/Executive, thermal rolls (58/76/80 mm)
  whose length is cut to the content, or a fully custom width x height in millimetres;
* orientation, margins, scale, copies, grayscale, preview, saved per-computer defaults;
* a compact single-column "receipt" layout is chosen automatically for rolls, and the full
  "document" layout for sheets (the owner can override).

Settings are stored per installation (not per business workspace) under ``print_*`` keys,
because they describe the hardware attached to this computer.
"""
from __future__ import annotations

import logging
import os
import base64
from dataclasses import dataclass, asdict, fields
from html.parser import HTMLParser
from pathlib import Path

from PySide6.QtCore import QMarginsF, QSizeF, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QImage, QPageLayout, QPageSize, QPainter, QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QTextBrowser, QVBoxLayout,
)

log = logging.getLogger("nextgen.printing")

SYSTEM_DEFAULT = ""          # use the operating-system default printer
PDF_PRINTER = "__pdf__"      # write a PDF file instead of using a physical printer
MAX_ROLL_LENGTH_MM = 1500.0  # many thermal drivers reject longer pages; Qt paginates beyond this
MIN_ROLL_LENGTH_MM = 40.0

SHEET_PAPERS = {
    "a3": ("A3", QPageSize.PageSizeId.A3),
    "a4": ("A4", QPageSize.PageSizeId.A4),
    "a5": ("A5", QPageSize.PageSizeId.A5),
    "a6": ("A6", QPageSize.PageSizeId.A6),
    "b5": ("B5", QPageSize.PageSizeId.B5),
    "letter": ("Letter", QPageSize.PageSizeId.Letter),
    "legal": ("Legal", QPageSize.PageSizeId.Legal),
    "tabloid": ("Tabloid / Ledger (11 x 17 in)", QPageSize.PageSizeId.Tabloid),
    "executive": ("Executive", QPageSize.PageSizeId.Executive),
}
ROLL_PAPERS = {
    "roll58": ("Receipt roll · 58 mm", 58.0),
    "roll76": ("Receipt roll · 76 mm (dot-matrix)", 76.0),
    "roll80": ("Receipt roll · 80 mm", 80.0),
}
PAPER_CUSTOM = "custom"
PAPER_PRINTER_DEFAULT = "printer"


def paper_choices():
    """(key, label) pairs in the order they appear in the dialog."""
    out = [(k, v[0]) for k, v in ROLL_PAPERS.items()]
    out += [(k, v[0]) for k, v in SHEET_PAPERS.items()]
    out += [(PAPER_CUSTOM, "Custom size (enter width x height)…"),
            (PAPER_PRINTER_DEFAULT, "Use the printer's current paper")]
    return out


@dataclass
class PrintSettings:
    printer: str = SYSTEM_DEFAULT
    paper: str = "a4"
    custom_width_mm: float = 80.0
    custom_height_mm: float = 0.0      # 0 = continuous roll (length follows the content)
    orientation: str = "auto"          # auto | portrait | landscape
    margin_mm: float = 10.0
    scale_pct: int = 100
    copies: int = 1
    grayscale: bool = False
    layout: str = "auto"               # auto | receipt | document
    preview: bool = True
    system_dialog: bool = False
    feed_mm: float = 12.0              # blank paper after the last line of a roll receipt
    auto_receipt: bool = False         # POS prints a receipt after each sale
    pdf_folder: str = ""

    # ---- derived helpers -------------------------------------------------
    @property
    def is_roll(self) -> bool:
        if self.paper in ROLL_PAPERS:
            return True
        return self.paper == PAPER_CUSTOM and self.custom_height_mm <= 0

    @property
    def roll_width_mm(self) -> float:
        if self.paper in ROLL_PAPERS:
            return ROLL_PAPERS[self.paper][1]
        return max(20.0, float(self.custom_width_mm))

    @property
    def effective_layout(self) -> str:
        """Pick receipt vs full invoice HTML from paper + explicit override.

        Rules (owner can still force either layout in Printer setup):
        * layout == "receipt"  → always compact receipt
        * layout == "document" → always full invoice / quotation sheet
        * layout == "auto":
            - receipt-roll paper (58/76/80 mm) or continuous custom → receipt
            - any standard sheet (A4, Letter, A5, …) or fixed custom sheet → document
            - only a very narrow fixed custom width (usable ≤ 90 mm) falls back to receipt
        """
        if self.layout == "receipt":
            return "receipt"
        if self.layout == "document":
            return "document"
        # layout == "auto"
        if self.is_roll:
            return "receipt"
        if self.paper in SHEET_PAPERS or self.paper == PAPER_PRINTER_DEFAULT:
            return "document"
        # Custom fixed sheet: only treat as receipt when the printable width is
        # truly thermal-roll narrow (≤ 90 mm after margins).
        width = max(self.custom_width_mm, self.custom_height_mm) if self.orientation == "landscape" else self.custom_width_mm
        usable = float(width) - 2 * max(0.0, float(self.margin_mm))
        if usable <= 90:
            return "receipt"
        return "document"

    def describe_paper(self) -> str:
        if self.paper == PAPER_CUSTOM:
            h = "roll" if self.custom_height_mm <= 0 else f"{self.custom_height_mm:g}"
            return f"Custom {self.custom_width_mm:g} x {h} mm"
        if self.paper == PAPER_PRINTER_DEFAULT:
            return "printer's current paper"
        label = dict(paper_choices()).get(self.paper, self.paper)
        return label


_PREFIX = "print_"


def load_settings(store) -> PrintSettings:
    s = PrintSettings()
    for f in fields(PrintSettings):
        raw = store.setting(_PREFIX + f.name, None)
        if raw is None or raw == "":
            continue
        try:
            current = getattr(s, f.name)
            if isinstance(current, bool):
                value = str(raw).strip().lower() in {"1", "true", "yes", "on"}
            elif isinstance(current, int):
                value = int(float(raw))
            elif isinstance(current, float):
                value = float(raw)
            else:
                value = str(raw)
            setattr(s, f.name, value)
        except (TypeError, ValueError):
            continue
    return _sanitize(s)


def save_settings(store, s: PrintSettings) -> None:
    s = _sanitize(s)
    for key, value in asdict(s).items():
        store.set_setting(_PREFIX + key, "1" if value is True else "0" if value is False else str(value))


def _sanitize(s: PrintSettings) -> PrintSettings:
    valid_papers = {k for k, _ in paper_choices()}
    if s.paper not in valid_papers:
        s.paper = "a4"
    if s.orientation not in {"auto", "portrait", "landscape"}:
        s.orientation = "auto"
    if s.layout not in {"auto", "receipt", "document"}:
        s.layout = "auto"
    s.custom_width_mm = min(max(float(s.custom_width_mm), 20.0), 1200.0)
    s.custom_height_mm = min(max(float(s.custom_height_mm), 0.0), 3000.0)
    s.margin_mm = min(max(float(s.margin_mm), 0.0), 40.0)
    s.scale_pct = min(max(int(s.scale_pct), 30), 300)
    s.copies = min(max(int(s.copies), 1), 99)
    s.feed_mm = min(max(float(s.feed_mm), 0.0), 60.0)
    return s


# ---------------------------------------------------------------------------
# Printers
# ---------------------------------------------------------------------------
def installed_printers():
    """Names of printers the OS knows about (can be empty on a fresh PC)."""
    try:
        return list(QPrinterInfo.availablePrinterNames())
    except Exception:  # pragma: no cover - platform specific
        log.exception("could not enumerate printers")
        return []


def default_printer_name() -> str:
    try:
        return QPrinterInfo.defaultPrinterName() or ""
    except Exception:  # pragma: no cover
        return ""


# ---------------------------------------------------------------------------
# Page set-up
# ---------------------------------------------------------------------------
def _margins(mm: float) -> QMarginsF:
    return QMarginsF(mm, mm, mm, mm)


def _try_layout(printer: QPrinter, page_size: QPageSize, orientation, margin_mm: float) -> bool:
    layout = QPageLayout(page_size, orientation, _margins(margin_mm), QPageLayout.Unit.Millimeter)
    if printer.setPageLayout(layout):
        return True
    # Some drivers refuse margins below their hardware minimum: retry with the printer's own.
    layout = QPageLayout(page_size, orientation, QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
    layout.setMode(QPageLayout.Mode.FullPageMode)
    if printer.setPageLayout(layout):
        return True
    return printer.setPageSize(page_size)


def configure_printer(printer: QPrinter, s: PrintSettings, content_height_mm: float | None = None) -> list[str]:
    """Apply paper/orientation/margins to ``printer``. Returns human-readable notes about fallbacks."""
    notes: list[str] = []
    printer.setColorMode(QPrinter.ColorMode.GrayScale if s.grayscale else QPrinter.ColorMode.Color)
    if s.paper == PAPER_PRINTER_DEFAULT:
        return notes
    portrait = QPageLayout.Orientation.Portrait
    landscape = QPageLayout.Orientation.Landscape
    mm = QPageSize.Unit.Millimeter

    if s.is_roll:
        width = s.roll_width_mm
        height = content_height_mm if content_height_mm else 300.0
        height = min(max(height, MIN_ROLL_LENGTH_MM), MAX_ROLL_LENGTH_MM)
        margin = min(s.margin_mm, max(0.0, width / 4))
        size = QPageSize(QSizeF(width, height), mm, f"Roll {width:g}mm", QPageSize.SizeMatchPolicy.ExactMatch)
        if not _try_layout(printer, size, portrait, margin):
            notes.append(f"This printer rejected a {width:g} mm roll; it will use its own paper setting. "
                         "Select the roll size in the printer's Windows preferences.")
        return notes

    if s.paper == PAPER_CUSTOM:
        w, h = s.custom_width_mm, s.custom_height_mm
        size = QPageSize(QSizeF(w, h), mm, f"Custom {w:g}x{h:g}mm", QPageSize.SizeMatchPolicy.ExactMatch)
    else:
        size = QPageSize(SHEET_PAPERS[s.paper][1])
    orient = landscape if s.orientation == "landscape" else portrait
    if s.orientation == "auto" and s.paper == PAPER_CUSTOM and s.custom_width_mm > s.custom_height_mm:
        orient = landscape
    if not _try_layout(printer, size, orient, s.margin_mm):
        notes.append(f"This printer does not support {s.describe_paper()}; its own paper setting will be used.")
    return notes


def build_printer(s: PrintSettings, pdf_path: str | None = None) -> tuple[QPrinter, list[str]]:
    notes: list[str] = []
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    if s.printer == PDF_PRINTER or pdf_path:
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        if pdf_path:
            printer.setOutputFileName(pdf_path)
    elif s.printer and s.printer != SYSTEM_DEFAULT:
        if s.printer in installed_printers():
            printer.setPrinterName(s.printer)
        else:
            notes.append(f"Printer “{s.printer}” is not installed on this computer; the default printer was used.")
    printer.setCopyCount(max(1, s.copies))
    return printer, notes


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def make_document(html: str) -> QTextDocument:
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setHtml(html)
    return doc


class _WatermarkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.encoded = ""

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        values = dict(attrs)
        if values.get("name") == "nextgen-invoice-watermark":
            self.encoded = values.get("content", "")


def _watermark_image(html: str) -> QImage | None:
    parser = _WatermarkParser()
    parser.feed(html)
    if not parser.encoded:
        return None
    try:
        image = QImage.fromData(base64.b64decode(parser.encoded, validate=True))
    except (ValueError, TypeError):
        return None
    return image if not image.isNull() else None


def _device_page_size(printer: QPrinter) -> QSizeF:
    rect = printer.pageRect(QPrinter.Unit.DevicePixel)
    return QSizeF(rect.width(), rect.height())


def measure_roll_height_mm(printer: QPrinter, doc: QTextDocument, s: PrintSettings) -> float:
    """Content height in mm of ``doc`` laid out at the roll's printable width."""
    configure_printer(printer, s, content_height_mm=MAX_ROLL_LENGTH_MM)
    scale = s.scale_pct / 100.0
    width_px = _device_page_size(printer).width()
    doc.documentLayout().setPaintDevice(printer)
    doc.setPageSize(QSizeF(width_px / scale, 10_000_000))
    content_px = doc.size().height() * scale
    content_mm = content_px / float(printer.resolution()) * 25.4
    return content_mm + 2 * min(s.margin_mm, s.roll_width_mm / 4) + s.feed_mm


def fit_scale_for_one_page(doc: QTextDocument, printer: QPrinter, s: PrintSettings) -> tuple[float, str]:
    """Pick the largest scale that still keeps sheet content on one page.

    Goal: one page when possible, with maximum paper utilisation (content fills the
    sheet instead of a small block at the top). Thermal rolls keep the user scale.
    """
    user_scale = max(0.3, min(3.0, s.scale_pct / 100.0))
    if s.is_roll:
        return user_scale, ""

    page = _device_page_size(printer)
    if page.width() <= 1 or page.height() <= 1:
        return user_scale, ""

    doc.documentLayout().setPaintDevice(printer)

    def page_count_at(scale: float) -> int:
        sc = max(scale, 0.05)
        doc.setPageSize(QSizeF(page.width() / sc, page.height() / sc))
        return max(1, doc.pageCount())

    # Largest scale that still fits on one page:
    # floor 55% (readable), ceiling 145% (fills sparse invoices).
    lo, hi = 0.55, 1.45
    best = lo
    fits_any = False
    for _ in range(16):
        mid = (lo + hi) / 2.0
        if page_count_at(mid) <= 1:
            fits_any = True
            best = mid
            lo = mid          # try larger → use more of the page
        else:
            hi = mid          # too big → shrink
    if fits_any:
        page_count_at(best)
        if abs(best - user_scale) < 0.02:
            return best, ""
        if best > user_scale:
            return best, f"Enlarged to {best * 100:.0f}% so the invoice fills the page."
        return best, f"Auto-fitted to one page at {best * 100:.0f}% zoom."

    page_count_at(lo)
    return lo, f"Could not fit on one page below {lo * 100:.0f}% — printing on {doc.pageCount()} pages."


def paint_document(printer: QPrinter, doc: QTextDocument, s: PrintSettings, watermark: QImage | None = None,
                   scale: float | None = None) -> int:
    """Lay ``doc`` out for ``printer``'s current page and paint every page. Returns the page count."""
    if scale is None:
        scale, _ = fit_scale_for_one_page(doc, printer, s)
    doc.documentLayout().setPaintDevice(printer)
    page = _device_page_size(printer)
    doc.setPageSize(QSizeF(page.width() / scale, page.height() / scale))
    page_h = doc.pageSize().height()
    pages = max(1, doc.pageCount())
    painter = QPainter()
    if not painter.begin(printer):
        raise RuntimeError("The printer could not be opened. Check that it is on, connected and not paused.")
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        from PySide6.QtCore import QRectF
        for index in range(pages):
            if index:
                printer.newPage()
            painter.save()
            painter.scale(scale, scale)
            painter.translate(0, -index * page_h)
            clip = QRectF(0, index * page_h, doc.pageSize().width(), page_h)
            painter.setClipRect(clip)
            if watermark is not None and not watermark.isNull() and watermark.width() > 0:
                page_width = doc.pageSize().width()
                target_width = page_width * 0.55
                target_height = target_width * watermark.height() / watermark.width()
                max_height = page_h * 0.4
                if target_height > max_height:
                    target_width *= max_height / target_height
                    target_height = max_height
                target = QRectF((page_width - target_width) / 2,
                                index * page_h + (page_h - target_height) / 2,
                                target_width, target_height)
                painter.save()
                painter.setOpacity(0.08)
                painter.drawImage(target, watermark)
                painter.restore()
            doc.drawContents(painter, clip)
            painter.restore()
    finally:
        painter.end()
    return pages


def render_to_printer(printer: QPrinter, html: str, s: PrintSettings) -> tuple[int, list[str]]:
    """Configure the page (cutting rolls to length) and paint. Returns (pages, notes)."""
    doc = make_document(html)
    notes: list[str] = []
    if s.is_roll:
        height_mm = measure_roll_height_mm(printer, doc, s)
        notes += configure_printer(printer, s, content_height_mm=height_mm)
        scale = s.scale_pct / 100.0
    else:
        notes += configure_printer(printer, s)
        scale, fit_note = fit_scale_for_one_page(doc, printer, s)
        if fit_note:
            notes.append(fit_note)
    pages = paint_document(printer, doc, s, _watermark_image(html), scale=scale)
    return pages, notes


# ---------------------------------------------------------------------------
# Options dialog
# ---------------------------------------------------------------------------
class PrintOptionsDialog(QDialog):
    """Pick printer, paper, layout and options. ``self.settings`` holds the result when accepted."""

    def __init__(self, parent, title: str, settings: PrintSettings, style: str = "", setup_mode: bool = False):
        super().__init__(parent)
        self.setup_mode = setup_mode
        self.setWindowTitle(("Printer setup" if setup_mode else "Print — " + title))
        self.setMinimumWidth(520)
        if style:
            self.setStyleSheet(style)
        self.settings = settings
        self.action = "print"          # print | preview | test
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<span style='font-size:16pt;font-weight:800'>Print options</span>"))
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        v.addWidget(self.hint)

        form = QFormLayout()
        self.printer = QComboBox()
        self.printer.addItem("Default printer of this computer", SYSTEM_DEFAULT)
        for name in installed_printers():
            self.printer.addItem(name, name)
        self.printer.addItem("Save as PDF file…", PDF_PRINTER)
        idx = self.printer.findData(settings.printer)
        if idx < 0 and settings.printer:
            self.printer.addItem(f"{settings.printer} (not installed)", settings.printer)
            idx = self.printer.count() - 1
        self.printer.setCurrentIndex(max(0, idx))
        form.addRow("Printer", self.printer)

        self.paper = QComboBox()
        for key, label in paper_choices():
            self.paper.addItem(label, key)
        self.paper.setCurrentIndex(max(0, self.paper.findData(settings.paper)))
        form.addRow("Paper", self.paper)

        size_row = QHBoxLayout()
        self.cw = QDoubleSpinBox(); self.cw.setRange(20, 1200); self.cw.setDecimals(1); self.cw.setSuffix(" mm"); self.cw.setValue(settings.custom_width_mm)
        self.ch = QDoubleSpinBox(); self.ch.setRange(0, 3000); self.ch.setDecimals(1); self.ch.setSuffix(" mm"); self.ch.setValue(settings.custom_height_mm)
        self.ch.setSpecialValueText("Roll (auto length)")
        size_row.addWidget(QLabel("Width")); size_row.addWidget(self.cw)
        size_row.addWidget(QLabel("Height")); size_row.addWidget(self.ch)
        self.size_widget = QLabel()  # holder so the row can be hidden together
        form.addRow("Custom size", size_row)
        self._size_row_widgets = [self.cw, self.ch]
        self._size_row_label = form.labelForField(size_row) if hasattr(form, "labelForField") else None

        self.orientation = QComboBox()
        for key, label in (("auto", "Automatic"), ("portrait", "Portrait"), ("landscape", "Landscape")):
            self.orientation.addItem(label, key)
        self.orientation.setCurrentIndex(max(0, self.orientation.findData(settings.orientation)))
        form.addRow("Orientation", self.orientation)

        self.layout_mode = QComboBox()
        for key, label in (
            ("auto", "Automatic — receipt roll → receipt, A4/Letter/… → full invoice"),
            ("receipt", "Force compact receipt layout"),
            ("document", "Force full invoice / document layout"),
        ):
            self.layout_mode.addItem(label, key)
        self.layout_mode.setCurrentIndex(max(0, self.layout_mode.findData(settings.layout)))
        form.addRow("Layout", self.layout_mode)

        self.margin = QDoubleSpinBox(); self.margin.setRange(0, 40); self.margin.setDecimals(1); self.margin.setSuffix(" mm"); self.margin.setValue(settings.margin_mm)
        form.addRow("Margins", self.margin)
        self.scale = QSpinBox(); self.scale.setRange(30, 300); self.scale.setSuffix(" %"); self.scale.setValue(settings.scale_pct)
        form.addRow("Size / zoom", self.scale)
        self.feed = QDoubleSpinBox(); self.feed.setRange(0, 60); self.feed.setDecimals(1); self.feed.setSuffix(" mm"); self.feed.setValue(settings.feed_mm)
        form.addRow("Roll tear-off space", self.feed)
        self.copies = QSpinBox(); self.copies.setRange(1, 99); self.copies.setValue(settings.copies)
        form.addRow("Copies", self.copies)
        v.addLayout(form)

        self.gray = QCheckBox("Print in black & white"); self.gray.setChecked(settings.grayscale); v.addWidget(self.gray)
        self.preview = QCheckBox("Show a preview before printing (always on for sheet invoices)")
        self.preview.setChecked(True if settings.effective_layout == "document" and not settings.is_roll else settings.preview)
        v.addWidget(self.preview)
        self.auto_receipt = QCheckBox("Print a receipt automatically after every POS sale (no questions asked)")
        self.auto_receipt.setChecked(bool(getattr(settings, "auto_receipt", False)))
        if setup_mode:
            v.addWidget(self.auto_receipt)
        self.sysdlg = QCheckBox("Also show the Windows printer dialog (duplex, tray, driver options)"); self.sysdlg.setChecked(settings.system_dialog); v.addWidget(self.sysdlg)
        self.remember = QCheckBox("Remember these choices on this computer"); self.remember.setChecked(True)
        if not setup_mode:
            v.addWidget(self.remember)

        row = QHBoxLayout()
        test = QPushButton("Print test page"); test.setToolTip("Prints a ruler so you can check width, margins and alignment.")
        cancel = QPushButton("Cancel")
        self.go = QPushButton("Print"); self.go.setObjectName("primary"); self.go.setDefault(True)
        row.addWidget(test); row.addStretch(); row.addWidget(cancel); row.addWidget(self.go)
        v.addLayout(row)
        cancel.clicked.connect(self.reject)
        self.go.clicked.connect(lambda: self._finish("save" if self.setup_mode else "print"))
        test.clicked.connect(lambda: self._finish("test"))
        for w in (self.paper, self.printer, self.layout_mode, self.ch, self.cw, self.margin):
            sig = w.currentIndexChanged if isinstance(w, QComboBox) else w.valueChanged
            sig.connect(lambda *_: self._refresh())
        self._refresh()

    # -- helpers ---------------------------------------------------------
    def _collect(self) -> PrintSettings:
        s = PrintSettings(
            printer=self.printer.currentData() or SYSTEM_DEFAULT, paper=self.paper.currentData(),
            custom_width_mm=self.cw.value(), custom_height_mm=self.ch.value(),
            orientation=self.orientation.currentData(), margin_mm=self.margin.value(),
            scale_pct=self.scale.value(), copies=self.copies.value(), grayscale=self.gray.isChecked(),
            layout=self.layout_mode.currentData(), preview=self.preview.isChecked(),
            system_dialog=self.sysdlg.isChecked(), feed_mm=self.feed.value(),
            auto_receipt=self.auto_receipt.isChecked(),
            pdf_folder=self.settings.pdf_folder)
        return _sanitize(s)

    def _refresh(self):
        s = self._collect()
        custom = s.paper == PAPER_CUSTOM
        for w in self._size_row_widgets:
            w.setVisible(custom)
        roll = s.is_roll
        self.orientation.setEnabled(not roll and s.paper != PAPER_PRINTER_DEFAULT)
        self.feed.setEnabled(roll)
        is_pdf = s.printer == PDF_PRINTER
        self.sysdlg.setEnabled(not is_pdf)
        self.go.setText("Save as default" if self.setup_mode else "Save PDF…" if is_pdf else "Print")
        if not installed_printers() and not is_pdf:
            self.hint.setText("<span style='color:#d98200'>No printer is installed on this computer. "
                              "Choose “Save as PDF file”, or install your printer in Windows first.</span>")
        elif s.effective_layout == "receipt" and not s.is_roll and s.layout == "auto":
            self.hint.setText("This custom size is narrower than ~90 mm after margins, so the compact "
                              "receipt layout will be used. Choose A4 / Letter (or set Layout to “Full document”) "
                              "for the full invoice design.")
        elif s.layout == "document":
            self.hint.setText("Full document layout is forced. Best on A4 / Letter / Legal sheet paper.")
        elif s.layout == "receipt":
            self.hint.setText("Compact receipt layout is forced — suited to thermal rolls and narrow paper.")
        elif roll:
            self.hint.setText("Receipt roll selected: compact receipt layout and page length cut to content. "
                              "Use “Print test page” the first time to check width.")
        else:
            self.hint.setText("Sheet paper → full invoice layout. Content is auto-fitted to one page "
                              "when possible, and a print preview is shown so you can confirm before printing.")

    def _finish(self, action):
        self.settings = self._collect()
        self.action = action
        self._remember = self.remember.isChecked()
        self.accept()

    @property
    def remember_choice(self) -> bool:
        return getattr(self, "_remember", False)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
def test_page_html(s: PrintSettings) -> str:
    width = s.roll_width_mm if s.is_roll else s.custom_width_mm if s.paper == PAPER_CUSTOM else 100
    ticks = "".join(f"<td style='border-left:1px solid #000;font-size:6pt'>{i}</td>" for i in range(0, 10))
    return f"""<html><body style='font-family:Arial'>
    <p style='font-size:11pt'><b>NextGen ERP — printer test</b></p>
    <p style='font-size:8pt'>Paper: {s.describe_paper()} · margins {s.margin_mm:g} mm · zoom {s.scale_pct}%<br>
    If the left and right edges below are both visible, the width is correct.</p>
    <table width='100%' cellspacing='0' cellpadding='2' style='border:1px solid #000'><tr>{ticks}</tr></table>
    <p style='font-size:8pt'>|&lt;— left edge &nbsp;&nbsp;&nbsp;&nbsp;&nbsp; right edge —&gt;|</p>
    <p style='font-size:9pt'>ABCDEFGHIJKLMNOPQRSTUVWXYZ<br>abcdefghijklmnopqrstuvwxyz<br>0123456789 KES 1,234.56</p>
    </body></html>"""


class PrintService:
    """Facade used by the application: ``PrintService(store, parent_style).print_document(...)``."""

    def __init__(self, store, style: str = "", username: str = ""):
        self.store = store
        self.style = style
        self.username = username

    def settings(self) -> PrintSettings:
        return load_settings(self.store)

    # ------------------------------------------------------------------
    def print_document(self, parent, title: str, render, activity: str = "", ask: bool = True) -> bool:
        """``render(layout)`` must return the HTML for layout ``'receipt'`` or ``'document'``."""
        s = self.settings()
        action = "print"
        if ask:
            dlg = PrintOptionsDialog(parent, title, s, self.style)
            if not dlg.exec():
                return False
            s, action = dlg.settings, dlg.action
            if dlg.remember_choice:
                try:
                    save_settings(self.store, s)
                except Exception:
                    log.exception("could not save print settings")
        html = test_page_html(s) if action == "test" else render(s.effective_layout)
        return self._output(parent, title, html, s, activity, force_no_preview=(action == "test"))

    def print_test_page(self, parent, s: PrintSettings) -> bool:
        return self._output(parent, "Printer test", test_page_html(s), s, "printer test", force_no_preview=True)

    def print_silent(self, parent, title: str, render, activity: str = "") -> bool:
        """Print with the saved settings and no questions (e.g. POS receipts). Falls back to the dialog on failure."""
        s = self.settings()
        s.preview = False
        s.system_dialog = False
        if s.printer == PDF_PRINTER:
            return self.print_document(parent, title, render, activity, ask=True)
        try:
            return self._output(parent, title, render(s.effective_layout), s, activity, force_no_preview=True)
        except Exception as exc:
            log.exception("silent print failed")
            QMessageBox.warning(parent, "Receipt not printed", f"{exc}\n\nChoose the printer and paper to continue.")
            return self.print_document(parent, title, render, activity, ask=True)

    # ------------------------------------------------------------------
    def _output(self, parent, title, html, s: PrintSettings, activity: str, force_no_preview: bool = False) -> bool:
        pdf_path = None
        if s.printer == PDF_PRINTER:
            pdf_path = self._ask_pdf_path(parent, title, s)
            if not pdf_path:
                return False
        try:
            printer, notes = build_printer(s, pdf_path)
            if s.system_dialog and not pdf_path:
                configure_printer(printer, s, content_height_mm=None if not s.is_roll else 300.0)
                dlg = QPrintDialog(printer, parent)
                if not dlg.exec():
                    return False
            # Always show the document outcome before printing (except silent POS / test).
            # Custom browser preview is consistent on Windows 10 and 11.
            if not force_no_preview and not pdf_path:
                return self._preview(parent, title, html, s, printer, activity)
            pages, more = render_to_printer(printer, html, s)
            notes += more
        except Exception as exc:
            log.exception("print failed")
            QMessageBox.critical(parent, "Printing failed", str(exc))
            self._log(activity, f"FAILED: {exc}"[:200])
            return False
        self._log(activity, f"{s.describe_paper()}; printer={'PDF' if pdf_path else s.printer or 'default'}; pages={pages}")
        if notes:
            QMessageBox.information(parent, "Printed with adjustments", "\n\n".join(dict.fromkeys(notes)))
        if pdf_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(pdf_path))
        return True

    def _preview(self, parent, title, html, s: PrintSettings, printer: QPrinter, activity: str) -> bool:
        """Show the actual document HTML, then print only if the user confirms.

        QPrintPreviewDialog is unreliable across Windows 10/11 drivers (blank or
        delayed pages). A document browser preview always shows the outcome first.
        """
        dlg = QDialog(parent)
        dlg.setWindowTitle("Document preview — " + title)
        dlg.resize(920, 780)
        if self.style:
            dlg.setStyleSheet(self.style)
        lay = QVBoxLayout(dlg)
        tip = QLabel(
            f"<b>{title}</b> · {s.describe_paper()} · "
            f"{'portrait' if s.orientation != 'landscape' else 'landscape'} · "
            "Review the document below, then print or cancel."
        )
        tip.setWordWrap(True)
        lay.addWidget(tip)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(html)
        browser.setMinimumHeight(560)
        lay.addWidget(browser, 1)
        buttons = QDialogButtonBox()
        print_btn = buttons.addButton("Print", QDialogButtonBox.AcceptRole)
        print_btn.setDefault(True)
        buttons.addButton(QDialogButtonBox.Cancel)
        lay.addWidget(buttons)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        if dlg.exec() != QDialog.Accepted:
            self._log(activity, f"preview cancelled; {s.describe_paper()}")
            return False
        try:
            pages, notes = render_to_printer(printer, html, s)
        except Exception as exc:
            log.exception("print after preview failed")
            QMessageBox.critical(parent, "Printing failed", str(exc))
            self._log(activity, f"FAILED after preview: {exc}"[:200])
            return False
        self._log(activity, f"printed after preview; {s.describe_paper()}; pages={pages}")
        if notes:
            QMessageBox.information(parent, "Printed with adjustments", "\n\n".join(dict.fromkeys(notes)))
        return True

    def _ask_pdf_path(self, parent, title, s: PrintSettings):
        folder = s.pdf_folder or str(Path.home() / "Documents")
        safe = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in title).strip() or "document"
        path, _ = QFileDialog.getSaveFileName(parent, "Save PDF", str(Path(folder) / f"{safe}.pdf"), "PDF files (*.pdf)")
        if not path:
            return None
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            s.pdf_folder = str(Path(path).parent)
            save_settings(self.store, s)
        except Exception:
            pass
        return path

    def _log(self, activity, detail):
        try:
            self.store.log(self.username or "user", "Printed", f"{activity} · {detail}"[:240])
        except Exception:
            pass
