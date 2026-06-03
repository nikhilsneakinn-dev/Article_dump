from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd
import requests


API_BASE = "https://cleancloudapp.com/api"
STATUS_CLEANING = 0
STATUS_READY = 1


def post_cleancloud(endpoint: str, payload: dict[str, Any]) -> Any:
    response = requests.post(
        f"{API_BASE}/{endpoint}",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and data.get("Error"):
        raise RuntimeError(f"CleanCloud API error from {endpoint}: {data['Error']}")

    return data


def find_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]

    if not isinstance(value, dict):
        return []

    preferred_keys = [
        "Orders",
        "orders",
        "Order",
        "order",
        "Garments",
        "garments",
        "Products",
        "products",
        "Items",
        "items",
        "Data",
        "data",
    ]
    for key in preferred_keys:
        records = find_records(value.get(key))
        if records:
            return records

    nested_lists = [find_records(item) for item in value.values()]
    for records in nested_lists:
        if records:
            return records

    return [value] if value else []


def find_nested_list(record: dict[str, Any], names: Iterable[str]) -> list[dict[str, Any]]:
    wanted = {name.lower() for name in names}
    for key, value in record.items():
        if key.lower() in wanted:
            records = find_records(value)
            if records:
                return records
    return []


def order_id_from(order: dict[str, Any]) -> Any:
    for key in ("orderID", "OrderID", "id", "ID"):
        if order.get(key) not in (None, ""):
            return order[key]
    return ""


def customer_id_from(order: dict[str, Any]) -> Any:
    for key in ("customerID", "CustomerID", "cid", "customer_id"):
        if order.get(key) not in (None, ""):
            return order[key]
    return ""


def flatten(prefix: str, value: Any, output: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            flatten(f"{prefix}{key}_", nested, output)
    elif isinstance(value, list):
        output[prefix[:-1]] = json.dumps(value, ensure_ascii=False)
    else:
        output[prefix[:-1]] = value


def flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in record.items():
        flatten(f"{key}_", value, output)
    return output


def get_orders(api_token: str, status: int, date_from: str | None, date_to: str | None) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "api_token": api_token,
        "status": status,
        "sendProductDetails": 1,
    }
    if date_from:
        payload["dateFrom"] = date_from
    if date_to:
        payload["dateTo"] = date_to

    return find_records(post_cleancloud("getOrders", payload))


def get_garments(api_token: str, order_id: Any) -> list[dict[str, Any]]:
    if not order_id:
        return []
    payload = {"api_token": api_token, "orderID": str(order_id)}
    return find_records(post_cleancloud("getGarments", payload))


def export_order_articles(args: argparse.Namespace) -> None:
    api_token = args.api_token or os.getenv("CLEANCLOUD_API_TOKEN")
    if not api_token:
        raise RuntimeError(
            "Missing API token. Pass --api-token YOUR_TOKEN or set CLEANCLOUD_API_TOKEN."
        )

    status_tabs = [("Cleaning", STATUS_CLEANING), ("Ready", STATUS_READY)]
    order_rows: list[dict[str, Any]] = []
    product_rows: list[dict[str, Any]] = []
    garment_rows: list[dict[str, Any]] = []

    for tab_name, status in status_tabs:
        orders = get_orders(api_token, status, args.date_from, args.date_to)
        print(f"{tab_name}: found {len(orders)} orders")

        for order in orders:
            order_id = order_id_from(order)
            customer_id = customer_id_from(order)
            order_rows.append({"tab": tab_name, **flatten_record(order)})

            products = find_nested_list(order, ["products", "productDetails", "items", "articles"])
            for product in products:
                product_rows.append(
                    {
                        "tab": tab_name,
                        "orderID": order_id,
                        "customerID": customer_id,
                        **flatten_record(product),
                    }
                )

            if args.include_garments:
                time.sleep(args.delay)
                for garment in get_garments(api_token, order_id):
                    garment_rows.append(
                        {
                            "tab": tab_name,
                            "orderID": order_id,
                            "customerID": customer_id,
                            **flatten_record(garment),
                        }
                    )

    output_path = Path(args.output).expanduser().resolve()
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        pd.DataFrame(order_rows).to_excel(writer, sheet_name="Orders", index=False)
        pd.DataFrame(product_rows).to_excel(writer, sheet_name="Articles", index=False)
        if args.include_garments:
            pd.DataFrame(garment_rows).to_excel(writer, sheet_name="Garments", index=False)

    print(f"Excel file created: {output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export CleanCloud order article/product data using the CleanCloud API."
    )
    parser.add_argument("--api-token", help="CleanCloud API token. Or use CLEANCLOUD_API_TOKEN.")
    parser.add_argument("--date-from", help="Optional beginning order date, yyyy-mm-dd.")
    parser.add_argument("--date-to", help="Optional ending order date, yyyy-mm-dd.")
    parser.add_argument(
        "--include-garments",
        action="store_true",
        help="Also call getGarments for each order to export barcode/garment-level rows.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Delay between garment calls. Default 0.4s keeps under the 3 requests/sec API limit.",
    )
    parser.add_argument(
        "--output",
        default="cleancloud_api_order_articles.xlsx",
        help="Excel file path to create.",
    )
    return parser.parse_args()


def main() -> int:
    try:
        export_order_articles(parse_args())
        return 0
    except Exception as exc:
        print(f"Export failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
