"""The tests' temp folders are pytest's to delete, not left in %TEMP% (see conftest.py)."""
import tempfile
import unittest
from pathlib import Path


class TempFoldersTest(unittest.TestCase):
    def test_mkdtemp_lands_in_pytests_own_folder(self):
        made = Path(tempfile.mkdtemp())
        self.assertTrue(any(part.startswith("pytest-") for part in made.parts), made)


if __name__ == "__main__":
    unittest.main()
