# 02_JOB_Delivery_Check — 10-Minute Per-Truck Receipt Tracking

Polls warehouse `101` goods-receipt barcodes every 10 minutes (`07:40` through `05:55` next-day rollover, with a `240`-minute night arrival cap and silent prior-day catchup), tracks scheduled Class C trucks plus day-level Class L pallet items across all SRM suppliers in `delivery_check.db`, and generates supplier `SRM_DeliveryStatus` workbooks (`Plan` vs `Delivered` rows, red shortfalls, and plain-English pre-cutoff `Warning` text).
