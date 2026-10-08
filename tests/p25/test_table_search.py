"""P2.5 Task 6：按关键词搜表、表结构分次取得、续取游标与刷新后失效（离线）。

真实 ``StarRocksAdapter`` + ``SchemaCache`` + ``starrocks_tools`` 的执行函数，最底层连接换成
recording 驱动替身（同 ``test_schema_scope``）。替身能证明匹配、按字节规划的页、游标与快照及参数
的绑定、参数在 I/O 前的拒绝，以及每页只探测本页对象；不能证明 StarRocks 元数据与权限的实际行为
（见 ``tests/p1b/test_starrocks_real.py``）。
"""

import json
from typing import Any

import pytest
from asyncmy.errors import OperationalError
from tests.p1b.test_starrocks_adapter import TARGET, Result
from tests.p25.test_schema_scope import Clock, Tables, Tools, World, cache_for, forget, statements

from xiaowei.governance import ToolRejectedError
from xiaowei.models import AUDIENCES, ToolObservation
from xiaowei.starrocks import SCHEMA_OBJECTS_SQL, StarRocksError, StarRocksErrorCode, probe_sql
from xiaowei.starrocks_tools import (
    CURSOR_INVALID,
    CURSOR_MISMATCH,
    DESCRIBE_TABLE,
    LIST_TABLES,
    SNAPSHOT_STALE,
    DescribeTableArgs,
    ListTablesArgs,
    starrocks_tools,
)

PAGE = TARGET.policy.max_rows

CATALOG: Tables = {
    ("ads", "clicks"): (("id", "int", "NO", None),),  # 不匹配、排在最前
    ("shop", "orders"): (("id", "int", "NO", None), ("region", "varchar", "YES", None)),
    ("shop", "order_items"): (("order_id", "int", "NO", None),),
    ("shop", "regions"): (("name", "varchar", "NO", None),),
    ("crm", "orders"): (("id", "int", "NO", None),),  # 与 shop.orders 同名
    ("crm", "customers"): (("id", "int", "NO", None), ("ORDER_COUNT", "int", "YES", None)),
    ("hr", "staff"): (("id", "int", "NO", None),),
}
COMMENTS = {("hr", "staff"): "员工花名册（含 Order 负责人）"}


def commented(world: World, comments: dict[tuple[str, str], str]) -> None:
    """替身的对象元数据带上表注释（``schema_results`` 默认没有注释）。"""
    objects = world.driver().make().results[SCHEMA_OBJECTS_SQL]
    rows = [
        (db, name, kind, comments.get((db, name), comment), created)
        for db, name, kind, comment, created in objects.rows
    ]
    world.overrides = {SCHEMA_OBJECTS_SQL: Result(objects.columns, rows)}


async def search_tools(tables: Tables = CATALOG, comments: dict[Any, str] | None = None) -> Tools:
    world = World(tables=dict(tables))
    commented(world, comments if comments is not None else COMMENTS)
    drv, clock = world.driver(), Clock()
    adapter, cache = cache_for(drv, clock)
    assert await cache.refresh()
    forget(drv)
    world.overrides = {}
    assembled = starrocks_tools(adapter, dict.fromkeys(AUDIENCES, 400_000), schema=cache)
    return Tools(drv, Clock(), cache, assembled.executes, world)


def search(**changes: object) -> dict[str, object]:
    return {"keyword": "order", "database": None, "page_size": PAGE, "cursor": None} | changes


def shown(observation: ToolObservation) -> list[tuple[str, str]]:
    return [(r["database"], r["name"]) for r in observation.payload["rows"]]  # type: ignore[index, union-attr]


async def walk(t: Tools, tool_id: str, **arguments: object) -> list[ToolObservation]:
    """从头开始，原样传回 ``next_cursor`` 直到它为 null；返回每次的结果。"""
    pages = [await t.call(tool_id, **arguments, cursor=None)]
    while (following := pages[-1].payload["next_cursor"]) is not None:
        assert pages[-1].truncated  # 还有未交付的内容时如实标记
        pages.append(await t.call(tool_id, **arguments, cursor=following))
        assert len(pages) <= 50
    assert not pages[-1].truncated
    return pages


def fits(observation: ToolObservation) -> bool:
    rows = observation.payload["rows"]
    return len(json.dumps(rows, ensure_ascii=False).encode()) <= TARGET.max_result_bytes


