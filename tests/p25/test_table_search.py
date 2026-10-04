"""P2.5 Task 6：按关键词搜表、分页与刷新后旧页失效（离线）。

真实 ``StarRocksAdapter`` + ``SchemaCache`` + ``starrocks_tools`` 的执行函数，最底层连接换成
recording 驱动替身（同 ``test_schema_scope``）。替身能证明匹配、分页、页与快照版本的绑定、参数
在 I/O 前的拒绝，以及每页只探测本页对象；不能证明 StarRocks 元数据与权限的实际行为（见
``tests/p1b/test_starrocks_real.py``）。
"""

from typing import Any

import pytest
from tests.p1b.test_starrocks_adapter import TARGET, Result
from tests.p25.test_schema_scope import Clock, Tables, Tools, World, cache_for, forget, statements

from xiaowei.governance import ToolRejectedError
from xiaowei.models import AUDIENCES, ToolObservation
from xiaowei.starrocks import SCHEMA_OBJECTS_SQL, probe_sql
from xiaowei.starrocks_tools import LIST_TABLES, ListTablesArgs, starrocks_tools

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
    return {
        "keyword": "order",
        "database": None,
        "page": 1,
        "page_size": PAGE,
        "snapshot": None,
    } | (changes)


def shown(observation: ToolObservation) -> list[tuple[str, str]]:
    return [(r["database"], r["name"]) for r in observation.payload["rows"]]  # type: ignore[index, union-attr]


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
    assert not observation.truncated
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
    assert statements(t.drv) == []


# ---- 分页与快照版本 -----------------------------------------------------------------------------


def numbered(count: int) -> Tables:
    return {("shop", f"t{i:02d}"): (("id", "int", "NO", None),) for i in range(count)}


async def test_pages_walk_the_matches_in_order_and_say_when_more_remain() -> None:
    t = await search_tools(numbered(7), {})
    first = await t.call(LIST_TABLES, **search(keyword="t0", page_size=3))
    version = first.payload["snapshot"]
    assert isinstance(version, str) and version
    second = await t.call(
        LIST_TABLES, **search(keyword="t0", page=2, page_size=3, snapshot=version)
    )
    third = await t.call(LIST_TABLES, **search(keyword="t0", page=3, page_size=3, snapshot=version))
    beyond = await t.call(
        LIST_TABLES, **search(keyword="t0", page=4, page_size=3, snapshot=version)
    )
    assert [shown(p) for p in (first, second, third, beyond)] == [
        [("shop", "t00"), ("shop", "t01"), ("shop", "t02")],
        [("shop", "t03"), ("shop", "t04"), ("shop", "t05")],
        [("shop", "t06")],
        [],
    ]
    assert [p.truncated for p in (first, second, third, beyond)] == [True, True, False, False]
    # 每页只探测本页对象：总共 7 次，不随页码累积重探前面的页。
    assert len(statements(t.drv)) == 7


async def test_a_page_after_a_refresh_is_rejected_before_io() -> None:
    t = await search_tools(numbered(7), {})
    first = await t.call(LIST_TABLES, **search(keyword="t0", page_size=3))
    t.clock.advance(1)
    assert await t.cache.refresh()  # 新快照：旧页码对应的列表可能已变化
    forget(t.drv)
    with pytest.raises(ToolRejectedError, match="表结构已刷新"):
        await t.call(
            LIST_TABLES,
            **search(keyword="t0", page=2, page_size=3, snapshot=first.payload["snapshot"]),
        )
    assert t.drv.attempts == 0
    # 从第 1 页重新搜索得到新版本，之后的页照常。
    again = await t.call(LIST_TABLES, **search(keyword="t0", page_size=3))
    assert again.payload["snapshot"] != first.payload["snapshot"]
    second = await t.call(
        LIST_TABLES, **search(keyword="t0", page=2, page_size=3, snapshot=again.payload["snapshot"])
    )
    assert shown(second)[0] == ("shop", "t03")


async def test_objects_revoked_since_the_refresh_are_left_out_of_their_page() -> None:
    t = await search_tools(numbered(7), {})
    t.world.denied = {("shop", "t01")}
    first = await t.call(LIST_TABLES, **search(keyword="t0", page_size=3))
    assert shown(first) == [("shop", "t00"), ("shop", "t02")]
    # 页的划分按快照中的匹配，不因撤权而前移：第 2 页仍从 t03 开始。
    second = await t.call(
        LIST_TABLES, **search(keyword="t0", page=2, page_size=3, snapshot=first.payload["snapshot"])
    )
    assert shown(second)[0] == ("shop", "t03")


# ---- 参数在 I/O 前拒绝 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"keyword": "  "}, "关键词"),
        ({"page_size": 0}, "page_size"),
        ({"page_size": PAGE + 1}, "page_size"),
        ({"page_size": True}, "page_size"),
        ({"page": 0}, "page"),
        ({"page": TARGET.schema_limits.max_objects + 1}, "page"),
        ({"page": 2}, "snapshot"),
        ({"snapshot": "not-the-current-one"}, "表结构已刷新"),
    ],
    ids=[
        "空关键词",
        "零页大小",
        "过大页",
        "布尔页大小",
        "零页码",
        "页码过大",
        "缺版本",
        "版本不符",
    ],
)
async def test_invalid_search_arguments_are_rejected_before_io(
    changes: dict[str, object], reason: str
) -> None:
    t = await search_tools()
    with pytest.raises(ToolRejectedError, match=reason):
        await t.call(LIST_TABLES, **search(**changes))
    assert t.drv.attempts == 0


def test_every_search_argument_is_required_and_explicit() -> None:
    schema = ListTablesArgs.model_json_schema()
    assert set(schema["required"]) == {
        "cluster",
        "keyword",
        "database",
        "page",
        "page_size",
        "snapshot",
    }
    assert schema.get("additionalProperties") is False
