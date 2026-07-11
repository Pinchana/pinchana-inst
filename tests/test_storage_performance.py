import asyncio

import pytest

from pinchana_core.storage import MediaStorage


@pytest.mark.asyncio
async def test_singleflight_shares_one_operation(tmp_path):
    storage = MediaStorage(tmp_path)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "ready"

    results = await asyncio.gather(
        *(storage.singleflight("instagram:post", operation) for _ in range(8))
    )

    assert results == ["ready"] * 8
    assert calls == 1


def test_metadata_is_replaced_atomically(tmp_path):
    storage = MediaStorage(tmp_path)
    storage.save_metadata("post", {"version": 1})
    storage.save_metadata("post", {"version": 2})

    assert storage.load_metadata("post") == {"version": 2}
    assert list((tmp_path / "post").glob("*.tmp")) == []
