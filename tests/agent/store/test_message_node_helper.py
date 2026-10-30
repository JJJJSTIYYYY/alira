"""Tests for rebuilding and traversing persisted message-node trees."""

from copy import deepcopy

import pytest

from apix.agent.store.utils.message_node_helper import MessageNodeHelper


def _row(
    node_id: str,
    parent_id: str | None,
    cursor: int,
    *,
    deleted: bool = False,
    **extra,
) -> dict:
    return {
        "node_id": node_id,
        "parent_id": parent_id,
        "msg_cursor": cursor,
        "is_deleted": deleted,
        **extra,
    }


def _node(
    node_id: str,
    cursor: int,
    *,
    visible: bool,
) -> dict:
    """Create a minimal node for isolated traversal-heuristic tests."""
    return {
        "node_id": node_id,
        "parent_id": "-",
        "rows": [],
        "first_cursor": cursor,
        "last_cursor": cursor,
        "visible": visible,
    }


def test_empty_helper_contains_only_root_and_has_empty_traversals():
    helper = MessageNodeHelper([])

    assert helper.nodes == [
        {
            "node_id": "-",
            "parent_id": None,
            "rows": [],
            "first_cursor": -1,
            "last_cursor": -1,
            "visible": True,
        }
    ]
    assert helper.node_map == {"-": helper.nodes[0]}
    assert helper.get_children("missing") == []
    assert helper.get_path("missing") == []
    assert helper.extend_path([]) == []
    assert helper.build_branch("-") == [helper.nodes[0]]
    assert helper.flatten_branch(helper.nodes) == []
    assert helper.find_nearest_visible("missing") is None


def test_grouping_preserves_node_order_and_sorts_rows_by_cursor():
    rows = [
        _row("node-a", "old-parent", 3, deleted=True, content="third"),
        _row("node-b", "-", 4, content="fourth"),
        _row("node-a", "new-parent", 1, content="first"),
        _row("node-a", "new-parent", 2, content="second"),
    ]

    helper = MessageNodeHelper(rows)
    node_a = helper.node_map["node-a"]

    assert [node["node_id"] for node in helper.nodes] == [
        "-",
        "node-a",
        "node-b",
    ]
    assert [row["msg_cursor"] for row in node_a["rows"]] == [1, 2, 3]
    assert node_a["parent_id"] == "-"
    assert node_a["first_cursor"] == 1
    assert node_a["last_cursor"] == 3
    assert node_a["visible"] is True
    assert [node["node_id"] for node in helper.get_children("-")] == [
        "node-a",
        "node-b",
    ]


def test_build_node_uses_shared_parent_and_detects_fully_deleted_node():
    helper = MessageNodeHelper([])
    rows = [
        _row("deleted", "parent", 2, deleted=True),
        _row("deleted", "parent", 1, deleted=True),
    ]

    node = helper._build_node(rows)

    assert node == {
        "node_id": "deleted",
        "parent_id": "parent",
        "rows": rows,
        "first_cursor": 1,
        "last_cursor": 2,
        "visible": False,
    }


def test_missing_deleted_flag_keeps_node_visible():
    helper = MessageNodeHelper([])
    row = {
        "node_id": "visible",
        "parent_id": "-",
        "msg_cursor": 1,
    }

    assert helper._build_node([row])["visible"] is True


@pytest.mark.parametrize(
    ("cursor", "expected_cursor"),
    [
        (5, 5),
        (-2, -1),
    ],
)
def test_real_dash_node_competes_with_synthetic_root(
    cursor,
    expected_cursor,
):
    """Duplicate node IDs retain whichever node has the latest cursor."""
    helper = MessageNodeHelper(
        [
            _row("-", None, cursor),
        ]
    )

    assert helper.node_map["-"]["last_cursor"] == expected_cursor


