import json
import unittest
from unittest.mock import patch

from plugins import drive_search


class DriveSearchTests(unittest.TestCase):
    @patch.object(drive_search, "_credentials", return_value=object())
    @patch.object(drive_search, "build")
    def test_search_returns_only_metadata_and_escapes_name(self, build, _credentials):
        files = build.return_value.files.return_value
        files.list.return_value.execute.return_value = {
            "files": [{"id": "f1", "name": "Carmine's plan", "webViewLink": "https://drive.google.com/file/f1"}],
            "nextPageToken": "next",
        }

        result = drive_search.run({"query": "Carmine's"})

        self.assertEqual(files.list.call_args.kwargs["q"],
                         "name contains 'Carmine\\'s' and trashed = false")
        self.assertEqual(files.list.call_args.kwargs["fields"],
                         "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink)")
        self.assertIn("More matches exist", result)
        self.assertEqual(json.loads(result.split("\n", 1)[1])[0]["id"], "f1")

    @patch.object(drive_search, "_credentials")
    def test_empty_query_does_not_access_drive(self, credentials):
        self.assertIn("file name", drive_search.run({"query": "  "}))
        credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
