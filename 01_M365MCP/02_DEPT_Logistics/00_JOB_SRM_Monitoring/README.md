# 00_JOB_SRM_Monitoring — Order Dispatch & Truck Leveling

Downloads portal delivery schedules (`06:40` today, `13:40` tomorrow, plus manual `--slot PM --refresh`), projects 2-hour stock against morning ERP/WMS inventory, levels all trucks per day within each supplier's trailer cart cap (`33` or `60` carts, absorbing non-truck slot orders and placing unavoidable overflow on the last truck with an `Extra truck needed` banner), and writes one 2-sheet Excel workbook (`Truck plan` + `2-hour detail`) per supplier.