def test_deleted_nodes_are_skipped_and_visible_descendants_are_relinked():
    rows = [
        _row("parent", "-", 1),
        _row("deleted", "parent", 2, deleted=True),
        _row("child", "deleted", 3),
        _row("orphan", "missing", 4),
        _row("none-parent", None, 5),
    ]

    helper = MessageNodeHelper(rows)

    assert helper.node_map["deleted"]["visible"] is False
    assert helper.node_map["child"]["parent_id"] == "parent"
    assert helper.node_map["orphan"]["parent_id"] == "-"
    assert helper.node_map["none-parent"]["parent_id"] == "-"
    assert helper.get_children("deleted") == []
    assert [node["node_id"] for node in helper.get_children("parent")] == [
        "child",
    ]
    assert [node["node_id"] for node in helper.get_children("-")] == [
        "parent",
        "orphan",
        "none-parent",
    ]


def test_get_path_and_find_nearest_visible_follow_parent_chain():
    helper = MessageNodeHelper(
        [
            _row("parent", "-", 1),
            _row("deleted", "parent", 2, deleted=True),
            _row("child", "deleted", 3),
        ]
    )

    assert [node["node_id"] for node in helper.get_path("child")] == [
        "-",
        "parent",
        "child",
    ]
    assert helper.find_nearest_visible("child")["node_id"] == "child"
    assert helper.find_nearest_visible("deleted")["node_id"] == "parent"
    assert helper.find_nearest_visible("unknown") is None


def test_extend_path_prefers_visible_child_with_latest_cursor():
    helper = MessageNodeHelper(
        [
            _row("start", "-", 1),
            _row("older", "start", 2),
            _row("newer", "start", 4),
            _row("leaf", "newer", 5),
        ]
    )

    initial_path = helper.get_path("start")
    result = helper.extend_path(initial_path)

    assert result is initial_path
    assert [node["node_id"] for node in result] == [
        "-",
        "start",
        "newer",
        "leaf",
    ]
    assert [node["node_id"] for node in helper.build_branch("start")] == [
        "-",
        "start",
        "newer",
        "leaf",
    ]


def test_extend_path_falls_back_to_latest_invisible_child():
    helper = MessageNodeHelper(
        [
            _row("start", "-", 1),
        ]
    )
    older = _node("older-deleted", 2, visible=False)
    newer = _node("newer-deleted", 3, visible=False)
    helper.children_map["start"] = [newer, older]

    path = [helper.node_map["start"]]

    assert [node["node_id"] for node in helper.extend_path(path)] == [
        "start",
        "newer-deleted",
    ]


def test_flatten_branch_combines_and_globally_sorts_rows():
    rows = [
        _row("parent", "-", 3, content="third"),
        _row("parent", "-", 1, content="first"),
        _row("child", "parent", 2, content="second"),
    ]
    helper = MessageNodeHelper(deepcopy(rows))
    branch = [
        helper.node_map["parent"],
        helper.node_map["child"],
    ]

    flattened = helper.flatten_branch(branch)

    assert [row["content"] for row in flattened] == [
        "first",
        "second",
        "third",
    ]


def test_relink_sorts_siblings_by_first_cursor():
    helper = MessageNodeHelper(
        [
            _row("late", "-", 5),
            _row("early", "-", 2),
            _row("middle", "-", 3),
        ]
    )

    assert [node["node_id"] for node in helper.get_children("-")] == [
        "early",
        "middle",
        "late",
    ]


def test_build_visible_messages_handles_empty_input():
    assert MessageNodeHelper([]).build_visible_messages(
        None, allow_roles=("user",)
    ) == ([], {}, [])


def test_build_visible_messages_filters_rows_and_reports_sibling_branches():
    rows = [
        _row("node-a", "-", 1, role="user", metadata="invalid-json"),
        _row("node-a", "-", 2, role="assistant", extensions='{"source":"json"}'),
        _row("node-a", "-", 3, role="internal"),
        _row("node-b", "-", 4, role="user"),
        _row("node-c", "node-a", 5, role="tool"),
        _row("node-c", "node-a", 6, role="tool", deleted=True),
    ]
    original = deepcopy(rows)
    visible, branches, chain = MessageNodeHelper(rows).build_visible_messages(
        "node-a", allow_roles=("user", "assistant", "tool")
    )

    assert [row["msg_cursor"] for row in visible] == [1, 2, 5]
    assert chain == ["node-a", "node-c"]
    assert branches == {
        "-": [
            {"node_id": "node-a", "cursor": 1},
            {"node_id": "node-b", "cursor": 4},
        ]
    }
    assert visible[0] is rows[0]
    assert visible[1] is rows[1]
    assert rows == original


