import tempfile
import unittest
from pathlib import Path
from tidalrestore.salinity import import_logger,summary
from tidalrestore.water_access import access_report


class SalinityLoggerTest(unittest.TestCase):
    def test_import_and_access(self):
        with tempfile.TemporaryDirectory() as root:
            db=Path(root)/'logger.sqlite'
            fixture=Path(__file__).resolve().parent.parent/'fixtures'/'salinity-logger-ok.json'
            self.assertEqual(import_logger(db,fixture)['samples'],2)
            self.assertTrue(import_logger(db,fixture)['replayed'])
            self.assertEqual(summary(db)[0]['peak'],120)
            self.assertEqual(access_report(db)[0]['access'],'closed')


if __name__=='__main__':unittest.main()
