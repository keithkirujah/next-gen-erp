from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from urllib.parse import urlsplit, urlunsplit


def _money(value):
    return f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def _pretty(value):
    """2026-11-02T04:32:00+00:00 -> 2026-11-02 04:32 (leave anything else untouched)."""
    text=str(value if value is not None else "")
    if len(text)>=16 and text[4:5]=="-" and text[10:11]=="T":
        return text[:10]+" "+text[11:16]
    return text


def normalize_website_url(value):
    text=str(value or "").strip()
    if not text:
        return ""
    if any(char.isspace() for char in text):
        raise ValueError("Enter a website URL without spaces.")
    if "://" not in text:
        text="https://"+text
    try:
        parsed=urlsplit(text)
        port=parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid website URL.") from exc
    if parsed.scheme.lower() not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public HTTP or HTTPS website URL without embedded credentials.")
    netloc=parsed.hostname.lower()
    if ":" in netloc and not netloc.startswith("["):
        netloc=f"[{netloc}]"
    if port is not None:
        netloc+=f":{port}"
    return urlunsplit((parsed.scheme.lower(),netloc,parsed.path,parsed.query,parsed.fragment))


def _website_html(business):
    try:
        url=normalize_website_url(business.get("website", ""))
    except ValueError:
        return ""
    if not url:
        return ""
    parsed=urlsplit(url)
    label=escape(parsed.netloc + parsed.path)
    return f"Website: <a href=\"{escape(url, quote=True)}\" style=\"color:#176B87;text-decoration:underline\">{label}</a>"


def mask_employee_code(code):
    """Mask middle digits of an employee code for privacy on receipts.

    EMP-1234  → EMP-**34
    EMP-00042 → EMP-***42
    Short codes are left mostly visible.
    """
    text = str(code or "").strip()
    if not text:
        return ""
    # Keep prefix (letters + hyphen) and last 2 characters; mask the middle
    if len(text) <= 4:
        return text[0] + "*" * (len(text) - 1) if len(text) > 1 else text
    prefix_end = 0
    for i, ch in enumerate(text):
        if ch.isalnum() and not ch.isdigit():
            prefix_end = i + 1
        elif ch in "-_":
            prefix_end = i + 1
        else:
            break
    if prefix_end < 2:
        prefix_end = min(3, len(text) - 2)
    visible_tail = 2
    middle_len = max(0, len(text) - prefix_end - visible_tail)
    return text[:prefix_end] + ("*" * middle_len) + text[-visible_tail:]


def first_name_only(full_name):
    """Return the first token of a display name for receipt privacy."""
    text = str(full_name or "").strip()
    if not text:
        return "Staff"
    return text.split()[0]


def _served_by_line(sale_or_meta):
    """Build the public 'You were served by …' line from sale fields or a meta dict.

    Accepts plain dicts and sqlite3.Row. Row supports keys()/[] but not .get(), so we
    normalise once instead of branching on hasattr(keys) which is true for both.

    Date priority: printed invoice_date (backdate) → created_at. The footer's date must
    match the DATE field on the invoice so a backdated document is consistent.
    """
    if sale_or_meta is None:
        name = code = when = ""
    elif isinstance(sale_or_meta, dict):
        name = sale_or_meta.get("served_by_name") or sale_or_meta.get("created_by") or ""
        code = sale_or_meta.get("served_by_code") or ""
        inv = str(sale_or_meta.get("invoice_date") or "").strip()
        when = inv or sale_or_meta.get("created_at") or ""
    else:
        # sqlite3.Row or any mapping-like object with [] access
        try:
            keys = set(sale_or_meta.keys()) if hasattr(sale_or_meta, "keys") else set()
            name = (sale_or_meta["served_by_name"] if "served_by_name" in keys else None) or (
                sale_or_meta["created_by"] if "created_by" in keys else None) or ""
            code = sale_or_meta["served_by_code"] if "served_by_code" in keys else ""
            inv = ""
            if "invoice_date" in keys:
                inv = str(sale_or_meta["invoice_date"] or "").strip()
            when = inv or (sale_or_meta["created_at"] if "created_at" in keys else "")
        except (TypeError, KeyError, IndexError):
            name = getattr(sale_or_meta, "served_by_name", "") or getattr(sale_or_meta, "created_by", "")
            code = getattr(sale_or_meta, "served_by_code", "")
            inv = str(getattr(sale_or_meta, "invoice_date", "") or "").strip()
            when = inv or getattr(sale_or_meta, "created_at", "")
    display = first_name_only(name)
    masked = mask_employee_code(code)
    date_part = _pretty(when)
    if masked:
        return f"You were served by {display} ({masked})" + (f" on {date_part}" if date_part else "")
    return f"You were served by {display}" + (f" on {date_part}" if date_part else "")


