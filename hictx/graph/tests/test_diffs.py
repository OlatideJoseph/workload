from django.test import SimpleTestCase

from hictx.graph.services import diffs

MODIFY_DIFF = """diff --git a/foo.py b/foo.py
index 111..222 100644
--- a/foo.py
+++ b/foo.py
@@ -10,3 +10,3 @@ def f():
 context line
-    bad = compute()
+    good = compute()
 trailing line
"""

NEW_FILE_DIFF = """diff --git a/new.py b/new.py
new file mode 100644
index 000..333
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+line one
+line two
"""


class ParsePreimageLinesTests(SimpleTestCase):
    def test_modified_line_recorded(self):
        [fc] = diffs.parse_preimage_lines(MODIFY_DIFF)
        self.assertEqual(fc.path, "foo.py")
        # context at 10, deleted line at 11.
        self.assertEqual(fc.preimage_lines, [11])

    def test_added_file_skipped(self):
        self.assertEqual(diffs.parse_preimage_lines(NEW_FILE_DIFF), [])

    def test_multiple_deletions_and_offsets(self):
        diff = (
            "--- a/m.py\n+++ b/m.py\n"
            "@@ -5,4 +5,2 @@\n c1\n-d1\n-d2\n c2\n"
        )
        [fc] = diffs.parse_preimage_lines(diff)
        # start 5: c1=5, d1=6, d2=7.
        self.assertEqual(fc.preimage_lines, [6, 7])
