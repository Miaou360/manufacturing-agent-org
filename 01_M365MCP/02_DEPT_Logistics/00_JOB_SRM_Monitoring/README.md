# 00_JOB_SRM_Monitoring — Order Dispatch & Truck Leveling

Downloads portal delivery schedules (`06:40` today, `13:40` tomorrow, plus manual `--slot PM --refresh`), projects 2-hour stock against morning ERP/WMS inventory, pulls whole boxes forward to prevent shortages and obey truck caps, and writes one 2-sheet Excel workbook (`Truck plan` + `2-hour detail`) per supplier.