# ---- 匹配 ---------------------------------------------------------------------------------------


async def test_keyword_matches_names_comments_and_columns_case_insensitively() -> None:
    t = await search_tools()
    observation = await t.call(LIST_TABLES, **search(keyword="ORDER"))
    # 表名（含同名的两张 orders）、列名（ORDER_COUNT、order_id）与表注释都匹配；按库表名排序。
    assert shown(observation) == [
        ("crm", "customers"),
        ("crm", "orders"),
        ("hr", "staff"),
        ("shop", "order_items"),
        ("shop", "orders"),
    ]
    assert observation.payload["rows"][2]["comment"] == COMMENTS[("hr", "staff")]  # type: ignore[index]
    assert not observation.truncated and observation.payload["next_cursor"] is None
    # 只探测本页的对象，没有其他元数据读取。
    assert sorted(statements(t.drv)) == sorted(probe_sql(*key) for key in shown(observation))


async def test_database_filter_keeps_same_named_tables_apart() -> None:
    t = await search_tools()
    crm = await t.call(LIST_TABLES, **search(keyword="orders", database="crm"))
    shop = await t.call(LIST_TABLES, **search(keyword="orders", database="shop"))
    assert (shown(crm), shown(shop)) == ([("crm", "orders")], [("shop", "orders")])
    nothing = await t.call(LIST_TABLES, **search(keyword="orders", database="nowhere"))
    assert shown(nothing) == [] and not nothing.truncated


async def test_no_match_is_an_empty_untruncated_page() -> None:
    t = await search_tools()
    observation = await t.call(LIST_TABLES, **search(keyword="payroll"))
    assert shown(observation) == [] and not observation.truncated
    assert observation.payload["next_cursor"] is None and statements(t.drv) == []


async def test_browse_a_database_without_guessing_a_keyword() -> None:
    arguments = search(keyword=None, database="shop", page_size=2)
    ListTablesArgs.model_validate({"cluster": TARGET.target_id, **arguments})
    t = await search_tools()
    pages = await walk(t, LIST_TABLES, keyword=None, database="shop", page_size=2)
    assert [key for p in pages for key in shown(p)] == [
        ("shop", "order_items"),
        ("shop", "orders"),
        ("shop", "regions"),
    ]


async def test_database_names_are_complete_paginated_and_currently_readable() -> None:
    t = await search_tools()
    tool = "local/list_databases"
    assert (tool, TARGET.target_id) in t.executes
    # 一个库全撤权不展示；另一个库首表撤权仍须寻找同库的其他可读表。
    t.world.denied = {("ads", "clicks"), ("crm", "customers")}
    pages = await walk(t, tool, page_size=2)
    assert [row for p in pages for row in p.payload["rows"]] == [
        {"database": "crm"},
        {"database": "hr"},
        {"database": "shop"},
    ]
    assert all(p.payload["columns"] == ["database"] for p in pages)
    assert [d.database for p in pages for d in p.dependencies] == ["crm", "hr", "shop"]
    assert all(fits(p) for p in pages)


async def test_database_listing_batches_probes_and_resumes_a_revoked_large_database() -> None:
    tables = {
        **{(db, "visible"): (("id", "int", "NO", None),) for db in "abcd"},
        **{("z", f"t{i:02d}"): (("id", "int", "NO", None),) for i in range(40)},
    }
    t = await search_tools(tables, {})
    t.world.denied = {key for key in tables if key[0] == "z"}
    first = await t.call("local/list_databases", page_size=5, cursor=None)
    assert first.payload["rows"] == [{"database": db} for db in "abcd"]
    assert [d.database for d in first.dependencies] == list("abcd")
    assert first.truncated and first.payload["next_cursor"] is not None
    assert t.drv.attempts <= 5  # 首批四库一连接，撤权库分批探测；逐对象连接会超出。
    assert len([s for s in statements(t.drv) if s.startswith("SELECT 1 FROM")]) <= 32

    forget(t.drv)
    second = await t.call(
        "local/list_databases", page_size=5, cursor=first.payload["next_cursor"]
    )
    assert second.payload["rows"] == [] and second.payload["next_cursor"] is None
    assert not second.truncated and t.drv.attempts <= 3
    assert len([s for s in statements(t.drv) if s.startswith("SELECT 1 FROM")]) == 12


