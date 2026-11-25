"""The user closing NOVA's tab or window must not kill the task."""
import pytest


@pytest.mark.browser
async def test_closed_tab_is_reopened_and_closed_browser_relaunched():
    from agent.executor import _session
    from agent.state import task_store
    from browser.browser_manager import browser_manager

    record = task_store.create("recovery test")
    try:
        session = await browser_manager.ensure_session(record)
        await session.page.close()  # user closes the only tab
        recovered = await _session(record)
        assert recovered is session and not recovered.page.is_closed()
        assert any("tab was closed" in log.message for log in record.logs)

        await session.browser.close()  # user closes the whole browser
        relaunched = await _session(record)
        assert relaunched is not session and relaunched.alive and not relaunched.page.is_closed()
    finally:
        await browser_manager.shutdown()
