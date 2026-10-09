from functools import lru_cache

from pydantic import BaseModel


class ShopSettings(BaseModel):
    shop_db_path: str = "data/shop.db"


@lru_cache
def get_shop_settings() -> ShopSettings:
    """商城的全部配置。刻意与网关的 Settings 分开——商城是被保护的一方，
    不应依赖保护者的配置结构。只读，进程生命周期内不变。"""
    import os

    return ShopSettings(shop_db_path=os.getenv("SHOP_DB_PATH", "data/shop.db"))