async def test_database_listing_does_not_skip_a_late_readable_object() -> None:
    tables = {
        **{("a", f"t{i:02d}"): (("id", "int", "NO", None),) for i in range(40)},
        ("b", "visible"): (("id", "int", "NO", None),),
    }
    t = await search_tools(tables, {})
    t.world.denied = {key for key in tables if key[0] == "a" and key[1] != "t39"}
    pages = await walk(t, "local/list_databases", page_size=5)
    assert [p.payload["rows"] for p in pages] == [[], [{"database": "a"}, {"database": "b"}]]
    assert [[(d.database, d.name) for d in p.dependencies] for p in pages] == [
        [], [("a", "t39"), ("b", "visible")]
    ]
    assert sum(1 for s in statements(t.drv) if s.startswith("SELECT 1 FROM")) <= 42


async def test_database_listing_probe_failure_does_not_return_partial_page() -> None:
    tables = {
        ("a", "visible"): (("id", "int", "NO", None),),
        **{("z", f"t{i:02d}"): (("id", "int", "NO", None),) for i in range(12)},
    }
    t = await search_tools(tables, {})
    t.world.denied = {key for key in tables if key[0] == "z"}
    t.world.overrides[probe_sql("z", "t04")] = OperationalError(1105, "connection failed")
    with pytest.raises(StarRocksError) as error:
        await t.call("local/list_databases", page_size=2, cursor=None)
    assert error.value.code == StarRocksErrorCode.QUERY_FAILED
    assert t.drv.attempts == 2


@pytest.mark.parametrize("tool", [LIST_TABLES, DESCRIBE_TABLE])
async def test_unchanged_refresh_does_not_interrupt_continuation(tool: str) -> None:
    catalog = numbered(7) if tool == LIST_TABLES else {("shop", "wide"): WIDE_COLUMNS}
    t = await search_tools(catalog, {})
    args = (
        search(keyword="t0", page_size=3)
        if tool == LIST_TABLES
        else {"database": "shop", "table": "wide", "cursor": None}
    )
    first = await t.call(tool, **args)
    cursor = first.payload["next_cursor"]
    assert cursor is not None
    assert await t.cache.refresh()
    following = await t.call(tool, **(args | {"cursor": cursor}))
    first_names = {r["name"] for r in first.payload["rows"]}
    assert first_names.isdisjoint(r["name"] for r in following.payload["rows"])


# ---- 续取游标 -----------------------------------------------------------------------------------


def numbered(count: int) -> Tables:
    return {("shop", f"t{i:02d}"): (("id", "int", "NO", None),) for i in range(count)}


def every(count: int) -> list[tuple[str, str]]:
    return [("shop", f"t{i:02d}") for i in range(count)]


async def test_pages_walk_the_matches_in_order_until_the_cursor_is_null() -> None:
    t = await search_tools(numbered(7), {})
    pages = await walk(t, LIST_TABLES, keyword="t0", database=None, page_size=3)
    assert [shown(p) for p in pages] == [every(7)[0:3], every(7)[3:6], every(7)[6:]]
    # 每页只探测本页对象：总共 7 次，不随续页累积重探前面的页。
    assert len(statements(t.drv)) == 7


async def test_a_byte_limited_page_continues_from_the_first_object_it_left_out() -> None:
    """审查复现：第 1 页按字节只放得下前几行时，续页必须从第一个未交付的对象开始。"""
    tables = numbered(8)
    t = await search_tools(tables, dict.fromkeys(tables, "x" * 150))  # 每行约 220 字节
    pages = await walk(t, LIST_TABLES, keyword="shop.t", database=None, page_size=5)
    assert len(shown(pages[0])) < 5  # 字节上限先于 page_size 生效
    assert [key for p in pages for key in shown(p)] == every(8)  # 无遗漏、无重复、按序
    assert all(fits(p) for p in pages)
    # 每个对象只探测一次：页在 I/O 前按字节规划，不探测放不下的对象。
    assert sorted(statements(t.drv)) == sorted(probe_sql(*key) for key in every(8))


