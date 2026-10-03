"""P2.5 Task 2 审查修订：证据依赖的生成与 StarRocks 复核（离线）。

真实 ``StarRocksAdapter.verify_objects`` + ``DependencyCheck``，只把最底层连接换成 recording
驱动替身。
能证明：依赖只来自快照对象、复核只发零行探测与绑定库表名的版本读取、同一批同一对象只探测一次、
三种结论的分支；不能证明 StarRocks 元数据的实际取值（见 ``tests/p1b/test_starrocks_real.py``）。
"""

from typing import Any

import pytest
from asyncmy.errors import OperationalError
from tests.p1b.test_starrocks_adapter import TARGET, Result
from tests.p25.test_schema_scope import WIDE, Clock, World, cache_for, forget, statements

from xiaowei.models import ObjectDependency
from xiaowei.starrocks import (
    OBJECT_COLUMNS_SQL,
    OBJECT_ID_SQL,
    OBJECT_TYPE_SQL,
    probe_sql,
)
from xiaowei.starrocks_schema import (
    DependencyCheck,
    ObjectInfo,
    SchemaSnapshot,
    listed_dependency,
    read_dependency,
)

VIEW = ("shop", "v_sales")


async def snapshot(world: World) -> tuple[SchemaSnapshot, DependencyCheck, Any]:
    drv, clock = world.driver(), Clock()
    adapter, cache = cache_for(drv, clock)
    assert await cache.refresh()
    forget(drv)
    return cache.current(), DependencyCheck({TARGET.target_id: adapter}), drv


def sales(snap: SchemaSnapshot) -> ObjectInfo:
    found = snap.object("shop", "sales")
    assert found is not None
    return found


def test_read_dependency_carries_the_snapshot_version_and_the_used_columns() -> None:
    obj = ObjectInfo(
        database="shop",
        name="sales",
        type="BASE TABLE",
        comment=None,
        created_at="2026-09-01T08:00:00+08:00",
        table_id=101,
        columns=(),
    )
    dep = read_dependency(obj, {"region"})
    assert (dep.use, dep.type, dep.table_id) == ("read", "BASE TABLE", 101)
    assert dep.replayable
    assert not read_dependency(obj.__class__(**{**obj.__dict__, "type": "VIEW"})).replayable
    listed = listed_dependency("shop", "sales")
    assert listed.use == "listed" and listed.replayable and listed.type is None
    assert "created_at" not in dep.model_dump()  # 创建时间不参与版本（见 ObjectVersion）


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "BASE TABLE"},
        {"table_id": 1},
        {"columns": (("a", "int"),)},
        {"replayable": False},
    ],
)
def test_listed_dependencies_cannot_carry_versions(bad: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "database": "shop",
        "name": "sales",
        "use": "listed",
        "type": None,
        "table_id": None,
        "columns": (),
        "replayable": True,
    }
    with pytest.raises(ValueError, match="listed"):
        ObjectDependency(**{**values, **bad})


async def test_unchanged_objects_are_valid_and_each_object_is_probed_once() -> None:
    snap, check, drv = await snapshot(World())
    deps = [
        read_dependency(sales(snap), {"region"}),
        read_dependency(sales(snap), {"total"}),
        listed_dependency("shop", "sales"),
        listed_dependency("shop", "regions"),
    ]
    assert await check(TARGET.target_id, deps) == "valid"
    sent = statements(drv)
    assert sent.count(probe_sql("shop", "sales")) == 1
    assert sent.count(probe_sql("shop", "regions")) == 1
    # 只为 read 依赖读取版本，且只用绑定的库表名；没有其他语句。
    assert sorted(set(sent) - {probe_sql("shop", "sales"), probe_sql("shop", "regions")}) == sorted(
        {OBJECT_TYPE_SQL, OBJECT_ID_SQL, OBJECT_COLUMNS_SQL}
    )
    args = [a for c in drv.connections for sql, a in c.executed if sql == OBJECT_TYPE_SQL]
    assert args == [("shop", "sales")]
    assert len(drv.connections) == 1


async def test_listed_dependencies_only_probe() -> None:
    _, check, drv = await snapshot(World())
    assert await check(TARGET.target_id, [listed_dependency("shop", "sales")]) == "valid"
    assert statements(drv) == [probe_sql("shop", "sales")]


async def test_revoked_or_dropped_objects_are_invalid() -> None:
    world = World()
    snap, check, _ = await snapshot(world)
    world.denied = {("shop", "sales")}
    assert await check(TARGET.target_id, [read_dependency(sales(snap))]) == "invalid"
    assert await check(TARGET.target_id, [listed_dependency("shop", "sales")]) == "invalid"


@pytest.mark.parametrize(
    "change",
    ["recreated", "retyped column", "dropped column", "became a view"],
)
async def test_changed_versions_are_invalid(change: str) -> None:
    world = World()
    snap, check, _ = await snapshot(world)
    dep = read_dependency(sales(snap), {"region", "total"})
    ids: dict[str, Any] = {}
    if change == "recreated":
        ids[OBJECT_ID_SQL] = Result(("id",), [(999,)])
    elif change == "retyped column":
        cols = (("region", "bigint", "YES", None), ("total", "decimal", "YES", None))
        world.tables = {**WIDE, ("shop", "sales"): cols}
    elif change == "dropped column":
        world.tables = {**WIDE, ("shop", "sales"): (("region", "varchar", "YES", None),)}
    else:
        ids[OBJECT_TYPE_SQL] = Result(("type",), [("VIEW",)])
    world.overrides = ids
    assert await check(TARGET.target_id, [dep]) == "invalid"


async def test_a_column_added_elsewhere_keeps_the_dependency_valid() -> None:
    world = World()
    snap, check, _ = await snapshot(world)
    dep = read_dependency(sales(snap), {"region"})
    world.tables = {
        **WIDE,
        ("shop", "sales"): (*WIDE[("shop", "sales")], ("x", "int", "YES", None)),
    }
    assert await check(TARGET.target_id, [dep]) == "valid"


@pytest.mark.parametrize(
    "failure",
    [
        {"probe": OperationalError(2013, "lost")},
        {"version": OperationalError(2013, "lost")},
        {"version": Result(("unexpected",), [("x",)])},
        {"connect": OperationalError(2003, "down")},
    ],
    ids=["probe lost", "version lost", "version contract", "connect"],
)
async def test_unverifiable_is_not_mistaken_for_revocation(failure: dict[str, Any]) -> None:
    world = World()
    snap, check, drv = await snapshot(world)
    if "probe" in failure:
        world.overrides = {probe_sql("shop", "sales"): failure["probe"]}
    elif "version" in failure:
        world.overrides = {OBJECT_COLUMNS_SQL: failure["version"]}
    else:
        drv.connect_error = failure["connect"]
    assert await check(TARGET.target_id, [read_dependency(sales(snap))]) == "unverifiable"


async def test_unknown_targets_are_invalid_without_io() -> None:
    snap, check, drv = await snapshot(World())
    assert await check("sr-other", [read_dependency(sales(snap))]) == "invalid"
    assert drv.attempts == 0
