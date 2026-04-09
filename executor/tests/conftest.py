"""Configure async test execution without pytest-asyncio.

The executor tests are run with ``-p no:asyncio -p no:anyio`` to avoid
pytest-asyncio's event loop management which hangs on Python 3.12 due
to breaking changes in the 0.23+ event loop scoping.  Async test
functions are executed via ``asyncio.run()`` instead.
"""

import asyncio

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run async test functions via asyncio.run()."""
    if asyncio.iscoroutinefunction(pyfuncitem.function):
        funcargs = pyfuncitem.funcargs
        testargs = {arg: funcargs[arg] for arg in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(pyfuncitem.obj(**testargs))
        return True
