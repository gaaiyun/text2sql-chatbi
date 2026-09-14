"""内置示例数据：连锁茶饮门店的销售明细（合成数据，固定随机种子）。

给“上传你的数据”一个开箱即用的样本：两张表（门店、门店销售）可以关联，
含日期、类别、数值、布尔字段，覆盖分布、排名、趋势、占比、关联等常见问法。
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

SAMPLE_SEED = 20260914
STORES = [
    ("GZ01", "天河体育中心店", "广州", "2021-03-18", 86),
    ("GZ02", "珠江新城店", "广州", "2022-07-01", 64),
    ("GZ03", "番禺万达店", "广州", "2024-05-20", 72),
    ("SZ01", "南山科技园店", "深圳", "2020-11-08", 90),
    ("SZ02", "福田中心店", "深圳", "2023-01-15", 58),
    ("FS01", "佛山千灯湖店", "佛山", "2022-09-09", 70),
    ("DG01", "东莞松山湖店", "东莞", "2023-08-26", 66),
    ("ZH01", "珠海拱北店", "珠海", "2025-04-12", 48),
]
PRODUCTS = [
    ("饮品", "柠檬茶", 16.0),
    ("饮品", "生椰拿铁", 19.0),
    ("饮品", "杨枝甘露", 22.0),
    ("饮品", "美式咖啡", 14.0),
    ("烘焙", "蛋挞", 8.0),
    ("烘焙", "牛角包", 12.0),
    ("轻食", "鸡胸肉沙拉", 32.0),
    ("轻食", "牛肉三明治", 28.0),
    ("周边", "保温杯", 89.0),
]
CITY_WEIGHT = {"广州": 1.25, "深圳": 1.35, "佛山": 0.9, "东莞": 0.85, "珠海": 0.7}


def write_retail_sample(directory: Path | str) -> list[Path]:
    rng = random.Random(SAMPLE_SEED)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)

    stores_path = target / "门店.csv"
    with stores_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["门店编号", "门店名称", "城市", "开业日期", "面积（平方米）"])
        writer.writerows(STORES)

    sales_path = target / "门店销售.csv"
    start = date(2025, 1, 1)
    with sales_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["日期", "门店编号", "城市", "品类", "商品", "数量", "单价", "销售额", "是否会员"]
        )
        for offset in range(0, 546, 3):  # 2025-01-01 至 2026-06-30，每三天一批
            day = start + timedelta(days=offset)
            season = 1.2 if day.month in (6, 7, 8) else 0.9 if day.month in (1, 2) else 1.0
            for code, _, city, opened, _ in STORES:
                if day.isoformat() < opened:
                    continue
                for category, product, price in rng.sample(PRODUCTS, k=4):
                    quantity = max(1, int(rng.gauss(18, 7) * CITY_WEIGHT[city] * season))
                    if category == "周边":
                        quantity = max(1, quantity // 9)
                    member = "是" if rng.random() < 0.42 else "否"
                    writer.writerow(
                        [
                            day.isoformat(),
                            code,
                            city,
                            category,
                            product,
                            quantity,
                            price,
                            round(quantity * price, 2),
                            member,
                        ]
                    )
    return [stores_path, sales_path]
