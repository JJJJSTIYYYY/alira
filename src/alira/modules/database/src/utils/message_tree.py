"""Message-node grouping, visible paths, and conversation branch selection."""

from collections import defaultdict
from collections.abc import Collection
from operator import itemgetter
from typing import Any


class MessageNodeTree:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

        self.nodes = []
        self.node_map = {}
        self.children_map = defaultdict(list)

        self._build()

    # Group rows by node_id
    def _group_by_node(self):
        groups = defaultdict(list)
        for row in self.rows:
            groups[row["node_id"]].append(row)
        return groups.values()

    def _build_node(self, rows: list[dict[str, Any]]):
        if len(rows) > 1:
            rows.sort(key=itemgetter("msg_cursor"))

        first = rows[0]
        last = rows[-1]

        visible = not first.get("is_deleted") or any(
            not row.get("is_deleted") for row in rows
        )

        return {
            "node_id": last["node_id"],
            "parent_id": first["parent_id"],
            "rows": rows,
            "first_cursor": first["msg_cursor"],
            "last_cursor": last["msg_cursor"],
            "visible": visible,
        }

    def _build(self):
        groups = self._group_by_node()
        self.nodes = [self._build_node(g) for g in groups]

        root_node = {
            "node_id": "-",
            "parent_id": None,
            "rows": [],
            "first_cursor": -1,
            "last_cursor": -1,
            "visible": True,
        }

        self.nodes.insert(0, root_node)

        for n in self.nodes:
            if n["node_id"] not in self.node_map:
                self.node_map[n["node_id"]] = n
            else:
                if n["last_cursor"] > self.node_map[n["node_id"]]["last_cursor"]:
                    self.node_map[n["node_id"]] = n

        self.children_map = self._relink_deleted_nodes()

    def build_visible_messages(
        self,
        current_node_id: str | None,
        *,
        allow_roles: Collection[str],
        guess_children: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, str | int]]], list[str]]:
        """Select visible rows, sibling branches, and the ordered node chain.

        Missing selections use the latest node; deleted selections use their
        nearest visible ancestor. ``guess_children`` extends that path to its
        latest leaf, otherwise rows stop at the selected node's last cursor.
        Returned rows reference the input rows; values are neither copied nor
        decoded here.
        """
        if not self.rows:
            return [], {}, []

        # fallback current node
        if current_node_id is None or current_node_id not in self.node_map:
            last_node = max(self.nodes, key=lambda x: x["last_cursor"])
            current_node_id = last_node["node_id"]

        # If current node is not visible (deleted), find the nearest parent node.
        current_node = self.find_nearest_visible(current_node_id)
        if current_node:
            current_node_id = current_node["node_id"]

        # build branch
        if guess_children:
            branch = self.build_branch(current_node_id)
        else:
            branch = self.get_path(current_node_id)

        cutoff = None
        if not guess_children:
            node = self.node_map.get(current_node_id)
            if node:
                cutoff = node["last_cursor"]

        # Each node's rows are sorted. Concatenation stays sorted unless node
        # cursor ranges overlap; only that case needs a final global sort.
        visible_messages = []
        roles = frozenset(allow_roles)
        ordered = True
        previous_cursor = None
        for node in branch:
            if not node["rows"]:
                continue
            if previous_cursor is not None and node["first_cursor"] < previous_cursor:
                ordered = False
            previous_cursor = node["last_cursor"]
            for row in node["rows"]:
                if cutoff is not None and row["msg_cursor"] > cutoff:
                    break
                if not row["is_deleted"] and row.get("role") in roles:
                    visible_messages.append(row)
        if not ordered:
            visible_messages.sort(key=itemgetter("msg_cursor"))

        node_id_chain = []
        for message in visible_messages:
            if not node_id_chain or node_id_chain[-1] != message["node_id"]:
                node_id_chain.append(message["node_id"])

        # build branches info
        branches = {}

        if guess_children:
            visited_parent = set()

            for node in branch:
                parent_id = node["parent_id"]

                if parent_id in visited_parent:
                    continue

                visited_parent.add(parent_id)

                siblings = self.get_children(parent_id)

                # Relinking includes visible nodes only.
                if len(siblings) > 1:
                    branches[parent_id] = [
                        {
                            "node_id": c["node_id"],
                            "cursor": c["first_cursor"],
                        }
                        for c in siblings
                    ]

        return visible_messages, branches, node_id_chain

    def get_children(self, node_id: str):
        return self.children_map.get(node_id, [])

    def get_path(self, node_id: str):
        path = []
        cur = self.node_map.get(node_id)

        while cur:
            path.append(cur)
            cur = self.node_map.get(cur["parent_id"])

        return list(reversed(path))

    def extend_path(self, path: list[dict[str, Any]]):
        if not path:
            return path

        cur = path[-1]

        while True:
            children = self.children_map.get(cur["node_id"])

            if not children:
                break

            next_node = max(children, key=lambda x: x["last_cursor"])

            path.append(next_node)
            cur = next_node

        return path

    def build_branch(self, current_node_id: str):
        path = self.get_path(current_node_id)
        return self.extend_path(path)

    def flatten_branch(self, branch: list[dict[str, Any]]):
        rows = []

        for node in branch:
            rows.extend(node["rows"])

        rows.sort(key=lambda x: x["msg_cursor"])
        return rows

    def find_nearest_visible(self, node_id: str):
        cur = self.node_map.get(node_id)

        while cur:
            if cur.get("visible"):
                return cur
            cur = self.node_map.get(cur["parent_id"])

        return None

    def _relink_deleted_nodes(self):
        """
        Rebuild parent-child relationship
        """

        new_children_map = defaultdict(list)
        visible_ancestors = {}

        def find_visible_ancestor(node):
            cur = self.node_map.get(node["parent_id"])
            skipped = []
            while cur and not cur["visible"]:
                node_id = cur["node_id"]
                if node_id in visible_ancestors:
                    cur = visible_ancestors[node_id]
                    break
                skipped.append(node_id)
                cur = self.node_map.get(cur["parent_id"])
            for node_id in skipped:
                visible_ancestors[node_id] = cur
            return cur

        for node in self.nodes:
            if node["node_id"] == "-":
                continue

            if node.get("visible"):
                parent = self.node_map.get(node["parent_id"])

                if parent and parent.get("visible"):
                    new_parent_id = parent["node_id"]
                else:
                    ancestor = find_visible_ancestor(node)
                    new_parent_id = ancestor["node_id"] if ancestor else "-"

                node["parent_id"] = new_parent_id
                new_children_map[new_parent_id].append(node)

            else:
                continue

        for children in new_children_map.values():
            if len(children) > 1:
                children.sort(key=itemgetter("first_cursor"))

        return new_children_map
