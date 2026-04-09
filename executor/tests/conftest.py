"""Configure async test execution without pytest-asyncio.

The executor tests are run with ``-p no:asyncio -p no:anyio`` to avoid
pytest-asyncio's event loop management which hangs on Python 3.12 due
to breaking changes in the 0.23+ event loop scoping.  Async test
functions are executed via ``loop.run_until_complete()`` instead of
``asyncio.run()`` — the latter's ``_cancel_all_tasks()`` cleanup can
hang in Python 3.12 if any internal transport tasks survive the test
coroutine.
"""

import asyncio
import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run async test functions via a fresh event loop."""
    # inspect.iscoroutinefunction is the canonical check;
    # asyncio.iscoroutinefunction was deprecated in Python 3.12.
    if inspect.iscoroutinefunction(pyfuncitem.function):
        funcargs = pyfuncitem.funcargs
        testargs = {arg: funcargs[arg] for arg in pyfuncitem._fixtureinfo.argnames}
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(pyfuncitem.obj(**testargs))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
        return True
