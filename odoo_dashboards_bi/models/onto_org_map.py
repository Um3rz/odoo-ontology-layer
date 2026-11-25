# -*- coding: utf-8 -*-
"""Onto org map (1.3): group > brand > region/city > branch > warehouse > location.

Stewards keep the tree in onto.org.map (table onto_org_node). Every node sits one
level below its parent, so the tree cannot have a cycle. Only location nodes link
a stock.location and carry a role.

onto.org.location reads the SQL view onto_org_map: one row per stock.location with
brand, branch, region and role as plain columns, so managed views join it by
location_id. A location without a node of its own takes the role and branch of its
nearest mapped ancestor location, or failing that the branch of its warehouse, with
no role.

The view is plain, not materialized: it reads the tree and the locations live, so a
change to either shows at once and nothing needs refreshing. Install and upgrade
rebuild the view definition (OntoOrgMap.init).
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

KINDS = [
    ("group", "Group"),
    ("brand", "Brand"),
    ("region", "Region / city"),
    ("branch", "Branch"),
    ("warehouse", "Warehouse"),
    ("location", "Location"),
]
KIND_RANK = {kind: rank for rank, (kind, _label) in enumerate(KINDS)}

ROLES = [
    ("central", "Central"),
    ("factory", "Factory"),
    ("branch", "Branch"),
    ("transit", "Transit"),
    ("damaged", "Damaged"),
    ("qc_hold", "QC hold"),
    ("expired", "Expired"),
    ("scrap", "Scrap"),
]
ROLE_VALUES = {role for role, _label in ROLES}

ORG_LOCATION_VIEW = "onto_org_map"
# UNION (not UNION ALL) in node_up stops on a loop, so a corrupt tree cannot hang the view.
ORG_LOCATION_SQL = """
WITH RECURSIVE up AS (
    SELECT id AS location_id, id AS ancestor_id, 0 AS dist FROM stock_location
    UNION ALL
    SELECT up.location_id, p.id, up.dist + 1
    FROM up
    JOIN stock_location c ON c.id = up.ancestor_id
    JOIN stock_location p ON p.id = c.location_id
),
anchor AS (
    SELECT DISTINCT ON (up.location_id) up.location_id, n.id AS node_id, n.role
    FROM up
    JOIN onto_org_node n ON n.kind = 'location' AND n.location_id = up.ancestor_id
    ORDER BY up.location_id, up.dist
),
node_up AS (
    SELECT id AS node_id, id AS ancestor_id FROM onto_org_node
    UNION
    SELECT u.node_id, p.id
    FROM node_up u
    JOIN onto_org_node c ON c.id = u.ancestor_id
    JOIN onto_org_node p ON p.id = c.parent_id
),
chain AS (
    SELECT u.node_id,
           MAX(CASE WHEN a.kind = 'group' THEN a.id END) AS group_id,
           MAX(CASE WHEN a.kind = 'brand' THEN a.id END) AS brand_id,
           MAX(CASE WHEN a.kind = 'brand' THEN a.name END) AS brand_name,
           MAX(CASE WHEN a.kind = 'region' THEN a.id END) AS region_id,
           MAX(CASE WHEN a.kind = 'region' THEN a.name END) AS region_name,
           MAX(CASE WHEN a.kind = 'branch' THEN a.id END) AS branch_id,
           MAX(CASE WHEN a.kind = 'branch' THEN a.name END) AS branch_name
    FROM node_up u
    JOIN onto_org_node a ON a.id = u.ancestor_id
    GROUP BY u.node_id
)
SELECT sl.id AS id,
       sl.id AS location_id,
       sl.warehouse_id,
       ch.group_id, ch.brand_id, ch.brand_name, ch.region_id, ch.region_name,
       ch.branch_id, ch.branch_name,
       an.role