def _business(store):
    return {
        "name": store.setting("business_name", "Your Business").strip() or "Your Business",
        "tagline": store.setting("business_tagline", ""),
        "address": store.setting("business_address", ""),
        "phone": store.setting("business_phone", ""),
        "email": store.setting("business_email", ""),
        "website": store.setting("business_website", ""),
        "tax_id": store.setting("business_tax_id", ""),
        "currency": store.setting("business_currency", "KES - Kenyan Shilling").split(" - ", 1)[0],
        "logo": store.setting("business_logo_b64", ""),
        "payment": store.setting("business_payment_details", ""),
        "footer": store.setting("business_invoice_footer", ""),
    }


def _multiline(text):
    return "<br>".join(escape(line) for line in str(text or "").splitlines() if line.strip())


def _logo_html(business, width):
    """<img> scaled to ``width`` keeping proportions (Qt needs both width and height to scale)."""
    b64=business.get("logo")
    if not b64:
        return ""
    import base64, struct
    try:
        head=base64.b64decode(b64[:48])
        w,h=struct.unpack(">II",head[16:24])
        height=max(1,round(width*h/w))
    except Exception:
        return ""
    return f"<img src='data:image/png;base64,{b64}' width='{width}' height='{height}'><br>"


def _roll_frame(store, title, meta, rows, totals, note="", extra="", narrow=False, invoice_watermark=False, served_line="", warranty=""):
    """Compact single-column layout for 58/76/80 mm receipt rolls (and any narrow paper)."""
    business=_business(store)
    base="7.5pt" if narrow else "8pt"
    head="10.5pt" if narrow else "12pt"
    logo_width=96 if narrow else 120
    # Strategic identity order: address → phone → email → website (if present) → tax ID
    identity = []
    for x in (business["address"], business["phone"], business["email"]):
        if x:
            identity.append(escape(x))
    website = _website_html(business)
    if website:
        identity.append(website)  # website sits with contact block, easy to find
    if business["tax_id"]:
        identity.append(escape("Tax ID: " + business["tax_id"]))
    ident = "<br>".join(identity)
    watermark_meta = (f"<meta name='nextgen-invoice-watermark' content='{escape(business['logo'], quote=True)}'>"
                      if invoice_watermark and business["logo"] else "")
    meta_html = "".join(
        "<tr><td width='36%' style='color:#444'>" + escape(str(label)) + "</td>"
        "<td width='64%' align='right'>" + escape(_pretty(value) or "—") + "</td></tr>"
        for label, value in meta if str(label) not in {"Reported fault", "Diagnosis", "Warranty / condition"}
    )
    item_rows = ""
    for r in rows:
        sku, desc, qty, unit, total = (str(x) for x in r)
        item_rows += ("<tr><td colspan='2'><b>" + escape(desc) + "</b></td></tr>"
                      "<tr><td style='color:#444'>" + escape(qty) + " x " + escape(unit) + "</td><td align='right'>" + escape(total) + "</td></tr>")
    total_rows = "".join("<tr><td><b>" + escape(str(l)) + "</b></td><td align='right'><b>" + escape(str(v)) + "</b></td></tr>" for l, v in totals)
    warranty_html = f"<p align='center'><b>Warranty:</b> {escape(warranty)}</p>" if warranty and warranty != "N/A" else ""
    served_html = f"<p align='center' style='font-size:7pt'>{escape(served_line)}</p>" if served_line else ""
    footer_text = _multiline(business["footer"]) if business["footer"] else (escape(note) if note else "Thank you for your business.")
    return f"""<html><head><meta charset='utf-8'>{watermark_meta}</head><body style="font-family:Arial,sans-serif;font-size:{base};color:#000">
    <div align='center'>{_logo_html(business, logo_width)}<div style='font-size:{head};font-weight:bold'>{escape(business['name'])}</div>
      <div>{escape(business['tagline'])}</div><div>{ident}</div></div>
      <hr/><div align='center'><b>{escape(title)}</b></div>
      <table width='100%' cellspacing='0' cellpadding='1'>{meta_html}</table><hr/>
      <table width='100%' cellspacing='0' cellpadding='1'>{item_rows}</table><hr/>
      <table width='100%' cellspacing='0' cellpadding='1'>{total_rows}</table>
      {warranty_html}
      {served_html}
      {('<div align="center"><b>How to pay</b><br>' + _multiline(business['payment']) + '</div>') if business['payment'] else ''}
      <p align='center'>{footer_text}</p>
      {extra}
      <hr style='border:none;border-top:1px dashed #999;margin-top:8px'/>
      <p align='center' style='font-size:6pt;color:#666;margin:4px 0 0'>Generated by NextGen ERP · Not an eTIMS fiscal receipt</p>
      </body></html>"""


