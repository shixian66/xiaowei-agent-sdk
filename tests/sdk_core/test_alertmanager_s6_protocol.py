"""锁版 Alertmanager 的静默写协议；只操作隔离 loopback 合成数据。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from tests.sdk_core.alertmanager_fixture import PASSWORD, official, seed, silence
from tests.sdk_core.alertmanager_fixture import alertmanager_binary as alertmanager_binary

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("years", [1, 3])
def test_official_create_expire_and_auth_zero_writes(alertmanager_binary: Path, years: int) -> None:
    with (
        official(alertmanager_binary) as url,
        httpx.Client(base_url=url, auth=("reader", PASSWORD), trust_env=False) as client,
    ):
        current = datetime.now(UTC)
        value = silence()
        for key in ("id", "status", "updatedAt", "annotations"):
            value.pop(key)
        value.update(
            startsAt=(current - timedelta(minutes=1)).isoformat(),
            endsAt=(current + timedelta(days=365 * years)).isoformat(),
            createdBy="xiaowei",
        )
        for auth in (None, ("reader", "wrong")):
            response = httpx.post(f"{url}/api/v2/silences", json=value, auth=auth, trust_env=False)
            assert response.status_code == 401
            assert client.get("/api/v2/silences").json() == []
        response = client.post("/api/v2/silences", json=value)
        assert response.status_code == 200
        sid = response.json()["silenceID"]
        actual = client.get(f"/api/v2/silence/{sid}").json()
        assert actual["status"]["state"] == "active"
        assert (
            datetime.fromisoformat(actual["endsAt"].replace("Z", "+00:00")).year
            == current.year + years
        )
        # 过去开始时间被服务端归到执行时刻；不能声称它从申请时刻起就抑制了告警。
        assert datetime.fromisoformat(
            actual["startsAt"].replace("Z", "+00:00")
        ) > current - timedelta(seconds=1)
        assert datetime.fromisoformat(
            actual["startsAt"].replace("Z", "+00:00")
        ) <= datetime.fromisoformat(actual["updatedAt"].replace("Z", "+00:00"))
        for auth in (None, ("reader", "wrong")):
            assert (
                httpx.delete(f"{url}/api/v2/silence/{sid}", auth=auth, trust_env=False).status_code
                == 401
            )
            assert client.get(f"/api/v2/silence/{sid}").json()["status"]["state"] == "active"
        assert client.delete(f"/api/v2/silence/{sid}").status_code == 200
        actual = client.get(f"/api/v2/silence/{sid}").json()
        assert actual["id"] == sid and actual["status"]["state"] == "expired"
        # DELETE 是提前结束，记录保留；重复取消在上游幂等，但小维不能据此自动重发。
        assert client.delete(f"/api/v2/silence/{sid}").status_code == 200
        assert len(client.get("/api/v2/silences").json()) == 1
        assert (
            client.delete("/api/v2/silence/11111111-1111-4111-8111-111111111111").status_code == 404
        )


def test_official_pending_cancel_filters_and_update_branch(alertmanager_binary: Path) -> None:
    with (
        official(alertmanager_binary) as url,
        httpx.Client(base_url=url, auth=("reader", PASSWORD), trust_env=False) as client,
    ):
        sid = seed(client)
        alerts = client.get("/api/v2/alerts", params=[("filter", '"instance"="host1"')])
        assert alerts.status_code == 200 and len(alerts.json()) == 1
        assert alerts.json()[0]["status"]["silencedBy"] == [sid]
        actual = client.get(f"/api/v2/silence/{sid}").json()
        actual["comment"] = "updated outside Xiaowei"
        # POST 带 id 是更新分支；S6 的创建请求必须省略 id。
        assert client.post("/api/v2/silences", json=actual).json() == {"silenceID": sid}
        assert client.get(f"/api/v2/silence/{sid}").json()["comment"] == actual["comment"]
        current = datetime.now(UTC)
        actual.pop("id")
        actual.update(
            startsAt=(current + timedelta(hours=1)).isoformat(),
            endsAt=(current + timedelta(hours=2)).isoformat(),
        )
        pending = client.post("/api/v2/silences", json=actual).json()["silenceID"]
        assert client.get(f"/api/v2/silence/{pending}").json()["status"]["state"] == "pending"
        assert client.delete(f"/api/v2/silence/{pending}").status_code == 200
        assert client.get(f"/api/v2/silence/{pending}").json()["status"]["state"] == "expired"
        assert client.delete(f"/api/v2/silence/{sid}").status_code == 200
        assert client.get("/api/v2/alerts").json()[0]["status"]["silencedBy"] == []
