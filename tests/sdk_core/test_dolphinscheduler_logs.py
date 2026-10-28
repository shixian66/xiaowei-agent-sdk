"""S2：真实治理/PG + HTTP transport 替身，检查原文边界和日志中段定位。"""

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from tests.sdk_core.synthetic_tools import Clock, Grants, context, ready_engine, store
from tests.sdk_core.test_dolphinscheduler_runtime import source

from xiaowei.dolphinscheduler import DolphinScheduler, DSConfig, catalog
from xiaowei.governance import DolphinSchedulerReadError, GovernedTools, ToolCatalog
from xiaowei.models import AUDIENCES, ToolRequest

pytestmark = pytest.mark.loopback


@pytest.fixture
async def logs(postgres_url, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    clock, grants, calls = Clock(), Grants(), []
    config = DSConfig.model_validate(source())
    ds = DolphinScheduler([config], clock)
    control = SimpleNamespace(body=b"first\nlast\n", fail=False, stream=None, task_ids=(801,))

    def get(req):
        calls.append(req.url.path)
        if req.url.path.endswith("download-log"):
            if control.fail:
                raise httpx2.ReadTimeout("do-not-echo-body-or-address")
            if control.stream is not None:
                return httpx2.Response(200, stream=control.stream)
            return httpx2.Response(200, content=control.body)
        if req.url.path.endswith("/701"):
            data = {"id": 701, "processDefinitionCode": 99, "processDefinitionVersion": 2}
        elif req.url.path.endswith("task-instances"):
            data = {
                "total": len(control.task_ids),
                "totalList": [
                    {
                        "id": task_id,
                        "processInstanceId": 701,
                        "taskCode": 999,
                        "retryTimes": 0,
                        "flag": "YES",
                        "state": "FAILURE",
                    }
                    for task_id in control.task_ids
                ],
            }
        else:
            data = {"total": 0, "totalList": []}
        return httpx2.Response(200, json={"code": 0, "data": data})

    adapter = ds.adapters["ds-b"]
    await adapter.client.aclose()
    adapter.client = httpx2.AsyncClient(transport=httpx2.MockTransport(get))
    contracts, policies = catalog([config], dict.fromkeys(AUDIENCES, 16_384))
    tools = frozenset(c.tool_id for c in contracts)
    ctx = context(tools=tools, targets=frozenset({"ds-b", "dolphinscheduler"}), max_tool_calls=16)
    for c in contracts:
        grants.grant("alice", c.tool_id, target=c.target_id)
    async with ready_engine(postgres_url) as engine:
        gov = GovernedTools(store(engine, grants, clock, ToolCatalog(contracts, policies)))
        await gov.select_dolphinscheduler(ctx, ("ds-b",))

        async def invoke(name, **extra):
            args = {
                "cluster": "ds-b",
                "project_code": 11,
                "workflow_instance_id": 701,
                "task_id": 801,
                "task_page_no": 1,
                **extra,
            }
            req = ToolRequest(
                tool_id="local/" + name,
                target_id="ds-b",
                call_id="log",
                tool_name=name,
                arguments=args,
            )
            return await gov.invoke(ctx, req, lambda r: ds.execute(r, ctx, gov))

        yield SimpleNamespace(
            ds=ds, gov=gov, ctx=ctx, control=control, calls=calls, invoke=invoke, engine=engine
        )
        await ds.end_turn(ctx.identity)
    await ds.aclose()


def search(**extra):
    return {
        "terms": ["ERROR", "SQLState", "1146"],
        "case_sensitive": False,
        "max_snippets": 8,
        "context_lines": 1,
        **extra,
    }


async def test_10m_middle_database_error_and_long_line_are_reachable_and_cached(logs):
    noise = b"ERROR generic repeated noise\n" * 180_000
    critical = b"x" * 70_000 + (
        b" SQLSyntaxErrorException SQLState 42S02 code 1146: Table 'ods.order_delta' doesn't exist "
        b"password=synthetic-log-secret\n"
    )
    logs.control.body = (
        b"[LOG-PATH]: private/path, [HOST]: private-host\n" + noise + critical + noise
    )
    assert len(logs.control.body) > 10 * 1024 * 1024
    result = await logs.invoke("ds_search_task_log", **search())
    body = json.loads(result.model_content)
    rendered = json.dumps(body, ensure_ascii=False)
    assert "ods.order_delta" in rendered and "42S02" in rendered
    assert "synthetic-log-secret" not in rendered and "private-host" not in rendered
    assert len(result.model_content.encode()) <= 16_384
    record = next(r for r in body["data"]["records"] if "ods.order_delta" in r["text"])
    assert record["line"] == 180_001 and record["byte_offset"] > 65_535
    await logs.invoke(
        "ds_read_task_log", line=record["line"], byte_offset=record["byte_offset"], line_count=2
    )
    assert len(logs.calls) == 3
    assert logs.calls[-1].endswith("/log/11/download-log")


async def test_concurrent_read_and_search_share_one_download(logs):
    results = await asyncio.gather(
        logs.invoke("ds_read_task_log", line=1, byte_offset=0, line_count=10),
        logs.invoke("ds_search_task_log", **search(terms=["last"])),
    )
    assert len(results) == 2
    assert sum(path.endswith("download-log") for path in logs.calls) == 1


async def test_log_timeout_is_cached_without_stopping_same_source_metadata(logs):
    logs.control.fail = True
    for terms in (["ERROR"], ["1146"]):
        with pytest.raises(DolphinSchedulerReadError) as caught:
            await logs.invoke("ds_search_task_log", **search(terms=terms))
        assert caught.value.reason == "ds_timeout"
    assert sum(path.endswith("download-log") for path in logs.calls) == 1
    req = ToolRequest(
        tool_id="local/ds_list_projects",
        target_id="ds-b",
        call_id="metadata",
        tool_name="ds_list_projects",
        arguments={"cluster": "ds-b", "keyword": None, "page_no": 1, "page_size": 20},
    )
    result = await logs.gov.invoke(logs.ctx, req, lambda r: logs.ds.execute(r, logs.ctx, logs.gov))
    assert json.loads(result.model_content)["data"]["records"] == []


async def test_single_log_cap_discloses_prefix_without_claiming_eof(logs):
    logs.ds.adapters["ds-b"].config = logs.ds.adapters["ds-b"].config.model_copy(
        update={"max_log_bytes": 4096}
    )
    logs.control.body = b"ERROR start\n" + b"x" * 10_000
    result = await logs.invoke("ds_search_task_log", **search(terms=["ERROR"]))
    data = json.loads(result.model_content)["data"]
    assert data["coverage"]["received_bytes"] == 4096
    assert not data["coverage"]["reached_end"]
    assert data["coverage"]["truncation_reason"] == "log_byte_limit"
    await logs.ds.end_turn(logs.ctx.identity)
    assert not logs.ds._turns


async def test_resume_cannot_redownload_released_sample_in_same_turn(logs):
    await logs.invoke("ds_read_task_log", line=1, byte_offset=0, line_count=1)
    await logs.ds.end_turn(logs.ctx.identity)
    with pytest.raises(DolphinSchedulerReadError) as caught:
        await logs.invoke("ds_search_task_log", **search(terms=["last"]))
    assert caught.value.reason == "ds_sample_expired"
    assert sum(path.endswith("download-log") for path in logs.calls) == 1


async def test_turn_budget_survives_sample_release_and_failed_bytes_are_retained(logs):
    logs.ds._log_limit = 4096
    logs.control.body = b"a" * 8192
    first = await logs.invoke("ds_read_task_log", line=1, byte_offset=0, line_count=1)
    coverage = json.loads(first.model_content)["data"]["coverage"]
    assert coverage["truncation_reason"] == "turn_byte_limit"
    assert logs.gov.turn_runs(logs.ctx.identity).ds_log_bytes == 4096
    await logs.ds.end_turn(logs.ctx.identity)
    # 新任务仍是同一轮，不能借缓存释放恢复额度。
    with pytest.raises(DolphinSchedulerReadError):
        await logs.invoke("ds_read_task_log", task_id=802, line=1, byte_offset=0, line_count=1)
    assert sum(path.endswith("download-log") for path in logs.calls) == 1


async def test_secret_is_masked_even_when_fragment_starts_inside_value(logs):
    logs.control.body = b"prefix password=" + b"verylong" + b"syntheticsecret suffix SQLState 42S02"
    result = await logs.invoke("ds_read_task_log", line=1, byte_offset=24, line_count=1)
    text = result.model_content
    assert "syntheticsecret" not in text
    assert "42S02" in text


async def test_cross_instance_task_never_downloads_log(logs):
    adapter = logs.ds.adapters["ds-b"]
    await adapter.client.aclose()

    def get(req):
        logs.calls.append(req.url.path)
        if req.url.path.endswith("/701"):
            data = {"id": 701, "processDefinitionCode": 99}
        else:
            data = {"totalList": [{"id": 801, "processInstanceId": 999}]}
        return httpx2.Response(200, json={"code": 0, "data": data})

    adapter.client = httpx2.AsyncClient(transport=httpx2.MockTransport(get))
    with pytest.raises(DolphinSchedulerReadError) as caught:
        await logs.invoke("ds_read_task_log", line=1, byte_offset=0, line_count=5)
    assert caught.value.reason == "ds_invalid_response"
    assert not any(path.endswith("download-log") for path in logs.calls)


async def test_partial_bytes_count_even_when_download_later_times_out(logs):
    class Partial(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b"already received" * 256
            raise httpx2.ReadTimeout("after partial response")

    logs.control.stream = Partial()
    with pytest.raises(DolphinSchedulerReadError):
        await logs.invoke("ds_read_task_log", line=1, byte_offset=0, line_count=1)
    assert logs.gov.turn_runs(logs.ctx.identity).ds_log_bytes == len(b"already received" * 256)


def test_common_credential_shapes_mask_values_without_changing_positions():
    from xiaowei.dolphinscheduler_logs import redact_bytes

    raw = b"Authorization: Bearer abcsecret\nBasic abcdef==\njdbc:mysql://user:pass@host/db\n"
    masked = redact_bytes(raw, "")
    assert len(masked) == len(raw)
    assert not any(value in masked for value in (b"abcsecret", b"abcdef", b"user:pass"))


async def test_cancellation_releases_inflight_log_and_turn_cache(logs):
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b"prefix\n"
            entered.set()
            await release.wait()
            yield b"end\n"

    logs.control.stream = Waiting()
    pending = asyncio.create_task(
        logs.invoke("ds_read_task_log", line=1, byte_offset=0, line_count=2)
    )
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await logs.ds.end_turn(logs.ctx.identity)
    assert not logs.ds._turns
    assert logs.gov.turn_runs(logs.ctx.identity).ds_log_bytes == len(b"prefix\n")


@pytest.mark.parametrize(
    "raw,term",
    [
        (b"ERROR head\nend", "head"),
        (b"start\nERROR tail", "tail"),
        ("首行\r\nSQLState 42S02 错误\r\n末行".encode(), "42S02"),
    ],
)
async def test_header_tail_and_utf8_crlf_are_offline_protocol_cases(logs, raw, term):
    logs.control.body = raw
    result = await logs.invoke("ds_search_task_log", **search(terms=[term]))
    assert term in result.model_content
    data = json.loads(result.model_content)["data"]
    assert data["coverage"]["reached_end"]
    assert not data["records"][0]["utf8_replacements"]


async def test_actual_32m_turn_cap_stops_third_log_without_extra_metadata(logs):
    logs.control.task_ids = (801, 802, 803)
    logs.control.body = b"x" * (16 * 1024 * 1024)
    for task_id in (801, 802):
        await logs.invoke("ds_read_task_log", task_id=task_id, line=1, byte_offset=0, line_count=1)
    assert logs.gov.turn_runs(logs.ctx.identity).ds_log_bytes == 32 * 1024 * 1024
    before = list(logs.calls)
    with pytest.raises(DolphinSchedulerReadError):
        await logs.invoke("ds_read_task_log", task_id=803, line=1, byte_offset=0, line_count=1)
    assert logs.calls == before


async def test_inflight_received_bytes_are_not_double_counted_against_reservation(logs):
    first_received, release = asyncio.Event(), asyncio.Event()

    class First(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * 3072
            first_received.set()
            await release.wait()
            yield b"a" * 1024

    logs.ds._log_limit = 8192
    adapter = logs.ds.adapters["ds-b"]
    adapter.config = adapter.config.model_copy(update={"max_log_bytes": 4096})
    await adapter.client.aclose()

    def get(req):
        logs.calls.append(req.url.path)
        if req.url.path.endswith("download-log"):
            if req.url.params["taskInstanceId"] == "801":
                return httpx2.Response(200, stream=First())
            return httpx2.Response(200, content=b"b" * 3000 + b" SQLState 42S02")
        if req.url.path.endswith("/701"):
            data = {"id": 701, "processDefinitionCode": 99}
        else:
            data = {
                "total": 2,
                "totalList": [{"id": t, "processInstanceId": 701} for t in (801, 802)],
            }
        return httpx2.Response(200, json={"code": 0, "data": data})

    adapter.client = httpx2.AsyncClient(transport=httpx2.MockTransport(get))
    pending = asyncio.create_task(
        logs.invoke("ds_read_task_log", line=1, byte_offset=0, line_count=1)
    )
    try:
        await asyncio.wait_for(first_received.wait(), 2)
        second = await logs.invoke("ds_search_task_log", task_id=802, **search(terms=["SQLState"]))
        assert "42S02" in second.model_content
        assert json.loads(second.model_content)["data"]["coverage"]["reached_end"]
    finally:
        release.set()
        await pending
    assert logs.gov.turn_runs(logs.ctx.identity).ds_log_bytes == 4096 + len(
        b"b" * 3000 + b" SQLState 42S02"
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"password":"credential-should-not-leak", "token":"json-token"}',
        b"{'password': 'python-secret', 'api_key': 'api-secret'}",
    ],
)
def test_quoted_credential_keys_cannot_reach_projection(raw):
    from xiaowei.dolphinscheduler_logs import redact_bytes

    masked = redact_bytes(raw, "")
    assert len(masked) == len(raw)
    assert not any(
        value in masked
        for value in (b"credential-should-not-leak", b"json-token", b"python-secret", b"api-secret")
    )


def test_multiline_secret_mask_keeps_original_line_number_and_byte_offset():
    from xiaowei.dolphinscheduler_logs import fragment, redact_bytes

    raw = b'password="first\r\nsecond"\r\nSQLState 42S02'
    masked = redact_bytes(raw, "")
    result = fragment(masked, raw.index(b"SQLState"), len(raw))
    assert len(masked) == len(raw)
    assert result["line"] == 3
    assert result["byte_offset"] == 0
    assert b"first" not in masked and b"second" not in masked


async def test_quoted_credentials_are_excluded_from_all_persisted_audiences(logs):
    from sqlalchemy import text

    logs.control.body = (
        b'{"password":"credential-should-not-leak", "token":"json-token"}\nSQLState 42S02'
    )
    result = await logs.invoke("ds_read_task_log", line=1, byte_offset=0, line_count=2)
    assert "credential-should-not-leak" not in result.model_content
    assert "json-token" not in result.model_content
    async with logs.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT model_content,session_content,web_content,feishu_content "
                    "FROM xiaowei_evidence"
                )
            )
        ).one()
    for value in row:
        assert "credential-should-not-leak" not in value and "json-token" not in value
        assert "42S02" in value