def _frame(store, title, meta, rows, totals, note="", extra="", layout="document", invoice_watermark=False,
           served_line="", warranty=""):
    if layout == "receipt":
        return _roll_frame(store, title, meta, rows, totals, note, extra, invoice_watermark=invoice_watermark,
                           served_line=served_line, warranty=warranty)
    business = _business(store)
    # Accent matches the brand link colour used elsewhere; keeps print readable in B/W too.
    accent = "#176B87"
    accent_dark = "#0F4C5C"
    identity_bits = []
    for x in (business["address"], business["phone"], business["email"]):
        if x:
            identity_bits.append(escape(x))
    website = _website_html(business)
    if website:
        identity_bits.append(website)
    if business["tax_id"]:
        identity_bits.append(escape("Tax ID · " + business["tax_id"]))
    identity = " &nbsp;·&nbsp; ".join(identity_bits)
    watermark_meta = (f"<meta name='nextgen-invoice-watermark' content='{escape(business['logo'], quote=True)}'>"
                      if invoice_watermark and business["logo"] else "")
    # Line items — zebra striping via inline styles (QTextDocument CSS is limited)
    table_rows = []
    for i, r in enumerate(rows):
        bg = "#F7FAFB" if i % 2 else "#FFFFFF"
        table_rows.append(
            f"<tr style='background:{bg}'>"
            f"<td style='font-family:Consolas,monospace;font-size:8pt;color:#445'>{escape(str(r[0]))}</td>"
            f"<td style='font-weight:600'>{escape(str(r[1]))}</td>"
            f"<td class='num'>{escape(str(r[2]))}</td>"
            f"<td class='num'>{escape(str(r[3]))}</td>"
            f"<td class='num' style='font-weight:600'>{escape(str(r[4]))}</td></tr>"
        )
    table_body = "".join(table_rows) if table_rows else (
        "<tr><td colspan='5' style='text-align:center;color:#888;padding:10px'>No line items</td></tr>"
    )
    # Totals — last row (usually Total / Balance) gets accent weight
    total_rows = []
    for idx, (label, value) in enumerate(totals):
        is_last = idx == len(totals) - 1
        is_emphasis = str(label).upper() in {"TOTAL", "BALANCE DUE", "BALANCE"} or is_last
        if is_emphasis:
            total_rows.append(
                f"<tr><td colspan='4' style='text-align:right;padding:5px 10px 5px 0;font-size:10pt;"
                f"font-weight:700;color:{accent_dark};border-top:2px solid {accent}'>{escape(str(label))}</td>"
                f"<td class='num' style='padding:5px 6px;font-size:10pt;font-weight:700;color:{accent_dark};"
                f"border-top:2px solid {accent}'>{escape(str(value))}</td></tr>"
            )
        else:
            total_rows.append(
                f"<tr><td colspan='4' style='text-align:right;padding:3px 10px 3px 0;color:#555'>{escape(str(label))}</td>"
                f"<td class='num' style='padding:3px 6px;color:#222'>{escape(str(value))}</td></tr>"
            )
    # Meta grid — two columns of label/value cards (compact for single-page fit)
    meta_cells = []
    for label, value in meta:
        meta_cells.append(
            f"<td style='width:50%;vertical-align:top;padding:4px 8px'>"
            f"<div style='font-size:7pt;letter-spacing:0.05em;text-transform:uppercase;color:#6A7A80;"
            f"font-weight:700'>{escape(str(label))}</div>"
            f"<div style='font-size:9.5pt;color:#1A1A1A;font-weight:600;margin-top:1px'>"
            f"{escape(_pretty(value) or '—')}</div></td>"
        )
    meta_rows = []
    for i in range(0, len(meta_cells), 2):
        pair = meta_cells[i:i + 2]
        if len(pair) == 1:
            pair.append("<td style='width:50%'></td>")
        meta_rows.append("<tr>" + "".join(pair) + "</tr>")
    metadata = "".join(meta_rows)
    warranty_block = (
        f"<div style='margin-top:8px;padding:6px 10px;background:#F0F7F9;border-left:3px solid {accent};"
        f"border-radius:0 4px 4px 0;font-size:9pt'><b style='color:{accent_dark}'>Warranty</b>"
        f"<span style='color:#333'> · {escape(warranty)}</span></div>"
        if warranty and warranty != "N/A" else ""
    )
    served_block = (
        f"<p style='font-size:8.5pt;color:#445;margin:8px 0 0'>{escape(served_line)}</p>"
        if served_line else ""
    )
    payment_block = (
        f"<div style='margin-top:8px;padding:8px 10px;background:#FAFBFC;border:1px solid #E2E8EB;border-radius:6px'>"
        f"<div style='font-size:7.5pt;letter-spacing:0.05em;text-transform:uppercase;color:#6A7A80;font-weight:700;"
        f"margin-bottom:2px'>How to pay</div>{_multiline(business['payment'])}</div>"
        if business["payment"] else ""
    )
    footer_block = (
        f"<p style='font-size:8.5pt;color:#444;margin-top:8px'>{_multiline(business['footer'])}</p>"
        if business["footer"] else ""
    )
    note_block = f"<p class='note'>{escape(note)}</p>" if note else ""
    # Compact document layout: designed to keep typical invoices on a single A4/Letter page.
    return f"""<!doctype html><html><head><meta charset='utf-8'>{watermark_meta}<style>
      body {{ font-family: 'Segoe UI', Arial, Helvetica, sans-serif; color:#1A1A1A; font-size:9pt; margin:0; padding:0 2px; }}
      .header {{ text-align:center; padding-bottom:6px; border-bottom:2px solid {accent}; }}
      .brand {{ font-size:16pt; font-weight:700; margin:2px 0 1px; color:#0F4C5C; letter-spacing:-0.02em; }}
      .tagline {{ color:#5A6A70; font-size:8pt; margin:0 0 2px; font-style:italic; }}
      .identity {{ color:#4A5A60; font-size:7.5pt; line-height:1.3; }}
      .doc-title {{ font-size:11pt; font-weight:700; color:#FFFFFF; background:{accent};
                   padding:6px 10px; margin:8px 0 0; letter-spacing:0.04em; text-transform:uppercase;
                   border-radius:4px 4px 0 0; }}
      .meta-wrap {{ border:1px solid #D8E2E6; border-top:none; border-radius:0 0 4px 4px;
                   background:#FFFFFF; margin-bottom:8px; }}
      table.items {{ width:100%; border-collapse:collapse; margin-top:2px; }}
      table.items thead th {{ background:{accent}; color:#FFFFFF; font-size:7.5pt; font-weight:700;
                             letter-spacing:0.03em; text-transform:uppercase; padding:5px 6px;
                             text-align:left; border:none; }}
      table.items thead th.num {{ text-align:right; }}
      table.items td {{ padding:4px 6px; border-bottom:1px solid #E8EEF0; vertical-align:top; font-size:8.5pt; }}
      .num {{ text-align:right; white-space:nowrap; }}
      table.totals {{ width:100%; border-collapse:collapse; margin-top:2px; }}
      .note {{ font-size:8pt; color:#555; margin-top:6px; padding:5px 8px; background:#FFF8E8;
               border-left:3px solid #E6B800; border-radius:0 4px 4px 0; }}
      .gen {{ font-size:6.5pt; color:#9AA6AB; margin-top:8px; border-top:1px solid #E2E8EB;
              padding-top:4px; text-align:center; }}
      a {{ color:{accent}; }}
      </style></head><body>
    <div class='header'>
      {_logo_html(business, 88)}
      <div class='brand'>{escape(business['name'])}</div>
      {f"<div class='tagline'>{escape(business['tagline'])}</div>" if business['tagline'] else ""}
      <div class='identity'>{identity}</div>
    </div>
    <div class='doc-title'>{escape(title)}</div>
    <div class='meta-wrap'><table width='100%' cellspacing='0' cellpadding='0'>{metadata}</table></div>
    <table class='items' cellspacing='0' cellpadding='0'>
      <thead><tr>
        <th style='width:16%'>SKU</th>
        <th>Item</th>
        <th class='num' style='width:10%'>Qty</th>
        <th class='num' style='width:15%'>Unit price</th>
        <th class='num' style='width:15%'>Line total</th>
      </tr></thead>
      <tbody>{table_body}</tbody>
    </table>
    <table class='totals' cellspacing='0' cellpadding='0'>{''.join(total_rows)}</table>
    {warranty_block}
    {served_block}
    <div style='margin-top:4px'>{extra}</div>
    {payment_block}
    {footer_block}
    {note_block}
    <p class='gen'>Generated by NextGen ERP · Sales tax uses the administrator's configured rate. This is not an eTIMS fiscal tax invoice.</p>
    </body></html>"""


