# Real change orders used in tests

Public documents from city and county meeting agendas, used unchanged.

| File | What it is | Source |
|---|---|---|
| `lawrence_co8.pdf` | City of Lawrence Utilities: EJCDC C-941 Change Order No. 08 (1 page; issue date next to an effective date; a $14,000 decrease; owner signature blank) | https://cityoflawrence.org/sites/default/files/attachments/JG%20Case%20-%20Change%20Order%20No.%208%20-%20Water%20System%20Improvements%20-%20Phase%20II%20Contract%20B.pdf |
| `marshall_mn_co10.pdf` | City of Marshall, MN: AIA G701 Change Order 10 (form + 7 pages of backup; 2 scanned) | https://mccmeetingspublic.blob.core.usgovcloudapi.net/marshall-meet-94541c331a6041098d3fed6d88be765b/ITEM-Attachment-001-9b8f38f2116c45528c027dceb815527f.pdf |
| `hidalgo_tx_co1.pdf` | Hidalgo County, TX: AIA G701 Change Order No. 1 (page 1 is a scan with a stamped text footer; net-zero change, +8 days; Exhibit A cost table; date-calculator printout) | https://public.destinyhosted.com/hidaldocs/2025/CC/20251110_5261/101389_Change_Order_No._1_to_C-24-0312-09-27.pdf |
| `sumter_county_fl_notice9.pdf` | Sumter County, FL: contractor's Change Order Request, Notice No. 9 (scanned with OCR text; subcontractor quote and drawing attached) | https://www.sumtercountyfl.gov/AgendaCenter/ViewFile/Item/26299?fileID=64718 |

The correct values are written in `tests/test_real_documents.py`, read by hand from page 1 of each document.
The full ground truth used by `cox evaluate` is in `ground_truth/<file name>.json` (with notes on how each value was read).
