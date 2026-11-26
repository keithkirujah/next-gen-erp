# NextGen ERP — Quick guide for shop owners

## Your first day
1. **System Settings → Business**: enter your business name, tagline, address, phone, email, tax number, upload your **logo**, add M-Pesa/bank **payment details** and an **invoice footer**.
2. Set **tax**: enter your rate (16% VAT) and tick **"Prices already include tax"** if your shelf prices already contain VAT.
3. **Printer setup** (button in Settings and on the POS): pick your printer and paper — A4 sheets, or a 58/80 mm receipt roll. Print the test page once. On the POS, tick **Receipt** to print each completed sale automatically; untick it to stop.
4. **Database and Backup**: choose a backup folder on a USB drive or Google Drive/OneDrive folder. Real-time Backups are ON by default; pick how often. You will see a notice after every backup.
5. Add products (or import a CSV in Inventory) and start selling.

## School / training centre
1. Select **School / training centre** under Business profile, then open **Students & Enrolment**.
2. In **Academic setup**, create the academic year, its terms, and class/course sections. Set each class capacity and homeroom teacher before enrollment.
3. In **Admissions**, enter applications, record review decisions, and enroll accepted applicants. Enrollment can assign the student to an active class section; capacity limits are enforced.
4. Use **Students & admissions** for the student register, **Guardians & contact log** for linked family contacts and follow-up history, and **Attendance** for daily attendance.
5. In **Class timetable**, add weekly periods. The system blocks overlapping class, teacher, and room bookings. **Assessments & results** records marks against the configured terms and classes.
6. **Report cards & transcripts** generates term summaries and cumulative transcripts for printing or CSV export. **Fee accounts** records charges and payments in the shared cash ledger.

Guardian communications are currently recorded as a staff contact log. The app does not send SMS, email, or portal messages, and M-Pesa entries are references rather than live payment confirmations.

## Every day
- **Open shift** on the POS with your cash float. **Close shift** at night: count the cash, and the system shows over/short and prints the **Z-report**.
- The **Dashboard Pulse card** shows today's sales, your daily goal, the last 14 days, your selling streak and a useful insight. Click the ring to set a goal.
- **F6** holds a sale while you serve someone else, **F7** recalls it. **F9** completes the sale.
- **Ctrl+F** jumps straight to record search. **Ctrl+K** opens the command palette: type a module or task (sale, customer, receive stock, repair intake, add product, expense, reports, backup, printer…) and press Enter. It only lists destinations your account can access. **Ctrl+L** locks the screen.
- **Reports** has ten ready reports (sales by day, best sellers, takings by payment method, tax collected, profit, unpaid invoices, stock value, reorder list, expenses, shifts). Export any to CSV or print it.

## If something goes wrong
- Backups failing? A red notice appears with a **Retry now** button. Check the backup folder is connected.
- Anything strange: Database and Backup → **Open log folder** and send `nextgen_erp.log` to support.
- Forgot to close the app? It saves a final backup when you close it.

## Good to know (limits of this version)
- Single till per business; one shift open at a time.
- No KRA eTIMS integration yet — receipts say "not a fiscal receipt".
- No POS discounts, no cash-drawer kick command, no M-Pesa STK push yet.