def render_invoice(store, sale_id, layout="document"):
    sale = store.operations.conn.execute("SELECT * FROM sales WHERE id=?", (int(sale_id),)).fetchone()
    if not sale:
        raise ValueError("The selected sale no longer exists.")
    rows = store.operations.conn.execute("SELECT * FROM sale_lines WHERE sale_id=? ORDER BY id", (int(sale_id),)).fetchall()
    currency = _business(store)["currency"]
    items = [[r["sku"], r["description"], r["quantity"],
              f"{currency} {_money(r['unit_price'])}",
              f"{currency} {_money(Decimal(r['line_total']) + Decimal(r['tax_amount'] or '0.00'))}"] for r in rows]
    net = Decimal(sale["total"])
    paid = Decimal(sale["paid_total"])
    balance = net - paid
    totals = [("Subtotal", f"{currency} {_money(sale['subtotal'])}")]
    if Decimal(sale["tax_rate"]) > 0:
        totals.append((f"{sale['tax_label']} ({_money(sale['tax_rate'])}%)", f"{currency} {_money(sale['tax_total'])}"))
    totals.append(("Total", f"{currency} {_money(net)}"))
    totals.append(("Paid", f"{currency} {_money(paid)}"))
    if balance > 0:
        totals.append(("BALANCE DUE", f"{currency} {_money(balance)}"))
    else:
        totals.append(("Balance", f"{currency} {_money(balance)}"))
    returns = store.operations.sales_returns(sale_id)
    extra = ""
    if returns:
        parts = "".join("<li>" + escape(r["return_number"]) + " · " + escape(r["reason"]) + " · refund " +
                        escape(currency) + " " + escape(_money(r["refund_amount"])) + "</li>" for r in returns)
        extra = "<h3>Returns and refunds</h3><ul>" + parts + "</ul>"
    # Title reflects credit vs paid
    is_credit = balance > 0
    title = "CREDIT SALE INVOICE" if is_credit else "SALE INVOICE / PAYMENT RECEIPT"
    if sale["status"] != "Completed":
        title += " · " + sale["status"].upper()
    # Printed date: explicit invoice_date (backdate) wins over system created_at
    try:
        inv = str(sale["invoice_date"] or "").strip()
    except (KeyError, IndexError, TypeError):
        inv = ""
    sale_date = _pretty(inv) if inv else _pretty(sale["created_at"])
    warranty = ""
    try:
        warranty = str(sale["warranty_period"] or "N/A")
    except (KeyError, IndexError):
        warranty = "N/A"
    payment_method = ""
    try:
        payment_method = str(sale["payment_method"] or "")
    except (KeyError, IndexError):
        payment_method = ""
    meta = [
        ("Invoice", sale["sale_number"]),
        ("Customer", sale["customer_name"]),
        ("Date", sale_date),
        ("Status", sale["status"]),
        ("Payment", sale["payment_status"] + (f" · {payment_method}" if payment_method else "")),
    ]
    if warranty and warranty != "N/A":
        meta.append(("Warranty", warranty))
    served_line = _served_by_line(sale)
    note = ""
    if is_credit:
        note = f"This is a credit sale. Balance of {currency} {_money(balance)} remains due."
    return title, _frame(
        store, title, meta, items, totals, note=note, extra=extra, layout=layout,
        invoice_watermark=True, served_line=served_line, warranty=warranty,
    )