@pytest.mark.parametrize(
    ("selection", "expected_nodes"),
    [("deleted", ["parent"]), ("missing", ["parent", "future"])],
)
def test_build_visible_messages_resolves_deleted_and_missing_selections(
    selection, expected_nodes
):
    helper = MessageNodeHelper(
        [
            _row("parent", "-", 1, role="user"),
            _row("deleted", "parent", 2, role="assistant", deleted=True),
            _row("future", "parent", 3, role="user"),
        ]
    )
    visible, branches, chain = helper.build_visible_messages(
        selection, allow_roles={"user", "assistant"}, guess_children=False
    )

    assert [row["node_id"] for row in visible] == expected_nodes
    assert chain == expected_nodes
    assert branches == {}


def test_build_visible_messages_cuts_off_later_rows_on_an_ancestor():
    helper = MessageNodeHelper(
        [
            _row("parent", "-", 1, role="user"),
            _row("child", "parent", 2, role="assistant"),
            _row("parent", "-", 3, role="user"),
        ]
    )
    visible, branches, chain = helper.build_visible_messages(
        "child", allow_roles=("user", "assistant"), guess_children=False
    )

    assert [row["msg_cursor"] for row in visible] == [1, 2]
    assert chain == ["parent", "child"]
    assert branches == {}


def test_build_visible_messages_handles_fully_deleted_messages():
    helper = MessageNodeHelper(
        [
            _row("deleted", "-", 1, role="user", deleted=True),
        ]
    )
    assert helper.build_visible_messages("deleted", allow_roles=("user",)) == (
        [],
        {},
        [],
    )


@pytest.mark.parametrize("ancestor", ["visible-parent", "missing-parent"])
def test_shared_deleted_ancestors_relink_all_visible_siblings(ancestor):
    rows = [_row("visible-parent", "-", 1, role="user")]
    parent_id = ancestor
    for index in range(128):
        node_id = f"deleted-{index}"
        rows.append(_row(node_id, parent_id, index + 2, role="assistant", deleted=True))
        parent_id = node_id
    for index in range(64):
        rows.append(_row(f"leaf-{index}", parent_id, index + 130, role="assistant"))

    helper = MessageNodeHelper(rows)
    expected_parent = ancestor if ancestor == "visible-parent" else "-"
    siblings = helper.get_children(expected_parent)
    leaves = [node for node in siblings if node["node_id"].startswith("leaf-")]
    assert [node["node_id"] for node in leaves] == [
        f"leaf-{index}" for index in range(64)
    ]
    assert all(node["parent_id"] == expected_parent for node in leaves)
    messages, branches, chain = helper.build_visible_messages(
        "leaf-63", allow_roles=("user", "assistant")
    )
    expected_nodes = (
        ["visible-parent", "leaf-63"] if ancestor == "visible-parent" else ["leaf-63"]
    )
    assert [row["node_id"] for row in messages] == expected_nodes
    assert chain == expected_nodes
    assert [node["node_id"] for node in branches[expected_parent]] == [
        node["node_id"] for node in siblings
    ]


def test_interleaved_node_rows_preserve_global_order_and_repeated_chain_entries():
    rows = [
        _row("child", "parent", 4, role="assistant"),
        _row("parent", "-", 3, role="info"),
        _row("child", "parent", 2, role="assistant"),
        _row("parent", "-", 1, role="user"),
    ]
    original = deepcopy(rows)
    helper = MessageNodeHelper(rows)
    messages, branches, chain = helper.build_visible_messages(
        "child", allow_roles=("user", "assistant", "info"), guess_children=False
    )
    assert [row["msg_cursor"] for row in messages] == [1, 2, 3, 4]
    assert chain == ["parent", "child", "parent", "child"]
    assert branches == {}
    filtered, _, filtered_chain = helper.build_visible_messages(
        "child", allow_roles=("user", "assistant"), guess_children=False
    )
    assert [row["msg_cursor"] for row in filtered] == [1, 2, 4]
    assert filtered_chain == ["parent", "child"]
    assert rows == original
