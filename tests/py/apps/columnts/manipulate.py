from datetime import date, datetime

from stdnet.utils import test

from .main import ColumnMixin


class TestManipulate(ColumnMixin, test.TestCase):

    def pop_range(self, byrank, ts, start, end, num_popped, sl, sl2=None):
        all_dates, all_fields = ts.irange()
        N = ts.size()
        fields = ts.fields()
        self.assertEqual(len(fields), 6)
        if byrank:
            dates, fields = ts.irange(start, end)
            dt, fs = ts.ipop_range(start, end)
        else:
            dates, fields = ts.range(start, end)
            dt, fs = ts.pop_range(start, end)
        self.assertEqual(len(dt), num_popped)
        self.assertEqual(ts.size(), N-num_popped)
        self.assertEqual(dates, dt)
        self.assertEqual(fields, fs)
        #
        dates = all_dates[sl]
        fields = dict(((f, all_fields[f][sl]) for f in all_fields))
        if sl2:
            dates.extend(all_dates[sl2])
            for f in fields:
                fields[f].extend(all_fields[f][sl2])
        dt, fs = ts.irange()
        self.assertEqual(len(dates), len(dt))
        self.assertEqual(dates, dt)
        for f in fields:
            self.assertEqual(len(fields[f]), len(fs[f]))
        self.assertEqual(fields, fs)
            
    def test_ipop_range_back(self):
        ts = self.create()
        self.pop_range(True, ts, -2, -1, 2, slice(0,-2))
        
    def test_ipop_range_middle(self):
        ts = self.create()
        all_dates, all_fields = ts.irange()
        self.pop_range(True, ts, -10, -5, 6, slice(0,-10), slice(-4, None))
        
    def test_ipop_range_start(self):
        ts = self.create()
        # popping the first 11 records
        self.pop_range(True, ts, 0, 10, 11, slice(11, None))
        
    def test_pop_range_back(self):
        ts = self.create()
        start, end = ts.itimes(-2)
        self.pop_range(False, ts, start, end, 2, slice(0,-2))
        
    def test_contains(self):
        ts = self.create()
        all_dates = list(ts.itimes())
        dt = all_dates[10]
        self.assertTrue(dt in ts)
        # now lets pop dt
        v = ts.pop(dt)
        self.assertEqual(len(v), 6)
        self.assertFalse(dt in ts)
        #
        dn = datetime.now()
        self.assertFalse(dn in ts)