def render_quote(store, quote_id, layout="document"):
    quote=store.operations.conn.execute("SELECT * FROM quotes WHERE id=?",(int(quote_id),)).fetchone()
    if not quote:
        raise ValueError("The selected quotation no longer exists.")
    lines=store.operations.quote_lines(quote_id)
    currency=_business(store)["currency"]
    rows=[[r["sku"],r["description"],r["quantity"],f"{currency} {_money(r['unit_price'])}",f"{currency} {_money(Decimal(r['line_total'])+Decimal(r['tax_amount'] or '0.00'))}"] for r in lines]
    title="SALES QUOTATION"
    totals=[("Subtotal",f"{currency} {_money(quote['subtotal'])}")]
    if Decimal(quote["tax_rate"]) > 0:
        totals.append((f"{quote['tax_label']} ({_money(quote['tax_rate'])}%)",f"{currency} {_money(quote['tax_total'])}"))
    totals.append(("Quotation total",f"{currency} {_money(quote['total'])}"))
    meta=[("Quotation",quote["quote_number"]),("Customer",quote["customer_name"]),("Created",quote["created_at"]),("Valid through",quote["expires_on"] or "Not specified"),("Status",quote["status"])]
    return title,_frame(store,title,meta,rows,totals,note="This quotation does not reserve inventory. Prices and availability may change until accepted and converted.",layout=layout)


def render_purchase_order(store, order_id, layout="document"):
    order=store.operations.conn.execute("SELECT * FROM purchase_orders WHERE id=?",(int(order_id),)).fetchone()
    if not order:
        raise ValueError("The selected purchase order no longer exists.")
    currency=_business(store)["currency"]
    lines=store.operations.purchase_order_lines(order_id)
    rows=[]
    total=Decimal("0.00")
    for line in lines:
        amount=(Decimal(line["quantity"])*Decimal(line["unit_cost"])).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
        total+=amount
        rows.append([line["sku"],line["description"],line["quantity"],f"{currency} {_money(line['unit_cost'])}",f"{currency} {_money(amount)}"])
    title="PURCHASE ORDER"
    meta=[("Order",order["order_number"]),("Supplier",order["supplier_name"]),("Created",order["created_at"]),("Status",order["status"])]
    received_total=sum((Decimal(r["paid_total"]) for r in store.operations.purchases() if r["order_id"]==int(order_id)),Decimal("0"))
    totals=[("Order value",f"{currency} {_money(total)}")]
    if received_total:
        totals.append(("Paid across receipts",f"{currency} {_money(received_total)}"))
    return title,_frame(store,title,meta,rows,totals,note="Goods received should be recorded in the ERP when physically delivered.",layout=layout)


