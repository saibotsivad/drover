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
import logging
import sys
import time

import pytest

# Per-test timeout in seconds – surfaces hangs as clear failures rather
# than letting CI hit its step-level timeout with no diagnostic info.
_TEST_TIMEOUT = 10

# Enable debug logging from the executor so CI output shows agent
# lifecycle events (connect, heartbeat, shutdown, etc.).
_executor_logger = logging.getLogger("drover_executor")
_executor_logger.setLevel(logging.DEBUG)
_executor_logger.addHandler(logging.StreamHandler(sys.stderr))


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
        t0 = time.monotonic()
        print(f"\n  [conftest] START {pyfuncitem.name}", flush=True)
        try:
            loop.run_until_complete(
                asyncio.wait_for(
                    pyfuncitem.obj(**testargs), timeout=_TEST_TIMEOUT
                )
            )
            print(
                f"  [conftest] OK   {pyfuncitem.name}"
                f" ({time.monotonic() - t0:.1f}s)",
                flush=True,
            )
        except Exception as exc:
            print(
                f"  [conftest] FAIL {pyfuncitem.name}"
                f" ({time.monotonic() - t0:.1f}s): {exc!r}",
                file=sys.stderr,
                flush=True,
            )
            raise
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                asyncio.set_event_loop(None)
        return True
