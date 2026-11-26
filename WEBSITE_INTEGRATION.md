# 99 COMPUTERS website catalogue integration

## Recommended setup

In NextGen ERP, open **Administration → Integrations**, select **99 COMPUTERS · live site catalogue**, and use:

`https://www.99computers.co.ke/catalog.json`

Select **Test connection & sync catalogue**. The connector reads the public `catalog.json` feed over HTTPS. It maps the site's own fields (`id`, `name`, `cat`, `brand`, `price`, `sale`, `qty`, `desc`, and `images`) and resolves relative photo paths such as `/images/shop/example.jpg` to absolute HTTPS URLs.

## What appears in ERP

- Product name, brand, category and description
- Website regular price and promotional sale price (kept separately)
- Website availability/quantity when supplied
- SKU when the site supplies one
- Product thumbnail and link to the website product page
- A local cache timestamp

## Important boundaries

This is a **read-only catalogue mirror**. It does not update the live website, change local ERP stock, alter local costs or selling prices, or create a sale. Website availability is a reference from the last successful sync, not a guarantee of current stock. The cached list remains browsable offline; thumbnails already downloaded by the operating system/session may not be available offline unless cached by the network stack.

The integration is based on the uploaded site's current `catalog.json` shape. If the site's public feed structure or image paths change, update and test the adapter before relying on it. The integration does not synchronize website orders or customer records.

## Verification performed for this build

- Parsed the uploaded `catalog.json` and normalized all 60 product records.
- Confirmed relative product image paths become absolute `https://www.99computers.co.ke/images/shop/...` URLs.
- Confirmed regular and sale prices are retained separately, and cache replacement/readback works in an in-memory SQLite database.
- Compiled the Python source files successfully.
- The Qt interface and live network connection were not launched from this build environment; test **Test connection & sync catalogue** on the target Windows machine before production use.

## Catalogue UI and image caching update

- The main ERP window starts maximized, and the website catalogue opens maximized.
- Dashboard includes a direct **Browse 99 COMPUTERS Catalogue** shortcut.
- Data tables size columns to their contents and show horizontal scrollbars when a table is wider than the available window; this avoids silently trimming headings.
- Product images are downloaded on first view and stored under `NextGen_ERP/catalog_images/` using a SHA-256 filename derived from the image URL. The image cache is reused on subsequent views and while offline. Image downloads are limited to 12 MB each. If the folder cannot be written, images may still display for the current session but will not persist locally.
- On opening the catalogue, an administrator session attempts a quiet background refresh if the last successful catalogue sync is older than 12 hours. A failed refresh leaves the previous catalogue and cached images intact. Manual refresh remains available.
- Automatic refresh only compares/updates the local website catalogue mirror. It never writes product prices, stock quantities, orders, or any other data to the live website, and it does not treat website stock as ERP stock.