def render_goods_received(store, purchase_id, layout="document"):
    purchase=store.operations.conn.execute("SELECT * FROM purchases WHERE id=?",(int(purchase_id),)).fetchone()
    if not purchase:
        raise ValueError("The selected goods receipt no longer exists.")
    currency=_business(store)["currency"]
    lines=store.operations.conn.execute("SELECT * FROM purchase_lines WHERE purchase_id=? ORDER BY id",(int(purchase_id),)).fetchall()
    rows=[[r["sku"],r["description"],r["quantity"],f"{currency} {_money(r['unit_cost'])}",f"{currency} {_money(r['line_total'])}"] for r in lines]
    title="GOODS RECEIVED NOTE"
    order=store.operations.conn.execute("SELECT order_number FROM purchase_orders WHERE id=?",(purchase["order_id"],)).fetchone() if purchase["order_id"] else None
    meta=[("Receipt",purchase["purchase_number"]),("Purchase order",order["order_number"] if order else "Unplanned receipt"),("Supplier",purchase["supplier_name"]),("Date",purchase["created_at"]),("Status",purchase["status"])]
    totals=[("Received value",f"{currency} {_money(purchase['total'])}"),("Paid now",f"{currency} {_money(purchase['paid_total'])}")]
    return title,_frame(store,title,meta,rows,totals,note="Goods-received record generated from the quantities entered in the ERP.",layout=layout)


def render_repair_job(store, job_id, layout="document"):
    job=store.operations.conn.execute("SELECT * FROM repair_jobs WHERE id=?",(int(job_id),)).fetchone()
    if not job:
        raise ValueError("The selected repair ticket no longer exists.")
    currency=_business(store)["currency"]
    parts=store.operations.repair_job_parts(job_id)
    rows=[[r["sku"],r["description"],r["quantity"],f"{currency} {_money(r['unit_price'])}",f"{currency} {_money(r['line_total'])}"] for r in parts]
    if not rows:
        rows=[["—","No parts issued", "—", "—", "—"]]
    subtotal=Decimal(job["subtotal"])
    if job["status"] not in {"Ready","Collected"}:
        subtotal=Decimal(job["labor_amount"])+sum((Decimal(r["line_total"]) for r in parts),Decimal("0.00"))
    parts_total=sum((Decimal(r["line_total"]) for r in parts),Decimal("0.00"))
    totals=[("Pre-tax estimate",f"{currency} {_money(job['estimate_amount'])}"),("Labor",f"{currency} {_money(job['labor_amount'])}"),("Parts",f"{currency} {_money(parts_total)}"),("Subtotal",f"{currency} {_money(subtotal)}")]
    if Decimal(job["tax_rate"])>0:
        totals.append((f"{job['tax_label']} ({_money(job['tax_rate'])}%)",f"{currency} {_money(job['tax_total'])}"))
    totals.extend([("Final total",f"{currency} {_money(job['total'])}"),("Paid",f"{currency} {_money(job['paid_total'])}"),("Balance",f"{currency} {_money(Decimal(job['total'])-Decimal(job['paid_total']))}")])
    events=store.operations.repair_job_events(job_id)
    event_html="<h3>Service history</h3><ul>"+"".join("<li>"+escape(e["created_at"]+" · "+e["event"]+" · "+e["details"]+" · "+e["actor"])+"</li>" for e in events)+"</ul>"
    meta=[("Ticket",job["ticket_number"]),("Customer",job["customer_name"]),("Device",job["device"]),("Serial number",job["serial_number"]),("Status",job["status"]),("Estimate approval",job["approval_status"]),("Technician",job["technician"]),("Received",job["created_at"]),("Reported fault",job["reported_fault"]),("Diagnosis",job["diagnosis"]),("Warranty / condition",job["warranty_notes"])]
    title="REPAIR JOB SHEET"
    return title,_frame(store,title,meta,rows,totals,note="Customer approval, parts usage, and collection status are recorded in the local repair history.",extra=event_html,layout=layout)