FROM stock_location sl
LEFT JOIN anchor an ON an.location_id = sl.id
LEFT JOIN onto_org_node wn ON wn.kind = 'warehouse' AND wn.warehouse_id = sl.warehouse_id
LEFT JOIN chain ch ON ch.node_id = COALESCE(an.node_id, wn.id)
"""


class OntoOrgMap(models.Model):
    _name = "onto.org.map"
    _inherit = ["onto.mixin"]
    _description = "Onto org map node"
    # Not onto_org_map: that name is the location view below.
    _table = "onto_org_node"
    _order = "id"
    _sql_constraints = [
        ("onto_org_node_location_uniq", "unique(location_id)", "A stock location has one org node."),
        ("onto_org_node_warehouse_uniq", "unique(warehouse_id)", "A warehouse has one org node."),
    ]

    name = fields.Char(required=True)
    kind = fields.Selection(KINDS, required=True)
    parent_id = fields.Many2one("onto.org.map", ondelete="restrict", index=True)
    child_ids = fields.One2many("onto.org.map", "parent_id")
    warehouse_id = fields.Many2one("stock.warehouse", ondelete="restrict", index=True)
    location_id = fields.Many2one("stock.location", ondelete="restrict", index=True)
    role = fields.Selection(ROLES, string="Location role")

    @api.constrains("kind", "parent_id", "warehouse_id", "location_id", "role")
    def _check_tree(self):
        for node in self:
            if node.kind == "group":
                if node.parent_id:
                    raise ValidationError(_("A group is the root of the map and cannot have a parent."))
            elif not node.parent_id:
                raise ValidationError(_("%s needs a parent one level up.") % node.name)
            elif KIND_RANK[node.parent_id.kind] >= KIND_RANK[node.kind]:
                raise ValidationError(
                    _("%s cannot sit under %s: the parent must be one level up.")
                    % (node.name, node.parent_id.name)
                )
            for child in node.child_ids:
                if KIND_RANK[child.kind] <= KIND_RANK[node.kind]:
                    raise ValidationError(
                        _("%s cannot hold %s: the child must be one level down.") % (node.name, child.name)
                    )
            if node.kind == "warehouse" and not node.warehouse_id:
                raise ValidationError(_("%s needs a warehouse.") % node.name)
            if node.kind != "warehouse" and node.warehouse_id:
                raise ValidationError(_("Only warehouse nodes link a warehouse."))
            if node.kind == "location" and (not node.location_id or not node.role):
                raise ValidationError(_("%s needs a stock location and a role.") % node.name)
            if node.kind != "location" and (node.location_id or node.role):
                raise ValidationError(_("Only location nodes link a stock location and carry a role."))

    def init(self):
        # Published inventory datasets depend on this view across module upgrades.
        self.env.cr.execute("CREATE OR REPLACE VIEW %s AS %s" % (ORG_LOCATION_VIEW, ORG_LOCATION_SQL))

    def apply_location_roles(self, entries):
        """Set the role of each stock location's org node, creating the node when it has none (contract 11, D54).

        entries: [{"location_id": int, "role": str}], applied in order. Each entry runs in its own savepoint, so a
        refused entry leaves nothing behind and the others still apply. Returns one result per entry:
        {"location_id", "status": created | updated | unchanged | failed}, plus "error" and "message" when failed,
        and "created_nodes" (every node the entry created, top down, the location node last) when it created any.

        The role is the steward's choice and overrides any rule that proposed one (2.9e). It is optional: an entry
        without a role keeps the role its location already has, and fails with no_role when there is none. Each
        created or updated role writes one onto.audit row (onto.orgmap.role) with the location, the role and the
        steward as actor.

        Runs as the calling user, never sudo. The stock.location read, the onto.org.map ACLs, the company record
        rule and the constraints above decide. A location the caller cannot see fails as unknown, the same as one
        that does not exist, so the reply does not reveal which ids exist.

        Missing chain: a new location node hangs under its warehouse's node. When that warehouse has no node, the
        minimum chain is created above the location, top down: a group, a brand, a branch, the warehouse node, then
        the location node. Region is skipped, as it is optional. Each level is reused when it exists rather than
        duplicated: the company's group is its only group when it has exactly one, otherwise the group named after
        the company (created once, so a retry finds it); the brand is named after the company and the branch after
        the warehouse, both matched by name and parent; the warehouse node is matched by its warehouse. Every node
        takes the location's company (its warehouse's, when the location has none). A location with no warehouse
        cannot be placed, so it fails with no_warehouse. An existing location node keeps its parent: only its role
        changes, which needs no warehouse. All writes sit in the entry's savepoint, so a refused entry leaves no
        chain behind.
        """
        Location = self.env["stock.location"].with_context(active_test=False)
        visible = {loc.id: loc for loc in Location.search([("id", "in", [e["location_id"] for e in entries])])}
        results = []
        for entry in entries:
            location_id, role = entry["location_id"], entry["role"]
            loc = visible.get(location_id)
            if loc is None:
                results.append(
                    _failed(location_id, "unknown_location", "No stock location with this id is visible to you.")
                )
            elif role is not None and role not in ROLE_VALUES:
                results.append(_failed(location_id, "unknown_role", "Not an org map role: %s." % role))
            else:
                results.append(self._apply_location_role(loc, role))
        return results

    def _apply_location_role(self, loc, role):
        node = self.search([("location_id", "=", loc.id)], limit=1)
        if role is None:  # no role given: keep the one the location has
            if node:
                return {"location_id": loc.id, "status": "unchanged"}
            return _failed(loc.id, "no_role", "The location has no role yet: choose one to map it.")
        if node.role == role:
            return {"location_id": loc.id, "status": "unchanged"}
        if not node and not loc.warehouse_id:
            return _failed(loc.id, "no_warehouse", "The location is not in a warehouse, so the map cannot place it.")
        created = []
        try:
            with self.env.cr.savepoint():
                if node:
                    node.write({"role": role})
                    status = "updated"
                else:
                    company = loc.company_id or loc.warehouse_id.company_id
                    parent = self._warehouse_node(loc.warehouse_id, company, created)
                    node = self._new_node(created, name=loc.complete_name, kind="location", parent_id=parent.id,
                                          location_id=loc.id, role=role, company_id=company.id)
                    status = "created"
                self._audit_role(node, loc, status)
        except (ValidationError, AccessError) as exc:
            return _failed(loc.id, "refused", str(exc))
        result = {"location_id": loc.id, "status": status}
        if created:
            result["created_nodes"] = created
        return result

    def _audit_role(self, node, loc, status):
        """One append-only row per role change: ids, the role and the status. The actor is the steward (env.user)."""
        self.env["onto.audit"].create(
            {
                "company_id": node.company_id.id,
                "action": "onto.orgmap.role",
                "res_model": self._name,
                "res_id": node.id,
                "detail": "location=%d role=%s status=%s" % (loc.id, node.role, status),
            }
        )

    def _warehouse_node(self, warehouse, company, created):
        """The warehouse's node, or the minimum chain above it when it has none. Created ids go to `created`."""
        node = self.search([("kind", "=", "warehouse"), ("warehouse_id", "=", warehouse.id)], limit=1)
        if node:
            return node
        group = self._company_group(company, created)
        brand = self._node_under("brand", company.name, group, company, created)
        branch = self._node_under("branch", warehouse.name, brand, company, created)
        return self._new_node(created, name=warehouse.name, kind="warehouse", parent_id=branch.id,
                              warehouse_id=warehouse.id, company_id=company.id)

    def _company_group(self, company, created):
        """The company's only group, else its group named after the company (created when neither exists)."""
        groups = self.search([("kind", "=", "group"), ("company_id", "=", company.id)])
        if len(groups) == 1:
            return groups
        named = groups.filtered(lambda g: g.name == company.name)[:1]
        return named or self._new_node(created, name=company.name, kind="group", company_id=company.id)

    def _node_under(self, kind, name, parent, company, created):
        node = self.search([("kind", "=", kind), ("name", "=", name), ("parent_id", "=", parent.id)], limit=1)
        return node or self._new_node(created, name=name, kind=kind, parent_id=parent.id, company_id=company.id)

    def _new_node(self, created, **vals):
        node = self.create(vals)
        created.append(node.id)
        return node


def _failed(location_id, error, message):
    return {"location_id": location_id, "status": "failed", "error": error, "message": message}


class OntoOrgLocation(models.Model):
    _name = "onto.org.location"
    _description = "Onto org map per stock location (read only, view onto_org_map)"
    _auto = False
    _log_access = False
    _table = ORG_LOCATION_VIEW
    _order = "location_id"

    location_id = fields.Many2one("stock.location", readonly=True)
    warehouse_id = fields.Many2one("stock.warehouse", readonly=True)
    group_id = fields.Many2one("onto.org.map", readonly=True)
    brand_id = fields.Many2one("onto.org.map", readonly=True)
    brand_name = fields.Char(readonly=True)
    region_id = fields.Many2one("onto.org.map", readonly=True)
    region_name = fields.Char(readonly=True)
    branch_id = fields.Many2one("onto.org.map", readonly=True)
    branch_name = fields.Char(readonly=True)
    role = fields.Selection(ROLES, readonly=True)
