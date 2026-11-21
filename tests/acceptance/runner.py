"""Run acceptance cases against the actual exchange executable."""

import argparse
import inspect
from pathlib import Path
import sys
import unittest

from . import cases_book, cases_flow, cases_transport
from .driver import launch

SUITES = {"book": cases_book, "flow": cases_flow, "transport": cases_transport}


def cases():
    for group, module in SUITES.items():
        if hasattr(module, "CASES"):
            entries = module.CASES
        else:
            entries = {
                name.removeprefix("case_"): (function, {})
                for name, function in inspect.getmembers(module, inspect.isfunction)
                if name.startswith("case_") and function.__module__ == module.__name__
            }
        if not entries:
            raise RuntimeError(f"acceptance suite {group} has no cases")
        for name, (function, limits) in entries.items():
            yield f"{group}.{name}", function, limits


class ExchangeCase(unittest.TestCase):
    def __init__(self, name, function, limits, executable, injector):
        super().__init__()
        self.name = name
        self.function = function
        self.limits = limits
        self.executable = executable
        self.injector = injector

    def __str__(self):
        return self.name

    def runTest(self):
        with launch(self.executable, self.limits) as exchange:
            exchange.injector = self.injector
            self.function(exchange)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange", type=Path)
    parser.add_argument("--injector", type=Path, help="defaults to ct_injector beside ct_exchange")
    parser.add_argument("--group", choices=SUITES)
    parser.add_argument("--case", help="full case name, such as transport.partial_output_retry")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    selected = [
        (name, function, limits)
        for name, function, limits in cases()
        if (args.group is None or name.startswith(args.group + "."))
        and (args.case is None or name == args.case)
    ]
    if not selected:
        parser.error("no acceptance cases selected")
    if args.list:
        for name, _, _ in selected:
            print(name)
        return 0
    if not __debug__:
        parser.error("run without -O so the acceptance checks execute")
    if args.exchange is None or not args.exchange.is_file():
        parser.error("--exchange must identify the built ct_exchange executable")
    executable = args.exchange.resolve()
    injector = args.injector.resolve() if args.injector else executable.with_name("ct_injector")
    suite = unittest.TestSuite(
        ExchangeCase(name, function, limits, executable, injector)
        for name, function, limits in selected
    )
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