def render_record(store, module, record_id, created_at, data, field_names, layout="document"):
    """Printable view of a custom-module / workspace record (label/value pairs)."""
    business=_business(store)
    rows=[(str(n), str(data.get(n,""))) for n in field_names]
    if layout=="receipt":
        ident="<br>".join(escape(x) for x in (business["address"],business["phone"],business["email"]) if x)
        body="".join("<tr><td style='color:#444'>"+escape(n)+"</td><td align='right'>"+escape(v)+"</td></tr>" for n,v in rows)
        return (f"<html><body style='font-family:Arial;font-size:8.5pt'><div align='center'><b style='font-size:12pt'>{escape(business['name'])}</b><br>{ident}</div><hr/>"
                f"<div align='center'><b>{escape(module.upper())} · #{int(record_id)}</b><br>{escape(str(created_at))}</div><hr/>"
                f"<table width='100%' cellspacing='0' cellpadding='1'>{body}</table><hr/><p align='center'>Thank you.</p></body></html>")
    body="".join("<tr><td>"+escape(n)+"</td><td>"+escape(v)+"</td></tr>" for n,v in rows)
    return (f"<html><head><meta charset='utf-8'><style>body{{font-family:Arial;font-size:10pt}}td,th{{padding:5px 4px;border-bottom:1px solid #ddd;text-align:left}}th{{background:#eee}}</style></head><body>"
            f"<h1 style='font-size:20pt;margin:0'>{escape(business['name'])}</h1><div style='color:#555'>{escape(business['address'])}<br>{escape(business['phone'])} {escape(business['email'])}</div><hr>"
            f"<h2>{escape(module.upper())} RECORD</h2><p><b>Record:</b> #{int(record_id)} &nbsp; <b>Created:</b> {escape(str(created_at))}</p>"
            f"<table width='100%' cellspacing='0'><thead><tr><th>Field</th><th>Value</th></tr></thead><tbody>{body}</tbody></table>"
            f"<p style='color:#555;font-size:9pt'>Printed from NextGen ERP</p></body></html>")


def render_shift_report(store, summary, layout="document"):
    """Z-report / end-of-shift report for a ShiftService.summary() result."""
    business=_business(store); cur=business["currency"]; shift=summary["shift"]
    def row(label,value,bold=False):
        b=("<b>","</b>") if bold else ("","")
        return f"<tr><td>{b[0]}{escape(str(label))}{b[1]}</td><td align='right'>{b[0]}{escape(str(value))}{b[1]}</td></tr>"
    lines=[row("Shift #",shift["id"]),row("Opened",_pretty(shift["opened_at"])+" by "+shift["opened_by"]),
           row("Closed",(_pretty(shift["closed_at"])+" by "+shift["closed_by"]) if shift["closed_at"] else "still open")]
    sales=[row("Sales made",summary["sales_count"]),row("Gross sales",f"{cur} {_money(summary['gross_sales'])}",True),
           row("  of which tax",f"{cur} {_money(summary['tax_total'])}"),row("  net of tax",f"{cur} {_money(summary['net_sales'])}"),
           row("Sold on credit (unpaid)",f"{cur} {_money(summary['credit_sales'])}"),row("Refunds paid out",f"{cur} {_money(summary['refunds'])}")]
    takings=[row(k,f"{cur} {_money(v)}") for k,v in summary["money_in"].items()] or [row("No takings","—")]
    outs=[row(k,f"{cur} {_money(v)}") for k,v in summary["money_out"].items()] or [row("None","—")]
    cash=[row("Opening float",f"{cur} {_money(summary['opening_float'])}"),row("Expected cash in till",f"{cur} {_money(summary['expected_cash'])}",True)]
    if summary["counted_cash"] is not None:
        var=summary["variance"]; flag="OVER" if var>0 else "SHORT" if var<0 else "BALANCED"
        cash+=[row("Counted cash",f"{cur} {_money(summary['counted_cash'])}",True),row(f"Variance ({flag})",f"{cur} {_money(var)}",True)]
    size="8.5pt" if layout=="receipt" else "10pt"
    def block(title,rows_):
        return f"<h4 style='margin:10px 0 2px'>{escape(title)}</h4><table width='100%' cellspacing='0' cellpadding='2'>{''.join(rows_)}</table>"
    ident="<br>".join(escape(x) for x in (business["address"],business["phone"]) if x)
    html=(f"<html><head><meta charset='utf-8'></head><body style='font-family:Arial;font-size:{size}'>"
          f"<div align='center'>{_logo_html(business,150 if layout=='receipt' else 170)}<b style='font-size:13pt'>{escape(business['name'])}</b><br>{ident}</div><hr/>"
          f"<div align='center'><b>Z-REPORT · END OF SHIFT</b></div>"
          +block("Shift",lines)+block("Sales",sales)+block("Takings by method",takings)+block("Money out",outs)+block("Cash drawer",cash)
          +"<br><br>Signed: ____________________ &nbsp; Checked by: ____________________"
          +f"<p style='color:#666;font-size:7pt'>NextGen ERP · printed {datetime.now():%Y-%m-%d %H:%M}</p></body></html>")
    return "Z-Report #%s" % shift["id"], html


