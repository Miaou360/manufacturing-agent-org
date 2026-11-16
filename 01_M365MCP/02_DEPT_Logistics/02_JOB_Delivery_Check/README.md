# 02_JOB_Delivery_Check — 10-Minute Per-Truck Receipt Tracking

Polls warehouse `101` goods-receipt barcodes every 10 minutes around active truck windows, freezes planned loads at departure in `delivery_check.db`, closes settled trucks, and generates supplier `SRM_DeliveryStatus` workbooks (`Plan` vs `Delivered` rows, red shortfalls, and plain-English pre-cutoff `Warning` text).