async def test_objects_revoked_since_the_refresh_are_left_out_without_shifting() -> None:
    t = await search_tools(numbered(7), {})
    t.world.denied = {("shop", "t01"), ("shop", "t03")}
    pages = await walk(t, LIST_TABLES, keyword="t0", database=None, page_size=3)
    # 撤权的不列出；续页从本页之后开始，其余对象恰好各出现一次。
    assert [shown(p) for p in pages] == [
        [("shop", "t00"), ("shop", "t02")],
        [("shop", "t04"), ("shop", "t05")],
        [("shop", "t06")],
    ]


async def test_a_cursor_after_a_refresh_is_rejected_before_io() -> None:
    t = await search_tools(numbered(7), {})
    first = await t.call(LIST_TABLES, **search(keyword="t0", page_size=3))
    t.clock.advance(1)
    t.world.tables[("shop", "added")] = (("id", "int", "NO", None),)
    assert await t.cache.refresh()  # 内容确有变化，旧游标不再可用。
    forget(t.drv)
    with pytest.raises(ToolRejectedError) as stale:
        await t.call(
            LIST_TABLES, **search(keyword="t0", page_size=3, cursor=first.payload["next_cursor"])
        )
    assert str(stale.value) == SNAPSHOT_STALE and t.drv.attempts == 0
    # 从头重新搜索，之后照常续取。
    pages = await walk(t, LIST_TABLES, keyword="t0", database=None, page_size=3)
    assert [key for p in pages for key in shown(p)] == every(7)


@pytest.mark.parametrize(
    "changes",
    [{"keyword": "t00"}, {"keyword": "shop"}, {"database": "shop"}, {"page_size": 2}],
    ids=["换关键词", "换成同样匹配的关键词", "加库过滤", "换页大小"],
)
async def test_a_cursor_is_not_mistaken_for_a_page_of_another_search(
    changes: dict[str, object],
) -> None:
    t = await search_tools(numbered(7), {})
    first = await t.call(LIST_TABLES, **search(keyword="t0", page_size=3))
    forget(t.drv)
    following = first.payload["next_cursor"]
    with pytest.raises(ToolRejectedError) as refused:
        await t.call(LIST_TABLES, **search(keyword="t0", page_size=3, cursor=following) | changes)
    assert str(refused.value) == CURSOR_MISMATCH and t.drv.attempts == 0


async def test_a_hand_written_offset_is_rejected() -> None:
    t = await search_tools(numbered(7), {})
    first = await t.call(LIST_TABLES, **search(keyword="t0", page_size=3))
    version, _, tag = str(first.payload["next_cursor"]).split("-")
    forget(t.drv)
    with pytest.raises(ToolRejectedError) as refused:
        await t.call(LIST_TABLES, **search(keyword="t0", page_size=3, cursor=f"{version}-6-{tag}"))
    assert str(refused.value) == CURSOR_MISMATCH and t.drv.attempts == 0


async def test_one_object_too_large_for_a_result_is_a_safe_correctable_rejection() -> None:
    tables = numbered(3)
    huge = {("shop", "t01"): "注" * 120}  # 单值超过 max_value_bytes（200 字节）
    t = await search_tools(tables, huge)
    first = await t.call(LIST_TABLES, **search(keyword="t0", page_size=3))
    assert shown(first) == [("shop", "t00")]  # 放不下的那一行留给下一页，不跳过
    forget(t.drv)
    with pytest.raises(ToolRejectedError, match="超过单值或结果字节上限") as refused:
        await t.call(
            LIST_TABLES, **search(keyword="t0", page_size=3, cursor=first.payload["next_cursor"])
        )
    # I/O 前拒绝，原因不是“换更小的页”（换页大小也放不下）；不点名可能已撤权的对象。
    assert "page_size" not in str(refused.value) and "t01" not in str(refused.value)
    assert t.drv.attempts == 0
    # 可按提示避开它。
    assert shown(await t.call(LIST_TABLES, **search(keyword="t02", page_size=1))) == [
        ("shop", "t02")
    ]


# ---- 表结构分次取得 -----------------------------------------------------------------------------

WIDE_COLUMNS = tuple((f"c{i:02d}_" + "y" * 40, "varchar", "YES", None) for i in range(20))