def render_student_report_card(store, student_id, term_id, layout="document"):
    report=store.operations.student_term_report(student_id,term_id)
    student,term=report["student"],report["term"]
    business=_business(store)
    size="8pt" if layout=="receipt" else "10pt"
    logo_width=100 if layout=="receipt" else 150
    result_rows=[]
    for row in report["results"]:
        score="Not recorded" if row["score"] is None else f"{_money(row['score'])} / {_money(row['max_score'])}"
        result_rows.append("<tr><td>"+escape(row["subject"])+"</td><td>"+escape(row["name"])+"</td><td align='right'>"+
            escape(score)+"</td><td>"+escape(row["grade"] or "—")+"</td><td>"+escape(row["remarks"] or "")+"</td></tr>")
    if not result_rows:
        result_rows.append("<tr><td colspan='5' align='center'>No assessments recorded for this term.</td></tr>")
    average="Not available" if report["average_percent"] is None else f"{report['average_percent']:.1f}%"
    attendance=report["attendance"]
    present=attendance.get("Present",0)+attendance.get("Late",0)
    total_attendance=sum(attendance.values())
    identity="<br>".join(escape(value) for value in (business["address"],business["phone"],business["email"]) if value)
    title="STUDENT REPORT CARD"
    html=(f"<html><head><meta charset='utf-8'><style>body{{font-family:Arial,sans-serif;font-size:{size};color:#202020}}"
          "table{width:100%;border-collapse:collapse}th,td{padding:5px;border-bottom:1px solid #bbb;text-align:left}th{background:#eee}"
          "</style></head><body><div align='center'>"+_logo_html(business,logo_width)+
          f"<b style='font-size:14pt'>{escape(business['name'])}</b><br>{identity}</div><hr/>"
          f"<h2 align='center'>{title}</h2><table><tr><td><b>Student</b></td><td>{escape(student['full_name'])}</td><td><b>Admission</b></td><td>{escape(student['admission_number'])}</td></tr>"
          f"<tr><td><b>Class</b></td><td>{escape(student['class_course'])}</td><td><b>Academic year</b></td><td>{escape(term['academic_year'])}</td></tr>"
          f"<tr><td><b>Term</b></td><td>{escape(term['name'])}</td><td><b>Period</b></td><td>{escape(term['start_date'])} – {escape(term['end_date'])}</td></tr></table>"
          "<h3>Academic results</h3><table><thead><tr><th>Subject</th><th>Assessment</th><th>Score</th><th>Grade</th><th>Remarks</th></tr></thead><tbody>"+
          "".join(result_rows)+"</tbody></table><h3>Term summary</h3><table>"
          f"<tr><td>Average of scored assessments</td><td>{average}</td><td>Attendance recorded</td><td>{present}/{total_attendance}</td></tr>"
          f"<tr><td>Fees charged</td><td>{business['currency']} {_money(report['fees_charged'])}</td><td>Fees allocated</td><td>{business['currency']} {_money(report['fees_paid'])}</td></tr>"
          f"</table><p style='margin-top:24px'>Class teacher: ____________________ &nbsp; Head of school: ____________________</p>"
          "<p style='color:#666;font-size:8pt'>Generated from recorded results and attendance. This report does not replace an approved official transcript.</p></body></html>")
    return title,html


def render_student_transcript(store, student_id):
    student=store.operations.conn.execute("SELECT * FROM academic_students WHERE id=?",(int(student_id),)).fetchone()
    if not student:
        raise ValueError("The selected student no longer exists.")
    rows=store.operations.student_transcript(student_id)
    body="".join("<tr><td>"+escape(row["term"])+"</td><td>"+escape(row["assessment_date"])+"</td><td>"+
        escape(row["subject"])+"</td><td>"+escape(row["name"])+"</td><td align='right'>"+
        escape(f"{_money(row['score'])} / {_money(row['max_score'])}")+"</td><td>"+escape(row["grade"] or "—")+"</td></tr>" for row in rows)
    if not body:
        body="<tr><td colspan='6' align='center'>No recorded results.</td></tr>"
    business=_business(store)
    identity="<br>".join(escape(value) for value in (business["address"],business["phone"],business["email"]) if value)
    html=("<html><head><meta charset='utf-8'><style>body{font-family:Arial,sans-serif;font-size:10pt;color:#202020}"
          "table{width:100%;border-collapse:collapse}th,td{padding:6px;border-bottom:1px solid #bbb;text-align:left}th{background:#eee}</style></head><body>"
          f"<div align='center'>{_logo_html(business,150)}<b style='font-size:14pt'>{escape(business['name'])}</b><br>{identity}</div><hr/>"
          f"<h2 align='center'>OFFICIAL ACADEMIC TRANSCRIPT</h2><p><b>Student:</b> {escape(student['full_name'])} &nbsp; <b>Admission:</b> {escape(student['admission_number'])}<br>"
          f"<b>Class / programme:</b> {escape(student['class_course'])}</p><table><thead><tr><th>Term</th><th>Date</th><th>Subject</th><th>Assessment</th><th>Score</th><th>Grade</th></tr></thead><tbody>{body}</tbody></table>"
          f"<p style='margin-top:24px'>Registrar: ____________________ &nbsp; Date: ____________________</p><p style='color:#666;font-size:8pt'>Generated {datetime.now():%Y-%m-%d %H:%M} from recorded academic results.</p></body></html>")
    return "Academic Transcript",html