async def test_truncated_quoted_secret_masks_the_entire_accepted_prefix(logs):
    logs.control.body = b'password="first secret-tail never closes SQLState 42S02'
    result = await logs.invoke("ds_read_task_log", line=1, byte_offset=0, line_count=1)
    assert "secret-tail" not in result.model_content
    assert "never closes" not in result.model_content


async def test_search_yields_before_scanning_all_no_match_bytes(monkeypatch):
    from xiaowei import dolphinscheduler_logs as module

    # A pending turn cancellation must be observed during a no-match scan too.
    calls = []
    original = module.re.compile

    class Pattern:
        def __init__(self, value):
            self.value = value

        def finditer(self, data):
            calls.append(len(data))
            return self.value.finditer(data)

    monkeypatch.setattr(module.re, "compile", lambda *args: Pattern(original(*args)))
    task = asyncio.create_task(
        module.search_log(b"x" * (2 * 1024 * 1024), search(terms=["not-present"]))
    )
    asyncio.get_running_loop().call_soon(task.cancel)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sum(calls) < 2 * 1024 * 1024


async def test_literal_search_counts_boundary_and_overlapping_chunk_hits_once():
    from xiaowei.dolphinscheduler_logs import search_log

    raw = b"x" * (65536 - 2) + b"abcabcabc" + b"x" * 65536 + b"abc"
    snippets, counts = await search_log(raw, search(terms=["abcabc", "abc"], context_lines=0))
    assert counts == {"abcabc": 1, "abc": 4}
    assert any("abcabcabc" in item["text"] for item in snippets)
