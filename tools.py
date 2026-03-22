import os
import json
import uuid
from collections import Counter
from datetime import datetime, timezone
import psycopg2


class OrderManager:

    def __init__(self, menu):

        self.MENU = menu
        self.ORDERS = {}

        self.db_conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT")),
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            sslmode="require"
        )

        self.db_conn.autocommit = True

    def _get_conn(self):
        """Return a live connection, reconnecting if needed."""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("SELECT 1;")
            return self.db_conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            self.db_conn = psycopg2.connect(
                host=os.getenv("DB_HOST"),
                port=int(os.getenv("DB_PORT")),
                dbname=os.getenv("DB_NAME"),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                sslmode="require"
            )
            self.db_conn.autocommit = True
            return self.db_conn

    # =========================
    # ORDER LIFECYCLE
    # =========================

    def create_new_order(self, stream_sid, phone_number=None):

        self.ORDERS[stream_sid] = {
            "order_id": str(uuid.uuid4()),
            "status": "in_progress",
            "items": [],
            "subtotal": 0,
            "currency": "USD",
            "customer": {
                "phone": phone_number
            }
        }


    def delete_order(self, stream_sid):

        if stream_sid in self.ORDERS:
            del self.ORDERS[stream_sid]


    # =========================
    # MENU TOOLS
    # =========================
    def get_items_by_category(self, category1, category2=None, category3=None, **kwargs):

        categories = {category1}

        if category2:
            categories.add(category2)

        if category3:
            categories.add(category3)

        items = []

        for id, item in self.MENU["items"].items():

            if any(cat in item["categoryIds"] for cat in categories):

                items.append({
                    "id": id,
                    "name": item["name"]
                })

        return items



    def get_item_details_by_id(self, id, **kwargs):

        return self.MENU["items"].get(id, {"error": "item not found"})


    def get_modifier_groups(self, item_id, **kwargs):

        return [
            {
                "id": group_id,
                "name": self.MENU["modifier_groups"][group_id]["name"],
                "minRequired": self.MENU["modifier_groups"][group_id]["minRequired"],
                "maxAllowed": self.MENU["modifier_groups"][group_id]["maxAllowed"],
                "modifiers": [
                    {
                        "id": mod_id,
                        "name": mod["name"],
                        "price": mod["price"]
                    }
                    for mod_id, mod in self.MENU["modifier_groups"][group_id]["modifiers"].items()
                ]
            }
            for group_id in self.MENU["items"][item_id]["modifier_groups_ids"]
        ]


    # =========================
    # ORDER TOOLS
    # =========================

    def add_item_to_order(self, stream_sid, item_id, quantity=1, modifiers=None, **kwargs):

        order = self.ORDERS[stream_sid]

        if modifiers is None:
            modifiers = []

        item = self.MENU["items"].get(item_id)

        if not item:
            return {
                "success": False,
                "error_type": "item_not_found"
            }


        # Validation

        invalid_groups = [
            m["modifier_group_id"]

            for m in modifiers

            if m["modifier_group_id"] not in item["modifier_groups_ids"]
        ]

        if invalid_groups:
            return {
                "success": False,
                "error_type": "invalid_modifier_group",
                "details": invalid_groups
            }


        invalid_modifiers = []

        for m in modifiers:

            group = self.MENU["modifier_groups"][m["modifier_group_id"]]

            if m["modifier_id"] not in group["modifiers"]:

                invalid_modifiers.append(m["modifier_id"])


        if invalid_modifiers:

            return {
                "success": False,
                "error_type": "invalid_modifier",
                "details": invalid_modifiers
            }


        modifier_groups_required = {

            group_id: self.MENU["modifier_groups"][group_id]["minRequired"]

            for group_id in item["modifier_groups_ids"]

            if self.MENU["modifier_groups"][group_id]["minRequired"] > 0
        }


        selected_counts = Counter(m["modifier_group_id"] for m in modifiers)


        missing_required = [

            group_id

            for group_id, min_required in modifier_groups_required.items()

            if selected_counts.get(group_id, 0) < min_required
        ]


        if missing_required:

            return {
                "success": False,
                "error_type": "missing_required_modifier",
                "details": missing_required
            }


        exceeded_groups = [
            group_id

            for group_id, count in selected_counts.items()

            if count > self.MENU["modifier_groups"][group_id]["maxAllowed"]
        ]


        if exceeded_groups:

            return {
                "success": False,
                "error_type": "too_many_modifiers",
                "details": exceeded_groups
            }


        # Add item

        line_id = str(uuid.uuid4())

        unit_price = item["price"]

        line_total = unit_price * quantity


        modifier_details = []


        for m in modifiers:
            
            grp = self.MENU["modifier_groups"][m["modifier_group_id"]]

            mod = grp["modifiers"][m["modifier_id"]]

            modifier_details.append({
                "id": m["modifier_id"],
                "groupId": m["modifier_group_id"],
                "groupName": grp["name"],
                "name": mod["name"],
                "quantity": 1,
                "price": mod["price"],
            })

            line_total += mod["price"] * quantity


        order["items"].append({
            "line_id": line_id,
            "item_id": item_id,
            "name": item["name"],
            "quantity": quantity,
            "unit_price": unit_price,
            "line_total": line_total,
            "modifiers": modifier_details
        })


        order["subtotal"] = sum(i["line_total"] for i in order["items"])


        return {
            "success": True,
            "line_id": line_id
        }


    def get_current_order(self, stream_sid, **kwargs):

        order = self.ORDERS[stream_sid]

        return {
            "success": True,
            "order_id": order["order_id"],
            "items": order["items"],
            "subtotal": order["subtotal"],
            "currency": order["currency"]
        }


    def remove_item_from_order(self, stream_sid, line_id, **kwargs):

        order = self.ORDERS[stream_sid]

        items = order["items"]


        index = next(
            (i for i, item in enumerate(items) if item["line_id"] == line_id),
            None
        )


        if index is None:

            return {
                "success": False,
                "error_type": "line_not_found"
            }


        removed = items.pop(index)


        order["subtotal"] = sum(i["line_total"] for i in items)


        return {
            "success": True,
            "removed_item": removed["name"],
            "subtotal": order["subtotal"]
        }


    def collect_client_info(self, stream_sid, full_name, **kwargs):

        order = self.ORDERS[stream_sid]

        order["customer"]["name"] = full_name

        return {
            "success": True
        }


    def confirm_order(self, stream_sid, **kwargs):

        order = self.ORDERS[stream_sid]
        payload = {}

        if not order["items"]:

            return {
                "success": False,
                "error_type": "order_empty"
            }


        if not order["customer"].get("name"):

            return {
                "success": False,
                "error_type": "client_info_missing"
            }


        order["status"] = "confirmed"
        order["confirmed_at"] = datetime.now(timezone.utc).isoformat()

        payload["items"] = [
            {
                "id": item["item_id"],
                "name": item["name"],
                "quantity": item["quantity"],
                "modifiers": [
                    {
                        "id": modifier["id"],
                        "groupId": modifier["groupId"],
                        "groupName": modifier["groupName"],
                        "name": modifier["name"],
                        "quantity": modifier["quantity"]
                    } for modifier in item["modifiers"]
                ]
            } for item in order["items"]
        ]

        payload["customer"] = {
            "name": order["customer"]["name"],
            "phone": order["customer"]["phone"]
        }

        payload["notes"] = ""
        payload["orderType"] = "Pickup"
        payload["pickupTime"] = None
        payload["taxExempt"] = False


        # ⭐ SAVE TO POSTGRESQL HERE

        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders (order_data)
                VALUES (%s)
                RETURNING id;
                """,
                (json.dumps(payload),)
            )
            db_id = cur.fetchone()[0]



        return {
            "success": True,
            "order_id": order["order_id"],
            "total_price": order["subtotal"]
        }