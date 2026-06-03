from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import pandas as pd


QUANTITY_RE = re.compile(r"^(?P<name>.+?)\s+x\s*(?P<qty>\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
DETAIL_RE = re.compile(r"^(?P<label>[A-Z])[\).\-\s]+(?P<detail>.+)$")
NON_ARTICLE_RE = re.compile(
    r"(^|\b)(delivery|pickup|postal|courier|shipping|in-?store|rounding|discount|credit|tax|tip)\b",
    re.IGNORECASE,
)


def clean_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).replace("&amp;", "&")).strip()


def split_summary(summary: str) -> list[str]:
    summary = str(summary or "").replace("\r", "")
    parts = re.split(r"<br\s*/?>|\n", summary, flags=re.IGNORECASE)
    return [clean_text(part) for part in parts if clean_text(part)]


def parse_detail(line: str) -> tuple[str, str, str]:
    match = DETAIL_RE.match(line)
    if match:
        return match.group("label"), clean_text(match.group("detail")), ""
    if line.startswith("(") and line.endswith(")"):
        return "", "", line
    return "", line, ""


def is_article_service(service_name: str) -> bool:
    return not NON_ARTICLE_RE.search(service_name.strip())


def classify_article(service_name: str) -> tuple[str, str, str]:
    service = clean_text(service_name)
    service_lower = service.lower()

    if "(b)" in service_lower:
        group = "Bag / Leather Goods"
    elif "(o)" in service_lower:
        group = "Footwear"
    elif "(g)" in service_lower:
        group = "Garment"
    elif "(r)" in service_lower:
        group = "Retail / Accessory"
    elif any(word in service_lower for word in ["sneaker", "shoe", "sole", "loafer", "heel", "boot", "slide", "sandal", "mule", "foamrunner", "ballerina"]):
        group = "Footwear"
    elif any(word in service_lower for word in ["bag", "handbag", "wallet", "belt", "clutch", "cardholder", "buckle"]):
        group = "Bag / Leather Goods"
    elif any(word in service_lower for word in ["jacket", "shirt", "t-shirt", "hoodie", "sweatshirt", "caps", "dryclean"]):
        group = "Garment"
    elif any(word in service_lower for word in ["lace", "dust", "protector", "sneakinn"]):
        group = "Retail / Accessory"
    else:
        group = "Other"

    if "no work" in service_lower:
        work_type = "No Work"
    elif "rework" in service_lower:
        work_type = "Rework"
    elif "repair" in service_lower or "pasting" in service_lower or "stitching" in service_lower or "shaft change" in service_lower or "buckle change" in service_lower:
        work_type = "Repair"
    elif "color" in service_lower or "recolor" in service_lower or "plating" in service_lower:
        work_type = "Color / Restoration"
    elif "protector" in service_lower or "water protection" in service_lower:
        work_type = "Protection"
    elif "clean" in service_lower or "dryclean" in service_lower or "finishing" in service_lower:
        work_type = "Cleaning"
    elif "free" in service_lower:
        work_type = "Free Service"
    else:
        work_type = "Other"

    article_type = service
    remove_tokens = [
        "Clean-",
        "Clean -",
        "Clean",
        "Dryclean",
        "SNEAKER CLEAN FREE",
        "Sneaker Clean",
        "FOOTWEAR CLEAN FREE",
        "BAG CLEAN FREE",
    ]
    for token in remove_tokens:
        article_type = re.sub(re.escape(token), "", article_type, flags=re.IGNORECASE)
    article_type = re.sub(r"\([A-Z]\)", "", article_type).strip(" -")
    article_type = clean_text(article_type) or service

    if "sneaker" in service_lower:
        article_type = "Sneaker"
    elif "loafer" in service_lower or "formal" in service_lower:
        article_type = "Loafers / Formals"
    elif "heel" in service_lower or "espadrille" in service_lower:
        article_type = "Heels / Espadrilles"
    elif "slide" in service_lower or "sandal" in service_lower:
        article_type = "Slides / Sandals"
    elif "boot" in service_lower:
        article_type = "Boots"
    elif "handbag" in service_lower:
        article_type = "Handbag"
    elif "backpack" in service_lower or "laptop" in service_lower:
        article_type = "Backpack / Laptop Bag"
    elif "duffle" in service_lower:
        article_type = "Duffle Bag"
    elif "trolly" in service_lower or "trolley" in service_lower:
        article_type = "Trolley"
    elif "wallet" in service_lower:
        article_type = "Wallet"
    elif "belt" in service_lower:
        article_type = "Belt"
    elif "clutch" in service_lower:
        article_type = "Clutch"
    elif "cardholder" in service_lower:
        article_type = "Cardholder"
    elif "jacket" in service_lower:
        article_type = "Jacket"
    elif "t-shirt" in service_lower or "shirt" in service_lower:
        article_type = "Shirt / T-Shirt"
    elif "hoodie" in service_lower or "sweatshirt" in service_lower:
        article_type = "Hoodie / Sweatshirt"
    elif "cap" in service_lower:
        article_type = "Cap"
    elif "lace" in service_lower:
        article_type = "Lace"
    elif "dust" in service_lower:
        article_type = "Dust Bag"

    return group, article_type, work_type


