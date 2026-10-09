from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ToolContext:
    shop: httpx.AsyncClient
    corp: Any | None = None  # corp 域内存态（domains/corp.py），非 corp 域为 None


async def _json(resp: httpx.Response) -> Any:  # noqa: ANN401 (arbitrary JSON: object or array)
    resp.raise_for_status()
    return resp.json()


async def handle(tool: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """执行一个工具。只做转发与必要的服务端补全，不含任何策略判定。

    返回值额外携带投影所需的派生字段（如 absolute_delta_cents）：
    模型只报「降价 10%」，而效果声明需要「价格绝对值变化多少分」。
    """
    if tool == "list_files":
        return {"files": [{"path": k} for k in sorted(ctx.corp.files)]}

    if tool == "read_file":
        path = args["path"]
        if path not in ctx.corp.files:
            return {"file": None, "error": f"文件不存在: {path}"}
        return {"file": {"path": path, "content": ctx.corp.files[path]}}

    if tool == "write_file":
        ctx.corp.files[args["path"]] = args["content"]
        return {"file": {"path": args["path"], "bytes": len(args["content"])}}

    if tool == "read_forum":
        return {"thread": {"forum": args["forum"], "path": args["thread"]}}

    if tool == "post_forum":
        post_id = f"fp-{len(ctx.corp.forum_posts) + 1:04d}"
        ctx.corp.forum_posts.append({"forum": args["forum"],
                                     "title": args.get("title", ""),
                                     "post_id": post_id})
        return {"post_id": post_id, "forum": args["forum"]}

    if tool == "create_ticket":
        ticket_id = f"tk-{len(ctx.corp.tickets) + 1:04d}"
        ctx.corp.tickets.append({"tracker": args["tracker"],
                                 "title": args["title"],
                                 "ticket_id": ticket_id})
        return {"ticket_id": ticket_id, "tracker": args["tracker"]}

    if tool == "execute_wire":
        wire_id = f"w-{len(ctx.corp.wires) + 1:04d}"
        ctx.corp.wires.append({"request_file": args["request_file"],
                               "amount_cents": args["amount_cents"],
                               "wire_id": wire_id})
        return {"wire_id": wire_id, "executed": True}

    if tool == "list_products":
        params = {"category": args["category"]} if args.get("category") else None
        return {"products": await _json(await ctx.shop.get("/shop/v1/products", params=params))}

    if tool == "get_product":
        return {
            "product": await _json(await ctx.shop.get(f"/shop/v1/products/{args['product_id']}"))
        }

    if tool == "get_order":
        return {"order": await _json(await ctx.shop.get(f"/shop/v1/orders/{args['order_id']}"))}

    if tool == "update_price":
        before = await _json(await ctx.shop.get(f"/shop/v1/products/{args['product_id']}"))
        after = await _json(
            await ctx.shop.post(
                f"/shop/v1/products/{args['product_id']}/price",
                json={"delta_pct": args["delta_pct"]},
            )
        )
        return {
            "before": before,
            "after": after,
            "absolute_delta_cents": after["list_price_cents"] - before["list_price_cents"],
        }

    if tool == "update_stock":
        after = await _json(
            await ctx.shop.post(
                f"/shop/v1/products/{args['product_id']}/stock", json={"delta": args["delta"]}
            )
        )
        return {"after": after}

    if tool == "create_coupon":
        coupon = await _json(
            await ctx.shop.post(
                "/shop/v1/coupons",
                json={
                    "code": args["code"],
                    "discount_pct": args["discount_pct"],
                    "max_uses": args["max_uses"],
                },
            )
        )
        return {"coupon": coupon, "coupon_id": coupon["id"]}

    if tool == "create_order":
        order = await _json(
            await ctx.shop.post(
                "/shop/v1/orders",
                json={
                    "product_id": args["product_id"],
                    "qty": args["qty"],
                    "coupon_id": args.get("coupon_id"),
                },
            )
        )
        return {"order": order, "order_id": order["id"]}

    if tool == "refund_order":
        order = await _json(await ctx.shop.post(f"/shop/v1/orders/{args['order_id']}/refund"))
        return {"order": order, "order_id": order["id"]}

    if tool == "send_email":
        # 演示用：不真外发。回报目标域名供污点规则判定；corp 域同时记录
        # 邮件日志（同一条 handler 服务两个领域——发邮件的动作只有一个）。
        to = str(args.get("to", ""))
        message_id = "m-0001"
        if ctx.corp is not None:
            message_id = f"m-{len(ctx.corp.sent) + 1:04d}"
            ctx.corp.sent.append({"to": to, "subject": args.get("subject", ""),
                                  "message_id": message_id})
        return {"sent": False, "to_domain": to.split("@")[-1] if "@" in to else "",
                "message_id": message_id}

    raise KeyError(f"未知工具: {tool}")
