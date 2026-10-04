# Acme Corp - Accounts Payable (AP) handbook (excerpt)

## Systems
- Vendor emails/invoices arrive as documents in the `inbox/` folder (file timestamps are unreliable because the folder is synced;
  always use the date written inside the document).
- Internal bills system ("Acme Payables" ERP): {{ERP_URL}}  (sandbox)
  Login: username `demo`, password `{{secret:ERP_PASSWORD}}`  (always type the placeholder literally; the tool layer substitutes the real value)

## Procedure: entering a vendor invoice into the ERP
1. Find the vendor's most recent *invoice* in the inbox. Reminders, statements and price lists are not invoices.
2. Before entering anything, check the ERP bills list for an existing bill with the same vendor + invoice number. Never create duplicates;
   if it already exists, do not enter it again - report that to the requester.
3. Amount = the total payable on the invoice (including tax/shipping), entered as a plain number with no commas or symbols (e.g. 1284.50).
   Choose the matching currency in the Currency dropdown (it defaults to USD - check it).
4. Due date: use the explicit due date if the invoice states one. If it only states payment terms (e.g. "Net 30"),
   compute it from the invoice date. Enter as YYYY-MM-DD.
5. After saving, confirm the new bill appears in the bills list with the right vendor, invoice number, amount, currency and due date.
6. Never mark a bill as paid unless the requester explicitly asks for that.
7. Report back: vendor, invoice number, amount + currency, due date and the ERP bill ID.