def parse_order(row: pd.Series) -> list[dict[str, Any]]:
    lines = split_summary(row.get("summary", ""))
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in lines:
        quantity_match = QUANTITY_RE.match(line)
        if quantity_match:
            current = {
                "service": clean_text(quantity_match.group("name")),
                "quantity": int(float(quantity_match.group("qty"))),
                "details": [],
            }
            blocks.append(current)
            continue

        if current is not None:
            current["details"].append(line)

    article_rows: list[dict[str, Any]] = []
    article_number = 1

    for block in blocks:
        service = block["service"]
        quantity = block["quantity"]
        details = block["details"]

        if not is_article_service(service):
            continue

        parsed_details = [parse_detail(detail) for detail in details]
        named_details = [item for item in parsed_details if item[1]]
        notes = [item[2] for item in parsed_details if item[2]]

        if named_details:
            for label, description, _note in named_details:
                article_rows.append(
                    build_article_row(
                        row,
                        article_number,
                        service,
                        quantity,
                        label,
                        description,
                        "; ".join(notes),
                        "high" if len(named_details) == quantity else "medium",
                    )
                )
                article_number += 1
        else:
            confidence = "medium" if quantity > 0 else "low"
            for _ in range(quantity):
                article_rows.append(
                    build_article_row(
                        row,
                        article_number,
                        service,
                        quantity,
                        "",
                        "",
                        "; ".join(details),
                        confidence,
                    )
                )
                article_number += 1

    if not article_rows:
        article_rows.append(
            build_article_row(row, 1, "", 0, "", "", clean_text(row.get("summary", "")), "low")
        )

    return article_rows


def build_article_row(
    order: pd.Series,
    article_number: int,
    service: str,
    service_quantity: int,
    article_label: str,
    article_description: str,
    notes: str,
    confidence: str,
) -> dict[str, Any]:
    order_id = clean_text(order.get("id", ""))
    article_suffix = article_label.lower() if article_label else str(article_number)
    article_group, article_type, work_type = classify_article(service)
    return {
        "article_id": f"{order_id}-{article_suffix}" if order_id else "",
        "order_id": order_id,
        "created_date": order.get("createdDate", ""),
        "cleaned_date": order.get("cleanedDate", ""),
        "completed_date": order.get("completedDate", ""),
        "customer": order.get("customer", ""),
        "customer_id": order.get("customerID", ""),
        "phone": order.get("phone", ""),
        "status": order.get("status", ""),
        "delivery_type": order.get("deliveryType", ""),
        "delivery_area": order.get("deliveryArea", ""),
        "order_pieces": order.get("pieces", ""),
        "order_total": order.get("total", ""),
        "article_number": article_number,
        "service": service,
        "article_group": article_group,
        "article_type": article_type,
        "work_type": work_type,
        "service_quantity": service_quantity,
        "article_label": article_label,
        "article_description": article_description,
        "notes": notes,
        "parse_confidence": confidence,
        "summary": order.get("summary", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Expand CleanCloud exported order summary text into estimated article-level rows."
    )
    parser.add_argument("input_csv", help="CleanCloud orders CSV export.")
    parser.add_argument(
        "--output",
        default="cleancloud_article_level_from_summary.xlsx",
        help="Output Excel file path.",
    )
    args = parser.parse_args()

    input_path = Path(args.input_csv).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    orders = pd.read_csv(input_path, dtype=str).fillna("")
    article_rows = []
    for _, row in orders.iterrows():
        article_rows.extend(parse_order(row))

    articles = pd.DataFrame(article_rows)
    checks = (
        articles.groupby("order_id", dropna=False)
        .size()
        .reset_index(name="inferred_article_rows")
        .merge(
            orders[["id", "pieces", "summary"]].rename(columns={"id": "order_id"}),
            on="order_id",
            how="left",
        )
    )

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        articles.to_excel(writer, sheet_name="Article Level", index=False)
        checks.to_excel(writer, sheet_name="Order Checks", index=False)

    print(f"Orders read: {len(orders)}")
    print(f"Article rows created: {len(articles)}")
    print(f"Excel created: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