async def test_columns_beyond_the_first_result_can_be_fetched() -> None:
    """审查复现：20 列的宽表首次只放得下几列，后续列须能按游标取得。"""
    t = await search_tools({("shop", "wide"): WIDE_COLUMNS, **numbered(1)}, {})
    pages = await walk(t, DESCRIBE_TABLE, database="shop", table="wide")
    assert len(pages) > 1 and all(fits(p) for p in pages)
    names = [row["name"] for p in pages for row in p.payload["rows"]]  # type: ignore[union-attr]
    assert names == [c[0] for c in WIDE_COLUMNS]  # 无遗漏、无重复、按序，含最后一列
    # 每次都复核当前权限。
    assert statements(t.drv) == [probe_sql("shop", "wide")] * len(pages)


async def test_a_column_cursor_is_bound_to_its_table_and_snapshot() -> None:
    t = await search_tools({("shop", "wide"): WIDE_COLUMNS, ("shop", "wider"): WIDE_COLUMNS}, {})
    first = await t.call(DESCRIBE_TABLE, database="shop", table="wide", cursor=None)
    following = first.payload["next_cursor"]
    forget(t.drv)
    with pytest.raises(ToolRejectedError) as other:
        await t.call(DESCRIBE_TABLE, database="shop", table="wider", cursor=following)
    with pytest.raises(ToolRejectedError) as junk:
        await t.call(DESCRIBE_TABLE, database="shop", table="wide", cursor="page-2")
    t.clock.advance(1)
    t.world.tables[("shop", "wide")] = (*WIDE_COLUMNS, ("new_col", "int", "YES", None))
    assert await t.cache.refresh()
    forget(t.drv)
    with pytest.raises(ToolRejectedError) as stale:
        await t.call(DESCRIBE_TABLE, database="shop", table="wide", cursor=following)
    assert [str(e.value) for e in (other, junk, stale)] == [
        CURSOR_MISMATCH,
        CURSOR_INVALID,
        SNAPSHOT_STALE,
    ]
    assert t.drv.attempts == 0


async def test_a_table_revoked_between_column_pages_fails_the_next_page() -> None:
    t = await search_tools({("shop", "wide"): WIDE_COLUMNS}, {})
    first = await t.call(DESCRIBE_TABLE, database="shop", table="wide", cursor=None)
    t.world.denied = {("shop", "wide")}
    with pytest.raises(StarRocksError) as raised:
        await t.call(
            DESCRIBE_TABLE, database="shop", table="wide", cursor=first.payload["next_cursor"]
        )
    assert raised.value.code is StarRocksErrorCode.OBJECT_UNREADABLE


async def test_one_column_too_large_for_a_result_is_rejected_before_io() -> None:
    columns = (("id", "int", "NO", None), ("x" * 300, "varchar", "YES", None))
    t = await search_tools({("shop", "odd"): columns}, {})
    first = await t.call(DESCRIBE_TABLE, database="shop", table="odd", cursor=None)
    assert [row["name"] for row in first.payload["rows"]] == ["id"]  # type: ignore[union-attr]
    forget(t.drv)
    with pytest.raises(ToolRejectedError, match="超过单值或结果字节上限"):
        await t.call(
            DESCRIBE_TABLE, database="shop", table="odd", cursor=first.payload["next_cursor"]
        )
    assert t.drv.attempts == 0


# ---- 参数在 I/O 前拒绝 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"keyword": "  "}, "关键词"),
        ({"page_size": 0}, "page_size"),
        ({"page_size": PAGE + 1}, "page_size"),
        ({"page_size": True}, "page_size"),
        ({"cursor": "2"}, "cursor 无效"),
        ({"cursor": "0123456789abcdef-0-0123456789abcdef"}, "cursor 无效"),
        ({"cursor": "0123456789abcdef-5-0123456789abcdef"}, "表结构已刷新"),
    ],
    ids=["空关键词", "零页大小", "过大页", "布尔页大小", "页码当游标", "零偏移", "他快照的游标"],
)
async def test_invalid_search_arguments_are_rejected_before_io(
    changes: dict[str, object], reason: str
) -> None:
    t = await search_tools()
    with pytest.raises(ToolRejectedError, match=reason):
        await t.call(LIST_TABLES, **search(**changes))
    assert t.drv.attempts == 0


def test_every_paging_argument_is_required_and_explicit() -> None:
    for model, fields in (
        (ListTablesArgs, {"cluster", "keyword", "database", "page_size", "cursor"}),
        (DescribeTableArgs, {"cluster", "database", "table", "cursor"}),
    ):
        schema = model.model_json_schema()
        assert set(schema["required"]) == fields
        assert schema.get("additionalProperties") is False
