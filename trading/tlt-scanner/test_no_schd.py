import contextlib
import io
import re
import unittest
from pathlib import Path

import tlt_scanner as ts


HERE = Path(__file__).resolve().parent
NEEDLE = re.compile(r"schd", re.IGNORECASE)


class NoSchdRegressionTests(unittest.TestCase):
    """SCHD was removed from this scanner. These fail if any of it comes back.

    It has returned twice already, each time through a git accident rather than
    a deliberate edit, so the guard is on the tree and on the rendered payload.
    """

    def test_no_schd_in_any_source_file(self):
        offenders = []
        for path in sorted(HERE.glob("*.py")):
            if path.name == Path(__file__).name:  # this file names it on purpose
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if NEEDLE.search(line):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "SCHD is back in the sources:\n" + "\n".join(offenders))

    def test_watchlist_is_tlt_ub_tyx_only(self):
        self.assertEqual(set(ts.TICKERS), {"TLT", "UB", "TYX"})
        self.assertEqual(set(ts.AUX_TICKERS), {"TNX"})

    def test_no_schd_in_the_rendered_dashboard(self):
        res = ts.analyze(ts.demo_frames())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ts.render_plain(res, demo=True)
        self.assertNotRegex(buf.getvalue(), NEEDLE)


if __name__ == "__main__":
    unittest.main()
