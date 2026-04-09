"""Configure async test execution without pytest-asyncio.

The executor tests are run with ``-p no:asyncio -p no:anyio`` to avoid
pytest-asyncio's event loop management which hangs on Python 3.12 due
to breaking changes in the 0.23+ event loop scoping.  Async test
functions are executed via ``loop.run_until_complete()`` instead of
``asyncio.run()`` — the latter's ``_cancel_all_tasks()`` cleanup can
hang in Python 3.12 if any internal transport tasks survive the test
coroutine.

The loop is installed via ``set_event_loop()`` so that signal-handler
wakeup FDs and any code calling ``get_event_loop()`` resolve to the
correct loop on Python 3.12+.
"""

import asyncio
import inspect

import pytest

# Per-test timeout in seconds – surfaces hangs as clear failures rather
# than letting CI hit its step-level timeout with no diagnostic info.
_TEST_TIMEOUT = 30


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run async test functions via a fresh event loop."""
    # inspect.iscoroutinefunction is the canonical check;
    # asyncio.iscoroutinefunction was deprecated in Python 3.12.
    if inspect.iscoroutinefunction(pyfuncitem.function):
        funcargs = pyfuncitem.funcargs
        testargs = {arg: funcargs[arg] for arg in pyfuncitem._fixtureinfo.argnames}
        loop = asyncio.new_event_loop()
        # Critical on Python 3.12+: set as current so signal wakeup FDs
        # and any get_event_loop() calls resolve to this loop.
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                asyncio.wait_for(
                    pyfuncitem.obj(**testargs), timeout=_TEST_TIMEOUT
                )
            )
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                asyncio.set_event_loop(None)
        return True
