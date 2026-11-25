import json

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..models import onto_token
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

BRANCH = "odoo_dashboards_bi.group_onto_branch_manager"
STEWARD = "odoo_dashboards_bi.group_onto_steward"
APPLY_ROUTE = "/onto/v1/orgmap/apply"


def _missing_location_id(env):
    env.cr.execute("SELECT COALESCE(MAX(id), 0) + 1000 FROM stock_location")
    return env.cr.fetchone()[0]


@tagged("post_install", "-at_install")
class TestOntoOrgMap(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        Map, Loc = cls.env["onto.org.map"], cls.env["stock.location"]

        def warehouse(name, code):
            return cls.env["stock.warehouse"].create({"name": name, "code": code, "company_id": cls.company.id})

        def location(name, usage, parent, **vals):
            return Loc.create(dict(name=name, usage=usage, location_id=parent.id, company_id=cls.company.id, **vals))

        def node(name, kind, parent=None, **vals):
            return Map.create(dict(name=name, kind=kind, parent_id=parent.id if parent else False, **vals))

        # Seed-like: a central warehouse with QC, damaged, expired and transit locations,
        # a factory, and a branch with a child shelf (the seed's layout, see ONTO_FIXTURES.md).
        cls.central = warehouse("Org central", "OMC")
        cls.factory = warehouse("Org factory", "OMF")
        cls.branch_a = warehouse("Org branch A1", "OMA")
        central_view = cls.central.view_location_id
        cls.qc = location("QC Hold", "internal", central_view)
        cls.damaged = location("Damaged", "internal", central_view)
        cls.expired = location("Expired", "internal", central_view)
        cls.transit = location("In Transit", "transit", central_view)
        cls.shelf = location("Shelf 1", "internal", cls.branch_a.lot_stock_id)
        cls.scrap = location(
            "Org scrap", "inventory", cls.env.ref("stock.stock_location_locations_virtual"), scrap_location=True
        )

        cls.node_group = node("Org group", "group")
        cls.node_brand_north = node("Brand North", "brand", cls.node_group)
        cls.node_brand_vale = node("Brand Vale", "brand", cls.node_group)
        cls.node_region = node("Amsterdam", "region", cls.node_brand_north)
        cls.node_branch_a = node("Branch A1", "branch", cls.node_region)
        cls.node_wh_a = node("A1 warehouse", "warehouse", cls.node_branch_a, warehouse_id=cls.branch_a.id)
        node("A1 stock", "location", cls.node_wh_a, location_id=cls.branch_a.lot_stock_id.id, role="branch")
        # Region is optional: the branch sits straight under the brand.
        cls.node_branch_c = node("Central", "branch", cls.node_brand_vale)
        cls.node_wh_c = node("Central warehouse", "warehouse", cls.node_branch_c, warehouse_id=cls.central.id)
        node("Central stock", "location", cls.node_wh_c, location_id=cls.central.lot_stock_id.id, role="central")
        for loc, role in ((cls.qc, "qc_hold"), (cls.damaged, "damaged"), (cls.expired, "expired"), (cls.transit, "transit")):
            node(loc.name, "location", cls.node_wh_c, location_id=loc.id, role=role)
        node("Org scrap", "location", cls.node_wh_c, location_id=cls.scrap.id, role="scrap")
        cls.node_branch_f = node("Factory", "branch", cls.node_brand_vale)
        cls.node_wh_f = node("Factory warehouse", "warehouse", cls.node_branch_f, warehouse_id=cls.factory.id)
        node("Factory stock", "location", cls.node_wh_f, location_id=cls.factory.lot_stock_id.id, role="factory")

    def _user(self, name, groups):
        return self.env["res.users"].create(
            {
                "name": name,
                "login": name.lower().replace(" ", "_"),
                "company_id": self.company.id,
                "company_ids": [Command.set([self.company.id])],
                "groups_id": [
                    Command.set([self.env.ref("base.group_user").id] + [self.env.ref(g).id for g in groups])
                ],
            }
        )

    def _row(self, location):
        # Invalidate first: a row read before a map change would otherwise come back from the cache.
        self.env.invalidate_all()
        return self.env["onto.org.location"].search([("location_id", "=", location.id)])

    def _refused(self, action):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            action()
        self.env.invalidate_all()

    def test_initialization_keeps_dependent_inventory_views(self):
        self.env.cr.execute("CREATE TEMP VIEW test_onto_stock_dependency AS SELECT location_id FROM onto_org_map")
        self.env["onto.org.map"].init()
        self.env.cr.execute("SELECT to_regclass('pg_temp.test_onto_stock_dependency') IS NOT NULL")
        self.assertTrue(self.env.cr.fetchone()[0])

    def test_every_internal_location_joins_brand_branch_and_role(self):
        warehouses = self.central | self.factory | self.branch_a
        self.env.flush_all()
        self.env.cr.execute("SELECT count(*) FROM onto_org_map")
        view_rows = self.env.cr.fetchone()[0]
        self.env.cr.execute("SELECT count(*) FROM stock_location")
        self.assertEqual(view_rows, self.env.cr.fetchone()[0], "one row per stock.location")

        # What a managed view does: join on location_id as plain columns. The view keeps archived
        # locations too (Odoo archives the unused Input/QC/Output of each warehouse); they carry
        # brand and branch but no role until a steward maps them, so only active ones are checked.
        self.env.cr.execute(
            """
            SELECT sl.id, v.brand_id, v.branch_id, v.role
            FROM stock_location sl
            JOIN onto_org_map v ON v.location_id = sl.id
            WHERE sl.warehouse_id IN %s AND sl.usage IN ('internal', 'transit') AND sl.active
            """,
            [tuple(warehouses.ids)],
        )
        rows = self.env.cr.fetchall()
        expected = self.env["stock.location"].search(
            [("warehouse_id", "in", warehouses.ids), ("usage", "in", ["internal", "transit"])]
        )
        self.assertEqual(len(rows), len(expected), "one joined row per location, no duplicates")
        by_location = {row[0]: row[1:] for row in rows}
        for loc in expected:
            brand, branch, role = by_location[loc.id]
            self.assertTrue(brand and branch and role, loc.complete_name)
        self.assertEqual(by_location[self.qc.id], (self.node_brand_vale.id, self.node_branch_c.id, "qc_hold"))
        self.assertEqual(by_location[self.transit.id], (self.node_brand_vale.id, self.node_branch_c.id, "transit"))
        self.assertEqual(by_location[self.factory.lot_stock_id.id], (self.node_brand_vale.id, self.node_branch_f.id, "factory"))

    def test_child_location_inherits_branch_and_role_from_its_ancestor(self):
        row = self._row(self.shelf)
        self.assertEqual(row.warehouse_id, self.branch_a)
        self.assertEqual(row.brand_id, self.node_brand_north)
        self.assertEqual(row.branch_id, self.node_branch_a)
        self.assertEqual(row.role, "branch")

    def test_own_node_overrides_the_inherited_role(self):
        self.env["onto.org.map"].create(
            {
                "name": "Shelf 1 quarantine",
                "kind": "location",
                "parent_id": self.node_wh_a.id,
                "location_id": self.shelf.id,
                "role": "qc_hold",
            }
        )
        row = self._row(self.shelf)
        self.assertEqual(row.role, "qc_hold")
        self.assertEqual(row.branch_id, self.node_branch_a)

    def test_unmapped_location_takes_its_warehouse_branch_and_no_role_until_mapped(self):
        bay = self.env["stock.location"].create(
            {"name": "Returns bay", "usage": "internal", "location_id": self.central.view_location_id.id,
             "company_id": self.company.id}
        )
        row = self._row(bay)
        self.assertEqual(row.brand_id, self.node_brand_vale)
        self.assertEqual(row.branch_id, self.node_branch_c)
        self.assertFalse(row.role)
        # No refresh step: mapping the location shows on the next read.
        self.env["onto.org.map"].create(
            {"name": "Returns bay", "kind": "location", "parent_id": self.node_wh_c.id,
             "location_id": bay.id, "role": "damaged"}
        )
        self.assertEqual(self._row(bay).role, "damaged")

    def test_tree_levels_and_cycles_are_refused(self):
        Map = self.env["onto.org.map"]
        # A group is a root.
        self._refused(lambda: self.node_group.write({"parent_id": self.node_brand_north.id}))
        # A brand under its own region closes a cycle; the parent must be one level up.
        self._refused(lambda: self.node_brand_north.write({"parent_id": self.node_region.id}))
        # A branch cannot sit under its warehouse.
        self._refused(lambda: Map.create({"name": "Upside down", "kind": "branch", "parent_id": self.node_wh_a.id}))

    def test_role_and_links_only_where_the_kind_allows(self):
        Map = self.env["onto.org.map"]
        loose = self.env["stock.location"].create(
            {"name": "Loose", "usage": "internal", "location_id": self.central.view_location_id.id,
             "company_id": self.company.id}
        )
        # Role only on location nodes.
        self._refused(lambda: self.node_wh_a.write({"role": "central"}))
        # Warehouse link only on warehouse nodes (a fresh warehouse: the factory already has a node).
        spare = self.env["stock.warehouse"].create({"name": "Org spare", "code": "OMS", "company_id": self.company.id})
        self._refused(lambda: Map.create({"name": "Brand with a warehouse", "kind": "brand",
                                          "parent_id": self.node_group.id, "warehouse_id": spare.id}))
        # Location nodes need a role and a stock location.
        self._refused(lambda: Map.create({"name": "Loose", "kind": "location",
                                          "parent_id": self.node_wh_a.id, "location_id": loose.id}))
        self._refused(lambda: Map.create({"name": "Warehouse without a link", "kind": "warehouse",
                                          "parent_id": self.node_branch_a.id}))

    def test_one_org_node_per_location(self):
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env["onto.org.map"].create(
                {"name": "Second node", "kind": "location", "parent_id": self.node_wh_a.id,
                 "location_id": self.branch_a.lot_stock_id.id, "role": "branch"}
            )

    def test_only_stewards_write_and_onto_users_read(self):
        manager = self._user("Org Manager", [BRANCH])
        steward = self._user("Org Steward", [STEWARD])
        plain = self._user("Org Plain", [])
        Map = self.env["onto.org.map"]

        self.assertTrue(Map.with_user(manager).search([("id", "=", self.node_group.id)]))
        self.assertTrue(self.env["onto.org.location"].with_user(manager).search([("location_id", "=", self.shelf.id)]))
        with self.assertRaises(AccessError):
            Map.with_user(manager).create({"name": "Refused", "kind": "group"})
        with self.assertRaises(AccessError):
            self.node_group.with_user(manager).write({"name": "Refused"})

        self.node_group.with_user(steward).write({"name": "Renamed by steward"})
        self.assertTrue(Map.with_user(steward).create({"name": "Steward group", "kind": "group"}))

        with self.assertRaises(AccessError):
            Map.with_user(plain).search([])

    def _new_location(self, name):
        return self.env["stock.location"].create(
            {"name": name, "usage": "internal", "location_id": self.central.view_location_id.id,
             "company_id": self.company.id}
        )

    def _apply(self, entries, user=None):
        Map = self.env["onto.org.map"]
        return (Map.with_user(user) if user else Map).apply_location_roles(entries)

    def test_apply_creates_updates_and_leaves_unchanged_locations(self):
        bay = self._new_location("Apply bay")
        results = self._apply([
            {"location_id": bay.id, "role": "damaged"},
            {"location_id": self.qc.id, "role": "qc_hold"},
            {"location_id": self.expired.id, "role": "damaged"},
        ])
        self.assertEqual([r["status"] for r in results], ["created", "unchanged", "updated"], results)
        Map = self.env["onto.org.map"]
        node = Map.search([("location_id", "=", bay.id)])
        self.assertEqual((node.kind, node.role, node.parent_id, node.company_id),
                         ("location", "damaged", self.node_wh_c, self.company))
        self.assertEqual(Map.search([("location_id", "=", self.qc.id)]).role, "qc_hold")
        self.assertEqual(Map.search([("location_id", "=", self.expired.id)]).role, "damaged")
        # The role shows in the view for that location, with the brand and branch of its warehouse.
        row = self._row(bay)
        self.assertEqual((row.role, row.brand_id, row.branch_id), ("damaged", self.node_brand_vale, self.node_branch_c))
        self.assertEqual(self._row(self.expired).role, "damaged")

    def test_an_unknown_location_or_role_fails_only_that_entry(self):
        bay = self._new_location("Apply bay")
        results = self._apply([
            {"location_id": _missing_location_id(self.env), "role": "damaged"},
            {"location_id": self.shelf.id, "role": "not_a_role"},
            {"location_id": bay.id, "role": "damaged"},
        ])
        self.assertEqual(
            [(r["status"], r.get("error")) for r in results],
            [("failed", "unknown_location"), ("failed", "unknown_role"), ("created", None)],
            results,
        )
        self.assertFalse(self.env["onto.org.map"].search([("location_id", "=", self.shelf.id)]), "a refused entry writes nothing")
        self.assertEqual(self._row(bay).role, "damaged")

    def _warehouse(self, company, name, code):
        return self.env["stock.warehouse"].create({"name": name, "code": code, "company_id": company.id})

    def test_an_empty_tree_gets_the_minimum_chain_and_the_role(self):
        company = self.env["res.company"].create({"name": "Org empty co"})
        wh = self._warehouse(company, "Empty WH", "OEW")
        loc = wh.lot_stock_id
        results = self._apply([{"location_id": loc.id, "role": "central"}])
        Map = self.env["onto.org.map"]
        node = Map.search([("location_id", "=", loc.id)])
        wh_node = node.parent_id
        branch = wh_node.parent_id
        brand = branch.parent_id
        group = brand.parent_id
        self.assertEqual(results[0]["status"], "created", results)
        self.assertEqual(results[0]["created_nodes"], [group.id, brand.id, branch.id, wh_node.id, node.id], results)
        self.assertEqual((group.kind, group.name, group.company_id), ("group", "Org empty co", company))
        self.assertFalse(group.parent_id)
        self.assertEqual((brand.kind, brand.name, brand.parent_id), ("brand", "Org empty co", group))
        self.assertEqual((branch.kind, branch.name, branch.parent_id), ("branch", "Empty WH", brand))
        self.assertEqual((wh_node.kind, wh_node.warehouse_id, wh_node.parent_id), ("warehouse", wh, branch))
        self.assertEqual((node.role, node.company_id, node.parent_id), ("central", company, wh_node))
        # The role shows in the view with the brand and branch the chain gave the warehouse.
        row = self._row(loc)
        self.assertEqual((row.role, row.brand_id, row.branch_id), ("central", brand, branch))

    def test_a_second_location_reuses_the_chain_and_a_second_warehouse_reuses_the_group(self):
        company = self.env["res.company"].create({"name": "Org shared co"})
        wh1 = self._warehouse(company, "First WH", "OSA")
        wh2 = self._warehouse(company, "Second WH", "OSB")
        shelf = self.env["stock.location"].create(
            {"name": "Shelf", "usage": "internal", "location_id": wh1.view_location_id.id, "company_id": company.id}
        )
        results = self._apply([
            {"location_id": wh1.lot_stock_id.id, "role": "central"},
            {"location_id": shelf.id, "role": "damaged"},
            {"location_id": wh2.lot_stock_id.id, "role": "branch"},
        ])
        self.assertEqual([r["status"] for r in results], ["created"] * 3, results)
        Map = self.env["onto.org.map"]
        first_node = Map.search([("location_id", "=", wh1.lot_stock_id.id)])
        brand = first_node.parent_id.parent_id.parent_id
        self.assertEqual(len(results[0]["created_nodes"]), 5, results)
        self.assertEqual(results[1]["created_nodes"], [Map.search([("location_id", "=", shelf.id)]).id])
        # The second warehouse adds only a branch, its warehouse node and its location: the group and brand are reused.
        self.assertEqual(len(results[2]["created_nodes"]), 3, results)
        branch, wh2_node, wh2_loc = (Map.browse(i) for i in results[2]["created_nodes"])
        self.assertEqual((branch.kind, branch.name, branch.parent_id), ("branch", "Second WH", brand))
        self.assertEqual((wh2_node.kind, wh2_node.parent_id), ("warehouse", branch))
        self.assertEqual((wh2_loc.kind, wh2_loc.parent_id), ("location", wh2_node))
        self.assertEqual(Map.search_count([("company_id", "=", company.id), ("kind", "=", "group")]), 1)
        self.assertEqual(Map.search_count([("company_id", "=", company.id), ("kind", "=", "brand")]), 1)

    def test_a_location_with_no_warehouse_fails_and_builds_nothing(self):
        floating = self.env["stock.location"].create(
            {"name": "Floating", "usage": "internal", "location_id": self.env.ref("stock.stock_location_locations").id,
             "company_id": self.company.id}
        )
        results = self._apply([{"location_id": floating.id, "role": "branch"}])
        self.assertEqual((results[0]["status"], results[0]["error"]), ("failed", "no_warehouse"), results)
        self.assertFalse(self.env["onto.org.map"].search([("location_id", "=", floating.id)]))

    def test_the_caller_rules_decide_who_may_apply(self):
        bay = self._new_location("Apply bay")
        manager = self._user("Org Manager", [BRANCH])
        steward = self._user("Org Steward", [STEWARD])
        refused = self._apply([{"location_id": bay.id, "role": "damaged"}], user=manager)
        self.assertEqual((refused[0]["status"], refused[0]["error"]), ("failed", "refused"), refused)
        self.assertFalse(self.env["onto.org.map"].search([("location_id", "=", bay.id)]))
        self.assertEqual(self._apply([{"location_id": bay.id, "role": "damaged"}], user=steward)[0]["status"], "created")


@tagged("post_install", "-at_install")
class TestOntoOrgMapApply(HttpCase):
    """POST /onto/v1/orgmap/apply: steward only, sealed, body checked before any write."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group_user = cls.env.ref("base.group_user")
        cls.steward = cls.env["res.users"].create({
            "name": "Org Apply Steward", "login": "org_apply_steward",
            "groups_id": [Command.set([group_user.id, cls.env.ref(STEWARD).id])],
        })
        cls.manager = cls.env["res.users"].create({
            "name": "Org Apply Manager", "login": "org_apply_manager",
            "groups_id": [Command.set([group_user.id, cls.env.ref(BRANCH).id])],
        })
        cls.steward_key = cls.env["res.users.apikeys"].with_user(cls.steward)._generate("mobile", "onto-orgmap")
        cls.manager_key = cls.env["res.users.apikeys"].with_user(cls.manager)._generate("mobile", "onto-orgmap")
        # Own warehouse with its node chain, so an apply has a warehouse node to hang under.
        Map = cls.env["onto.org.map"]
        cls.company = cls.env.company
        cls.warehouse = cls.env["stock.warehouse"].create({"name": "Apply WH", "code": "OAP", "company_id": cls.company.id})
        group = Map.create({"name": "Apply group", "kind": "group"})
        brand = Map.create({"name": "Apply brand", "kind": "brand", "parent_id": group.id})
        branch = Map.create({"name": "Apply branch", "kind": "branch", "parent_id": brand.id})
        cls.warehouse_node = Map.create(
            {"name": "Apply warehouse", "kind": "warehouse", "parent_id": branch.id, "warehouse_id": cls.warehouse.id}
        )
        cls.bay = cls.env["stock.location"].create(
            {"name": "Apply bay", "usage": "internal", "location_id": cls.warehouse.view_location_id.id,
             "company_id": cls.company.id}
        )

    def _apply(self, body, key=None):
        return sealed_request(self, key or self.steward_key, "POST", APPLY_ROUTE, body=body)

    def _bearer_post(self, token, body):
        headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}
        return self.opener.request(
            "POST", self.base_url() + APPLY_ROUTE, data=json.dumps(body), headers=headers, timeout=12
        )

    def _mapped(self, location):
        self.env.invalidate_all()
        return self.env["onto.org.map"].search([("location_id", "=", location.id)])

    def test_steward_applies_over_http_and_the_view_shows_the_role(self):
        missing = _missing_location_id(self.env)
        resp = self._apply({"locations": [
            {"location_id": self.bay.id, "role": "qc_hold"},
            {"location_id": missing, "role": "qc_hold"},
            {"location_id": self.bay.id, "role": "qc_hold"},
        ]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual([r["status"] for r in resp.json()["results"]], ["created", "failed", "unchanged"], resp.text)
        # The warehouse already has its node, so the only node created is the bay's own.
        self.assertEqual(resp.json()["results"][0]["created_nodes"], [self._mapped(self.bay).id], resp.text)
        self.env.invalidate_all()
        row = self.env["onto.org.location"].search([("location_id", "=", self.bay.id)])
        branch = self.warehouse_node.parent_id
        self.assertEqual((row.role, row.branch_id, row.brand_id), ("qc_hold", branch, branch.parent_id))

    def test_a_non_steward_gets_404_and_writes_nothing(self):
        resp = self._apply({"locations": [{"location_id": self.bay.id, "role": "damaged"}]}, key=self.manager_key)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertFalse(self._mapped(self.bay))

    def test_a_query_token_is_refused(self):
        resp = sealed_request(self, self.steward_key, "POST", "/onto/v1/token", body={})
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self._bearer_post(resp.json()["token"], {"locations": [{"location_id": self.bay.id, "role": "damaged"}]})
        self.assertEqual((resp.status_code, json.loads(resp.text)["error"]), (403, "token_kind"), resp.text)
        self.assertFalse(self._mapped(self.bay))

    def test_a_bot_token_is_refused(self):
        resp = self._bearer_post(
            onto_token.mint_bot_token(self.env), {"locations": [{"location_id": self.bay.id, "role": "damaged"}]}
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertFalse(self._mapped(self.bay))

    def test_the_body_is_checked_before_any_write(self):
        entry = {"location_id": self.bay.id, "role": "damaged"}
        bad = [
            {},
            {"locations": "bay"},
            {"locations": [{"location_id": str(self.bay.id), "role": "damaged"}]},
            {"locations": [{"location_id": True, "role": "damaged"}]},
            {"locations": [{"location_id": self.bay.id, "role": 3}]},
            {"locations": [{"location_id": self.bay.id, "role": ["damaged"]}]},
            {"locations": [entry] * 501},
        ]
        for body in bad:
            self.assertEqual(self._apply(body).status_code, 422, body)
        self.assertFalse(self._mapped(self.bay))
        resp = self._apply({"locations": [{"location_id": _missing_location_id(self.env), "role": "damaged"}] * 500})
        self.assertEqual((resp.status_code, len(resp.json()["results"])), (200, 500), resp.text)

    def _fresh_bay(self, name):
        return self.env["stock.location"].create(
            {"name": name, "usage": "internal", "location_id": self.warehouse.view_location_id.id,
             "company_id": self.company.id}
        )

    def _apply_audits(self, node):
        return self.env["onto.audit"].search(
            [("action", "=", "onto.orgmap.role"), ("res_model", "=", "onto.org.map"), ("res_id", "=", node.id)],
            order="id desc",
        )

    def test_an_unknown_role_refuses_the_whole_request_before_any_write(self):
        bay, other = self._fresh_bay("Apply bay X"), self._fresh_bay("Apply bay Y")
        resp = self._apply({"locations": [
            {"location_id": bay.id, "role": "damaged"},
            {"location_id": other.id, "role": "not_a_role"},
        ]})
        self.assertEqual((resp.status_code, resp.json()), (422, {"error": "unknown_role"}), resp.text)
        self.assertFalse(self._mapped(bay))
        self.assertFalse(self._mapped(other))

    def test_a_missing_role_fails_when_unmapped_and_keeps_the_mapped_role(self):
        bay = self._fresh_bay("Apply bay Z")
        resp = self._apply({"locations": [{"location_id": bay.id}]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((resp.json()["results"][0]["status"], resp.json()["results"][0]["error"]),
                         ("failed", "no_role"), resp.text)
        self.assertFalse(self._mapped(bay))
        self._apply({"locations": [{"location_id": bay.id, "role": "damaged"}]})
        resp = self._apply({"locations": [{"location_id": bay.id}]})
        self.assertEqual([r["status"] for r in resp.json()["results"]], ["unchanged"], resp.text)
        self.assertEqual(self._mapped(bay).role, "damaged")

    def test_each_applied_role_is_audited_with_the_steward_and_the_role(self):
        bay = self._fresh_bay("Apply bay audit")
        self._apply({"locations": [{"location_id": bay.id, "role": "qc_hold"}]})
        audit = self._apply_audits(self._mapped(bay))
        self.assertEqual(len(audit), 1, audit)
        self.assertEqual((audit.actor_id, audit.company_id), (self.steward, self.company))
        self.assertIn("role=qc_hold status=created", audit.detail)
        self._apply({"locations": [{"location_id": bay.id, "role": "qc_hold"}]})  # unchanged: no new row
        self._apply({"locations": [{"location_id": bay.id, "role": "damaged"}]})  # updated: one more row
        audit = self._apply_audits(self._mapped(bay))
        self.assertEqual(len(audit), 2, audit)
        self.assertIn("role=damaged status=updated", audit[0].detail)
